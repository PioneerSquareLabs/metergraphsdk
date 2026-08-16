from __future__ import annotations

import json
import threading
import time

import pytest

import metergraph
from metergraph._batch_first import (
    BatchFirstClock,
    BatchFirstIneligibleError,
    LateBatchInfo,
    batch_first,
    run_batch_first,
)
from metergraph._provider_batch import (
    BatchHandle,
    BatchPollResult,
    ProviderBatchAdapter,
    ProviderBatchEligibility,
    ProviderBatchResult,
)


REQUEST = {"model": "gpt-5-mini", "input": "hello"}


def base_policy(**overrides):
    policy = {
        "deadline_seconds": 0.2,
        "accept_duplicate_provider_execution": True,
        "poll_interval_seconds": 0.01,
    }
    policy.update(overrides)
    return policy


class FakeAdapter(ProviderBatchAdapter):
    """A fake whose batch completes `batch_completes_after_seconds` after
    submit_one() — matching the plan's "batch result arrives before/after
    the deadline" scenarios with real, small waits (tens of milliseconds)
    rather than a fully virtual clock, since these delays are short enough
    to keep the suite fast while staying deterministic (see the dedicated
    fake-clock test below for the injectable-seam proof)."""

    def __init__(
        self,
        *,
        batch_completes_after_seconds=None,
        batch_outcome="completed",
        direct_delay_seconds=0.005,
        direct_result=None,
        batch_result=None,
        contained_tool_call_plan=False,
        eligible=True,
    ):
        self.batch_completes_after_seconds = batch_completes_after_seconds
        self.batch_outcome = batch_outcome
        self.direct_delay_seconds = direct_delay_seconds
        self.direct_result = direct_result if direct_result is not None else {"via": "direct"}
        self.batch_result = batch_result if batch_result is not None else {"via": "batch"}
        self.contained_tool_call_plan = contained_tool_call_plan
        self._eligible = eligible
        self.submitted_at = None
        self.direct_call_count = 0
        self.poll_count = 0

    def eligibility(self, request):
        return ProviderBatchEligibility(eligible=self._eligible)

    def submit_one(self, request):
        self.submitted_at = time.monotonic()
        return BatchHandle(provider_batch_id="batch_fake_1")

    def poll(self, handle):
        self.poll_count += 1
        if self.batch_completes_after_seconds is None:
            return BatchPollResult(status="pending")
        elapsed = time.monotonic() - self.submitted_at
        if elapsed >= self.batch_completes_after_seconds:
            return BatchPollResult(status=self.batch_outcome)
        return BatchPollResult(status="pending")

    def read_result(self, handle):
        return ProviderBatchResult(
            result=self.batch_result, contained_tool_call_plan=self.contained_tool_call_plan
        )

    def direct(self, request):
        self.direct_call_count += 1
        time.sleep(self.direct_delay_seconds)
        return ProviderBatchResult(result=self.direct_result, contained_tool_call_plan=False)


def test_returns_batch_result_when_it_arrives_before_the_deadline():
    adapter = FakeAdapter(batch_completes_after_seconds=0.05)

    outcome = run_batch_first(adapter, REQUEST, **base_policy(deadline_seconds=0.3))

    assert outcome.source == "batch"
    assert outcome.result == adapter.batch_result
    assert outcome.metadata.canonical_result == "batch"
    assert outcome.metadata.batch_outcome == "completed"
    assert outcome.metadata.duplicate_provider_execution is False
    assert outcome.metadata.late_batch_completed is False


def test_sends_exactly_one_direct_fallback_and_never_returns_a_late_batch_result():
    adapter = FakeAdapter(batch_completes_after_seconds=0.3, direct_delay_seconds=0.02)

    outcome = run_batch_first(adapter, REQUEST, **base_policy(deadline_seconds=0.15))

    assert outcome.source == "direct"
    assert outcome.result == adapter.direct_result
    assert outcome.result != adapter.batch_result
    assert adapter.direct_call_count == 1
    assert outcome.metadata.canonical_result == "direct"
    assert outcome.metadata.duplicate_provider_execution is True
    # At the moment run_batch_first() returns, the batch (completing at
    # ~300ms) has not settled yet — accurate as-of-return, not a claim
    # about the future.
    assert outcome.metadata.late_batch_completed is False

    # Let the background poll observe the late completion, then confirm it
    # never mutated or re-exposed anything through the already-returned
    # value.
    time.sleep(0.2)
    assert outcome.result == adapter.direct_result
    assert adapter.direct_call_count == 1  # still exactly one direct call, ever


