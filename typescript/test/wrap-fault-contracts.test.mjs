// Fault-injection contracts for the direct provider wrapper (wrap.ts). Each
// case injects a fault at a telemetry boundary and states the fail-open
// contract an application should observe: provider invocation count and
// result/error/chunk identity must not change because capture faulted.
//
// wrap.ts does not currently guard capture.start()/finish() or stream
// observation, so those contracts fail today. They are held under
// expectContractViolation, which requires the specific current-behavior
// assertion and fails if the contract starts passing, forcing the marker to be
// removed when the production fix lands. The two control cases show that,
// absent telemetry faults, the same fixtures pass straight through, so the
// gaps are owned by the telemetry boundary rather than the wrapper.

import assert from "node:assert/strict";
import test from "node:test";

import {
  assertSameSequence,
  asyncIterableStream,
  installRuntime,
  seamDouble,
} from "./support/instrumentation-doubles.mjs";
import {
  expectContractViolation,
  fieldFaultChunk,
  installFaultRuntime,
} from "./support/wrap-fault-doubles.mjs";

const START_FAULT = new Error("capture.start fault");
const FINISH_FAULT = new Error("capture.finish fault");

// --- Control: ownership absent telemetry faults -----------------------------

test("wrap invokes the provider once and returns its result by identity when telemetry is healthy", (t) => {
  const rows = installRuntime(t);
  let invocations = 0;
  const result = { id: "healthy", usage: { input_tokens: 1, output_tokens: 1 } };
  const { call } = seamDouble({
    provider: "openai",
    path: "responses",
    method: "create",
    impl: () => { invocations++; return result; },
  });

  const returned = call({ model: "m" });

  assert.equal(invocations, 1);
  assert.equal(returned, result);
  assert.equal(rows.length, 1);
});

test("wrap rejects with the provider error by identity when telemetry is healthy", async (t) => {
  const rows = installRuntime(t);
  const providerError = new TypeError("provider rejected");
  const { call } = seamDouble({
    provider: "openai",
    path: "responses",
    method: "create",
    impl: async () => { throw providerError; },
  });

  await assert.rejects(call({ model: "m" }), (error) => error === providerError);
  assert.equal(rows.length, 1);
  assert.equal(rows[0].error, true);
});

// --- Gap: capture.start before provider invocation --------------------------

test("wrap start fault must not stop provider invocation (expected failure)", (t) => {
  installFaultRuntime(t, { startError: START_FAULT });
  let invocations = 0;
  const result = { id: "start-fault" };
  const { call } = seamDouble({
    provider: "openai",
    path: "responses",
    method: "create",
    impl: () => { invocations++; return result; },
  });

  return expectContractViolation(() => {
    let leaked;
    let returned;
    try { returned = call({ model: "m" }); } catch (error) { leaked = error; }
    assert.equal(leaked, undefined, "start fault must not surface to the caller");
    assert.equal(invocations, 1, "provider must be invoked despite a start fault");
    assert.equal(returned, result, "provider result identity must be preserved");
  }, { failsWith: "start fault must not surface to the caller" });
});

// --- Gap: sync/promise finish on success ------------------------------------

test("wrap finish fault must not surface on a synchronous success (expected failure)", (t) => {
  installFaultRuntime(t, { finishError: FINISH_FAULT });
  const result = { id: "sync-success" };
  const { call } = seamDouble({
    provider: "openai",
    path: "responses",
    method: "create",
    impl: () => result,
  });

  return expectContractViolation(() => {
    let leaked;
    let returned;
    try { returned = call({ model: "m" }); } catch (error) { leaked = error; }
    assert.equal(leaked, undefined, "finish fault must not surface on a synchronous success");
    assert.equal(returned, result, "provider result identity must be preserved");
  }, { failsWith: "finish fault must not surface on a synchronous success" });
});

test("wrap finish fault must not reject a fulfilled promise (expected failure)", (t) => {
  installFaultRuntime(t, { finishError: FINISH_FAULT });
  const result = { id: "async-success" };
  const { call } = seamDouble({
    provider: "openai",
    path: "responses",
    method: "create",
    impl: async () => result,
  });

  return expectContractViolation(async () => {
    let leaked;
    let returned;
    try { returned = await call({ model: "m" }); } catch (error) { leaked = error; }
    assert.equal(leaked, undefined, "finish fault must not reject a fulfilled promise");
    assert.equal(returned, result, "provider result identity must be preserved");
  }, { failsWith: "finish fault must not reject a fulfilled promise" });
});

// --- Gap: sync/promise finish on provider error -----------------------------

