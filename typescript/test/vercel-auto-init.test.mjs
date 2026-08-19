import assert from "node:assert/strict";
import test from "node:test";

import { flush, shutdown, vercelAISDKMiddleware } from "../dist/index.js";

test("Vercel middleware initializes capture from its Metergraph options", async (t) => {
  t.after(() => shutdown());
  const requests = [];
  t.mock.method(globalThis, "fetch", async (input, options = {}) => {
    const url = String(input);
    requests.push({ url, options });
    if (url.endsWith("/v1/config")) {
      return new Response(JSON.stringify({ routes: {} }), { status: 200 });
    }
    if (url.endsWith("/v1/ingest/sessions")) {
      return new Response(JSON.stringify({
        session_token: "session-token",
        expires_at: new Date(Date.now() + 300_000).toISOString(),
      }), { status: 201 });
    }
    if (url.endsWith("/v1/ingest")) return new Response(null, { status: 202 });
    throw new Error(`unexpected URL: ${url}`);
  });

  const middleware = vercelAISDKMiddleware({
    token: "app-token",
    ingestUrl: "https://collector.example",
    repository: "acme/widgets",
    environment: "test",
    transport: "buffered",
  });
  const result = await middleware.wrapGenerate({
    model: { provider: "openai", modelId: "gpt-5-mini" },
    params: { prompt: [{ role: "user", content: "hello" }] },
    doGenerate: async () => ({
      content: [{ type: "text", text: "hi" }],
      usage: { inputTokens: 1, outputTokens: 1 },
      finishReason: "stop",
      response: { id: "response-1", modelId: "gpt-5-mini" },
    }),
    doStream: async () => { throw new Error("not used"); },
  });

  assert.equal(result.response.id, "response-1");
  assert.equal(await flush(), true);
  const sessionRequest = requests.find(({ url }) => url.endsWith("/v1/ingest/sessions"));
  assert.ok(sessionRequest);
  const sessionPayload = JSON.parse(sessionRequest.options.body);
  assert.equal(sessionPayload.protocol_version, 2);
  assert.equal(sessionPayload.repository, "acme/widgets");
  assert.equal(typeof sessionPayload.sdk_version, "string");
  const ingestRequest = requests.find(({ url }) => url.endsWith("/v1/ingest"));
  assert.ok(ingestRequest);
  const payload = JSON.parse(await ingestRequest.options.body.text());
  assert.equal(payload.rows.length, 1);
  assert.equal(payload.rows[0].environment, "test");
  assert.equal(ingestRequest.options.headers.Authorization, "Bearer session-token");
});
