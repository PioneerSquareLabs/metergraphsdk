import assert from "node:assert/strict";
import { createRequire } from "node:module";
import test from "node:test";
import * as importedMetergraph from "metergraph";

test("package root can be required from CommonJS", () => {
  const require = createRequire(import.meta.url);
  const metergraph = require("metergraph");

  assert.equal(typeof metergraph.init, "function");
  assert.equal(typeof metergraph.wrap, "function");
  assert.equal(typeof metergraph.vercelAISDKMiddleware, "function");
});

test("ESM import and CommonJS require share one SDK instance", () => {
  const require = createRequire(import.meta.url);
  const requiredMetergraph = require("metergraph");

  assert.strictEqual(requiredMetergraph.init, importedMetergraph.init);
  assert.strictEqual(requiredMetergraph.route, importedMetergraph.route);
  assert.deepEqual(
    Object.keys(requiredMetergraph).sort(),
    Object.keys(importedMetergraph).sort(),
  );
});
