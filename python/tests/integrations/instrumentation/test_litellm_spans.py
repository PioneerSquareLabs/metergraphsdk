"""LiteLLM call spans: request id, tools, tool calls and the caller's call name.

The attribute fixtures follow spans LiteLLM 1.84.10 and 1.103.0 exported to
MetergraphGenAIExporter against loopback fake providers. Content is synthetic.
"""

from __future__ import annotations

import json

import metergraph
import pytest
from metergraph import _capture
from metergraph._capture import Options, Runtime
from metergraph._genai_attrs import MappedCall, map_span_attributes
from metergraph.opentelemetry import MetergraphGenAIExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.util.instrumentation import InstrumentationScope
from opentelemetry.trace import SpanContext, Status, StatusCode, TraceFlags

RESPONSE_ID = "chatcmpl-fake-45a636ef3a5d42d4a7d093f8e3e62528"
PARAMETERS = {
    "type": "object",
    "properties": {"city": {"type": "string"}},
    "required": ["city"],
}


def _common(version: str) -> dict:
    attributes = {
        "gen_ai.operation.name": "chat",
        "gen_ai.request.model": "gpt-5.4-mini",
        "gen_ai.response.model": "gpt-5.4-mini",
        "gen_ai.response.id": RESPONSE_ID,
        "gen_ai.input.messages": json.dumps(
            [{"role": "user", "parts": [{"type": "text", "content": "Weather?"}]}]
        ),
        "gen_ai.usage.input_tokens": 120,
        "gen_ai.usage.output_tokens": 30,
        "gen_ai.usage.total_tokens": 150,
        "litellm.call_id": "99e0f0e7-526b-4fe4-b02a-d55aa25cec6c",
        "llm.request.type": "completion",
        "metadata.user_api_key_hash": "",
    }
    if version == "1.84.10":
        attributes.update({"gen_ai.system": "openai", "llm.is_streaming": False})
    elif version == "1.103.0":
        attributes.update(
            {
                "gen_ai.system": "openai",
                "llm.is_streaming": False,
                "litellm.provider.model": "openai/gpt-5.4-mini",
                "gen_ai.cost.total_cost": 0.000225,
            }
        )
    else:  # 1.103.0 with OTEL_SEMCONV_STABILITY_OPT_IN=gen_ai_latest_experimental
        attributes.update(
            {
                "gen_ai.provider.name": "openai",
                "litellm.provider.model": "openai/gpt-5.4-mini",
                "gen_ai.cost.total_cost": 0.000225,
            }
        )
    return attributes


def _tool_call_span(version: str) -> dict:
    """A call that declared one tool and got a tool call back."""
    return {
        **_common(version),
        "gen_ai.output.messages": json.dumps(
            [{"role": "assistant", "parts": [], "finish_reason": "tool_calls"}]
        ),
        "gen_ai.response.finish_reasons": '["tool_calls"]',
        "llm.request.functions.0.name": "lookup_weather",
        "llm.request.functions.0.description": "Weather for a city.",
        "llm.request.functions.0.parameters": json.dumps(PARAMETERS),
        "gen_ai.completion.0.function_call.name": "lookup_weather",
        "gen_ai.completion.0.function_call.arguments": '{"city": "Springfield"}',
    }


def _text_span(version: str) -> dict:
    return {
        **_common(version),
        "gen_ai.output.messages": json.dumps(
            [
                {
                    "role": "assistant",
                    "parts": [{"type": "text", "content": "Synthetic answer."}],
                    "finish_reason": "stop",
                }
            ]
        ),
        "gen_ai.response.finish_reasons": '["stop"]',
    }


VERSIONS = ("1.84.10", "1.103.0", "1.103.0-semconv")


def _span(
    attributes: dict, *, name: str = "litellm_request", scope: str = "litellm"
) -> ReadableSpan:
    return ReadableSpan(
        name=name,
        context=SpanContext(
            trace_id=int("12" * 16, 16),
            span_id=int("34" * 8, 16),
            is_remote=False,
            trace_flags=TraceFlags(TraceFlags.SAMPLED),
        ),
        resource=Resource.create({"service.name": "synthetic-litellm-app"}),
        attributes=attributes,
        instrumentation_scope=InstrumentationScope(scope),
        status=Status(StatusCode.OK),
        start_time=1_786_496_400_000_000_000,
        end_time=1_786_496_400_125_000_000,
    )


@pytest.fixture
def rows(monkeypatch):
    class Rows:
        def __init__(self) -> None:
            self.rows: list[dict] = []

        def enqueue(self, row: dict) -> bool:
            self.rows.append(row)
            return True

    collected = Rows()
    _capture.set_runtime(Runtime(collected, Options(app_root="")))
    monkeypatch.setattr(metergraph, "init", lambda: None)
    yield collected.rows
    _capture.set_runtime(None)


def _export(*spans: ReadableSpan) -> None:
    MetergraphGenAIExporter().export(list(spans))


@pytest.mark.parametrize("version", VERSIONS)
def test_response_id_becomes_the_request_id(rows, version):
    _export(_span(_text_span(version)))

    assert rows[0]["request_id"] == RESPONSE_ID
    assert json.loads(rows[0]["response_text"])["request_id"] == RESPONSE_ID


