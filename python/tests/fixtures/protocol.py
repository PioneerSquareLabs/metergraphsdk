"""Reusable, test-only protocol doubles for stream lifecycle contracts.

These model the shape that matters most for MeterGraph's stream wrappers: a
streaming *context manager* (like ``client.messages.stream(...)``) whose
``__enter__``/``__aenter__`` returns a **distinct** entered stream object. The
manager owns context entry/exit; the entered stream owns iteration and helper
methods such as ``get_final_message()``.

A single configurable pair of doubles (sync and async) covers every lifecycle
case by construction: successful iteration, context-entry failure, mid-stream
iteration failure, caller errors, exit-time suppression, and explicit
close/exit. Each double carries a shared :class:`StreamProbe` so a test can
observe *which* object served each operation and what exception each exit
received — provider-visible facts the parity harness folds into its comparison.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any


@dataclass
class StreamProbe:
    """Provider-side lifecycle record, shared by a manager and the distinct
    stream object it hands back on entry."""

    entered: int = 0
    exited: int = 0
    iterated: int = 0
    helper_calls: int = 0
    closed: int = 0
    exit_excs: list[Any] = field(default_factory=list)


class SyncEnteredStream:
    """The distinct object a sync stream manager yields from ``__enter__``.

    Owns iteration, the ``get_final_message()`` helper, ``close()``, and a
    public attribute — everything the wrapper must delegate to the entered
    stream rather than the manager.
    """

    def __init__(
        self,
        chunks: list[Any],
        final: Any,
        probe: StreamProbe,
        *,
        raise_after: int | None = None,
        error: BaseException | None = None,
    ) -> None:
        self._chunks = list(chunks)
        self._final = final
        # Same probe instance as the manager, exposed publicly so the parity
        # harness can read ``entry.probe`` uniformly whether ``entry`` is the
        # manager (unwrapped) or the wrapper delegating to this entered stream.
        self.probe = probe
        self._raise_after = raise_after
        self._error = error
        self.request_id = "req-entered-stream"

    def __iter__(self):
        self.probe.iterated += 1
        for index, chunk in enumerate(self._chunks):
            yield chunk
            if self._raise_after is not None and index == self._raise_after:
                raise self._error

    def get_final_message(self) -> Any:
        self.probe.helper_calls += 1
        return self._final

    def close(self) -> None:
        self.probe.closed += 1


class SyncStreamManager:
    """A sync streaming context manager that is *not itself iterable* — only
    the entered stream is. ``__enter__`` may raise to model entry failure, and
    ``__exit__`` returns ``suppress`` to model exception-swallowing managers."""

    def __init__(
        self,
        entered: SyncEnteredStream,
        probe: StreamProbe,
        *,
        enter_error: BaseException | None = None,
        suppress: bool = False,
    ) -> None:
        self._entered = entered
        self.probe = probe
        self._enter_error = enter_error
        self._suppress = suppress

    def __enter__(self) -> SyncEnteredStream:
        self.probe.entered += 1
        if self._enter_error is not None:
            raise self._enter_error
        return self._entered

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.probe.exited += 1
        self.probe.exit_excs.append(exc)
        return self._suppress


class AsyncEnteredStream:
    """Async counterpart to :class:`SyncEnteredStream`."""

    def __init__(
        self,
        chunks: list[Any],
        final: Any,
        probe: StreamProbe,
        *,
        raise_after: int | None = None,
        error: BaseException | None = None,
    ) -> None:
        self._chunks = list(chunks)
        self._final = final
        # See SyncEnteredStream: shared with the manager, exposed publicly.
        self.probe = probe
        self._raise_after = raise_after
        self._error = error
        self.request_id = "req-entered-stream"

    def __aiter__(self):
        self.probe.iterated += 1
        return self._aiter()

    async def _aiter(self):
        for index, chunk in enumerate(self._chunks):
            yield chunk
            if self._raise_after is not None and index == self._raise_after:
                raise self._error

    async def get_final_message(self) -> Any:
        self.probe.helper_calls += 1
        return self._final

    async def aclose(self) -> None:
        self.probe.closed += 1


class AsyncStreamManager:
    """Async counterpart to :class:`SyncStreamManager`."""

    def __init__(
        self,
        entered: AsyncEnteredStream,
        probe: StreamProbe,
        *,
        enter_error: BaseException | None = None,
        suppress: bool = False,
    ) -> None:
        self._entered = entered
        self.probe = probe
        self._enter_error = enter_error
        self._suppress = suppress

    async def __aenter__(self) -> AsyncEnteredStream:
        self.probe.entered += 1
        if self._enter_error is not None:
            raise self._enter_error
        return self._entered

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        self.probe.exited += 1
        self.probe.exit_excs.append(exc)
        return self._suppress


@dataclass
class StreamScenario:
    """A factory bundle: fresh manager instances that share the same scripted
    chunk objects, final message, and (where relevant) the *same* exception
    instance, so unwrapped and wrapped runs observe identical values and
    exception identity."""

    chunks: list[Any] = field(default_factory=list)
    final: Any = None
    enter_error: BaseException | None = None
    iter_error: BaseException | None = None
    raise_after: int | None = None
    suppress: bool = False

    def sync_factory(self) -> SyncStreamManager:
        probe = StreamProbe()
        entered = SyncEnteredStream(
            self.chunks,
            self.final,
            probe,
            raise_after=self.raise_after,
            error=self.iter_error,
        )
        return SyncStreamManager(
            entered, probe, enter_error=self.enter_error, suppress=self.suppress
        )

    def async_factory(self) -> AsyncStreamManager:
        probe = StreamProbe()
        entered = AsyncEnteredStream(
            self.chunks,
            self.final,
            probe,
            raise_after=self.raise_after,
            error=self.iter_error,
        )
        return AsyncStreamManager(
            entered, probe, enter_error=self.enter_error, suppress=self.suppress
        )


def _content_delta(text: str) -> Any:
    return SimpleNamespace(
        type="content_block_delta", delta=SimpleNamespace(text=text)
    )


def _final_message() -> Any:
    return SimpleNamespace(
        content=[SimpleNamespace(text="hello")],
        stop_reason="end_turn",
        usage=SimpleNamespace(input_tokens=6, output_tokens=2),
    )


def make_scenario(
    *,
    enter_error: BaseException | None = None,
    iter_error: BaseException | None = None,
    raise_after: int | None = None,
    suppress: bool = False,
) -> StreamScenario:
    """Build a two-chunk streaming context-manager scenario. Exception
    instances are shared across factory calls so identity is preserved between
    the unwrapped and wrapped runs."""
    return StreamScenario(
        chunks=[_content_delta("he"), _content_delta("llo")],
        final=_final_message(),
        enter_error=enter_error,
        iter_error=iter_error,
        raise_after=raise_after,
        suppress=suppress,
    )