def test_on_late_batch_settled_reports_the_late_outcome_without_returning_its_content():
    adapter = FakeAdapter(
        batch_completes_after_seconds=0.05, direct_delay_seconds=0.005, contained_tool_call_plan=True
    )
    late_info = {}

    def on_late(info: LateBatchInfo) -> None:
        late_info["value"] = info

    outcome = run_batch_first(
        adapter,
        REQUEST,
        **base_policy(deadline_seconds=0.01, on_late_batch_settled=on_late),  # fires long before the 50ms batch
    )

    assert outcome.source == "direct"
    time.sleep(0.15)  # give the background poll time to observe completion
    assert late_info["value"] == LateBatchInfo(outcome="completed", contained_tool_call_plan=True)


def test_a_failed_batch_before_the_deadline_falls_back_to_direct_immediately():
    adapter = FakeAdapter(batch_completes_after_seconds=0.02, batch_outcome="failed")
    started_at = time.monotonic()

    outcome = run_batch_first(adapter, REQUEST, **base_policy(deadline_seconds=5.0))

    assert outcome.source == "direct"
    assert outcome.metadata.batch_outcome == "failed"
    assert time.monotonic() - started_at < 1.0, "must not wait out the full 5s deadline after an early batch failure"


def test_a_completed_batch_whose_result_cannot_be_read_falls_back_once_never_raises():
    read_result_calls = 0
    direct_calls = 0
    late_calls = 0

    class UnreadableAdapter(ProviderBatchAdapter):
        def eligibility(self, request):
            return ProviderBatchEligibility(eligible=True)

        def submit_one(self, request):
            return BatchHandle(provider_batch_id="b1")

        def poll(self, handle):
            # Reports completed on the very first check — this is the
            # PRIMARY race outcome, not a late one.
            return BatchPollResult(status="completed")

        def read_result(self, handle):
            nonlocal read_result_calls
            read_result_calls += 1
            # Stands in for: missing output file, an item-level provider
            # error, a malformed/missing matching line, or a transient
            # read failure — all collapse to the same "can't produce a
            # canonical batch result" outcome.
            raise RuntimeError("missing output file")

        def direct(self, request):
            nonlocal direct_calls
            direct_calls += 1
            return ProviderBatchResult(result={"via": "direct"}, contained_tool_call_plan=False)

    def on_late(info):
        nonlocal late_calls
        late_calls += 1

    outcome = run_batch_first(
        UnreadableAdapter(),
        REQUEST,
        # Large on purpose — must not matter; the batch resolves on the
        # first poll.
        **base_policy(deadline_seconds=5.0, on_late_batch_settled=on_late),
    )

    assert outcome.source == "direct"
    assert outcome.result["via"] == "direct"
    assert direct_calls == 1
    assert read_result_calls == 1  # never retried
    assert outcome.metadata.batch_outcome == "failed"
    assert outcome.metadata.canonical_result == "direct"
    assert outcome.metadata.duplicate_provider_execution is True

    # Give any stray background task a moment, then confirm the late-batch
    # callback was never invoked — an unreadable *completed* batch is the
    # immediate resolution, not something observed asynchronously later.
    time.sleep(0.05)
    assert late_calls == 0


def test_a_submit_one_failure_is_still_raised_never_treated_as_a_fallback_trigger():
    direct_calls = 0

    class FailingSubmitAdapter(ProviderBatchAdapter):
        def eligibility(self, request):
            return ProviderBatchEligibility(eligible=True)

        def submit_one(self, request):
            raise RuntimeError("network error creating batch")

        def poll(self, handle):
            return BatchPollResult(status="pending")

        def read_result(self, handle):
            raise AssertionError("must not be called: no batch was ever created")

        def direct(self, request):
            nonlocal direct_calls
            direct_calls += 1
            return ProviderBatchResult(result={}, contained_tool_call_plan=False)

    with pytest.raises(RuntimeError, match="network error creating batch"):
        run_batch_first(FailingSubmitAdapter(), REQUEST, **base_policy())
    assert direct_calls == 0


