/**
 * Explicit, opt-in deferred execution: submit one request through a
 * provider's Batch API, wait a caller-selected deadline, and fall back to
 * a single direct call if the batch hasn't finished in time. Never enabled
 * by wrap()/capture defaults or by an environment variable — a caller
 * reaches this only by importing and calling deferred() directly, and only
 * after explicitly acknowledging the duplicate-execution semantic below.
 *
 * On a missed deadline, the request may execute twice against the
 * provider (once via batch, once via direct) — an accepted, deliberate
 * semantic, never silently avoided. What this module guarantees instead:
 * exactly one direct fallback is ever issued, and a batch result that
 * arrives after the fallback already won is never returned, never
 * executed, and never mutates an already-resolved result.
 */
import {
  createOpenAIBatchAdapter,
  type DeferredRequest,
  type OpenAIBatchCapableClient,
  type ProviderBatchAdapter,
} from "./provider-batch.js";

export type { DeferredRequest } from "./provider-batch.js";

/** An injectable time source. Real timers by default; tests can supply a
 * fake to drive the deadline deterministically, with no real waiting. */
export interface DeferredClock {
  setTimeout(handler: () => void, ms: number): unknown;
  clearTimeout(handle: unknown): void;
}

function realClock(): DeferredClock {
  return {
    setTimeout: (handler, ms) => setTimeout(handler, ms),
    clearTimeout: (handle) => clearTimeout(handle as ReturnType<typeof setTimeout>),
  };
}

/** Reported asynchronously, after deferred() has already returned via the
 * direct fallback, if the losing batch eventually reaches a terminal
 * state. Its actual result content is never included here and never
 * returned from deferred() — only whether it happened to contain a
 * tool-call plan, which may differ from the one the direct fallback
 * produced (see allowDuplicateToolCallPlans on DeferredPolicy). */
export interface LateBatchInfo {
  outcome: "completed" | "failed" | "expired";
  containedToolCallPlan: boolean;
}

export interface DeferredPolicy {
  /** How long to wait for the batch before issuing the one direct
   * fallback. Required — there is no default deadline. */
  deadlineMs: number;
  /**
   * Required, explicit acknowledgement that a missed deadline can result
   * in the same request executing twice against the provider. Must be
   * exactly `true`; never read from an environment variable, and never
   * defaulted.
   */
  acceptDuplicateProviderExecution: true;
  /**
   * Required, explicit acknowledgement for a request that includes
   * `tools`: the batch result and the direct fallback are two independent
   * provider executions of the same prompt, and may each choose a
   * DIFFERENT tool-call plan (different tool, different arguments). A
   * caller whose tools have side effects must not assume the two plans
   * agree. Without this flag, a request carrying `tools` is rejected
   * before any provider call is made.
   */
  allowDuplicateToolCallPlans?: boolean;
  /** How often to re-check batch status while waiting. Default 2000ms. */
  pollIntervalMs?: number;
  /** Injectable time source — see DeferredClock. */
  clock?: DeferredClock;
  /** Fired once, later, only when the losing batch eventually settles.
   * Never blocks deferred()'s own return. */
  onLateBatchSettled?: (info: LateBatchInfo) => void;
}

export type DeferredSource = "batch" | "direct";

export interface DeferredMetadata {
  execution_mode: "deferred";
  deadline_ms: number;
  /** Wall-clock time from submission to the canonical result settling. */
  batch_wait_ms: number;
  /** The batch's own status as of when deferred() returned — not its
   * eventual status if that differs (see onLateBatchSettled). */
  batch_outcome: "completed" | "failed" | "expired" | "pending_at_deadline";
  canonical_result: DeferredSource;
  duplicate_provider_execution: boolean;
  /** Always false when canonical_result is "batch" (no lateness is
   * possible — the batch IS the canonical result). When
   * canonical_result is "direct", this reflects what was known AT THE
   * MOMENT deferred() returned, which is always false: a completion
   * confirmed later only reaches the caller through
   * onLateBatchSettled, never by mutating this object. */
  late_batch_completed: boolean;
  late_batch_contained_tool_call_plan: boolean;
}

