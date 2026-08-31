from __future__ import annotations

import json

from metergraph._capture import _usage
from metergraph._genai_attrs import MappedCall, SkipReason, map_span_attributes


def test_eligible_span_without_model_is_skipped_with_reason():
    assert (
        map_span_attributes({"gen_ai.operation.name": "chat"})
        is SkipReason.NO_MODEL
    )


def test_empty_usage_yields_usage_absent_record():
    mapped = map_span_attributes(
        {
            "gen_ai.operation.name": "chat",
            "gen_ai.provider.name": "openai",
            "gen_ai.request.model": "gpt-5-mini",
        }
    )

    assert isinstance(mapped, MappedCall)
    assert mapped.usage_absent is True
    assert mapped.response["usage"] == {"input_tokens": None, "output_tokens": None}


def test_gen_ai_span_maps_to_the_exact_exporter_shapes():
    system = json.dumps(
        [{"type": "text", "content": "Answer with synthetic data only."}]
    )
    messages = json.dumps(
        [
            {
                "role": "user",
                "parts": [{"type": "text", "content": "First synthetic input"}],
            },
            {
                "role": "assistant",
                "parts": [{"type": "text", "content": "Earlier synthetic reply"}],
            },
        ]
    )
    output = json.dumps(
        [
            {
                "role": "assistant",
                "parts": [{"type": "text", "content": "Synthetic result"}],
            }
        ]
    )
    mapped = map_span_attributes(
        {
            "gen_ai.operation.name": "chat",
            "gen_ai.provider.name": "anthropic",
            "gen_ai.request.model": "claude-opus-5",
            "gen_ai.response.model": "claude-opus-5-20260801",
            "gen_ai.system_instructions": system,
            "gen_ai.input.messages": messages,
            "gen_ai.output.messages": output,
            "gen_ai.usage.input_tokens": 41,
            "gen_ai.usage.output_tokens": 7,
            "gen_ai.response.finish_reasons": ("stop",),
        }
    )

    assert isinstance(mapped, MappedCall)
    assert mapped.provider == "anthropic"
    assert mapped.operation == "chat"
    # Golden comparison with what MetergraphGenAIExporter._export_span builds.
    assert mapped.request == {
        "model": "claude-opus-5",
        "system_instructions": system,
        "messages": messages,
    }
    assert mapped.response == {
        "model": "claude-opus-5-20260801",
        "usage": {"input_tokens": 41, "output_tokens": 7},
        "finish_reason": "stop",
        "choices": [{"finish_reason": "stop"}],
    }
    assert mapped.response_text == "Synthetic result"


def test_gen_ai_system_role_split_matches_exporter_behavior():
    messages = json.dumps(
        [
            {
                "role": "system",
                "parts": [{"type": "text", "content": "Synthetic system"}],
            },
            {
                "role": "user",
                "parts": [{"type": "text", "content": "First"}],
            },
        ]
    )
    mapped = map_span_attributes(
        {
            "gen_ai.operation.name": "chat",
            "gen_ai.system": "openai",
            "gen_ai.request.model": "gpt-5-mini",
            "gen_ai.input.messages": messages,
        }
    )

    assert isinstance(mapped, MappedCall)
    assert mapped.provider == "openai"
    assert json.loads(mapped.request["system_instructions"]) == [
        {"type": "text", "content": "Synthetic system"}
    ]
    assert [
        message["role"] for message in json.loads(mapped.request["messages"])
    ] == ["user"]
    # No response model on the span: the exporter falls back to the request
    # model in the response envelope.
    assert mapped.response["model"] == "gpt-5-mini"


def test_gen_ai_accepts_legacy_prompt_and_completion_token_spellings():
    mapped = map_span_attributes(
        {
            "gen_ai.operation.name": "chat",
            "gen_ai.provider.name": "openai",
            "gen_ai.request.model": "gpt-5-mini",
            "gen_ai.usage.prompt_tokens": 33,
            "gen_ai.usage.completion_tokens": 4,
        }
    )

    assert isinstance(mapped, MappedCall)
    assert mapped.response["usage"] == {"input_tokens": 33, "output_tokens": 4}


