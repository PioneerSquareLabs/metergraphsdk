// Fault-injection contracts for the Vercel AI SDK middleware's Web Streams
// wrapper (vercel-ai.ts). Cases are derived from that wrapper's own structure,
// not the direct-wrapper/Python suites: the middleware drives a
// ReadableStream whose pull() runs telemetry-owned per-part work (outputChunk,
// chunkText, content collection, response-metadata accumulation, finish-part
// extraction) BEFORE it enqueues the part, and whose finish() reads finish-part
// fields at stream end.
//
// Two boundaries are already fail-open and are pinned here as permanent tests:
// capture.start() faults are swallowed by startCapture (stream passes through
// unwrapped, generation still runs), and capture.finish() faults are swallowed
// by finishCapture (delivery, read rejections, and cancel identity survive).
//
// Per-part field access inside observe() and end-of-stream assembly inside
// finish() are telemetry-owned and fail-open: a part that resolves reader.read()
// but throws when telemetry touches one of its fields is still delivered by
// identity, is not reported to the consumer as a provider stream error, and does
// not set a stream error status. These are pinned below as plain tests.
//
// outputChunk and content collection read only the `type` discriminant of a
// valid part (a string), so they have no independent fault surface; chunkText,
// response-metadata accumulation, and finish-part extraction read further
// fields and are exercised below.

import assert from "node:assert/strict";
import test from "node:test";

import { createVercelAISDKMiddleware } from "../dist/vercel-ai.js";
import {
  assertSameSequence,
  drainReadable,
  installRuntime,
  readerFromParts,
  streamResultFromReader,
} from "./support/instrumentation-doubles.mjs";
import { installFaultRuntime } from "./support/wrap-fault-doubles.mjs";

const START_FAULT = new Error("capture.start fault");
const FINISH_FAULT = new Error("capture.finish fault");
const FIELD_FAULT = new Error("part field access fault");

const MODEL = { provider: "openai", modelId: "gpt-x" };
const PARAMS = { prompt: [] };

const textDelta = (delta) => ({ type: "text-delta", id: "t", delta });

// Valid AI SDK parts whose non-discriminant fields throw on access. reader.read()
// resolves each by reference; only telemetry touches the faulting field.
const textDeltaWithFaultingText = (error) => ({
  type: "text-delta",
  id: "t",
  get delta() { throw error; },
});
const responseMetadataWithFaultingField = (error) => ({
  type: "response-metadata",
  id: "resp-1",
  get modelId() { throw error; },
});
const finishWithFaultingUsage = (error) => ({
  type: "finish",
  finishReason: { unified: "stop", raw: "stop" },
  get usage() { throw error; },
});

function readerRejectingCancel(parts, cancelError) {
  let index = 0;
  return {
    read() {
      return index < parts.length
        ? Promise.resolve({ done: false, value: parts[index++] })
        : Promise.resolve({ done: true, value: undefined });
    },
    cancel() { return Promise.reject(cancelError); },
  };
}

function wrapGenerate(middleware, doGenerate) {
  return middleware.wrapGenerate({
    model: MODEL,
    params: PARAMS,
    doGenerate,
    doStream: async () => assert.fail("unexpected stream fallback"),
  });
}

function wrapStream(middleware, reader, response) {
  return middleware.wrapStream({
    model: MODEL,
    params: PARAMS,
    doStream: async () => streamResultFromReader(reader, response),
  });
}

async function readAll(readable) {
  const reader = readable.getReader();
  const observed = [];
  let leaked;
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      observed.push(value);
    }
  } catch (error) {
    leaked = error;
  }
  return { observed, leaked };
}

// --- Safe boundary: capture.start()/finish() faults on wrapGenerate ----------

test("wrapGenerate start fault does not stop generation and returns the result by identity", async (t) => {
  const rows = installFaultRuntime(t, { startError: START_FAULT });
  const middleware = createVercelAISDKMiddleware();
  let invocations = 0;
  const result = { content: [], usage: { inputTokens: 1, outputTokens: 1 }, response: { id: "g" } };

  const returned = await wrapGenerate(middleware, async () => { invocations++; return result; });

  assert.equal(invocations, 1);
  assert.equal(returned, result);
  assert.equal(rows.length, 0); // start faulted -> no state -> nothing enqueued
});

test("wrapGenerate finish fault does not reject a fulfilled generation", async (t) => {
  installFaultRuntime(t, { finishError: FINISH_FAULT });
  const middleware = createVercelAISDKMiddleware();
  const result = { content: [], usage: {}, response: { id: "g" } };

  assert.equal(await wrapGenerate(middleware, async () => result), result);
});

test("wrapGenerate finish fault does not mask a generation rejection", async (t) => {
  installFaultRuntime(t, { finishError: FINISH_FAULT });
  const middleware = createVercelAISDKMiddleware();
  const providerError = new Error("gateway down");

  await assert.rejects(
    wrapGenerate(middleware, async () => { throw providerError; }),
    (error) => error === providerError,
  );
});

