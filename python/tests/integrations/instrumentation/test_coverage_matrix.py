"""Deterministic end-to-end verification for the Dropzone coverage matrix.

The fixture drives real ``ReadableSpan`` objects through the public GenAI
exporter and the capture runtime. It uses no provider credentials or network,
so it is a repeatable regression gate. Upstream drift is covered separately by
``test_upstream_dialects.py``.
"""

from __future__ import annotations

import json

import pytest
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.trace import SpanContext, Status, StatusCode, TraceFlags
from opentelemetry.sdk.util.instrumentation import InstrumentationScope

import metergraph
from metergraph import _capture
from metergraph._capture import Options, Runtime
from metergraph.opentelemetry import MetergraphGenAIExporter


class Rows:
    def __init__(self) -> None:
        self.rows: list[dict] = []

    def enqueue(self, row: dict) -> bool:
        self.rows.append(row)
        return True


def _span(attributes: dict, *, error: bool = False) -> ReadableSpan:
    return ReadableSpan(
        name="coverage-matrix-call",
        context=SpanContext(
            trace_id=int("12" * 16, 16),
            span_id=int("34" * 8, 16),
            is_remote=False,
            trace_flags=TraceFlags(TraceFlags.SAMPLED),
        ),
        parent=None,
        resource=Resource.create({"service.name": "coverage-matrix"}),
        attributes=attributes,
        status=Status(StatusCode.ERROR if error else StatusCode.OK),
        start_time=1_786_496_400_000_000_000,
        end_time=1_786_496_400_125_000_000,
        instrumentation_scope=InstrumentationScope("coverage-matrix"),
    )


def _genai(provider: str, model: str, operation: str) -> dict:
    return {
        "gen_ai.operation.name": operation,
        "gen_ai.provider.name": provider,
        "gen_ai.request.model": model,
        "gen_ai.input.messages": json.dumps(
            [{"role": "user", "parts": [{"type": "text", "content": "Hi"}]}]
        ),
        "gen_ai.output.messages": json.dumps(
            [
                {
                    "role": "assistant",
                    "parts": [{"type": "text", "content": "Hello"}],
                }
            ]
        ),
        "gen_ai.usage.input_tokens": 11,
        "gen_ai.usage.output_tokens": 4,
    }


def _langfuse() -> dict:
    return {
        "langfuse.observation.type": "generation",
        "langfuse.observation.model.name": "claude-opus-5",
        "langfuse.observation.input": json.dumps(
            [{"role": "user", "content": "Hi"}]
        ),
        "langfuse.observation.output": json.dumps(
            {"role": "assistant", "content": "Hello"}
        ),
        "langfuse.observation.usage_details": json.dumps(
            {"input": 11, "output": 4}
        ),
    }


def _langsmith() -> dict:
    return {
        "langsmith.span.kind": "llm",
        "langsmith.trace.name": "coverage-matrix-call",
        "langsmith.trace.session_id": "coverage-session",
        "gen_ai.operation.name": "chat",
        "gen_ai.system": "openai",
        "gen_ai.request.model": "gpt-4o-mini",
        "gen_ai.usage.input_tokens": 11,
        "gen_ai.usage.output_tokens": 4,
        "gen_ai.prompt": json.dumps(
            {"messages": [{"role": "user", "content": "Hi"}]}
        ),
        "gen_ai.completion": json.dumps(
            {
                "choices": [
                    {"message": {"role": "assistant", "content": "Hello"}}
                ]
            }
        ),
    }


