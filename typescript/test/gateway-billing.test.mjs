import assert from "node:assert/strict";
import test from "node:test";

import { wrap } from "../dist/index.js";
import { CaptureRuntime, DEFAULT_TEXT_MAX_BYTES } from "../dist/capture.js";
import { setCaptureRuntime } from "../dist/wrap.js";
import { detectGateway, resolveGateway, gatewayEvidence } from "../dist/gateway.js";

const OPENROUTER_MODEL = "anthropic/claude-sonnet-4.6";
const MISSING = Symbol("missing");

function stubRuntime(rows, options = {}) {
  return new CaptureRuntime(
    { enqueue(row) { rows.push(row); return true; } },
    { captureText: true, appRoot: "", skipFrames: [], textMaxBytes: DEFAULT_TEXT_MAX_BYTES, ...options },
  );
}

function chatUsage(cost, upstream) {
  const usage = { prompt_tokens: 920, completion_tokens: 110 };
  if (cost !== MISSING) usage.cost = cost;
  if (upstream !== MISSING) usage.cost_details = { upstream_inference_cost: upstream };
  return usage;
}

function chatResponse({ model = OPENROUTER_MODEL, cost = 0.00482, upstream = 0.00482 } = {}) {
  return {
    id: "req_openrouter_1",
    model,
    usage: chatUsage(cost, upstream),
    choices: [{ message: { content: "hi" }, finish_reason: "stop" }],
  };
}

function openrouterClient({ baseURL = "https://openrouter.ai/api/v1", create } = {}) {
  return {
    baseURL,
    apiKey: "sk-or-supersecret",
    chat: { completions: { create: create ?? (() => chatResponse()) } },
    responses: null,
  };
}

// Detection: exact HTTPS openrouter.ai only.

for (const baseURL of [
  "https://openrouter.ai",
  "https://openrouter.ai/api/v1",
  "https://openrouter.ai/api/v1/",
]) {
  test(`detectGateway accepts ${baseURL}`, () => {
    assert.equal(detectGateway({ baseURL, chat: {} }), "openrouter");
  });
}

for (const baseURL of [
  "http://openrouter.ai/api/v1",            // not HTTPS
  "https://openrouter.ai.evil.com/api/v1",  // lookalike suffix
  "https://myopenrouter.ai/api/v1",         // substring / prefix
  "https://api.openrouter.ai/api/v1",       // subdomain, not exact
  "https://openrouter.ai.example/v1",       // different TLD lookalike
  "https://example.com/openrouter.ai",      // host is example.com
]) {
  test(`detectGateway rejects ${baseURL}`, () => {
    assert.equal(detectGateway({ baseURL, chat: {} }), undefined);
  });
}

test("detectGateway with no base URL is undefined", () => {
  assert.equal(detectGateway({ chat: {} }), undefined);
});

// Override resolution.

test("resolveGateway canonicalizes case", () => {
  assert.equal(resolveGateway("OpenRouter"), "openrouter");
});

test("resolveGateway rejects unsupported", () => {
  assert.throws(() => resolveGateway("litellm"), /unsupported gateway/);
});

test("resolveGateway message excludes the caller value", () => {
  const secret = "sk-or-secret-vendor-xyz";
  let message = "";
  try { resolveGateway(secret); } catch (error) { message = String(error.message); }
  assert.ok(message.length > 0);
  assert.ok(!message.includes(secret));
  assert.ok(!message.includes(secret.toUpperCase()));
  assert.ok(message.includes("openrouter"));
});

// gatewayEvidence unit: identity always, cost only on qualified endpoints.

test("gatewayEvidence emits identity but no cost on Responses endpoint", () => {
  const evidence = gatewayEvidence("openrouter", "responses", {
    model: OPENROUTER_MODEL,
    usage: { cost: 0.99 },
  });
  assert.equal(evidence.gateway, "openrouter");
  assert.equal(evidence.served_model, OPENROUTER_MODEL);
  assert.equal(evidence.reported_cost_usd, undefined);
});

// Direct-provider rows unchanged.