def test_rejects_a_streaming_request_before_any_provider_call():
    adapter = FakeAdapter()
    with pytest.raises(BatchFirstIneligibleError):
        run_batch_first(adapter, {**REQUEST, "stream": True}, **base_policy())
    assert adapter.direct_call_count == 0


def test_rejects_a_request_with_tools_unless_allow_duplicate_tool_call_plans_is_true():
    adapter = FakeAdapter()
    with_tools = {**REQUEST, "tools": [{"type": "function", "function": {"name": "lookup"}}]}

    with pytest.raises(BatchFirstIneligibleError):
        run_batch_first(adapter, with_tools, **base_policy())

    adapter2 = FakeAdapter(batch_completes_after_seconds=0.4)
    outcome = run_batch_first(
        adapter2,
        with_tools,
        **base_policy(deadline_seconds=0.03, allow_duplicate_tool_call_plans=True),
    )
    assert outcome.result == adapter2.direct_result


def test_rejects_when_accept_duplicate_provider_execution_is_not_exactly_true():
    adapter = FakeAdapter()
    with pytest.raises(BatchFirstIneligibleError):
        run_batch_first(adapter, REQUEST, deadline_seconds=0.2, accept_duplicate_provider_execution=False)
    with pytest.raises(BatchFirstIneligibleError):
        run_batch_first(adapter, REQUEST, deadline_seconds=0.2, accept_duplicate_provider_execution=None)


def test_rejects_a_non_positive_deadline():
    adapter = FakeAdapter()
    with pytest.raises(BatchFirstIneligibleError):
        run_batch_first(adapter, REQUEST, deadline_seconds=0, accept_duplicate_provider_execution=True)
    with pytest.raises(BatchFirstIneligibleError):
        run_batch_first(adapter, REQUEST, deadline_seconds=-1, accept_duplicate_provider_execution=True)


def test_rejects_when_the_adapter_itself_reports_the_request_ineligible():
    adapter = FakeAdapter(eligible=False)
    with pytest.raises(BatchFirstIneligibleError):
        run_batch_first(adapter, REQUEST, **base_policy())


def test_background_polling_stops_calling_poll_once_a_batch_result_is_returned():
    adapter = FakeAdapter(batch_completes_after_seconds=0.02)

    run_batch_first(adapter, REQUEST, **base_policy(deadline_seconds=0.3, poll_interval_seconds=0.01))
    count_at_return = adapter.poll_count

    time.sleep(0.1)
    assert adapter.poll_count == count_at_return, "polling must stop once the canonical batch result is returned"


def test_an_injected_fake_clock_drives_the_deadline_deterministically_with_no_real_waiting():
    # Proves run_batch_first() only ever waits through the injected clock: a
    # huge deadline_seconds is passed through untouched, but the fake
    # resolves its own wait() call instantly instead of really blocking —
    # matching this seam's purpose exactly (deterministic control, no real
    # waiting), just shaped around threading.Event.wait rather than
    # setTimeout/clearTimeout.
    waits = []

    class InstantDeadlineClock(BatchFirstClock):
        def monotonic(self):
            return 0.0

        def wait(self, event, timeout):
            waits.append(timeout)
            if timeout == 999_999:
                return False  # simulate the deadline firing immediately
            # Any other wait (the poll loop's own interval sleep) really
            # blocks — nothing in this test ever sets `stop_polling`, so
            # this mirrors the TS fake clock's equivalent test, where the
            # poll interval's timer is scheduled but deliberately never
            # fired. A real, indefinite Event.wait costs no CPU (unlike a
            # 0-timeout poll loop) and the background thread is a daemon,
            # so it never blocks process/test exit.
            return event.wait(timeout)

    resolved = threading.Event()
    direct_called = threading.Event()

    class BlockedAdapter(ProviderBatchAdapter):
        def eligibility(self, request):
            return ProviderBatchEligibility(eligible=True)

        def submit_one(self, request):
            return BatchHandle(provider_batch_id="b1")

        def poll(self, handle):
            return BatchPollResult(status="pending")  # never completes on its own

        def read_result(self, handle):
            raise AssertionError("must not be called: batch never completed")

        def direct(self, request):
            direct_called.set()
            resolved.wait()
            return ProviderBatchResult(result={"via": "direct"}, contained_tool_call_plan=False)

    started_at = time.monotonic()
    result_box = {}

    def run():
        result_box["outcome"] = run_batch_first(
            BlockedAdapter(),
            REQUEST,
            deadline_seconds=999_999,
            accept_duplicate_provider_execution=True,
            poll_interval_seconds=500_000,
            clock=InstantDeadlineClock(),
        )

    thread = threading.Thread(target=run)
    thread.start()
    assert direct_called.wait(timeout=1.0), "direct() must be reached without waiting out the real deadline"
    assert time.monotonic() - started_at < 1.0
    resolved.set()
    thread.join(timeout=1.0)

    assert result_box["outcome"].source == "direct"
    assert 999_999 in waits


