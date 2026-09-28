# Bedrock and Azure OpenTelemetry GenAI paths

The MeterGraph OpenTelemetry exporter accepts the standard GenAI spans emitted
by Bedrock instrumentation and Azure OpenAI-compatible clients. It canonicalizes
Bedrock provider spellings such as `aws.bedrock` and `aws` to `bedrock`, and
Azure spellings such as `azure.openai` to `azure`.

## Bedrock

Install the optional exporter and the pinned instrumentor range:

```bash
python -m pip install 'metergraph[otel]' 'opentelemetry-instrumentation-bedrock>=0.49b0,<1'
```

Attach `MetergraphGenAIExporter` to the tracer provider used by the
instrumentation. The provider call remains unchanged. Model, operation, token
usage, timing, errors, and GenAI content are captured when the instrumentor
emits those fields.

## Azure OpenAI

Use the existing OpenAI-compatible entry point with the Azure endpoint and API
version configured by the provider client. Python applications use
`openai>=2.50,<3`; TypeScript applications use `openai>=4,<8`. The endpoint is
the supported configuration boundary. Do not pass a public OpenAI endpoint and
claim Azure attribution.

## Unsupported configurations

The exporter is fail-open. A missing optional instrumentor, an unsupported
client version, a non-Azure endpoint, or a span without a model never changes
the provider result. Install the pinned package range and use the documented
endpoint configuration. A span without a model is returned as exporter skip
reason `no-model`; ordinary non-GenAI spans use `not-genai`.