test("direct OpenAI row has no gateway fields", () => {
  const rows = [];
  setCaptureRuntime(stubRuntime(rows));
  try {
    const client = wrap({
      chat: { completions: { create: () => chatResponse() } },
      responses: null,
    }, "openai");
    client.chat.completions.create({ model: "gpt-test", messages: [{ role: "user", content: "hi" }] });
    assert.equal(rows.length, 1);
    const row = rows[0];
    assert.equal(row.provider, "openai");
    assert.equal(row.model, "gpt-test");
    for (const field of [
      "gateway",
      "served_model",
      "reported_cost_usd",
      "reported_upstream_cost_usd",
      "reported_cost_source",
      "reported_upstream_cost_source",
    ]) {
      assert.ok(!(field in row), `${field} should be absent`);
    }
  } finally {
    setCaptureRuntime();
  }
});

// Non-streaming Chat Completions: full evidence, return identity preserved.

test("non-stream OpenRouter emits full evidence", () => {
  const rows = [];
  setCaptureRuntime(stubRuntime(rows));
  try {
    const client = openrouterClient();
    const same = wrap(client);
    assert.equal(same, client); // return identity preserved
    const response = client.chat.completions.create({
      model: OPENROUTER_MODEL,
      messages: [{ role: "user", content: "hi" }],
    });
    assert.equal(response.id, "req_openrouter_1");
    const row = rows[0];
    assert.equal(row.provider, "openai");
    assert.equal(row.model, OPENROUTER_MODEL);
    assert.equal(row.gateway, "openrouter");
    assert.equal(row.served_model, OPENROUTER_MODEL);
    assert.equal(row.reported_cost_usd, 0.00482);
    assert.equal(row.reported_upstream_cost_usd, 0.00482);
    assert.equal(row.reported_cost_source, "openrouter.usage.cost");
    assert.equal(row.reported_upstream_cost_source, "openrouter.usage.cost_details.upstream_inference_cost");
    assert.equal(row.input_tokens, 920);
    assert.equal(row.output_tokens, 110);
    assert.ok(!("served_provider" in row));
  } finally {
    setCaptureRuntime();
  }
});

test("zero reported cost is preserved", () => {
  const rows = [];
  setCaptureRuntime(stubRuntime(rows));
  try {
    const client = openrouterClient({ create: () => chatResponse({ cost: 0, upstream: 0 }) });
    wrap(client);
    client.chat.completions.create({ model: OPENROUTER_MODEL, messages: [] });
    const row = rows[0];
    assert.equal(row.reported_cost_usd, 0);
    assert.equal(row.reported_cost_source, "openrouter.usage.cost");
    assert.equal(row.reported_upstream_cost_usd, 0);
  } finally {
    setCaptureRuntime();
  }
});

test("missing cost omits cost but keeps gateway and served_model", () => {
  const rows = [];
  setCaptureRuntime(stubRuntime(rows));
  try {
    const client = openrouterClient({ create: () => chatResponse({ cost: MISSING, upstream: MISSING }) });
    wrap(client);
    client.chat.completions.create({ model: OPENROUTER_MODEL, messages: [] });
    const row = rows[0];
    assert.equal(row.gateway, "openrouter");
    assert.equal(row.served_model, OPENROUTER_MODEL);
    for (const field of ["reported_cost_usd", "reported_cost_source", "reported_upstream_cost_usd", "reported_upstream_cost_source"]) {
      assert.ok(!(field in row));
    }
  } finally {
    setCaptureRuntime();
  }
});

test("missing served model is omitted", () => {
  const rows = [];
  setCaptureRuntime(stubRuntime(rows));
  try {
    const client = openrouterClient({ create: () => chatResponse({ model: null }) });
    wrap(client);
    client.chat.completions.create({ model: OPENROUTER_MODEL, messages: [] });
    const row = rows[0];
    assert.equal(row.gateway, "openrouter");
    assert.ok(!("served_model" in row));
  } finally {
    setCaptureRuntime();
  }
});