test("wrap finish fault must not mask a synchronous provider error (expected failure)", (t) => {
  installFaultRuntime(t, { finishError: FINISH_FAULT });
  const providerError = new RangeError("provider threw synchronously");
  const { call } = seamDouble({
    provider: "openai",
    path: "responses",
    method: "create",
    impl: () => { throw providerError; },
  });

  return expectContractViolation(() => {
    let leaked;
    try { call({ model: "m" }); } catch (error) { leaked = error; }
    assert.equal(leaked, providerError, "synchronous provider error identity must survive a finish fault");
  }, { failsWith: "synchronous provider error identity must survive a finish fault" });
});

test("wrap finish fault must not mask a promise provider error (expected failure)", (t) => {
  installFaultRuntime(t, { finishError: FINISH_FAULT });
  const providerError = new RangeError("provider rejected");
  const { call } = seamDouble({
    provider: "openai",
    path: "responses",
    method: "create",
    impl: async () => { throw providerError; },
  });

  return expectContractViolation(async () => {
    let leaked;
    try { await call({ model: "m" }); } catch (error) { leaked = error; }
    assert.equal(leaked, providerError, "promise provider error identity must survive a finish fault");
  }, { failsWith: "promise provider error identity must survive a finish fault" });
});

// --- Gap: async-stream observation/classification ---------------------------

test("wrap stream classification fault must not surface to the consumer (expected failure)", (t) => {
  installRuntime(t);
  const first = { type: "content_block_delta", delta: { text: "a" } };
  const faulted = fieldFaultChunk(new Error("field access fault"));
  const last = { type: "content_block_delta", delta: { text: "b" } };
  const chunks = [first, faulted, last];
  const { call } = seamDouble({
    provider: "anthropic",
    path: "messages",
    method: "stream",
    impl: () => asyncIterableStream(chunks),
  });

  return expectContractViolation(async () => {
    const observed = [];
    let leaked;
    try {
      for await (const chunk of call({ model: "c", messages: [] })) observed.push(chunk);
    } catch (error) { leaked = error; }
    assert.equal(leaked, undefined, "a classification field fault must not surface to the consumer");
    assertSameSequence(observed, chunks);
  }, { failsWith: "a classification field fault must not surface to the consumer" });
});

// --- Gap: async-stream normal exhaustion ------------------------------------

test("wrap finish fault must not surface after full stream exhaustion (expected failure)", (t) => {
  installFaultRuntime(t, { finishError: FINISH_FAULT });
  const chunks = [
    { type: "content_block_delta", delta: { text: "a" } },
    { type: "content_block_delta", delta: { text: "b" } },
  ];
  const { call } = seamDouble({
    provider: "anthropic",
    path: "messages",
    method: "stream",
    impl: () => asyncIterableStream(chunks),
  });

  return expectContractViolation(async () => {
    const observed = [];
    let leaked;
    try {
      for await (const chunk of call({ model: "c", messages: [] })) observed.push(chunk);
    } catch (error) { leaked = error; }
    assert.equal(leaked, undefined, "finish fault must not surface after full stream exhaustion");
    assertSameSequence(observed, chunks);
  }, { failsWith: "finish fault must not surface after full stream exhaustion" });
});

// --- Gap: async-stream provider iteration error -----------------------------

test("wrap finish fault must not mask a provider iteration error (expected failure)", (t) => {
  installFaultRuntime(t, { finishError: FINISH_FAULT });
  const first = { type: "content_block_delta", delta: { text: "a" } };
  const providerError = new URIError("iterator failed mid-stream");
  const { call } = seamDouble({
    provider: "anthropic",
    path: "messages",
    method: "stream",
    impl: () => asyncIterableStream([first], { throwAfter: providerError }),
  });

  return expectContractViolation(async () => {
    const observed = [];
    let leaked;
    try {
      for await (const chunk of call({ model: "c", messages: [] })) observed.push(chunk);
    } catch (error) { leaked = error; }
    assert.equal(leaked, providerError, "provider iteration error identity must survive a finish fault");
    assertSameSequence(observed, [first]);
  }, { failsWith: "provider iteration error identity must survive a finish fault" });
});

// --- Gap: async-stream finalMessage() ---------------------------------------

