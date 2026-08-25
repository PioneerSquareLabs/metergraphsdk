// Drive the real `ai` package (streamText + wrapLanguageModel) through the real
// @ai-sdk/openai provider, with the network replaced by a mocked fetch, and the
// MeterGraph middleware plus an ordinary transparent Proxy wrapper composed over
// the provider model. The type tests (ai-sdk-*.types.ts) prove the middleware's
// types line up with `ai`; the parity/fault suites prove the middleware against
// hand-built doubles. Neither drives the AI SDK stream runtime through
// our ReadableStream wrapper. This closes that gap: real request-building, real
// SSE parsing, real stream assembly — only the transport is mocked.

import assert from "node:assert/strict";
import test from "node:test";

import { createOpenAI } from "@ai-sdk/openai";
import { streamText, wrapLanguageModel } from "ai";

import { CaptureRuntime } from "../dist/capture.js";
import { setCaptureRuntime } from "../dist/wrap.js";
import { createVercelAISDKMiddleware } from "../dist/vercel-ai.js";

function stubRuntime(rows) {
  return new CaptureRuntime(
    { enqueue(row) { rows.push(row); return true; } },
    { captureText: true, appRoot: "", skipFrames: [], textMaxBytes: 100_000 },
  );
}

function sse(events) {
  const body = events
    .map((e) => (e === "[DONE]" ? "data: [DONE]\n\n" : `data: ${JSON.stringify(e)}\n\n`))
    .join("");
  return new Response(body, { status: 200, headers: { "content-type": "text/event-stream" } });
}

const STREAM_CHUNKS = [
  { id: "chatcmpl-1", object: "chat.completion.chunk", model: "gpt-4o-mini", choices: [{ index: 0, delta: { role: "assistant", content: "" }, finish_reason: null }] },
  { id: "chatcmpl-1", object: "chat.completion.chunk", model: "gpt-4o-mini", choices: [{ index: 0, delta: { content: "Hel" }, finish_reason: null }] },
  { id: "chatcmpl-1", object: "chat.completion.chunk", model: "gpt-4o-mini", choices: [{ index: 0, delta: { content: "lo" }, finish_reason: null }] },
  { id: "chatcmpl-1", object: "chat.completion.chunk", model: "gpt-4o-mini", choices: [{ index: 0, delta: {}, finish_reason: "stop" }] },
  { id: "chatcmpl-1", object: "chat.completion.chunk", model: "gpt-4o-mini", choices: [], usage: { prompt_tokens: 5, completion_tokens: 2, total_tokens: 7 } },
  "[DONE]",
];

// A transparent Proxy over the provider model — an ordinary app-owned wrapper
// layer sitting under MeterGraph. It records the properties the AI SDK reads so
// the test can prove the layer is traversed, not bypassed.
function tracingProxy(model, accessed) {
  return new Proxy(model, {
    get(target, property, receiver) {
      if (typeof property === "string") accessed.add(property);
      return Reflect.get(target, property, receiver);
    },
  });
}

test("streamText over the real openai provider through a transparent proxy and the MeterGraph middleware", async (t) => {
  const rows = [];
  setCaptureRuntime(stubRuntime(rows));
  t.after(() => setCaptureRuntime());

  const provider = createOpenAI({ apiKey: "test", fetch: async () => sse(STREAM_CHUNKS) });
  const accessed = new Set();
  const model = wrapLanguageModel({
    model: tracingProxy(provider.chat("gpt-4o-mini"), accessed),
    middleware: createVercelAISDKMiddleware({ specificationVersion: "v4" }),
  });

  const result = streamText({ model, prompt: "hi" });

  const deltas = [];
  for await (const delta of result.textStream) deltas.push(delta);

  // Exact streamed chunk order and text.
  assert.deepEqual(deltas, ["Hel", "lo"]);
  assert.equal(await result.text, "Hello");

  // Native AI SDK result helpers resolve to the mocked provider's values.
  const usage = await result.usage;
  assert.equal(usage.inputTokens, 5);
  assert.equal(usage.outputTokens, 2);
  assert.equal(usage.totalTokens, 7);
  assert.equal(await result.finishReason, "stop");

  // The ordinary proxy layer is traversed by the SDK, not bypassed.
  assert.equal(accessed.has("doStream"), true);

  // Exactly one capture row for the whole stream lifecycle.
  assert.equal(rows.length, 1);
  const row = rows[0];
  assert.equal(row.provider, "openai");
  assert.equal(row.endpoint, "ai.doStream");
  assert.equal(row.model, "gpt-4o-mini");
  assert.equal(row.stream, true);
  assert.equal(row.status, "stop");
  assert.equal(row.finish_reason, "stop");
  assert.equal(row.input_tokens, 5);
  assert.equal(row.output_tokens, 2);
  assert.ok(row.ttft_ms >= 0);
});

test("streamText surfaces a provider HTTP error and MeterGraph records one error row", async (t) => {
  const rows = [];
  setCaptureRuntime(stubRuntime(rows));
  t.after(() => setCaptureRuntime());

  let fetchCalls = 0;
  const provider = createOpenAI({
    apiKey: "test",
    fetch: async () => {
      fetchCalls += 1;
      return new Response(
        JSON.stringify({ error: { message: "boom", type: "server_error" } }),
        { status: 500, headers: { "content-type": "application/json" } },
      );
    },
  });
  const model = wrapLanguageModel({
    model: provider.chat("gpt-4o-mini"),
    middleware: createVercelAISDKMiddleware({ specificationVersion: "v4" }),
  });

  // onError suppresses streamText's default error logging for this expected
  // fault; the error is still asserted below via the fullStream error part.
  const result = streamText({ model, prompt: "hi", maxRetries: 0, onError: () => {} });

  const partTypes = [];
  let errorPart;
  for await (const part of result.fullStream) {
    partTypes.push(part.type);
    if (part.type === "error") errorPart = part;
  }

  assert.equal(fetchCalls, 1);
  assert.ok(partTypes.includes("error"));
  assert.equal(errorPart.error.name, "AI_APICallError");

  assert.equal(rows.length, 1);
  assert.equal(rows[0].status, "error");
  assert.equal(rows[0].error, true);
  assert.equal(rows[0].error_type, "AI_APICallError");
  assert.equal(rows[0].stream, true);
});
