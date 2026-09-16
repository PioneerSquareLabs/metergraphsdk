import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import * as PublicApi from "../dist/index.js";
import { CaptureRuntime, DEFAULT_TEXT_MAX_BYTES } from "../dist/capture.js";
import { templateHash } from "../dist/template.js";
import {
  DEFAULT_SCRUB_CATEGORIES,
  removeSensitiveKeys,
  scrubText,
  scrubValue,
} from "../dist/scrub.js";

const fixture = JSON.parse(readFileSync(new URL("../../spec/scrub-cases.json", import.meta.url), "utf8"));

function runtime(rows, options = {}) {
  return new CaptureRuntime(
    { enqueue(row) { rows.push(row); return true; } },
    { captureText: true, scrubText: false, appRoot: "", skipFrames: [], textMaxBytes: DEFAULT_TEXT_MAX_BYTES, ...options },
  );
}

function request() {
  return {
    model: "test-model",
    messages: [{ role: "user", content: "email a@example.com phone 206-555-0100 key sk-proj-abcdefghijklmnopqrstuvwxyz" }],
    tools: [{ type: "function", function: { name: "lookup" } }],
  };
}

function response() {
  return {
    id: "response-1",
    choices: [{
      message: {
        content: "email b@example.com phone +1 (206) 555-0100 key sk-ant-api03-abcdefghijklmnop",
        tool_calls: [{
          id: "call-1",
          function: { name: "lookup", arguments: JSON.stringify({ email: "c@example.com", phone: "206-555-0100", key: "sk-proj-abcdefghijklmnopqrstuvwxyz" }) },
        }],
      },
      finish_reason: "stop",
    }],
  };
}

test("shared fixture cases", () => {
  for (const item of fixture.cases) {
    assert.equal(
      scrubText(item.input, item.categories ?? DEFAULT_SCRUB_CATEGORIES),
      item.expected,
      item.name,
    );
  }
  JSON.parse(fixture.cases.find((item) => item.name === "JSON remains valid").expected);
});

test("scrub helpers are exported from the package root", () => {
  assert.equal(typeof PublicApi.scrubText, "function");
  assert.equal(typeof PublicApi.scrubValue, "function");
  assert.deepEqual(PublicApi.DEFAULT_SCRUB_CATEGORIES, ["secret", "profile_url", "email", "phone"]);
});

test("scrubText validates input and categories", () => {
  assert.throws(() => scrubText(123), TypeError);
  assert.throws(() => scrubText("hello", ["unknown"]), /unknown scrub category/);
});

test("scrubValue walks nested values without touching keys", () => {
  const value = { email: "a@example.com", nested: ["206-555-0100", ["sk-proj-abcdefghijklmnopqrstuvwxyz", 3]] };
  assert.deepEqual(scrubValue(value), {
    email: "<email>",
    nested: ["<phone>", ["<secret>", 3]],
  });
  assert.equal(value.email, "a@example.com");
});

test("removeSensitiveKeys preserves the existing key filter", () => {
  assert.deepEqual(removeSensitiveKeys({ Authorization: "secret", keep: { token: "hidden", text: "ok" } }), { keep: { text: "ok" } });
});

function captureRow(scrubEnabled, captureEnabled = true, stream = false) {
  const rows = [];
  const capture = runtime(rows, { scrubText: scrubEnabled, captureText: captureEnabled });
  const state = capture.start("openai", "responses", request());
  capture.finish(state, response(), {
    stream,
    responseText: stream ? "stream e@example.com 206-555-0100 sk-proj-abcdefghijklmnopqrstuvwxyz" : undefined,
  });
  return rows[0];
}

test("opt-in capture scrubs request, response, tool calls, and streams", () => {
  const row = captureRow(true, true, true);
  for (const field of ["request_json", "response_text", "tool_calls"]) {
    const encoded = JSON.stringify(row[field]);
    assert.doesNotMatch(encoded, /@example\.com|206-555-0100|sk-proj-/);
  }
  assert.match(row.request_json, /<email>/);
  assert.match(row.response_text, /<phone>/);
  assert.match(row.response_text, /<secret>/);
  assert.match(JSON.stringify(row.tool_calls), /<secret>/);
  assert.equal(row.template_hash, templateHash(request()));
});

test("capture remains unchanged by default and emits no text when disabled", () => {
  const row = captureRow(false);
  assert.match(row.request_json, /a@example\.com/);
  assert.match(row.response_text, /206-555-0100/);
  const metadataOnly = captureRow(true, false);
  assert.equal(metadataOnly.request_json, undefined);
  assert.equal(metadataOnly.response_text, undefined);
  assert.deepEqual(Object.keys(metadataOnly.tool_calls[0]).sort(), ["call_id", "idempotency", "name", "status"]);
});

test("default response keeps tool argument keys", () => {
  const rows = [];
  const capture = runtime(rows, { scrubText: false });
  const state = capture.start("openai", "responses", { model: "test-model" });
  capture.finish(state, {
    id: "response-token",
    choices: [{
      message: {
        content: "ok",
        tool_calls: [{
          id: "call-token",
          function: { name: "lookup", arguments: JSON.stringify({ token: "present" }) },
        }],
      },
      finish_reason: "stop",
    }],
  });

  assert.match(rows[0].response_text, /"token":"present"/);
});

test("capture scrubs values after serialization", () => {
  const value = {
    toJSON() {
      return "opaque a@example.com";
    },
  };
  const rows = [];
  const capture = runtime(rows, { scrubText: true });
  const state = capture.start("openai", "responses", { model: "test-model" });
  capture.finish(state, {
    id: "response-serialized",
    output: [{
      type: "function_call",
      call_id: "call-serialized",
      name: "lookup",
      arguments: value,
    }],
  });

  assert.doesNotMatch(rows[0].response_text, /@example\.com/);
  assert.match(rows[0].response_text, /<email>/);
});
