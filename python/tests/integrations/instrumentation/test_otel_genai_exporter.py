from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SpanExportResult
from opentelemetry.sdk.util.instrumentation import InstrumentationScope
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


def _scoped_span(
    attributes: dict,
    *,
    scope_name: str | None = None,
    status: Status | None = None,
) -> ReadableSpan:
    scope = (
        InstrumentationScope(scope_name) if scope_name is not None else None
    )
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
        instrumentation_scope=scope,
    )


def _scope_traffic_attributes() -> dict:
    """A minimal eligible gen_ai span, for tests that vary only the scope."""
    return {
        "gen_ai.request.model": "gpt-5-mini",
        "gen_ai.provider.name": "openai",
        "gen_ai.usage.input_tokens": 4,
        "gen_ai.usage.output_tokens": 1,
    }


def _openinference_attributes() -> dict:
    return {
        "openinference.span.kind": "LLM",
        "llm.model_name": "gpt-5-mini",
        "llm.provider": "openai",
        "llm.token_count.prompt": 30,
        "llm.token_count.completion": 12,
        "llm.token_count.prompt_details.cache_read": 5,
        "llm.token_count.prompt_details.cache_write": 3,
        "llm.token_count.completion_details.reasoning": 7,
        "llm.input_messages.0.message.role": "user",
        "llm.input_messages.0.message.content": "Synthetic input",
        "llm.output_messages.0.message.role": "assistant",
        "llm.output_messages.0.message.content": "Synthetic result",
    }


def test_genai_span_without_provider_falls_back_to_dialect(monkeypatch):
    rows = Rows()
    _capture.set_runtime(Runtime(rows, Options(app_root="")))
    monkeypatch.setattr(metergraph, "init", lambda: None)
    exporter = MetergraphGenAIExporter()

    exporter.export(
        [
            _span(
                {
                    "gen_ai.operation.name": "chat",
                    "gen_ai.request.model": "gpt-5-mini",
                }
            )
        ]
    )

    assert len(rows.rows) == 1
    assert rows.rows[0]["provider"] == "gen_ai"
    assert rows.rows[0]["model"] == "gpt-5-mini"
    _capture.set_runtime(None)


def test_exclude_scopes_skips_without_logging(monkeypatch, caplog):
    rows = Rows()
    _capture.set_runtime(Runtime(rows, Options(app_root="")))
    monkeypatch.setattr(metergraph, "init", lambda: None)
    exporter = MetergraphGenAIExporter(exclude_scopes=["noisy.scope"])

    with caplog.at_level(logging.DEBUG, logger="metergraph"):
        result = exporter.export(
            [
                _scoped_span(
                    _scope_traffic_attributes(), scope_name="noisy.scope"
                )
            ]
        )

    assert result is SpanExportResult.SUCCESS
    assert rows.rows == []
    assert exporter.skipped["scope"] == 1
    assert [r for r in caplog.records if r.name == "metergraph"] == []
    _capture.set_runtime(None)


def test_include_scopes_passes_only_listed(monkeypatch):
    rows = Rows()
    _capture.set_runtime(Runtime(rows, Options(app_root="")))
    monkeypatch.setattr(metergraph, "init", lambda: None)
    exporter = MetergraphGenAIExporter(include_scopes=["good.scope"])

    exporter.export(
        [
            _scoped_span(_scope_traffic_attributes(), scope_name="good.scope"),
            _scoped_span(_scope_traffic_attributes(), scope_name="other.scope"),
        ]
    )

    assert len(rows.rows) == 1
    assert exporter.skipped["scope"] == 1
    _capture.set_runtime(None)


def test_exclude_scopes_wins_over_include_scopes(monkeypatch):
    rows = Rows()
    _capture.set_runtime(Runtime(rows, Options(app_root="")))
    monkeypatch.setattr(metergraph, "init", lambda: None)
    exporter = MetergraphGenAIExporter(
        include_scopes=["both.scope"], exclude_scopes=["both.scope"]
    )

    exporter.export(
        [_scoped_span(_scope_traffic_attributes(), scope_name="both.scope")]
    )

    assert rows.rows == []
    assert exporter.skipped["scope"] == 1
    _capture.set_runtime(None)


def test_default_scope_filter_captures_all_eligible(monkeypatch):
    rows = Rows()
    _capture.set_runtime(Runtime(rows, Options(app_root="")))
    monkeypatch.setattr(metergraph, "init", lambda: None)
    exporter = MetergraphGenAIExporter()

    exporter.export(
        [
            _scoped_span(_scope_traffic_attributes(), scope_name="any.scope"),
            _scoped_span(_scope_traffic_attributes(), scope_name="other.scope"),
        ]
    )

    assert len(rows.rows) == 2
    assert "scope" not in exporter.skipped
    _capture.set_runtime(None)


