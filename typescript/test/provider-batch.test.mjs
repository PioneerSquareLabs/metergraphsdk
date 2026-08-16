import assert from "node:assert/strict";
import test from "node:test";

import {
  createAnthropicBatchAdapter,
  createGoogleBatchAdapter,
  createOpenAIBatchAdapter,
} from "../dist/provider-batch.js";

const REQUEST = { model: "gpt-5-mini", input: "hello" };

function fakeOpenAIClient({
  batchStatus = "completed",
  outputFileId = "file_out_1",
  responseBody = { id: "resp_1", model: "gpt-5-mini", output: [] },
  statusCode = 200,
} = {}) {
  const calls = { filesCreate: [], batchesCreate: [], batchesRetrieve: [], filesContent: [], responsesCreate: [] };
  let uploadedCustomId;
  const client = {
    files: {
      async create(params) {
        calls.filesCreate.push(params);
        const text = await params.file.text();
        uploadedCustomId = JSON.parse(text.trim()).custom_id;
        return { id: "file_in_1" };
      },
      async content(fileId) {
        calls.filesContent.push(fileId);
        return {
          async text() {
            return `${JSON.stringify({
              custom_id: uploadedCustomId,
              response: { status_code: statusCode, body: responseBody },
            })}\n`;
          },
        };
      },
    },
    batches: {
      async create(params) {
        calls.batchesCreate.push(params);
        return { id: "batch_1", status: "validating" };
      },
      async retrieve(batchId) {
        calls.batchesRetrieve.push(batchId);
        return { id: batchId, status: batchStatus, output_file_id: outputFileId };
      },
    },
    responses: {
      async create(request) {
        calls.responsesCreate.push(request);
        return { id: "resp_direct_1", model: request.model, output: [] };
      },
    },
  };
  return { client, calls, uploadedCustomId: () => uploadedCustomId };
}

test("submitOne uploads exactly one JSONL line targeting /v1/responses, then creates the batch", async () => {
  const { client, calls } = fakeOpenAIClient();
  const adapter = createOpenAIBatchAdapter(client);

  const handle = await adapter.submitOne(REQUEST);

  assert.equal(calls.filesCreate.length, 1);
  assert.equal(calls.filesCreate[0].purpose, "batch");
  const uploadedText = await calls.filesCreate[0].file.text();
  const lines = uploadedText.split("\n").filter((line) => line.trim().length > 0);
  assert.equal(lines.length, 1);
  const line = JSON.parse(lines[0]);
  assert.equal(line.method, "POST");
  assert.equal(line.url, "/v1/responses");
  assert.deepEqual(line.body, REQUEST);
  assert.equal(typeof line.custom_id, "string");
  assert.ok(line.custom_id.length > 0);

  assert.equal(calls.batchesCreate.length, 1);
  assert.equal(calls.batchesCreate[0].input_file_id, "file_in_1");
  assert.equal(calls.batchesCreate[0].endpoint, "/v1/responses");
  assert.equal(calls.batchesCreate[0].completion_window, "24h");
  assert.equal(handle.providerBatchId, "batch_1");
});

const POLL_STATUS_CASES = [
  ["validating", "pending"],
  ["in_progress", "pending"],
  ["finalizing", "pending"],
  ["cancelling", "pending"],
  ["completed", "completed"],
  ["expired", "expired"],
  ["failed", "failed"],
  ["cancelled", "failed"],
  ["some_future_status_openai_might_add", "pending"],
];

for (const [wireStatus, normalized] of POLL_STATUS_CASES) {
  test(`poll normalizes OpenAI batch status "${wireStatus}" to "${normalized}"`, async () => {
    const { client } = fakeOpenAIClient({ batchStatus: wireStatus });
    const adapter = createOpenAIBatchAdapter(client);
    const handle = await adapter.submitOne(REQUEST);

    const result = await adapter.poll(handle);

    assert.equal(result.status, normalized);
  });
}

test("readResult fetches the output file and returns the matching line's response body", async () => {
  const responseBody = { id: "resp_1", model: "gpt-5-mini", output: [] };
  const { client, calls } = fakeOpenAIClient({ responseBody });
  const adapter = createOpenAIBatchAdapter(client);
  const handle = await adapter.submitOne(REQUEST);

  const outcome = await adapter.readResult(handle);

  assert.equal(calls.filesContent.length, 1);
  assert.equal(calls.filesContent[0], "file_out_1");
  assert.deepEqual(outcome.result, responseBody);
  assert.equal(outcome.containedToolCallPlan, false);
});

