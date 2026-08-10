import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { mkdirSync, mkdtempSync, readFileSync, realpathSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import {
  discoverRepoConfig,
  ensureRepoConfig,
  normalizeGithubRemote,
  writeConfigAtomically,
} from "../dist/repo-config.js";

function tempDir() {
  return realpathSync(mkdtempSync(join(tmpdir(), "metergraph-repo-config-")));
}

function git(args, cwd) {
  execFileSync("git", args, { cwd, stdio: "ignore" });
}

function initRepoWithOrigin(root, origin) {
  git(["init"], root);
  git(["config", "user.email", "test@example.com"], root);
  git(["config", "user.name", "Test"], root);
  git(["remote", "add", "origin", origin], root);
}

// --- normalizeGithubRemote ---------------------------------------------------

test("normalizeGithubRemote handles ssh and https forms", () => {
  const cases = {
    "git@github.com:owner/repo.git": "owner/repo",
    "git@github.com:owner/repo": "owner/repo",
    "https://github.com/owner/repo.git": "owner/repo",
    "https://github.com/owner/repo": "owner/repo",
    "https://github.com/owner/repo/": "owner/repo",
    "ssh://git@github.com/owner/repo.git": "owner/repo",
  };
  for (const [url, expected] of Object.entries(cases)) {
    assert.equal(normalizeGithubRemote(url), expected);
  }
});

test("normalizeGithubRemote returns undefined for non-github hosts", () => {
  assert.equal(normalizeGithubRemote("git@gitlab.com:owner/repo.git"), undefined);
  assert.equal(normalizeGithubRemote("not a url"), undefined);
});

// --- discoverRepoConfig (pure, read-only) ------------------------------------

test("discoverRepoConfig finds file at appRoot", (t) => {
  const root = tempDir();
  t.after(() => rmSync(root, { recursive: true, force: true }));
  mkdirSync(join(root, ".metergraph"));
  writeFileSync(
    join(root, ".metergraph", "config.json"),
    JSON.stringify({ version: 2, repository: "acme/widgets" }),
  );

  const config = discoverRepoConfig(root);

  assert.ok(config);
  assert.equal(config.repository, "acme/widgets");
});

test("discoverRepoConfig walks up from a nested appRoot", (t) => {
  const root = tempDir();
  t.after(() => rmSync(root, { recursive: true, force: true }));
  mkdirSync(join(root, ".metergraph"));
  writeFileSync(
    join(root, ".metergraph", "config.json"),
    JSON.stringify({ version: 2, repository: "acme/monorepo" }),
  );
  const nested = join(root, "services", "backend");
  mkdirSync(nested, { recursive: true });

  const config = discoverRepoConfig(nested);

  assert.ok(config);
  assert.equal(config.repository, "acme/monorepo");
  assert.equal(config.repoRoot, root);
});

test("discoverRepoConfig returns undefined and logs nothing when absent", (t) => {
  const root = tempDir();
  t.after(() => rmSync(root, { recursive: true, force: true }));
  const warnings = [];
  const originalWarn = console.warn;
  console.warn = (...args) => warnings.push(args.join(" "));

  const config = discoverRepoConfig(root);
  console.warn = originalWarn;

  assert.equal(config, undefined);
  assert.deepEqual(warnings, []);
});

test("discoverRepoConfig ignores malformed json and warns", (t) => {
  const root = tempDir();
  t.after(() => rmSync(root, { recursive: true, force: true }));
  mkdirSync(join(root, ".metergraph"));
  writeFileSync(join(root, ".metergraph", "config.json"), "{not json");
  const warnings = [];
  const originalWarn = console.warn;
  console.warn = (...args) => warnings.push(args.join(" "));

  const config = discoverRepoConfig(root);
  console.warn = originalWarn;

  assert.equal(config, undefined);
  assert.ok(warnings.some((message) => message.includes("could not read it")));
});

test("discoverRepoConfig ignores unsupported version and warns", (t) => {
  const root = tempDir();
  t.after(() => rmSync(root, { recursive: true, force: true }));
  mkdirSync(join(root, ".metergraph"));
  writeFileSync(
    join(root, ".metergraph", "config.json"),
    JSON.stringify({ version: 99, repository: "acme/widgets" }),
  );
  const warnings = [];
  const originalWarn = console.warn;
  console.warn = (...args) => warnings.push(args.join(" "));

  const config = discoverRepoConfig(root);
  console.warn = originalWarn;

  assert.equal(config, undefined);
  assert.ok(warnings.some((message) => message.includes("unsupported schema version")));
});

test("discoverRepoConfig works without a .git directory", (t) => {
  const root = tempDir();
  t.after(() => rmSync(root, { recursive: true, force: true }));
  mkdirSync(join(root, ".metergraph"));
  writeFileSync(
    join(root, ".metergraph", "config.json"),
    JSON.stringify({ version: 2, repository: "acme/widgets" }),
  );

  const config = discoverRepoConfig(root);

  assert.ok(config);
  assert.equal(config.repository, "acme/widgets");
});

// --- ensureRepoConfig (discovery + git detection + atomic write) -----------

test("ensureRepoConfig writes config for https origin", (t) => {
  const root = tempDir();
  t.after(() => rmSync(root, { recursive: true, force: true }));
  initRepoWithOrigin(root, "https://github.com/acme/widgets.git");

  const config = ensureRepoConfig(root);

  assert.ok(config);
  assert.equal(config.repository, "acme/widgets");
  const written = JSON.parse(readFileSync(join(root, ".metergraph", "config.json"), "utf8"));
  assert.deepEqual(written, { version: 2, repository: "acme/widgets" });
});

test("ensureRepoConfig writes config for ssh origin", (t) => {
  const root = tempDir();
  t.after(() => rmSync(root, { recursive: true, force: true }));
  initRepoWithOrigin(root, "git@github.com:acme/widgets.git");

  const config = ensureRepoConfig(root);

  assert.ok(config);
  assert.equal(config.repository, "acme/widgets");
});

test("ensureRepoConfig writes at git top level from a nested appRoot", (t) => {
  const root = tempDir();
  t.after(() => rmSync(root, { recursive: true, force: true }));
  initRepoWithOrigin(root, "https://github.com/acme/monorepo.git");
  const nested = join(root, "services", "backend");
  mkdirSync(nested, { recursive: true });

  const config = ensureRepoConfig(nested);

  assert.ok(config);
  assert.equal(config.repoRoot, root);
  assert.doesNotThrow(() => readFileSync(join(root, ".metergraph", "config.json")));
  assert.throws(() => readFileSync(join(nested, ".metergraph", "config.json")));
});

test("ensureRepoConfig reads origin through a git worktree commondir", (t) => {
  const root = tempDir();
  const worktree = `${root}-worktree`;
  t.after(() => rmSync(worktree, { recursive: true, force: true }));
  t.after(() => rmSync(root, { recursive: true, force: true }));
  initRepoWithOrigin(root, "https://github.com/acme/worktree.git");
  git(["commit", "--allow-empty", "-m", "initial"], root);
  git(["worktree", "add", worktree], root);

  const config = ensureRepoConfig(worktree);

  assert.ok(config);
  assert.equal(config.repository, "acme/worktree");
  assert.equal(config.repoRoot, realpathSync(worktree));
});

test("ensureRepoConfig prefers an existing config over git detection", (t) => {
  const root = tempDir();
  t.after(() => rmSync(root, { recursive: true, force: true }));
  initRepoWithOrigin(root, "https://github.com/acme/from-git.git");
  mkdirSync(join(root, ".metergraph"));
  const existingPath = join(root, ".metergraph", "config.json");
  writeFileSync(existingPath, JSON.stringify({ version: 2, repository: "acme/from-file" }));

  const config = ensureRepoConfig(root);

  assert.equal(config.repository, "acme/from-file");
});

test("ensureRepoConfig is idempotent", (t) => {
  const root = tempDir();
  t.after(() => rmSync(root, { recursive: true, force: true }));
  initRepoWithOrigin(root, "https://github.com/acme/widgets.git");
  const first = ensureRepoConfig(root);
  const configPath = join(root, ".metergraph", "config.json");
  const contentBefore = readFileSync(configPath, "utf8");

  const second = ensureRepoConfig(root);

  assert.deepEqual(second, first);
  assert.equal(readFileSync(configPath, "utf8"), contentBefore);
});

test("ensureRepoConfig returns undefined when no git repo", (t) => {
  const root = tempDir();
  t.after(() => rmSync(root, { recursive: true, force: true }));

  assert.equal(ensureRepoConfig(root), undefined);
  assert.throws(() => readFileSync(join(root, ".metergraph", "config.json")));
});

test("ensureRepoConfig returns undefined when no origin remote", (t) => {
  const root = tempDir();
  t.after(() => rmSync(root, { recursive: true, force: true }));
  git(["init"], root);

  assert.equal(ensureRepoConfig(root), undefined);
});

test("ensureRepoConfig returns undefined when origin is not github", (t) => {
  const root = tempDir();
  t.after(() => rmSync(root, { recursive: true, force: true }));
  initRepoWithOrigin(root, "git@gitlab.com:acme/widgets.git");

  assert.equal(ensureRepoConfig(root), undefined);
});

test("writeConfigAtomically never overwrites a file written between discovery and write", (t) => {
  const root = tempDir();
  t.after(() => rmSync(root, { recursive: true, force: true }));
  mkdirSync(join(root, ".metergraph"));
  const winnerPath = join(root, ".metergraph", "config.json");
  writeFileSync(winnerPath, JSON.stringify({ version: 2, repository: "acme/winner" }) + "\n");

  const result = writeConfigAtomically(root, "acme/loser");

  assert.ok(result);
  assert.equal(result.repository, "acme/winner");
  assert.equal(JSON.parse(readFileSync(winnerPath, "utf8")).repository, "acme/winner");
});
