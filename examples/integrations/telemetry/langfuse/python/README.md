# Langfuse SDK live capture

Use this integration when your application already traces LLM calls with the
[Langfuse Python SDK](https://langfuse.com/docs/sdk/python) (v3/v4, which is
OpenTelemetry-based) and you want MeterGraph to receive the same completed
generation spans. MeterGraph is added as one more span processor on the
tracer provider Langfuse uses; existing `@observe` decorators and Langfuse
call sites stay unchanged.

**One capture path per call.** A call captured through this exporter must not
also be wrapped with `metergraph.wrap()` (or Langfuse's own OpenAI wrapper on
top of a MeterGraph-wrapped client), or it is counted twice. Pick one path
per client.

## Install

```bash
python -m pip install 'metergraph[otel]' langfuse
```

Configure the normal MeterGraph environment:

```bash
export METERGRAPH_APP_TOKEN=<your-metergraph-token>
export METERGRAPH_INGEST_URL=<your-metergraph-ingest-url>
export METERGRAPH_REPOSITORY=owner/repository
```

## Real-world setup

The exporter must be attached to whichever tracer provider carries Langfuse's
spans. There are two routes, depending on how your application configures
Langfuse:

**Route 1 — Langfuse uses the global tracer provider.** When Langfuse
attaches to the globally registered OpenTelemetry `TracerProvider`, add
MeterGraph to that same provider:

```python
from opentelemetry import trace
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from metergraph.opentelemetry import MetergraphGenAIExporter

trace.get_tracer_provider().add_span_processor(
    BatchSpanProcessor(MetergraphGenAIExporter())
)
```

**Route 2 — pass Langfuse an explicit provider.** When you construct the
Langfuse client with your own `tracer_provider`, register MeterGraph's
processor on it before handing it over:

```python
from langfuse import Langfuse
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from metergraph.opentelemetry import MetergraphGenAIExporter

provider = TracerProvider()
provider.add_span_processor(BatchSpanProcessor(MetergraphGenAIExporter()))
langfuse = Langfuse(tracer_provider=provider)
```

**Verify the attachment against your Langfuse SDK version.** This is the
fiddliest step: whether Langfuse uses the global provider or an internal one
has changed across v3/v4 releases and configurations. After wiring it up,
make one traced call and confirm a row reaches MeterGraph; if nothing
arrives, the exporter is on a different provider than Langfuse's spans.

Only spans with `langfuse.observation.type: generation` become capture rows;
other observation types (spans, events) are skipped and counted in the
public `exporter.skipped` dict. To restrict the exporter to Langfuse's spans
on a shared provider, pass `include_scopes=` / `exclude_scopes=` (matched
against `span.instrumentation_scope.name`; exclude wins) — print
`span.instrumentation_scope.name` or watch `exporter.skipped["scope"]` to
discover the exact scope name your installed Langfuse version emits.

## Run the offline demo

```bash
python main.py
```

[`main.py`](main.py) needs no Langfuse keys, no provider key, and no network:
it builds a plain `TracerProvider`, attaches `MetergraphGenAIExporter`, and
emits the same `langfuse.observation.*`-attribute spans the Langfuse SDK
records for one generation. Open it and search for `MeterGraph integration` —
the only integration block is the `add_span_processor` call; everything else
is synthetic demo traffic.

## What MeterGraph captures

The exporter maps Langfuse observation attributes — the model name,
`usage_details` token counts, observation input/output, completion start time
(as TTFT), session id, trace name, and ERROR level — onto the same capture
rows as provider-client capture, preserving OpenTelemetry trace identity,
latency, and error status. Message content is subject to the SDK's normal
scrub/redact/truncate policy; set `METERGRAPH_CAPTURE_TEXT=0` for
metadata-only capture.
