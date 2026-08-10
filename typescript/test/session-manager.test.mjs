import assert from "node:assert/strict";
import http from "node:http";
import test from "node:test";

import { SessionManager } from "../dist/session.js";

async function serve(handler) {
  const server = http.createServer(handler);
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  return server;
}

function expiresIn(ms) {
  return new Date(Date.now() + ms).toISOString();
}

test("getToken performs exchange and caches result", async (t) => {
  const server = await serve((request, response) => {
    request.resume();
    response.writeHead(200, { "content-type": "application/json" });
    response.end(JSON.stringify({
      session_token: "session-abc",
      expires_at: expiresIn(300_000),
      repository_id: "repo_123",
    }));
  });
  t.after(() => new Promise((resolve) => server.close(resolve)));
  const address = server.address();

  const manager = new SessionManager(
    "app-token-secret",
    `http://127.0.0.1:${address.port}`,
    "owner/repo",
    "0.4.0",
  );

  assert.equal(await manager.getToken(), "session-abc");
});

test("getToken reuses cached token without a second request", async (t) => {
  let calls = 0;
  const server = await serve((request, response) => {
    request.resume();
    calls += 1;
    response.writeHead(200, { "content-type": "application/json" });
    response.end(JSON.stringify({
      session_token: `session-${calls}`,
      expires_at: expiresIn(300_000),
      repository_id: "repo_123",
    }));
  });
  t.after(() => new Promise((resolve) => server.close(resolve)));
  const address = server.address();

  const manager = new SessionManager(
    "app-token-secret",
    `http://127.0.0.1:${address.port}`,
    "owner/repo",
    "0.4.0",
  );

  const first = await manager.getToken();
  const second = await manager.getToken();

  assert.equal(first, "session-1");
  assert.equal(second, "session-1");
  assert.equal(calls, 1);
});

test("exchange request has the agreed shape", async (t) => {
  let captured;
  const server = await serve((request, response) => {
    const chunks = [];
    request.on("data", (chunk) => chunks.push(chunk));
    request.on("end", () => {
      captured = {
        path: request.url,
        authorization: request.headers.authorization,
        body: JSON.parse(Buffer.concat(chunks).toString("utf8")),
      };
      response.writeHead(200, { "content-type": "application/json" });
      response.end(JSON.stringify({
        session_token: "session-abc",
        expires_at: expiresIn(300_000),
        repository_id: "repo_123",
      }));
    });
  });
  t.after(() => new Promise((resolve) => server.close(resolve)));
  const address = server.address();

  const manager = new SessionManager(
    "app-token-secret",
    `http://127.0.0.1:${address.port}`,
    "owner/repo",
    "0.4.0",
  );
  await manager.getToken();

  assert.equal(captured.path, "/v1/ingest/sessions");
  assert.equal(captured.authorization, "Bearer app-token-secret");
  assert.deepEqual(captured.body, {
    protocol_version: 2,
    repository: "owner/repo",
    sdk_version: "0.4.0",
  });
});

test("getToken returns undefined when server unreachable", async () => {
  const manager = new SessionManager(
    "app-token-secret",
    "http://127.0.0.1:1",
    "owner/repo",
    "0.4.0",
    1_000,
  );

  assert.equal(await manager.getToken(), undefined);
});

test("getToken returns undefined when response is missing session_token", async (t) => {
  const server = await serve((request, response) => {
    request.resume();
    response.writeHead(200, { "content-type": "application/json" });
    response.end(JSON.stringify({ repository_id: "repo_123" }));
  });
  t.after(() => new Promise((resolve) => server.close(resolve)));
  const address = server.address();

  const manager = new SessionManager(
    "app-token-secret",
    `http://127.0.0.1:${address.port}`,
    "owner/repo",
    "0.4.0",
  );

  assert.equal(await manager.getToken(), undefined);
});

test("failed exchange is backed off instead of retried per batch", async (t) => {
  let calls = 0;
  const server = await serve((request, response) => {
    request.resume();
    calls += 1;
    response.writeHead(500);
    response.end();
  });
  t.after(() => new Promise((resolve) => server.close(resolve)));
  const address = server.address();
  const manager = new SessionManager(
    "app-token-secret",
    `http://127.0.0.1:${address.port}`,
    "owner/repo",
    "0.4.0",
  );

  assert.equal(await manager.getToken(), undefined);
  assert.equal(await manager.getToken(), undefined);
  assert.equal(calls, 1);
});

test("invalidate forces a fresh exchange on next getToken", async (t) => {
  let calls = 0;
  const server = await serve((request, response) => {
    request.resume();
    calls += 1;
    response.writeHead(200, { "content-type": "application/json" });
    response.end(JSON.stringify({
      session_token: `session-${calls}`,
      expires_at: expiresIn(300_000),
      repository_id: "repo_123",
    }));
  });
  t.after(() => new Promise((resolve) => server.close(resolve)));
  const address = server.address();

  const manager = new SessionManager(
    "app-token-secret",
    `http://127.0.0.1:${address.port}`,
    "owner/repo",
    "0.4.0",
  );

  const first = await manager.getToken();
  manager.invalidate();
  const second = await manager.getToken();

  assert.equal(first, "session-1");
  assert.equal(second, "session-2");
  assert.equal(calls, 2);
});

test("getToken refreshes once the cached token is near expiry", async (t) => {
  let calls = 0;
  const server = await serve((request, response) => {
    request.resume();
    calls += 1;
    response.writeHead(200, { "content-type": "application/json" });
    response.end(JSON.stringify({
      session_token: `session-${calls}`,
      expires_at: expiresIn(1_000), // well inside the refresh margin
      repository_id: "repo_123",
    }));
  });
  t.after(() => new Promise((resolve) => server.close(resolve)));
  const address = server.address();

  const manager = new SessionManager(
    "app-token-secret",
    `http://127.0.0.1:${address.port}`,
    "owner/repo",
    "0.4.0",
  );

  const first = await manager.getToken();
  const second = await manager.getToken();

  assert.equal(first, "session-1");
  assert.equal(second, "session-2");
  assert.equal(calls, 2);
});

test("stop clears cached token and short-circuits further exchanges", async (t) => {
  let calls = 0;
  const server = await serve((request, response) => {
    request.resume();
    calls += 1;
    response.writeHead(200, { "content-type": "application/json" });
    response.end(JSON.stringify({
      session_token: "session-abc",
      expires_at: expiresIn(300_000),
      repository_id: "repo_123",
    }));
  });
  t.after(() => new Promise((resolve) => server.close(resolve)));
  const address = server.address();

  const manager = new SessionManager(
    "app-token-secret",
    `http://127.0.0.1:${address.port}`,
    "owner/repo",
    "0.4.0",
  );

  await manager.getToken();
  manager.stop();
  const tokenAfterStop = await manager.getToken();

  assert.equal(tokenAfterStop, undefined);
  assert.equal(calls, 1);
});
