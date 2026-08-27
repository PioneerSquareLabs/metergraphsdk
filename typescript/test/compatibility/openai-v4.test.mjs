import assert from "node:assert/strict";
import test from "node:test";

import OpenAI from "openai-v4";

import { wrap } from "../../dist/index.js";
import { CaptureRuntime } from "../../dist/capture.js";
import { setCaptureRuntime } from "../../dist/wrap.js";

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

function completionResponse(content = "hi") {
  return {
    id: "chatcmpl-test",
    object: "chat.completion",
    created: 0,
    model: "gpt-4o-mini",
    choices: [{
      index: 0,
      message: { role: "assistant", content },
      finish_reason: "stop",
    }],
    usage: { prompt_tokens: 5, completion_tokens: 2, total_tokens: 7 },
  };
}

test("wrap captures chat.completions.create through OpenAI v4", async (t) => {
  const rows = [];
  setCaptureRuntime(stubRuntime(rows));
  t.after(() => setCaptureRuntime());

  const client = wrap(new OpenAI({
    apiKey: "test",
    fetch: async () => jsonResponse(completionResponse()),
  }), "openai");

  const response = await client.chat.completions.create({
    model: "gpt-4o-mini",
    messages: [{ role: "user", content: "hi" }],
  });

  assert.equal(response.choices[0].message.content, "hi");
  assert.equal(rows.length, 1);
  assert.equal(rows[0].endpoint, "chat.completions");
  assert.equal(rows[0].provider, "openai");
  assert.equal(rows[0].input_tokens, 5);
  assert.equal(rows[0].output_tokens, 2);
});

test("wrap captures chat.completions.parse through OpenAI v4", async (t) => {
  const rows = [];
  setCaptureRuntime(stubRuntime(rows));
  t.after(() => setCaptureRuntime());

  const client = wrap(new OpenAI({
    apiKey: "test",
    fetch: async () => jsonResponse(completionResponse(JSON.stringify({ text: "hi" }))),
  }), "openai");

  const response = await client.beta.chat.completions.parse({
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