test("readResult reports containedToolCallPlan when the output includes a function_call item", async () => {
  const responseBody = {
    id: "resp_1",
    output: [{ type: "function_call", name: "lookup", call_id: "call_1", arguments: "{}" }],
  };
  const { client } = fakeOpenAIClient({ responseBody });
  const adapter = createOpenAIBatchAdapter(client);
  const handle = await adapter.submitOne(REQUEST);

  const outcome = await adapter.readResult(handle);

  assert.equal(outcome.containedToolCallPlan, true);
});

test("readResult never exposes the batch's raw error body — a per-item error surfaces as a bounded failure", async () => {
  const { client } = fakeOpenAIClient({
    statusCode: 400,
    responseBody: { error: { message: "some verbose provider error text that must never leak" } },
  });
  const adapter = createOpenAIBatchAdapter(client);
  const handle = await adapter.submitOne(REQUEST);

  await assert.rejects(() => adapter.readResult(handle), (error) => {
    assert.ok(!String(error.message).includes("must never leak"));
    return true;
  });
});

test("direct calls responses.create with the exact caller-supplied request", async () => {
  const { client, calls } = fakeOpenAIClient();
  const adapter = createOpenAIBatchAdapter(client);

  const outcome = await adapter.direct(REQUEST);

  assert.equal(calls.responsesCreate.length, 1);
  assert.deepEqual(calls.responsesCreate[0], REQUEST);
  assert.equal(outcome.result.id, "resp_direct_1");
  assert.equal(outcome.containedToolCallPlan, false);
});

test("direct reports containedToolCallPlan from the immediate response too", async () => {
  const client = fakeOpenAIClient().client;
  client.responses.create = async () => ({
    id: "resp_direct_1",
    output: [{ type: "function_call", name: "lookup", call_id: "call_1", arguments: "{}" }],
  });
  const adapter = createOpenAIBatchAdapter(client);

  const outcome = await adapter.direct(REQUEST);

  assert.equal(outcome.containedToolCallPlan, true);
});

test("eligibility rejects a streaming request", () => {
  const { client } = fakeOpenAIClient();
  const adapter = createOpenAIBatchAdapter(client);

  const result = adapter.eligibility({ ...REQUEST, stream: true });

  assert.equal(result.eligible, false);
  assert.match(result.reason, /stream/i);
});

test("eligibility accepts a plain non-streaming Responses-shaped request", () => {
  const { client } = fakeOpenAIClient();
  const adapter = createOpenAIBatchAdapter(client);

  assert.equal(adapter.eligibility(REQUEST).eligible, true);
});

// ---------- Anthropic ----------

const ANTHROPIC_REQUEST = { model: "claude-opus-4-8", max_tokens: 256, messages: [{ role: "user", content: "hello" }] };

function fakeAnthropicClient({
  processingStatus = "ended",
  requestCounts = { processing: 0, succeeded: 1, errored: 0, canceled: 0, expired: 0 },
  resultType = "succeeded",
  messageBody = { id: "msg_1", role: "assistant", content: [{ type: "text", text: "hi" }] },
  errorBody = { type: "invalid_request", message: "some verbose provider error text that must never leak" },
  omitResultItem = false,
} = {}) {
  const calls = { batchesCreate: [], batchesRetrieve: [], batchesResults: [], messagesCreate: [] };
  let submittedCustomId;
  const client = {
    messages: {
      async create(request) {
        calls.messagesCreate.push(request);
        return { id: "msg_direct_1", role: "assistant", content: [] };
      },
      batches: {
        async create(params) {
          calls.batchesCreate.push(params);
          submittedCustomId = params.requests[0].custom_id;
          return { id: "batch_1", processing_status: "in_progress" };
        },
        async retrieve(batchId) {
          calls.batchesRetrieve.push(batchId);
          return { id: batchId, processing_status: processingStatus, request_counts: requestCounts };
        },
        results(batchId) {
          calls.batchesResults.push(batchId);
          const item = resultType === "succeeded"
            ? { custom_id: submittedCustomId, result: { type: "succeeded", message: messageBody } }
            : { custom_id: submittedCustomId, result: { type: resultType, error: errorBody } };
          return {
            async *[Symbol.asyncIterator]() {
              if (!omitResultItem) yield item;
            },
          };
        },
      },
    },
  };
  return { client, calls, submittedCustomId: () => submittedCustomId };
}

