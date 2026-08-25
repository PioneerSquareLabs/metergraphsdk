// Strict expected-failure contracts for two production defects the direct
// wrapper (wrap.ts) currently exhibits under valid provider compositions. Each
// states the behavior an application should observe; wrap.ts violates it today,
// so expectContractViolation requires the contract to trip a node:assert
// AssertionError with the named message. When a fix lands the contract starts
// holding, expectContractViolation fails, and the marker is removed — leaving
// the assertion as a plain regression test.

import assert from "node:assert/strict";
import test from "node:test";

import OpenAI from "openai";

import { setCaptureRuntime, wrap } from "../dist/wrap.js";
import {
  asyncIterableStream,
  installRuntime,
  recordingRuntime,
  seamDouble,
} from "./support/instrumentation-doubles.mjs";
import { expectContractViolation } from "./support/wrap-fault-doubles.mjs";

function jsonResponse(body) {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "content-type": "application/json" },
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

// Defect 1: patch() returns `result.then(complete, ...)`, a plain Promise. The
// provider's APIPromise (openai and anthropic) resolves through Promise species,
// so its helper methods (.withResponse(), .asResponse(), raw-response access)
// are dropped from the value the application receives once capture is active.
// Pinned here for openai .withResponse(); the same root cause strips .asResponse()
// and the equivalent @anthropic-ai/sdk helpers.
test("EXPECTED FAILURE: wrap() drops the openai APIPromise .withResponse() helper", async (t) => {
  const { runtime } = recordingRuntime();
  setCaptureRuntime(runtime);
  t.after(() => setCaptureRuntime());

  const client = wrap(
    new OpenAI({ apiKey: "test", fetch: async () => jsonResponse(CHAT_BODY) }),
    "openai",
  );

  await expectContractViolation(async () => {
    const pending = client.chat.completions.create({
      model: "gpt-4o-mini",
      messages: [{ role: "user", content: "hi" }],
    });
    assert.ok(
      typeof pending?.withResponse === "function",
      "wrap() must preserve the openai APIPromise .withResponse() helper",
    );
    await pending;
  }, { failsWith: "must preserve the openai APIPromise" });
});

// Defect 2: streamProxy suppresses the openai chat.completions usage-only final
// chunk (empty choices + usage) so an injected stream_options.include_usage stays
// invisible. The suppression is not gated on whether MeterGraph injected that
// option, so a chunk the caller requested via their own
// stream_options.include_usage is also swallowed.
test("EXPECTED FAILURE: openai streaming swallows a caller-requested usage-only chunk", async (t) => {
  installRuntime(t);
  const textChunk = { choices: [{ delta: { content: "hi" } }] };
  const usageOnlyChunk = { choices: [], usage: { prompt_tokens: 2, completion_tokens: 1 } };
  const { call } = seamDouble({
    provider: "openai",
    path: "chat.completions",
    method: "create",
    impl: () => asyncIterableStream([textChunk, usageOnlyChunk]),
  });

  await expectContractViolation(async () => {
    const observed = [];
    for await (const chunk of call({
      model: "m",
      messages: [],
      stream: true,
      stream_options: { include_usage: true },
    })) {
      observed.push(chunk);
    }
    assert.ok(
      observed.includes(usageOnlyChunk),
      "a caller-supplied stream_options.include_usage chunk must reach the consumer",
    );
  }, { failsWith: "must reach the consumer" });
});