# ---------- OpenAI/Anthropic/Google end-to-end via batch_first() ----------


class _FakeOpenAIFiles:
    def __init__(self, outer):
        self._outer = outer

    def create(self, *, file, purpose):
        _name, content, _content_type = file
        self._outer.uploaded_custom_id = json.loads(content.decode("utf-8").strip())["custom_id"]
        return {"id": "file_1"}

    def content(self, file_id):
        body = json.dumps(
            {
                "custom_id": self._outer.uploaded_custom_id,
                "response": {"status_code": 200, "body": {"id": "resp_1", "output": []}},
            }
        )
        return type("Content", (), {"text": f"{body}\n"})()


class _FakeOpenAIBatches:
    def create(self, **params):
        return {"id": "batch_1", "status": "validating"}

    def retrieve(self, batch_id):
        return {"id": "batch_1", "status": "completed", "output_file_id": "file_out_1"}


class _FakeOpenAIResponses:
    def create(self, **request):
        raise AssertionError("responses.create must not be called when the batch completes in time")


class _FakeOpenAIClient:
    def __init__(self):
        self.uploaded_custom_id = None
        self.files = _FakeOpenAIFiles(self)
        self.batches = _FakeOpenAIBatches()
        self.responses = _FakeOpenAIResponses()


def test_batch_first_resolves_the_openai_adapter_from_a_duck_typed_client():
    outcome = batch_first(
        _FakeOpenAIClient(), "openai", REQUEST, deadline_seconds=2.0, accept_duplicate_provider_execution=True
    )
    assert outcome.source == "batch"
    assert outcome.result["id"] == "resp_1"


class _FakeAnthropicMessages:
    def __init__(self, batches):
        self.batches = batches

    def create(self, **request):
        raise AssertionError("direct must not be called when the batch completes in time")


class _FakeAnthropicBatches:
    def __init__(self, *, processing_status="ended", request_counts=None, result_item=None):
        self.processing_status = processing_status
        self.request_counts = request_counts or {"succeeded": 1}
        self._submitted_custom_id = None
        self._result_item = result_item

    def create(self, **params):
        self._submitted_custom_id = params["requests"][0]["custom_id"]
        return {"id": "batch_1", "processing_status": "in_progress"}

    def retrieve(self, batch_id):
        return {"id": "batch_1", "processing_status": self.processing_status, "request_counts": self.request_counts}

    def results(self, batch_id):
        if self._result_item == "omit":
            return iter(())
        item = self._result_item or {
            "custom_id": self._submitted_custom_id,
            "result": {"type": "succeeded", "message": {"id": "msg_1", "role": "assistant", "content": []}},
        }
        if callable(item):
            item = item(self._submitted_custom_id)
        return iter([item])


def test_batch_first_resolves_the_anthropic_adapter_from_a_duck_typed_client():
    batches = _FakeAnthropicBatches()
    client = type("Client", (), {"messages": _FakeAnthropicMessages(batches)})()

    outcome = batch_first(
        client, "anthropic", REQUEST, deadline_seconds=2.0, accept_duplicate_provider_execution=True
    )
    assert outcome.source == "batch"
    assert outcome.result["id"] == "msg_1"


