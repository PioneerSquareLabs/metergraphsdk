from __future__ import annotations

import json

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
