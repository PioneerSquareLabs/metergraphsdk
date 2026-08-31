from __future__ import annotations

import json

import metergraph
from metergraph.opentelemetry import MetergraphGenAIExporter
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor


def run_example() -> MetergraphGenAIExporter:
    # MeterGraph integration: add MeterGraph as one more span processor on the
    # tracer provider that carries Langfuse SDK spans. In a real application
    # that is the provider the Langfuse client uses (see README.md); this
    # offline demo builds a plain TracerProvider so it needs no Langfuse keys,
    # no provider key, and no network.
    exporter = MetergraphGenAIExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    # Offline stand-in for the Langfuse SDK: emit the same spans a
    # `@observe(as_type="generation")` call records, with the SDK's
    # `langfuse.observation.*` attributes. Everything below the integration
    # block above is synthetic demo traffic, not integration code.
    tracer = provider.get_tracer("langfuse-sdk")

    # A non-generation observation (a nested workflow span): the exporter
    # skips it and counts the skip in `exporter.skipped["ineligible-kind"]`.
    with tracer.start_as_current_span(
        "demo-workflow", attributes={"langfuse.observation.type": "span"}
    ):
        # The generation itself, with the attributes the Langfuse SDK records
        # for one chat call.
        with tracer.start_as_current_span(
            "demo-generation",
            attributes={
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
                # Session and user reach spans as bare OTel-style keys, not
                # under the langfuse.* prefix: the SDK defines these as
                # TRACE_SESSION_ID = "session.id" / TRACE_USER_ID = "user.id"
                # and propagates them through baggage.
                "session.id": "sess-1",
                "user.id": "u-1",
                "langfuse.trace.name": "synthetic-trace",
            },
        ):
            pass

    return exporter


if __name__ == "__main__":
    exporter = run_example()
    print(f"exported Langfuse demo spans; skipped: {exporter.skipped}")
    # shutdown() delivers the queued capture row and stops background work.
    metergraph.shutdown()
