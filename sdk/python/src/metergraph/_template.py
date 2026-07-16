"""Stable, privacy-conscious request fingerprinting for untagged traffic."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from typing import Any


_UUID = re.compile(r"\b[0-9a-f]{8}-[0-9a-f-]{27,}\b", re.I)
_EMAIL = re.compile(r"\b[^\s@]+@[^\s@]+\.[^\s@]+\b")
_URL = re.compile(r"\bhttps?://\S+")
_NUMBER = re.compile(r"(?<![A-Za-z])[-+]?\d+(?:\.\d+)?(?![A-Za-z])")
_LONG_TOKEN = re.compile(r"\b[A-Za-z0-9_-]{24,}\b")
_SENSITIVE_KEYS = {"api_key", "apikey", "authorization", "headers", "token", "secret"}


def _normalize_text(value: str) -> str:
    value = _UUID.sub("<uuid>", value)
    value = _EMAIL.sub("<email>", value)
    value = _URL.sub("<url>", value)
    value = _LONG_TOKEN.sub("<token>", value)
    value = _NUMBER.sub("<n>", value)
    return " ".join(value.split())


def scrub(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(k): scrub(v)
            for k, v in value.items()
            if str(k).lower() not in _SENSITIVE_KEYS
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [scrub(item) for item in value]
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return repr(value)


def template_hash(request: Mapping[str, Any]) -> str:
    def skeleton(value: Any) -> Any:
        if isinstance(value, Mapping):
            return {str(k): skeleton(v) for k, v in sorted(value.items())}
        if isinstance(value, list):
            return [skeleton(item) for item in value]
        if isinstance(value, str):
            return _normalize_text(value)
        return value

    encoded = json.dumps(
        skeleton(scrub(request)), sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(encoded.encode()).hexdigest()