SUPPORTED_CASES = [
    pytest.param("openai-python", _genai("openai", "gpt-5-mini", "chat")),
    pytest.param("openai-typescript", _genai("openai", "gpt-5-mini", "responses")),
    pytest.param("anthropic-python", _genai("anthropic", "claude-opus-5", "messages")),
    pytest.param("anthropic-typescript", _genai("anthropic", "claude-opus-5", "messages")),
    pytest.param("gemini-python", _genai("google", "gemini-2.5-flash", "generate_content")),
    pytest.param("gemini-typescript", _genai("google", "gemini-2.5-flash", "generate_content")),
    pytest.param("vercel-ai-gateway-python", _genai("openai", "gpt-5-mini", "chat")),
    pytest.param("vercel-ai-sdk-typescript", _genai("openai", "gpt-5-mini", "generate")),
    pytest.param("openrouter", _genai("openai", "openai/gpt-5-mini", "chat")),
    pytest.param("litellm-legacy", _genai("openai", "gpt-5-mini", "completion")),
    pytest.param("litellm-current", _genai("anthropic", "claude-opus-5", "completion")),
    pytest.param("bedrock", _genai("aws.bedrock", "amazon.nova-lite-v1:0", "chat")),
    pytest.param("azure-openai", _genai("azure.openai", "gpt-4o-deployment", "chat")),
    pytest.param("openinference-phoenix", {
        "openinference.span.kind": "LLM",
        "llm.model_name": "gpt-5-mini",
        "llm.provider": "openai",
        "llm.input_messages.0.message.role": "user",
        "llm.input_messages.0.message.content": "Hi",
        "llm.output_messages.0.message.role": "assistant",
        "llm.output_messages.0.message.content": "Hello",
        "llm.token_count.prompt": 11,
        "llm.token_count.completion": 4,
        "llm.finish_reason": "stop",
    }),
    pytest.param("langfuse", _langfuse()),
    pytest.param("langsmith", _langsmith()),
]


@pytest.mark.parametrize("case, attributes", SUPPORTED_CASES, ids=lambda value: value if isinstance(value, str) else None)
def test_supported_matrix_paths_capture_required_fields(monkeypatch, case, attributes):
    rows = Rows()
    _capture.set_runtime(Runtime(rows, Options(app_root="")))
    monkeypatch.setattr(metergraph, "init", lambda: None)
    exporter = MetergraphGenAIExporter()

    exporter.export([_span(attributes)])

    assert len(rows.rows) == 1, case
    row = rows.rows[0]
    assert row["provider"] in {
        "openai",
        "anthropic",
        "google",
        "bedrock",
        "azure",
        "langfuse",
    }, case
    assert row["model"], case
    assert row["route"], case
    assert row["input_tokens"] == 11, case
    assert row["output_tokens"] == 4, case
    assert row["request_json"], case
    assert row["response_text"], case
    _capture.set_runtime(None)


@pytest.mark.parametrize(
    "attributes, reason",
    [
        ({"http.request.method": "GET"}, "not-genai"),
        ({"gen_ai.operation.name": "chat"}, "no-model"),
        (
            {"openinference.span.kind": "CHAIN", "llm.model_name": "gpt-5-mini"},
            "ineligible-kind",
        ),
        (
            {
                "langfuse.observation.type": "span",
                "langfuse.observation.model.name": "gpt-5-mini",
            },
            "ineligible-kind",
        ),
        (
            _langsmith() | {"langsmith.span.kind": "chain"},
            "ineligible-kind",
        ),
    ],
)
def test_unsupported_paths_are_explicit_and_fail_open(monkeypatch, attributes, reason):
    rows = Rows()
    _capture.set_runtime(Runtime(rows, Options(app_root="")))
    monkeypatch.setattr(metergraph, "init", lambda: None)
    exporter = MetergraphGenAIExporter()

    result = exporter.export([_span(attributes)])

    assert result.name == "SUCCESS"
    assert rows.rows == []
    assert exporter.skipped[reason] == 1
    _capture.set_runtime(None)


def test_malformed_optional_content_captures_metadata_and_marks_degraded(monkeypatch):
    rows = Rows()
    _capture.set_runtime(Runtime(rows, Options(app_root="")))
    monkeypatch.setattr(metergraph, "init", lambda: None)
    exporter = MetergraphGenAIExporter()
    attributes = _langsmith() | {"gen_ai.prompt": "{broken"}

    exporter.export([_span(attributes)])

    assert len(rows.rows) == 1
    assert rows.rows[0]["model"] == "gpt-4o-mini"
    assert rows.rows[0]["input_tokens"] == 11
    assert exporter.skipped["parse-degraded"] == 1
    _capture.set_runtime(None)


def test_error_path_keeps_capture_and_reports_failure(monkeypatch):
    rows = Rows()
    _capture.set_runtime(Runtime(rows, Options(app_root="")))
    monkeypatch.setattr(metergraph, "init", lambda: None)
    exporter = MetergraphGenAIExporter()

    exporter.export([_span(_genai("azure.openai", "gpt-4o-deployment", "chat"), error=True)])

    assert len(rows.rows) == 1
    assert rows.rows[0]["provider"] == "azure"
    assert rows.rows[0]["status_code"] == "error"
    _capture.set_runtime(None)
