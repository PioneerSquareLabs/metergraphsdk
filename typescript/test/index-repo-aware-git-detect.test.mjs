import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { existsSync, mkdtempSync, readFileSync, rmSync } from "node:fs";
import http from "node:http";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { gunzipSync } from "node:zlib";

import { flush, init, shutdown, wrap } from "../dist/index.js";

function git(args, cwd) {
  execFileSync("git", args, { cwd, stdio: "ignore" });
}

function tempRepoWithOrigin(origin) {
  const root = mkdtempSync(join(tmpdir(), "metergraph-init-git-"));
  git(["init"], root);
  git(["config", "user.email", "test@example.com"], root);
  git(["config", "user.name", "Test"], root);
  git(["remote", "add", "origin", origin], root);
  return root;
}

function fakeServer(sessionToken) {
  const ingestRequests = [];
  const server = http.createServer((request, response) => {
    if (request.url === "/v1/config") {
      request.resume();
      response.writeHead(200, { "content-type": "application/json" });
      response.end(JSON.stringify({ routes: {} }));
      return;
    }
    if (request.url === "/v1/ingest/sessions") {
      request.resume();
      response.writeHead(200, { "content-type": "application/json" });
      response.end(JSON.stringify({
        session_token: sessionToken,
        expires_at: new Date(Date.now() + 300_000).toISOString(),
        repository_id: "repo_123",
      }));
      return;
    }
    const chunks = [];
    request.on("data", (chunk) => chunks.push(chunk));
    request.on("end", () => {
      let body = Buffer.concat(chunks);
      if (request.headers["content-encoding"] === "gzip") body = gunzipSync(body);
      ingestRequests.push({
        authorization: request.headers.authorization,
        body: JSON.parse(body.toString()),
      });
      response.writeHead(202);
      response.end();
    });
  });
  return { server, ingestRequests };
}

test("init discovers a git origin and wires the session token + repo-relative frame path", async (t) => {
  const root = tempRepoWithOrigin("https://github.com/acme/widgets.git");
  t.after(() => rmSync(root, { recursive: true, force: true }));
  const { server, ingestRequests } = fakeServer("session-xyz");
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  t.after(async () => {
    await shutdown();
    await new Promise((resolve) => server.close(resolve));
  });
  const address = server.address();

  init({
    token: "mg_test",
    ingestUrl: `http://127.0.0.1:${address.port}`,
    appRoot: root,
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

  assert.equal(await flush(), true);
  assert.equal(ingestRequests.length, 1);
  assert.equal(ingestRequests[0].authorization, "Bearer session-xyz");

  const written = JSON.parse(readFileSync(join(root, ".metergraph", "config.json"), "utf8"));
  assert.deepEqual(written, { version: 2, repository: "acme/widgets" });
  assert.ok(existsSync(join(root, ".metergraph", "config.json")));
});
