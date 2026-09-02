"""Cross-dialect GenAI span-attribute translation.

Turns a plain span-attribute mapping into the exact (request dict, response
dict, metadata) shapes ``MetergraphGenAIExporter._export_span`` builds, so the
capture layer re-derives usage and finish_reason the same way for every
telemetry dialect.

This module ships all four dialects Metergraph understands: the OpenTelemetry
``gen_ai.*`` semantic conventions, the Langfuse SDK, LangSmith, and
OpenInference (Arize Phoenix). When a span carries more than one dialect,
every field falls back per-field in the order ``langfuse.*`` > ``langsmith.*``
> ``gen_ai.*`` > OpenInference, which is why the merge exists before there is
anything to merge.

LangSmith is the one dialect that is not its own attribute namespace: it
writes the ``gen_ai.*`` conventions and adds a ``langsmith.*`` sidecar, so its
extractor reads the gen_ai spellings the semantic conventions dropped rather
than a vocabulary of its own.

This module is a stdlib-only leaf: it must not import ``_capture`` or
``opentelemetry``. Usage keys emitted here are deliberately spelled in the
alias vocabulary ``_capture._usage`` already accepts.
"""

from __future__ import annotations

import ast
import enum
import json
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

# The merge order is fixed here, ahead of the dialects that populate it, so
# adding one is a new extractor rather than a reshuffle of existing behavior.
DIALECT_LANGFUSE = "langfuse"
DIALECT_GENAI = "gen_ai"
DIALECT_OPENINFERENCE = "openinference"
DIALECT_LANGSMITH = "langsmith"


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

# LangChain's standard token-detail keys, as LangSmith forwards them in
# gen_ai.usage.{input,output}_token_details. "audio" has no home in the alias
# vocabulary and is dropped rather than guessed at.
_LANGSMITH_TOKEN_DETAIL_RENAMES = {
    "cache_read": "cache_read_input_tokens",
    "cache_creation": "cache_creation_input_tokens",
    "reasoning": "reasoning_tokens",
}


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


def _flattened_messages(
    attributes: Mapping[str, Any], prefix: str
) -> list[dict[str, Any]]:
    """Reassemble OpenInference index-flattened messages into dicts."""
    flat: dict[int, dict[str, Any]] = {}
    contents: dict[int, dict[int, dict[str, Any]]] = {}
    lead = prefix + "."
    for key, value in attributes.items():
        if not key.startswith(lead):
            continue
        parts = key[len(lead) :].split(".")
        if len(parts) < 3 or not parts[0].isdigit() or parts[1] != "message":
            continue
        index = int(parts[0])
        tail = parts[2:]
        if tail == ["role"] or tail == ["content"]:
            flat.setdefault(index, {})[tail[0]] = value
        elif (
            len(tail) == 4
            and tail[0] == "contents"
            and tail[1].isdigit()
            and tail[2] == "message_content"
        ):
            contents.setdefault(index, {}).setdefault(int(tail[1]), {})[
                tail[3]
            ] = value
    messages: list[dict[str, Any]] = []
    for index in sorted(set(flat) | set(contents)):
        message = dict(flat.get(index, {}))
        if "content" not in message and index in contents:
            parts_list = [contents[index][j] for j in sorted(contents[index])]
            texts = [
                part.get("text")
                for part in parts_list
                if part.get("type") == "text" and isinstance(part.get("text"), str)
            ]
            if texts and len(texts) == len(parts_list):
                message["content"] = "".join(texts)
            else:
                message["content"] = parts_list
        messages.append(message)
    return messages


def _message_text(message: Mapping[str, Any]) -> str | None:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts = [
            part.get("text")
            for part in content
            if isinstance(part, Mapping) and isinstance(part.get("text"), str)
        ]
        if texts:
            return "".join(texts)
    return None


