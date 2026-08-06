import assert from "node:assert/strict";
import test from "node:test";

import { CaptureRuntime } from "../dist/capture.js";
import { createVercelAISDKMiddleware } from "../dist/vercel-ai.js";
import { setCaptureRuntime, wrap } from "../dist/wrap.js";


function stubRuntime(rows, options = {}) {
  return new CaptureRuntime(
    { enqueue(row) { rows.push(row); return true; } },
    {
      captureText: true,
      appRoot: "",
      skipFrames: [],
      textMaxBytes: 100 * 1024,
      ...options,
    },
  );
}


function capturedResponse(row) {
  return JSON.parse(row.response_text);
}


async function readAll(stream) {
  const reader = stream.getReader();
  const parts = [];
  while (true) {
    const next = await reader.read();
    if (next.done) return parts;
    parts.push(next.value);
  }
}


test("Vercel middleware versions and disabled capture are exact pass-throughs", async (t) => {
  setCaptureRuntime();
  t.after(() => setCaptureRuntime());
  assert.equal(createVercelAISDKMiddleware().specificationVersion, "v3");
  assert.equal(createVercelAISDKMiddleware({ specificationVersion: "v2" }).specificationVersion, "v2");
  assert.equal(createVercelAISDKMiddleware({ specificationVersion: "v4" }).specificationVersion, "v4");
  assert.equal(
    createVercelAISDKMiddleware({ aiSdkVersion: 5 }).specificationVersion,
    "v2",
  );
  assert.throws(
    () => createVercelAISDKMiddleware({
      aiSdkVersion: 5,
      specificationVersion: "v3",
    }),
    /aiSdkVersion.*specificationVersion/,
  );

  const generated = { content: [{ type: "text", text: "unchanged" }] };
  let generateCalls = 0;
  const result = await createVercelAISDKMiddleware().wrapGenerate({
    model: {},
    params: {},
    doGenerate: () => {
      generateCalls += 1;
      return generated;
    },
  });
  assert.equal(result, generated);
  assert.equal(generateCalls, 1);

  const streamed = { stream: new ReadableStream() };
  let streamCalls = 0;
  const streamResult = await createVercelAISDKMiddleware().wrapStream({
    model: {},
    params: {},
    doStream: () => {
      streamCalls += 1;
      return streamed;
    },
  });
  assert.equal(streamResult, streamed);
  assert.equal(streamCalls, 1);
});


test("Vercel provider aliases normalize and request capture excludes transport secrets", async (t) => {
  const rows = [];
  setCaptureRuntime(stubRuntime(rows));
  t.after(() => setCaptureRuntime());
  const middleware = createVercelAISDKMiddleware({ specificationVersion: "v4" });
  const cases = [
    ["gateway", "anthropic/claude-sonnet", "anthropic"],
    ["vercel-ai-gateway", "amazon/nova-pro", "amazon"],
    ["amazon-bedrock", "nova-pro", "bedrock"],
    ["google.genai", "gemini-3", "google"],
    ["openai.responses", "gpt-5", "openai"],
    ["xai", "grok-4", "xai"],
  ];

  for (const [provider, modelId] of cases) {
    const response = {
      content: [{ type: "text", text: "ok" }],
      usage: { inputTokens: "4", outputTokens: 2 },
    };
    const actual = await middleware.wrapGenerate({
      model: { provider, modelId },
      params: {
        prompt: [{ role: "user", content: "safe prompt" }],
        temperature: 0,
        headers: { authorization: "Bearer header-secret" },
        providerOptions: { apiKey: "provider-secret" },
        abortSignal: { secret: "abort-secret" },
        callback: () => "callback-secret",
      },
      doGenerate: () => response,
    });
    assert.equal(actual, response);
  }

  assert.deepEqual(rows.map((row) => row.provider), cases.map((item) => item[2]));
  for (const row of rows) {
    assert.equal(row.input_tokens, 4);
    assert.equal(row.output_tokens, 2);
    assert.doesNotMatch(
      row.request_json,
      /header-secret|provider-secret|abort-secret|callback-secret|authorization|apiKey/,
    );
    const request = JSON.parse(row.request_json);
    assert.equal(request.temperature, 0);
    assert.equal(request.messages[0].content, "safe prompt");
    assert.equal("headers" in request, false);
    assert.equal("providerOptions" in request, false);
    assert.equal("abortSignal" in request, false);
  }
});