test("Anthropic submitOne creates a batch with exactly one request carrying a fresh custom_id", async () => {
  const { client, calls } = fakeAnthropicClient();
  const adapter = createAnthropicBatchAdapter(client);

  const handle = await adapter.submitOne(ANTHROPIC_REQUEST);

  assert.equal(calls.batchesCreate.length, 1);
  const { requests } = calls.batchesCreate[0];
  assert.equal(requests.length, 1);
  assert.equal(typeof requests[0].custom_id, "string");
  assert.ok(requests[0].custom_id.length > 0);
  assert.deepEqual(requests[0].params, ANTHROPIC_REQUEST);
  assert.equal(handle.providerBatchId, "batch_1");
});

const ANTHROPIC_POLL_CASES = [
  ["in_progress", { succeeded: 0 }, "pending"],
  ["canceling", { succeeded: 0 }, "pending"],
  ["ended", { succeeded: 1, errored: 0, canceled: 0, expired: 0 }, "completed"],
  ["ended", { succeeded: 0, errored: 1, canceled: 0, expired: 0 }, "failed"],
  ["ended", { succeeded: 0, errored: 0, canceled: 1, expired: 0 }, "failed"],
  ["ended", { succeeded: 0, errored: 0, canceled: 0, expired: 1 }, "expired"],
  ["ended", {}, "failed"],
  ["some_future_status_anthropic_might_add", { succeeded: 1 }, "pending"],
];

for (const [wireStatus, requestCounts, normalized] of ANTHROPIC_POLL_CASES) {
  test(`Anthropic poll normalizes processing_status "${wireStatus}" with counts ${JSON.stringify(requestCounts)} to "${normalized}"`, async () => {
    const { client } = fakeAnthropicClient({ processingStatus: wireStatus, requestCounts });
    const adapter = createAnthropicBatchAdapter(client);
    const handle = await adapter.submitOne(ANTHROPIC_REQUEST);

    const result = await adapter.poll(handle);

    assert.equal(result.status, normalized);
  });
}

test("Anthropic readResult iterates batch results and returns the matching item's message", async () => {
  const messageBody = { id: "msg_1", role: "assistant", content: [{ type: "text", text: "hi" }] };
  const { client, calls } = fakeAnthropicClient({ messageBody });
  const adapter = createAnthropicBatchAdapter(client);
  const handle = await adapter.submitOne(ANTHROPIC_REQUEST);

  const outcome = await adapter.readResult(handle);

  assert.equal(calls.batchesResults.length, 1);
  assert.equal(calls.batchesResults[0], "batch_1");
  assert.deepEqual(outcome.result, messageBody);
  assert.equal(outcome.containedToolCallPlan, false);
});

test("Anthropic readResult reports containedToolCallPlan when the message includes a tool_use block", async () => {
  const messageBody = {
    id: "msg_1",
    role: "assistant",
    content: [{ type: "tool_use", id: "toolu_1", name: "lookup", input: { q: "x" } }],
  };
  const { client } = fakeAnthropicClient({ messageBody });
  const adapter = createAnthropicBatchAdapter(client);
  const handle = await adapter.submitOne(ANTHROPIC_REQUEST);

  const outcome = await adapter.readResult(handle);

  assert.equal(outcome.containedToolCallPlan, true);
});

test("Anthropic readResult never exposes the item's raw error body — an errored result surfaces as a bounded failure", async () => {
  const { client } = fakeAnthropicClient({ resultType: "errored" });
  const adapter = createAnthropicBatchAdapter(client);
  const handle = await adapter.submitOne(ANTHROPIC_REQUEST);

  await assert.rejects(() => adapter.readResult(handle), (error) => {
    assert.ok(!String(error.message).includes("must never leak"));
    return true;
  });
});

test("Anthropic readResult throws a bounded error when no result item matches our custom_id", async () => {
  const { client } = fakeAnthropicClient({ omitResultItem: true });
  const adapter = createAnthropicBatchAdapter(client);
  const handle = await adapter.submitOne(ANTHROPIC_REQUEST);

  await assert.rejects(() => adapter.readResult(handle), /did not contain|no matching/i);
});

