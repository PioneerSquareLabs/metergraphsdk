// Behavioral parity suite: what an application observes must be unchanged by
// instrumentation. Each case drives a protocol double through wrap() or the
// Vercel AI SDK middleware and asserts the exact result, error, or chunk
// references pass through, plus the lifecycle invariant that a single call
// enqueues at most one row. These are parity/lifecycle characterizations of
// behavior that already works; they do not exercise fail-open fault handling.

import assert from "node:assert/strict";
import test from "node:test";

import { createVercelAISDKMiddleware } from "../dist/vercel-ai.js";
import {
  asyncIterableStream,
  assertSameSequence,
  drainIterable,
  drainReadable,
  installRuntime,
  readerFromParts,
  seamDouble,
  streamResultFromReader,
} from "./support/instrumentation-doubles.mjs";

function capturedResponse(row) {
  return JSON.parse(row.response_text);
}

// --- Direct provider wrapping (wrap() / patch seams) ------------------------

test("wrap returns a synchronous provider result by identity and keeps it synchronous", (t) => {
  const rows = installRuntime(t);
  const result = { id: "sync-1", usage: { input_tokens: 1, output_tokens: 1 } };
  const { call } = seamDouble({
    provider: "openai",
    path: "responses",
    method: "create",
    impl: () => result,
  });

  const returned = call({ model: "m" });

  assert.equal(returned, result);
  assert.equal(typeof returned?.then, "undefined");
  assert.equal(rows.length, 1);
  assert.equal(rows[0].endpoint, "responses");
});

test("wrap fulfills a promise with the provider result by identity", async (t) => {
  const rows = installRuntime(t);
  const result = { id: "async-1", usage: { input_tokens: 1, output_tokens: 1 } };
  const { call } = seamDouble({
    provider: "openai",
    path: "responses",
    method: "create",
    impl: async () => result,
  });

  assert.equal(await call({ model: "m" }), result);
  assert.equal(rows.length, 1);
});

test("wrap rejects with the provider error by identity", async (t) => {
  const rows = installRuntime(t);
  const failure = new TypeError("provider rejected");
  const { call } = seamDouble({
    provider: "openai",
    path: "responses",
    method: "create",
    impl: async () => { throw failure; },
  });

  await assert.rejects(call({ model: "m" }), (error) => error === failure);
  assert.equal(rows.length, 1);
  assert.equal(rows[0].error, true);
  assert.equal(rows[0].error_type, "TypeError");
});

test("wrap rethrows a synchronous provider error by identity", (t) => {
  const rows = installRuntime(t);
  const failure = new RangeError("provider threw synchronously");
  const { call } = seamDouble({
    provider: "openai",
    path: "responses",
    method: "create",
    impl: () => { throw failure; },
  });

  assert.throws(() => call({ model: "m" }), (error) => error === failure);
  assert.equal(rows.length, 1);
  assert.equal(rows[0].error, true);
  assert.equal(rows[0].error_type, "RangeError");
});

test("wrap streams async-iterable chunks by identity and order", async (t) => {
  const rows = installRuntime(t);
  const chunks = [
    { type: "content_block_delta", delta: { text: "he" } },
    { type: "content_block_delta", delta: { text: "llo" } },
  ];
  const finalMessage = { content: [{ type: "text", text: "hello" }], usage: { input_tokens: 2, output_tokens: 1 } };
  const { call } = seamDouble({
    provider: "anthropic",
    path: "messages",
    method: "stream",
    impl: () => asyncIterableStream(chunks, { finalMessage }),
  });

  const observed = await drainIterable(call({ model: "claude", messages: [] }));

  assertSameSequence(observed, chunks);
  assert.equal(rows.length, 1);
  assert.equal(rows[0].stream, true);
  assert.equal(rows[0].endpoint, "messages.stream");
});

test("wrap resolves finalMessage() to the provider message by identity", async (t) => {
  const rows = installRuntime(t);
  const finalMessage = { content: [{ type: "text", text: "done" }], usage: { input_tokens: 3, output_tokens: 2 } };
  const { call } = seamDouble({
    provider: "anthropic",
    path: "messages",
    method: "stream",
    impl: () => asyncIterableStream([{ type: "content_block_delta", delta: { text: "d" } }], { finalMessage }),
  });

  const stream = call({ model: "claude", messages: [] });
  assert.equal(await stream.finalMessage(), finalMessage);
  assert.equal(rows.length, 1);
});