def _extract_openinference(attributes: Mapping[str, Any]) -> _Fields:
    fields = _Fields()
    fields.model = _string(attributes.get("llm.request.model_name")) or _string(
        attributes.get("llm.model_name")
    )
    fields.response_model = _string(attributes.get("llm.response.model_name"))
    fields.provider = _string(attributes.get("llm.provider")) or _string(
        attributes.get("llm.system")
    )
    fields.finish_reason = _string(attributes.get("llm.finish_reason"))

    usage_pairs = (
        ("llm.token_count.prompt", "input_tokens"),
        ("llm.token_count.completion", "output_tokens"),
        ("llm.token_count.prompt_details.cache_read", "cache_read_input_tokens"),
        (
            "llm.token_count.prompt_details.cache_write",
            "cache_creation_input_tokens",
        ),
        ("llm.token_count.completion_details.reasoning", "reasoning_tokens"),
    )
    for attr, canonical in usage_pairs:
        value = attributes.get(attr)
        if value is not None:
            fields.usage[canonical] = value

    cost = _number(attributes.get("llm.cost.total"))
    if cost is not None:
        fields.cost = cost
        fields.cost_source = "openinference.llm.cost.total"

    messages = _flattened_messages(attributes, "llm.input_messages")
    if messages:
        fields.request["messages"] = messages
    else:
        raw_input = attributes.get("input.value")
        if isinstance(raw_input, str):
            if attributes.get("input.mime_type") == "application/json":
                decoded, ok = _load_json(raw_input)
                if ok:
                    fields.request["input"] = decoded
                else:
                    fields.request["input"] = raw_input
                    fields.parse_degraded = True
            else:
                fields.request["input"] = raw_input

    parameters = attributes.get("llm.invocation_parameters")
    if isinstance(parameters, str):
        decoded, ok = _load_json(parameters)
        if ok and isinstance(decoded, Mapping):
            fields.request["parameters"] = dict(decoded)
        else:
            fields.parse_degraded = True

    output_messages = _flattened_messages(attributes, "llm.output_messages")
    if output_messages:
        for message in output_messages:
            text = _message_text(message)
            if text is not None:
                fields.response_text = text
                break
    else:
        raw_output = attributes.get("output.value")
        if isinstance(raw_output, str):
            if attributes.get("output.mime_type") == "application/json":
                decoded, ok = _load_json(raw_output)
                if not ok:
                    fields.response_text = raw_output
                    fields.parse_degraded = True
                elif isinstance(decoded, str):
                    fields.response_text = decoded
                else:
                    fields.output_structure = decoded
            else:
                fields.response_text = raw_output
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


def _load_repr_mapping(value: Any) -> tuple[Any, bool]:
    """Decode an attribute holding a Python ``repr`` of a mapping.

    LangSmith stringifies token-detail dicts with ``str()``, not JSON, so the
    value arrives single-quoted -- ``"{'cache_read': 8}"`` -- and json.loads
    rejects it. Try JSON first anyway -- if upstream ever switches to a real
    JSON encoding this keeps working -- then fall back to literal_eval, which
    evaluates only Python literals and cannot execute code.
    """
    decoded, ok = _load_json(value)
    if ok:
        return decoded, True
    if not isinstance(value, str):
        return None, False
    try:
        return ast.literal_eval(value), True
    except (ValueError, SyntaxError, MemoryError, RecursionError):
        return None, False


def _chat_completion_text(value: Any) -> str | None:
    """Pull the assistant text out of a provider chat-completion payload.

    LangSmith stores whatever the wrapped client returned, so for the common
    wrap_openai case gen_ai.completion is the raw response body rather than a
    string. Anything else keeps its structure and lands on response["output"].
    """
    if not isinstance(value, Mapping):
        return None
    choices = value.get("choices")
    if not isinstance(choices, list) or not choices:
        return None
    message = choices[0].get("message") if isinstance(choices[0], Mapping) else None
    if isinstance(message, Mapping) and isinstance(message.get("content"), str):
        return message["content"]
    return None


