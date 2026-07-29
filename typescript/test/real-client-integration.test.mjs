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
