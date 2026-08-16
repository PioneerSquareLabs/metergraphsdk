import assert from "node:assert/strict";
import test from "node:test";

import * as PublicApi from "../dist/index.js";

const REQUIRED_VALUE_EXPORTS = ["deferred", "DeferredIneligibleError"];

// Internal batch-adapter plumbing that must stay out of the package root —
// only the customer-facing deferred() entry point and its supporting types
// are meant to be public.
const FORBIDDEN_VALUE_EXPORTS = [
  "runDeferred",
  "createOpenAIBatchAdapter",
  "createAnthropicBatchAdapter",
  "createGoogleBatchAdapter",
  "ProviderBatchError",
];

test("package root exports the customer-facing deferred values", () => {
  for (const name of REQUIRED_VALUE_EXPORTS) {
    assert.equal(typeof PublicApi[name], "function", `expected ${name} to be exported as a function`);
  }
});

test("package root does not export internal batch-adapter machinery", () => {
  for (const name of FORBIDDEN_VALUE_EXPORTS) {
    assert.equal(name in PublicApi, false, `expected ${name} not to be exported from the package root`);
  }
});
