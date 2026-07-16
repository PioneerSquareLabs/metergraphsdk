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

## Where the data goes

```bash
export METERGRAPH_INGEST_URL=http://localhost:8787   # your self-hosted server
export METERGRAPH_APP_TOKEN=<token>
```

Point `METERGRAPH_INGEST_URL` at a [self-hosted Metergraph server](https://github.com/PioneerSquareLabs/metergraph) or leave it unset for the hosted service. **Without a token, capture is disabled entirely** — the SDK never sends anything silently. The SDK is fail-open: transport problems never break or slow your LLM calls.

See [`examples/`](examples) for runnable per-provider examples, including an offline fake-provider demo that needs no API keys.

## License

[Apache-2.0](LICENSE)