def test_langfuse_generation_parses_json_string_attributes():
    mapped = map_span_attributes(
        {
            "langfuse.observation.type": "generation",
            "langfuse.observation.model.name": "claude-opus-5",
            "langfuse.observation.model.parameters": json.dumps(
                {"temperature": 0.2, "max_tokens": 512}
            ),
            "langfuse.observation.usage_details": json.dumps(
                {"input": 41, "output": 7, "total": 48, "cache_read_input_tokens": 16}
            ),
            "langfuse.observation.cost_details": json.dumps(
                {"total": 0.0042, "input": 0.003, "output": 0.0012}
            ),
            "langfuse.observation.input": json.dumps(
                [{"role": "user", "content": "Synthetic input"}]
            ),
            "langfuse.observation.output": json.dumps(
                {"role": "assistant", "content": "Synthetic result"}
            ),
            "langfuse.observation.completion_start_time": "2026-08-28T00:00:00+00:00",
            "langfuse.trace.name": "synthetic-trace",
            "user.id": "synthetic-user",
            "session.id": "synthetic-session",
        }
    )

    assert isinstance(mapped, MappedCall)
    assert mapped.dialects == ("langfuse",)
    assert mapped.model == "claude-opus-5"
    assert mapped.provider is None
    # Parsed structures, not raw JSON strings, so downstream scrub/redact can
    # walk the keys.
    assert mapped.request["messages"] == [
        {"role": "user", "content": "Synthetic input"}
    ]
    assert mapped.request["parameters"] == {"temperature": 0.2, "max_tokens": 512}
    assert mapped.response_text == "Synthetic result"
    assert mapped.cost == 0.0042
    assert mapped.cost_source == "langfuse.observation.cost_details.total"
    assert mapped.session_id == "synthetic-session"
    assert mapped.trace_name == "synthetic-trace"
    assert mapped.completion_start_time == "2026-08-28T00:00:00+00:00"
    assert mapped.parse_degraded is False
    assert mapped.usage_absent is False
    assert mapped.dropped_usage_keys == ()
    usage = _usage(mapped.response)
    assert usage["input_tokens"] == 41
    assert usage["output_tokens"] == 7
    assert usage["cache_read_tokens"] == 16


def test_langfuse_malformed_json_degrades_to_metadata_only():
    mapped = map_span_attributes(
        {
            "langfuse.observation.type": "generation",
            "langfuse.observation.model.name": "claude-opus-5",
            "langfuse.observation.model.parameters": "{not json",
            "langfuse.observation.usage_details": "{broken",
            "langfuse.observation.cost_details": "[",
        }
    )

    assert isinstance(mapped, MappedCall)
    assert mapped.model == "claude-opus-5"
    assert mapped.parse_degraded is True
    assert mapped.usage_absent is True
    assert mapped.cost is None
    assert mapped.cost_source is None
    assert "parameters" not in mapped.request


def test_langfuse_bare_string_input_stays_a_string():
    mapped = map_span_attributes(
        {
            "langfuse.observation.type": "generation",
            "langfuse.observation.model.name": "claude-opus-5",
            "langfuse.observation.input": "plain synthetic prompt",
            "langfuse.observation.output": "plain synthetic answer",
        }
    )

    assert isinstance(mapped, MappedCall)
    assert mapped.parse_degraded is False
    assert mapped.request["input"] == "plain synthetic prompt"
    assert mapped.response_text == "plain synthetic answer"


def test_non_llm_kinds_are_skipped_with_reason():
    assert (
        map_span_attributes(
            {
                "langfuse.observation.type": "span",
                "langfuse.observation.model.name": "claude-opus-5",
            }
        )
        is SkipReason.INELIGIBLE_KIND
    )
    assert (
        map_span_attributes({"http.request.method": "GET"}) is SkipReason.NOT_GENAI
    )


def test_total_only_usage_yields_usage_absent_record():
    mapped = map_span_attributes(
        {
            "langfuse.observation.type": "generation",
            "langfuse.observation.model.name": "claude-opus-5",
            "langfuse.observation.usage_details": json.dumps({"total": 48}),
        }
    )

    assert isinstance(mapped, MappedCall)
    assert mapped.usage_absent is True
    assert mapped.dropped_usage_keys == ()
    usage = _usage(mapped.response)
    assert usage["input_tokens"] is None
    assert usage["output_tokens"] is None


def test_unrecognized_usage_detail_keys_are_reported_dropped():
    mapped = map_span_attributes(
        {
            "langfuse.observation.type": "generation",
            "langfuse.observation.model.name": "claude-opus-5",
            "langfuse.observation.usage_details": json.dumps(
                {"input": 10, "output": 2, "input_audio_tokens": 4, "custom_metric": 9}
            ),
        }
    )

    assert isinstance(mapped, MappedCall)
    assert mapped.usage_absent is False
    assert mapped.dropped_usage_keys == ("custom_metric", "input_audio_tokens")
    assert "custom_metric" not in mapped.response["usage"]
    assert "input_audio_tokens" not in mapped.response["usage"]


def test_langfuse_error_level_maps_to_error_message():
    mapped = map_span_attributes(
        {
            "langfuse.observation.type": "generation",
            "langfuse.observation.model.name": "claude-opus-5",
            "langfuse.observation.level": "ERROR",
            "langfuse.observation.status_message": "synthetic failure",
        }
    )

    assert isinstance(mapped, MappedCall)
    assert mapped.error_message == "synthetic failure"
