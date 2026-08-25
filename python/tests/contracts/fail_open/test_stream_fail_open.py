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
4. ``finish`` / ``finish_async`` raises during ``close`` / ``aclose``;
5. ``_usage_only_chunk`` classification raises after a valid provider chunk.

Each is a permanent contract the wrappers must honor: every finalization site
(the iteration finalizers, context exit, and close/aclose) and per-chunk
recording — both aggregation and usage-only classification — routes through a
shared fail-open boundary, so a swallowed telemetry failure may lose the trace
but never alters what the application observes.
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


def test_sync_finish_failure_during_close_preserves_close(monkeypatch):
    monkeypatch.setattr(_capture._StreamState, "finish", _raise_telemetry)
    scenario = make_scenario()
    stream, provider, rows = _sync_wrapped(scenario)

    with stream as s:
        s.close()

    assert provider.probe.closed == 1
    _assert_capture_at_most_once(rows)


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


# --- 5. usage-only classification raises after a valid provider chunk ---------


def test_sync_usage_only_classification_failure_preserves_provider_chunk(monkeypatch):
    # Classification runs on the already-obtained provider chunk; if it raises,
    # the chunk must still be yielded, not dropped or replaced by the telemetry
    # error. (``_usage_only_chunk`` calls generic ``_get`` on arbitrary/proxied
    # provider chunks and can raise on a pathological one.)
    monkeypatch.setattr(_capture, "_usage_only_chunk", _raise_telemetry)
    scenario = make_scenario()
    stream, _provider, rows = _sync_wrapped(scenario)

    yielded = []
    with stream as s:
        for chunk in s:
            yielded.append(chunk)

    assert yielded == scenario.chunks
    _assert_capture_at_most_once(rows)


def test_async_usage_only_classification_failure_preserves_provider_chunk(monkeypatch):
    monkeypatch.setattr(_capture, "_usage_only_chunk", _raise_telemetry)
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