for (const bad of [-1, -0.5, Number.NaN, Number.POSITIVE_INFINITY, true, false, "0.5", null]) {
  test(`malformed cost ${String(bad)} is omitted`, () => {
    const rows = [];
    setCaptureRuntime(stubRuntime(rows));
    try {
      const client = openrouterClient({ create: () => chatResponse({ cost: bad, upstream: bad }) });
      wrap(client);
      client.chat.completions.create({ model: OPENROUTER_MODEL, messages: [] });
      const row = rows[0];
      assert.equal(row.gateway, "openrouter");
      assert.ok(!("reported_cost_usd" in row));
      assert.ok(!("reported_cost_source" in row));
      assert.ok(!("reported_upstream_cost_usd" in row));
      assert.ok(!("reported_upstream_cost_source" in row));
    } finally {
      setCaptureRuntime();
    }
  });
}

test("upstream source only when upstream emitted", () => {
  const rows = [];
  setCaptureRuntime(stubRuntime(rows));
  try {
    const client = openrouterClient({ create: () => chatResponse({ cost: 0.01, upstream: MISSING }) });
    wrap(client);
    client.chat.completions.create({ model: OPENROUTER_MODEL, messages: [] });
    const row = rows[0];
    assert.equal(row.reported_cost_usd, 0.01);
    assert.equal(row.reported_cost_source, "openrouter.usage.cost");
    assert.ok(!("reported_upstream_cost_usd" in row));
    assert.ok(!("reported_upstream_cost_source" in row));
  } finally {
    setCaptureRuntime();
  }
});

// Responses API: identity emitted, cost not qualified.

test("Responses API emits identity without cost", () => {
  const rows = [];
  setCaptureRuntime(stubRuntime(rows));
  try {
    const client = {
      baseURL: "https://openrouter.ai/api/v1",
      chat: { completions: { create: () => chatResponse() } },
      responses: {
        create: () => ({
          id: "resp_1",
          model: OPENROUTER_MODEL,
          usage: { input_tokens: 10, output_tokens: 2, cost: 0.99 },
          status: "completed",
        }),
      },
    };
    wrap(client);
    client.responses.create({ model: OPENROUTER_MODEL, input: "hi" });
    const row = rows[0];
    assert.equal(row.endpoint, "responses");
    assert.equal(row.gateway, "openrouter");
    assert.equal(row.served_model, OPENROUTER_MODEL);
    for (const field of ["reported_cost_usd", "reported_cost_source", "reported_upstream_cost_usd", "reported_upstream_cost_source"]) {
      assert.ok(!(field in row));
    }
  } finally {
    setCaptureRuntime();
  }
});

// Streaming: every gateway chunk preserved by identity/order, incl. final usage.

test("stream preserves all chunks and captures final usage evidence", async () => {
  const rows = [];
  setCaptureRuntime(stubRuntime(rows));
  try {
    const chunkA = { model: OPENROUTER_MODEL, choices: [{ delta: { content: "Hel" }, finish_reason: null }] };
    const chunkB = { model: OPENROUTER_MODEL, choices: [{ delta: { content: "lo" }, finish_reason: "stop" }] };
    const usageChunk = {
      model: OPENROUTER_MODEL,
      choices: [],
      usage: { prompt_tokens: 920, completion_tokens: 110, cost: 0.00482, cost_details: { upstream_inference_cost: 0.001 } },
    };
    async function* streamed() { yield chunkA; yield chunkB; yield usageChunk; }
    const client = openrouterClient({ create: () => streamed() });
    wrap(client);

    const seen = [];
    for await (const chunk of client.chat.completions.create({
      model: OPENROUTER_MODEL,
      messages: [{ role: "user", content: "hi" }],
      stream: true,
    })) {
      seen.push(chunk);
    }
    assert.deepEqual(seen, [chunkA, chunkB, usageChunk]);
    assert.equal(seen[0], chunkA);
    assert.equal(seen[1], chunkB);
    assert.equal(seen[2], usageChunk);

    assert.equal(rows.length, 1);
    const row = rows[0];
    assert.equal(row.stream, true);
    assert.equal(row.gateway, "openrouter");
    assert.equal(row.served_model, OPENROUTER_MODEL);
    assert.equal(row.reported_cost_usd, 0.00482);
    assert.equal(row.reported_upstream_cost_usd, 0.001);
    assert.equal(row.input_tokens, 920);
  } finally {
    setCaptureRuntime();
  }
});