test("wrap preserves chunk identity and passthrough properties through a nested proxy stream", async (t) => {
  const rows = installRuntime(t);
  const chunks = [{ type: "content_block_delta", delta: { text: "x" } }];
  const controller = { requestId: "req-proxy-1" };
  const { call } = seamDouble({
    provider: "anthropic",
    path: "messages",
    method: "stream",
    impl: () => asyncIterableStream(chunks, { proxy: true, extra: { controller } }),
  });

  const stream = call({ model: "claude", messages: [] });
  assert.equal(stream.controller, controller);
  assertSameSequence(await drainIterable(stream), chunks);
  assert.equal(rows.length, 1);
});

test("wrap rethrows a mid-stream iterator error by identity after prior chunks", async (t) => {
  const rows = installRuntime(t);
  const first = { type: "content_block_delta", delta: { text: "partial" } };
  const failure = new URIError("iterator failed mid-stream");
  const { call } = seamDouble({
    provider: "anthropic",
    path: "messages",
    method: "stream",
    impl: () => asyncIterableStream([first], { throwAfter: failure }),
  });

  const observed = [];
  await assert.rejects(async () => {
    for await (const chunk of call({ model: "claude", messages: [] })) observed.push(chunk);
  }, (error) => error === failure);

  assertSameSequence(observed, [first]);
  assert.equal(rows.length, 1);
  assert.equal(rows[0].status, "error");
  assert.equal(rows[0].error_type, "URIError");
});

test("wrap runs close()/abort() by return identity and marks the call abandoned", (t) => {
  const rows = installRuntime(t);
  const closeSentinel = { closed: true };
  const abortSentinel = { aborted: true };
  const closable = seamDouble({
    provider: "anthropic",
    path: "messages",
    method: "stream",
    impl: () => asyncIterableStream([], { onClose: () => closeSentinel }),
  });
  const abortable = seamDouble({
    provider: "anthropic",
    path: "messages",
    method: "stream",
    impl: () => asyncIterableStream([], { onAbort: () => abortSentinel }),
  });

  assert.equal(closable.call({ model: "claude", messages: [] }).close(), closeSentinel);
  assert.equal(abortable.call({ model: "claude", messages: [] }).abort(), abortSentinel);

  assert.equal(rows.length, 2);
  assert.deepEqual(rows.map((row) => row.status), ["abandoned", "abandoned"]);
});

test("wrap hides the injected usage-only chunk for openai chat.completions streaming", async (t) => {
  const rows = installRuntime(t);
  const textChunk = { choices: [{ delta: { content: "hi" } }] };
  const usageOnlyChunk = { choices: [], usage: { prompt_tokens: 2, completion_tokens: 1 } };
  let received;
  const { call } = seamDouble({
    provider: "openai",
    path: "chat.completions",
    method: "create",
    impl: (request) => {
      received = request;
      return asyncIterableStream([textChunk, usageOnlyChunk]);
    },
  });

  const request = { model: "m", messages: [], stream: true };
  const observed = await drainIterable(call(request));

  assertSameSequence(observed, [textChunk]); // usage-only chunk stays invisible
  assert.deepEqual(received.stream_options, { include_usage: true });
  assert.equal("stream_options" in request, false); // caller request is not mutated
  assert.equal(rows.length, 1);
  assert.equal(rows[0].input_tokens, 2);
});

test("wrap captures at most one row across full drain then close", async (t) => {
  const rows = installRuntime(t);
  const { call } = seamDouble({
    provider: "anthropic",
    path: "messages",
    method: "stream",
    impl: () => asyncIterableStream(
      [{ type: "content_block_delta", delta: { text: "ok" } }],
      { onClose: () => undefined },
    ),
  });

  const stream = call({ model: "claude", messages: [] });
  await drainIterable(stream);
  stream.close();

  assert.equal(rows.length, 1);
  assert.equal(rows[0].status, "success"); // completion wins; the later close is a no-op
});

// --- Vercel AI SDK middleware -----------------------------------------------
// Each middleware case also asserts rows.length === 1: a single generate or
// stream call enqueues at most one row across its whole lifecycle.

