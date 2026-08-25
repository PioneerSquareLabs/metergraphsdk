"""Wrapper-order composition parity: MeterGraph stays transparent when stacked
with *other* transparent middleware, in either nesting direction.

The sibling ``test_stream_lifecycle_parity`` suite covers the full lifecycle for
the single shape ``MeterGraph(raw)``. The real-package ``ddtrace`` composition
test covers one concrete *MeterGraph-outer* stack. Neither exercises the two
wrapper-order signals that only appear once a *generic* transparent layer sits
on the opposite side of MeterGraph:

  * ``proxy(MeterGraph(raw))``          — MeterGraph is the *inner* layer and a
                                          transparent manager wraps it.
  * ``MeterGraph(proxy(proxy(raw)))``   — MeterGraph is the *outer* layer over a
                                          multi-hop transparent manager chain.

Both are driven on the success path only, sync and async (4 cases). Lifecycle
*outcomes* (entry/iteration/caller errors, suppression, early close) are already
owned by ``test_stream_lifecycle_parity`` and are intentionally not duplicated
here — this file asserts just the composition-order invariant: ordered chunk
values, the final helper result, a normal single exit, and exactly one
MeterGraph row.

``TransparentProxy`` is a generic sync+async context-manager shim that forwards
every operation verbatim, so any divergence is attributable to MeterGraph rather
than to the middleware.
"""

from __future__ import annotations

import asyncio

import pytest

from fixtures import parity
from fixtures.protocol import make_scenario

from metergraph._capture import AsyncStream, Options, Runtime, SyncStream


class _Rows:
    def __init__(self) -> None:
        self.rows: list[dict] = []

    def enqueue(self, row: dict) -> bool:
        self.rows.append(row)
        return True


class TransparentProxy:
    """Generic behavior-preserving proxy over a (sync or async) streaming
    context manager: every dunder forwards verbatim to the wrapped object."""

    def __init__(self, wrapped: object) -> None:
        self._wrapped = wrapped

    def __getattr__(self, name: str):
        return getattr(self._wrapped, name)

    def __enter__(self):
        return self._wrapped.__enter__()

    def __exit__(self, *exc):
        return self._wrapped.__exit__(*exc)

    async def __aenter__(self):
        return await self._wrapped.__aenter__()

    async def __aexit__(self, *exc):
        return await self._wrapped.__aexit__(*exc)

    def __iter__(self):
        return iter(self._wrapped)

    def __aiter__(self):
        return self._wrapped.__aiter__()


def _wrap_meter(mode: str, inner: object, runtime: Runtime):
    call = runtime.call_state("anthropic", "messages.stream", {"model": "m"})
    return AsyncStream(inner, call) if mode == "async" else SyncStream(inner, call)


def _compose(mode: str, shape: str, raw: object, runtime: Runtime):
    if shape == "proxy(MeterGraph(raw))":
        return TransparentProxy(_wrap_meter(mode, raw, runtime))
    if shape == "MeterGraph(proxy(proxy(raw)))":
        return _wrap_meter(mode, TransparentProxy(TransparentProxy(raw)), runtime)
    raise AssertionError(f"unhandled shape {shape!r}")


SHAPES = ("proxy(MeterGraph(raw))", "MeterGraph(proxy(proxy(raw)))")
MODES = ("sync", "async")


@pytest.mark.parametrize("shape", SHAPES)
@pytest.mark.parametrize("mode", MODES)
def test_composition_order_is_transparent(mode: str, shape: str) -> None:
    scenario = make_scenario()  # clean two-chunk success stream
    factory = scenario.async_factory if mode == "async" else scenario.sync_factory
    driver = parity.async_run_driver() if mode == "async" else parity.sync_run_driver()

    runtime = Runtime(_Rows(), Options(capture_text=True, app_root=""))
    entry = _compose(mode, shape, factory(), runtime)
    observed = asyncio.run(driver(entry)) if mode == "async" else driver(entry)

    # Ordered chunk values reach the application unchanged and in order.
    assert [chunk.delta.text for chunk in observed.yielded] == ["he", "llo"]
    # The final helper result is the manager's own final message.
    assert observed.helper is scenario.final
    assert observed.helper.content[0].text == "hello"
    # Iteration/helpers came from the distinct entered stream, not the manager.
    assert observed.public_attr == "req-entered-stream"

    # Normal, single context exit; no error and no early close.
    assert observed.error is None
    assert observed.suppressed is False
    assert observed.exit_count == 1
    assert observed.close_count == 0

    # MeterGraph captured the stream exactly once.
    rows = runtime.writer.rows
    assert len(rows) == 1
    assert rows[0]["stream"] is True
    assert rows[0]["provider"] == "anthropic"
    assert rows[0]["endpoint"] == "messages.stream"
    assert rows[0]["error"] is False
