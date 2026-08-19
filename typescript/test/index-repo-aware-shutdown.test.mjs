import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { mkdtempSync, rmSync } from "node:fs";
import http from "node:http";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import { flush, init, shutdown, wrap } from "../dist/index.js";

function git(args, cwd) {
  execFileSync("git", args, { cwd, stdio: "ignore" });
}

function tempRepoWithOrigin(origin) {
  const root = mkdtempSync(join(tmpdir(), "metergraph-init-shutdown-"));
  git(["init"], root);
  git(["config", "user.email", "test@example.com"], root);
  git(["config", "user.name", "Test"], root);
  git(["remote", "add", "origin", origin], root);
  return root;
}

test("shutdown stops the repo-aware session manager cleanly", async (t) => {
  const root = tempRepoWithOrigin("https://github.com/acme/widgets.git");
  t.after(() => rmSync(root, { recursive: true, force: true }));
  let sessionExchanges = 0;
  let ingested = 0;
  const server = http.createServer((request, response) => {
    if (request.url === "/v1/config") {
      request.resume();
      response.writeHead(200, { "content-type": "application/json" });
      response.end(JSON.stringify({ routes: {} }));
      return;
    }
    if (request.url === "/v1/ingest/sessions") {
      sessionExchanges += 1;
      request.resume();
      response.writeHead(200, { "content-type": "application/json" });
      response.end(JSON.stringify({
        session_token: "session-xyz",
        expires_at: new Date(Date.now() + 300_000).toISOString(),
        repository_id: "repo_123",
      }));
      return;
    }
    const chunks = [];
    request.on("data", (chunk) => chunks.push(chunk));
    request.on("end", () => {
      ingested += 1;
      response.writeHead(202);
      response.end();
    });
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  t.after(() => new Promise((resolve) => server.close(resolve)));
  const address = server.address();

  init({
    token: "mg_test",
    ingestUrl: `http://127.0.0.1:${address.port}`,
    appRoot: root,
    repository: "acme/widgets",
    transport: "background",
    flushMs: 60_000,
    configPollMs: 60_000,
  });
  await new Promise((resolve) => setTimeout(resolve, 30));

  const client = wrap({
    chat: {
      completions: {
        async create() {
          return { id: "req_1", choices: [{ message: { content: "ok" }, finish_reason: "stop" }] };
        },
      },
    },
  }, "openai");
  await client.chat.completions.create({ model: "m", messages: [] });

  await assert.doesNotReject(shutdown());
  assert.equal(await flush(), true); // no transport left; resolves trivially, doesn't throw
  assert.equal(sessionExchanges, 1);
  assert.equal(ingested, 1); // pending telemetry is delivered before the session is stopped
});
