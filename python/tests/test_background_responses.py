"""Background-mode Responses: ``create(background=True)`` returns a queued
response, and output and usage arrive on a later ``retrieve`` or ``cancel``.

The call must be recorded once, from the response that first reports a
terminal status. A call whose final result is never observed is recorded as
``abandoned`` with unknown usage, never as a completed call with zero usage.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from types import SimpleNamespace

import metergraph
import pytest
from metergraph import _capture
from metergraph._capture import Options, Runtime

os.environ.pop("METERGRAPH_APP_TOKEN", None)
os.environ.pop("METERGRAPH_INGEST_URL", None)

USAGE = SimpleNamespace(
    input_tokens=120,
    output_tokens=45,
    input_tokens_details=SimpleNamespace(cached_tokens=20),
    output_tokens_details=SimpleNamespace(reasoning_tokens=5),
)


class Rows:
    def __init__(self):
        self.rows = []

    def enqueue(self, row):
        self.rows.append(row)
        return True


def response(status, response_id="resp_1", usage=None, background=True):
    return SimpleNamespace(
        id=response_id,
        object="response",
        model="gpt-test",
        status=status,
        background=background,
        usage=usage,
        output=[],
        incomplete_details=None,
    )


class Responses:
    """A fake Responses resource whose server-side status advances per call."""

    def __init__(self, statuses, response_id="resp_1"):
        self.statuses = list(statuses)
        self.response_id = response_id

    def _next(self):
        status = self.statuses.pop(0)
        terminal = status in {"completed", "failed", "cancelled", "incomplete"}
        return response(status, self.response_id, USAGE if terminal else None)

    def create(self, **kwargs):
        return self._next()

    def retrieve(self, response_id, **kwargs):
        assert response_id == self.response_id
        return self._next()

    def cancel(self, response_id, **kwargs):
        assert response_id == self.response_id
        return response("cancelled", self.response_id, USAGE)


@pytest.fixture
def rows():
    collected = Rows()
    _capture.set_runtime(Runtime(collected, Options(app_root=str(Path(__file__).parents[1]))))
    _capture.finish_background_calls()
    collected.rows.clear()
    yield collected
    _capture.finish_background_calls()
    _capture.set_runtime(None)


def wrapped(statuses, response_id="resp_1"):
    client = SimpleNamespace(responses=Responses(statuses, response_id))
    return metergraph.wrap(client, provider="openai")


def test_background_call_is_recorded_once_from_the_completed_retrieve(rows):
    client = wrapped(["queued", "in_progress", "completed"])

    queued = client.responses.create(model="gpt-test", input="hi", background=True)
    assert queued.status == "queued"
    assert rows.rows == []
    client.responses.retrieve("resp_1")
    assert rows.rows == []
    client.responses.retrieve("resp_1")

    [row] = rows.rows
    assert row["endpoint"] == "responses"
    assert row["status"] == "completed"
    assert row["status_code"] == "unset"
    assert row["input_tokens"] == 120
    assert row["output_tokens"] == 45
    assert row["request_id"] == "resp_1"
    assert '"background":true' in row["request_json"]


def test_retrieving_again_after_completion_adds_no_row(rows):
    client = wrapped(["queued", "completed", "completed"])
    client.responses.create(model="gpt-test", input="hi", background=True)
    client.responses.retrieve("resp_1")
    client.responses.retrieve("resp_1")
    assert len(rows.rows) == 1


def test_cancel_records_the_cancelled_terminal_response(rows):
    client = wrapped(["queued"])
    client.responses.create(model="gpt-test", input="hi", background=True)
    client.responses.cancel("resp_1")
    [row] = rows.rows
    assert row["status"] == "cancelled"
    assert row["input_tokens"] == 120


def test_failed_background_response_is_an_error(rows):
    client = wrapped(["queued", "failed"])
    client.responses.create(model="gpt-test", input="hi", background=True)
    client.responses.retrieve("resp_1")
    [row] = rows.rows
    assert row["status"] == "failed"
    assert row["status_code"] == "error"
    assert row["error"] is True


def test_unobserved_background_call_is_recorded_as_abandoned_not_zero(rows):
    client = wrapped(["queued"])
    client.responses.create(model="gpt-test", input="hi", background=True)
    assert rows.rows == []

    assert _capture.finish_background_calls() == 1

    [row] = rows.rows
    assert row["status"] == "abandoned"
    assert row["input_tokens"] is None
    assert row["output_tokens"] is None
    # The last status the client saw, so the row says why usage is unknown.
    assert row["finish_reason"] == "queued"
    assert row["status_code"] == "unset"


def test_shutdown_records_unobserved_background_calls(rows):
    client = wrapped(["in_progress"])
    client.responses.create(model="gpt-test", input="hi", background=True)
    runtime = _capture._get_runtime()
    metergraph.shutdown()
    _capture.set_runtime(runtime)
    [row] = rows.rows
    assert row["status"] == "abandoned"


def test_pending_calls_are_bounded(rows, monkeypatch):
    monkeypatch.setattr(_capture, "_BACKGROUND_MAX_PENDING", 2)
    for index in range(3):
        client = wrapped(["queued"], response_id=f"resp_{index}")
        client.responses.create(model="gpt-test", input="hi", background=True)
    [row] = rows.rows
    assert row["status"] == "abandoned"
    assert row["request_id"] == "resp_0"


def test_foreground_and_already_finished_background_calls_record_immediately(rows):
    foreground = wrapped(["completed"])
    foreground.responses.create(model="gpt-test", input="hi")
    fast = wrapped(["completed"], response_id="resp_2")
    fast.responses.create(model="gpt-test", input="hi", background=True)
    assert [row["status"] for row in rows.rows] == ["completed", "completed"]
    assert _capture.finish_background_calls() == 0


def test_retrieve_of_an_unknown_response_records_nothing(rows):
    client = wrapped(["completed"], response_id="resp_other")
    client.responses.retrieve("resp_other")
    assert rows.rows == []


def test_async_retrieve_records_the_completed_response(rows):
    class AsyncResponses(Responses):
        async def create(self, **kwargs):
            return self._next()

        async def retrieve(self, response_id, **kwargs):
            return self._next()

    client = metergraph.wrap(
        SimpleNamespace(responses=AsyncResponses(["queued", "completed"])), provider="openai"
    )

    async def run():
        await client.responses.create(model="gpt-test", input="hi", background=True)
        assert rows.rows == []
        await client.responses.retrieve("resp_1")

    asyncio.run(run())
    [row] = rows.rows
    assert row["status"] == "completed"
    assert row["output_tokens"] == 45


def test_fork_child_does_not_inherit_held_calls(rows):
    client = wrapped(["queued"])
    client.responses.create(model="gpt-test", input="hi", background=True)
    _capture._clear_background_after_fork()
    assert _capture.finish_background_calls() == 0
    assert rows.rows == []
