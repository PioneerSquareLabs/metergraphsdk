from __future__ import annotations

import metergraph
from metergraph.opentelemetry import MetergraphGenAIExporter
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor


def run_example() -> MetergraphGenAIExporter:
    # MeterGraph integration: add MeterGraph as one more span processor on the
    # tracer provider that carries OpenInference spans. In a real application
    # that provider comes from `phoenix.otel.register()` (see README.md); this
    # offline demo builds a plain TracerProvider so it needs no Phoenix server,
    # no OpenAI key, and no network.
    exporter = MetergraphGenAIExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    # Offline stand-in for OpenInference auto-instrumentation: emit the same
    # spans `openinference-instrumentation-openai` would emit for one chat
    # call, under the same instrumentation-scope name. Everything below the
    # integration block above is synthetic demo traffic, not integration code.
    tracer = provider.get_tracer("openinference.instrumentation.openai")

    # A non-LLM OpenInference span (a chain step): the exporter skips it and
    # counts the skip in `exporter.skipped["ineligible-kind"]`.
    with tracer.start_as_current_span(
        "demo-chain", attributes={"openinference.span.kind": "CHAIN"}
    ):
        # The LLM span itself, with the OpenInference attributes Phoenix
        # instrumentors record for a chat completion.
        with tracer.start_as_current_span(
            "ChatCompletion",
            attributes={
                "openinference.span.kind": "LLM",
                "llm.provider": "openai",
                "llm.model_name": "gpt-5-mini",
                "llm.token_count.prompt": 30,
                "llm.token_count.completion": 12,
                "llm.token_count.prompt_details.cache_read": 5,
                "llm.token_count.completion_details.reasoning": 7,
                "llm.input_messages.0.message.role": "user",
                "llm.input_messages.0.message.content": "Synthetic input",
                "llm.output_messages.0.message.role": "assistant",
                "llm.output_messages.0.message.content": "Synthetic result",
            },
        ):
            pass

    return exporter


if __name__ == "__main__":
    exporter = run_example()
    print(f"exported OpenInference demo spans; skipped: {exporter.skipped}")
    # shutdown() delivers the queued capture row and stops background work.
    metergraph.shutdown()
