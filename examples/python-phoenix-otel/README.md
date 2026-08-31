# Arize Phoenix (OpenInference) live capture

Use this integration when your application is already instrumented for
[Arize Phoenix](https://phoenix.arize.com/) with OpenInference
instrumentation and you want MeterGraph to receive the same completed LLM
spans. MeterGraph is added as one more span processor on the tracer provider
Phoenix registers; existing call sites stay unchanged.

**One capture path per call.** A call captured through this exporter must not
also be wrapped with `metergraph.wrap()` (or any other MeterGraph capture
path), or it is counted twice. Pick one path per client.

## Install

```bash
python -m pip install 'metergraph[otel]' arize-phoenix-otel openinference-instrumentation-openai
```

(Substitute the OpenInference instrumentor packages your application uses.)

Configure the normal MeterGraph environment:

```bash
export METERGRAPH_APP_TOKEN=<your-metergraph-token>
export METERGRAPH_INGEST_URL=<your-metergraph-ingest-url>
export METERGRAPH_REPOSITORY=owner/repository
```

## Real-world setup

Register Phoenix as usual, then add MeterGraph to the provider Phoenix
returns:

```python
from phoenix.otel import register
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from metergraph.opentelemetry import MetergraphGenAIExporter

tracer_provider = register(project_name="my-app")  # existing Phoenix setup
tracer_provider.add_span_processor(
    BatchSpanProcessor(MetergraphGenAIExporter())
)
```

Both destinations now receive every span: Phoenix keeps its full traces, and
MeterGraph captures the LLM spans (OpenInference `openinference.span.kind:
LLM`). Non-LLM spans (chains, tools, retrievers) are skipped and counted in
the public `exporter.skipped` dict.

To restrict the exporter to specific instrumentation scopes, pass
`include_scopes=` / `exclude_scopes=` (matched against
`span.instrumentation_scope.name`; exclude wins). OpenInference instrumentors
use scope names like `openinference.instrumentation.openai` — print
`span.instrumentation_scope.name` or watch `exporter.skipped["scope"]` to
discover the exact names your installed versions emit.

## Run the offline demo

```bash
python main.py
```

[`main.py`](main.py) needs no Phoenix server, no OpenAI key, and no network:
it builds a plain `TracerProvider`, attaches `MetergraphGenAIExporter`, and
emits the same OpenInference-attribute spans the OpenAI instrumentor would
emit for one chat call. Open it and search for `MeterGraph integration` —
the only integration block is the `add_span_processor` call; everything else
is synthetic demo traffic.

## What MeterGraph captures

The exporter maps OpenInference attributes — `llm.provider`,
`llm.model_name`, `llm.token_count.*` (prompt, completion, cache and
reasoning details), `llm.input_messages.*`, `llm.output_messages.*`, and
`llm.cost.total` as reported-cost evidence — onto the same capture rows as
provider-client capture, preserving OpenTelemetry trace identity, latency,
and error status. Message content is subject to the SDK's normal
scrub/redact/truncate policy; set `METERGRAPH_CAPTURE_TEXT=0` for
metadata-only capture.
