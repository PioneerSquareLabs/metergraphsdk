"""Rate-limited failure logging.

Logs the first occurrence of a failure kind immediately, then suppresses
repeats within a quiet window and reports how many were suppressed on the
next log line for that kind. Keeps log volume bounded under sustained
failure without ever going completely silent.
"""

from __future__ import annotations

import logging
import time
from typing import Callable


log = logging.getLogger("metergraph")


class FailureLogger:
    def __init__(
        self,
        quiet_seconds: float = 60.0,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._quiet_seconds = quiet_seconds
        self._clock = clock
        self._last_logged: dict[str, float] = {}
        self._suppressed: dict[str, int] = {}

    def report(self, kind: str, message: str, level: int = logging.WARNING) -> None:
        now = self._clock()
        last = self._last_logged.get(kind)
        if last is not None and now - last < self._quiet_seconds:
            self._suppressed[kind] = self._suppressed.get(kind, 0) + 1
            return
        suppressed = self._suppressed.pop(kind, 0)
        suffix = (
            f" ({suppressed} more suppressed in the last {self._quiet_seconds:.0f}s)"
            if suppressed
            else ""
        )
        log.log(level, "metergraph: %s%s", message, suffix)
        self._last_logged[kind] = now