def _extract_langsmith(attributes: Mapping[str, Any]) -> _Fields:
    """Read the fields LangSmith spells differently, or spells alone.

    Model, provider, response model, finish reason and the flat token counts
    are plain gen_ai.* attributes that ``_extract_genai`` already reads. What
    is left is the content -- which LangSmith writes in the pre-1.37 gen_ai
    spellings the current conventions replaced -- the token-detail breakdown,
    and the two trace fields it puts under its own prefix.
    """
    fields = _Fields()
    fields.session_id = _string(attributes.get("langsmith.trace.session_id"))
    fields.trace_name = _string(attributes.get("langsmith.trace.name"))

    for attribute in ("input_token_details", "output_token_details"):
        raw = attributes.get(f"gen_ai.usage.{attribute}")
        if raw is None:
            continue
        decoded, ok = _load_repr_mapping(raw)
        if not ok or not isinstance(decoded, Mapping):
            fields.parse_degraded = True
            continue
        dropped: list[str] = []
        for key, value in decoded.items():
            canonical = _LANGSMITH_TOKEN_DETAIL_RENAMES.get(key)
            if canonical is None:
                dropped.append(key)
            else:
                fields.usage[canonical] = value
        fields.dropped_usage_keys = tuple(
            sorted({*fields.dropped_usage_keys, *dropped})
        )

    raw_input = attributes.get("gen_ai.prompt")
    if isinstance(raw_input, str):
        decoded, ok = _load_json(raw_input)
        if not ok:
            fields.request["input"] = raw_input
            fields.parse_degraded = True
        elif isinstance(decoded, Mapping) and isinstance(decoded.get("messages"), list):
            # A wrapped chat call: the run inputs are the request kwargs, so
            # the messages list is the conversation the exporter wants.
            fields.request["messages"] = decoded["messages"]
        else:
            fields.request["input"] = decoded

    raw_output = attributes.get("gen_ai.completion")
    if isinstance(raw_output, str):
        decoded, ok = _load_json(raw_output)
        if not ok:
            fields.response_text = raw_output
            fields.parse_degraded = True
        elif isinstance(decoded, str):
            fields.response_text = decoded
        else:
            text = _chat_completion_text(decoded)
            if text is not None:
                fields.response_text = text
            else:
                fields.output_structure = decoded
    return fields


def _detected_dialects(attributes: Mapping[str, Any]) -> dict[str, bool | None]:
    """Detected dialects mapped to their verdict on whether this is an LLM call.

    ``True`` claims the span, ``False`` denies it, and ``None`` means the
    dialect is present but carries no span-kind attribute to judge by. See
    ``_vetoed`` for how the verdicts combine.
    """
    detected: dict[str, bool | None] = {}
    if any(key.startswith("langfuse.observation.") for key in attributes):
        observation_type = _string(attributes.get("langfuse.observation.type"))
        detected[DIALECT_LANGFUSE] = observation_type == "generation"
    if (
        attributes.get("gen_ai.request.model") is not None
        or attributes.get("gen_ai.operation.name") is not None
    ):
        # No opinion, not a claim. The gen_ai conventions have no span-kind
        # attribute, and producers that emit gen_ai.* for chains, tools and
        # retrievers alongside their own dialect say so there. Asserting True
        # here would let those spans outvote the dialect that knows better.
        detected[DIALECT_GENAI] = None
    if any(key.startswith("langsmith.") for key in attributes):
        # LangSmith exports EVERY run as a gen_ai span -- chains, tools and
        # retrievers included -- and records the real kind here. Without this
        # verdict a chain span that inherits a model name maps as a second
        # complete call carrying its child's tokens.
        span_kind = _string(attributes.get("langsmith.span.kind"))
        detected[DIALECT_LANGSMITH] = (
            span_kind.lower() == "llm" if span_kind is not None else None
        )
    kind = attributes.get("openinference.span.kind")
    if kind is not None:
        detected[DIALECT_OPENINFERENCE] = isinstance(kind, str) and kind.upper() == "LLM"
    return detected


def _vetoed(detected: Mapping[str, bool | None]) -> bool:
    """Whether a dialect denies this span and no other dialect claims it.

    Abstaining dialects (``None``) never decide the outcome: a span described
    only by dialects with no span-kind opinion stays eligible, which is what
    keeps a bare ``gen_ai.*`` span capturing.
    """
    verdicts = [verdict for verdict in detected.values() if verdict is not None]
    return bool(verdicts) and not any(verdicts)


_EXTRACTORS = {
    DIALECT_LANGFUSE: _extract_langfuse,
    DIALECT_LANGSMITH: _extract_langsmith,
    DIALECT_GENAI: _extract_genai,
    DIALECT_OPENINFERENCE: _extract_openinference,
}
# Field-level precedence order: langfuse > langsmith > gen_ai > OpenInference.
_PRECEDENCE = (
    DIALECT_LANGFUSE,
    DIALECT_LANGSMITH,
    DIALECT_GENAI,
    DIALECT_OPENINFERENCE,
)


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
    if _vetoed(detected):
        return SkipReason.INELIGIBLE_KIND

    contributions = [
        _EXTRACTORS[name](attributes) for name in _PRECEDENCE if name in detected
    ]
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
        dialects=tuple(name for name in _PRECEDENCE if name in detected),
        parse_degraded=any(
            contribution.parse_degraded for contribution in contributions
        ),
        usage_absent=usage_absent,
        dropped_usage_keys=tuple(sorted(dropped)),
    )


__all__ = ["MappedCall", "SkipReason", "map_span_attributes"]
