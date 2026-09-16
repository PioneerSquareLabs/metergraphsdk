"""Shared key and text scrubbing helpers."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any


SENSITIVE_KEYS: frozenset[str] = frozenset(
    {
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
)

DEFAULT_CATEGORIES: tuple[str, ...] = ("secret", "profile_url", "email", "phone")
_CATEGORIES = frozenset(DEFAULT_CATEGORIES)

_PEM_PRIVATE_KEY = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"
)
_SCHEME_CREDENTIAL = re.compile(
    r"\b(bearer|basic)([ \t\r\n\f\v]+)([A-Za-z0-9._~+/=-]{8,})",
    re.IGNORECASE | re.ASCII,
)
_AUTHORIZATION_SCHEME = re.compile(
    r"(?<![A-Za-z0-9_])"
    r"(authorization|proxy-authorization)"
    r"([\"']?[ \t\r\n\f\v]*[:=][ \t\r\n\f\v]*[\"']?)"
    r"(bearer|basic|token|digest)([ \t\r\n\f\v]+)"
    r"([^ \t\r\n\f\v\"',;<>]+)",
    re.IGNORECASE | re.ASCII,
)
_KEY_VALUE_CREDENTIAL = re.compile(
    r"(?<![A-Za-z0-9_])"
    r"(?:api[_-]?key|access[_-]?key|secret[_-]?key|client[_-]?secret|"
    r"private[_-]?key|access[_-]?token|refresh[_-]?token|id[_-]?token|"
    r"auth[_-]?token|session[_-]?token|bearer[_-]?token|password|passwd|"
    r"token|secret|authorization|proxy-authorization)"
    r"(?![A-Za-z0-9_])"
    r"([\"']?[ \t\r\n\f\v]*[:=][ \t\r\n\f\v]*[\"']?)"
    r"([^ \t\r\n\f\v\"',;<>]{6,})",
    re.IGNORECASE | re.ASCII,
)
_PROVIDER_TOKEN = re.compile(
    r"(?<![A-Za-z0-9])(?:"
    r"sk-[A-Za-z0-9_-]{8,}|"
    r"AKIA[0-9A-Z]{16}|"
    r"gh[pousr]_[A-Za-z0-9]{36,}|"
    r"xox[abprs]-[A-Za-z0-9-]{10,}|"
    r"AIza[0-9A-Za-z_-]{35}|"
    r"eyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"
    r")"
)
_PROFILE_URL = re.compile(
    r"https?://([a-z]{2,3}\.)?(www\.)?linkedin\.com/in/[A-Za-z0-9_.%-]+/?",
    re.IGNORECASE | re.ASCII,
)
_EMAIL = re.compile(
    r"(?<![A-Za-z0-9.+_-])"
    r"[A-Za-z0-9.!#$%&*+/=?^_`{|}~-]+"
    r"@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}",
    re.ASCII,
)
_PHONE = re.compile(
    r"(?<![A-Za-z0-9_+])"
    r"(?:\+?[0-9]{1,3}[ .-]?)?"
    r"(?:\([0-9]{2,4}\)[ .-]?|[0-9]{2,4}[ .-])"
    r"(?:[0-9]{2,4}[ .-])*"
    r"[0-9]{3,4}"
    r"(?![A-Za-z0-9_])",
    re.ASCII,
)


def remove_sensitive_keys(value: Any) -> Any:
    """Copy a value while removing the SDK's existing sensitive key names."""
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        try:
            return remove_sensitive_keys(model_dump(mode="json", exclude_none=True))
        except Exception:
            return repr(value)
    if isinstance(value, Mapping):
        return {
            str(k): remove_sensitive_keys(v)
            for k, v in value.items()
            if str(k).strip().lower() not in SENSITIVE_KEYS
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [remove_sensitive_keys(item) for item in value]
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return repr(value)


def _validated_categories(categories: Iterable[str]) -> tuple[str, ...]:
    selected = tuple(categories)
    unknown = [category for category in selected if category not in _CATEGORIES]
    if unknown:
        raise ValueError(f"unknown scrub category: {unknown[0]}")
    return selected


def _scrub_secret(text: str) -> str:
    text = _PEM_PRIVATE_KEY.sub("<secret>", text)

    text = _AUTHORIZATION_SCHEME.sub(
        lambda match: (
            f"{match.group(1)}{match.group(2)}{match.group(3)}"
            f"{match.group(4)}<secret>"
        ),
        text,
    )

    def replace_scheme(match: re.Match[str]) -> str:
        candidate = match.group(3)
        suspicious = (
            any(character.isdigit() for character in candidate)
            or any(character in "._~+/=-" for character in candidate)
            or any(character.isupper() for character in candidate[1:])
        )
        return (
            f"{match.group(1)}{match.group(2)}<secret>"
            if suspicious
            else match.group(0)
        )

    text = _SCHEME_CREDENTIAL.sub(replace_scheme, text)

    def replace_key_value(match: re.Match[str]) -> str:
        value = match.group(2)
        if value.lower() in {"bearer", "basic", "token", "digest"} and re.match(
            r"[ \t\r\n\f\v]+<secret>", match.string[match.end() :]
        ):
            return match.group(0)
        return f"{match.group(0)[:-len(value)]}<secret>"

    text = _KEY_VALUE_CREDENTIAL.sub(replace_key_value, text)
    return _PROVIDER_TOKEN.sub("<secret>", text)


def _replace_phone(match: re.Match[str]) -> str:
    candidate = match.group(0)
    digits = sum(character.isdigit() for character in candidate)
    if not 10 <= digits <= 15 or re.fullmatch(
        r"[0-9]{1,3}(?:\.[0-9]{1,3}){3}", candidate, re.ASCII
    ):
        return candidate
    return "<phone>"


def scrub_text(text: str, categories: Iterable[str] = DEFAULT_CATEGORIES) -> str:
    """Scrub selected pattern-based PII and credentials from text."""
    if not isinstance(text, str):
        raise TypeError("scrub_text() requires a string")
    selected = _validated_categories(categories)
    for category in selected:
        if category == "secret":
            text = _scrub_secret(text)
        elif category == "profile_url":
            text = _PROFILE_URL.sub("<profile-url>", text)
        elif category == "email":
            text = _EMAIL.sub("<email>", text)
        elif category == "phone":
            text = _PHONE.sub(_replace_phone, text)
    return text


def scrub_value(value: Any, categories: Iterable[str] = DEFAULT_CATEGORIES) -> Any:
    """Copy a nested value, scrubbing every string leaf and preserving keys."""
    selected = _validated_categories(categories)
    if isinstance(value, str):
        return scrub_text(value, selected)
    if isinstance(value, dict):
        return {key: scrub_value(item, selected) for key, item in value.items()}
    if isinstance(value, list):
        return [scrub_value(item, selected) for item in value]
    if isinstance(value, tuple):
        return [scrub_value(item, selected) for item in value]
    return value
