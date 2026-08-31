"""OpenTelemetry GenAI span export through MeterGraph's existing transport.

Span attributes are translated by ``_genai_attrs``; this module does the span
plumbing, instrumentation-scope filtering, and skip accounting.
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

_SKIP_SCOPE = "scope"


class OpenTelemetrySpanError(Exception):
    """Internal marker used to preserve an exported span's error status."""


def _hex_id(value: int, width: int) -> str | None:
    return f"{value:0{width}x}" if value else None


def _iso_epoch_seconds(value: str) -> float | None:
    """Parse an ISO-8601 timestamp to epoch seconds, or None."""
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


class MetergraphGenAIExporter(SpanExporter):
    """Export completed OpenTelemetry GenAI spans through MeterGraph.

    ``include_scopes``/``exclude_scopes`` filter on
    ``span.instrumentation_scope.name``: exclude always wins, and when
    ``include_scopes`` is set only the listed scopes pass. Spans that produce
    no capture row are counted per reason in the public ``skipped`` dict
    (keys: ``"scope"`` plus the ``SkipReason`` values). ``"parse-degraded"``
    is a diagnostic counter rather than a skip: it tallies spans that captured
    despite malformed JSON in one or more attributes.
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
            # Ordinary spans on a shared TracerProvider land in NOT_GENAI,
            # and expected non-LLM observations (spans, events, tools) land in
            # INELIGIBLE_KIND; both stay silent. Only an eligible-but-unusable
            # span is worth a diagnostic. The message names no attribute values.
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

        if mapped.parse_degraded:
            # Diagnostic counter, not a skip: the span still captures with
            # whatever parsed. The log names scope and dialect only — never
            # attribute values.
            self._count_skip("parse-degraded")
            dialects = ",".join(mapped.dialects) or "unknown"
            self._failure_log.report(
                "otel_span_parse_degraded",
                "captured a GenAI span with malformed JSON in one or more "
                f"attributes (scope={scope_name!r}, dialects={dialects}); "
                "some fields may be incomplete",
            )

        response = dict(mapped.response)
        gateway: str | None = None
        if mapped.cost is not None and mapped.cost_source is not None:
            # An evidence-contract source string passed as ``gateway`` makes
            # CallState.finish() read ``response["cost"]`` and emit
            # reported_cost_usd/reported_cost_source without a gateway key.
            response["cost"] = mapped.cost
            gateway = mapped.cost_source

        call = runtime.call_state(
            provider,
            mapped.operation,
            mapped.request,
            context=CaptureContext(
                route=mapped.operation,
                session_id=mapped.session_id,
                trace_id=trace_id,
                trace_name=mapped.trace_name or span.name,
                parent_span_id=parent_span_id,
                func_name=function_name,
                func_module=function_module,
            ),
            gateway=gateway,
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

        ttft_ms: int | None = None
        if mapped.completion_start_time is not None and span.start_time is not None:
            completion_epoch = _iso_epoch_seconds(mapped.completion_start_time)
            if completion_epoch is not None:
                delta_ms = round(completion_epoch * 1000 - span.start_time / 1_000_000)
                if delta_ms >= 0:
                    ttft_ms = delta_ms

        error: OpenTelemetrySpanError | None = None
        if span.status.status_code is StatusCode.ERROR:
            error = OpenTelemetrySpanError(
                span.status.description or "OpenTelemetry GenAI span failed"
            )
        elif mapped.error_message is not None:
            error = OpenTelemetrySpanError(mapped.error_message)
        # finish() treats error=None exactly as an omitted error.
        call.finish(
            response,
            error=error,
            response_text=mapped.response_text,
            ttft_ms=ttft_ms,
        )

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        return metergraph.flush(max(0, timeout_millis) / 1000)

    def shutdown(self) -> None:
        metergraph.shutdown()


__all__ = ["MetergraphGenAIExporter"]
