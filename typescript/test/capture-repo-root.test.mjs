import assert from "node:assert/strict";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";

import { CaptureRuntime } from "../dist/capture.js";

const TEST_DIR = dirname(fileURLToPath(import.meta.url)); // typescript/test
const TS_DIR = join(TEST_DIR, ".."); // typescript
const REPO_ROOT = join(TEST_DIR, "..", ".."); // worktree root

function stubRuntime(rows, options = {}) {
  return new CaptureRuntime(
    { enqueue(row) { rows.push(row); return true; } },
    { captureText: true, appRoot: TS_DIR, skipFrames: [], textMaxBytes: 100 * 1024, ...options },
  );
}

function run(runtime) {
  const state = runtime.start("openai", "responses", { model: "test" }, new Error().stack);
  runtime.finish(state, { id: "r1", output_text: "ok", status: "completed" });
  return state.frames;
}

test("frames include a repo-relative path when under repoRoot", () => {
  const rows = [];
  const runtime = stubRuntime(rows, { repoRoot: REPO_ROOT });

  const frames = run(runtime);

  assert.ok(frames.length > 0);
  const thisFrame = frames.find((f) => f.m.includes("capture-repo-root.test.mjs"));
  assert.ok(thisFrame);
  assert.equal(thisFrame.p, "typescript/test/capture-repo-root.test.mjs");
});

test("frames have no p key when repoRoot is not set", () => {
  const rows = [];
  const runtime = stubRuntime(rows);

  const frames = run(runtime);

  assert.ok(frames.length > 0);
  assert.ok(frames.every((f) => !("p" in f)));
});

test("frames have no p key for frames outside repoRoot", (t) => {
  const unrelated = mkdtempSync(join(tmpdir(), "metergraph-unrelated-repo-"));
  t.after(() => rmSync(unrelated, { recursive: true, force: true }));
  const rows = [];
  const runtime = stubRuntime(rows, { repoRoot: unrelated });

  const frames = run(runtime);

  assert.ok(frames.length > 0);
  assert.ok(frames.every((f) => !("p" in f)));
});

test("repoRoot requires directory containment, not a string prefix", () => {
  const rows = [];
  const runtime = stubRuntime(rows, { repoRoot: REPO_ROOT.slice(0, -1) });

  const frames = run(runtime);

  assert.ok(frames.every((f) => !("p" in f)));
});
