import assert from "node:assert/strict";
import test from "node:test";

import { CaptureRuntime } from "../dist/capture.js";
import { jsonEqual, readDeclarations, validDeclarations } from "../dist/tool-definitions.js";

/**
 * The canonical tool-declaration view and the Anthropic tool-only content rule.
 * Every assertion here fails against the SDK as it was before this change: the
 * declaration view did not exist, and a tool-only Anthropic reply recorded its
 * provider block list as `content`.
 */

function stubRuntime(rows, options = {}) {
  return new CaptureRuntime(
    { enqueue(row) { rows.push(row); return true; } },
    { captureText: true, appRoot: "", skipFrames: [], textMaxBytes: 100 * 1024, ...options },
  );
}

function capture(request, response, { provider = "anthropic", endpoint = "messages",
  options = {}, extra = {} } = {}) {
  const rows = [];
  const runtime = stubRuntime(rows, options);
  const state = runtime.start(provider, endpoint, request);
  runtime.finish(state, response, extra);
  return rows[0];
}

function envelopeOf(row) {
  return JSON.parse(row.response_text);
}

const ANTHROPIC_SCHEMA = {
  type: "object",
  properties: {
    query: { type: "string", description: "what to rank" },
    limit: { type: "integer", default: 5 },
    mode: { type: "string", enum: ["fast", "thorough"] },
    filters: { type: "array", items: { $ref: "#/$defs/filter" } },
  },
  required: ["query"],
  $defs: { filter: { type: "object", properties: { field: { type: "string" } } } },
};

const TOOL_REQUEST = {
  model: "claude-test",
  tools: [{ name: "rank", input_schema: { type: "object" } }],
  tool_choice: { type: "tool", name: "rank" },
};

function toolBlock({ id = "toolu_1", name = "rank", input = { a: 1 }, caller = "direct",
  ...rest } = {}) {
  const block = { type: "tool_use", id, name, input, ...rest };
  if (caller !== null) block.caller = { type: caller };
  return block;
}

function message(content, stop_reason = "tool_use") {
  return { content, stop_reason, id: "msg_1", model: "claude-test" };
}

test("an Anthropic declaration is recorded verbatim", () => {
  const row = capture(
    { model: "claude-test", tools: [{ name: "rank", description: "Rank experts.", input_schema: ANTHROPIC_SCHEMA }] },
    message([], "end_turn"),
  );
  assert.equal(row.tool_definitions.version, 1);
  assert.equal(row.tool_definitions.fidelity, "verbatim");
  assert.equal(row.tool_definitions.scope, "effective");
  assert.deepEqual(row.tool_definitions.declarations, [{
    index: 0,
    container: "tools",
    kind: "function",
    dialect: "anthropic",
    name: "rank",
    description: "Rank experts.",
    schema_key: "input_schema",
    schema: ANTHROPIC_SCHEMA,
    status: "declared",
  }]);
});

test("OpenAI chat and Responses dialects are recognized", () => {
  const chat = capture(
    { model: "gpt-test", tools: [{ type: "function", function: { name: "lookup", parameters: { type: "object" } } }] },
    { choices: [{ message: { content: "hi" } }] },
    { provider: "openai", endpoint: "chat.completions" },
  );
  assert.equal(chat.tool_definitions.declarations[0].dialect, "openai_chat");

  const responses = capture(
    { model: "gpt-test", tools: [{ type: "function", name: "flat", parameters: { type: "object" } }] },
    { output_text: "hi" },
    { provider: "openai", endpoint: "responses" },
  );
  assert.equal(responses.tool_definitions.declarations[0].dialect, "openai_responses");
});

test("Gemini declarations inside config are read", () => {
  const row = capture(
    {
      model: "gemini-test",
      config: { tools: [{ function_declarations: [
        { name: "list_directory", parameters: { type: "object" } },
        { name: "read_file", parameters: { type: "object" } },
      ] }] },
    },
    { candidates: [] },
    { provider: "google", endpoint: "models.generate_content" },
  );
  assert.deepEqual(row.tool_definitions.declarations.map((entry) => entry.name),
    ["list_directory", "read_file"]);
  assert.deepEqual(row.tool_definitions.declarations.map((entry) => entry.container),
    ["config.tools", "config.tools"]);
  assert.equal(row.tool_definitions.scope, "effective");
});

