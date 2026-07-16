"""ETag-aware, fail-open canary configuration polling."""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Mapping
from typing import Any


log = logging.getLogger("metergraph")


def _bucket(seed: str) -> float:
    """FNV-1a/64 over UTF-8; kept byte-identical to the TypeScript SDK."""
    value = 0xCBF29CE484222325
    for byte in seed.encode():
        value ^= byte
        value = (value * 0x100000001B3) & 0xFFFF_FFFF_FFFF_FFFF
    return value / 2**64 * 100


def choose_model(
    route: str,
    default: str,
    session_key: str | None,
    route_config: Mapping[str, Any] | None,
) -> str:
    """Deterministically assign a session to a canary arm."""
    if not route_config or route_config.get("enabled", True) is False:
        return default
    incumbent = str(route_config.get("incumbent_model") or default)
    challenger = route_config.get("challenger_model") or route_config.get("model")
    if not challenger or not session_key:
        return incumbent
    try:
        percent = float(
            route_config.get(
                "traffic_percent",
                route_config.get("percentage", route_config.get("allocation", 0)),
            )
        )
    except (TypeError, ValueError):
        return incumbent
    percent = max(0.0, min(percent, 100.0))
    seed = ":".join(
        (
            route,
            str(route_config.get("version", "")),
            str(route_config.get("salt", "")),
            str(session_key),
        )
    )
    return str(challenger) if _bucket(seed) < percent else incumbent


class ConfigPoller:
    def __init__(
        self,
        token: str,
        base_url: str,
        *,
        poll_seconds: float = 30.0,
        hard_ttl_seconds: float = 120.0,
    ) -> None:
        self._token = token
        self._url = f"{base_url.rstrip('/')}/v1/config"
        self._poll_seconds = max(1.0, poll_seconds)
        self._hard_ttl = max(self._poll_seconds, hard_ttl_seconds)
        self._etag: str | None = None
        self._routes: dict[str, dict[str, Any]] = {}
        self._last_success = 0.0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name="metergraph-config", daemon=True
        )
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            self.poll_once()
            self._stop.wait(self._poll_seconds)

    def poll_once(self) -> bool:
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/json",
        }
        if self._etag:
            headers["If-None-Match"] = self._etag
        request = urllib.request.Request(self._url, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                if response.status == 304:
                    with self._lock:
                        self._last_success = time.monotonic()
                    return True
                doc = json.loads(response.read())
                routes = doc.get("routes", {}) if isinstance(doc, dict) else {}
                if not isinstance(routes, dict):
                    return False
                with self._lock:
                    self._routes = {
                        str(k): dict(v)
                        for k, v in routes.items()
                        if isinstance(v, dict)
                    }
                    self._etag = response.headers.get("ETag")
                    self._last_success = time.monotonic()
                return True
        except urllib.error.HTTPError as exc:
            # urllib raises for 304 even though it is a successful revalidation.
            if exc.code == 304:
                with self._lock:
                    self._last_success = time.monotonic()
                return True
            if exc.code in (401, 403):
                log.warning(
                    "Metergraph config authentication failed; using default models"
                )
                self._stop.set()
            return False
        except Exception:
            return False

    def model_for(self, route: str, default: str, session_key: str | None) -> str:
        with self._lock:
            if (
                not self._last_success
                or time.monotonic() - self._last_success > self._hard_ttl
            ):
                return default
            config = self._routes.get(route)
        return choose_model(route, default, session_key, config)

    def stop(self) -> None:
        self._stop.set()
        if threading.current_thread() is not self._thread:
            self._thread.join(timeout=1)