test("middleware fulfills wrapGenerate with the provider result by identity", async (t) => {
  const rows = installRuntime(t);
  const middleware = createVercelAISDKMiddleware();
  const result = {
    content: [{ type: "text", text: "generated" }],
    usage: { inputTokens: 4, outputTokens: 2 },
    response: { id: "gen-1", modelId: "gpt-x" },
  };

  const returned = await middleware.wrapGenerate({
    model: { provider: "openai", modelId: "gpt-x" },
    params: { prompt: [] },
    doGenerate: async () => result,
    doStream: async () => assert.fail("unexpected stream fallback"),
  });

  assert.equal(returned, result);
  assert.equal(rows.length, 1);
  assert.equal(rows[0].request_id, "gen-1");
});

test("middleware rejects wrapGenerate with the provider error by identity", async (t) => {
  const rows = installRuntime(t);
  const middleware = createVercelAISDKMiddleware();
  const failure = new Error("gateway down");

  await assert.rejects(middleware.wrapGenerate({
    model: { provider: "openai", modelId: "gpt-x" },
    params: { prompt: [] },
    doGenerate: async () => { throw failure; },
    doStream: async () => assert.fail("unexpected stream fallback"),
  }), (error) => error === failure);

  assert.equal(rows.length, 1);
  assert.equal(rows[0].error, true);
});

test("middleware streams ReadableStream parts by identity and order and passes response through", async (t) => {
  const rows = installRuntime(t);
  const middleware = createVercelAISDKMiddleware();
  const parts = [
    { type: "text-delta", id: "t", delta: "hel" },
    { type: "text-delta", id: "t", delta: "lo" },
    { type: "finish", finishReason: { unified: "stop", raw: "stop" }, usage: { inputTokens: 3, outputTokens: 2 } },
  ];
  const response = { id: "stream-1" };

  const result = await middleware.wrapStream({
    model: { provider: "openai", modelId: "gpt-x" },
    params: { prompt: [] },
    doStream: async () => streamResultFromReader(readerFromParts(parts), response),
  });

  assert.equal(result.response, response);
  assertSameSequence(await drainReadable(result.stream), parts);
  assert.equal(rows.length, 1);
  assert.equal(rows[0].stream, true);
});

test("middleware preserves an in-band error part and reports a stream error", async (t) => {
  const rows = installRuntime(t);
  const middleware = createVercelAISDKMiddleware();
  const failure = new SyntaxError("bad provider event");
  const parts = [
    { type: "text-delta", id: "t", delta: "partial" },
    { type: "error", error: failure },
  ];

  const result = await middleware.wrapStream({
    model: { provider: "openai", modelId: "gpt-x" },
    params: { prompt: [] },
    doStream: async () => streamResultFromReader(readerFromParts(parts)),
  });

  assertSameSequence(await drainReadable(result.stream), parts);
  assert.equal(rows.length, 1);
  assert.equal(rows[0].status, "error");
  assert.equal(rows[0].error_type, "SyntaxError");
  assert.deepEqual(capturedResponse(rows[0]).error, { type: "SyntaxError", message: "bad provider event" });
});

test("middleware rejects a failing read with the provider error by identity after prior parts", async (t) => {
  const rows = installRuntime(t);
  const middleware = createVercelAISDKMiddleware();
  const first = { type: "text-delta", id: "t", delta: "partial" };
  const failure = new RangeError("reader failed");

  const result = await middleware.wrapStream({
    model: { provider: "openai", modelId: "gpt-x" },
    params: { prompt: [] },
    doStream: async () => streamResultFromReader(readerFromParts([first], { error: failure })),
  });

  const reader = result.stream.getReader();
  assert.equal((await reader.read()).value, first);
  await assert.rejects(reader.read(), (error) => error === failure);

  assert.equal(rows.length, 1);
  assert.equal(rows[0].status, "error");
  assert.equal(rows[0].error_type, "RangeError");
});

test("middleware cancels the underlying reader with the reason and marks the call abandoned", async (t) => {
  const rows = installRuntime(t);
  const middleware = createVercelAISDKMiddleware();
  const first = { type: "text-delta", id: "t", delta: "partial" };
  let cancelReason;

  const result = await middleware.wrapStream({
    model: { provider: "openai", modelId: "gpt-x" },
    params: { prompt: [] },
    doStream: async () => streamResultFromReader(
      readerFromParts([first], { onCancel: (reason) => { cancelReason = reason; } }),
    ),
  });

  const reader = result.stream.getReader();
  assert.equal((await reader.read()).value, first);
  await reader.cancel("caller stopped");

  assert.equal(cancelReason, "caller stopped");
  assert.equal(rows.length, 1);
  assert.equal(rows[0].status, "abandoned");
});
