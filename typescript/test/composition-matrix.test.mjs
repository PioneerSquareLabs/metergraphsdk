// Generic wrapper composition matrix for orders/shapes the parity suite does
// not cover: repeated wrap(), a caller Proxy on either side of wrap(), provider
// auto-detection, and MeterGraph middleware nested between other AI SDK
// middlewares. Each case asserts application-visible identity/ordering plus the
// single-row lifecycle invariant. Behavior-changing composition defects are
// pinned separately in wrap-composition-expected-failures.test.mjs.

import assert from "node:assert/strict";
import test from "node:test";

import { createVercelAISDKMiddleware } from "../dist/vercel-ai.js";
import { setCaptureRuntime, wrap } from "../dist/wrap.js";
import {
  assertSameSequence,
  drainReadable,
  installRuntime,
  readerFromParts,
  streamResultFromReader,
} from "./support/instrumentation-doubles.mjs";

const MODEL = { provider: "openai", modelId: "gpt-x" };
const PARAMS = { prompt: [] };
const textDelta = (delta) => ({ type: "text-delta", id: "t", delta });

// --- Direct wrapper: wrapper orders/shapes ----------------------------------

test("wrap() applied twice instruments once and enqueues a single row", (t) => {
  const rows = installRuntime(t);
  const result = { id: "double", usage: { input_tokens: 1, output_tokens: 1 } };
  const client = { responses: { create: () => result } };

  wrap(client, "openai");
  wrap(client, "openai"); // second call is a no-op: method already patched

  assert.equal(client.responses.create({ model: "m" }), result);
  assert.equal(rows.length, 1);
});

test("wrap() patches through a caller Proxy placed outside the client", (t) => {
  const rows = installRuntime(t);
  const result = { id: "outside" };
  const client = { responses: { create: () => result } };
  const proxied = new Proxy(client, { get: (target, property, receiver) => Reflect.get(target, property, receiver) });

  wrap(proxied, "openai"); // seams resolve and patch through the proxy

  assert.equal(proxied.responses.create({ model: "m" }), result);
  assert.equal(rows.length, 1);
  assert.equal(rows[0].endpoint, "responses");
});

test("a caller Proxy placed outside an already-wrapped client preserves capture", (t) => {
  const rows = installRuntime(t);
  const result = { id: "inside" };
  const client = { responses: { create: () => result } };

  wrap(client, "openai");
  const proxied = new Proxy(client, { get: (target, property, receiver) => Reflect.get(target, property, receiver) });

  assert.equal(proxied.responses.create({ model: "m" }), result);
  assert.equal(rows.length, 1);
});

test("wrap() without an explicit provider detects it from the client shape", (t) => {
  const rows = installRuntime(t);
  const result = { id: "detect" };
  const client = { responses: { create: () => result } };

  wrap(client); // detectProvider -> openai (has responses)

  assert.equal(client.responses.create({ model: "m" }), result);
  assert.equal(rows.length, 1);
  assert.equal(rows[0].provider, "openai");
});

// --- Vercel middleware: nesting order ---------------------------------------

test("MeterGraph middleware nested between other middlewares preserves order, identity, and one row", async (t) => {
  const rows = installRuntime(t);
  const order = [];
  const tracer = (label) => ({
    specificationVersion: "v3",
    async wrapStream({ doStream }) {
      order.push(`in:${label}`);
      const result = await doStream();
      order.push(`out:${label}`);
      return result;
    },
  });
  const metergraph = createVercelAISDKMiddleware();
  const outer = tracer("A");
  const inner = tracer("B");
  const parts = [textDelta("a"), textDelta("b")];
  const doStream = async () => streamResultFromReader(readerFromParts(parts));

  // Realistic stack: outer app middleware -> MeterGraph -> inner app middleware.
  const result = await outer.wrapStream({
    model: MODEL,
    params: PARAMS,
    doStream: () => metergraph.wrapStream({
      model: MODEL,
      params: PARAMS,
      doStream: () => inner.wrapStream({ model: MODEL, params: PARAMS, doStream }),
    }),
  });

  assertSameSequence(await drainReadable(result.stream), parts);
  assert.deepEqual(order, ["in:A", "in:B", "out:B", "out:A"]);
  assert.equal(rows.length, 1);
  assert.equal(rows[0].stream, true);
});