test("malformed usage never becomes a token count", async (t) => {
  const rows = [];
  setCaptureRuntime(stubRuntime(rows));
  t.after(() => setCaptureRuntime());
  await createVercelAISDKMiddleware().wrapGenerate({
    model: { provider: "gateway", modelId: "openai/gpt-5" },
    params: { prompt: [] },
    doGenerate: () => ({
      content: [],
      usage: {
        inputTokens: true,
        outputTokens: -1,
        inputTokenDetails: { cacheReadTokens: 1.5 },
        outputTokenDetails: { reasoningTokens: Number.POSITIVE_INFINITY },
        raw: { cache_creation_input_tokens: Number.NaN },
      },
    }),
  });

  for (const field of [
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "reasoning_tokens",
  ]) {
    assert.equal(rows[0][field], undefined, field);
  }
});


test("Vercel adapter is fail-open when capture start or finish throws", async (t) => {
  t.after(() => setCaptureRuntime());
  const middleware = createVercelAISDKMiddleware();
  const generated = { content: [{ type: "text", text: "provider result" }] };

  setCaptureRuntime({
    start() { throw new Error("capture start failed"); },
    finish() { assert.fail("finish must not run without state"); },
  });
  assert.equal(await middleware.wrapGenerate({
    model: {},
    params: {},
    doGenerate: () => generated,
  }), generated);

  setCaptureRuntime({
    start() { return { done: false }; },
    finish() { throw new Error("capture finish failed"); },
  });
  assert.equal(await middleware.wrapGenerate({
    model: {},
    params: {},
    doGenerate: () => generated,
  }), generated);

  const failure = new TypeError("provider failed");
  await assert.rejects(
    middleware.wrapGenerate({
      model: {},
      params: {},
      doGenerate() { throw failure; },
    }),
    (error) => error === failure,
  );
});


test("non-readable and getReader-failing streams capture exactly once", async (t) => {
  const rows = [];
  setCaptureRuntime(stubRuntime(rows));
  t.after(() => setCaptureRuntime());
  const middleware = createVercelAISDKMiddleware();

  const nonReadable = { stream: { reason: "not a web stream" } };
  assert.equal(await middleware.wrapStream({
    model: { provider: "openai", modelId: "m" },
    params: { prompt: [] },
    doStream: () => nonReadable,
  }), nonReadable);

  const lockFailure = new Error("stream already locked");
  const locked = {
    stream: {
      getReader() { throw lockFailure; },
    },
  };
  assert.equal(await middleware.wrapStream({
    model: { provider: "openai", modelId: "m" },
    params: { prompt: [] },
    doStream: () => locked,
  }), locked);

  assert.equal(rows.length, 2);
  assert.equal(rows[0].status, "success");
  assert.equal(rows[0].stream, true);
  assert.equal(rows[1].status, "error");
  assert.equal(rows[1].error_type, "Error");
});


test("stream read rejection and cancellation preserve provider behavior and terminal status", async (t) => {
  const rows = [];
  setCaptureRuntime(stubRuntime(rows));
  t.after(() => setCaptureRuntime());
  const middleware = createVercelAISDKMiddleware();
  const readFailure = new RangeError("reader failed");

  const failed = await middleware.wrapStream({
    model: { provider: "openai", modelId: "m" },
    params: { prompt: [] },
    doStream: () => ({
      stream: {
        getReader() {
          return {
            read() { return Promise.reject(readFailure); },
            cancel() { return Promise.resolve(); },
          };
        },
      },
    }),
  });
  await assert.rejects(failed.stream.getReader().read(), (error) => error === readFailure);

  let cancelReason;
  const cancellable = await middleware.wrapStream({
    model: { provider: "anthropic", modelId: "claude" },
    params: { prompt: [] },
    doStream: () => ({
      stream: {
        getReader() {
          return {
            read() { return new Promise(() => {}); },
            cancel(reason) {
              cancelReason = reason;
              return Promise.resolve();
            },
          };
        },
      },
    }),
  });
  await cancellable.stream.getReader().cancel("caller stopped");

  assert.equal(cancelReason, "caller stopped");
  assert.equal(rows.length, 2);
  assert.equal(rows[0].status, "error");
  assert.equal(rows[0].error_type, "RangeError");
  assert.equal(rows[1].status, "abandoned");
});