test("a Gemini JSON Schema key is supported, not reported missing", () => {
  const row = capture(
    { model: "gemini-test", tools: [{ function_declarations: [
      { name: "search", parameters_json_schema: { type: "object", title: "Search" } }] }] },
    { candidates: [] },
    { provider: "google", endpoint: "models.generate_content" },
  );
  const [record] = row.tool_definitions.declarations;
  assert.equal(record.schema_key, "parameters_json_schema");
  assert.equal(record.status, "declared");
});

test("the AI SDK inputSchema dialect is recognized", () => {
  const row = capture(
    { model: "claude", tools: [{ type: "function", name: "convert", inputSchema: { type: "object" } }] },
    { content: [] },
    { endpoint: "ai.doGenerate" },
  );
  const [record] = row.tool_definitions.declarations;
  assert.equal(record.dialect, "ai_sdk");
  assert.equal(record.schema_key, "inputSchema");
});

test("every declaration status is reported, in order", () => {
  const row = capture({
    model: "claude-test",
    tools: [
      { name: "good", input_schema: { type: "object" } },
      { name: "web_search", type: "web_search_20250305" },
      { name: "good", input_schema: { type: "object", title: "second" } },
      { type: "function", parameters: { type: "object" } },
      { name: "bad", input_schema: "not-a-schema" },
      { name: 17, input_schema: { type: "object" } },
      "not-a-mapping",
    ],
  }, message([], "end_turn"));

  const records = row.tool_definitions.declarations;
  assert.deepEqual(records.map((entry) => entry.status), [
    "declared", "provider_tool", "declared", "incomplete", "malformed", "malformed", "unsupported",
  ]);
  assert.deepEqual(records.map((entry) => entry.index), [0, 1, 2, 3, 4, 5, 6]);
  assert.equal(records[1].provider_type, "web_search_20250305");
  assert.equal(records[1].dialect, "anthropic");
  assert.equal(records[2].duplicate_of, 0);
  assert.equal(records[2].schema.title, "second");
  assert.equal(records[4].schema, null);
  assert.equal(records[5].name, null);
});

test("unequal schema sources are ambiguous, equal ones resolve by precedence", () => {
  const ambiguous = capture(
    { model: "gemini-test", tools: [{ function_declarations: [{
      name: "search",
      parameters: { type: "object", title: "a" },
      parameters_json_schema: { type: "object", title: "b" },
    }] }] },
    { candidates: [] },
    { provider: "google", endpoint: "models.generate_content" },
  );
  assert.equal(ambiguous.tool_definitions.declarations[0].status, "ambiguous");
  assert.equal(ambiguous.tool_definitions.declarations[0].schema, null);

  const schema = { type: "object" };
  const equal = capture(
    { model: "gemini-test", tools: [{ function_declarations: [
      { name: "search", parameters: schema, parametersJsonSchema: schema }] }] },
    { candidates: [] },
    { provider: "google", endpoint: "models.generate_content" },
  );
  assert.equal(equal.tool_definitions.declarations[0].status, "declared");
  assert.equal(equal.tool_definitions.declarations[0].schema_key, "parametersJsonSchema");
});

test("competing containers make the view inventory, not effective", () => {
  const row = capture({
    model: "claude-test",
    tools: [{ name: "top", input_schema: { type: "object" } }],
    extra_body: { tools: [{ name: "extra", input_schema: { type: "object" } }] },
  }, message([], "end_turn"));
  assert.equal(row.tool_definitions.scope, "inventory");
  assert.deepEqual(row.tool_definitions.declarations.map((entry) => entry.container),
    ["tools", "extra_body.tools"]);
});