def test_anthropic_item_level_batch_error_falls_back_to_direct_once_never_raises():
    direct_calls = 0

    def failing_create(**request):
        nonlocal direct_calls
        direct_calls += 1
        return {"id": "msg_direct_1", "role": "assistant", "content": []}

    result_item = lambda custom_id: {
        "custom_id": custom_id,
        "result": {"type": "errored", "error": {"type": "invalid_request", "message": "leak-me-not"}},
    }
    batches = _FakeAnthropicBatches(request_counts={"errored": 1}, result_item=result_item)
    messages = _FakeAnthropicMessages(batches)
    messages.create = failing_create
    client = type("Client", (), {"messages": messages})()

    outcome = batch_first(
        client, "anthropic", REQUEST, deadline_seconds=2.0, accept_duplicate_provider_execution=True
    )
    assert outcome.source == "direct"
    assert direct_calls == 1
    assert outcome.metadata.batch_outcome == "failed"


def test_anthropic_missing_custom_id_in_results_falls_back_to_direct_once_never_raises():
    direct_calls = 0

    def create(**request):
        nonlocal direct_calls
        direct_calls += 1
        return {"id": "msg_direct_1", "role": "assistant", "content": []}

    batches = _FakeAnthropicBatches(result_item="omit")
    messages = _FakeAnthropicMessages(batches)
    messages.create = create
    client = type("Client", (), {"messages": messages})()

    outcome = batch_first(
        client, "anthropic", REQUEST, deadline_seconds=2.0, accept_duplicate_provider_execution=True
    )
    assert outcome.source == "direct"
    assert direct_calls == 1
    assert outcome.metadata.batch_outcome == "failed"


GOOGLE_REQUEST = {"model": "gemini-2.5-flash", "contents": [{"role": "user", "parts": [{"text": "hello"}]}]}


class _FakeGoogleModels:
    def generate_content(self, **request):
        raise AssertionError("direct must not be called when the batch completes in time")


class _FakeGoogleBatches:
    def __init__(self, *, state="JOB_STATE_SUCCEEDED", inlined_responses=None, omit_dest=False):
        self.state = state
        self.inlined_responses = inlined_responses if inlined_responses is not None else [{"response": {"candidates": []}}]
        self.omit_dest = omit_dest

    def create(self, **params):
        return {"name": "batches/batch_1", "state": "JOB_STATE_PENDING"}

    def get(self, *, name):
        job = {"name": name, "state": self.state}
        if not self.omit_dest:
            job["dest"] = {"inlined_responses": self.inlined_responses}
        return job


def test_batch_first_resolves_the_google_adapter_from_a_duck_typed_client():
    client = type("Client", (), {"models": _FakeGoogleModels(), "batches": _FakeGoogleBatches()})()

    outcome = batch_first(
        client, "google", GOOGLE_REQUEST, deadline_seconds=2.0, accept_duplicate_provider_execution=True
    )
    assert outcome.source == "batch"
    assert outcome.result == {"candidates": []}


def test_google_item_level_batch_error_falls_back_to_direct_once_never_raises():
    direct_calls = 0

    class Models:
        def generate_content(self, **request):
            nonlocal direct_calls
            direct_calls += 1
            return {"candidates": []}

    batches = _FakeGoogleBatches(inlined_responses=[{"error": {"code": 400, "message": "leak-me-not"}}])
    client = type("Client", (), {"models": Models(), "batches": batches})()

    outcome = batch_first(
        client, "google", GOOGLE_REQUEST, deadline_seconds=2.0, accept_duplicate_provider_execution=True
    )
    assert outcome.source == "direct"
    assert direct_calls == 1
    assert outcome.metadata.batch_outcome == "failed"


def test_google_no_inlined_responses_falls_back_to_direct_once_never_raises():
    direct_calls = 0

    class Models:
        def generate_content(self, **request):
            nonlocal direct_calls
            direct_calls += 1
            return {"candidates": []}

    batches = _FakeGoogleBatches(omit_dest=True)
    client = type("Client", (), {"models": Models(), "batches": batches})()

    outcome = batch_first(
        client, "google", GOOGLE_REQUEST, deadline_seconds=2.0, accept_duplicate_provider_execution=True
    )
    assert outcome.source == "direct"
    assert direct_calls == 1
    assert outcome.metadata.batch_outcome == "failed"


def test_public_api_exports():
    # run_batch_first and the adapter factories are internal-module imports
    # only — see test_public_api_surface.py for the full root-export
    # contract.
    assert callable(metergraph.batch_first)
    assert metergraph.BatchFirstIneligibleError is BatchFirstIneligibleError