@pytest.mark.parametrize("version", VERSIONS)
def test_request_functions_become_request_tools(rows, version):
    _export(_span(_tool_call_span(version)))

    request = json.loads(rows[0]["request_json"])
    assert request["tools"] == [
        {
            "type": "function",
            "function": {
                "name": "lookup_weather",
                "description": "Weather for a city.",
                "parameters": PARAMETERS,
            },
        }
    ]
    [declaration] = rows[0]["tool_definitions"]["declarations"]
    assert declaration["name"] == "lookup_weather"
    assert declaration["status"] == "declared"
    assert declaration["schema"] == PARAMETERS


@pytest.mark.parametrize("version", VERSIONS)
def test_completion_function_call_becomes_a_tool_call(rows, version):
    _export(_span(_tool_call_span(version)))

    row = rows[0]
    assert row["tool_names"] == ["lookup_weather"]
    [call] = row["tool_calls"]
    assert call["call_id"] == f"{RESPONSE_ID}-0"
    assert call["name"] == "lookup_weather"
    assert call["arguments"] == {"city": "Springfield"}
    assert row["finish_reason"] == "tool-calls"


def test_completion_tool_calls_keep_their_ids():
    attributes = {
        **_common("1.103.0"),
        "gen_ai.completion.0.tool_calls.0.id": "call_a",
        "gen_ai.completion.0.tool_calls.0.name": "lookup_weather",
        "gen_ai.completion.0.tool_calls.0.arguments": '{"city": "A"}',
        "gen_ai.completion.0.tool_calls.1.name": "lookup_weather",
        "gen_ai.completion.0.tool_calls.1.arguments": '{"city": "B"}',
    }

    mapped = map_span_attributes(attributes)

    assert isinstance(mapped, MappedCall)
    calls = mapped.response["choices"][0]["message"]["tool_calls"]
    assert [call["id"] for call in calls] == ["call_a", f"{RESPONSE_ID}-0-1"]


def test_standard_tool_attributes_win_over_the_legacy_ones():
    attributes = {
        **_tool_call_span("1.103.0"),
        "gen_ai.tool.definitions": json.dumps(
            [{"type": "function", "name": "lookup_weather"}]
        ),
        "gen_ai.output.messages": json.dumps(
            [
                {
                    "role": "assistant",
                    "parts": [
                        {
                            "type": "tool_call",
                            "id": "call_1",
                            "name": "lookup_weather",
                            "arguments": {"city": "Springfield"},
                        }
                    ],
                }
            ]
        ),
    }

    mapped = map_span_attributes(attributes)

    assert isinstance(mapped, MappedCall)
    assert "tools" not in mapped.request
    # The standard part wins and keeps its real call id.
    (call,) = mapped.response["choices"][0]["message"]["tool_calls"]
    assert call["id"] == "call_1"
    assert call["function"]["name"] == "lookup_weather"
    assert json.loads(call["function"]["arguments"]) == {"city": "Springfield"}


def test_malformed_function_parameters_are_kept_and_flagged():
    attributes = {
        **_tool_call_span("1.84.10"),
        "llm.request.functions.0.parameters": "{not json",
    }

    mapped = map_span_attributes(attributes)

    assert isinstance(mapped, MappedCall)
    assert mapped.parse_degraded is True
    assert mapped.request["tools"][0]["function"]["parameters"] == "{not json"


@pytest.mark.parametrize("version", VERSIONS)
def test_generation_name_span_names_the_route(rows, version):
    _export(_span(_text_span(version), name="label-alert"))

    assert (rows[0]["route"], rows[0]["route_source"]) == ("label-alert", "explicit")
    assert rows[0]["trace_name"] == "label-alert"


@pytest.mark.parametrize(
    "name",
    ["litellm_request", "raw_gen_ai_request", "chat gpt-5.4-mini", "chat", "  "],
)
def test_litellm_default_span_names_keep_the_derived_route(rows, name):
    _export(_span(_text_span("1.103.0"), name=name))

    assert (rows[0]["route"], rows[0]["route_source"]) == ("chat", "derived")


def test_explicit_route_attributes_win_over_the_span_name(rows):
    attributes = _text_span("1.103.0")
    _export(
        _span({**attributes, "metergraph.route": "ticket-triage"}, name="label-alert"),
        _span({**attributes, "gen_ai.prompt.name": "classify"}, name="label-alert"),
    )

    assert [(row["route"], row["route_source"]) for row in rows] == [
        ("ticket-triage", "explicit"),
        ("classify", "explicit"),
    ]


def test_application_span_carrying_litellm_attributes_keeps_the_derived_route(rows):
    # Without USE_OTEL_LITELLM_REQUEST_SPAN, LiteLLM writes its attributes onto
    # the caller's active span, whose name is the application's, not the call's.
    _export(_span(_text_span("1.84.10"), name="weather-agent-run", scope="e2e-agent"))

    assert (rows[0]["route"], rows[0]["route_source"]) == ("chat", "derived")
    assert rows[0]["request_id"] == RESPONSE_ID


def test_spans_from_other_producers_are_unchanged(rows):
    attributes = {
        key: value
        for key, value in _tool_call_span("1.103.0").items()
        if not key.startswith("litellm.")
    }

    _export(_span(attributes, name="label-alert", scope="some-other-instrumentor"))
    mapped = map_span_attributes(attributes)

    row = rows[0]
    assert (row["route"], row["route_source"]) == ("chat", "derived")
    assert row["request_id"] is None
    assert row["tool_calls"] is None
    assert "tool_definitions" not in row
    assert "tools" not in json.loads(row["request_json"])
    assert isinstance(mapped, MappedCall)
    assert "id" not in mapped.response
    assert mapped.response["choices"] == [{"finish_reason": "tool_calls"}]
