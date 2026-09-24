"""Stable, privacy-conscious request fingerprinting for untagged traffic."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from typing import Any


_UUID = re.compile(r"\b[0-9a-f]{8}-[0-9a-f-]{27,}\b", re.I)
_EMAIL = re.compile(r"\b[^\s@]+@[^\s@]+\.[^\s@]+\b")
_URL = re.compile(r"\bhttps?://\S+")
_NUMBER = re.compile(r"(?<![A-Za-z])[-+]?\d+(?:\.\d+)?(?![A-Za-z])")
_LONG_TOKEN = re.compile(r"\b[A-Za-z0-9_-]{24,}\b")
_SENSITIVE_KEYS = {
    "api-key",
    "api_key",
    "apikey",
    "authorization",
    "client_secret",
    "cookie",
    "headers",
    "id_token",
    "password",
    "proxy-authorization",
    "refresh_token",
    "secret",
    "set-cookie",
    "token",
    "x-api-key",
}


def _normalize_text(value: str) -> str:
    value = _UUID.sub("<uuid>", value)
    value = _EMAIL.sub("<email>", value)
    value = _URL.sub("<url>", value)
    value = _LONG_TOKEN.sub("<token>", value)
    value = _NUMBER.sub("<n>", value)
    return " ".join(value.split())


# Header and query containers carry transport credentials and nothing analysis
# reads, so they are dropped whole. Keys compare lower-cased, so camelCase
# client options match.
_TRANSPORT_CONTAINERS = {
    "default_headers",
    "default_query",
    "defaultheaders",
    "defaultquery",
    "extra_headers",
    "extra_query",
    "extraheaders",
    "extraquery",
    "headers",
    "query",
}


def json_value(value: Any) -> Any:
    """`value` as plain JSON data, with nothing removed."""
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        try:
            return json_value(model_dump(mode="json", exclude_none=True))
        except Exception:
            pass
        # The Python-mode dump keeps the structure analysis reads; only the
        # values JSON cannot hold fall back to text.
        try:
            return json_value(model_dump(exclude_none=True))
        except Exception:
            return repr(value)
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return json_value({
            field.name: getattr(value, field.name)
            for field in dataclasses.fields(value)
            if getattr(value, field.name) is not None
        })
    if isinstance(value, Mapping):
        return {str(k): json_value(v) for k, v in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [json_value(item) for item in value]
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return repr(value)


class Unrepresentable(Exception):
    """A value JSON cannot hold, where degrading it to text would be a lie.

    `json_value` degrades such a value to `repr()`, which is right for a best
    effort capture of a whole request. It is wrong where the caller is deciding
    whether a structure was represented faithfully, so that decision uses
    `json_value_strict` and treats this exception as "not representable".
    """


def json_value_strict(value: Any) -> Any:
    """`value` as plain JSON data, raising rather than degrading to `repr()`."""
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        try:
            return json_value_strict(model_dump(mode="json", exclude_none=True))
        except Unrepresentable:
            raise
        except Exception:
            pass
        try:
            dumped = model_dump(exclude_none=True)
        except Exception as exc:
            raise Unrepresentable(type(value).__name__) from exc
        return json_value_strict(dumped)
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return json_value_strict({
            field.name: getattr(value, field.name)
            for field in dataclasses.fields(value)
            if getattr(value, field.name) is not None
        })
    if isinstance(value, Mapping):
        return {str(k): json_value_strict(v) for k, v in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [json_value_strict(item) for item in value]
    if isinstance(value, str):
        return value
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        # NaN and Infinity are not JSON. Promising a lossless copy of one would
        # be a lie, so the caller treats the value as unrepresentable.
        if not math.isfinite(value):
            raise Unrepresentable("non-finite float")
        return value
    raise Unrepresentable(type(value).__name__)


def json_equal(left: Any, right: Any) -> bool:
    """Semantic JSON equality: object key order ignored, array order kept.

    `True` and `1` compare equal under `==` in Python and are different JSON
    values, so booleans are compared by type as well as value. Numbers compare
    by value, so `1` and `1.0` are equal.
    """
    if isinstance(left, bool) or isinstance(right, bool):
        return isinstance(left, bool) and isinstance(right, bool) and left is right
    if isinstance(left, Mapping) and isinstance(right, Mapping):
        if set(left) != set(right):
            return False
        return all(json_equal(left[key], right[key]) for key in left)
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(
            json_equal(one, other) for one, other in zip(left, right)
        )
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return left == right
    if type(left) is not type(right):
        return False
    return left == right


def _credential_key(key: str) -> bool:
    key = key.strip().lower()
    return key in _SENSITIVE_KEYS or key in _TRANSPORT_CONTAINERS


def scrub_request(request: Any) -> Any:
    """The provider request as captured: JSON data without its credentials.

    Credentials travel as request-level parameters and in header or query
    containers. `extra_body` is merged into the request body, so its keys are
    request-level too. Messages, tools and schemas are kept exactly as sent,
    even where a name matches a credential's (a tool parameter called `token`),
    because they are the request analysis replays.
    """
    value = json_value(request)
    if not isinstance(value, Mapping):
        return value
    clean = {k: v for k, v in value.items() if not _credential_key(k)}
    extra_body = clean.get("extra_body")
    if isinstance(extra_body, Mapping):
        clean["extra_body"] = {
            k: v for k, v in extra_body.items() if not _credential_key(k)
        }
    return clean


def _without_credential_names(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            k: _without_credential_names(v)
            for k, v in value.items()
            if k.strip().lower() not in _SENSITIVE_KEYS
        }
    if isinstance(value, list):
        return [_without_credential_names(item) for item in value]
    return value


def template_hash(request: Mapping[str, Any]) -> str:
    def skeleton(value: Any) -> Any:
        if isinstance(value, Mapping):
            return {str(k): skeleton(v) for k, v in sorted(value.items())}
        if isinstance(value, list):
            return [skeleton(item) for item in value]
        if isinstance(value, str):
            return _normalize_text(value)
        return value

    # Credential names are left out at every depth. This hash routes unnamed
    # workloads, so its input must stay stable across SDK versions.
    encoded = json.dumps(
        skeleton(_without_credential_names(json_value(request))),
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(encoded.encode()).hexdigest()
