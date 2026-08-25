// Regression contracts for two production defects the direct wrapper (wrap.ts)
// exhibited under valid provider compositions:
//   1. wrap() replaced the provider APIPromise with a plain Promise, dropping
//      .withResponse()/.asResponse() and the equivalent @anthropic-ai/sdk helpers.
//   2. openai chat.completions streaming suppressed the usage-only final chunk
//      unconditionally, swallowing one a caller requested via their own
//      stream_options.include_usage.
// Each states the behavior an application must observe.

import assert from "node:assert/strict";
import test from "node:test";

import OpenAI from "openai";
import Anthropic from "@anthropic-ai/sdk";

import { setCaptureRuntime, wrap } from "../dist/wrap.js";
import {
  asyncIterableStream,
  assertSameSequence,
  drainIterable,
  installRuntime,
  seamDouble,
} from "./support/instrumentation-doubles.mjs";

function jsonResponse(body) {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "content-type": "application/json" },
  });
}

// A mocked OpenAI SSE stream: one `data:` line per chunk, terminated by [DONE].
function sseResponse(chunks) {
  const body = `${chunks.map((chunk) => `data: ${JSON.stringify(chunk)}\n\n`).join("")}data: [DONE]\n\n`;
  return new Response(body, {
    status: 200,
    headers: { "content-type": "text/event-stream" },
  });
}

const CHAT_BODY = {
  id: "chatcmpl-x",
  object: "chat.completion",
  created: 0,
  model: "gpt-4o-mini",
  choices: [{ index: 0, message: { role: "assistant", content: "hi" }, finish_reason: "stop" }],
  usage: { prompt_tokens: 4, completion_tokens: 1, total_tokens: 5 },
};

const MESSAGE_BODY = {
  id: "msg_x",
  type: "message",
  role: "assistant",
  model: "claude-haiku-4-5-20251001",
  content: [{ type: "text", text: "hi" }],
  stop_reason: "end_turn",
  usage: { input_tokens: 5, output_tokens: 2 },
};

// Defect 1: the openai APIPromise is a Promise subclass whose helper methods
// (.withResponse(), .asResponse(), raw-response access) must survive wrap().
test("wrap() preserves the openai APIPromise .withResponse() helper and captures once", async (t) => {
  const rows = installRuntime(t);
  const client = wrap(
    new OpenAI({ apiKey: "test", fetch: async () => jsonResponse(CHAT_BODY) }),
    "openai",
  );

  const pending = client.chat.completions.create({
    model: "gpt-4o-mini",
    messages: [{ role: "user", content: "hi" }],
  });
  assert.equal(typeof pending.withResponse, "function", "openai APIPromise .withResponse() must survive wrap()");

  const { data, response } = await pending.withResponse();
  assert.equal(data.choices[0].message.content, "hi");
  assert.ok(response instanceof Response, "raw provider Response must be reachable via .withResponse()");
  assert.equal(rows.length, 1, "MeterGraph must capture the call exactly once");
  assert.equal(rows[0].endpoint, "chat.completions");
  assert.equal(rows[0].input_tokens, 4);
  assert.equal(rows[0].output_tokens, 1);
});

// The same root cause stripped .asResponse(); it must reach the raw Response
// without issuing the provider request twice.
test("wrap() preserves the openai APIPromise .asResponse() helper", async (t) => {
  const rows = installRuntime(t);
  let requests = 0;
  const client = wrap(
    new OpenAI({
      apiKey: "test",
      fetch: async () => {
        requests += 1;
        return jsonResponse(CHAT_BODY);
      },
    }),
    "openai",
  );

  const pending = client.chat.completions.create({
    model: "gpt-4o-mini",
    messages: [{ role: "user", content: "hi" }],
  });
  assert.equal(typeof pending.asResponse, "function", "openai APIPromise .asResponse() must survive wrap()");

  const response = await pending.asResponse();
  assert.ok(response instanceof Response);
  const body = await response.json();
  assert.equal(body.choices[0].message.content, "hi");
  assert.equal(requests, 1, "helper access must not issue the provider request twice");
});

