import assert from "node:assert/strict";
import { mkdirSync, mkdtempSync, realpathSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import * as repoConfigModule from "../dist/repo-config.js";
import { discoverRepoConfig } from "../dist/repo-config.js";

function tempDir(t) {
  const root = realpathSync(mkdtempSync(join(tmpdir(), "metergraph-repo-config-")));
  t.after(() => rmSync(root, { recursive: true, force: true }));
  return root;
}

function writeConfig(root, document) {
  mkdirSync(join(root, ".metergraph"));
  writeFileSync(join(root, ".metergraph", "config.json"), JSON.stringify(document));
}

test("repo config module has no detection or write API", () => {
  assert.equal("ensureRepoConfig" in repoConfigModule, false);
  assert.equal("writeConfigAtomically" in repoConfigModule, false);
});

test("discoverRepoConfig finds a file and defaults its missing version", (t) => {
  const root = tempDir(t);
  writeConfig(root, { repository: "acme/widgets" });
  const config = discoverRepoConfig(root);
  assert.equal(config?.repository, "acme/widgets");
  assert.equal(config?.repoRoot, root);
});

test("discoverRepoConfig walks upward without Git metadata", (t) => {
  const root = tempDir(t);
  writeConfig(root, { version: 2, repository: "acme/monorepo" });
  const nested = join(root, "services", "backend");
  mkdirSync(nested, { recursive: true });
  const config = discoverRepoConfig(nested);
  assert.equal(config?.repository, "acme/monorepo");
  assert.equal(config?.repoRoot, root);
});

test("discoverRepoConfig returns undefined and logs nothing when absent", (t) => {
  const root = tempDir(t);
  const warnings = [];
  const originalWarn = console.warn;
  console.warn = (...args) => warnings.push(args.join(" "));
  try {
    assert.equal(discoverRepoConfig(root), undefined);
  } finally {
    console.warn = originalWarn;
  }
  assert.deepEqual(warnings, []);
});

test("discoverRepoConfig ignores malformed JSON and warns", (t) => {
  const root = tempDir(t);
  mkdirSync(join(root, ".metergraph"));
  writeFileSync(join(root, ".metergraph", "config.json"), "{not json");
  const warnings = [];
  const originalWarn = console.warn;
  console.warn = (...args) => warnings.push(args.join(" "));
  try {
    assert.equal(discoverRepoConfig(root), undefined);
  } finally {
    console.warn = originalWarn;
  }
  assert.ok(warnings.some((message) => message.includes("could not read it")));
});

test("discoverRepoConfig ignores unsupported versions and warns", (t) => {
  const root = tempDir(t);
  writeConfig(root, { version: 99, repository: "acme/widgets" });
  const warnings = [];
  const originalWarn = console.warn;
  console.warn = (...args) => warnings.push(args.join(" "));
  try {
    assert.equal(discoverRepoConfig(root), undefined);
  } finally {
    console.warn = originalWarn;
  }
  assert.ok(warnings.some((message) => message.includes("unsupported schema version")));
});