test("tool-only and in-band error streams capture TTFT, tools, metadata, and errors", async (t) => {
  const rows = [];
  setCaptureRuntime(stubRuntime(rows));
  t.after(() => setCaptureRuntime());
  const middleware = createVercelAISDKMiddleware({ specificationVersion: "v4" });

  const toolParts = [
    { type: "response-metadata", id: "meta-id", modelId: "claude" },
    {
      type: "tool-call",
      toolCallId: "call-1",
      toolName: "lookup",
      input: "{not-json",
    },
    {
      type: "tool-error",
      toolCallId: "call-1",
      toolName: "lookup",
      error: { message: "tool failed" },
    },
    {
      type: "finish",
      finishReason: { unified: "tool-calls", raw: "tool_use" },
      usage: { inputTokens: 8, outputTokens: 3 },
    },
  ];
  const toolResult = await middleware.wrapStream({
    model: { provider: "gateway", modelId: "anthropic/claude" },
    params: { prompt: [] },
    doStream: () => ({
      response: { id: "result-id" },
      stream: new ReadableStream({
        start(controller) {
          for (const part of toolParts) controller.enqueue(part);
          controller.close();
        },
      }),
    }),
  });
  assert.deepEqual(await readAll(toolResult.stream), toolParts);

  const streamFailure = new SyntaxError("bad provider event");
  const errorPart = { type: "error", error: streamFailure };
  const errorResult = await middleware.wrapStream({
    model: { provider: "openai", modelId: "gpt" },
    params: { prompt: [] },
    doStream: () => ({
      stream: new ReadableStream({
        start(controller) {
          controller.enqueue(errorPart);
          controller.close();
        },
      }),
    }),
  });
  assert.deepEqual(await readAll(errorResult.stream), [errorPart]);

  assert.equal(rows.length, 2);
  assert.ok(rows[0].ttft_ms >= 0);
  assert.equal(rows[0].request_id, "result-id");
  assert.equal(rows[0].input_tokens, 8);
  assert.deepEqual(rows[0].tool_names, ["lookup"]);
  assert.deepEqual(rows[0].tool_calls[0], {
    call_id: "call-1",
    name: "lookup",
    arguments: "{not-json",
    result: { message: "tool failed" },
    status: "error",
    idempotency: "non_idempotent",
  });
  assert.equal(rows[1].status, "error");
  assert.equal(rows[1].error_type, "SyntaxError");
  assert.deepEqual(capturedResponse(rows[1]).error, {
    type: "SyntaxError",
    message: "bad provider event",
  });
});


test("provider wrapper sets TTFT for a tool-only stream and captures iterator errors", async (t) => {
  const rows = [];
  setCaptureRuntime(stubRuntime(rows));
  t.after(() => setCaptureRuntime());

  const iteratorFailure = new URIError("provider iterator failed");
  const client = wrap({
    messages: {
      stream(request) {
        if (request.model === "reasoning-only") {
          return {
            async *[Symbol.asyncIterator]() {
              yield {
                type: "content_block_delta",
                delta: { type: "thinking_delta", thinking: "reasoning" },
              };
            },
          };
        }
        return {
          async *[Symbol.asyncIterator]() {
            yield {
              type: "content_block_start",
              index: 0,
              content_block: {
                type: "tool_use",
                id: "tool-1",
                name: "lookup",
                input: {},
              },
            };
            yield {
              type: "content_block_delta",
              index: 0,
              delta: { type: "input_json_delta", partial_json: '{"id":"1"}' },
            };
            throw iteratorFailure;
          },
        };
      },
    },
  }, "anthropic");

  const reasoningStream = client.messages.stream({
    model: "reasoning-only",
    messages: [],
  });
  for await (const _part of reasoningStream) { /* consume */ }

  const stream = client.messages.stream({ model: "claude", messages: [] });
  await assert.rejects(async () => {
    for await (const _part of stream) { /* consume */ }
  }, (error) => error === iteratorFailure);

  assert.equal(rows.length, 2);
  assert.ok(rows[0].ttft_ms >= 0);
  assert.equal(rows[0].status, "success");
  assert.ok(rows[1].ttft_ms >= 0);
  assert.equal(rows[1].status, "error");
  assert.equal(rows[1].error_type, "URIError");
  assert.deepEqual(rows[1].tool_names, ["lookup"]);
  assert.deepEqual(rows[1].tool_calls[0].arguments, { id: "1" });
});