test("a non-list container is reported rather than silently dropped", () => {
  const row = capture({ model: "claude-test", tools: "auto" }, message([], "end_turn"));
  const [record] = row.tool_definitions.declarations;
  assert.equal(record.status, "unsupported");
  assert.equal(record.name, null);
});

test("absent and empty tools omit the field", () => {
  assert.equal(capture({ model: "claude-test" }, message([], "end_turn")).tool_definitions, undefined);
  assert.equal(capture({ model: "claude-test", tools: [] }, message([], "end_turn")).tool_definitions,
    undefined);
});

test("the captured provider request is unchanged by the addition", () => {
  const tools = [{ name: "rank", input_schema: ANTHROPIC_SCHEMA }];
  const row = capture({ model: "claude-test", tools }, message([], "end_turn"));
  assert.deepEqual(JSON.parse(row.request_json).tools, tools);
});

test("text capture off withholds the field", () => {
  const row = capture(TOOL_REQUEST, message([], "end_turn"), { options: { captureText: false } });
  assert.equal(row.tool_definitions, undefined);
});

test("redaction marks the view filtered and a hook cannot claim verbatim", () => {
  const row = capture(
    { model: "claude-test", tools: [{ name: "rank", description: "s3cret", input_schema: { type: "object" } }] },
    message([], "end_turn"),
    { options: { redact: (value) => value.replace("s3cret", "<redacted>") } },
  );
  assert.equal(row.tool_definitions.fidelity, "filtered");
  assert.equal(row.tool_definitions.declarations[0].description, "<redacted>");
  assert.ok(!JSON.stringify(row.tool_definitions).includes("s3cret"));
});

test("redaction returning junk omits the field but keeps the row", () => {
  for (const broken of [() => "not json", () => '{"not":"an array"}', () => '[{"index":"zero"}]']) {
    const row = capture(TOOL_REQUEST, message([], "end_turn"), { options: { redact: broken } });
    assert.equal(row.tool_definitions, undefined);
    assert.equal(row.model, "claude-test");
  }
});

test("an oversize view is omitted and marks the row truncated", () => {
  const tools = Array.from({ length: 20 }, (_, index) => ({
    name: `tool_${index}`,
    input_schema: { type: "object", title: "x".repeat(40) },
  }));
  const row = capture({ model: "claude-test", tools }, message([], "end_turn"),
    { options: { textMaxBytes: 400 } });
  assert.equal(row.tool_definitions, undefined);
  assert.equal(row.text_truncated, true);
});

test("a tool-only reply records explicit null content", () => {
  const row = capture(TOOL_REQUEST, message([toolBlock()]));
  const envelope = envelopeOf(row);
  assert.ok("content" in envelope);
  assert.equal(envelope.content, null);
  assert.deepEqual(envelope.tool_calls.map((call) => [call.call_id, call.name, call.arguments]),
    [["toolu_1", "rank", { a: 1 }]]);
});

test("the envelope key set stays inside the downstream contract", () => {
  const envelope = envelopeOf(capture(TOOL_REQUEST, message([toolBlock()])));
  const allowed = new Set(["content", "finish_reason", "model", "request_id", "role", "status",
    "tool_calls"]);
  for (const key of Object.keys(envelope)) assert.ok(allowed.has(key), key);
});

test("a block without a caller, or with null caller siblings, is recognized", () => {
  assert.equal(envelopeOf(capture(TOOL_REQUEST, message([toolBlock({ caller: null })]))).content, null);
  const block = { type: "tool_use", id: "toolu_1", name: "rank", input: { a: 1 },
    caller: { type: "direct", detail: null } };
  assert.equal(envelopeOf(capture(TOOL_REQUEST, message([block]))).content, null);
});

test("a reply stopped for another reason keeps its content", () => {
  for (const stop of ["max_tokens", "end_turn", "stop_sequence", "refusal"]) {
    const envelope = envelopeOf(capture(TOOL_REQUEST, message([toolBlock()], stop)));
    assert.ok(Array.isArray(envelope.content), stop);
  }
});