// A streaming .withResponse() must keep its stream instrumented: the caller's
// data is the MeterGraph-wrapped stream, capture fires once on drain, and the
// raw Response is still reachable — all from a single HTTP request.
test("wrap() keeps a streaming openai .withResponse() data stream instrumented", async (t) => {
  const rows = installRuntime(t);
  const textChunk = {
    id: "chatcmpl-x", object: "chat.completion.chunk", created: 0, model: "gpt-4o-mini",
    choices: [{ index: 0, delta: { content: "hi" }, finish_reason: null }],
  };
  const usageChunk = {
    id: "chatcmpl-x", object: "chat.completion.chunk", created: 0, model: "gpt-4o-mini",
    choices: [], usage: { prompt_tokens: 2, completion_tokens: 1, total_tokens: 3 },
  };
  let requests = 0;
  const client = wrap(
    new OpenAI({
      apiKey: "test",
      fetch: async () => {
        requests += 1;
        return sseResponse([textChunk, usageChunk]);
      },
    }),
    "openai",
  );

  const { data, response } = await client.chat.completions.create({
    model: "gpt-4o-mini",
    messages: [{ role: "user", content: "hi" }],
    stream: true,
    stream_options: { include_usage: true },
  }).withResponse();
  assert.ok(response instanceof Response, "raw Response must remain available");

  const observed = [];
  for await (const chunk of data) observed.push(chunk);

  // The caller asked for the usage chunk, so both chunks are delivered.
  assert.ok(observed.some((c) => c.choices?.[0]?.delta?.content === "hi"), "text chunk must be delivered");
  assert.ok(
    observed.some((c) => c.choices?.length === 0 && c.usage?.total_tokens === 3),
    "caller-requested usage-only chunk must be delivered",
  );
  assert.equal(requests, 1, "streaming .withResponse() must issue exactly one HTTP request");
  assert.equal(rows.length, 1, "draining the instrumented .withResponse() stream must capture exactly one row");
  assert.equal(rows[0].input_tokens, 2);
  assert.equal(rows[0].output_tokens, 1);
});

// Awaiting the promise directly still yields the parsed value and one capture.
test("wrap() preserves normal await fulfillment for the openai APIPromise", async (t) => {
  const rows = installRuntime(t);
  const client = wrap(
    new OpenAI({ apiKey: "test", fetch: async () => jsonResponse(CHAT_BODY) }),
    "openai",
  );

  const response = await client.chat.completions.create({
    model: "gpt-4o-mini",
    messages: [{ role: "user", content: "hi" }],
  });
  assert.equal(response.choices[0].message.content, "hi");
  assert.equal(rows.length, 1);
  assert.equal(rows[0].output_tokens, 1);
});

// Awaiting the promise and calling .withResponse() on the same APIPromise must
// share the one in-flight request and one capture — no duplicate rows.
test("wrap() shares one request and one capture across await and .withResponse()", async (t) => {
  const rows = installRuntime(t);
  let requests = 0;
  const client = wrap(
    new OpenAI({
      apiKey: "test",
      fetch: async () => {
        requests += 1;
        return jsonResponse(CHAT_BODY);
      },
    }),
    "openai",
  );

  const pending = client.chat.completions.create({
    model: "gpt-4o-mini",
    messages: [{ role: "user", content: "hi" }],
  });
  const direct = await pending;
  const { data, response } = await pending.withResponse();

  assert.equal(direct.choices[0].message.content, "hi");
  assert.equal(data.choices[0].message.content, "hi");
  assert.ok(response instanceof Response);
  assert.equal(requests, 1, "await and .withResponse() must share one request");
  assert.equal(rows.length, 1, "await and .withResponse() must not double-capture");
});

// Anthropic uses the same APIPromise surface; parity must hold.
test("wrap() preserves the anthropic APIPromise .withResponse() helper", async (t) => {
  const rows = installRuntime(t);
  const client = wrap(
    new Anthropic({ apiKey: "test", fetch: async () => jsonResponse(MESSAGE_BODY) }),
    "anthropic",
  );

  const pending = client.messages.create({
    model: "claude-haiku-4-5-20251001",
    max_tokens: 10,
    messages: [{ role: "user", content: "hi" }],
  });
  assert.equal(typeof pending.withResponse, "function", "anthropic APIPromise .withResponse() must survive wrap()");

  const { data, response } = await pending.withResponse();
  assert.equal(data.content[0].text, "hi");
  assert.ok(response instanceof Response);
  assert.equal(rows.length, 1);
  assert.equal(rows[0].provider, "anthropic");
  assert.equal(rows[0].input_tokens, 5);
});

// Defect 2: the usage-only final chunk is suppressed only when MeterGraph itself
// injected stream_options.include_usage. A caller-supplied include_usage chunk
// must reach the consumer by identity, with token capture preserved and caller
// input left unmutated.
test("wrap() preserves a caller-requested openai usage-only streaming chunk", async (t) => {
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

  const request = {
    model: "m",
    messages: [],
    stream: true,
    stream_options: { include_usage: true },
  };
  const observed = await drainIterable(call(request));

  assert.ok(observed.includes(usageOnlyChunk), "a caller-supplied stream_options.include_usage chunk must reach the consumer");
  assertSameSequence(observed, [textChunk, usageOnlyChunk]);
  assert.equal(received.stream_options, request.stream_options, "caller input must not be mutated");
  assert.equal(rows.length, 1);
  assert.equal(rows[0].input_tokens, 2);
});
