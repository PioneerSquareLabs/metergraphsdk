// Wrap the real, unmodified provider SDK clients and drive a call through
// their actual request-building/response-parsing code — with the network
// replaced by a mocked fetch, not the SDK itself.
//
// sdk.test.mjs's "seams exist on the real SDK" tests prove a seam *exists*
// on the real client. Its behavioral tests prove wrap() *works*, but only
// against hand-built fakes that mimic the real client's shape. Neither
// proves that wrapping the real client and calling a real method actually
// produces a captured row — which is exactly the gap that let the original
// chat.completions.parse capture bug ship unnoticed. These tests close it,
// without needing live API keys or network access.

import assert from "node:assert/strict";
import test from "node:test";

import OpenAI from "openai";
import Anthropic from "@anthropic-ai/sdk";
import { GoogleGenAI } from "@google/genai";

import { wrap } from "../dist/index.js";
import { CaptureRuntime } from "../dist/capture.js";
import { setCaptureRuntime } from "../dist/wrap.js";

function stubRuntime(rows) {
  return new CaptureRuntime(
    { enqueue(row) { rows.push(row); return true; } },
    { captureText: true, appRoot: "", skipFrames: [], textMaxBytes: 100_000 },
  );
}

function jsonResponse(body) {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "content-type": "application/json" },
  });
}

test("wrap captures openai chat.completions.parse through a real client", async (t) => {
  const rows = [];
  setCaptureRuntime(stubRuntime(rows));
  t.after(() => setCaptureRuntime());

  const client = wrap(new OpenAI({
    apiKey: "test",
    fetch: async () => jsonResponse({
      id: "chatcmpl-test",
      object: "chat.completion",
      created: 0,
      model: "gpt-4o-mini",
      choices: [{
        index: 0,
        message: { role: "assistant", content: JSON.stringify({ text: "hi" }) },
        finish_reason: "stop",
      }],
      usage: { prompt_tokens: 5, completion_tokens: 2, total_tokens: 7 },
    }),
  }), "openai");

  const response = await client.chat.completions.parse({
    model: "gpt-4o-mini",
    messages: [{ role: "user", content: "hi" }],
    response_format: {
      type: "json_schema",
      json_schema: {
        name: "answer",
        schema: {
          type: "object",
          properties: { text: { type: "string" } },
          required: ["text"],
          additionalProperties: false,
        },
      },
    },
  });

  assert.deepEqual(JSON.parse(response.choices[0].message.content), { text: "hi" });
  assert.equal(rows.length, 1);
  assert.equal(rows[0].endpoint, "chat.completions.parse");
  assert.equal(rows[0].provider, "openai");
  assert.equal(rows[0].input_tokens, 5);
  assert.equal(rows[0].output_tokens, 2);
});

test("wrap captures openai chat.completions.create through a real client", async (t) => {
  const rows = [];
  setCaptureRuntime(stubRuntime(rows));
  t.after(() => setCaptureRuntime());

  const client = wrap(new OpenAI({
    apiKey: "test",
    fetch: async () => jsonResponse({
      id: "chatcmpl-test2",
      object: "chat.completion",
      created: 0,
      model: "gpt-4o-mini",
      choices: [{ index: 0, message: { role: "assistant", content: "hi" }, finish_reason: "stop" }],
      usage: { prompt_tokens: 4, completion_tokens: 1, total_tokens: 5 },
    }),
  }), "openai");

  const response = await client.chat.completions.create({
    model: "gpt-4o-mini",
    messages: [{ role: "user", content: "hi" }],
  });

  assert.equal(response.choices[0].message.content, "hi");
  assert.equal(rows.length, 1);
  assert.equal(rows[0].endpoint, "chat.completions");
});

test("wrap captures anthropic messages.create through a real client", async (t) => {
  const rows = [];
  setCaptureRuntime(stubRuntime(rows));
  t.after(() => setCaptureRuntime());

  const client = wrap(new Anthropic({
    apiKey: "test",
    fetch: async () => jsonResponse({
      id: "msg_test",
      type: "message",
      role: "assistant",
      model: "claude-haiku-4-5-20251001",
      content: [{ type: "text", text: "hi" }],
      stop_reason: "end_turn",
      usage: { input_tokens: 5, output_tokens: 2 },
    }),
  }), "anthropic");

  const response = await client.messages.create({
    model: "claude-haiku-4-5-20251001",
    max_tokens: 10,
    messages: [{ role: "user", content: "hi" }],
  });

  assert.equal(response.content[0].text, "hi");
  assert.equal(rows.length, 1);
  assert.equal(rows[0].endpoint, "messages");
  assert.equal(rows[0].provider, "anthropic");
  assert.equal(rows[0].input_tokens, 5);
  assert.equal(rows[0].output_tokens, 2);
});