test("Anthropic direct calls messages.create with the exact caller-supplied request", async () => {
  const { client, calls } = fakeAnthropicClient();
  const adapter = createAnthropicBatchAdapter(client);

  const outcome = await adapter.direct(ANTHROPIC_REQUEST);

  assert.equal(calls.messagesCreate.length, 1);
  assert.deepEqual(calls.messagesCreate[0], ANTHROPIC_REQUEST);
  assert.equal(outcome.result.id, "msg_direct_1");
  assert.equal(outcome.containedToolCallPlan, false);
});

test("Anthropic direct reports containedToolCallPlan from the immediate response too", async () => {
  const { client } = fakeAnthropicClient();
  client.messages.create = async () => ({
    id: "msg_direct_1",
    role: "assistant",
    content: [{ type: "tool_use", id: "toolu_1", name: "lookup", input: {} }],
  });
  const adapter = createAnthropicBatchAdapter(client);

  const outcome = await adapter.direct(ANTHROPIC_REQUEST);

  assert.equal(outcome.containedToolCallPlan, true);
});

test("Anthropic eligibility rejects a streaming request", () => {
  const { client } = fakeAnthropicClient();
  const adapter = createAnthropicBatchAdapter(client);

  const result = adapter.eligibility({ ...ANTHROPIC_REQUEST, stream: true });

  assert.equal(result.eligible, false);
  assert.match(result.reason, /stream/i);
});

test("Anthropic eligibility accepts a plain non-streaming Messages-shaped request", () => {
  const { client } = fakeAnthropicClient();
  const adapter = createAnthropicBatchAdapter(client);

  assert.equal(adapter.eligibility(ANTHROPIC_REQUEST).eligible, true);
});

// ---------- Google ----------

const GOOGLE_REQUEST = { model: "gemini-2.5-flash", contents: [{ role: "user", parts: [{ text: "hello" }] }] };

function fakeGoogleClient({
  state = "JOB_STATE_SUCCEEDED",
  inlinedResponses = [{ response: { candidates: [{ content: { parts: [{ text: "hi" }] } }] } }],
  generateContentResponse = { candidates: [{ content: { parts: [{ text: "hi" }] } }] },
  omitDest = false,
} = {}) {
  const calls = { batchesCreate: [], batchesGet: [], generateContent: [] };
  const client = {
    models: {
      async generateContent(request) {
        calls.generateContent.push(request);
        return generateContentResponse;
      },
    },
    batches: {
      async create(params) {
        calls.batchesCreate.push(params);
        return { name: "batches/batch_1", state: "JOB_STATE_PENDING" };
      },
      async get(params) {
        calls.batchesGet.push(params);
        return {
          name: params.name,
          state,
          ...(omitDest ? {} : { dest: { inlinedResponses } }),
        };
      },
    },
  };
  return { client, calls };
}

test("Google submitOne puts model on the outer batch job and omits it from the inlined request", async () => {
  const { client, calls } = fakeGoogleClient();
  const adapter = createGoogleBatchAdapter(client);

  const handle = await adapter.submitOne(GOOGLE_REQUEST);

  assert.equal(calls.batchesCreate.length, 1);
  assert.equal(calls.batchesCreate[0].model, "gemini-2.5-flash");
  assert.equal(calls.batchesCreate[0].src.inlinedRequests.length, 1);
  assert.equal("model" in calls.batchesCreate[0].src.inlinedRequests[0], false);
  assert.deepEqual(calls.batchesCreate[0].src.inlinedRequests[0], { contents: GOOGLE_REQUEST.contents });
  assert.equal(handle.providerBatchId, "batches/batch_1");
});

const GOOGLE_INVALID_MODELS = [undefined, null, "", "   ", 42, {}];

for (const invalidModel of GOOGLE_INVALID_MODELS) {
  test(`Google submitOne rejects a non-blank-string model (${JSON.stringify(invalidModel)}) before any provider call`, async () => {
    const { client, calls } = fakeGoogleClient();
    const adapter = createGoogleBatchAdapter(client);

    await assert.rejects(
      () => adapter.submitOne({ ...GOOGLE_REQUEST, model: invalidModel }),
    );
    assert.equal(calls.batchesCreate.length, 0);
  });
}

