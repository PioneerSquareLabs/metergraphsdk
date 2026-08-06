# Metergraph SDKs

Capture SDKs for [Metergraph](https://github.com/PioneerSquareLabs/metergraph), which tracks LLM costs by application function and trace. Wrap your OpenAI, Anthropic, Gemini, or Python Vercel AI Gateway client—or add Metergraph middleware to a TypeScript Vercel AI SDK language model—and every call is attributed to the function that made it, with token counts (input/output, cache reads, aggregate and TTL-specific cache writes, reasoning), latency, and model. SDK rows contain usage counters, never embedded prices or client-computed cost. SDK 0.3 sends scrubbed request and normalized response content to the hosted service by default; applications can opt out globally or around a sensitive route or trace. The SDKs have no runtime dependencies.

| Package | Registry | Source |
|---|---|---|
| `metergraph` | PyPI | [`python/`](python) |
| `metergraph` | npm | [`typescript/`](typescript) |

## Set up with an AI coding agent

The fastest way to instrument an existing codebase is to paste this into Claude Code, Codex, Cursor, or whatever agent you use, from inside the repo you want instrumented. It covers both the Python and TypeScript SDKs:

```text
Instrument this codebase's LLM API costs with Metergraph
(https://github.com/PioneerSquareLabs/metergraphsdk). It captures per-call
token usage (in/out, cached, reasoning), latency, model, scrubbed request, and
normalized response, attributed to the application function and logical trace
that made the call. Set METERGRAPH_CAPTURE_TEXT=0 for metadata-only capture.

1. Install the SDK: `pip install metergraph` (Python) or `npm install
   metergraph` (TypeScript/JavaScript). Zero runtime dependencies.
2. Find every place an OpenAI, Anthropic, or Google Gemini client is
   constructed (OpenAI()/AsyncOpenAI(), Anthropic()/AsyncAnthropic(),
   genai.Client(), new OpenAI(), new Anthropic(), new GoogleGenAI()) and wrap
   it in place:
   - Python: `client = metergraph.wrap(OpenAI())` after `import metergraph`
   - TypeScript: `const client = mg.wrap(new OpenAI())` after
     `import * as mg from "metergraph"`
   wrap() returns the same client and initializes itself from the environment.
   Do not change any call sites, arguments, or error handling; streaming and
   async work unchanged.
   Python OpenAI/Anthropic clients whose base URL is
   https://ai-gateway.vercel.sh are Vercel AI Gateway clients and are detected
   automatically. Preserve their AI_GATEWAY_API_KEY / VERCEL_OIDC_TOKEN and
   creator-qualified model IDs; Metergraph must never capture those secrets.
   For Vercel AI SDK `generateText` / `streamText` calls, wrap each language
   model with `wrapLanguageModel({ model, middleware:
   mg.vercelAISDKMiddleware() })` instead of wrapping a provider client.
3. Configuration is env-var based: METERGRAPH_APP_TOKEN is required (capture
   is silently off without it), and METERGRAPH_INGEST_URL is only needed when
   self-hosting the server from https://github.com/PioneerSquareLabs/metergraph.
   Add both to .env.example or the deployment config; never commit a real
   token.
4. Attribution:
   - Python: automatic via stack walk. Optionally decorate key LLM-calling
     functions with @metergraph.track to pin a stable name.
   - TypeScript: wrap each LLM-calling function with
     mg.track("stable.name", fn), because stack-based attribution is
   unreliable under bundlers. Do this for every function that calls a
   wrapped client.
5. Wrap multi-call operations in metergraph.trace("stable-name") in Python or
   mg.trace("stable-name", fn) in TypeScript. Use capture_text=False /
   captureText: false around sensitive operations.
6. Serverless only (Lambda / Cloudflare Workers / Vercel): ensure delivery
   before the runtime freezes. Wrap handlers with mg.wrapHandler(handler),
   or call mg.bindWaitUntil(ctx) once per request, or await mg.flush() before
   returning. Long-running servers and scripts need nothing extra.
7. The SDK is fail-open: transport problems never break or slow LLM calls, so
   do not add defensive try/except around wrapping or the wrapped calls.

When done, list every client and Vercel AI SDK model you instrumented and where,
and flag any LLM calls made through other paths, since those are not captured.
```

## Python

```bash
pip install metergraph
export METERGRAPH_APP_TOKEN=<token>
```

Setup is one line per client. `wrap()` reads `METERGRAPH_APP_TOKEN` from the environment and starts capture on its own:

```python
import metergraph

# OpenAI
from openai import OpenAI
openai_client = metergraph.wrap(OpenAI())

# Anthropic
from anthropic import Anthropic
anthropic_client = metergraph.wrap(Anthropic())

# Gemini
from google import genai
gemini_client = metergraph.wrap(genai.Client())
```

Vercel's Python integration is AI Gateway through the official OpenAI or
Anthropic client (the `ai` middleware package itself is TypeScript). Metergraph
detects the public gateway URL automatically and uses the `creator/model`
prefix for catalog pricing:

```python
import os
from openai import OpenAI

gateway = metergraph.wrap(OpenAI(
    api_key=os.getenv("AI_GATEWAY_API_KEY") or os.getenv("VERCEL_OIDC_TOKEN"),
    base_url="https://ai-gateway.vercel.sh/v1",
))

gateway.chat.completions.create(
    model="anthropic/claude-sonnet-4.6",
    messages=[{"role": "user", "content": "Hello"}],
)
```

Sync, async, streaming, tool calls, and Responses API requests use the same
capture path. For a compatible client behind a custom gateway URL, pass
`provider="vercel"` to `metergraph.wrap()`.

Then use the wrapped client exactly as before:

```python
@metergraph.track
def summarize_invoice(invoice):
    return openai_client.chat.completions.create(model="gpt-5.6-luna", messages=[...])
```

Attribution is automatic in Python: the SDK walks the stack to the nearest application function. `@metergraph.track` pins an explicit, stable name instead. Sync and async clients both work, streaming included. To configure in code rather than env vars, call `metergraph.init(token=..., ...)` before the first `wrap()`.

Use `with metergraph.trace("checkout"):` or the equivalent decorator to group
multiple provider calls. Set `capture_text=False` on `init()`, `route()`, or
`trace()` when an operation must remain metadata-only.

## TypeScript / JavaScript

```bash
npm install metergraph
export METERGRAPH_APP_TOKEN=<token>
```

The same one-line setup applies; `wrap()` initializes from the environment:

```ts
import * as mg from "metergraph";

// OpenAI
import OpenAI from "openai";
const openai = mg.wrap(new OpenAI());

// Anthropic
import Anthropic from "@anthropic-ai/sdk";
const anthropic = mg.wrap(new Anthropic());

// Gemini
import { GoogleGenAI } from "@google/genai";
const gemini = mg.wrap(new GoogleGenAI({}));
```

```ts
const summarizeInvoice = mg.track("billing.summarize_invoice", async (invoice) => {
  return openai.chat.completions.create({ model: "gpt-5.6-luna", messages: [...] });
});
```

In TypeScript, use `track()` for attribution. It stays reliable across bundlers and minifiers, where stack parsing does not. Provider SDKs and the Vercel AI SDK are optional peer dependencies, and Metergraph itself has no runtime dependencies. To configure in code, call `mg.init({ token, ... })` before the first `wrap()`.

Use `await mg.trace("checkout", async () => { ... })` to group multiple calls,
and pass `{ captureText: false }` for a metadata-only operation.

Vercel AI SDK models use the same capture and trace path through middleware:

```ts
import { generateText, wrapLanguageModel } from "ai";
import { openai } from "@ai-sdk/openai";

const model = wrapLanguageModel({
  model: openai("gpt-5.6-luna"),
  middleware: mg.vercelAISDKMiddleware(),
});

await mg.trace("support-answer", () =>
  generateText({ model, prompt: "Help this customer" })
);
```

Swap in a Vercel AI Gateway model the same way, with no other changes:

```ts
import { gateway } from "ai";

const model = wrapLanguageModel({
  model: gateway("anthropic/claude-sonnet-4.5"),
  middleware: mg.vercelAISDKMiddleware(),
});
```

Each provider request in a multi-step AI SDK tool loop becomes its own costed
span. Provider options and transport headers are excluded from capture.

| Vercel AI SDK | Metergraph middleware | Node.js |
|---|---|---|
| 5 | `vercelAISDKMiddleware({ aiSdkVersion: 5 })` | 18+ |
| 6 | `vercelAISDKMiddleware()` | 18+ |
| 7 | `vercelAISDKMiddleware()` | 22+ |

Metergraph itself supports Node.js 18+; the AI SDK version you choose may
require a newer runtime. See [`typescript/README.md`](typescript/README.md)
for the `specificationVersion` advanced/backward-compatibility option.

## Where the data goes

```bash
export METERGRAPH_INGEST_URL=http://localhost:8787   # your self-hosted server
export METERGRAPH_APP_TOKEN=<token>
```

Point `METERGRAPH_INGEST_URL` at a [self-hosted Metergraph server](https://github.com/PioneerSquareLabs/metergraph), or leave it unset to use the hosted service. Without a token, capture is off entirely; the SDK never sends anything silently. Hosted SDK 0.3 capture includes scrubbed request and response content by default, independently capped at 100 KiB; the public self-hosted server discards content even when the SDK sends it. Transport problems never break or slow your LLM calls either. When the collector is unreachable, capture drops and your application carries on.

See [`examples/`](examples) for runnable per-provider examples, including an offline fake-provider demo that needs no API keys.

## License

[Apache-2.0](LICENSE)
