"""Ambient route/session metadata, safe across async call trees."""

from __future__ import annotations

import contextvars
import functools
import inspect
import logging
import secrets
from concurrent.futures import Executor
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Mapping


@dataclass(frozen=True)
class CaptureContext:
    route: str | None = None
    session_id: str | None = None
    tags: Mapping[str, str] = field(default_factory=dict)
    unit_name: str | None = None
    unit_count: float | None = None
    capture_text: bool | None = None
    func_name: str | None = None
    func_module: str | None = None
    trace_id: str | None = None
    trace_name: str | None = None
    parent_span_id: str | None = None


_current: contextvars.ContextVar[CaptureContext] = contextvars.ContextVar(
    "metergraph_context", default=CaptureContext()
)
_active_depth: contextvars.ContextVar[int] = contextvars.ContextVar(
    "metergraph_context_depth", default=0
)
_default_tags: dict[str, str] = {}
_warned_session_outside_scope = False
_warned_tags_outside_scope = False
log = logging.getLogger("metergraph")


def snapshot() -> CaptureContext:
    current = _current.get()
    if _active_depth.get() == 0 and _default_tags:
        return replace(current, tags={**_default_tags, **current.tags})
    return current


def _enter_scope(value: CaptureContext):
    return _current.set(value), _active_depth.set(_active_depth.get() + 1)


def _exit_scope(tokens) -> None:
    current_token, depth_token = tokens
    _current.reset(current_token)
    _active_depth.reset(depth_token)


class context:
    """Scoped session and tag context manager and sync/async decorator."""

    def __init__(
        self,
        *,
        session_id: str | None = None,
        tags: Mapping[str, Any] | None = None,
    ) -> None:
        self.session_id = str(session_id) if session_id is not None else None
        self.tags = {str(k): str(v) for k, v in (tags or {}).items()}
        self._tokens = None

    def __enter__(self) -> "context":
        current = snapshot()
        self._tokens = _enter_scope(
            replace(
                current,
                session_id=(
                    self.session_id
                    if self.session_id is not None
                    else current.session_id
                ),
                tags={**current.tags, **self.tags},
            )
        )
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._tokens is not None:
            _exit_scope(self._tokens)
            self._tokens = None

    def __call__(self, fn: Callable):
        if inspect.iscoroutinefunction(fn):

            @functools.wraps(fn)
            async def async_wrapped(*args, **kwargs):
                with type(self)(session_id=self.session_id, tags=self.tags):
                    return await fn(*args, **kwargs)

            return async_wrapped

        @functools.wraps(fn)
        def wrapped(*args, **kwargs):
            with type(self)(session_id=self.session_id, tags=self.tags):
                return fn(*args, **kwargs)

        return wrapped


def session(session_id: str | None) -> context:
    """Return a scope that overrides the current session ID."""
    return context(session_id=session_id)


def tags(**values: Any) -> context:
    """Return a scope that merges tags into the current context."""
    return context(tags=values)


def set_default_tags(**values: Any) -> None:
    """Replace process-wide tags inherited by new Metergraph scopes."""
    global _default_tags
    _default_tags = {str(k): str(v) for k, v in values.items()}


class route:
    """Route context manager and sync/async decorator."""

    def __init__(
        self,
        name: str,
        *,
        unit: str | None = None,
        unit_count: float | None = None,
        tags: Mapping[str, Any] | None = None,
        capture_text: bool | None = None,
    ) -> None:
        self.name = str(name)
        self.unit = str(unit) if unit is not None else None
        self.unit_count = (
            float(unit_count) if unit_count is not None else (1.0 if unit else None)
        )
        self.tags = {str(k): str(v) for k, v in (tags or {}).items()}
        self.capture_text = (
            bool(capture_text) if capture_text is not None else None
        )
        self._tokens = None

    def __enter__(self) -> "route":
        current = snapshot()
        merged = {**current.tags, **self.tags}
        self._tokens = _enter_scope(
            replace(
                current,
                route=self.name,
                tags=merged,
                unit_name=self.unit if self.unit is not None else current.unit_name,
                unit_count=self.unit_count
                if self.unit is not None
                else current.unit_count,
                capture_text=self.capture_text
                if self.capture_text is not None
                else current.capture_text,
            )
        )
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._tokens is not None:
            _exit_scope(self._tokens)
            self._tokens = None

    def __call__(self, fn: Callable):
        if inspect.iscoroutinefunction(fn):

            @functools.wraps(fn)
            async def async_wrapped(*args, **kwargs):
                with type(self)(
                    self.name,
                    unit=self.unit,
                    unit_count=self.unit_count,
                    tags=self.tags,
                    capture_text=self.capture_text,
                ):
                    return await fn(*args, **kwargs)

            return async_wrapped

        @functools.wraps(fn)
        def wrapped(*args, **kwargs):
            with type(self)(
                self.name,
                unit=self.unit,
                unit_count=self.unit_count,
                tags=self.tags,
                capture_text=self.capture_text,
            ):
                return fn(*args, **kwargs)

        return wrapped