test("a non-direct caller or an unexpected block key keeps its content", () => {
  assert.ok(Array.isArray(envelopeOf(capture(TOOL_REQUEST,
    message([toolBlock({ caller: "skill" })]))).content));
  assert.ok(Array.isArray(envelopeOf(capture(TOOL_REQUEST,
    message([toolBlock({ cache_control: { type: "ephemeral" } })]))).content));
});

test("duplicate or missing block ids keep their content", () => {
  assert.ok(Array.isArray(envelopeOf(capture(TOOL_REQUEST,
    message([toolBlock(), toolBlock()]))).content));
  assert.ok(Array.isArray(envelopeOf(capture(TOOL_REQUEST,
    message([toolBlock({ id: "" })]))).content));
});

test("mixed text, thinking and other block types are untouched", () => {
  assert.equal(envelopeOf(capture(TOOL_REQUEST,
    message([{ type: "text", text: "thinking aloud" }, toolBlock()]))).content, "thinking aloud");
  assert.ok(Array.isArray(envelopeOf(capture(TOOL_REQUEST,
    message([{ type: "thinking", thinking: "...", signature: "s" }, toolBlock()]))).content));
  for (const type of ["server_tool_use", "web_search_tool_result", "code_execution_tool_result",
    "mcp_tool_use", "mcp_tool_result", "container_upload", "something_new"]) {
    assert.ok(Array.isArray(envelopeOf(capture(TOOL_REQUEST,
      message([{ type, id: "b1", name: "x", input: {} }]))).content), type);
  }
});

test("errors, abandonment and out-of-scope seams are untouched", () => {
  assert.ok(Array.isArray(envelopeOf(capture(TOOL_REQUEST, message([toolBlock()]),
    { extra: { error: new Error("boom") } })).content));
  assert.ok(Array.isArray(envelopeOf(capture(TOOL_REQUEST, message([toolBlock()]),
    { extra: { status: "abandoned", stream: true } })).content));
  assert.ok(Array.isArray(envelopeOf(capture(TOOL_REQUEST, message([toolBlock()]),
    { provider: "bedrock" })).content));
  assert.ok(Array.isArray(envelopeOf(capture(TOOL_REQUEST, message([toolBlock()]),
    { endpoint: "chat.completions" })).content));
});

test("an OpenAI chat tool-call reply keeps the null content it already records", () => {
  const row = capture(
    { model: "gpt-test", tools: [{ type: "function", function: { name: "rank", parameters: { type: "object" } } }] },
    { choices: [{ message: { content: null, tool_calls: [{ id: "call_1",
      function: { name: "rank", arguments: '{"a":1}' } }] }, finish_reason: "tool_calls" }] },
    { provider: "openai", endpoint: "chat.completions" },
  );
  const envelope = envelopeOf(row);
  assert.equal(envelope.content, null);
  assert.equal(envelope.tool_calls[0].call_id, "call_1");
});

function streamChunks({ messageStop = true, blockStop = true, stopReason = "tool_use",
  partial = '{"a":1}', extra = [] } = {}) {
  const chunks = [
    { type: "message_start" },
    { type: "content_block_start", index: 0,
      content_block: toolBlock({ id: "toolu_s", input: {} }) },
    { type: "content_block_delta", index: 0,
      delta: { type: "input_json_delta", partial_json: partial } },
    ...extra,
  ];
  if (blockStop) chunks.push({ type: "content_block_stop", index: 0 });
  if (stopReason) chunks.push({ type: "message_delta", delta: { stop_reason: stopReason } });
  if (messageStop) chunks.push({ type: "message_stop" });
  return chunks;
}

function rawStream(chunks, extra = {}) {
  return envelopeOf(capture(TOOL_REQUEST, chunks[chunks.length - 1],
    { extra: { stream: true, responseChunks: chunks, ...extra } }));
}