test("wrap finish fault must not reject finalMessage() (expected failure)", (t) => {
  installFaultRuntime(t, { finishError: FINISH_FAULT });
  const finalMessage = {
    content: [{ type: "text", text: "done" }],
    usage: { input_tokens: 2, output_tokens: 1 },
  };
  const { call } = seamDouble({
    provider: "anthropic",
    path: "messages",
    method: "stream",
    impl: () => asyncIterableStream(
      [{ type: "content_block_delta", delta: { text: "a" } }],
      { finalMessage },
    ),
  });

  return expectContractViolation(async () => {
    const stream = call({ model: "c", messages: [] });
    let leaked;
    let resolved;
    try { resolved = await stream.finalMessage(); } catch (error) { leaked = error; }
    assert.equal(leaked, undefined, "finish fault must not reject finalMessage()");
    assert.equal(resolved, finalMessage, "finalMessage() identity must be preserved");
  }, { failsWith: "finish fault must not reject finalMessage()" });
});

test("wrap finish fault must not mask a finalMessage() rejection (expected failure)", (t) => {
  installFaultRuntime(t, { finishError: FINISH_FAULT });
  const providerError = new URIError("finalMessage rejected");
  const { call } = seamDouble({
    provider: "anthropic",
    path: "messages",
    method: "stream",
    impl: () => {
      // parity double resolves finalMessage; a rejecting one is composed here
      const stream = asyncIterableStream([{ type: "content_block_delta", delta: { text: "a" } }]);
      stream.finalMessage = async () => { throw providerError; };
      return stream;
    },
  });

  return expectContractViolation(async () => {
    const stream = call({ model: "c", messages: [] });
    let leaked;
    try { await stream.finalMessage(); } catch (error) { leaked = error; }
    assert.equal(leaked, providerError, "finalMessage() rejection identity must survive a finish fault");
  }, { failsWith: "finalMessage() rejection identity must survive a finish fault" });
});

// --- Gap: async-stream close()/abort() --------------------------------------

test("wrap finish fault must not surface from close() (expected failure)", (t) => {
  installFaultRuntime(t, { finishError: FINISH_FAULT });
  const closeSentinel = { closed: true };
  const { call } = seamDouble({
    provider: "anthropic",
    path: "messages",
    method: "stream",
    impl: () => asyncIterableStream([], { onClose: () => closeSentinel }),
  });

  return expectContractViolation(() => {
    let leaked;
    let returned;
    try { returned = call({ model: "c", messages: [] }).close(); }
    catch (error) { leaked = error; }
    assert.equal(leaked, undefined, "finish fault in a finally must not surface from close()");
    assert.equal(returned, closeSentinel, "close() return identity must be preserved");
  }, { failsWith: "finish fault in a finally must not surface from close()" });
});

test("wrap finish fault must not surface from abort() (expected failure)", (t) => {
  installFaultRuntime(t, { finishError: FINISH_FAULT });
  const abortSentinel = { aborted: true };
  const { call } = seamDouble({
    provider: "anthropic",
    path: "messages",
    method: "stream",
    impl: () => asyncIterableStream([], { onAbort: () => abortSentinel }),
  });

  return expectContractViolation(() => {
    let leaked;
    let returned;
    try { returned = call({ model: "c", messages: [] }).abort(); }
    catch (error) { leaked = error; }
    assert.equal(leaked, undefined, "finish fault in a finally must not surface from abort()");
    assert.equal(returned, abortSentinel, "abort() return identity must be preserved");
  }, { failsWith: "finish fault in a finally must not surface from abort()" });
});

test("wrap finish fault must not mask a close() error (expected failure)", (t) => {
  installFaultRuntime(t, { finishError: FINISH_FAULT });
  const providerError = new RangeError("close failed");
  const { call } = seamDouble({
    provider: "anthropic",
    path: "messages",
    method: "stream",
    impl: () => asyncIterableStream([], { onClose: () => { throw providerError; } }),
  });

  return expectContractViolation(() => {
    let leaked;
    try { call({ model: "c", messages: [] }).close(); } catch (error) { leaked = error; }
    assert.equal(leaked, providerError, "close() error identity must survive a finish fault");
  }, { failsWith: "close() error identity must survive a finish fault" });
});

test("wrap finish fault must not mask an abort() error (expected failure)", (t) => {
  installFaultRuntime(t, { finishError: FINISH_FAULT });
  const providerError = new RangeError("abort failed");
  const { call } = seamDouble({
    provider: "anthropic",
    path: "messages",
    method: "stream",
    impl: () => asyncIterableStream([], { onAbort: () => { throw providerError; } }),
  });

  return expectContractViolation(() => {
    let leaked;
    try { call({ model: "c", messages: [] }).abort(); } catch (error) { leaked = error; }
    assert.equal(leaked, providerError, "abort() error identity must survive a finish fault");
  }, { failsWith: "abort() error identity must survive a finish fault" });
});
