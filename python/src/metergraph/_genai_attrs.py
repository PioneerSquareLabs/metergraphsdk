"""Cross-dialect GenAI span-attribute translation.

Turns a plain span-attribute mapping into the exact (request dict, response
dict, metadata) shapes ``MetergraphGenAIExporter._export_span`` builds, so the
capture layer re-derives usage and finish_reason the same way for every
telemetry dialect.

This module ships the dialect framework and the OpenTelemetry ``gen_ai.*``
semantic conventions — the vocabulary the exporter already understood, moved
here unchanged. The OpenInference (Arize Phoenix) and Langfuse dialects are
added by follow-up changes; each contributes an extractor and the fields it
alone populates. When a span carries more than one dialect, every field falls
back per-field in the order ``langfuse.*`` > ``gen_ai.*`` > OpenInference,
which is why the merge exists before there is anything to merge.

This module is a stdlib-only leaf: it must not import ``_capture`` or
``opentelemetry``. Usage keys emitted here are deliberately spelled in the
alias vocabulary ``_capture._usage`` already accepts.
"""

from __future__ import annotations

import enum
import json
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

# The merge order is fixed here, ahead of the dialects that populate it, so
# adding one is a new extractor rather than a reshuffle of existing behavior.
DIALECT_LANGFUSE = "langfuse"
DIALECT_GENAI = "gen_ai"
DIALECT_OPENINFERENCE = "openinference"


class SkipReason(enum.Enum):
    """Why a span produced no mapped call."""

    NOT_GENAI = "not-genai"
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
    dialects: tuple[str, ...]
    usage_absent: bool


# Top-level usage keys _capture._usage understands, verbatim.
def _string(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


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
    """Split gen_ai request content exactly like the exporter does today."""
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


def _detected_dialects(attributes: Mapping[str, Any]) -> dict[str, bool]:
    """Detected dialects mapped to whether they mark the span as an LLM call."""
    detected: dict[str, bool] = {}
    if (
        attributes.get("gen_ai.request.model") is not None
        or attributes.get("gen_ai.operation.name") is not None
    ):
        # A gen_ai span is always an LLM call; dialects that also describe
        # non-LLM work (chains, tools, retrievers) report False here instead.
        detected[DIALECT_GENAI] = True
    return detected


_EXTRACTORS = {
    DIALECT_GENAI: _extract_genai,
}
# Field-level precedence order: langfuse > gen_ai > OpenInference.
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

    return MappedCall(
        provider=_first_value(contributions, "provider"),
        model=model,
        operation=_first_value(contributions, "operation") or "inference",
        request=request,
        response=response,
        response_text=response_text,
        dialects=tuple(name for name in _PRECEDENCE if name in detected),
        usage_absent=usage_absent,
    )


__all__ = ["MappedCall", "SkipReason", "map_span_attributes"]