export interface DeferredResult<TResult = unknown> {
  source: DeferredSource;
  result: TResult;
  metadata: DeferredMetadata;
}

/** Thrown before any provider call when a request/policy combination is
 * not eligible for deferred execution — streaming, tools without
 * acknowledgement, a missing/false acceptDuplicateProviderExecution, or
 * an adapter-specific ineligibility. */
export class DeferredIneligibleError extends Error {}

function hasTools(request: DeferredRequest): boolean {
  return Array.isArray(request.tools) && request.tools.length > 0;
}

function validate(request: DeferredRequest, policy: DeferredPolicy): void {
  if (policy.acceptDuplicateProviderExecution !== true) {
    throw new DeferredIneligibleError(
      "deferred() requires policy.acceptDuplicateProviderExecution: true — "
        + "a missed deadline can execute the request twice against the provider",
    );
  }
  if (!Number.isFinite(policy.deadlineMs) || policy.deadlineMs <= 0) {
    throw new DeferredIneligibleError("deferred() requires a positive policy.deadlineMs");
  }
  if (request.stream === true) {
    throw new DeferredIneligibleError(
      "deferred() does not support streaming requests — streaming is direct-only",
    );
  }
  if (hasTools(request) && policy.allowDuplicateToolCallPlans !== true) {
    throw new DeferredIneligibleError(
      "deferred() requires policy.allowDuplicateToolCallPlans: true for requests with "
        + "tools — the batch result and the direct fallback are independent provider "
        + "executions and may each choose a different tool call plan",
    );
  }
}

const DEFAULT_POLL_INTERVAL_MS = 2_000;

type RaceOutcome =
  | { kind: "batch"; status: "completed" | "failed" | "expired" }
  | { kind: "deadline" };

/**
 * The adapter-injected core state machine — exported so fake-adapter,
 * fake-clock tests can drive it directly, and used internally by the
 * public, provider-explicit deferred() below.
 */
