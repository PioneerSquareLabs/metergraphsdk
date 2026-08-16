import assert from "node:assert/strict";
import test from "node:test";

import { DeferredIneligibleError, deferred, runDeferred } from "../dist/deferred.js";
import * as PublicApi from "../dist/index.js";

function delay(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

const REQUEST = { model: "gpt-5-mini", input: "hello" };

function basePolicy(overrides = {}) {
  return {
    deadlineMs: 200,
    acceptDuplicateProviderExecution: true,
    ...overrides,
  };
}

function baseControls(overrides = {}) {
  return {
    pollIntervalMs: 10,
    ...overrides,
  };
}

// A fake adapter whose batch completes `completesAfterMs` after submission
// (measured from submitOne(), not from construction) — matching the plan's
// "batch result arrives before/after the deadline" scenarios with real,
// small millisecond delays rather than a virtual clock, since these delays
// are short enough (tens to a few hundred ms) to keep the suite fast while
// staying fully deterministic (see the dedicated fake-clock test below for
// the injectable-seam proof).
function fakeAdapter({
  batchCompletesAfterMs,
  batchOutcome = "completed",
  directDelayMs = 5,
  directResult = { via: "direct" },
  batchResult = { via: "batch" },
  containedToolCallPlan = false,
} = {}) {
  let submittedAt;
  let directCallCount = 0;
  let pollCount = 0;
  const adapter = {
    eligibility() {
      return { eligible: true };
    },
    async submitOne() {
      submittedAt = Date.now();
      return { providerBatchId: "batch_fake_1" };
    },
    async poll() {
      pollCount += 1;
      if (batchCompletesAfterMs == null) return { status: "pending" };
      const elapsed = Date.now() - submittedAt;
      return elapsed >= batchCompletesAfterMs ? { status: batchOutcome } : { status: "pending" };
    },
    async readResult() {
      return { result: batchResult, containedToolCallPlan };
    },
    async direct() {
      directCallCount += 1;
      await delay(directDelayMs);
      return { result: directResult, containedToolCallPlan: false };
    },
  };
  return {
    adapter,
    directResult,
    batchResult,
    get directCallCount() { return directCallCount; },
    get pollCount() { return pollCount; },
  };
}

test("returns the batch result when it arrives before the deadline", async () => {
  const { adapter, batchResult } = fakeAdapter({ batchCompletesAfterMs: 60 });

  const outcome = await runDeferred(adapter, REQUEST, basePolicy({ deadlineMs: 300 }), baseControls());

  assert.equal(outcome.source, "batch");
  assert.equal(outcome.result, batchResult);
  assert.equal(outcome.metadata.canonical_result, "batch");
  assert.equal(outcome.metadata.batch_outcome, "completed");
  assert.equal(outcome.metadata.duplicate_provider_execution, false);
  assert.equal(outcome.metadata.late_batch_completed, false);
});

test("sends exactly one direct fallback and never returns a late batch result", async () => {
  // Keep the whole fixture (not a destructured directCallCount) — it's a
  // getter, and destructuring it here would freeze it at its value before
  // runDeferred() ever calls direct(), always reading back 0.
  const fixture = fakeAdapter({ batchCompletesAfterMs: 300, directDelayMs: 20 });

  const outcome = await runDeferred(fixture.adapter, REQUEST, basePolicy({ deadlineMs: 200 }), baseControls());

  assert.equal(outcome.source, "direct");
  assert.equal(outcome.result, fixture.directResult);
  assert.notEqual(outcome.result, fixture.batchResult);
  assert.equal(fixture.directCallCount, 1);
  assert.equal(outcome.metadata.canonical_result, "direct");
  assert.equal(outcome.metadata.duplicate_provider_execution, true);
  // At the moment deferred() returns, the batch (completing at ~300ms) has
  // not settled yet — accurate as-of-return, not a claim about the future.
  assert.equal(outcome.metadata.late_batch_completed, false);

  // Let the background poll observe the late completion, then confirm it
  // never mutated or re-exposed anything through the already-returned value.
  await delay(150);
  assert.equal(outcome.result, fixture.directResult);
  assert.equal(fixture.directCallCount, 1); // still exactly one direct call, ever
});

test("onLateBatchSettled reports the late outcome without ever returning its content", async () => {
  const { adapter } = fakeAdapter({
    batchCompletesAfterMs: 60,
    directDelayMs: 5,
    containedToolCallPlan: true,
  });
  let lateInfo;

  const outcome = await runDeferred(adapter, REQUEST, basePolicy({
    deadlineMs: 10, // fires long before the fake batch's 60ms completion
    onLateBatchSettled: (info) => { lateInfo = info; },
  }), baseControls());

  assert.equal(outcome.source, "direct");
  await delay(120); // give the background poll time to observe completion
  assert.deepEqual(lateInfo, { outcome: "completed", containedToolCallPlan: true });
});

test("a failed batch before the deadline falls back to direct immediately, not after waiting out the full deadline", async () => {
  const { adapter } = fakeAdapter({ batchCompletesAfterMs: 20, batchOutcome: "failed" });
  const startedAt = Date.now();

  const outcome = await runDeferred(adapter, REQUEST, basePolicy({ deadlineMs: 5_000 }), baseControls());

  assert.equal(outcome.source, "direct");
  assert.equal(outcome.metadata.batch_outcome, "failed");
  assert.ok(Date.now() - startedAt < 1_000, "must not wait out the full 5s deadline after an early batch failure");
});

test("a completed batch whose result cannot be read falls back to direct exactly once, never rejects, and never fires the late-batch callback", async () => {
  let readResultCalls = 0;
  let directCalls = 0;
  let lateCalls = 0;
  const adapter = {
    eligibility: () => ({ eligible: true }),
    async submitOne() { return { providerBatchId: "b1" }; },
    // Reports completed on the very first check — this is the PRIMARY
    // race outcome, not a late one.
    async poll() { return { status: "completed" }; },
    async readResult() {
      readResultCalls += 1;
      // Stands in for: missing output file, an item-level provider error,
      // a malformed/missing matching line, or a transient read failure —
      // all collapse to the same "can't produce a canonical batch
      // result" outcome.
      throw new Error("missing output file");
    },
    async direct() {
      directCalls += 1;
      return { result: { via: "direct" }, containedToolCallPlan: false };
    },
  };

  const outcome = await runDeferred(adapter, REQUEST, basePolicy({
    deadlineMs: 5_000, // large on purpose — must not matter; the batch resolves on the first poll
    onLateBatchSettled: () => { lateCalls += 1; },
  }));

  assert.equal(outcome.source, "direct");
  assert.equal(outcome.result.via, "direct");
  assert.equal(directCalls, 1);
  assert.equal(readResultCalls, 1); // never retried
  assert.equal(outcome.metadata.batch_outcome, "failed");
  assert.equal(outcome.metadata.canonical_result, "direct");
  assert.equal(outcome.metadata.duplicate_provider_execution, true);

  // Give any stray background task a moment, then confirm the late-batch
  // callback was never invoked — an unreadable *completed* batch is the
  // immediate resolution, not something observed asynchronously later.
  await delay(50);
  assert.equal(lateCalls, 0);
});

test("a submitOne failure — no batch ever existed — is still thrown, never treated as a fallback trigger", async () => {
  let directCalls = 0;
  const adapter = {
    eligibility: () => ({ eligible: true }),
    async submitOne() { throw new Error("network error creating batch"); },
    async poll() { return { status: "pending" }; },
    async readResult() { throw new Error("must not be called: no batch was ever created"); },
    async direct() {
      directCalls += 1;
      return { result: {}, containedToolCallPlan: false };
    },
  };

  await assert.rejects(
    () => runDeferred(adapter, REQUEST, basePolicy()),
    /network error creating batch/,
  );
  assert.equal(directCalls, 0);
});

test("rejects a streaming request before any provider call", async () => {
  const { adapter, directCallCount } = fakeAdapter();
  await assert.rejects(
    () => runDeferred(adapter, { ...REQUEST, stream: true }, basePolicy()),
    DeferredIneligibleError,
  );
  assert.equal(directCallCount, 0);
});

test("rejects a request with tools unless allowDuplicateToolCallPlans is explicitly true", async () => {
  const { adapter } = fakeAdapter();
  const withTools = { ...REQUEST, tools: [{ type: "function", function: { name: "lookup" } }] };

  await assert.rejects(
    () => runDeferred(adapter, withTools, basePolicy()),
    DeferredIneligibleError,
  );

  const { adapter: adapter2, directResult } = fakeAdapter({ batchCompletesAfterMs: 400 });
  const outcome = await runDeferred(adapter2, withTools, basePolicy({
    deadlineMs: 30,
    allowDuplicateToolCallPlans: true,
  }));
  assert.equal(outcome.result, directResult);
});

test("rejects when acceptDuplicateProviderExecution is not exactly true", async () => {
  const { adapter } = fakeAdapter();
  await assert.rejects(
    () => runDeferred(adapter, REQUEST, { deadlineMs: 200 }),
    DeferredIneligibleError,
  );
  await assert.rejects(
    () => runDeferred(adapter, REQUEST, { deadlineMs: 200, acceptDuplicateProviderExecution: false }),
    DeferredIneligibleError,
  );
});

test("rejects when the adapter itself reports the request ineligible", async () => {
  const { adapter } = fakeAdapter();
  adapter.eligibility = () => ({ eligible: false, reason: "unsupported endpoint shape" });
  await assert.rejects(
    () => runDeferred(adapter, REQUEST, basePolicy()),
    /unsupported endpoint shape/,
  );
});

test("an injected fake clock drives the deadline deterministically, with no real waiting", async () => {
  // Tracks every scheduled timer (the deadline AND the poll loop's own
  // interval sleep both call clock.setTimeout independently) so the test
  // can fire exactly the deadline timer, identified by its distinct ms
  // value, without disturbing the other.
  const timers = [];
  const clock = {
    setTimeout(handler, ms) {
      const timer = { handler, ms, cancelled: false };
      timers.push(timer);
      return timer;
    },
    clearTimeout(timer) { timer.cancelled = true; },
  };
  let resolveDirect;
  const adapter = {
    eligibility: () => ({ eligible: true }),
    async submitOne() { return { providerBatchId: "b1" }; },
    async poll() { return { status: "pending" }; }, // never completes on its own
    async readResult() { throw new Error("must not be called: batch never completed"); },
    async direct() {
      return new Promise((resolve) => { resolveDirect = resolve; });
    },
  };

  const pending = runDeferred(adapter, REQUEST, basePolicy({
    deadlineMs: 999_999,
  }), baseControls({
    pollIntervalMs: 500_000,
    clock,
  }));
  // Nothing has happened yet — the fake clock never fires on its own.
  await delay(20);
  const deadlineTimer = timers.find((timer) => timer.ms === 999_999 && !timer.cancelled);
  assert.ok(deadlineTimer, "the deadline timer must have been scheduled");
  deadlineTimer.handler(); // fire the deadline manually — no real time passed
  await delay(5); // let the microtask queue settle so direct() gets invoked
  assert.equal(typeof resolveDirect, "function");
  resolveDirect({ result: { via: "direct" }, containedToolCallPlan: false });

  const outcome = await pending;
  assert.equal(outcome.source, "direct");
});

test("deferred() resolves the OpenAI adapter from a duck-typed client and runs the same state machine", async () => {
  // Stateful fake: echoes back whatever custom_id the adapter itself put in
  // the uploaded JSONL line, exactly as a real Batch API round trip would —
  // the adapter's custom_id is internal, so the test must not guess it.
  let uploadedCustomId;
  const fakeClient = {
    files: {
      async create({ file }) {
        const text = await file.text();
        uploadedCustomId = JSON.parse(text.trim()).custom_id;
        return { id: "file_1" };
      },
      async content() {
        return {
          async text() {
            return `${JSON.stringify({
              custom_id: uploadedCustomId,
              response: { status_code: 200, body: { id: "resp_1", output: [] } },
            })}\n`;
          },
        };
      },
    },
    batches: {
      async create() { return { id: "batch_1", status: "validating" }; },
      async retrieve() { return { id: "batch_1", status: "completed", output_file_id: "file_out_1" }; },
    },
    responses: {
      async create() { throw new Error("direct must not be called when the batch completes in time"); },
    },
  };

  const outcome = await deferred(fakeClient, "openai", REQUEST, basePolicy({ deadlineMs: 2_000 }));
  assert.equal(outcome.source, "batch");
  assert.equal(outcome.result.id, "resp_1");
});

test("deferred and DeferredIneligibleError are reachable from the package's public entry point", () => {
  // The package.json "exports" field whitelists only ".", so anything not
  // re-exported from index.ts is unreachable by real consumers regardless
  // of what the internal modules export — this is the actual contract
  // surface, not an implementation detail. runDeferred and the adapter
  // factories are internal-module exports only (see
  // deferred-public-surface.test.mjs for the full root-export contract).
  assert.equal(typeof PublicApi.deferred, "function");
  assert.equal(PublicApi.DeferredIneligibleError, DeferredIneligibleError);
});

test("deferred() resolves the Anthropic adapter from a duck-typed client and runs the same state machine", async () => {
  let submittedCustomId;
  const fakeClient = {
    messages: {
      async create() { throw new Error("direct must not be called when the batch completes in time"); },
      batches: {
        async create(params) {
          submittedCustomId = params.requests[0].custom_id;
          return { id: "batch_1", processing_status: "in_progress" };
        },
        async retrieve() {
          return { id: "batch_1", processing_status: "ended", request_counts: { succeeded: 1 } };
        },
        results() {
          return {
            async *[Symbol.asyncIterator]() {
              yield {
                custom_id: submittedCustomId,
                result: { type: "succeeded", message: { id: "msg_1", role: "assistant", content: [] } },
              };
            },
          };
        },
      },
    },
  };

  const outcome = await deferred(fakeClient, "anthropic", REQUEST, basePolicy({ deadlineMs: 2_000 }));
  assert.equal(outcome.source, "batch");
  assert.equal(outcome.result.id, "msg_1");
});

test("Anthropic: an item-level batch error falls back to direct exactly once and never rejects", async () => {
  let directCalls = 0;
  let submittedCustomId;
  const fakeClient = {
    messages: {
      async create() {
        directCalls += 1;
        return { id: "msg_direct_1", role: "assistant", content: [] };
      },
      batches: {
        async create(params) {
          submittedCustomId = params.requests[0].custom_id;
          return { id: "batch_1", processing_status: "in_progress" };
        },
        async retrieve() {
          return { id: "batch_1", processing_status: "ended", request_counts: { errored: 1 } };
        },
        results() {
          return {
            async *[Symbol.asyncIterator]() {
              yield {
                custom_id: submittedCustomId,
                result: { type: "errored", error: { type: "invalid_request", message: "leak-me-not" } },
              };
            },
          };
        },
      },
    },
  };

  const outcome = await deferred(fakeClient, "anthropic", REQUEST, basePolicy({ deadlineMs: 2_000 }));
  assert.equal(outcome.source, "direct");
  assert.equal(directCalls, 1);
  assert.equal(outcome.metadata.batch_outcome, "failed");
});

test("Anthropic: a completed batch missing our custom_id in results() falls back to direct exactly once and never rejects", async () => {
  let directCalls = 0;
  const fakeClient = {
    messages: {
      async create() {
        directCalls += 1;
        return { id: "msg_direct_1", role: "assistant", content: [] };
      },
      batches: {
        async create() { return { id: "batch_1", processing_status: "in_progress" }; },
        async retrieve() {
          return { id: "batch_1", processing_status: "ended", request_counts: { succeeded: 1 } };
        },
        results() {
          return { async *[Symbol.asyncIterator]() { /* no matching item */ } };
        },
      },
    },
  };

  const outcome = await deferred(fakeClient, "anthropic", REQUEST, basePolicy({ deadlineMs: 2_000 }));
  assert.equal(outcome.source, "direct");
  assert.equal(directCalls, 1);
  assert.equal(outcome.metadata.batch_outcome, "failed");
});

test("deferred() resolves the Google adapter from a duck-typed client and runs the same state machine", async () => {
  const GOOGLE_REQUEST = { model: "gemini-2.5-flash", contents: [{ role: "user", parts: [{ text: "hello" }] }] };
  const fakeClient = {
    models: {
      async generateContent() { throw new Error("direct must not be called when the batch completes in time"); },
    },
    batches: {
      async create() { return { name: "batches/batch_1", state: "JOB_STATE_PENDING" }; },
      async get() {
        return {
          name: "batches/batch_1",
          state: "JOB_STATE_SUCCEEDED",
          dest: { inlinedResponses: [{ response: { candidates: [] } }] },
        };
      },
    },
  };

  const outcome = await deferred(fakeClient, "google", GOOGLE_REQUEST, basePolicy({ deadlineMs: 2_000 }));
  assert.equal(outcome.source, "batch");
  assert.deepEqual(outcome.result, { candidates: [] });
});

test("Google: an item-level batch error falls back to direct exactly once and never rejects", async () => {
  const GOOGLE_REQUEST = { model: "gemini-2.5-flash", contents: [{ role: "user", parts: [{ text: "hello" }] }] };
  let directCalls = 0;
  const fakeClient = {
    models: {
      async generateContent() {
        directCalls += 1;
        return { candidates: [] };
      },
    },
    batches: {
      async create() { return { name: "batches/batch_1", state: "JOB_STATE_PENDING" }; },
      async get() {
        return {
          name: "batches/batch_1",
          state: "JOB_STATE_SUCCEEDED",
          dest: { inlinedResponses: [{ error: { code: 400, message: "leak-me-not" } }] },
        };
      },
    },
  };

  const outcome = await deferred(fakeClient, "google", GOOGLE_REQUEST, basePolicy({ deadlineMs: 2_000 }));
  assert.equal(outcome.source, "direct");
  assert.equal(directCalls, 1);
  assert.equal(outcome.metadata.batch_outcome, "failed");
});

test("Google: a completed batch with no inlined responses falls back to direct exactly once and never rejects", async () => {
  const GOOGLE_REQUEST = { model: "gemini-2.5-flash", contents: [{ role: "user", parts: [{ text: "hello" }] }] };
  let directCalls = 0;
  const fakeClient = {
    models: {
      async generateContent() {
        directCalls += 1;
        return { candidates: [] };
      },
    },
    batches: {
      async create() { return { name: "batches/batch_1", state: "JOB_STATE_PENDING" }; },
      async get() {
        return { name: "batches/batch_1", state: "JOB_STATE_SUCCEEDED" };
      },
    },
  };

  const outcome = await deferred(fakeClient, "google", GOOGLE_REQUEST, basePolicy({ deadlineMs: 2_000 }));
  assert.equal(outcome.source, "direct");
  assert.equal(directCalls, 1);
  assert.equal(outcome.metadata.batch_outcome, "failed");
});
