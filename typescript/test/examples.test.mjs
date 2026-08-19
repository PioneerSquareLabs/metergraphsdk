import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { test } from "node:test";
import { fileURLToPath } from "node:url";
import path from "node:path";

const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");

for (const name of ["provider-registry.mjs", "existing-factory.mjs"]) {
  test(`${name} is runnable JavaScript`, () => {
    const example = path.join(repoRoot, "examples", "node-vercel-ai-factory", name);
    const output = execFileSync(process.execPath, ["--check", example], {
      encoding: "utf8",
      stdio: ["ignore", "pipe", "pipe"],
    });
    assert.equal(output, "");
  });
}
