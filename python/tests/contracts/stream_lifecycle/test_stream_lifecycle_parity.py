"""Stream lifecycle parity: a streaming context manager whose entry returns a
distinct entered stream must behave identically wrapped and unwrapped, across
the full lifecycle — successful iteration, entry failure, mid-stream failure,
caller errors, exit-time suppression, and explicit close/exit.

The harness (``fixtures/parity.py``) compares only application-visible
observations and folds provider-visible exit/close facts (read through the
shared probe) into that comparison, so *parity is the authority*. These tests
add absolute per-case expectations on top.

The ownership split this protects lives in ``_StreamState.use_entered_stream``:
the **manager** owns entry/exit; the **entered stream** owns iteration and
helpers. The historical regression is a wrapper that iterates/delegates to the
*manager* instead of the entered stream, which silently breaks any provider (or
middleware such as Datadog's Anthropic integration) that hands back a distinct
object on entry.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Callable

import pytest

from fixtures import parity
from fixtures.parity import ParityResult, async_stream_parity, sync_stream_parity
from fixtures.protocol import StreamScenario, make_scenario


@dataclass(frozen=True)
class Expect:
    yielded_len: int
    rows: int
    exit_count: int
    close_count: int
    suppressed: bool
    error_source: str | None  # "enter" | "iter" | "caller" | None


@dataclass(frozen=True)
class Case:
    name: str
    kind: str  # "run" | "close1" | "close2" | "reexit"
    scenario: Callable[[], StreamScenario]
    caller: bool
    expect: Expect


CASES = [
    Case(
        "success",
        "run",
        lambda: make_scenario(),
        False,
        Expect(yielded_len=2, rows=1, exit_count=1, close_count=0,
               suppressed=False, error_source=None),
    ),
    Case(
        "entry_error",
        "run",
        lambda: make_scenario(enter_error=ConnectionError("provider unavailable")),
        False,
        Expect(yielded_len=0, rows=1, exit_count=0, close_count=0,
               suppressed=False, error_source="enter"),
    ),
    Case(
        "iteration_error",
        "run",
        lambda: make_scenario(iter_error=LookupError("stream broke"), raise_after=0),
        False,
        Expect(yielded_len=1, rows=1, exit_count=1, close_count=0,
               suppressed=False, error_source="iter"),
    ),
    Case(
        "caller_error",
        "run",
        lambda: make_scenario(),
        True,
        Expect(yielded_len=2, rows=1, exit_count=1, close_count=0,
               suppressed=False, error_source="caller"),
    ),
    Case(
        "suppression",
        "run",
        lambda: make_scenario(suppress=True),
        True,
        Expect(yielded_len=2, rows=1, exit_count=1, close_count=0,
               suppressed=True, error_source=None),
    ),
    Case(
        "early_close",
        "close1",
        lambda: make_scenario(),
        False,
        Expect(yielded_len=0, rows=1, exit_count=1, close_count=1,
               suppressed=False, error_source=None),
    ),
    Case(
        "repeated_close",
        "close2",
        lambda: make_scenario(),
        False,
        Expect(yielded_len=0, rows=1, exit_count=1, close_count=2,
               suppressed=False, error_source=None),
    ),
    Case(
        "repeated_exit",
        "reexit",
        lambda: make_scenario(),
        False,
        Expect(yielded_len=0, rows=1, exit_count=2, close_count=0,
               suppressed=False, error_source=None),
    ),
]


def _sync_driver(kind: str, caller_error: BaseException | None):
    return {
        "run": lambda: parity.sync_run_driver(caller_error),
        "close1": lambda: parity.sync_close_driver(1),
        "close2": lambda: parity.sync_close_driver(2),
        "reexit": lambda: parity.sync_reexit_driver(),
    }[kind]()


def _async_driver(kind: str, caller_error: BaseException | None):
    return {
        "run": lambda: parity.async_run_driver(caller_error),
        "close1": lambda: parity.async_close_driver(1),
        "close2": lambda: parity.async_close_driver(2),
        "reexit": lambda: parity.async_reexit_driver(),
    }[kind]()


def _assert_case(
    case: Case,
    result: ParityResult,
    scenario: StreamScenario,
    caller_error: BaseException | None,
) -> None:
    obs = result.observation

    # MeterGraph captured the stream exactly once whenever it was entered.
    assert len(result.rows) == case.expect.rows
    assert all(row["stream"] is True for row in result.rows)

    # Application-visible facts (also enforced wrapped-vs-unwrapped by parity).
    assert len(obs.yielded) == case.expect.yielded_len
    assert obs.exit_count == case.expect.exit_count
    assert obs.close_count == case.expect.close_count
    assert obs.suppressed is case.expect.suppressed

    expected_error = {
        "enter": scenario.enter_error,
        "iter": scenario.iter_error,
        "caller": caller_error,
        None: None,
    }[case.expect.error_source]

    # Exact exception object, type, and message reach the application.
    assert obs.error is expected_error
    if expected_error is not None:
        assert obs.error_type == type(expected_error).__name__
        assert obs.error_message == str(expected_error)

    if case.expect.error_source == "enter":
        # Entry failed: the manager's exit must not be spuriously called.
        assert obs.exit_count == 0
        assert obs.exit_excs == ()
    if case.expect.error_source in {"iter", "caller"}:
        # Exit received that same exception object.
        assert obs.exit_excs[-1] is expected_error
    if case.expect.suppressed:
        # Suppressed: nothing reached the caller, but exit saw the exception.
        assert obs.error is None
        assert any(exc is caller_error for exc in obs.exit_excs)


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.name)
def test_sync_stream_lifecycle_parity(case: Case):
    scenario = case.scenario()
    caller_error = RuntimeError("caller boom") if case.caller else None
    driver = _sync_driver(case.kind, caller_error)

    result = sync_stream_parity(scenario.sync_factory, driver)
    _assert_case(case, result, scenario, caller_error)

    if case.name == "success":
        # Ownership: manager served entry/exit; entered stream served
        # iteration, the helper, and the public attribute.
        probe = result.provider.probe
        assert probe.entered == 1
        assert probe.iterated == 1
        assert probe.helper_calls >= 1
        assert result.observation.helper is scenario.final
        assert result.observation.public_attr == "req-entered-stream"


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.name)
def test_async_stream_lifecycle_parity(case: Case):
    scenario = case.scenario()
    caller_error = RuntimeError("caller boom") if case.caller else None
    driver = _async_driver(case.kind, caller_error)

    result = asyncio.run(async_stream_parity(scenario.async_factory, driver))
    _assert_case(case, result, scenario, caller_error)

    if case.name == "success":
        probe = result.provider.probe
        assert probe.entered == 1
        assert probe.iterated == 1
        assert probe.helper_calls >= 1
        assert result.observation.helper is scenario.final
        assert result.observation.public_attr == "req-entered-stream"
