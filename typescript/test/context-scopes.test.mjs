import assert from "node:assert/strict";
import test from "node:test";

import {
  contextSnapshot,
  setDefaultTags,
  setSession,
  setTags,
  withContext,
  withSession,
  withTags,
} from "../dist/context.js";

test("overlapping jobs keep session and tags isolated", async () => {
  let release;
  const gate = new Promise((resolve) => { release = resolve; });
  const seen = [];

  const first = withContext({ sessionId: "job-a", tags: { customer: "a" } }, async () => {
    await gate;
    seen.push(contextSnapshot());
  });
  const second = withContext({ sessionId: "job-b", tags: { customer: "b" } }, async () => {
    release();
    await new Promise((resolve) => setTimeout(resolve, 0));
    seen.push(contextSnapshot());
  });
  await Promise.all([first, second]);

  assert.deepEqual(seen.map(({ sessionId, tags }) => [sessionId, tags.customer]).sort(), [
    ["job-a", "a"],
    ["job-b", "b"],
  ]);
  assert.equal(contextSnapshot().sessionId, undefined);
});

test("nested helpers derive child contexts and restore parents after errors", async () => {
  await withSession("parent", async () => {
    await assert.rejects(
      withTags({ layer: "child" }, async () => {
        assert.equal(contextSnapshot().sessionId, "parent");
        assert.equal(contextSnapshot().tags.layer, "child");
        await new Promise((resolve) => setTimeout(resolve, 0));
        throw new Error("boom");
      }),
      /boom/,
    );
    assert.equal(contextSnapshot().sessionId, "parent");
    assert.equal(contextSnapshot().tags.layer, undefined);
  });
  assert.equal(contextSnapshot().sessionId, undefined);
});

test("legacy setters outside a scope warn once and do not create request state", (t) => {
  const warnings = [];
  t.mock.method(console, "warn", (message) => warnings.push(String(message)));

  setSession("unsafe");
  setSession("unsafe-again");
  setTags({ customer: "unsafe" });
  setTags({ customer: "unsafe-again" });

  assert.equal(contextSnapshot().sessionId, undefined);
  assert.equal(contextSnapshot().tags.customer, undefined);
  assert.equal(warnings.filter((message) => message.includes("active Metergraph context")).length, 2);
});

test("scoped setters remain compatible and default tags are explicit", async () => {
  setDefaultTags({ service: "worker" });
  assert.deepEqual(contextSnapshot().tags, { service: "worker" });
  await withContext({}, async () => {
    setSession("scoped");
    setTags({ customer: "acme" });
    assert.equal(contextSnapshot().sessionId, "scoped");
    assert.deepEqual(contextSnapshot().tags, { service: "worker", customer: "acme" });
  });
  assert.equal(contextSnapshot().sessionId, undefined);
  assert.deepEqual(contextSnapshot().tags, { service: "worker" });
  setDefaultTags({});
});