def test_ordinary_span_skipped_silently(monkeypatch, caplog):
    rows = Rows()
    _capture.set_runtime(Runtime(rows, Options(app_root="")))
    monkeypatch.setattr(metergraph, "init", lambda: None)
    exporter = MetergraphGenAIExporter()

    with caplog.at_level(logging.DEBUG, logger="metergraph"):
        exporter.export([_span({"http.request.method": "GET"})])

    assert rows.rows == []
    assert [r for r in caplog.records if r.name == "metergraph"] == []
    assert "no-model" not in exporter.skipped
    _capture.set_runtime(None)


def test_no_model_span_logs_once_without_attribute_values(monkeypatch, caplog):
    rows = Rows()
    _capture.set_runtime(Runtime(rows, Options(app_root="")))
    monkeypatch.setattr(metergraph, "init", lambda: None)
    exporter = MetergraphGenAIExporter()
    span = _scoped_span(
        {
            "gen_ai.operation.name": "chat",
            "gen_ai.system_instructions": "SECRET-VALUE",
        },
        scope_name="synthetic.scope",
    )

    with caplog.at_level(logging.DEBUG, logger="metergraph"):
        exporter.export([span])
        exporter.export([span])

    assert rows.rows == []
    assert exporter.skipped["no-model"] == 2
    records = [r for r in caplog.records if r.name == "metergraph"]
    assert len(records) == 1
    assert "SECRET-VALUE" not in records[0].getMessage()
    assert "synthetic.scope" in records[0].getMessage()
    _capture.set_runtime(None)


def test_capture_text_disabled_yields_metadata_only_rows(monkeypatch):
    rows = Rows()
    _capture.set_runtime(
        Runtime(rows, Options(app_root="", capture_text=False))
    )
    monkeypatch.setattr(metergraph, "init", lambda: None)
    exporter = MetergraphGenAIExporter()
    genai_messages = json.dumps(
        [
            {
                "role": "user",
                "parts": [{"type": "text", "content": "Synthetic input"}],
            }
        ]
    )

    exporter.export(
        [
            _span(
                {
                    "gen_ai.operation.name": "chat",
                    "gen_ai.provider.name": "anthropic",
                    "gen_ai.request.model": "claude-opus-5",
                    "gen_ai.input.messages": genai_messages,
                    "gen_ai.usage.input_tokens": 9,
                    "gen_ai.usage.output_tokens": 2,
                }
            ),
            _span(_scope_traffic_attributes()),
        ]
    )

    assert len(rows.rows) == 2
    for row in rows.rows:
        assert row["request_json"] is None
        assert row["response_text"] is None
        assert row["content_opted_in"] is False
        assert row["input_tokens"] is not None
    _capture.set_runtime(None)


def _langfuse_attributes() -> dict:
    return {
        "langfuse.observation.type": "generation",
        "langfuse.observation.model.name": "claude-opus-5",
        "langfuse.observation.usage_details": json.dumps(
            {"input": 11, "output": 4, "total": 15}
        ),
        "langfuse.observation.input": json.dumps(
            [{"role": "user", "content": "Synthetic input"}]
        ),
        "langfuse.observation.output": json.dumps(
            {"role": "assistant", "content": "Synthetic reply"}
        ),
        "session.id": "sess-1",
        "langfuse.trace.name": "synthetic-trace",
    }


def test_langfuse_generation_span_falls_back_to_langfuse_provider(monkeypatch):
    rows = Rows()
    _capture.set_runtime(Runtime(rows, Options(app_root="")))
    monkeypatch.setattr(metergraph, "init", lambda: None)
    exporter = MetergraphGenAIExporter()

    exporter.export([_span(_langfuse_attributes())])

    assert len(rows.rows) == 1
    row = rows.rows[0]
    assert row["provider"] == "langfuse"
    assert row["model"] == "claude-opus-5"
    assert row["input_tokens"] == 11
    assert row["output_tokens"] == 4
    assert row["session_id"] == "sess-1"
    assert row["trace_name"] == "synthetic-trace"
    response = json.loads(row["response_text"])
    assert response["content"] == "Synthetic reply"
    _capture.set_runtime(None)


