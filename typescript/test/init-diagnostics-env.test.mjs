import assert from "node:assert/strict";
import test from "node:test";

import { init, shutdown } from "../dist/index.js";

test("repeated environment-based init uses the same generic warning", async (t) => {
  const original = { ...process.env };
  t.after(() => {
    process.env = original;
  });
  t.after(() => shutdown());
  process.env.METERGRAPH_APP_TOKEN = "secret-env-initial";
  process.env.METERGRAPH_INGEST_URL = "http://127.0.0.1:9";
  process.env.METERGRAPH_REPOSITORY = "acme/widgets";
  process.env.METERGRAPH_ENV = "staging";
  const warnings = [];
  t.mock.method(console, "warn", (message) => warnings.push(String(message)));

  init();
  process.env.METERGRAPH_APP_TOKEN = "secret-env-conflict";
  process.env.METERGRAPH_REPOSITORY = "other/widgets";
  process.env.METERGRAPH_ENV = "production";
  init();

  const repeatedInitWarnings = warnings.filter((message) => message.includes("called more than once"));
  assert.deepEqual(repeatedInitWarnings, [
    "Metergraph init() was called more than once; the first configuration remains active.",
  ]);
  assert.doesNotMatch(repeatedInitWarnings[0], /secret-env-initial|secret-env-conflict/);
  assert.doesNotMatch(repeatedInitWarnings[0], /token|repository|environment/);
});
