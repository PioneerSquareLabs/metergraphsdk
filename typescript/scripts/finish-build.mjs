import { copyFileSync, writeFileSync } from "node:fs";

copyFileSync("src/index-wrapper.mjs", "dist/index.mjs");
writeFileSync("dist/package.json", '{"type":"commonjs"}\n');
