import assert from "node:assert/strict";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import { init, shutdown, wrap } from "../dist/index.js";

test("repeated explicit init warns once without configuration details", async (t) => {
  const root = mkdtempSync(join(tmpdir(), "metergraph-init-diagnostics-"));
  t.after(() => rmSync(root, { recursive: true, force: true }));
  t.after(() => shutdown());
  const warnings = [];
  t.mock.method(console, "warn", (message) => warnings.push(String(message)));

  const initial = {
    token: "secret-initial",
    ingestUrl: "http://127.0.0.1:9",
    repository: "acme/widgets",
    environment: "staging",
    captureText: false,
    appRoot: root,
    configPollMs: 60_000,
  };
  init(initial);
  init(initial);
  init({ ...initial, token: "secret-conflict", repository: "other/widgets", environment: "prod" });
  init({ ...initial, token: "secret-third", repository: "third/widgets" });
  wrap({ chat: { completions: { create() {} } } }, "openai");

  const repeatedInitWarnings = warnings.filter((message) => message.includes("called more than once"));
  assert.deepEqual(repeatedInitWarnings, [
    "Metergraph init() was called more than once; the first configuration remains active.",
  ]);
  assert.doesNotMatch(repeatedInitWarnings[0], /secret-initial|secret-conflict|secret-third/);
  assert.doesNotMatch(repeatedInitWarnings[0], /token|repository|environment/);
});
