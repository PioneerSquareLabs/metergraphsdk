# Metergraph SDKs

Zero-dependency capture SDKs for [Metergraph](https://github.com/PioneerSquareLabs/metergraph) — LLM cost tracking by function. Wrap your OpenAI, Anthropic, or Gemini client and every call is attributed to the application function that made it, with token counts (in/out, cached, reasoning), latency, and model — **never prompt or completion content** unless you explicitly opt in against the hosted service.

| Package | Registry | Source |
|---|---|---|
| `metergraph` | PyPI | [`python/`](python) |
| `metergraph` | npm | [`typescript/`](typescript) |

## Python

```bash
pip install metergraph
export METERGRAPH_APP_TOKEN=<token>
```

Setup is one line per client — `wrap()` reads `METERGRAPH_APP_TOKEN` from the environment and starts capture automatically:

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

Then use the wrapped client exactly as before:

```python
@metergraph.track
def summarize_invoice(invoice):
    return openai_client.chat.completions.create(model="gpt-5.6-luna", messages=[...])
```

Attribution is automatic in Python (stack-walk to the nearest app function); `@metergraph.track` pins an explicit, stable name. Sync and async clients both work, streaming included. To configure in code instead of env vars, call `metergraph.init(token=..., ...)` before the first `wrap()`.

## TypeScript / JavaScript

```bash
npm install metergraph
export METERGRAPH_APP_TOKEN=<token>
```

Same one-line setup — `wrap()` initializes from the environment:

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

In TypeScript use `track()` for attribution — it is reliable across bundlers and minifiers, where stack parsing is not. All three provider SDKs are optional peer dependencies; the SDK itself has zero runtime dependencies. To configure in code, call `mg.init({ token, ... })` before the first `wrap()`.

## Set up with an AI coding agent

Paste this into Claude Code, Codex, Cursor, or any coding agent inside the codebase you want instrumented — it works for both the Python and TypeScript SDKs:

```text
Instrument this codebase's LLM API costs with Metergraph
(https://github.com/PioneerSquareLabs/metergraphsdk). It captures per-call
token usage (in/out, cached, reasoning), latency, and model, attributed to the
application function that made the call — metadata only, never prompt or
completion content.

1. Install the SDK: `pip install metergraph` (Python) or `npm install
   metergraph` (TypeScript/JavaScript). Zero runtime dependencies.
2. Find every place an OpenAI, Anthropic, or Google Gemini client is
   constructed — OpenAI()/AsyncOpenAI(), Anthropic()/AsyncAnthropic(),
   genai.Client(), new OpenAI(), new Anthropic(), new GoogleGenAI() — and wrap
   it in place:
   - Python: `client = metergraph.wrap(OpenAI())` after `import metergraph`
   - TypeScript: `const client = mg.wrap(new OpenAI())` after
     `import * as mg from "metergraph"`
   wrap() returns the same client and initializes itself from the environment.
   Do not change any call sites, arguments, or error handling; streaming and
   async work unchanged.
3. Configuration is env-var based: METERGRAPH_APP_TOKEN (required — capture is
   silently off without it) and METERGRAPH_INGEST_URL (only when self-hosting
   the server from https://github.com/PioneerSquareLabs/metergraph). Add both
   to .env.example or the deployment config; never commit a real token.
4. Attribution:
   - Python: automatic via stack walk. Optionally decorate key LLM-calling
     functions with @metergraph.track to pin a stable name.
   - TypeScript: wrap each LLM-calling function with
     mg.track("stable.name", fn) — stack-based attribution is unreliable
     under bundlers, so do this for every function that calls a wrapped client.
5. Serverless only (Lambda / Cloudflare Workers / Vercel): ensure delivery
   before the runtime freezes — wrap handlers with mg.wrapHandler(handler),
   or call mg.bindWaitUntil(ctx) once per request, or await mg.flush() before
   returning. Long-running servers and scripts need nothing extra.
6. The SDK is fail-open: transport problems never break or slow LLM calls, so
   do not add defensive try/except around wrapping or the wrapped calls.

When done, list every client you wrapped and where, and flag any LLM calls
made through libraries other than the official openai / anthropic /
@google/genai (google-genai) SDKs — those are not captured.
```

## Where the data goes

```bash
export METERGRAPH_INGEST_URL=http://localhost:8787   # your self-hosted server
export METERGRAPH_APP_TOKEN=<token>
```

Point `METERGRAPH_INGEST_URL` at a [self-hosted Metergraph server](https://github.com/PioneerSquareLabs/metergraph) or leave it unset for the hosted service. **Without a token, capture is disabled entirely** — the SDK never sends anything silently. The SDK is fail-open: transport problems never break or slow your LLM calls.

See [`examples/`](examples) for runnable per-provider examples, including an offline fake-provider demo that needs no API keys.

## License

[Apache-2.0](LICENSE)
