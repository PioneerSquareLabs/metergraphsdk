"""Fail-open contracts for the non-streaming ``_patch`` seam.

MeterGraph instruments a provider method by replacing it with ``_patch``'s
wrapper. The wrapper's promise is *transparency under telemetry failure*: if
MeterGraph's own bookkeeping (creating the call state, or finishing the record)
throws, the application must still observe exactly what an uninstrumented client
would — the provider is invoked exactly once whenever it otherwise would be, the
exact successful result or the exact provider exception reaches the caller, and
MeterGraph's internal exception never escapes.

These tests inject a fault at each telemetry boundary the wrapper crosses around
a *non-streaming* call:

1. ``Runtime.call_state`` raises, before the provider method would be invoked;
2. the returned ``CallState.finish`` raises, after the provider returns a result;
3. the returned ``CallState.finish`` raises, while recording an exact provider
   exception.

Each asserts the fail-open contract above as a permanent guarantee the seam
must honor: the provider is invoked exactly once whenever it otherwise would be,
the exact successful result or provider exception reaches the caller, and
MeterGraph's injected telemetry failure never escapes.

The writer-enqueue fail-open boundary is deliberately *not* re-covered here: it
is already characterized by
``test_edge_cases.test_capture_is_idempotent_and_fail_open_when_redaction_or_enqueue_fails``.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from metergraph import _capture
from metergraph._capture import Options, Runtime, set_runtime


# --- Test-only doubles -------------------------------------------------------


class TelemetryError(RuntimeError):
    """A fault injected into MeterGraph's own bookkeeping. It must never be the
    exception (or the substitute result) an application observes."""


class ProviderError(RuntimeError):
    """The provider's own failure. Its exact identity must reach the caller."""


PROVIDER_RESULT = SimpleNamespace(id="resp-1", model="m", output_text="hi")


class _Rows:
    def __init__(self) -> None:
        self.rows: list[dict] = []

    def enqueue(self, row: dict) -> bool:
        self.rows.append(row)
        return True


class SyncProvider:
    """A minimal instrumentable owner: one method that records its call count
    and either returns a fixed result or raises a fixed provider exception."""

    def __init__(self, *, error: BaseException | None = None) -> None:
        self.calls = 0
        self._error = error

    def create(self, **kwargs: Any) -> Any:
        self.calls += 1
        if self._error is not None:
            raise self._error
        return PROVIDER_RESULT


class AsyncProvider:
    def __init__(self, *, error: BaseException | None = None) -> None:
        self.calls = 0
        self._error = error

    async def create(self, **kwargs: Any) -> Any:
        self.calls += 1
        if self._error is not None:
            raise self._error
        return PROVIDER_RESULT


@pytest.fixture(autouse=True)
def _runtime_and_rows():
    """Install a live runtime for the duration of a test and always tear it
    down, keeping the module-global runtime clean for the next test."""
    rows = _Rows()
    set_runtime(Runtime(rows, Options(app_root="")))
    try:
        yield rows
    finally:
        set_runtime(None)


def _instrument(owner: Any) -> Any:
    """Patch ``owner.create`` exactly as ``wrap()`` would for a provider seam."""
    assert _capture._patch(owner, "create", "openai", "responses")
    return owner


def _raise_telemetry(*_args: Any, **_kwargs: Any):
    raise TelemetryError("injected telemetry failure")


# --- 1. call_state raises before the provider is invoked ---------------------


def test_sync_call_state_failure_still_invokes_provider(monkeypatch):
    monkeypatch.setattr(Runtime, "call_state", _raise_telemetry)
    owner = _instrument(SyncProvider())

    result = owner.create(model="m")

    assert owner.calls == 1
    assert result is PROVIDER_RESULT


def test_async_call_state_failure_still_invokes_provider(monkeypatch):
    monkeypatch.setattr(Runtime, "call_state", _raise_telemetry)
    owner = _instrument(AsyncProvider())

    result = asyncio.run(owner.create(model="m"))

    assert owner.calls == 1
    assert result is PROVIDER_RESULT


# --- 2. finish raises after a successful provider result ---------------------


def test_sync_finish_failure_preserves_successful_result(monkeypatch):
    monkeypatch.setattr(_capture.CallState, "finish", _raise_telemetry)
    owner = _instrument(SyncProvider())

    result = owner.create(model="m")

    assert owner.calls == 1
    assert result is PROVIDER_RESULT


def test_async_finish_failure_preserves_successful_result(monkeypatch):
    monkeypatch.setattr(_capture.CallState, "finish", _raise_telemetry)
    owner = _instrument(AsyncProvider())

    result = asyncio.run(owner.create(model="m"))

    assert owner.calls == 1
    assert result is PROVIDER_RESULT


# --- 3. finish raises while recording an exact provider exception ------------


def test_sync_finish_failure_preserves_provider_exception(monkeypatch):
    monkeypatch.setattr(_capture.CallState, "finish", _raise_telemetry)
    provider_error = ProviderError("provider down")
    owner = _instrument(SyncProvider(error=provider_error))

    with pytest.raises(ProviderError) as excinfo:
        owner.create(model="m")

    assert excinfo.value is provider_error
    assert owner.calls == 1


def test_async_finish_failure_preserves_provider_exception(monkeypatch):
    monkeypatch.setattr(_capture.CallState, "finish", _raise_telemetry)
    provider_error = ProviderError("provider down")
    owner = _instrument(AsyncProvider(error=provider_error))

    with pytest.raises(ProviderError) as excinfo:
        asyncio.run(owner.create(model="m"))

    assert excinfo.value is provider_error
    assert owner.calls == 1