test("a completed raw event stream records explicit null content", () => {
  const envelope = rawStream(streamChunks());
  assert.ok("content" in envelope);
  assert.equal(envelope.content, null);
  assert.deepEqual(envelope.tool_calls.map((call) => [call.call_id, call.arguments]),
    [["toolu_s", { a: 1 }]]);
});

test("an incomplete or abandoned stream keeps its content", () => {
  const cases = {
    "no message_stop": streamChunks({ messageStop: false }),
    "no content_block_stop": streamChunks({ blockStop: false }),
    "truncated generation": streamChunks({ stopReason: "max_tokens" }),
    "unparseable arguments": streamChunks({ partial: '{"a":' }),
    "text delta": streamChunks({ extra: [{ type: "content_block_delta", index: 1,
      delta: { type: "text_delta", text: "hi" } }] }),
  };
  for (const [reason, chunks] of Object.entries(cases)) {
    const envelope = rawStream(chunks);
    assert.ok(!("content" in envelope && envelope.content === null), reason);
  }
  const abandoned = rawStream(streamChunks(), { status: "abandoned" });
  assert.ok(!("content" in abandoned && abandoned.content === null));
});

test("validDeclarations refuses smuggled payloads", () => {
  const { declarations } = readDeclarations({ tools: [{ name: "rank", input_schema: { type: "object" } }] });
  assert.ok(validDeclarations(declarations));
  for (const mutation of [
    { name: { nested: "object" } },
    { index: "zero" },
    { status: "invented" },
    { container: "elsewhere" },
    { schema: ["not", "an", "object"] },
    { name: "x".repeat(600) },
    { surprise: "extra key" },
  ]) {
    assert.ok(!validDeclarations([{ ...declarations[0], ...mutation }]), JSON.stringify(mutation));
  }
});

test("competing schema sources use structural equality, not key order", () => {
  const reordered = capture(
    { model: "gemini-test", tools: [{ function_declarations: [{
      name: "search", parameters: { a: 1, b: 2 }, parameters_json_schema: { b: 2, a: 1 } }] }] },
    { candidates: [] },
    { provider: "google", endpoint: "models.generate_content" },
  );
  assert.equal(reordered.tool_definitions.declarations[0].status, "declared");

  const arrays = capture(
    { model: "gemini-test", tools: [{ function_declarations: [{
      name: "search", parameters: { x: [1, 2] }, parameters_json_schema: { x: [2, 1] } }] }] },
    { candidates: [] },
    { provider: "google", endpoint: "models.generate_content" },
  );
  assert.equal(arrays.tool_definitions.declarations[0].status, "ambiguous");

  const booleans = capture(
    { model: "gemini-test", tools: [{ function_declarations: [{
      name: "search", parameters: { d: 1 }, parameters_json_schema: { d: true } }] }] },
    { candidates: [] },
    { provider: "google", endpoint: "models.generate_content" },
  );
  assert.equal(booleans.tool_definitions.declarations[0].status, "ambiguous");
});

test("a schema carrying a legal __proto__ key is copied verbatim", () => {
  const schema = JSON.parse(String.raw`{"type":"object","properties":{"__proto__":{"type":"string"}}}`);
  const row = capture({ model: "claude-test", tools: [{ name: "rank", input_schema: schema }] },
    message([], "end_turn"));
  const copied = row.tool_definitions.declarations[0].schema.properties;
  assert.ok(Object.prototype.hasOwnProperty.call(copied, "__proto__"));
  assert.deepEqual(Object.keys(copied), ["__proto__"]);
  assert.equal(JSON.parse(JSON.stringify(row.tool_definitions)).declarations[0]
    .schema.properties["__proto__"].type, "string");
});

test("a non-finite schema value is unsupported rather than claimed verbatim", () => {
  const row = capture(
    { model: "claude-test", tools: [{ name: "rank", input_schema: { threshold: NaN } }] },
    message([], "end_turn"),
  );
  const [record] = row.tool_definitions.declarations;
  assert.equal(record.status, "unsupported");
  assert.equal(record.schema, null);
});

