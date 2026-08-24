"""OpenTelemetry GenAI span export through MeterGraph's existing transport."""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

import metergraph
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
from opentelemetry.trace import StatusCode

from ._capture import get_runtime
from ._context import CaptureContext


class OpenTelemetrySpanError(Exception):
    """Internal marker used to preserve an exported span's error status."""


def _attribute(attributes: Mapping[str, Any], name: str) -> Any:
    return attributes.get(name)


def _first_string(value: Any) -> str | None:
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            decoded = None
        if isinstance(decoded, list) and decoded:
            return str(decoded[0])
        return value
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return str(value[0]) if value else None
    return None


def _text_from_output_messages(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        messages = json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(messages, list):
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


def _request_content(attributes: Mapping[str, Any]) -> tuple[str | None, str | None]:
    system = _attribute(attributes, "gen_ai.system_instructions")
    messages = _attribute(attributes, "gen_ai.input.messages")
    if not isinstance(messages, str):
        return system if isinstance(system, str) else None, None
    if isinstance(system, str):
        return system, messages
    try:
        decoded = json.loads(messages)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None, messages
    if not isinstance(decoded, list):
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


def _hex_id(value: int, width: int) -> str | None:
    return f"{value:0{width}x}" if value else None


class MetergraphGenAIExporter(SpanExporter):
    """Export completed OpenTelemetry GenAI spans through MeterGraph."""

    def __init__(self) -> None:
        metergraph.init()

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        runtime = get_runtime()
        if runtime is None:
            return SpanExportResult.SUCCESS
        for span in spans:
            try:
                self._export_span(runtime, span)
            except Exception:
                # Observability must never break the application or its OTel pipeline.
                continue
        return SpanExportResult.SUCCESS

    def _export_span(self, runtime: Any, span: ReadableSpan) -> None:
        attributes = span.attributes or {}
        model = _attribute(attributes, "gen_ai.request.model")
        provider = _attribute(attributes, "gen_ai.provider.name") or _attribute(
            attributes, "gen_ai.system"
        )
        if not isinstance(model, str) or not isinstance(provider, str):
            return

        operation = _attribute(attributes, "gen_ai.operation.name")
        operation = operation if isinstance(operation, str) else "inference"
        request: dict[str, Any] = {"model": model}
        system, messages = _request_content(attributes)
        if system is not None:
            request["system_instructions"] = system
        if messages is not None:
            request["messages"] = messages

        context = span.context
        parent = span.parent
        resource_attributes = (
            span.resource.attributes if span.resource is not None else {}
        )
        function_name = _attribute(attributes, "code.function.name")
        if not isinstance(function_name, str) or not function_name:
            function_name = span.name
        function_module = _attribute(attributes, "code.namespace") or _attribute(
            resource_attributes, "service.name"
        )
        if not isinstance(function_module, str):
            function_module = None
        trace_id = _hex_id(context.trace_id, 32) if context is not None else None
        span_id = _hex_id(context.span_id, 16) if context is not None else None
        parent_span_id = _hex_id(parent.span_id, 16) if parent is not None else None
        call = runtime.call_state(
            provider,
            operation,
            request,
            context=CaptureContext(
                route=operation,
                trace_id=trace_id,
                trace_name=span.name,
                parent_span_id=parent_span_id,
                func_name=function_name,
                func_module=function_module,
            ),
        )
        if span_id is not None:
            call.span_id = span_id
        if span.start_time is not None:
            call.ts = datetime.fromtimestamp(
                span.start_time / 1_000_000_000, tz=timezone.utc
            ).isoformat()
        if span.start_time is not None and span.end_time is not None:
            duration_seconds = max(0, span.end_time - span.start_time) / 1_000_000_000
            call.started = time.perf_counter() - duration_seconds

        response_model = _attribute(attributes, "gen_ai.response.model")
        finish_reason = _first_string(
            _attribute(attributes, "gen_ai.response.finish_reasons")
        )
        response = {
            "model": response_model if isinstance(response_model, str) else model,
            "usage": {
                "input_tokens": _attribute(
                    attributes, "gen_ai.usage.input_tokens"
                ),
                "output_tokens": _attribute(
                    attributes, "gen_ai.usage.output_tokens"
                ),
            },
            "finish_reason": finish_reason,
            "choices": (
                [{"finish_reason": finish_reason}]
                if finish_reason is not None
                else []
            ),
        }
        output_text = _text_from_output_messages(
            _attribute(attributes, "gen_ai.output.messages")
        )
        if span.status.status_code is StatusCode.ERROR:
            call.finish(
                response,
                error=OpenTelemetrySpanError(
                    span.status.description or "OpenTelemetry GenAI span failed"
                ),
                response_text=output_text,
            )
        else:
            call.finish(response, response_text=output_text)

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        return metergraph.flush(max(0, timeout_millis) / 1000)

    def shutdown(self) -> None:
        metergraph.shutdown()


__all__ = ["MetergraphGenAIExporter"]
