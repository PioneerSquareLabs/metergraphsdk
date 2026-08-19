# Metergraph SDKs

Capture SDKs for [Metergraph](https://github.com/PioneerSquareLabs/metergraph), which tracks LLM costs by application function and trace. Wrap your OpenAI, Anthropic, Gemini, or Python Vercel AI Gateway client—or add Metergraph middleware to a TypeScript Vercel AI SDK language model—and every call is attributed to the function that made it, with token counts (input/output, cache reads, aggregate and TTL-specific cache writes, reasoning), latency, and model. SDK rows contain usage counters, never embedded prices or client-computed cost. Metergraph sends scrubbed request and normalized response content to the hosted service by default; applications can opt out globally or around a sensitive route or trace. The SDKs have no runtime dependencies.

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
3. METERGRAPH_APP_TOKEN is required; the SDK warns and disables capture when
   it is missing. METERGRAPH_INGEST_URL is only needed when
   self-hosting the server from https://github.com/PioneerSquareLabs/metergraph.
   Document variable names with placeholders in `.env.example`, and put real
   values only in the deployment configuration. Repository identity enables
   repository-level attribution and [MeterGraph Bot](https://github.com/apps/metergraph).
   Configure it using any one of these sufficient options: the `repository`
   option to `init()` (`repository="owner/repository"` in Python or
   `{ repository: "owner/repository" }` in TypeScript),
   `METERGRAPH_REPOSITORY`, or a read-only
   `.metergraph/config.json` file containing `{"repository":"owner/repository"}`.
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

Initialize Metergraph once, then wrap each provider client. The token remains
in the environment:

```python
import metergraph

metergraph.init(repository="owner/repository")

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

Initialization is process-wide in both SDKs. The first `init()` configuration
remains active; later explicit calls are ignored and produce one generic
warning without option names, token values, or other secrets.

Vercel's Python integration is AI Gateway through the official OpenAI or
Anthropic client (the `ai` middleware package itself is TypeScript). Metergraph
detects the public gateway URL automatically and uses the `creator/model`
prefix for catalog pricing:

```python
import os
import metergraph
from openai import OpenAI

metergraph.init(repository="owner/repository")

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

Initialize Metergraph once, then wrap each provider client. The token remains
in the environment:

```ts
import * as mg from "metergraph";

mg.init({ repository: "owner/repository" });

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

In TypeScript, use `track()` for attribution. It stays reliable across bundlers and minifiers, where stack parsing does not. Provider SDKs and the Vercel AI SDK are optional peer dependencies, and Metergraph itself has no runtime dependencies. To configure in code, call `mg.init({ repository: "owner/repository", token, ... })` before the first `wrap()`.

Use `await mg.trace("checkout", async () => { ... })` to group multiple calls,
and pass `{ captureText: false }` for a metadata-only operation.

Vercel AI SDK models use the same capture and trace path through middleware:

```ts
import { generateText, wrapLanguageModel } from "ai";
import { openai } from "@ai-sdk/openai";

const model = wrapLanguageModel({
  model: openai("gpt-5.6-luna"),
  middleware: mg.vercelAISDKMiddleware({ repository: "owner/repository" }),
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
The middleware initializes Metergraph itself; pass `init()` options to it as
shown above, or call `mg.init()` first in applications with centralized setup.
For multi-provider applications, use the [examples chooser](examples/README.md)
to select the provider-registry pattern or instrument a custom factory that
your application already has. Each example clearly marks original application
code versus MeterGraph additions.

For concurrent Node workers, bind request identity to a callback instead of
mutable process state:

```ts
await mg.withContext({
  sessionId: runId,
  tags: { customer: customerId },
}, async () => {
  await runJob();
});
```

`withSession()` and `withTags()` provide narrower forms. Context follows async
work created inside the callback and is restored afterward.

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

Point `METERGRAPH_INGEST_URL` at a [self-hosted Metergraph server](https://github.com/PioneerSquareLabs/metergraph), or leave it unset to use the hosted service. Without a token, capture is off and the SDK emits a warning. Hosted capture includes scrubbed request and response content by default, independently capped at 100 KiB; the public self-hosted server discards content even when the SDK sends it. Transport problems never break or slow your LLM calls either. When the collector is unreachable, capture drops and your application carries on.

See [`examples/`](examples) for runnable per-provider examples, including an offline fake-provider demo that needs no API keys.

## Batch-first execution (opt-in, not part of default capture)

Both SDKs also expose an explicit, separately opt-in `batchFirst()` / `batch_first()` API: submit one request through a provider's Batch API, wait up to a caller-chosen deadline, and fall back to exactly one direct call if the batch hasn't finished in time. This is a distinct code path from `wrap()`/capture — never enabled by `wrap()`, by default configuration, or by any environment variable — and it carries real cost and behavioral consequences a caller must accept explicitly before any provider call is made:

- **Duplicate execution and cost on a missed deadline.** The batch request is always submitted first; if it hasn't reached a terminal state by the deadline, `batchFirst()` also issues a direct call while the batch keeps running — the same prompt can be billed and executed twice. Both SDKs require an explicit, non-defaulted acknowledgement of this (`acceptDuplicateProviderExecution: true` in TypeScript, `accept_duplicate_provider_execution=True` in Python).
- **Streaming is not supported.** A request with `stream: true` / `stream=True` is rejected before any provider call.
- **Tool calls need a separate acknowledgement.** The batch result and the direct fallback are independent provider executions of the same prompt and may each choose a *different* tool-call plan. A request carrying `tools` is rejected unless the caller also sets `allowDuplicateToolCallPlans: true` / `allow_duplicate_tool_call_plans=True` — a caller whose tools have side effects must not assume the two plans agree. Neither path executes a tool call automatically; the caller receives the tool-call plan in the returned result and remains responsible for executing it, exactly as with a normal (non-batch-first) provider response.
- **Exactly one canonical result, ever.** Whichever result — batch or direct — settles first is the only one ever returned or executed; a batch result that arrives after a direct fallback already won is never returned, never executed, and never mutates the already-returned result. A losing batch's eventual outcome is observable only through an async, best-effort `onLateBatchSettled` / `on_late_batch_settled` callback, and only as whether it happened to contain a tool-call plan — never its content.
- **Long-lived-process assumptions differ by language, and neither is fully solved yet.** In TypeScript, the background poll that watches a losing batch for late telemetry uses an ordinary (non-unref'd) timer, so it keeps a Node process alive until the batch reaches a terminal state — which can take up to 24 hours. In Python, the equivalent background poll runs on a daemon thread, which does *not* keep the process alive — a short-lived script may exit before `on_late_batch_settled` ever fires, silently dropping that signal. Both are consequences of this feature only having been designed against a long-running-server assumption so far, not yet against short-lived scripts or serverless invocations.

Adapters exist for OpenAI, Anthropic, and Google Gemini in both SDKs, built against each provider's documented/introspected current Batch API shape and covered by fake-client contract tests — but **none of the three has been exercised against a live provider Batch API from either SDK**. Treat this as a beta-quality, code-reviewed-but-not-live-verified surface. See [`typescript/README.md`](typescript/README.md) and [`python/README.md`](python/README.md) for exact API shapes and examples.

## License

[Apache-2.0](LICENSE)
