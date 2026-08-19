# Vercel AI SDK factories

These AI SDK 7 examples show two ways to instrument a multi-provider model
factory without wrapping each provider's internal HTTP client. They require
Node.js 22+.

Install from this directory:

```bash
npm install
export METERGRAPH_APP_TOKEN=<token>
```

In this repository, `npm install` also installs and builds the local Metergraph
SDK so the examples exercise the current checkout. In an application, install
the published `metergraph` package with the AI SDK provider packages instead.

Replace `owner/repository` in each example with your repository identity.

## Provider registry (recommended for adaptable codebases)

`provider-registry.mjs` applies Metergraph once through the AI SDK's
`languageModelMiddleware` option. Every language model obtained from the
registry is instrumented:

```bash
OPENAI_API_KEY=... node provider-registry.mjs openai:gpt-5-mini
ANTHROPIC_API_KEY=... node provider-registry.mjs anthropic:claude-haiku-4-5
AI_GATEWAY_API_KEY=... node provider-registry.mjs gateway:anthropic/claude-sonnet-4.5
COMPATIBLE_BASE_URL=http://localhost:11434/v1 \
  node provider-registry.mjs compatible:gemma3
```

Registry-wide middleware maximizes coverage and avoids an uninstrumented
factory branch. Because wrapping happens centrally, source-stack attribution
may point to the factory. Use `mg.track("stable.operation", fn)` around the
calling operation, as the example does, for precise application attribution.

## Existing factory

`existing-factory.mjs` keeps an existing switch-based factory and wraps its
single exit. It handles the AI SDK's broad `LanguageModel` union: a
`creator/model` string is resolved through `gateway()` before being passed to
`wrapLanguageModel()`.

```bash
OPENAI_API_KEY=... node existing-factory.mjs openai:gpt-5-mini
ANTHROPIC_API_KEY=... node existing-factory.mjs anthropic:claude-haiku-4-5
AI_GATEWAY_API_KEY=... node existing-factory.mjs gateway:anthropic/claude-sonnet-4.5
COMPATIBLE_BASE_URL=http://localhost:11434/v1 \
  node existing-factory.mjs compatible:gemma3
```

The middleware array demonstrates coexistence with application middleware.
AI SDK applies middleware in array order; Metergraph is last so it wraps the
provider-bound model after the default-settings middleware.

For teams that intentionally instrument only selected calls, set
`INSTRUMENT_AT_CALL_SITE=1`. This uses the same existing factory but applies
`wrapLanguageModel()` at the call site. It provides narrower coverage and can
make ownership clearer, at the cost of requiring every applicable caller to
remember instrumentation.

AI SDK 5 and 6 use the same patterns with their compatible provider-package
versions. Pass `{ aiSdkVersion: 5 }` to `vercelAISDKMiddleware()` on AI SDK 5;
AI SDK 6 and 7 use the default middleware protocol.

References: [provider registry](https://ai-sdk.dev/docs/reference/ai-sdk-core/provider-registry),
[`wrapLanguageModel`](https://ai-sdk.dev/docs/reference/ai-sdk-core/wrap-language-model),
and [language-model middleware](https://ai-sdk.dev/docs/ai-sdk-core/middleware).
