"""Fail-open contracts for the *streaming* seam (``SyncStream``/``AsyncStream``).

The non-streaming promise characterized in ``test_patch_fail_open.py`` —
transparency under telemetry failure — applies just as strictly once MeterGraph
wraps a provider stream. If MeterGraph's own per-chunk bookkeeping or its
end-of-stream ``finish`` throws, the application must still observe exactly what
an unwrapped stream would: every provider chunk, normal exhaustion, the exact
provider iteration exception, and provider close — with MeterGraph's internal
exception never escaping.

These tests reuse the shared streaming protocol double (``fixtures.protocol``)
and the *real* ``SyncStream``/``AsyncStream``, then inject a telemetry fault at
each boundary the wrappers cross:

1. ``_StreamState.chunk`` raises after the provider yields a valid chunk;
2. ``_StreamState.finish`` / ``finish_async`` raises at normal exhaustion;
3. ``finish`` / ``finish_async`` raises while recording a provider iteration
   exception;
4. ``finish`` / ``finish_async`` raises during ``close`` / ``aclose``.

Every current call site invokes these unguarded, so each contract is a minimal
``xfail(strict=True)`` naming the exact production gap and the call-site guard a
later PR must land — without touching production here. A strict xfail flips to a
failure the moment the guard lands, forcing the marker's removal.
"""

from __future__ import annotations

import asyncio

import pytest

from metergraph import _capture
from metergraph._capture import AsyncStream, Options, Runtime, SyncStream

from fixtures.protocol import make_scenario


# --- Test-only doubles -------------------------------------------------------


class TelemetryError(RuntimeError):
    """A fault injected into MeterGraph's own stream bookkeeping. It must never
    be the exception an application observes."""


def _raise_telemetry(*_args, **_kwargs):
    raise TelemetryError("injected telemetry failure")


class _Rows:
    def __init__(self) -> None:
        self.rows: list[dict] = []

    def enqueue(self, row: dict) -> bool:
        self.rows.append(row)
        return True


def _sync_wrapped(scenario):
    """Wrap a fresh sync manager double in the real ``SyncStream``; return the
    wrapper, the underlying provider (for its shared probe), and the row sink
    (so every case can assert capture-at-most-once)."""
    rows = _Rows()
    provider = scenario.sync_factory()
    call = Runtime(rows, Options(capture_text=True, app_root="")).call_state(
        "anthropic", "messages.stream", {"model": "m"}
    )
    return SyncStream(provider, call), provider, rows


def _async_wrapped(scenario):
    rows = _Rows()
    provider = scenario.async_factory()
    call = Runtime(rows, Options(capture_text=True, app_root="")).call_state(
        "anthropic", "messages.stream", {"model": "m"}
    )
    return AsyncStream(provider, call), provider, rows


def _assert_capture_at_most_once(rows: _Rows) -> None:
    """However telemetry fails, MeterGraph must enqueue at most one row for the
    stream. Losing telemetry is acceptable when capture itself fails; emitting
    duplicate telemetry is not. (``CallState.finish`` is idempotent, so the
    risk is a second finalization site running its own enqueue.)"""
    assert len(rows.rows) <= 1


# --- 1. chunk raises after a valid provider chunk ----------------------------


@pytest.mark.xfail(
    strict=True,
    reason=(
        "GAP: SyncStream.__next__ calls _state.chunk(next(...)) unguarded; a "
        "telemetry failure there is caught by the except branch, finishes, and "
        "re-raises, so it escapes as the iteration exception and drops the "
        "provider chunk. Fix (later PR): swallow per-chunk recording errors and "
        "return the provider value, so a failed chunk() never diverts iteration "
        "into the error-finalization path."
    ),
)
def test_sync_chunk_failure_preserves_provider_chunk(monkeypatch):
    monkeypatch.setattr(_capture._StreamState, "chunk", _raise_telemetry)
    scenario = make_scenario()
    stream, _provider, rows = _sync_wrapped(scenario)

    yielded = []
    with stream as s:
        for chunk in s:
            yielded.append(chunk)

    assert yielded == scenario.chunks
    _assert_capture_at_most_once(rows)


@pytest.mark.xfail(
    strict=True,
    reason=(
        "GAP: AsyncStream.__anext__ calls _state.chunk(await ...) unguarded "
        "identically, so a telemetry failure escapes as the iteration exception "
        "and drops the provider chunk. Same fix as the sync case."
    ),
)
def test_async_chunk_failure_preserves_provider_chunk(monkeypatch):
    monkeypatch.setattr(_capture._StreamState, "chunk", _raise_telemetry)
    scenario = make_scenario()
    stream, _provider, rows = _async_wrapped(scenario)

    async def drive():
        yielded = []
        async with stream as s:
            async for chunk in s:
                yielded.append(chunk)
        return yielded

    assert asyncio.run(drive()) == scenario.chunks
    _assert_capture_at_most_once(rows)


# --- 2. finish raises at normal exhaustion -----------------------------------


@pytest.mark.xfail(
    strict=True,
    reason=(
        "GAP: __next__'s StopIteration branch calls _state.finish() unguarded "
        "before re-raising, so a failure replaces StopIteration and escapes. A "
        "local guard at only this site is insufficient: a swallowed finish "
        "leaves call.done False, so __exit__ finalizes the same state again and "
        "can re-raise. Fix (later PR): route every finalization (the "
        "StopIteration/except branches, close, and __exit__) through one shared "
        "fail-open boundary that swallows telemetry errors, so normal "
        "completion is preserved wherever finish runs."
    ),
)
def test_sync_finish_failure_preserves_normal_completion(monkeypatch):
    monkeypatch.setattr(_capture._StreamState, "finish", _raise_telemetry)
    scenario = make_scenario()
    stream, _provider, rows = _sync_wrapped(scenario)

    yielded = []
    with stream as s:
        for chunk in s:
            yielded.append(chunk)

    assert yielded == scenario.chunks
    _assert_capture_at_most_once(rows)


