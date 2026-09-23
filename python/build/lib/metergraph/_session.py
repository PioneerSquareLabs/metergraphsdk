"""SDK 0.4+ repository-aware ingestion: app-token -> session-token exchange.

The app token is sent only on POST /v1/ingest/sessions. The resulting
session token is cached in memory and reused until it's within
REFRESH_MARGIN_SECONDS of expiry, at which point the next get_token() call
transparently re-exchanges it. Exchanges happen lazily on the writer's own
background delivery thread (never on the caller's request path), so a
blocking round trip here adds no latency to the customer's LLM call. Every
failure mode is fail-open: get_token() returns None and callers drop/buffer
that batch rather than ever sending the app token to /v1/ingest.
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any

from ._failure_log import FailureLogger


DEFAULT_TIMEOUT_SECONDS = 3.0
DEFAULT_TTL_SECONDS = 300.0
REFRESH_MARGIN_SECONDS = 30.0
MAX_BACKOFF_SECONDS = 60.0


def _parse_expires_at(value: Any) -> float | None:
    if not isinstance(value, str):
        return None
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


class SessionManager:
    def __init__(
        self,
        app_token: str,
        base_url: str,
        *,
        repository: str,
        sdk_version: str,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._app_token = app_token
        self._url = f"{base_url.rstrip('/')}/v1/ingest/sessions"
        self._repository = repository
        self._sdk_version = sdk_version
        self._timeout = max(0.1, timeout_seconds)
        self._lock = threading.Lock()
        self._token: str | None = None
        self._expires_at = 0.0
        self._stopped = False
        self._retry_at = 0.0
        self._backoff = 1.0
        self._failure_log = FailureLogger()
        if hasattr(os, "register_at_fork"):
            os.register_at_fork(after_in_child=self._after_fork)

    def _after_fork(self) -> None:
        # A lock held by another thread at fork stays locked forever in the
        # child. The cached token itself is safe to reuse; only reset the lock.
        self._lock = threading.Lock()

    def get_token(self) -> str | None:
        with self._lock:
            if self._stopped:
                return None
            if self._token is not None and time.time() < self._expires_at - REFRESH_MARGIN_SECONDS:
                return self._token
            if time.monotonic() < self._retry_at:
                return None
        self._exchange()
        with self._lock:
            return None if self._stopped else self._token

    def invalidate(self) -> None:
        with self._lock:
            self._token = None
            self._expires_at = 0.0
            self._retry_at = 0.0
            self._backoff = 1.0

    def stop(self) -> None:
        with self._lock:
            self._stopped = True
            self._token = None
            self._expires_at = 0.0

    def _exchange(self) -> None:
        body = json.dumps(
            {
                "protocol_version": 2,
                "repository": self._repository,
                "sdk_version": self._sdk_version,
            }
        ).encode()
        request = urllib.request.Request(
            self._url,
            data=body,
            headers={
                "Authorization": f"Bearer {self._app_token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                doc = json.loads(response.read())
        except urllib.error.HTTPError as exc:
            self._failure_log.report(
                "session_exchange_error",
                f"session exchange to {self._url} failed with HTTP {exc.code}",
            )
            self._mark_failed()
            return
        except Exception as exc:
            self._failure_log.report(
                "session_exchange_error",
                f"session exchange to {self._url} failed: {type(exc).__name__}: {exc}",
            )
            self._mark_failed()
            return
        token = doc.get("session_token") if isinstance(doc, dict) else None
        if not isinstance(token, str) or not token:
            self._failure_log.report(
                "session_exchange_error",
                f"session exchange to {self._url} returned no session_token",
            )
            self._mark_failed()
            return
        expires_at = _parse_expires_at(doc.get("expires_at")) or (
            time.time() + DEFAULT_TTL_SECONDS
        )
        with self._lock:
            if not self._stopped:
                self._token = token
                self._expires_at = expires_at
                self._retry_at = 0.0
                self._backoff = 1.0

    def _mark_failed(self) -> None:
        with self._lock:
            self._retry_at = time.monotonic() + self._backoff
            self._backoff = min(self._backoff * 2, MAX_BACKOFF_SECONDS)
