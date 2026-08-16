import assert from "node:assert/strict";
import test from "node:test";

import { createOpenAIBatchAdapter } from "../dist/provider-batch.js";

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
