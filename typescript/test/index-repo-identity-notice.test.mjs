import assert from "node:assert/strict";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import { init, shutdown } from "../dist/index.js";

test("missing repository identity is a one-time notice, not a failure", async (t) => {
  const root = mkdtempSync(join(tmpdir(), "metergraph-identity-notice-"));
  const warnings = [];
  const originalWarn = console.warn;
  console.warn = (...args) => warnings.push(args.join(" "));
  t.after(async () => {
    console.warn = originalWarn;
    await shutdown();
    rmSync(root, { recursive: true, force: true });
  });

  const options = {
    token: "mg_test",
    ingestUrl: "http://127.0.0.1:9",
    appRoot: root,
    transport: "background",
    flushMs: 60_000,
    configPollMs: 60_000,
  };
  init(options);
  init(options);

  const notices = warnings.filter((message) => message.includes("Repository identity is optional"));
  assert.equal(notices.length, 1);
  assert.match(notices[0], /capturing calls normally/);
  assert.doesNotMatch(notices[0], /legacy/i);
  for (const way of [".metergraph/config.json", "METERGRAPH_REPOSITORY", "metergraph setup"]) {
    assert.ok(notices[0].includes(way), way);
  }
});
