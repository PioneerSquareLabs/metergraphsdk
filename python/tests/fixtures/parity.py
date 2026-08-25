"""A minimal behavioral-parity harness for stream lifecycle contracts.

The contract MeterGraph must uphold is *transparency*: an application that
drives a provider stream must observe the same values, ordering, helper
results, exceptions, and control flow whether or not the stream is wrapped by
MeterGraph.

The harness runs one scenario twice — once against the raw protocol double and
once against the MeterGraph wrapper — and compares only **application-visible
observations** (:class:`Observation`), never MeterGraph internals. Provider-
visible facts (which exception each exit received, how many times close/exit
ran) are read back through the shared probe and folded into the Observation so
that *parity itself* is the authority — the harness enforces behavior rather
than merely asserting on instrumentation. The captured rows are returned
separately so a test can additionally assert lifecycle facts such as
capture-at-most-once.

Both the raw manager (unwrapped run) and the MeterGraph wrapper (wrapped run)
expose ``.probe``: the manager directly, and the wrapper via attribute
delegation to the entered stream, which shares the manager's probe. A driver
therefore reads ``entry.probe`` uniformly in both runs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from metergraph._capture import AsyncStream, Options, Runtime, SyncStream


class _Rows:
    def __init__(self) -> None:
        self.rows: list[dict] = []

    def enqueue(self, row: dict) -> bool:
        self.rows.append(row)
        return True


def _runtime() -> tuple[Runtime, _Rows]:
    rows = _Rows()
    return Runtime(rows, Options(capture_text=True, app_root="")), rows


@dataclass(frozen=True)
class Observation:
    """A comparable snapshot of what an application sees while driving a
    stream. Fields default so a driver only fills what it exercises."""

    yielded: tuple = ()
    helper: Any = None
    public_attr: Any = None
    # Exception that propagated to the application (exact object, so identity
    # is compared), plus its type/message.
    error: Any = None
    error_type: str | None = None
    error_message: str | None = None
    # True when a caller-raised exception was swallowed by the context manager.
    suppressed: bool = False
    # Provider-visible exit/close facts, read back through the shared probe.
    exit_count: int = 0
    exit_excs: tuple = ()
    close_count: int = 0


@dataclass
class ParityResult:
    observation: Observation
    rows: list[dict] = field(default_factory=list)
    provider: Any = None


def _observe(
    entry: Any,
    *,
    yielded: list[Any],
    helper: Any = None,
    public_attr: Any = None,
    error: BaseException | None = None,
    caller_error: BaseException | None = None,
) -> Observation:
    probe = entry.probe
    return Observation(
        yielded=tuple(yielded),
        helper=helper,
        public_attr=public_attr,
        error=error,
        error_type=type(error).__name__ if error is not None else None,
        error_message=str(error) if error is not None else None,
        suppressed=bool(caller_error is not None and error is None),
        exit_count=probe.exited,
        exit_excs=tuple(probe.exit_excs),
        close_count=probe.closed,
    )


def sync_stream_parity(
    make_provider: Callable[[], Any],
    drive: Callable[[Any], Observation],
    *,
    endpoint: str = "messages.stream",
) -> ParityResult:
    """Drive a fresh provider double both unwrapped and MeterGraph-wrapped;
    assert the application sees identical observations."""
    unwrapped = drive(make_provider())

    runtime, rows = _runtime()
    provider = make_provider()
    wrapped_entry = SyncStream(
        provider, runtime.call_state("anthropic", endpoint, {"model": "m"})
    )
    wrapped = drive(wrapped_entry)

    assert wrapped == unwrapped, (
        f"MeterGraph changed application-visible behavior:\n"
        f"  unwrapped={unwrapped!r}\n  wrapped=  {wrapped!r}"
    )
    return ParityResult(observation=wrapped, rows=rows.rows, provider=provider)


async def async_stream_parity(
    make_provider: Callable[[], Any],
    drive: Callable[[Any], Awaitable[Observation]],
    *,
    endpoint: str = "messages.stream",
) -> ParityResult:
    """Async counterpart to :func:`sync_stream_parity`."""
    unwrapped = await drive(make_provider())

    runtime, rows = _runtime()
    provider = make_provider()
    wrapped_entry = AsyncStream(
        provider, runtime.call_state("anthropic", endpoint, {"model": "m"})
    )
    wrapped = await drive(wrapped_entry)

    assert wrapped == unwrapped, (
        f"MeterGraph changed application-visible behavior:\n"
        f"  unwrapped={unwrapped!r}\n  wrapped=  {wrapped!r}"
    )
    return ParityResult(observation=wrapped, rows=rows.rows, provider=provider)


# --- Drivers: the application programs the harness replays against both the
# --- raw manager and the MeterGraph wrapper. Kept generic and configurable so
# --- the whole lifecycle matrix reuses these two shapes.


def sync_run_driver(caller_error: BaseException | None = None):
    """Enter the context, iterate fully, read the helper/public attribute, and
    optionally raise ``caller_error`` inside the context."""

    def drive(entry: Any) -> Observation:
        yielded: list[Any] = []
        helper = None
        public_attr = None
        error: BaseException | None = None
        try:
            with entry as stream:
                for chunk in stream:
                    yielded.append(chunk)
                helper = stream.get_final_message()
                public_attr = stream.request_id
                if caller_error is not None:
                    raise caller_error
        except BaseException as exc:  # noqa: BLE001 - record whatever escapes
            error = exc
        return _observe(
            entry,
            yielded=yielded,
            helper=helper,
            public_attr=public_attr,
            error=error,
            caller_error=caller_error,
        )

    return drive


def sync_close_driver(times: int):
    """Enter the context and close the stream ``times`` times (early cancel)."""

    def drive(entry: Any) -> Observation:
        with entry as stream:
            for _ in range(times):
                stream.close()
        return _observe(entry, yielded=[])

    return drive


def sync_reexit_driver():
    """Exit the context normally, then call ``__exit__`` again explicitly."""

    def drive(entry: Any) -> Observation:
        with entry as _stream:
            pass
        entry.__exit__(None, None, None)
        return _observe(entry, yielded=[])

    return drive


def async_run_driver(caller_error: BaseException | None = None):
    async def drive(entry: Any) -> Observation:
        yielded: list[Any] = []
        helper = None
        public_attr = None
        error: BaseException | None = None
        try:
            async with entry as stream:
                async for chunk in stream:
                    yielded.append(chunk)
                helper = await stream.get_final_message()
                public_attr = stream.request_id
                if caller_error is not None:
                    raise caller_error
        except BaseException as exc:  # noqa: BLE001 - record whatever escapes
            error = exc
        return _observe(
            entry,
            yielded=yielded,
            helper=helper,
            public_attr=public_attr,
            error=error,
            caller_error=caller_error,
        )

    return drive


def async_close_driver(times: int):
    async def drive(entry: Any) -> Observation:
        async with entry as stream:
            for _ in range(times):
                await stream.aclose()
        return _observe(entry, yielded=[])

    return drive


def async_reexit_driver():
    async def drive(entry: Any) -> Observation:
        async with entry as _stream:
            pass
        await entry.__aexit__(None, None, None)
        return _observe(entry, yielded=[])

    return drive
