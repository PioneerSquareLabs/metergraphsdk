"""Explicit, opt-in batch-first execution: submit one request through a
provider's Batch API, wait a caller-selected deadline, and fall back to a
single direct call if the batch hasn't finished in time. Never enabled by
wrap()/capture defaults or an environment variable — a caller reaches this
only by importing and calling batch_first() directly, and only after
explicitly acknowledging the duplicate-execution semantic below.

On a missed deadline, the request may execute twice against the provider
(once via batch, once via direct) — an accepted, deliberate semantic,
never silently avoided. What this module guarantees instead: exactly one
direct fallback is ever issued, and a batch result that arrives after the
fallback already won is never returned, never executed, and never mutates
an already-returned result.

This module's own concurrency (background late-batch polling after a
deadline-triggered fallback) uses a daemon threading.Thread/Event, matching
the rest of this SDK's background work (see _transport.Writer,
_config.ConfigPoller) rather than asyncio — batch_first()/run_batch_first()
are synchronous, blocking calls, and the adapters in _provider_batch call
their client's methods directly (synchronously). Async provider clients
(AsyncOpenAI, AsyncAnthropic, google-genai's `.aio` namespace) are not
supported by this milestone's adapters — see the package's batch-adapter
notes for what needs live/gated verification before this is extended to
them.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from ._provider_batch import (
    ProviderBatchAdapter,
    create_anthropic_batch_adapter,
    create_google_batch_adapter,
    create_openai_batch_adapter,
)


class BatchFirstIneligibleError(Exception):
    """Raised before any provider call when a request/policy combination is
    not eligible for batch-first execution — streaming, tools without
    acknowledgement, a missing/false accept_duplicate_provider_execution,
    a non-positive deadline, or an adapter-specific ineligibility."""


@dataclass(frozen=True)
class LateBatchInfo:
    """Reported asynchronously, after run_batch_first()/batch_first() have
    already returned via the direct fallback, if the losing batch
    eventually reaches a terminal state. Its actual result content is
    never included here and never returned — only whether it happened to
    contain a tool-call plan, which may differ from the one the direct
    fallback produced (see allow_duplicate_tool_call_plans)."""

    outcome: str  # "completed" | "failed" | "expired"
    contained_tool_call_plan: bool


@dataclass(frozen=True)
class BatchFirstMetadata:
    execution_mode: str
    deadline_seconds: float
    # Wall-clock time from submission to the canonical result settling.
    batch_wait_seconds: float
    # The batch's own status as of when run_batch_first()/batch_first()
    # returned — not its eventual status if that differs (see
    # LateBatchInfo).
    batch_outcome: str  # "completed" | "failed" | "expired" | "pending_at_deadline"
    canonical_result: str  # "batch" | "direct"
    duplicate_provider_execution: bool
    # Always False when canonical_result is "batch" (no lateness is
    # possible — the batch IS the canonical result). When canonical_result
    # is "direct", this reflects what was known AT THE MOMENT the call
    # returned, which is always False: a completion confirmed later only
    # reaches the caller through on_late_batch_settled, never by mutating
    # this object.
    late_batch_completed: bool
    late_batch_contained_tool_call_plan: bool


@dataclass(frozen=True)
class BatchFirstResult:
    source: str  # "batch" | "direct"
    result: Any
    metadata: BatchFirstMetadata


class BatchFirstClock:
    """Injectable wait primitive. Real time.monotonic()/threading.Event.wait
    by default; tests can substitute a fake for deterministic control, with
    no real waiting — the same purpose as the TypeScript SDK's injectable
    BatchFirstClock, shaped around this SDK's own concurrency primitive
    (threading.Event) rather than setTimeout/clearTimeout."""

    def monotonic(self) -> float:
        return time.monotonic()

    def wait(self, event: threading.Event, timeout: float) -> bool:
        return event.wait(timeout)


def _has_tools(request: Mapping[str, Any]) -> bool:
    tools = request.get("tools")
    return isinstance(tools, list) and len(tools) > 0


def _validate(
    request: Mapping[str, Any],
    *,
    accept_duplicate_provider_execution: bool,
    allow_duplicate_tool_call_plans: bool,
    deadline_seconds: float,
) -> None:
    if accept_duplicate_provider_execution is not True:
        raise BatchFirstIneligibleError(
            "batch_first() requires accept_duplicate_provider_execution=True — a missed "
            "deadline can execute the request twice against the provider"
        )
    if not isinstance(deadline_seconds, (int, float)) or isinstance(deadline_seconds, bool) or deadline_seconds <= 0:
        raise BatchFirstIneligibleError("batch_first() requires a positive deadline_seconds")
    if request.get("stream") is True:
        raise BatchFirstIneligibleError(
            "batch_first() does not support streaming requests — streaming is direct-only"
        )
    if _has_tools(request) and allow_duplicate_tool_call_plans is not True:
        raise BatchFirstIneligibleError(
            "batch_first() requires allow_duplicate_tool_call_plans=True for requests with "
            "tools — the batch result and the direct fallback are independent provider "
            "executions and may each choose a different tool call plan"
        )


_DEFAULT_POLL_INTERVAL_SECONDS = 2.0
_TERMINAL_STATUSES = ("completed", "failed", "expired")


def run_batch_first(
    adapter: ProviderBatchAdapter,
    request: Mapping[str, Any],
    *,
    deadline_seconds: float,
    accept_duplicate_provider_execution: bool,
    allow_duplicate_tool_call_plans: bool = False,
    poll_interval_seconds: float = _DEFAULT_POLL_INTERVAL_SECONDS,
    clock: BatchFirstClock | None = None,
    on_late_batch_settled: Callable[[LateBatchInfo], None] | None = None,
) -> BatchFirstResult:
    """The adapter-injected core state machine — importable directly so
    fake-adapter, fake-clock tests can drive it, and used internally by the
    public, provider-explicit batch_first() below."""
    _validate(
        request,
        accept_duplicate_provider_execution=accept_duplicate_provider_execution,
        allow_duplicate_tool_call_plans=allow_duplicate_tool_call_plans,
        deadline_seconds=deadline_seconds,
    )
    eligibility = adapter.eligibility(request)
    if not eligibility.eligible:
        raise BatchFirstIneligibleError(
            "request is not eligible for batch-first execution: "
            f"{eligibility.reason or 'unsupported by this adapter'}"
        )

    clock = clock or BatchFirstClock()
    started_at = clock.monotonic()

    # Not wrapped: a submission failure means no batch was ever created,
    # and is raised directly rather than treated as a fallback trigger.
    handle = adapter.submit_one(request)

    terminal_event = threading.Event()
    stop_polling = threading.Event()
    terminal_status: list[str | None] = [None]

    def poll_loop() -> None:
        try:
            while not stop_polling.is_set():
                outcome = adapter.poll(handle)
                if outcome.status != "pending":
                    terminal_status[0] = outcome.status
                    terminal_event.set()
                    return
                if clock.wait(stop_polling, poll_interval_seconds):
                    return
        except Exception:
            # An unexpected poll() failure never surfaces here — the
            # deadline simply wins on its own, exactly as if the batch
            # were still pending.
            return

    poll_thread = threading.Thread(target=poll_loop, daemon=True)
    poll_thread.start()

    batch_won = clock.wait(terminal_event, deadline_seconds)

    # A batch reported "completed" but whose result cannot be read (a
    # missing output file, an item-level provider error, a malformed or
    # missing matching line, a transient read failure) is neither a valid
    # canonical batch result nor grounds to raise out of run_batch_first()
    # instead of the promised fallback — it is treated exactly like a
    # batch that reported "failed": exactly one direct fallback, never a
    # second read attempt, never surfaced as an exception.
    unreadable_completed_batch = False

    if batch_won and terminal_status[0] == "completed":
        stop_polling.set()
        try:
            batch_result = adapter.read_result(handle)
            return BatchFirstResult(
                source="batch",
                result=batch_result.result,
                metadata=BatchFirstMetadata(
                    execution_mode="batch_first",
                    deadline_seconds=deadline_seconds,
                    batch_wait_seconds=clock.monotonic() - started_at,
                    batch_outcome="completed",
                    canonical_result="batch",
                    duplicate_provider_execution=False,
                    late_batch_completed=False,
                    late_batch_contained_tool_call_plan=False,
                ),
            )
        except Exception:
            unreadable_completed_batch = True

    # Either the deadline fired first, the batch reached a non-completed
    # terminal status before the deadline, or the batch completed but its
    # result could not be read — either way, issue exactly one direct
    # fallback now, and never wait further (or retry a read) on the batch
    # for the canonical result. Only stop the poll loop when the batch
    # itself already produced a terminal status (it has nothing left to
    # do, so this is a no-op) — when the DEADLINE won, deliberately leave
    # polling running in the background: "keep polling only to write
    # terminal telemetry" requires the loop to keep going, not stop here.
    batch_outcome_at_fallback = (
        "failed"
        if unreadable_completed_batch
        else terminal_status[0]
        if batch_won
        else "pending_at_deadline"
    )
    if batch_won:
        stop_polling.set()

    if not batch_won:
        def late_watcher() -> None:
            # Observe the batch purely for telemetry — never read for its
            # content to be returned, executed, or used to mutate the
            # already-in-flight direct result.
            poll_thread.join()
            status = terminal_status[0]
            if status not in _TERMINAL_STATUSES:
                return
            contained = False
            if status == "completed":
                try:
                    contained = adapter.read_result(handle).contained_tool_call_plan
                except Exception:
                    pass  # telemetry only — never raised, never surfaced
            if on_late_batch_settled is not None:
                try:
                    on_late_batch_settled(
                        LateBatchInfo(outcome=status, contained_tool_call_plan=contained)
                    )
                except Exception:
                    pass  # telemetry only

        threading.Thread(target=late_watcher, daemon=True).start()

    direct_result = adapter.direct(request)
    return BatchFirstResult(
        source="direct",
        result=direct_result.result,
        metadata=BatchFirstMetadata(
            execution_mode="batch_first",
            deadline_seconds=deadline_seconds,
            batch_wait_seconds=clock.monotonic() - started_at,
            batch_outcome=batch_outcome_at_fallback,
            canonical_result="direct",
            duplicate_provider_execution=True,
            late_batch_completed=False,
            late_batch_contained_tool_call_plan=False,
        ),
    )


_PROVIDER_FACTORIES: dict[str, Callable[[Any], ProviderBatchAdapter]] = {
    "openai": create_openai_batch_adapter,
    "anthropic": create_anthropic_batch_adapter,
    "google": create_google_batch_adapter,
}


def _resolve_adapter(client: Any, provider: str) -> ProviderBatchAdapter:
    factory = _PROVIDER_FACTORIES.get(provider)
    if factory is None:
        raise BatchFirstIneligibleError(f'batch_first() has no adapter for provider "{provider}" yet')
    return factory(client)


def batch_first(
    client: Any,
    provider: str,
    request: Mapping[str, Any],
    *,
    deadline_seconds: float,
    accept_duplicate_provider_execution: bool,
    allow_duplicate_tool_call_plans: bool = False,
    on_late_batch_settled: Callable[[LateBatchInfo], None] | None = None,
) -> BatchFirstResult:
    """Explicit, provider-specific batch-first execution. Never inferred
    from the client instance — the caller states the provider, matching
    wrap()'s own explicit-provider option. Polling interval and time
    source are test-only controls on run_batch_first(), not public here."""
    adapter = _resolve_adapter(client, provider)
    return run_batch_first(
        adapter,
        request,
        deadline_seconds=deadline_seconds,
        accept_duplicate_provider_execution=accept_duplicate_provider_execution,
        allow_duplicate_tool_call_plans=allow_duplicate_tool_call_plans,
        on_late_batch_settled=on_late_batch_settled,
    )
