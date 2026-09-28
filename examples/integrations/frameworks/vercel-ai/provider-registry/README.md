# Instrument an AI SDK provider registry

Use this pattern when your application already uses—or can adopt—the AI SDK's
`createProviderRegistry()`. It is the recommended multi-provider pattern for
new and adaptable applications. The example requires Node.js 22+ and AI SDK 7.

## What MeterGraph changes

Open [`main.mjs`](main.mjs) and search for `MeterGraph integration`. The
application's provider setup and model selection remain normal AI SDK code.
MeterGraph adds only:

1. The `metergraph` import.
2. `languageModelMiddleware: mg.vercelAISDKMiddleware(...)` on the registry.
3. An optional `mg.track(...)` operation name for stable attribution.
4. `flush()` and `shutdown()` in the application's shutdown path.

The middleware option covers every language model returned by this registry,
so a newly registered provider cannot accidentally bypass MeterGraph.

## Run it

From this directory:

```bash
npm install
export METERGRAPH_APP_TOKEN=<token>

OPENAI_API_KEY=... node main.mjs openai:gpt-5-mini
ANTHROPIC_API_KEY=... node main.mjs anthropic:claude-haiku-4-5
AI_GATEWAY_API_KEY=... node main.mjs gateway:anthropic/claude-sonnet-4.5
COMPATIBLE_BASE_URL=http://localhost:11434/v1 node main.mjs compatible:gemma3
```

Replace `owner/repository` with your repository identity. In this repository,
`npm install` builds the local MeterGraph SDK. In your application, install the
published `metergraph` package together with your AI SDK provider packages.

Registry-wide instrumentation maximizes coverage. Since model construction is
centralized, add `mg.track("stable.operation", fn)` around meaningful
application operations when you want more precise route attribution.

AI SDK 5 uses `vercelAISDKMiddleware({ aiSdkVersion: 5, ... })`; AI SDK 6 and 7
use the default middleware protocol.

References: [provider registry](https://ai-sdk.dev/docs/reference/ai-sdk-core/provider-registry)
and [language-model middleware](https://ai-sdk.dev/docs/ai-sdk-core/middleware).