export async function runDeferred<TResult>(
  adapter: ProviderBatchAdapter<TResult>,
  request: DeferredRequest,
  policy: DeferredPolicy,
): Promise<DeferredResult<TResult>> {
  validate(request, policy);
  const eligibility = adapter.eligibility(request);
  if (!eligibility.eligible) {
    throw new DeferredIneligibleError(
      `request is not eligible for deferred execution: ${eligibility.reason ?? "unsupported by this adapter"}`,
    );
  }

  const clock = policy.clock ?? realClock();
  const pollIntervalMs = policy.pollIntervalMs ?? DEFAULT_POLL_INTERVAL_MS;
  const startedAt = Date.now();

  const handle = await adapter.submitOne(request);

  let settleDeadline: (() => void) | undefined;
  const deadlineSettled = new Promise<void>((resolve) => {
    settleDeadline = resolve;
  });
  const deadlineTimer = clock.setTimeout(() => settleDeadline?.(), policy.deadlineMs);

  // Resolves at most once, and only from inside the poll loop below, the
  // moment a real terminal status is observed — never resolves at all if
  // the deadline wins first. Racing this directly (rather than racing the
  // loop's own completion promise) means there is no "loop stopped without
  // a terminal status" case to fake a result for: that case simply never
  // resolves this promise, which is exactly the semantics Promise.race
  // needs.
  let resolveBatchTerminal: ((status: "completed" | "failed" | "expired") => void) | undefined;
  const batchTerminalSettled = new Promise<"completed" | "failed" | "expired">((resolve) => {
    resolveBatchTerminal = resolve;
  });

  let stopPolling = false;
  const pollUntilTerminal: Promise<"completed" | "failed" | "expired" | undefined> = (async () => {
    while (!stopPolling) {
      const outcome = await adapter.poll(handle);
      if (outcome.status !== "pending") {
        resolveBatchTerminal?.(outcome.status);
        return outcome.status;
      }
      if (stopPolling) return undefined;
      await new Promise<void>((resolve) => clock.setTimeout(resolve, pollIntervalMs));
    }
    return undefined;
  })();

  const winner = await Promise.race<RaceOutcome>([
    batchTerminalSettled.then((status): RaceOutcome => ({ kind: "batch", status })),
    deadlineSettled.then((): RaceOutcome => ({ kind: "deadline" })),
  ]);

  clock.clearTimeout(deadlineTimer);

  // A batch reported "completed" but whose result cannot be read (a
  // missing output file, an item-level provider error, a malformed or
  // missing matching line, a transient read failure) is neither a valid
  // canonical batch result nor grounds to throw out of deferred() instead
  // of the promised fallback — it is treated exactly like a batch that
  // reported "failed": exactly one direct fallback, never a second read
  // attempt, never surfaced as a rejection.
  let unreadableCompletedBatch = false;

  if (winner.kind === "batch" && winner.status === "completed") {
    stopPolling = true;
    try {
      const { result } = await adapter.readResult(handle);
      return {
        source: "batch",
        result,
        metadata: {
          execution_mode: "deferred",
          deadline_ms: policy.deadlineMs,
          batch_wait_ms: Date.now() - startedAt,
          batch_outcome: "completed",
          canonical_result: "batch",
          duplicate_provider_execution: false,
          late_batch_completed: false,
          late_batch_contained_tool_call_plan: false,
        },
      };
    } catch {
      unreadableCompletedBatch = true;
    }
  }

  // Either the deadline fired first, the batch reached a non-completed
  // terminal status before the deadline, or the batch completed but its
  // result could not be read — either way, issue exactly one direct
  // fallback now, and never wait further (or retry a read) on the batch
  // for the canonical result. Only stop the poll loop when the batch
  // itself already produced a terminal status (it has nothing left to do,
  // so this is a no-op) — when the DEADLINE won, deliberately leave
  // polling running in the background: "keep polling only to write
  // terminal telemetry" requires the loop to keep going, not stop here.
  const batchOutcomeAtFallback = unreadableCompletedBatch
    ? "failed"
    : winner.kind === "batch"
      ? winner.status
      : "pending_at_deadline";
  if (winner.kind === "batch") stopPolling = true;

  const directResultPromise = adapter.direct(request);

  if (winner.kind === "deadline") {
    // Observe the batch purely for telemetry — never read for its content
    // to be returned, executed, or used to mutate the already-in-flight
    // direct result.
    pollUntilTerminal
      .then(async (status) => {
        if (status !== "completed" && status !== "failed" && status !== "expired") return;
        let containedToolCallPlan = false;
        if (status === "completed") {
          try {
            const late = await adapter.readResult(handle);
            containedToolCallPlan = late.containedToolCallPlan;
          } catch {
            /* telemetry only — never thrown, never surfaced */
          }
        }
        policy.onLateBatchSettled?.({ outcome: status, containedToolCallPlan });
      })
      .catch(() => { /* telemetry only */ });
  }

  const { result } = await directResultPromise;
  return {
    source: "direct",
    result,
    metadata: {
      execution_mode: "deferred",
      deadline_ms: policy.deadlineMs,
      batch_wait_ms: Date.now() - startedAt,
      batch_outcome: batchOutcomeAtFallback,
      canonical_result: "direct",
      duplicate_provider_execution: true,
      late_batch_completed: false,
      late_batch_contained_tool_call_plan: false,
    },
  };
}

export type DeferredProvider = "openai";

function resolveAdapter(
  client: unknown,
  provider: DeferredProvider,
): ProviderBatchAdapter {
  if (provider === "openai") {
    return createOpenAIBatchAdapter(client as OpenAIBatchCapableClient);
  }
  throw new DeferredIneligibleError(`deferred() has no adapter for provider "${provider}" yet`);
}

/**
 * Explicit, provider-specific deferred execution. Never inferred from the
 * client instance — the caller states the provider, matching wrap()'s own
 * explicit-provider option.
 */
export async function deferred<TResult = unknown>(
  client: unknown,
  provider: DeferredProvider,
  request: DeferredRequest,
  policy: DeferredPolicy,
): Promise<DeferredResult<TResult>> {
  const adapter = resolveAdapter(client, provider) as ProviderBatchAdapter<TResult>;
  return runDeferred(adapter, request, policy);
}