class trace:
    """Logical trace context manager and sync/async decorator."""

    def __init__(
        self,
        name: str,
        *,
        trace_id: str | None = None,
        parent_span_id: str | None = None,
        capture_text: bool | None = None,
    ) -> None:
        self.name = str(name)
        self.trace_id = str(trace_id).strip() if trace_id is not None else None
        self.parent_span_id = (
            str(parent_span_id).strip() if parent_span_id is not None else None
        )
        self.capture_text = (
            bool(capture_text) if capture_text is not None else None
        )
        self._tokens = None

    def __enter__(self) -> "trace":
        current = snapshot()
        requested = self.trace_id
        reuse = current.trace_id is not None and (
            requested is None or requested == current.trace_id
        )
        self._tokens = _enter_scope(
            replace(
                current,
                trace_id=(
                    current.trace_id
                    if reuse
                    else requested or secrets.token_hex(16)
                ),
                trace_name=current.trace_name if reuse else self.name,
                parent_span_id=(
                    self.parent_span_id
                    if self.parent_span_id is not None
                    else current.parent_span_id
                    if reuse
                    else None
                ),
                capture_text=(
                    self.capture_text
                    if self.capture_text is not None
                    else current.capture_text
                ),
            )
        )
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._tokens is not None:
            _exit_scope(self._tokens)
            self._tokens = None

    def __call__(self, fn: Callable):
        if inspect.iscoroutinefunction(fn):

            @functools.wraps(fn)
            async def async_wrapped(*args, **kwargs):
                with type(self)(
                    self.name,
                    trace_id=self.trace_id,
                    parent_span_id=self.parent_span_id,
                    capture_text=self.capture_text,
                ):
                    return await fn(*args, **kwargs)

            return async_wrapped

        @functools.wraps(fn)
        def wrapped(*args, **kwargs):
            with type(self)(
                self.name,
                trace_id=self.trace_id,
                parent_span_id=self.parent_span_id,
                capture_text=self.capture_text,
            ):
                return fn(*args, **kwargs)

        return wrapped


def set_session(session_id: str | None) -> None:
    global _warned_session_outside_scope
    if _active_depth.get() == 0:
        if not _warned_session_outside_scope:
            _warned_session_outside_scope = True
            log.warning(
                "metergraph.set_session() requires an active Metergraph context; "
                "use metergraph.context() or metergraph.session()."
            )
        return
    _current.set(
        replace(snapshot(), session_id=str(session_id) if session_id else None)
    )


def set_tags(**tags: Any) -> None:
    global _warned_tags_outside_scope
    if _active_depth.get() == 0:
        if not _warned_tags_outside_scope:
            _warned_tags_outside_scope = True
            log.warning(
                "metergraph.set_tags() requires an active Metergraph context; "
                "use metergraph.context() or metergraph.tags()."
            )
        return
    current = snapshot()
    merged = {**current.tags, **{str(k): str(v) for k, v in tags.items()}}
    _current.set(replace(current, tags=merged))


def wrap_executor(executor: Executor) -> Executor:
    """Propagate the current context into executor submissions."""
    if getattr(executor, "__metergraph__", False):
        return executor
    original_submit = executor.submit

    @functools.wraps(original_submit)
    def submit(fn, /, *args, **kwargs):
        ctx = contextvars.copy_context()
        return original_submit(ctx.run, fn, *args, **kwargs)

    executor.submit = submit  # type: ignore[method-assign]
    executor.__metergraph__ = True  # type: ignore[attr-defined]
    return executor
