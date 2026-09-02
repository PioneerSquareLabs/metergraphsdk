"""Cross-dialect GenAI span-attribute translation.

Turns a plain span-attribute mapping into the request/response shapes the
capture layer expects, so usage and finish_reason are re-derived the same way
for every telemetry dialect: a detection pass, an extractor per dialect, and a
per-field merge in the order ``langfuse.*`` > ``gen_ai.*`` > OpenInference.
``gen_ai.*`` and ``langfuse.*`` have extractors today.

This module is a stdlib-only leaf: it must not import ``_capture`` or
``opentelemetry``. Usage keys emitted here are deliberately spelled in the
alias vocabulary ``_capture._usage`` already accepts.
"""

from __future__ import annotations

import enum
import json
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

DIALECT_LANGFUSE = "langfuse"
DIALECT_GENAI = "gen_ai"
DIALECT_OPENINFERENCE = "openinference"


class SkipReason(enum.Enum):
    """Why a span produced no mapped call."""

    NOT_GENAI = "not-genai"
    INELIGIBLE_KIND = "ineligible-kind"
    NO_MODEL = "no-model"


@dataclass(frozen=True)
class MappedCall:
    """Normalized shapes for one LLM call, ready for the capture layer."""

    provider: str | None
    model: str
    operation: str
    request: dict[str, Any]
    response: dict[str, Any]
    response_text: str | None
    cost: float | None
    cost_source: str | None
    session_id: str | None
    trace_name: str | None
    completion_start_time: str | None
    error_message: str | None
    # Only the dialects that marked this span an LLM call, in precedence
    # order -- a dialect present but *vetoing* is excluded. These are the
    # dialects whose extractors actually ran, so dialects[0] is what the
    # exporter falls back to when no vendor attribute is present. Leaving a
    # vetoing dialect in relabels a call it explicitly disclaimed: the same
    # gen_ai span reports "gen_ai" on its own and "langfuse" as soon as a
    # Langfuse type="span" workflow observation happens to wrap it.
    dialects: tuple[str, ...]
    parse_degraded: bool
    usage_absent: bool
    dropped_usage_keys: tuple[str, ...]


# Top-level usage keys _capture._usage understands, verbatim.
_USAGE_TOP_KEYS = frozenset(
    {
        "prompt_tokens",
        "input_tokens",
        "prompt_token_count",
        "promptTokenCount",
        "completion_tokens",
        "output_tokens",
        "candidates_token_count",
        "candidatesTokenCount",
        "cache_read_input_tokens",
        "cached_content_token_count",
        "cachedContentTokenCount",
        "cache_creation_input_tokens",
        "cacheCreationInputTokens",
        "thoughts_token_count",
        "thoughtsTokenCount",
    }
)

# Langfuse usage_details spellings that translate onto that vocabulary.
_LANGFUSE_USAGE_RENAMES = {"input": "input_tokens", "output": "output_tokens"}


def map_usage_details(
    decoded: Mapping[str, Any],
) -> tuple[dict[str, Any], tuple[str, ...]]:
    """Map Langfuse usage-details keys onto the ``_usage`` alias vocabulary.

    Returns ``(usage, dropped_keys)``: the renamed keys the capture layer
    understands, plus the sorted source keys that did not translate. The
    derived ``"total"`` key is skipped silently (it is neither mapped nor
    reported as dropped).
    """
    usage: dict[str, Any] = {}
    dropped: list[str] = []
    for key, value in decoded.items():
        if key == "total":
            continue
        canonical = _LANGFUSE_USAGE_RENAMES.get(key, key)
        if canonical in _USAGE_TOP_KEYS:
            usage[canonical] = value
        else:
            dropped.append(key)
    return usage, tuple(sorted(dropped))


