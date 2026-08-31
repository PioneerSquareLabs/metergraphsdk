"""OpenTelemetry GenAI span export through MeterGraph's existing transport.

Captures spans emitted in any telemetry dialect the attribute mapper
understands — OpenTelemetry ``gen_ai.*`` semantic conventions, OpenInference
(Arize Phoenix), and the Langfuse SDK — with optional instrumentation-scope
filtering and rate-limited skip observability.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any, Iterable, Sequence

import metergraph
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
from opentelemetry.trace import StatusCode

from ._capture import _get_runtime
from ._context import CaptureContext
from ._failure_log import FailureLogger
from ._genai_attrs import SkipReason, map_span_attributes

# Skip-counter key for spans filtered by include_scopes/exclude_scopes.
_SKIP_SCOPE = "scope"


class OpenTelemetrySpanError(Exception):
    """Internal marker used to preserve an exported span's error status."""


def _hex_id(value: int, width: int) -> str | None:
    return f"{value:0{width}x}" if value else None


class MetergraphGenAIExporter(SpanExporter):
    """Export completed OpenTelemetry GenAI spans through MeterGraph.

    ``include_scopes``/``exclude_scopes`` filter on
    ``span.instrumentation_scope.name``: exclude always wins, and when
    ``include_scopes`` is set only the listed scopes pass. Spans that produce
    no capture row are counted per reason in the public ``skipped`` dict
    (keys: ``"scope"`` plus the ``SkipReason`` values ``"not-genai"`` and
    ``"no-model"``). Dialects added later contribute further keys.
    """

    def __init__(
        self,
        include_scopes: Iterable[str] | None = None,
        exclude_scopes: Iterable[str] | None = None,
    ) -> None:
        metergraph.init()
        self._include_scopes = (
            frozenset(include_scopes) if include_scopes is not None else None
        )
        self._exclude_scopes = frozenset(exclude_scopes or ())
        self.skipped: dict[str, int] = {}
        self._failure_log = FailureLogger()

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        runtime = _get_runtime()
        if runtime is None:
            return SpanExportResult.SUCCESS
        for span in spans:
            try:
                self._export_span(runtime, span)
            except Exception:
                # Observability must never break the application or its OTel pipeline.
                continue
        return SpanExportResult.SUCCESS

    def _count_skip(self, reason: str) -> None:
        self.skipped[reason] = self.skipped.get(reason, 0) + 1

    def _export_span(self, runtime: Any, span: ReadableSpan) -> None:
        scope = getattr(span, "instrumentation_scope", None)
        scope_name = getattr(scope, "name", None)
        if scope_name in self._exclude_scopes or (
            self._include_scopes is not None
            and scope_name not in self._include_scopes
        ):
            self._count_skip(_SKIP_SCOPE)
            return

        attributes = span.attributes or {}
        mapped = map_span_attributes(attributes)
        if isinstance(mapped, SkipReason):
            self._count_skip(mapped.value)
            # NOT_GENAI covers every ordinary span on a shared
            # TracerProvider, so it stays silent. Only an eligible-but-unusable
            # span warrants a diagnostic — and it names no attribute values.
            if mapped is SkipReason.NO_MODEL:
                span_kind = getattr(span.kind, "name", None)
                self._failure_log.report(
                    "otel_span_no_model",
                    "skipped an eligible GenAI span with no model attribute "
                    f"(scope={scope_name!r}, span_kind={span_kind}, "
                    f"skip={mapped.value})",
                )
            return

        provider = mapped.provider or (
            mapped.dialects[0] if mapped.dialects else "gen_ai"
        )
        context = span.context
        parent = span.parent
        resource_attributes = (
            span.resource.attributes if span.resource is not None else {}
        )
        function_name = attributes.get("code.function.name")
        if not isinstance(function_name, str) or not function_name:
            function_name = span.name
        function_module = attributes.get("code.namespace") or resource_attributes.get(
            "service.name"
        )
        if not isinstance(function_module, str):
            function_module = None
        trace_id = _hex_id(context.trace_id, 32) if context is not None else None
        span_id = _hex_id(context.span_id, 16) if context is not None else None
        parent_span_id = _hex_id(parent.span_id, 16) if parent is not None else None

        response = dict(mapped.response)

        call = runtime.call_state(
            provider,
            mapped.operation,
            mapped.request,
            context=CaptureContext(
                route=mapped.operation,
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
            if span.end_time is not None:
                duration = max(0, span.end_time - span.start_time) / 1_000_000_000
                call.started = time.perf_counter() - duration

        error: OpenTelemetrySpanError | None = None
        if span.status.status_code is StatusCode.ERROR:
            error = OpenTelemetrySpanError(
                span.status.description or "OpenTelemetry GenAI span failed"
            )
        # finish() treats error=None exactly as an omitted error.
        call.finish(response, error=error, response_text=mapped.response_text)

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        return metergraph.flush(max(0, timeout_millis) / 1000)

    def shutdown(self) -> None:
        metergraph.shutdown()


__all__ = ["MetergraphGenAIExporter"]