// Custom-domain override.

test("custom-domain override enables extraction", () => {
  const rows = [];
  setCaptureRuntime(stubRuntime(rows));
  try {
    const client = openrouterClient({ baseURL: "https://llm.internal.example/v1" });
    assert.equal(detectGateway(client), undefined); // would not auto-detect
    wrap(client, { gateway: "openrouter" });
    client.chat.completions.create({ model: OPENROUTER_MODEL, messages: [] });
    const row = rows[0];
    assert.equal(row.gateway, "openrouter");
    assert.equal(row.reported_cost_usd, 0.00482);
    assert.equal(row.provider, "openai");
  } finally {
    setCaptureRuntime();
  }
});

// Configuration combinations.

test("consistent provider plus gateway option is allowed", () => {
  const rows = [];
  setCaptureRuntime(stubRuntime(rows));
  try {
    const client = openrouterClient({ baseURL: "https://llm.internal.example/v1" });
    wrap(client, { provider: "openai", gateway: "openrouter" });
    client.chat.completions.create({ model: OPENROUTER_MODEL, messages: [] });
    const row = rows[0];
    assert.equal(row.gateway, "openrouter");
    assert.equal(row.reported_cost_usd, 0.00482);
  } finally {
    setCaptureRuntime();
  }
});

for (const provider of ["anthropic", "google"]) {
  test(`contradictory provider ${provider} with gateway is rejected`, () => {
    const client = openrouterClient();
    assert.throws(() => wrap(client, { provider, gateway: "openrouter" }), /OpenAI-compatible/);
  });
}

test("unsupported gateway option is rejected", () => {
  const client = openrouterClient();
  assert.throws(() => wrap(client, { gateway: "portkey" }), /unsupported gateway/);
});

test("gateway override requires an OpenAI-compatible client", () => {
  const anthropicLike = {
    baseURL: "https://llm.internal.example",
    apiKey: "sk-secret",
    messages: { create: () => ({}) },
  };
  assert.throws(() => wrap(anthropicLike, { gateway: "openrouter" }), /OpenAI-compatible/);
});

test("rejection message hides secrets", () => {
  const secretUrl = "https://tenant-abc.secret-host.example/v1";
  const secretKey = "sk-or-this-is-secret";
  const client = { baseURL: secretUrl, apiKey: secretKey, messages: { create: () => ({}) } };
  let message = "";
  try { wrap(client, { gateway: "openrouter" }); } catch (error) { message = String(error.message); }
  assert.ok(message.length > 0);
  assert.ok(!message.includes(secretUrl));
  assert.ok(!message.includes(secretKey));
});

// Fail-open: an extraction fault never alters the provider result or base row.

test("gateway extraction fault is fail-open", () => {
  const rows = [];
  setCaptureRuntime(stubRuntime(rows));
  try {
    const usage = { prompt_tokens: 920, completion_tokens: 110 };
    Object.defineProperty(usage, "cost", { enumerable: true, get() { throw new Error("telemetry fault"); } });
    const sentinel = {
      id: "req_fault",
      model: OPENROUTER_MODEL,
      usage,
      choices: [{ message: { content: "ok" }, finish_reason: "stop" }],
    };
    const client = openrouterClient({ create: () => sentinel });
    wrap(client);
    const result = client.chat.completions.create({ model: OPENROUTER_MODEL, messages: [] });
    assert.equal(result, sentinel); // provider result unchanged
    assert.equal(rows.length, 1);
    const row = rows[0];
    assert.equal(row.input_tokens, 920);
    assert.ok(!("reported_cost_usd" in row));
  } finally {
    setCaptureRuntime();
  }
});
