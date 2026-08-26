// Offline contract test: runs main() from main.mjs against a local fake that
// plays both the OpenRouter-compatible Chat Completions endpoint and the
// MeterGraph ingest. No network or billable calls. Divergence in main.mjs fails.
import assert from "node:assert/strict";
import http from "node:http";
import test from "node:test";
import { gunzipSync } from "node:zlib";

import { main } from "./main.mjs";

const SERVED_MODEL = "anthropic/claude-sonnet-4.6";
const REPORTED_COST = 0.00482;
const UPSTREAM_COST = 0.00131;

function completion(model) {
  return {
    id: "gen-nonstream",
    object: "chat.completion",
    created: 0,
    model,
    choices: [{ index: 0, message: { role: "assistant", content: "Cache-aware pricing keeps repeated context cheap." }, finish_reason: "stop" }],
    usage: { prompt_tokens: 920, completion_tokens: 110, total_tokens: 1030, cost: REPORTED_COST, cost_details: { upstream_inference_cost: UPSTREAM_COST } },
  };
}

function streamBody(model) {
  const event = (choices, usage) => {
    const payload = { id: "gen-stream", object: "chat.completion.chunk", created: 0, model, choices };
    if (usage) payload.usage = usage;
    return `data: ${JSON.stringify(payload)}\n\n`;
  };
  return [
    event([{ index: 0, delta: { role: "assistant", content: "Streaming " }, finish_reason: null }]),
    event([{ index: 0, delta: { content: "keeps " }, finish_reason: null }]),
    event([{ index: 0, delta: { content: "usage final." }, finish_reason: "stop" }]),
    event([], { prompt_tokens: 920, completion_tokens: 110, total_tokens: 1030, cost: REPORTED_COST, cost_details: { upstream_inference_cost: UPSTREAM_COST } }),
    "data: [DONE]\n\n",
  ].join("");
}

async function readBody(request) {
  const chunks = [];
  for await (const chunk of request) chunks.push(chunk);
  return Buffer.concat(chunks.map((chunk) => Buffer.from(chunk)));
}

function startServer(batches) {
  return http.createServer(async (request, response) => {
    if (request.method === "GET" && request.url.startsWith("/v1/config")) {
      response.writeHead(200, { "content-type": "application/json", etag: '"v1"' });
      response.end(JSON.stringify({ routes: {} }));
      return;
    }
    if (request.url === "/api/v1/chat/completions") {
      const body = JSON.parse((await readBody(request)).toString() || "{}");
      if (body.stream) {
        response.writeHead(200, { "content-type": "text/event-stream" });
        response.end(streamBody(SERVED_MODEL));
      } else {
        response.writeHead(200, { "content-type": "application/json" });
        response.end(JSON.stringify(completion(SERVED_MODEL)));
      }
      return;
    }
    if (request.url === "/v1/ingest/sessions") {
      response.writeHead(200, { "content-type": "application/json" });
      response.end(JSON.stringify({
        session_token: "session-fixture",
        expires_at: new Date(Date.now() + 300_000).toISOString(),
        repository_id: "repo_fixture",
      }));
      return;
    }
    let body = await readBody(request);
    if (request.headers["content-encoding"] === "gzip") body = gunzipSync(body);
    try { batches.push(JSON.parse(body.toString())); } catch { /* non-batch */ }
    response.writeHead(202);
    response.end();
  });
}

test("node-openrouter example: native results and captured wire rows (offline)", async (t) => {
  const batches = [];
  const server = startServer(batches);
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const port = server.address().port;
  t.after(() => new Promise((resolve) => server.close(resolve)));

  process.env.OPENROUTER_BASE_URL = `http://127.0.0.1:${port}/api/v1`;
  process.env.OPENROUTER_API_KEY = "sk-or-PLACEHOLDER";
  process.env.METERGRAPH_APP_TOKEN = "mg_test";
  process.env.METERGRAPH_INGEST_URL = `http://127.0.0.1:${port}`;

  const summary = await main();

  // Native OpenAI results survive the wrapper unchanged.
  assert.equal(summary.nonstream.servedModel, SERVED_MODEL);
  assert.match(summary.nonstream.content, /Cache-aware/);
  assert.equal(summary.stream.servedModel, SERVED_MODEL);
  assert.equal(summary.stream.content, "Streaming keeps usage final.");
  assert.ok(summary.stream.chunkCount >= 4); // final usage chunk stays visible

  // shutdown() delivers exactly two gateway rows: one non-stream, one stream.
  const rows = batches.flatMap((batch) => batch.rows ?? []);
  const gatewayRows = rows.filter((row) => row.gateway === "openrouter");
  assert.equal(gatewayRows.length, 2);
  assert.deepEqual(new Set(gatewayRows.map((row) => Boolean(row.stream))), new Set([false, true]));
  for (const row of gatewayRows) {
    assert.equal(row.provider, "openai");
    assert.equal(row.model, SERVED_MODEL);
    assert.equal(row.served_model, SERVED_MODEL);
    assert.equal(row.reported_cost_usd, REPORTED_COST);
    assert.equal(row.reported_cost_source, "openrouter.usage.cost");
    assert.equal(row.reported_upstream_cost_usd, UPSTREAM_COST);
  }
});
