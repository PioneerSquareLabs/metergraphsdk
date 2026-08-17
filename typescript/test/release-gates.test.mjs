import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const packageJson = JSON.parse(
  await readFile(new URL("../package.json", import.meta.url), "utf8"),
);

test("release lifecycle validates the packed package", () => {
  assert.match(packageJson.scripts.prepublishOnly, /test:package/);
  assert.equal(
    packageJson.scripts["test:package"],
    "npm run build && node --test test/package/packed-package.test.mjs",
  );
});