test("wrap captures google generateContent through a real client", async (t) => {
  const rows = [];
  setCaptureRuntime(stubRuntime(rows));
  t.after(() => setCaptureRuntime());

  // GoogleGenAI has no constructor hook for a custom transport, so the
  // mocked network is installed by temporarily replacing global fetch for
  // the duration of this test — everything from there on (request
  // building, response parsing) is the real SDK's own code.
  const originalFetch = global.fetch;
  global.fetch = async () => jsonResponse({
    candidates: [{ content: { parts: [{ text: "hi" }], role: "model" }, finishReason: "STOP" }],
    usageMetadata: { promptTokenCount: 5, candidatesTokenCount: 2, totalTokenCount: 7 },
    modelVersion: "gemini-2.5-flash",
  });

  try {
    const client = wrap(new GoogleGenAI({ apiKey: "test" }), "google");
    const response = await client.models.generateContent({
      model: "gemini-2.5-flash",
      contents: "hi",
    });

    assert.equal(response.text, "hi");
    assert.equal(rows.length, 1);
    assert.equal(rows[0].endpoint, "models.generate_content");
    assert.equal(rows[0].provider, "google");
  } finally {
    global.fetch = originalFetch;
  }
});

// --- MET-9: the declaration view and the tool-only content rule, driven
// through the real Anthropic client's own request building and SSE parsing.

const MET9_TOOL_REQUEST = {
  model: "claude-haiku-4-5-20251001",
  max_tokens: 64,
  messages: [{ role: "user", content: "rank these" }],
  tools: [{
    name: "rank_experts",
    description: "Rank candidate experts.",
    input_schema: {
      type: "object",
      properties: { query: { type: "string" } },
      required: ["query"],
    },
  }],
  tool_choice: { type: "tool", name: "rank_experts" },
};

const MET9_TOOL_ONLY_MESSAGE = {
  id: "msg_tool",
  type: "message",
  role: "assistant",
  model: "claude-haiku-4-5-20251001",
  content: [{ type: "tool_use", id: "toolu_real", name: "rank_experts",
    input: { query: "vision" } }],
  stop_reason: "tool_use",
  usage: { input_tokens: 9, output_tokens: 3 },
};

function sseResponse(events) {
  const body = events
    .map(([name, payload]) => `event: ${name}\ndata: ${JSON.stringify(payload)}\n\n`)
    .join("");
  return new Response(body, { status: 200, headers: { "content-type": "text/event-stream" } });
}

function met9StreamEvents(stopReason = "tool_use") {
  return [
    ["message_start", { type: "message_start", message: {
      id: "msg_stream", type: "message", role: "assistant",
      model: "claude-haiku-4-5-20251001", content: [], stop_reason: null,
      usage: { input_tokens: 9, output_tokens: 0 } } }],
    ["content_block_start", { type: "content_block_start", index: 0, content_block: {
      type: "tool_use", id: "toolu_stream", name: "rank_experts", input: {} } }],
    ["content_block_delta", { type: "content_block_delta", index: 0, delta: {
      type: "input_json_delta", partial_json: '{"query": "vision"}' } }],
    ["content_block_stop", { type: "content_block_stop", index: 0 }],
    ["message_delta", { type: "message_delta",
      delta: { stop_reason: stopReason, stop_sequence: null },
      usage: { output_tokens: 7 } }],
    ["message_stop", { type: "message_stop" }],
  ];
}