// --- Safe boundary: capture.start()/finish() faults on wrapStream ------------

test("wrapStream start fault returns the raw doStream result and delivers parts by identity", async (t) => {
  const rows = installFaultRuntime(t, { startError: START_FAULT });
  const middleware = createVercelAISDKMiddleware();
  const parts = [textDelta("a"), textDelta("b")];
  const response = { id: "stream-1" };
  const raw = streamResultFromReader(readerFromParts(parts), response);

  const result = await middleware.wrapStream({ model: MODEL, params: PARAMS, doStream: async () => raw });

  assert.equal(result, raw); // unwrapped: no capture state to attach
  assert.equal(result.response, response);
  assertSameSequence(await drainReadable(result.stream), parts);
  assert.equal(rows.length, 0);
});

test("wrapStream finish fault does not surface after full stream exhaustion", async (t) => {
  installFaultRuntime(t, { finishError: FINISH_FAULT });
  const middleware = createVercelAISDKMiddleware();
  const parts = [textDelta("a"), textDelta("b")];

  const result = await wrapStream(middleware, readerFromParts(parts));
  const { observed, leaked } = await readAll(result.stream);

  assert.equal(leaked, undefined);
  assertSameSequence(observed, parts);
});

test("wrapStream finish fault does not mask a provider read rejection", async (t) => {
  installFaultRuntime(t, { finishError: FINISH_FAULT });
  const middleware = createVercelAISDKMiddleware();
  const first = textDelta("a");
  const providerError = new RangeError("reader failed");

  const result = await wrapStream(middleware, readerFromParts([first], { error: providerError }));
  const reader = result.stream.getReader();

  assert.equal((await reader.read()).value, first);
  await assert.rejects(reader.read(), (error) => error === providerError);
});

test("wrapStream cancel forwards the reason and a finish fault in the finally does not surface", async (t) => {
  installFaultRuntime(t, { finishError: FINISH_FAULT });
  const middleware = createVercelAISDKMiddleware();
  let cancelReason;

  const result = await wrapStream(
    middleware,
    readerFromParts([textDelta("a")], { onCancel: (reason) => { cancelReason = reason; } }),
  );
  const reader = result.stream.getReader();
  await reader.read();
  await reader.cancel("caller stopped");

  assert.equal(cancelReason, "caller stopped");
});

test("wrapStream cancel rejection identity survives a finish fault in the finally", async (t) => {
  installFaultRuntime(t, { finishError: FINISH_FAULT });
  const middleware = createVercelAISDKMiddleware();
  const cancelError = new TypeError("cancel failed");

  const result = await wrapStream(middleware, readerRejectingCancel([textDelta("a")], cancelError));
  const reader = result.stream.getReader();
  await reader.read();
  await assert.rejects(reader.cancel("stop"), (error) => error === cancelError);
});

// --- Per-part field access inside observe()/finish() is fail-open ------------

test("per-part text-field fault must not surface to the stream consumer", async (t) => {
  installRuntime(t);
  const middleware = createVercelAISDKMiddleware();
  const first = textDelta("a");
  const faulted = textDeltaWithFaultingText(FIELD_FAULT);
  const last = textDelta("b");
  const parts = [first, faulted, last];

  const result = await wrapStream(middleware, readerFromParts(parts));
  const { observed, leaked } = await readAll(result.stream);
  assert.equal(leaked, undefined, "a per-part field fault must not surface to the stream consumer");
  assertSameSequence(observed, parts);
});

test("per-part text-field fault must not be recorded as a provider read error", async (t) => {
  const rows = installRuntime(t);
  const middleware = createVercelAISDKMiddleware();
  const faulted = textDeltaWithFaultingText(FIELD_FAULT);

  const result = await wrapStream(middleware, readerFromParts([faulted]));
  await readAll(result.stream); // drive pull() so the observe() fault is recorded
  assert.equal(rows.length, 1);
  assert.equal(rows[0].status, "success", "a per-part field fault must not be recorded as a provider read error");
});

test("response-metadata accumulation fault must not surface to the stream consumer", async (t) => {
  installRuntime(t);
  const middleware = createVercelAISDKMiddleware();
  const first = textDelta("a");
  const metadata = responseMetadataWithFaultingField(FIELD_FAULT);

  const result = await wrapStream(middleware, readerFromParts([first, metadata]));
  const { observed, leaked } = await readAll(result.stream);
  assert.equal(leaked, undefined, "a response-metadata accumulation fault must not surface to the stream consumer");
  assertSameSequence(observed, [first, metadata]);
});

test("finish-part field fault must not surface at stream end", async (t) => {
  installRuntime(t);
  const middleware = createVercelAISDKMiddleware();
  const first = textDelta("a");
  const finishPart = finishWithFaultingUsage(FIELD_FAULT);

  const result = await wrapStream(middleware, readerFromParts([first, finishPart]));
  const { observed, leaked } = await readAll(result.stream);
  assert.equal(leaked, undefined, "a finish-part field fault must not surface at stream end");
  assertSameSequence(observed, [first, finishPart]);
});
