import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { test } from "node:test";
import { fileURLToPath } from "node:url";
import path from "node:path";

const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");

for (const [folder, name] of [
  ["integrations/frameworks/vercel-ai/provider-registry", "main.mjs"],
  ["integrations/frameworks/vercel-ai/existing-factory", "main.mjs"],
  ["integrations/providers/openrouter/typescript", "main.mjs"],
]) {
  test(`${folder}/${name} is runnable JavaScript`, () => {
    const example = path.join(repoRoot, "examples", folder, name);
    const output = execFileSync(process.execPath, ["--check", example], {
      encoding: "utf8",
      stdio: ["ignore", "pipe", "pipe"],
    });
    assert.equal(output, "");
  });
}