def _string(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _load_json(value: Any) -> tuple[Any, bool]:
    """Decode a JSON-string attribute. Returns (parsed, ok)."""
    if not isinstance(value, str):
        return None, False
    try:
        return json.loads(value), True
    except (TypeError, ValueError):
        return None, False


def _first_string(value: Any) -> str | None:
    if isinstance(value, str):
        decoded, ok = _load_json(value)
        if ok and isinstance(decoded, list) and decoded:
            return str(decoded[0])
        return value
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return str(value[0]) if value else None
    return None


def _text_from_output_messages(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    messages, ok = _load_json(value)
    if not ok or not isinstance(messages, list):
        return None
    for message in messages:
        if not isinstance(message, Mapping):
            continue
        parts = message.get("parts")
        if not isinstance(parts, list):
            continue
        texts: list[str] = []
        for part in parts:
            if (
                isinstance(part, Mapping)
                and part.get("type") == "text"
                and isinstance(part.get("content"), str)
            ):
                texts.append(part["content"])
        if texts:
            return "".join(texts)
    return None


def _genai_request_content(
    attributes: Mapping[str, Any],
) -> tuple[str | None, str | None]:
    """Split gen_ai request content into (system_instructions, messages)."""
    system = attributes.get("gen_ai.system_instructions")
    messages = attributes.get("gen_ai.input.messages")
    if not isinstance(messages, str):
        return system if isinstance(system, str) else None, None
    if isinstance(system, str):
        return system, messages
    decoded, ok = _load_json(messages)
    if not ok or not isinstance(decoded, list):
        return None, messages

    system_parts: list[Any] = []
    conversation: list[Any] = []
    for message in decoded:
        if isinstance(message, Mapping) and message.get("role") == "system":
            parts = message.get("parts")
            if isinstance(parts, list):
                system_parts.extend(parts)
            continue
        conversation.append(message)
    if not system_parts:
        return None, messages
    return (
        json.dumps(system_parts, separators=(",", ":"), ensure_ascii=False),
        json.dumps(conversation, separators=(",", ":"), ensure_ascii=False),
    )


@dataclass
class _Fields:
    """One dialect's contribution, before the per-field precedence merge."""

    model: str | None = None
    response_model: str | None = None
    provider: str | None = None
    operation: str | None = None
    request: dict[str, Any] = field(default_factory=dict)
    usage: dict[str, Any] = field(default_factory=dict)
    finish_reason: str | None = None
    response_text: str | None = None
    output_structure: Any = None
    cost: float | None = None
    cost_source: str | None = None
    session_id: str | None = None
    trace_name: str | None = None
    completion_start_time: str | None = None
    error_message: str | None = None
    parse_degraded: bool = False
    dropped_usage_keys: tuple[str, ...] = ()


def _extract_genai(attributes: Mapping[str, Any]) -> _Fields:
    fields = _Fields()
    fields.model = _string(attributes.get("gen_ai.request.model"))
    fields.provider = _string(attributes.get("gen_ai.provider.name")) or _string(
        attributes.get("gen_ai.system")
    )
    fields.operation = _string(attributes.get("gen_ai.operation.name"))
    fields.response_model = _string(attributes.get("gen_ai.response.model"))
    system, messages = _genai_request_content(attributes)
    if system is not None:
        fields.request["system_instructions"] = system
    if messages is not None:
        fields.request["messages"] = messages
    input_tokens = attributes.get("gen_ai.usage.input_tokens")
    if input_tokens is None:
        input_tokens = attributes.get("gen_ai.usage.prompt_tokens")
    output_tokens = attributes.get("gen_ai.usage.output_tokens")
    if output_tokens is None:
        output_tokens = attributes.get("gen_ai.usage.completion_tokens")
    if input_tokens is not None:
        fields.usage["input_tokens"] = input_tokens
    if output_tokens is not None:
        fields.usage["output_tokens"] = output_tokens
    fields.finish_reason = _first_string(
        attributes.get("gen_ai.response.finish_reasons")
    )
    fields.response_text = _text_from_output_messages(
        attributes.get("gen_ai.output.messages")
    )
    return fields


def _extract_langfuse(attributes: Mapping[str, Any]) -> _Fields:
    fields = _Fields()
    fields.model = _string(attributes.get("langfuse.observation.model.name"))
    # Session is one of the trace-level fields the Langfuse SDK spells WITHOUT
    # its own prefix: both v3 and v4 define TRACE_SESSION_ID = "session.id"
    # and propagate it through baggage. There has never been a
    # langfuse.session.id, so do not "helpfully" add one.
    fields.session_id = _string(attributes.get("session.id"))
    fields.trace_name = _string(attributes.get("langfuse.trace.name"))
    completion_start = attributes.get("langfuse.observation.completion_start_time")
    if isinstance(completion_start, str):
        # Langfuse JSON-encodes this timestamp, so the attribute value arrives
        # wrapped in literal quote characters. Decode before it reaches the
        # exporter's ISO parser, which rejects the quoted form and would drop
        # time-to-first-token for every Langfuse span without a word.
        decoded, ok = _load_json(completion_start)
        fields.completion_start_time = (
            decoded if ok and isinstance(decoded, str) else completion_start
        )
    level = _string(attributes.get("langfuse.observation.level"))
    if level is not None and level.upper() == "ERROR":
        fields.error_message = (
            _string(attributes.get("langfuse.observation.status_message"))
            or "Langfuse observation reported an error"
        )

    parameters = attributes.get("langfuse.observation.model.parameters")
    if isinstance(parameters, str):
        decoded, ok = _load_json(parameters)
        if ok and isinstance(decoded, Mapping):
            fields.request["parameters"] = dict(decoded)
        else:
            fields.parse_degraded = True

    usage_details = attributes.get("langfuse.observation.usage_details")
    if isinstance(usage_details, str):
        decoded, ok = _load_json(usage_details)
        if ok and isinstance(decoded, Mapping):
            mapped_usage, fields.dropped_usage_keys = map_usage_details(decoded)
            fields.usage.update(mapped_usage)
        else:
            fields.parse_degraded = True

    cost_details = attributes.get("langfuse.observation.cost_details")
    if isinstance(cost_details, str):
        decoded, ok = _load_json(cost_details)
        if ok and isinstance(decoded, Mapping):
            total = _number(decoded.get("total"))
            if total is not None:
                fields.cost = total
                fields.cost_source = "langfuse.observation.cost_details.total"
        else:
            fields.parse_degraded = True

    raw_input = attributes.get("langfuse.observation.input")
    if isinstance(raw_input, str):
        decoded, ok = _load_json(raw_input)
        value = decoded if ok else raw_input
        if isinstance(value, list):
            fields.request["messages"] = value
        else:
            fields.request["input"] = value

    raw_output = attributes.get("langfuse.observation.output")
    if isinstance(raw_output, str):
        decoded, ok = _load_json(raw_output)
        value = decoded if ok else raw_output
        if isinstance(value, str):
            fields.response_text = value
        elif isinstance(value, Mapping) and isinstance(value.get("content"), str):
            fields.response_text = value["content"]
        elif isinstance(value, Mapping) and isinstance(value.get("text"), str):
            fields.response_text = value["text"]
        else:
            fields.output_structure = value
    return fields


def _detected_dialects(attributes: Mapping[str, Any]) -> dict[str, bool]:
    """Detected dialects mapped to whether they mark the span as an LLM call."""
    detected: dict[str, bool] = {}
    if any(key.startswith("langfuse.observation.") for key in attributes):
        observation_type = _string(attributes.get("langfuse.observation.type"))
        detected[DIALECT_LANGFUSE] = observation_type == "generation"
    if (
        attributes.get("gen_ai.request.model") is not None
        or attributes.get("gen_ai.operation.name") is not None
    ):
        # The value says whether the dialect marks this span as an LLM call.
        detected[DIALECT_GENAI] = True
    return detected


_EXTRACTORS = {
    DIALECT_LANGFUSE: _extract_langfuse,
    DIALECT_GENAI: _extract_genai,
}
_PRECEDENCE = (DIALECT_LANGFUSE, DIALECT_GENAI, DIALECT_OPENINFERENCE)


def _first_value(contributions: list[_Fields], name: str) -> Any:
    for contribution in contributions:
        value = getattr(contribution, name)
        if value is not None:
            return value
    return None


def map_span_attributes(
    attributes: Mapping[str, Any],
) -> MappedCall | SkipReason:
    """Map one span's attributes to capture shapes, or say why not."""
    detected = _detected_dialects(attributes)
    if not detected:
        return SkipReason.NOT_GENAI
    eligible = [name for name in _PRECEDENCE if detected.get(name)]
    if not eligible:
        return SkipReason.INELIGIBLE_KIND

    contributions = [_EXTRACTORS[name](attributes) for name in eligible]
    model = _first_value(contributions, "model")
    if model is None:
        return SkipReason.NO_MODEL

    request: dict[str, Any] = {"model": model}
    usage: dict[str, Any] = {}
    for contribution in contributions:
        for key, value in contribution.request.items():
            request.setdefault(key, value)
        for key, value in contribution.usage.items():
            usage.setdefault(key, value)

    usage_absent = not any(value is not None for value in usage.values())
    reasoning_tokens = usage.pop("reasoning_tokens", None)
    final_usage: dict[str, Any] = {
        "input_tokens": usage.pop("input_tokens", None),
        "output_tokens": usage.pop("output_tokens", None),
    }
    final_usage.update(usage)
    if reasoning_tokens is not None:
        final_usage["completion_tokens_details"] = {
            "reasoning_tokens": reasoning_tokens
        }

    finish_reason = _first_value(contributions, "finish_reason")
    response: dict[str, Any] = {
        "model": _first_value(contributions, "response_model") or model,
        "usage": final_usage,
        "finish_reason": finish_reason,
        "choices": (
            [{"finish_reason": finish_reason}] if finish_reason is not None else []
        ),
    }
    response_text = _first_value(contributions, "response_text")
    output_structure = _first_value(contributions, "output_structure")
    if output_structure is not None and response_text is None:
        response["output"] = output_structure

    priced = next((c for c in contributions if c.cost is not None), None)

    dropped: set[str] = set()
    for contribution in contributions:
        dropped.update(contribution.dropped_usage_keys)

    return MappedCall(
        provider=_first_value(contributions, "provider"),
        model=model,
        operation=_first_value(contributions, "operation") or "inference",
        request=request,
        response=response,
        response_text=response_text,
        cost=priced.cost if priced is not None else None,
        cost_source=priced.cost_source if priced is not None else None,
        session_id=_first_value(contributions, "session_id"),
        trace_name=_first_value(contributions, "trace_name"),
        completion_start_time=_first_value(contributions, "completion_start_time"),
        error_message=_first_value(contributions, "error_message"),
        dialects=tuple(eligible),
        parse_degraded=any(
            contribution.parse_degraded for contribution in contributions
        ),
        usage_absent=usage_absent,
        dropped_usage_keys=tuple(sorted(dropped)),
    )


__all__ = ["MappedCall", "SkipReason", "map_span_attributes"]
