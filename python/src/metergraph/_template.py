"""Stable, privacy-conscious request fingerprinting for untagged traffic."""

from __future__ import annotations

import dataclasses
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
# reads, so they are dropped whole. Compared lower-cased, matching the
# TypeScript SDK's camelCase client options too.
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
        # A value JSON mode cannot hold must not cost the structure around it,
        # which is what analysis reads: dump the Python form and let each leaf
        # fall back on its own.
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

    # Credential names are left out at every depth, as they always were: this
    # hash routes unnamed workloads, so it must not move for existing traffic.
    encoded = json.dumps(
        skeleton(_without_credential_names(json_value(request))),
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(encoded.encode()).hexdigest()
