# Instrumentation coverage contract

This document is the source of truth for the Dropzone instrumentation coverage
project. A row is not supported because a client happens to look compatible.
It is supported only when the package anchor, entry point, capture fields, and
unsupported-path behavior below are covered by the SDK test matrix.

## Capture contract

Every supported call must produce one Metergraph row with these fields when the
provider supplies them:

| Field | Contract |
| --- | --- |
| Provider | Canonical provider identity. Gateway or proxy identity is separate from the direct provider when the path exposes both. |
| Model | The requested model, plus the provider response model when the provider returns one. |
| Route | Explicit `metergraph.route`, prompt name, framework route, or the documented operation fallback, with `route_source` declaring `explicit` for a name the developer chose and `derived` for a fallback. |
| Usage | Input and output tokens. Cache and reasoning counters remain distinct when the source exposes them. |
| Timing | Start timestamp and call latency. |
| Trace identity | Trace and span identity when the source emits it. Wrapped clients receive deterministic single-call identity when no parent trace exists. |
| Content | Scrubbed request and normalized response only when capture is enabled and the source provides content. Missing content is not inferred. |
| Failure | Provider errors remain visible as an error row without changing the exception or provider result observed by the application. |

An integration must preserve the host application's behavior. Capture is
fail-open: an unsupported client, missing optional dependency, exporter error,
or malformed optional attribute cannot make the provider call fail.

## Provider and framework matrix

The package anchors are compatibility test anchors, not a promise that every
future release has identical internals. A dependency outside an anchor must be
verified before it is added to the supported range.

| Path | Package anchor | Entry point | Status | Required evidence |
| --- | --- | --- | --- | --- |
| OpenAI Python | `openai>=2.50,<3` | `metergraph.wrap(OpenAI())` and `AsyncOpenAI()` | Supported | Chat Completions, Responses, streaming, parse, and Batch API fixtures. |
| OpenAI TypeScript | `openai>=4,<8` | `mg.wrap(new OpenAI())` | Supported | Chat Completions, Responses, streaming, and package type checks. |
| Anthropic Python | `anthropic>=0.40,<1` | `metergraph.wrap(Anthropic())` and `AsyncAnthropic()` | Supported | Messages, streaming, and Batch API fixtures. |
| Anthropic TypeScript | `@anthropic-ai/sdk>=0.30` | `mg.wrap(new Anthropic())` | Supported | Messages and streaming fixtures. |
| Google Gemini Python | `google-genai>=1` | `metergraph.wrap(genai.Client())` | Supported | Sync and async `generate_content` fixtures. |
| Google Gemini TypeScript | `@google/genai>=1` | `mg.wrap(new GoogleGenAI())` | Supported | Generated-content and streaming fixtures. |
| Vercel AI Gateway through Python clients | OpenAI or Anthropic anchor above; public URL `https://ai-gateway.vercel.sh/v1` | `metergraph.wrap(client)` or explicit `provider="vercel"` for a trusted custom URL | Supported | Creator-qualified model keeps gateway pricing identity while the provider call remains fail-open. |
| Vercel AI SDK TypeScript | `ai>=5,<8` with the installed provider package | `mg.vercelAISDKMiddleware()` through `wrapLanguageModel()` | Supported | AI SDK v5 and current type and integration fixtures. |
| OpenRouter through OpenAI clients | OpenAI anchor above; public URL `https://openrouter.ai/api/v1` | Automatic host detection or `gateway="openrouter"` for a trusted custom URL | Supported | Requested model, served model, and reported-cost evidence remain distinct. |
| LiteLLM OpenTelemetry exporter, legacy shape | `litellm[proxy]>=1.96.2,<1.101` | `MetergraphGenAIExporter` registered as LiteLLM's exporter | Project target | `gen_ai.system` and LiteLLM metadata provider fallback, cache counters, route identity, and actionable skip diagnostics. |
| LiteLLM OpenTelemetry exporter, current shape | `litellm[proxy]>=1.101,<2` with `LITELLM_OTEL_V2` enabled | Same exporter registration | Project target | `gen_ai.provider.name`, cache counters, content, proxy identity, and no inferred-channel requirement when provider data is present. |
| Amazon Bedrock GenAI spans | `opentelemetry-instrumentation-bedrock>=0.49b0,<1` | Standard OpenTelemetry GenAI exporter path | Supported | `aws.bedrock`, `aws`, and canonical Bedrock provider spellings map to `bedrock`; model, route, usage, timing, and unsupported configuration diagnostics are verified. |
| Azure OpenAI | Python `openai>=2.50,<3`; TypeScript `openai>=4,<8` | Azure endpoint configuration through the OpenAI-compatible client entry point | Supported | Azure endpoint configuration is recognized as Azure, while model, route, usage, timing, and errors retain the normal OpenAI-compatible contract. |
| OpenInference / Phoenix | `opentelemetry-sdk>=1.30` plus the installed OpenInference instrumentor | `MetergraphGenAIExporter` on the existing tracer provider | Supported | `LLM` spans map to provider, model, usage, messages, and output; non-LLM spans increment an explicit skip reason. |
| Langfuse | Langfuse SDK v3/v4 with `opentelemetry-sdk>=1.30` | `MetergraphGenAIExporter` on the tracer provider used by the Langfuse client | Supported | Generation observations map to usage, content, session, trace name, and errors; other observations are skipped. |
| LangSmith | LangSmith OpenTelemetry spans with `opentelemetry-sdk>=1.30` | `MetergraphGenAIExporter` on the shared tracer provider, with `LANGSMITH_TRACING_MODE=otel` or `hybrid` | Supported | LLM runs map to model, provider, usage, content, and errors; non-LLM runs are skipped. |

The Bedrock anchor is the OpenTelemetry Python instrumentor range exercised by
the provider fixtures. Azure uses the same OpenAI-compatible client anchors as
the existing direct-provider paths, with the endpoint and API-version
configuration called out separately so an Azure deployment is not mistaken for
the public OpenAI service.

## Unsupported paths and diagnostics

| Surface | Unsupported behavior |
| --- | --- |
| Wrapped client | Return the original client, leave the provider call untouched, and emit one bounded diagnostic. Never claim a captured row. |
| OpenTelemetry exporter | Return export success to the host tracer, increment `exporter.skipped` with a stable reason such as `scope`, `not-genai`, `ineligible-kind`, or `no-model`, and never raise into the host application. |
| Missing optional package | Keep the base package importable. The optional integration is documented as unavailable until its extra and version anchor are installed. |
| Unsupported version or configuration | Do not silently downgrade to a different provider. Record the unsupported fixture and give the user the supported anchor and configuration required for capture. |
| Malformed optional attributes | Preserve model and usage metadata when possible, mark degraded parsing in diagnostics, and never discard a valid provider call because content is malformed. |

## Verification requirements

Each matrix row becomes supported only after it has:

1. A focused unit or dialect test covering provider, model, route, usage, and
   failure behavior.
2. A real-client or real-exporter shape test when an upstream package is
   available without network credentials.
3. A runnable example or documented configuration when the integration is
   user-facing.
4. An unsupported-path fixture that proves the diagnostic is explicit and
   fail-open.

MET-210 owns the end-to-end execution of this matrix. It must report each row
as pass, fail, or intentionally unsupported, rather than collapsing missing
coverage into a generic success.
