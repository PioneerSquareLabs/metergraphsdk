"""Public Metergraph Python SDK."""

from __future__ import annotations

import atexit
import logging
import math
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Callable

from ._capture import Options, Runtime, set_runtime
from ._capture import wrap as _wrap
from ._config import ConfigPoller
from ._context import (
    context,
    route,
    session,
    set_default_tags,
    set_session,
    set_tags,
    snapshot,
    tags,
    trace,
    wrap_executor,
)
from ._batch_first import (
    BatchFirstIneligibleError,
    BatchFirstMetadata,
    BatchFirstResult,
    LateBatchInfo,
    batch_first,
)
from ._repo_config import RepoConfig, discover_repo_config
from ._session import SessionManager
from ._track import track
from ._transport import Writer
from ._version import SDK_VERSION


__version__ = SDK_VERSION
DEFAULT_INGEST_URL = "https://d2xus7mp8zdv6t.cloudfront.net"
log = logging.getLogger("metergraph")
_writer: Writer | None = None
_config: ConfigPoller | None = None
_session_manager: SessionManager | None = None
_initialized = False
_warned_no_token = False
_warned_no_repository = False
_warned_repeated_init = False


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.lower() not in {"0", "false", "no", "off"}


def _warn_repeated_init() -> None:
    global _warned_repeated_init
    if _warned_repeated_init:
        return
    _warned_repeated_init = True
    log.warning(
        "Metergraph init() was called more than once; "
        "the first configuration remains active."
    )


def init(
    *,
    token: str | None = None,
    ingest_url: str | None = None,
    capture_text: bool | None = None,
    redact: Callable[[str, str], str] | None = None,
    app_root: str | None = None,
    repository: str | None = None,
    skip_frames: list[str] | None = None,
    environment: str | None = None,
    disabled: bool | None = None,
) -> None:
    """Initialize capture. This function is idempotent and never raises."""
    global _initialized, _warned_no_token, _warned_no_repository
    global _writer, _config, _session_manager
    if _initialized:
        _warn_repeated_init()
        return
    if os.getenv("METERGRAPH_DISABLED") == "1" or disabled:
        _initialized = True
        return
    token = token or os.getenv("METERGRAPH_APP_TOKEN")
    ingest_url = ingest_url or os.getenv("METERGRAPH_INGEST_URL") or DEFAULT_INGEST_URL
    if not token or not ingest_url:
        # Stay uninitialized so a later init() that supplies a token succeeds.
        if not _warned_no_token:
            _warned_no_token = True
            log.warning(
                "Metergraph capture disabled: token and ingest URL are required"
            )
        return
    try:
        app_root_resolved = os.path.realpath(app_root or os.getcwd())
        repository_value = repository or os.getenv("METERGRAPH_REPOSITORY")
        repo_config = (
            RepoConfig(repository_value.strip(), app_root_resolved)
            if isinstance(repository_value, str)
            and "/" in repository_value.strip()
            else discover_repo_config(app_root_resolved)
        )
        _initialized = True
        if repo_config is None and not _warned_no_repository:
            _warned_no_repository = True
            log.warning(
                "Metergraph repository identity is not configured; set "
                "init(repository='owner/repository'), METERGRAPH_REPOSITORY, "
                "or provide .metergraph/config.json. Continuing with legacy ingestion."
            )
        session = (
            SessionManager(
                token,
                ingest_url,
                repository=repo_config.repository,
                sdk_version=SDK_VERSION,
            )
            if repo_config is not None
            else None
        )
        _session_manager = session
        _writer = Writer(
            token,
            ingest_url,
            session=session,
            queue_size=int(os.getenv("METERGRAPH_QUEUE_SIZE", "2000")),
            batch_size=int(os.getenv("METERGRAPH_BATCH_SIZE", "100")),
            flush_seconds=float(os.getenv("METERGRAPH_FLUSH_SECONDS", "5")),
        )
        options = Options(
            capture_text=(
                _env_bool("METERGRAPH_CAPTURE_TEXT", True)
                if capture_text is None
                else capture_text
            ),
            redact=redact,
            app_root=app_root_resolved,
            repo_root=repo_config.repo_root if repo_config is not None else None,
            skip_frames=tuple(skip_frames or ()),
            environment=environment or os.getenv("METERGRAPH_ENV"),
            text_max_bytes=min(
                100 * 1024,
                max(
                    1,
                    int(
                        os.getenv(
                            "METERGRAPH_TEXT_MAX_BYTES", str(100 * 1024)
                        )
                    ),
                ),
            ),
        )
        set_runtime(Runtime(_writer, options))
        _config = ConfigPoller(
            token,
            ingest_url,
            poll_seconds=float(os.getenv("METERGRAPH_CONFIG_POLL_SECONDS", "30")),
            hard_ttl_seconds=float(
                os.getenv("METERGRAPH_CONFIG_HARD_TTL_SECONDS", "120")
            ),
        )
        atexit.register(shutdown)
    except Exception:
        set_runtime(None)
        if _writer:
            _writer.shutdown()
        _writer = None
        _config = None
        _session_manager = None
        log.warning(
            "Metergraph initialization failed; application is running uninstrumented"
        )