test("wrap records the declaration view and null content through a real anthropic client",
  async (t) => {
    const rows = [];
    setCaptureRuntime(stubRuntime(rows));
    t.after(() => setCaptureRuntime());

    const client = wrap(new Anthropic({
      apiKey: "test",
      fetch: async () => jsonResponse(MET9_TOOL_ONLY_MESSAGE),
    }), "anthropic");

    const response = await client.messages.create(MET9_TOOL_REQUEST);

    assert.equal(response.content[0].name, "rank_experts");
    assert.equal(rows.length, 1);
    const [record] = rows[0].tool_definitions.declarations;
    assert.equal(record.name, "rank_experts");
    assert.equal(record.schema_key, "input_schema");
    assert.deepEqual(record.schema.required, ["query"]);
    const envelope = JSON.parse(rows[0].response_text);
    assert.ok("content" in envelope);
    assert.equal(envelope.content, null);
    assert.equal(envelope.tool_calls[0].call_id, "toolu_real");
    assert.deepEqual(envelope.tool_calls[0].arguments, { query: "vision" });
  });

test("a real anthropic reply truncated at max_tokens keeps its content", async (t) => {
  const rows = [];
  setCaptureRuntime(stubRuntime(rows));
  t.after(() => setCaptureRuntime());

  const client = wrap(new Anthropic({
    apiKey: "test",
    fetch: async () => jsonResponse({ ...MET9_TOOL_ONLY_MESSAGE, stop_reason: "max_tokens" }),
  }), "anthropic");

  await client.messages.create(MET9_TOOL_REQUEST);

  assert.ok(Array.isArray(JSON.parse(rows[0].response_text).content));
});

test("a real anthropic raw event stream records null content", async (t) => {
  const rows = [];
  setCaptureRuntime(stubRuntime(rows));
  t.after(() => setCaptureRuntime());

  const client = wrap(new Anthropic({
    apiKey: "test",
    fetch: async () => sseResponse(met9StreamEvents()),
  }), "anthropic");

  const kinds = [];
  for await (const event of await client.messages.create({ ...MET9_TOOL_REQUEST, stream: true })) {
    kinds.push(event.type);
  }

  assert.equal(kinds[kinds.length - 1], "message_stop");
  assert.equal(rows.length, 1);
  assert.equal(rows[0].stream, true);
  const envelope = JSON.parse(rows[0].response_text);
  assert.ok("content" in envelope);
  assert.equal(envelope.content, null);
  assert.equal(envelope.tool_calls[0].call_id, "toolu_stream");
  assert.deepEqual(envelope.tool_calls[0].arguments, { query: "vision" });
});

test("a real anthropic stream truncated at max_tokens keeps its content", async (t) => {
  const rows = [];
  setCaptureRuntime(stubRuntime(rows));
  t.after(() => setCaptureRuntime());

  const client = wrap(new Anthropic({
    apiKey: "test",
    fetch: async () => sseResponse(met9StreamEvents("max_tokens")),
  }), "anthropic");

  for await (const _ of await client.messages.create({ ...MET9_TOOL_REQUEST, stream: true })) {
    // drain
  }

  const envelope = JSON.parse(rows[0].response_text);
  assert.ok(!("content" in envelope && envelope.content === null));
});

test("the real anthropic stream helper keeps its content while its events disagree",
  async (t) => {
    // The TypeScript stream helper mutates the `content_block_start` block in
    // place as input_json_delta chunks arrive, so `toolEvents` seeds the
    // accumulation with the already-complete input and then appends the deltas
    // again: the event's arguments read `{"query":"vision"}{"query": "vision"}`.
    // That predates this change and belongs to the tool-event contract, which
    // this change does not touch. The recognition rule requires the reply's
    // blocks and their events to agree, so it declines to convert and the
    // provider's own representation is preserved. The Python stream helper does
    // not double its arguments and does convert, which
    // tests/integrations/providers/test_real_client_integration.py asserts.
    const rows = [];
    setCaptureRuntime(stubRuntime(rows));
    t.after(() => setCaptureRuntime());

    const client = wrap(new Anthropic({
      apiKey: "test",
      fetch: async () => sseResponse(met9StreamEvents()),
    }), "anthropic");

    const stream = client.messages.stream(MET9_TOOL_REQUEST);
    for await (const _ of stream) {
      // drain
    }
    const final = await stream.finalMessage();

    assert.equal(final.stop_reason, "tool_use");
    const envelope = JSON.parse(rows[0].response_text);
    assert.ok(Array.isArray(envelope.content), "the fallback keeps the blocks");
    assert.equal(rows[0].tool_calls[0].call_id, "toolu_stream");
    assert.match(String(rows[0].tool_calls[0].arguments), /^\{"query":"vision"\}\{/,
      "records the pre-existing doubling this rule refuses to trust");
  });