test("a malformed hook result costs the field and nothing else", () => {
  // The hook is caller-supplied, so its output is untrusted input: an
  // unhashable enum value, a value the durable column cannot hold, or output
  // that is not an array at all must omit the field and keep the row.
  const deep = {};
  let node = deep;
  for (let index = 0; index < 300; index += 1) { node.a = {}; node = node.a; }
  const mutations = [
    ["an unhashable enum value", (records) => { records[0].kind = {}; return records; }],
    ["a list enum value", (records) => { records[0].kind = []; return records; }],
    ["an object status", (records) => { records[0].status = { a: 1 }; return records; }],
    ["a numeric dialect", (records) => { records[0].dialect = 7; return records; }],
    ["a string index", (records) => { records[0].index = "zero"; return records; }],
    ["a lone surrogate name", (records) => {
      records[0].name = String.fromCharCode(0xD800); return records; }],
    ["a NUL description", (records) => {
      records[0].description = `a${String.fromCharCode(0)}b`; return records; }],
    ["a schema past the depth bound", (records) => { records[0].schema = deep; return records; }],
    ["an added key", (records) => { records[0].surprise = "extra"; return records; }],
  ];
  for (const [reason, mutate] of mutations) {
    const row = capture(TOOL_REQUEST, message([], "end_turn"), {
      options: { redact: (value) => JSON.stringify(mutate(JSON.parse(value))) },
    });
    assert.equal(row.tool_definitions, undefined, `a malformed view survived ${reason}`);
    assert.equal(row.model, "claude-test", `the row was lost for ${reason}`);
  }
  for (const [reason, hook] of [
    ["output that is not JSON", () => "not json at all"],
    ["output that is not an array", () => '{"not":"an array"}'],
    ["a hook that throws", () => { throw new Error("boom"); }],
  ]) {
    const row = capture(TOOL_REQUEST, message([], "end_turn"), { options: { redact: hook } });
    assert.equal(row.tool_definitions, undefined, `a malformed view survived ${reason}`);
    assert.equal(row.model, "claude-test", `the row was lost for ${reason}`);
  }
});

test("a hook that introduces a non-finite value yields a filtered view, not a claim", () => {
  // `JSON.stringify` writes NaN as null, so the SDK never sees the non-finite
  // value: it sees a schema that differs from the one it read. The view
  // survives with the changed value and reports `filtered`, which is the
  // honest answer. `JSON.parse` rejects a bare NaN literal, so a hook cannot
  // smuggle one in through the text either.
  const row = capture(TOOL_REQUEST, message([], "end_turn"), {
    options: {
      redact: (value) => {
        const records = JSON.parse(value);
        records[0].schema = { threshold: NaN };
        return JSON.stringify(records);
      },
    },
  });
  assert.equal(row.tool_definitions.fidelity, "filtered");
  assert.deepEqual(row.tool_definitions.declarations[0].schema, { threshold: null });

  const rejected = capture(TOOL_REQUEST, message([], "end_turn"), {
    options: { redact: () => '[{"index":0,"schema":{"t":NaN}}]' },
  });
  assert.equal(rejected.tool_definitions, undefined);
  assert.equal(rejected.model, "claude-test");
});

test("jsonEqual is the equality the recognition rule and fidelity use", () => {
  assert.ok(jsonEqual({ a: 1, b: 2 }, { b: 2, a: 1 }), "object key order is ignored");
  assert.ok(!jsonEqual([1, 2], [2, 1]), "array order is kept");
  assert.ok(!jsonEqual(true, 1), "a boolean is not a number");
  assert.ok(!jsonEqual({ default: true }, { default: 1 }), "recursively");
  assert.ok(jsonEqual(1, 1.0), "numbers compare by value");
  assert.ok(jsonEqual(JSON.parse(String.raw`{"__proto__":1}`),
    JSON.parse(String.raw`{"__proto__":1}`)), "own __proto__ keys compare");
  assert.ok(!jsonEqual({ a: 1 }, { a: 1, b: 2 }), "an added key is a difference");
});