def wrap(client: Any, *, provider: str | None = None) -> Any:
    """Wrap an OpenAI, Anthropic, Google, or Vercel AI Gateway client.

    Calls init() automatically, so with env-var configuration this is the
    only setup line needed. OpenAI and Anthropic clients using Vercel's public
    AI Gateway URL are detected automatically; pass ``provider="vercel"`` to
    force gateway handling for a compatible client with a custom URL. Call
    init(...) first to pass Metergraph options in code.
    """
    if not _initialized:
        init()
    return _wrap(client, provider=provider)


def model_for(route_name: str, *, default: str, session_key: str | None = None) -> str:
    """Return a sticky canary model, or the incumbent on every failure path."""
    if _config is None:
        return default
    return _config.model_for(route_name, default, session_key or snapshot().session_id)


def record_outcome(
    route_name: str,
    *,
    model: str,
    task_completed: bool,
    session_key: str | None = None,
    feedback_score: float | None = None,
    turns_to_resolution: int | None = None,
    escalated: bool | None = None,
    abandoned: bool | None = None,
    edit_distance_ratio: float | None = None,
    regeneration_count: int | None = None,
    event_id: str | None = None,
) -> bool:
    """Enqueue a content-free real outcome without touching the request path."""
    if _writer is None or not isinstance(task_completed, bool):
        return False
    route_name = str(route_name).strip()[:512]
    model = str(model).strip()[:512]
    session_key = str(session_key or snapshot().session_id or "").strip()[:512]
    event_id = str(event_id or uuid.uuid4()).strip()[:128]
    try:
        feedback_score = float(feedback_score) if feedback_score is not None else None
        edit_distance_ratio = (
            float(edit_distance_ratio) if edit_distance_ratio is not None else None
        )
    except (TypeError, ValueError, OverflowError):
        return False
    if not route_name or not model or not session_key or not event_id:
        return False
    if feedback_score is not None and (
        not math.isfinite(feedback_score) or not -1 <= feedback_score <= 1
    ):
        return False
    if turns_to_resolution is not None and (
        isinstance(turns_to_resolution, bool)
        or not isinstance(turns_to_resolution, int)
        or not 1 <= turns_to_resolution <= 1_000_000
    ):
        return False
    if edit_distance_ratio is not None and (
        not math.isfinite(edit_distance_ratio)
        or not 0 <= edit_distance_ratio <= 1
    ):
        return False
    if regeneration_count is not None and (
        isinstance(regeneration_count, bool)
        or not isinstance(regeneration_count, int)
        or not 0 <= regeneration_count <= 1_000_000
    ):
        return False
    if escalated is not None and not isinstance(escalated, bool):
        return False
    if abandoned is not None and not isinstance(abandoned, bool):
        return False
    return _writer.enqueue(
        {
            "event_type": "outcome",
            "event_id": event_id,
            "ts": datetime.now(timezone.utc).isoformat(),
            "route": route_name,
            "session_id": session_key,
            "model": model,
            "task_completed": task_completed,
            "feedback_score": feedback_score,
            "turns_to_resolution": turns_to_resolution,
            "escalated": escalated,
            "abandoned": abandoned,
            "edit_distance_ratio": edit_distance_ratio,
            "regeneration_count": regeneration_count,
        }
    )


def flush(timeout: float = 3.0) -> bool:
    return True if _writer is None else _writer.flush(timeout)


def shutdown() -> None:
    global _writer, _config, _session_manager
    if _config:
        _config.stop()
        _config = None
    if _writer:
        _writer.shutdown()
        _writer = None
    if _session_manager:
        _session_manager.stop()
        _session_manager = None
    set_runtime(None)


__all__ = [
    "DEFAULT_INGEST_URL",
    "BatchFirstIneligibleError",
    "BatchFirstMetadata",
    "BatchFirstResult",
    "LateBatchInfo",
    "batch_first",
    "context",
    "flush",
    "init",
    "model_for",
    "record_outcome",
    "route",
    "session",
    "set_default_tags",
    "set_session",
    "set_tags",
    "shutdown",
    "tags",
    "track",
    "trace",
    "wrap",
    "wrap_executor",
]
