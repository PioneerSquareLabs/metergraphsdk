"""The canonical view of the tool declarations a request carried.

A provider request states its tools in one of five dialects, and MeterGraph
captured only their names. A name is not a schema: it cannot tell an analysis
what the model was allowed to ask for, so a tool-using call arrived
unreplayable. This module reads every declaration a request carries and records
it once, in one shape, preserving order and the declared schema exactly.

What it will not do is guess. Where a declaration is unreadable, incomplete,
duplicated, or where two schema keys disagree, the record says so rather than
inventing a schema, and the envelope's `scope` says whether the declarations
came from one container or several, because nothing in the capture reveals the
precedence a provider applies across competing containers.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ._template import Unrepresentable, json_equal, json_value_strict

VERSION = 1

# Read in this order, so a request carrying tools in more than one place
# produces the same records every time.
_CONTAINERS = (
    ("tools", ("tools",)),
    ("config.tools", ("config", "tools")),
    ("extra_body.tools", ("extra_body", "tools")),
)

# Precedence within one Gemini declaration. Listed once, applied everywhere.
_SCHEMA_KEYS = ("parameters_json_schema", "parametersJsonSchema", "parameters")

_GEMINI_NATIVE_TOOLS = frozenset(
    {"google_search", "google_search_retrieval", "code_execution", "url_context"}
)

# A provider-native tool declares a type, not a schema, so its dialect is only
# known where the captured provider is itself unambiguous. OpenAI stays unknown:
# `{"type": "web_search_preview"}` is the same entry on Chat and on Responses.
_PROVIDER_DIALECTS = {"anthropic": "anthropic", "google": "gemini"}

_IDENTITY_MAX_BYTES = 512
_INDEX_MAX = 10_000

_RECORD_KEYS = (
    "index",
    "container",
    "kind",
    "dialect",
    "name",
    "description",
    "schema_key",
    "schema",
    "status",
)
_OPTIONAL_RECORD_KEYS = ("provider_type", "duplicate_of")
_KINDS = frozenset({"function", "provider", "unknown"})
_DIALECTS = frozenset(
    {"anthropic", "openai_chat", "openai_responses", "gemini", "ai_sdk", "unknown"}
)
_STATUSES = frozenset(
    {"declared", "incomplete", "malformed", "unsupported", "provider_tool", "ambiguous"}
)
_CONTAINER_NAMES = frozenset(name for name, _ in _CONTAINERS)
_SCHEMA_KEY_NAMES = frozenset(_SCHEMA_KEYS) | {"input_schema", "inputSchema"}


def _attribute(value: Any, name: str) -> Any:
    if isinstance(value, Mapping):
        return value.get(name)
    return getattr(value, name, None)


def _has(value: Any, name: str) -> bool:
    if isinstance(value, Mapping):
        return name in value
    return hasattr(value, name) and getattr(value, name) is not None


def _text(value: Any) -> str | None:
    """A declared identity string, or None when it is absent or unusable."""
    if not isinstance(value, str):
        return None
    return value if len(value.encode("utf-8")) <= _IDENTITY_MAX_BYTES else None


def _record(
    index: int,
    container: str,
    *,
    kind: str,
    dialect: str,
    status: str,
    name: Any = None,
    description: Any = None,
    schema_key: str | None = None,
    schema: Any = None,
    provider_type: Any = None,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "index": index,
        "container": container,
        "kind": kind,
        "dialect": dialect,
        "name": _text(name),
        "description": description if isinstance(description, str) else None,
        "schema_key": schema_key,
        "schema": schema,
        "status": status,
    }
    if provider_type is not None:
        resolved = _text(provider_type)
        if resolved is not None:
            record["provider_type"] = resolved
    return record


def _schema_sources(entry: Any, keys: tuple[str, ...]) -> list[tuple[str, Any]]:
    found = []
    for key in keys:
        if _has(entry, key):
            found.append((key, _attribute(entry, key)))
    return found


def _function_record(
    index: int,
    container: str,
    *,
    dialect: str,
    entry: Any,
    name_source: Any,
    schema_keys: tuple[str, ...],
    description_source: Any,
) -> dict[str, Any]:
    """One caller-authored function declaration, read without normalization."""
    name = _attribute(name_source, "name")
    description = _attribute(description_source, "description")
    sources = _schema_sources(entry, schema_keys)
    converted: list[tuple[str, Any]] = []
    for key, value in sources:
        try:
            converted.append((key, json_value_strict(value)))
        except Unrepresentable:
            # A schema the request serializer cannot hold is not a schema this
            # view can carry. `request_json` still shows whatever was sent.
            return _record(
                index,
                container,
                kind="function",
                dialect=dialect,
                status="unsupported",
                name=name,
                description=description,
            )
    if len(converted) > 1:
        first = converted[0][1]
        if any(not json_equal(value, first) for _, value in converted[1:]):
            # Which key the provider honors is not knowable from the capture,
            # so neither is the effective schema.
            return _record(
                index,
                container,
                kind="function",
                dialect=dialect,
                status="ambiguous",
                name=name,
                description=description,
            )
    schema_key, schema = converted[0] if converted else (None, None)
    name_malformed = name is not None and _text(name) is None
    schema_malformed = bool(converted) and not isinstance(schema, Mapping)
    if name_malformed or schema_malformed:
        return _record(
            index,
            container,
            kind="function",
            dialect=dialect,
            status="malformed",
            name=name if not name_malformed else None,
            description=description,
            schema_key=None if schema_malformed else schema_key,
            schema=None if schema_malformed else schema,
        )
    if name is None or not converted:
        return _record(
            index,
            container,
            kind="function",
            dialect=dialect,
            status="incomplete",
            name=name,
            description=description,
            schema_key=schema_key,
            schema=schema,
        )
    return _record(
        index,
        container,
        kind="function",
        dialect=dialect,
        status="declared",
        name=name,
        description=description,
        schema_key=schema_key,
        schema=schema,
    )


def _entry_records(
    index: int, container: str, entry: Any, provider: str | None = None
) -> list[dict[str, Any]]:
    """Every record one container entry produces, in declaration order."""
    if not isinstance(entry, Mapping) and not hasattr(entry, "__dict__"):
        return [
            _record(index, container, kind="unknown", dialect="unknown", status="unsupported")
        ]
    declarations = _attribute(entry, "function_declarations")
    if declarations is None:
        declarations = _attribute(entry, "functionDeclarations")
    if isinstance(declarations, list):
        records = []
        for position, declaration in enumerate(declarations):
            records.append(
                _function_record(
                    index + position,
                    container,
                    dialect="gemini",
                    entry=declaration,
                    name_source=declaration,
                    schema_keys=_SCHEMA_KEYS,
                    description_source=declaration,
                )
            )
        return records or [
            _record(index, container, kind="unknown", dialect="unknown", status="unsupported")
        ]
    function = _attribute(entry, "function")
    if isinstance(function, Mapping) or (
        function is not None and hasattr(function, "name")
    ):
        return [
            _function_record(
                index,
                container,
                dialect="openai_chat",
                entry=function,
                name_source=function,
                schema_keys=("parameters",),
                description_source=function,
            )
        ]
    if _has(entry, "input_schema"):
        return [
            _function_record(
                index,
                container,
                dialect="anthropic",
                entry=entry,
                name_source=entry,
                schema_keys=("input_schema",),
                description_source=entry,
            )
        ]
    if _has(entry, "inputSchema"):
        return [
            _function_record(
                index,
                container,
                dialect="ai_sdk",
                entry=entry,
                name_source=entry,
                schema_keys=("inputSchema",),
                description_source=entry,
            )
        ]
    declared_type = _attribute(entry, "type")
    if declared_type == "function":
        # A function stays a function even with no name: it is incomplete, not
        # a provider-native tool.
        return [
            _function_record(
                index,
                container,
                dialect="openai_responses",
                entry=entry,
                name_source=entry,
                schema_keys=("parameters",),
                description_source=entry,
            )
        ]
    if isinstance(declared_type, str):
        return [
            _record(
                index,
                container,
                kind="provider",
                dialect=_PROVIDER_DIALECTS.get(provider or "", "unknown"),
                status="provider_tool",
                name=_attribute(entry, "name"),
                provider_type=declared_type,
            )
        ]
    if isinstance(entry, Mapping) and len(entry) == 1:
        (key, value), = entry.items()
        if key in _GEMINI_NATIVE_TOOLS and isinstance(value, Mapping):
            return [
                _record(
                    index,
                    container,
                    kind="provider",
                    dialect="gemini",
                    status="provider_tool",
                    name=key,
                )
            ]
    return [
        _record(index, container, kind="unknown", dialect="unknown", status="unsupported")
    ]


def _container_value(request: Mapping[str, Any], path: tuple[str, ...]) -> tuple[bool, Any]:
    value: Any = request
    for name in path:
        if value is None:
            return False, None
        if not _has(value, name):
            return False, None
        value = _attribute(value, name)
    return True, value


def declarations(
    request: Mapping[str, Any], provider: str | None = None
) -> tuple[list[dict[str, Any]], str] | None:
    """The declarations a request carries, and whether one container supplied them.

    Returns None when the request declares no tools at all, so an absent field
    and an empty declaration list are never confused.
    """
    records: list[dict[str, Any]] = []
    contributors = 0
    for container, path in _CONTAINERS:
        present, value = _container_value(request, path)
        if not present:
            continue
        before = len(records)
        if not isinstance(value, list):
            # A tools key that is not a list still happened. Saying so is not
            # the same as saying no tools were declared.
            records.append(
                _record(
                    len(records),
                    container,
                    kind="unknown",
                    dialect="unknown",
                    status="unsupported",
                )
            )
        else:
            for entry in value:
                records.extend(
                    _entry_records(len(records), container, entry, provider)
                )
        if len(records) > before:
            contributors += 1
    if not records or len(records) > _INDEX_MAX:
        return None
    seen: dict[str, int] = {}
    for record in records:
        name = record["name"]
        if name is None:
            continue
        if name in seen:
            record["duplicate_of"] = seen[name]
        else:
            seen[name] = record["index"]
    return records, ("effective" if contributors <= 1 else "inventory")


def valid_declarations(value: Any) -> bool:
    """Whether a declarations array still matches the contract.

    Applied to the redaction hook's output, which is caller-supplied and may
    return anything at all.
    """
    if not isinstance(value, list) or not value:
        return False
    for record in value:
        if not isinstance(record, Mapping):
            return False
        keys = set(record)
        if not set(_RECORD_KEYS) <= keys:
            return False
        if keys - set(_RECORD_KEYS) - set(_OPTIONAL_RECORD_KEYS):
            return False
        index = record["index"]
        if not isinstance(index, int) or isinstance(index, bool) or not 0 <= index < _INDEX_MAX:
            return False
        duplicate = record.get("duplicate_of")
        if "duplicate_of" in record and (
            not isinstance(duplicate, int)
            or isinstance(duplicate, bool)
            or not 0 <= duplicate < _INDEX_MAX
        ):
            return False
        if record["kind"] not in _KINDS or record["dialect"] not in _DIALECTS:
            return False
        if record["status"] not in _STATUSES:
            return False
        if record["container"] not in _CONTAINER_NAMES:
            return False
        schema_key = record["schema_key"]
        if schema_key is not None and schema_key not in _SCHEMA_KEY_NAMES:
            return False
        for key in ("name", "provider_type"):
            text = record.get(key)
            if text is not None and _text(text) is None:
                return False
        description = record["description"]
        if description is not None and not isinstance(description, str):
            return False
        schema = record["schema"]
        if schema is not None and not isinstance(schema, Mapping):
            return False
    return True


def envelope(records: list[dict[str, Any]], scope: str, fidelity: str) -> dict[str, Any]:
    return {
        "version": VERSION,
        "fidelity": fidelity,
        "scope": scope,
        "declarations": records,
    }
