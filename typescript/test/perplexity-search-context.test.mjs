import assert from "node:assert/strict";
import test from "node:test";

import { CaptureRuntime, DEFAULT_TEXT_MAX_BYTES } from "../dist/capture.js";
import { setCaptureRuntime } from "../dist/wrap.js";
import { wrap } from "../dist/index.js";

function stubRuntime(rows) {
  return new CaptureRuntime(
    { enqueue(row) { rows.push(row); return true; } },
    { captureText: true, appRoot: "", skipFrames: [], textMaxBytes: DEFAULT_TEXT_MAX_BYTES },
  );
}

function response(searchContextSize) {
  return {
    id: "request-1",
    usage: { prompt_tokens: 1, completion_tokens: 1, ...(searchContextSize === undefined ? {} : { search_context_size: searchContextSize }) },
    choices: [{ message: { content: "ok" }, finish_reason: "stop" }],
  };
}

function client({ baseURL, create } = {}) {
  return {
    ...(baseURL === undefined ? {} : { baseURL }),
    chat: { completions: { create: create ?? (() => response(undefined)) } },
  };
}

test("request search_context_size normalizes all tiers", () => {
  const rows = [];
  setCaptureRuntime(stubRuntime(rows));
  try {
    for (const expected of ["low", "medium", "high"]) {
      const wrapped = client({ create: () => response(undefined) });
      wrap(wrapped, "openai");
      wrapped.chat.completions.create({
        model: "sonar", messages: [],
        web_search_options: { search_context_size: `  ${expected.toUpperCase()}  ` },
      });
    }
    assert.deepEqual(rows.map((row) => row.search_context_size), ["low", "medium", "high"]);
  } finally {
    setCaptureRuntime();
  }
});

test("response search_context_size wins and invalid values are absent", () => {
  const rows = [];
  setCaptureRuntime(stubRuntime(rows));
  try {
    const wrapped = client({ create: () => response(" HIGH ") });
    wrap(wrapped, "openai");
    wrapped.chat.completions.create({
      model: "sonar", messages: [], web_search_options: { search_context_size: "low" },
    });
    assert.equal(rows[0].search_context_size, "high");

    for (const value of ["", "unknown", 1]) {
      const invalid = client({ create: () => response(value) });
      wrap(invalid, "openai");
      invalid.chat.completions.create({
        model: "sonar", messages: [], web_search_options: { search_context_size: value },
      });
    }
    assert.ok(rows.slice(1).every((row) => !("search_context_size" in row)));
  } finally {
    setCaptureRuntime();
  }
});

test("stream uses the last chunk with usage and request fallback", async () => {
  const rows = [];
  setCaptureRuntime(stubRuntime(rows));
  try {
    const first = { choices: [{ delta: { content: "ok" } }] };
    const earlierUsageChunk = { choices: [], usage: { search_context_size: " LOW " } };
    const usageChunk = { choices: [], usage: { search_context_size: " MEDIUM " } };
    const final = { choices: [{ delta: { content: "" }, finish_reason: "stop" }] };
    const wrapped = client({
      create: async function* () { yield first; yield earlierUsageChunk; yield usageChunk; yield final; },
    });
    wrap(wrapped, "openai");
    for await (const _chunk of wrapped.chat.completions.create({ model: "sonar", messages: [], stream: true })) {}
    assert.equal(rows[0].search_context_size, "medium");

    const fallback = client({
      create: async function* () { yield first; yield final; },
    });
    wrap(fallback, "openai");
    for await (const _chunk of fallback.chat.completions.create({
      model: "sonar", messages: [], stream: true,
      web_search_options: { search_context_size: " HIGH " },
    })) {}
    assert.equal(rows[1].search_context_size, "high");
  } finally {
    setCaptureRuntime();
  }
});

test("a raising search_context_size property does not drop the row", () => {
  const rows = [];
  setCaptureRuntime(stubRuntime(rows));
  try {
    const usage = { prompt_tokens: 1, completion_tokens: 1 };
    Object.defineProperty(usage, "search_context_size", {
      enumerable: true,
      get() { throw new Error("provider property failed"); },
    });
    const wrapped = client({ create: () => ({ ...response(undefined), usage }) });
    wrap(wrapped, "openai");
    wrapped.chat.completions.create({ model: "gpt-test", messages: [] });
    assert.equal(rows.length, 1);
    assert.ok(!("search_context_size" in rows[0]));
  } finally {
    setCaptureRuntime();
  }
});

test("exact HTTPS Perplexity host is auto-labeled and normal OpenAI stays unchanged", () => {
  const rows = [];
  setCaptureRuntime(stubRuntime(rows));
  try {
    for (const [baseURL, expected] of [
      ["https://api.perplexity.ai", "perplexity"],
      ["https://api.perplexity.ai.evil.com/v1", "openai"],
      ["http://api.perplexity.ai/v1", "openai"],
    ]) {
      const wrapped = client({ baseURL });
      wrap(wrapped);
      wrapped.chat.completions.create({ model: "sonar", messages: [] });
      assert.equal(rows.at(-1).provider, expected);
    }

    const explicit = client({ baseURL: "https://api.perplexity.ai/v1" });
    wrap(explicit, "openai");
    explicit.chat.completions.create({ model: "sonar", messages: [] });
    assert.equal(rows.at(-1).provider, "openai");

    const normal = client();
    wrap(normal, "openai");
    normal.chat.completions.create({ model: "gpt-test", messages: [] });
    assert.equal(rows.at(-1).provider, "openai");
    assert.ok(!("search_context_size" in rows.at(-1)));
  } finally {
    setCaptureRuntime();
  }
});
