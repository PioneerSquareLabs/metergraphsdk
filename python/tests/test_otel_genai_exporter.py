from __future__ import annotations

import json
from datetime import datetime, timezone

from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SpanExportResult
from opentelemetry.trace import SpanContext, Status, StatusCode, TraceFlags

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


def _span(attributes: dict, *, status: Status | None = None) -> ReadableSpan:
    return ReadableSpan(
        name="chat claude-opus-5",
        context=SpanContext(
            trace_id=int("12" * 16, 16),
            span_id=int("34" * 8, 16),
            is_remote=False,
            trace_flags=TraceFlags(TraceFlags.SAMPLED),
        ),
        parent=SpanContext(
            trace_id=int("12" * 16, 16),
            span_id=int("56" * 8, 16),
            is_remote=False,
            trace_flags=TraceFlags(TraceFlags.SAMPLED),
        ),
        resource=Resource.create({"service.name": "synthetic-genai-app"}),
        attributes=attributes,
        status=status or Status(StatusCode.OK),
        start_time=1_786_496_400_000_000_000,
        end_time=1_786_496_400_125_000_000,
    )


def test_exports_completed_genai_span_without_changing_message_order(monkeypatch):
    rows = Rows()
    _capture.set_runtime(Runtime(rows, Options(app_root="")))
    monkeypatch.setattr(metergraph, "init", lambda: None)
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
            {
                "role": "user",
                "parts": [{"type": "text", "content": "Second synthetic input"}],
            },
        ]
    )
    output = json.dumps(
        [
            {
                "role": "assistant",
                "parts": [{"type": "text", "content": "Synthetic result"}],
            },
            {
                "role": "assistant",
                "parts": [{"type": "text", "content": "Alternate result"}],
            },
        ]
    )
    exporter = MetergraphGenAIExporter()

    result = exporter.export(
        [
            _span(
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
        ]
    )

    assert result is SpanExportResult.SUCCESS
    assert len(rows.rows) == 1
    row = rows.rows[0]
    assert row["provider"] == "anthropic"
    assert row["model"] == "claude-opus-5"
    assert row["input_tokens"] == 41
    assert row["output_tokens"] == 7
    assert row["latency_ms"] == 125
    assert row["trace_id"] == "12" * 16
    assert row["span_id"] == "34" * 8
    assert row["parent_span_id"] == "56" * 8
    assert row["trace_name"] == "chat claude-opus-5"
    assert row["func"] == "chat claude-opus-5"
    assert row["module"] == "synthetic-genai-app"
    assert row["ts"] == datetime.fromtimestamp(
        1_786_496_400, tz=timezone.utc
    ).isoformat()
    request = json.loads(row["request_json"])
    assert request["system_instructions"] == system
    assert request["messages"] == messages
    assert json.loads(request["messages"])[1]["role"] == "assistant"
    response = json.loads(row["response_text"])
    assert response["content"] == "Synthetic result"
    assert response["model"] == "claude-opus-5-20260801"
    assert response["finish_reason"] == "stop"
    assert row["finish_reason"] == "stop"

    _capture.set_runtime(None)


def test_ignores_non_genai_spans(monkeypatch):
    rows = Rows()
    _capture.set_runtime(Runtime(rows, Options(app_root="")))
    monkeypatch.setattr(metergraph, "init", lambda: None)
    exporter = MetergraphGenAIExporter()

    result = exporter.export([_span({"http.request.method": "GET"})])

    assert result is SpanExportResult.SUCCESS
    assert rows.rows == []
    _capture.set_runtime(None)


def test_translates_current_litellm_span_shape(monkeypatch):
    rows = Rows()
    _capture.set_runtime(Runtime(rows, Options(app_root="")))
    monkeypatch.setattr(metergraph, "init", lambda: None)
    exporter = MetergraphGenAIExporter()
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
            {
                "role": "assistant",
                "parts": [{"type": "text", "content": "Earlier reply"}],
            },
            {
                "role": "user",
                "parts": [{"type": "text", "content": "Second"}],
            },
        ]
    )

    exporter.export(
        [
            _span(
                {
                    "gen_ai.operation.name": "chat",
                    "gen_ai.system": "openai",
                    "gen_ai.request.model": "gpt-5-mini",
                    "gen_ai.input.messages": messages,
                    "gen_ai.output.messages": json.dumps(
                        [
                            {
                                "role": "assistant",
                                "parts": [
                                    {"type": "text", "content": "Synthetic result"}
                                ],
                                "finish_reason": "stop",
                            }
                        ]
                    ),
                    "gen_ai.response.finish_reasons": '["stop"]',
                    "gen_ai.usage.input_tokens": 10,
                    "gen_ai.usage.output_tokens": 20,
                }
            )
        ]
    )

    assert len(rows.rows) == 1
    row = rows.rows[0]
    assert row["provider"] == "openai"
    request = json.loads(row["request_json"])
    assert json.loads(request["system_instructions"]) == [
        {"type": "text", "content": "Synthetic system"}
    ]
    assert [message["role"] for message in json.loads(request["messages"])] == [
        "user",
        "assistant",
        "user",
    ]
    assert row["finish_reason"] == "stop"
    _capture.set_runtime(None)


def test_maps_otel_error_status_without_raising(monkeypatch):
    rows = Rows()
    _capture.set_runtime(Runtime(rows, Options(app_root="")))
    monkeypatch.setattr(metergraph, "init", lambda: None)
    exporter = MetergraphGenAIExporter()

    result = exporter.export(
        [
            _span(
                {
                    "gen_ai.operation.name": "chat",
                    "gen_ai.provider.name": "openai",
                    "gen_ai.request.model": "gpt-5-mini",
                },
                status=Status(StatusCode.ERROR, "synthetic failure"),
            )
        ]
    )

    assert result is SpanExportResult.SUCCESS
    assert rows.rows[0]["status_code"] == "error"
    assert rows.rows[0]["error"] is True
    assert rows.rows[0]["error_type"] == "OpenTelemetrySpanError"
    _capture.set_runtime(None)