const GOOGLE_POLL_CASES = [
  ["JOB_STATE_PENDING", "pending"],
  ["JOB_STATE_RUNNING", "pending"],
  ["JOB_STATE_QUEUED", "pending"],
  ["JOB_STATE_SUCCEEDED", "completed"],
  ["JOB_STATE_FAILED", "failed"],
  ["JOB_STATE_CANCELLED", "failed"],
  ["JOB_STATE_EXPIRED", "expired"],
  ["JOB_STATE_SOME_FUTURE_STATE", "pending"],
];

for (const [wireState, normalized] of GOOGLE_POLL_CASES) {
  test(`Google poll normalizes job state "${wireState}" to "${normalized}"`, async () => {
    const { client } = fakeGoogleClient({ state: wireState });
    const adapter = createGoogleBatchAdapter(client);
    const handle = await adapter.submitOne(GOOGLE_REQUEST);

    const result = await adapter.poll(handle);

    assert.equal(result.status, normalized);
  });
}

test("Google readResult fetches the job and returns the sole inlined response", async () => {
  const response = { candidates: [{ content: { parts: [{ text: "hi" }] } }] };
  const { client, calls } = fakeGoogleClient({ inlinedResponses: [{ response }] });
  const adapter = createGoogleBatchAdapter(client);
  const handle = await adapter.submitOne(GOOGLE_REQUEST);

  const outcome = await adapter.readResult(handle);

  assert.equal(calls.batchesGet.length, 1);
  assert.deepEqual(outcome.result, response);
  assert.equal(outcome.containedToolCallPlan, false);
});

test("Google readResult reports containedToolCallPlan when a part includes a functionCall", async () => {
  const response = { candidates: [{ content: { parts: [{ functionCall: { name: "lookup", args: {} } }] } }] };
  const { client } = fakeGoogleClient({ inlinedResponses: [{ response }] });
  const adapter = createGoogleBatchAdapter(client);
  const handle = await adapter.submitOne(GOOGLE_REQUEST);

  const outcome = await adapter.readResult(handle);

  assert.equal(outcome.containedToolCallPlan, true);
});

test("Google readResult never exposes the item's raw error body — an item-level error surfaces as a bounded failure", async () => {
  const { client } = fakeGoogleClient({
    inlinedResponses: [{ error: { code: 400, message: "some verbose provider error text that must never leak" } }],
  });
  const adapter = createGoogleBatchAdapter(client);
  const handle = await adapter.submitOne(GOOGLE_REQUEST);

  await assert.rejects(() => adapter.readResult(handle), (error) => {
    assert.ok(!String(error.message).includes("must never leak"));
    return true;
  });
});

test("Google readResult throws a bounded error when the job has no inlined responses at all", async () => {
  const { client } = fakeGoogleClient({ omitDest: true });
  const adapter = createGoogleBatchAdapter(client);
  const handle = await adapter.submitOne(GOOGLE_REQUEST);

  await assert.rejects(() => adapter.readResult(handle), /no inline|no response/i);
});

test("Google direct calls models.generateContent with the exact caller-supplied request", async () => {
  const { client, calls } = fakeGoogleClient();
  const adapter = createGoogleBatchAdapter(client);

  const outcome = await adapter.direct(GOOGLE_REQUEST);

  assert.equal(calls.generateContent.length, 1);
  assert.deepEqual(calls.generateContent[0], GOOGLE_REQUEST);
  assert.equal(outcome.containedToolCallPlan, false);
});

test("Google direct reports containedToolCallPlan from the immediate response too", async () => {
  const { client } = fakeGoogleClient({
    generateContentResponse: { candidates: [{ content: { parts: [{ functionCall: { name: "lookup", args: {} } }] } }] },
  });
  const adapter = createGoogleBatchAdapter(client);

  const outcome = await adapter.direct(GOOGLE_REQUEST);

  assert.equal(outcome.containedToolCallPlan, true);
});

test("Google eligibility rejects a streaming request", () => {
  const { client } = fakeGoogleClient();
  const adapter = createGoogleBatchAdapter(client);

  const result = adapter.eligibility({ ...GOOGLE_REQUEST, stream: true });

  assert.equal(result.eligible, false);
  assert.match(result.reason, /stream/i);
});

test("Google eligibility accepts a plain non-streaming generateContent-shaped request", () => {
  const { client } = fakeGoogleClient();
  const adapter = createGoogleBatchAdapter(client);

  assert.equal(adapter.eligibility(GOOGLE_REQUEST).eligible, true);
});