def test_mixed_dialect_span_falls_back_to_the_eligible_dialect(monkeypatch):
    """A Langfuse `type="span"` observation wrapping a real gen_ai LLM call.

    Langfuse only vetoes here; gen_ai is what makes the span eligible and is
    the only dialect that contributes a field. The row must go out as
    provider="gen_ai" -- the same value the identical span reports without the
    Langfuse wrapper. Reporting "langfuse" would split one call across two
    provider buckets on nothing but instrumentation nesting.
    """
    rows = Rows()
    _capture.set_runtime(Runtime(rows, Options(app_root="")))
    monkeypatch.setattr(metergraph, "init", lambda: None)
    exporter = MetergraphGenAIExporter()

    exporter.export(
        [
            _span(
                {
                    "langfuse.observation.type": "span",
                    "gen_ai.operation.name": "chat",
                    "gen_ai.request.model": "gpt-5-mini",
                    "gen_ai.usage.input_tokens": 10,
                    "gen_ai.usage.output_tokens": 2,
                }
            )
        ]
    )

    assert len(rows.rows) == 1
    row = rows.rows[0]
    assert row["provider"] == "gen_ai"
    assert row["model"] == "gpt-5-mini"
    assert row["input_tokens"] == 10
    assert row["output_tokens"] == 2
    _capture.set_runtime(None)


def test_langfuse_completion_start_time_sets_ttft(monkeypatch):
    rows = Rows()
    _capture.set_runtime(Runtime(rows, Options(app_root="")))
    monkeypatch.setattr(metergraph, "init", lambda: None)
    exporter = MetergraphGenAIExporter()
    attributes = _langfuse_attributes()
    attributes["langfuse.observation.completion_start_time"] = (
        datetime.fromtimestamp(1_786_496_400.05, tz=timezone.utc).isoformat()
    )

    exporter.export([_span(attributes)])

    assert len(rows.rows) == 1
    assert rows.rows[0]["ttft_ms"] == 50
    _capture.set_runtime(None)


def test_langfuse_json_encoded_completion_start_time_still_sets_ttft(monkeypatch):
    """The real SDK JSON-encodes this timestamp, so the attribute value arrives
    wrapped in literal quotes. Feeding that straight to the ISO parser fails and
    drops TTFT for every Langfuse span -- verified against langfuse 3.15/4.15."""
    rows = Rows()
    _capture.set_runtime(Runtime(rows, Options(app_root="")))
    monkeypatch.setattr(metergraph, "init", lambda: None)
    exporter = MetergraphGenAIExporter()
    attributes = _langfuse_attributes()
    attributes["langfuse.observation.completion_start_time"] = json.dumps(
        datetime.fromtimestamp(1_786_496_400.05, tz=timezone.utc).isoformat()
    )

    exporter.export([_span(attributes)])

    assert len(rows.rows) == 1
    assert rows.rows[0]["ttft_ms"] == 50
    _capture.set_runtime(None)


def test_langfuse_error_level_marks_row_error(monkeypatch):
    rows = Rows()
    _capture.set_runtime(Runtime(rows, Options(app_root="")))
    monkeypatch.setattr(metergraph, "init", lambda: None)
    exporter = MetergraphGenAIExporter()
    attributes = _langfuse_attributes()
    attributes["langfuse.observation.level"] = "ERROR"
    attributes["langfuse.observation.status_message"] = "synthetic Langfuse failure"

    exporter.export([_span(attributes)])

    assert len(rows.rows) == 1
    row = rows.rows[0]
    assert row["status_code"] == "error"
    assert row["error"] is True
    assert row["error_type"] == "OpenTelemetrySpanError"
    _capture.set_runtime(None)


def test_parse_degraded_span_captures_counts_and_logs_without_values(
    monkeypatch, caplog
):
    rows = Rows()
    _capture.set_runtime(Runtime(rows, Options(app_root="")))
    monkeypatch.setattr(metergraph, "init", lambda: None)
    exporter = MetergraphGenAIExporter()
    attributes = _langfuse_attributes()
    attributes["langfuse.observation.model.parameters"] = (
        '{"temperature": SECRET-NOT-JSON'
    )
    span = _scoped_span(attributes, scope_name="synthetic.scope")

    with caplog.at_level(logging.DEBUG, logger="metergraph"):
        result = exporter.export([span])
        exporter.export([span])

    assert result is SpanExportResult.SUCCESS
    # Degraded spans still capture: parse-degraded is a diagnostic counter,
    # not a skip.
    assert len(rows.rows) == 2
    assert rows.rows[0]["model"] == "claude-opus-5"
    assert exporter.skipped["parse-degraded"] == 2
    records = [r for r in caplog.records if r.name == "metergraph"]
    assert len(records) == 1  # rate-limited to one line
    message = records[0].getMessage()
    assert "synthetic.scope" in message
    assert "langfuse" in message
    assert "SECRET-NOT-JSON" not in message
    _capture.set_runtime(None)
