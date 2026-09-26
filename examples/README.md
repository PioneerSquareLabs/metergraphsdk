# Examples

Start with the customer workflow you want to instrument. The workflow examples
show how traces and routes map to product behavior. The integration examples
then show provider, framework, and telemetry-specific setup.

Setup for all of them:

```bash
export METERGRAPH_INGEST_URL=http://localhost:8787   # your self-hosted server
export METERGRAPH_APP_TOKEN=dev-token                # one of MG_TOKENS
```

The one-shot examples call `shutdown()` when they finish. `shutdown()` delivers
queued events and stops MeterGraph's background work, so a separate `flush()` is
not needed. In a long-running service, call `shutdown()` from the application's
normal process-shutdown hook. Use `flush()` only when you need to deliver queued
events while keeping MeterGraph running.

| Example | Needs |
|---|---|
| `test-fixtures/fake-providers/run_e2e.py` | Nothing required. Offline demo traffic |
| `integrations/providers/openai/` | Native OpenAI wrappers for Python and TypeScript |
| `integrations/providers/anthropic/` | Native Anthropic wrappers for Python and TypeScript |
| `integrations/providers/gemini/` | Native Gemini wrapper for TypeScript |
| `integrations/providers/openrouter/` | OpenRouter gateway examples for Python and TypeScript |
| `integrations/frameworks/vercel-ai/` | Vercel AI SDK middleware patterns |
| `integrations/telemetry/` | Langfuse, LiteLLM, Phoenix, and Bedrock/Azure OpenTelemetry |
| `execution/batch-first/` | Opt-in Batch API with a deadline and direct fallback |

## Workflows

For a customer-perspective workflow, start with
[`workflows/content-generation/draft-review/`](workflows/content-generation/draft-review/).
It shows one `trace()` containing three calls while separate `route()` scopes
keep the draft and review workloads distinct.

`integrations/frameworks/vercel-ai/direct/main.mjs` uses AI SDK 7 and therefore
requires Node.js 22+. It wraps a language model with
`mg.vercelAISDKMiddleware()` instead of a provider client. It calls a direct
OpenAI model by default, or the Vercel AI Gateway when `AI_GATEWAY_API_KEY` is
set:

```bash
npm i metergraph ai @ai-sdk/openai
OPENAI_API_KEY=... node examples/integrations/frameworks/vercel-ai/direct/main.mjs
AI_GATEWAY_API_KEY=... node examples/integrations/frameworks/vercel-ai/direct/main.mjs
```

## Choose a multi-provider example

| Your application | Start here | MeterGraph integration point |
|---|---|---|
| Calls one provider or model directly | [`direct/`](integrations/frameworks/vercel-ai/direct/) | Wrap that model with `vercelAISDKMiddleware()` |
| Uses or can adopt `createProviderRegistry()` | [`provider-registry/`](integrations/frameworks/vercel-ai/provider-registry/) | Add middleware once to the registry |
| Already has a custom model factory | [`existing-factory/`](integrations/frameworks/vercel-ai/existing-factory/) | Wrap the factory's controlled exit |

Use the provider registry for new multi-provider integrations. Do not create a
custom factory solely for MeterGraph. Each detailed README identifies the
original application code and every MeterGraph addition; the runnable source
uses matching `MeterGraph integration` comments.

## OpenRouter (OpenAI-compatible gateway)

[`integrations/providers/openrouter/python/`](integrations/providers/openrouter/python/)
and [`integrations/providers/openrouter/typescript/`](integrations/providers/openrouter/typescript/)
wrap an ordinary OpenAI client pointed at OpenRouter. `https://openrouter.ai`
is auto-detected; a trusted custom domain uses the `gateway="openrouter"`
override. Each README explains requested versus served model, reported versus
catalog cost, final streaming usage, and the BYOK limitation.

## Batch-first execution

[`execution/batch-first/`](execution/batch-first/) is a separately opt-in
execution example, not a capture example. Use it only when the request may run
through a provider Batch API and you explicitly accept that a deadline fallback
can execute and bill the same request twice.

## LiteLLM with OpenTelemetry

[`integrations/telemetry/litellm/python/`](integrations/telemetry/litellm/python/)
attaches MeterGraph as LiteLLM's custom OpenTelemetry exporter. Use it when
LiteLLM already emits GenAI semantic-convention spans; do not also wrap the
same calls with `metergraph.wrap()`.
