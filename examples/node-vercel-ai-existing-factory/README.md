# Instrument an existing AI SDK model factory

Use this pattern when your application already has a function that selects
among providers or models. Do not introduce a custom factory solely for
MeterGraph; use the [provider-registry example](../node-vercel-ai-registry/)
for a new multi-provider integration. This example requires Node.js 22+ and
AI SDK 7.

## What MeterGraph changes

Open [`main.mjs`](main.mjs) and search for `MeterGraph integration`. The
existing `createBaseModel()` switch and application middleware remain intact.
MeterGraph adds only:

1. The `metergraph` import and one `vercelAISDKMiddleware(...)` instance.
2. `createInstrumentedModel()`, which wraps the factory's controlled exit.
3. An optional `mg.track(...)` operation name for stable attribution.
4. `flush()` and `shutdown()` in the application's shutdown path.

Put the wrapper at the factory exit rather than every call site. That makes a
new provider branch observable automatically and avoids relying on every
caller to remember instrumentation.

## Run it

From this directory:

```bash
npm install
export METERGRAPH_APP_TOKEN=<token>

OPENAI_API_KEY=... node main.mjs openai:gpt-5-mini
ANTHROPIC_API_KEY=... node main.mjs anthropic:claude-haiku-4-5
AI_GATEWAY_API_KEY=... node main.mjs gateway:anthropic/claude-sonnet-4.5
COMPATIBLE_BASE_URL=http://localhost:11434/v1 node main.mjs compatible:llama3.2:latest
```

Replace `owner/repository` with your repository identity. In this repository,
`npm install` builds the local MeterGraph SDK. In your application, install the
published `metergraph` package together with your AI SDK provider packages.

The middleware array demonstrates composition with existing application
middleware. MeterGraph is last so it observes the provider-bound model after
the default-settings middleware. Add `mg.track("stable.operation", fn)` around
meaningful operations when you want precise route attribution.

AI SDK 5 uses `vercelAISDKMiddleware({ aiSdkVersion: 5, ... })`; AI SDK 6 and 7
use the default middleware protocol.

Reference: [`wrapLanguageModel`](https://ai-sdk.dev/docs/reference/ai-sdk-core/wrap-language-model).
