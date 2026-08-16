// Type-only surface test: the package root must export exactly the
// customer-facing deferred types, and must not re-export internal
// batch-adapter types. No runtime assertions here — `tsc --noEmit` is the
// test runner.
import type {
  DeferredMetadata,
  DeferredPolicy,
  DeferredProvider,
  DeferredRequest,
  DeferredResult,
  DeferredSource,
  LateBatchInfo,
} from "../dist/index.js";

declare const policy: DeferredPolicy;
declare const result: DeferredResult;
declare const metadata: DeferredMetadata;
declare const source: DeferredSource;
declare const provider: DeferredProvider;
declare const request: DeferredRequest;
declare const lateBatchInfo: LateBatchInfo;
void policy;
void result;
void metadata;
void source;
void provider;
void request;
void lateBatchInfo;

// @ts-expect-error DeferredClock must not be exported from the package root.
import type { DeferredClock } from "../dist/index.js";
// @ts-expect-error BatchHandle must not be exported from the package root.
import type { BatchHandle } from "../dist/index.js";
// @ts-expect-error BatchPollResult must not be exported from the package root.
import type { BatchPollResult } from "../dist/index.js";
// @ts-expect-error BatchPollStatus must not be exported from the package root.
import type { BatchPollStatus } from "../dist/index.js";
// @ts-expect-error ProviderBatchAdapter must not be exported from the package root.
import type { ProviderBatchAdapter } from "../dist/index.js";
// @ts-expect-error ProviderBatchEligibility must not be exported from the package root.
import type { ProviderBatchEligibility } from "../dist/index.js";
// @ts-expect-error ProviderBatchResult must not be exported from the package root.
import type { ProviderBatchResult } from "../dist/index.js";
// @ts-expect-error OpenAIBatchCapableClient must not be exported from the package root.
import type { OpenAIBatchCapableClient } from "../dist/index.js";
// @ts-expect-error AnthropicBatchCapableClient must not be exported from the package root.
import type { AnthropicBatchCapableClient } from "../dist/index.js";
// @ts-expect-error GoogleBatchCapableClient must not be exported from the package root.
import type { GoogleBatchCapableClient } from "../dist/index.js";

// A public caller must not be able to set polling or test-clock mechanics
// on DeferredPolicy — only onLateBatchSettled is a caller-facing hook.
const withPollInterval: DeferredPolicy = {
  deadlineMs: 1000,
  acceptDuplicateProviderExecution: true,
  // @ts-expect-error DeferredPolicy must not accept a caller-supplied pollIntervalMs.
  pollIntervalMs: 10,
};
void withPollInterval;

const withClock: DeferredPolicy = {
  deadlineMs: 1000,
  acceptDuplicateProviderExecution: true,
  // @ts-expect-error DeferredPolicy must not accept a caller-supplied clock.
  clock: { setTimeout: () => 0, clearTimeout: () => {} },
};
void withClock;

const withOnLateBatchSettled: DeferredPolicy = {
  deadlineMs: 1000,
  acceptDuplicateProviderExecution: true,
  onLateBatchSettled: () => {},
};
void withOnLateBatchSettled;