@pytest.mark.xfail(
    strict=True,
    reason=(
        "GAP: __anext__'s StopAsyncIteration branch awaits finish_async() "
        "unguarded before re-raising, so a failure replaces StopAsyncIteration "
        "and escapes. As in the sync case, guarding only this site is "
        "insufficient — the swallowed finish leaves call.done False and "
        "__aexit__ finalizes again. Fix (later PR): route every async "
        "finalization (StopAsyncIteration/except branches, aclose, and "
        "__aexit__) through one shared fail-open boundary."
    ),
)
def test_async_finish_failure_preserves_normal_completion(monkeypatch):
    monkeypatch.setattr(_capture._StreamState, "finish_async", _raise_telemetry)
    scenario = make_scenario()
    stream, _provider, rows = _async_wrapped(scenario)

    async def drive():
        yielded = []
        async with stream as s:
            async for chunk in s:
                yielded.append(chunk)
        return yielded

    assert asyncio.run(drive()) == scenario.chunks
    _assert_capture_at_most_once(rows)


# --- 3. finish raises while recording a provider iteration exception ---------


@pytest.mark.xfail(
    strict=True,
    reason=(
        "GAP: __next__'s except branch runs _state.finish(error=exc) unguarded "
        "before re-raising, so a failure propagates instead and replaces the "
        "exact provider exception. Guarding only this site is insufficient: the "
        "swallowed finish leaves call.done False, so __exit__ (which receives "
        "the same exception) finalizes again and can still escape. Fix (later "
        "PR): funnel every finalization call site through one shared fail-open "
        "boundary so the original provider exception is always re-raised."
    ),
)
def test_sync_finish_failure_preserves_iteration_exception(monkeypatch):
    monkeypatch.setattr(_capture._StreamState, "finish", _raise_telemetry)
    iter_error = LookupError("stream broke")
    scenario = make_scenario(iter_error=iter_error, raise_after=0)
    stream, _provider, rows = _sync_wrapped(scenario)

    with pytest.raises(LookupError) as excinfo:
        with stream as s:
            for _chunk in s:
                pass

    assert excinfo.value is iter_error
    _assert_capture_at_most_once(rows)


@pytest.mark.xfail(
    strict=True,
    reason=(
        "GAP: __anext__'s except branch awaits finish_async(error=exc) "
        "unguarded before re-raising, so a failure replaces the exact provider "
        "exception reaching the awaiter. As in the sync case, guarding only "
        "this site is insufficient — the swallowed finish leaves call.done "
        "False and __aexit__ finalizes again. Fix (later PR): funnel every "
        "async finalization call site through one shared fail-open boundary."
    ),
)
def test_async_finish_failure_preserves_iteration_exception(monkeypatch):
    monkeypatch.setattr(_capture._StreamState, "finish_async", _raise_telemetry)
    iter_error = LookupError("stream broke")
    scenario = make_scenario(iter_error=iter_error, raise_after=0)
    stream, _provider, rows = _async_wrapped(scenario)

    async def drive():
        async with stream as s:
            async for _chunk in s:
                pass

    with pytest.raises(LookupError) as excinfo:
        asyncio.run(drive())

    assert excinfo.value is iter_error
    _assert_capture_at_most_once(rows)


# --- 4. finish raises during close / aclose ----------------------------------


@pytest.mark.xfail(
    strict=True,
    reason=(
        "GAP: SyncStream.close() calls _state.finish(status='abandoned') "
        "unguarded after the provider close(), so a failure escapes close(). A "
        "local guard in close() alone still leaves exit broken: the swallowed "
        "finish leaves call.done False, so the enclosing __exit__ finalizes "
        "again and can re-raise. Fix (later PR): route close() and __exit__ "
        "(and the iteration finalizers) through one shared fail-open boundary "
        "so close stays transparent no matter where finish runs."
    ),
)
def test_sync_finish_failure_during_close_preserves_close(monkeypatch):
    monkeypatch.setattr(_capture._StreamState, "finish", _raise_telemetry)
    scenario = make_scenario()
    stream, provider, rows = _sync_wrapped(scenario)

    with stream as s:
        s.close()

    assert provider.probe.closed == 1
    _assert_capture_at_most_once(rows)


@pytest.mark.xfail(
    strict=True,
    reason=(
        "GAP: AsyncStream.aclose() awaits finish_async(status='abandoned') "
        "unguarded after the provider aclose(), so a failure escapes aclose(). "
        "As in the sync case, a local guard in aclose() alone leaves __aexit__ "
        "broken — the swallowed finish leaves call.done False and __aexit__ "
        "finalizes again. Fix (later PR): route aclose() and __aexit__ (and the "
        "iteration finalizers) through one shared fail-open boundary."
    ),
)
def test_async_finish_failure_during_close_preserves_close(monkeypatch):
    monkeypatch.setattr(_capture._StreamState, "finish_async", _raise_telemetry)
    scenario = make_scenario()
    stream, provider, rows = _async_wrapped(scenario)

    async def drive():
        async with stream as s:
            await s.aclose()

    asyncio.run(drive())

    assert provider.probe.closed == 1
    _assert_capture_at_most_once(rows)
