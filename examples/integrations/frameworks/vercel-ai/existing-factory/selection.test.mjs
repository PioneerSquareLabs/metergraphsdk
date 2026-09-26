import assert from "node:assert/strict";
import test from "node:test";

import { parseModelSelection } from "./selection.mjs";

test("preserves colons inside model IDs", () => {
  assert.deepEqual(parseModelSelection("compatible:llama3.2:latest"), {
    provider: "compatible",
    model: "llama3.2:latest",
  });
});

test("uses the documented default selection", () => {
  assert.deepEqual(parseModelSelection(), {
    provider: "openai",
    model: "gpt-5-mini",
  });
});
