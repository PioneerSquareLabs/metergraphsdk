# Metergraph SDKs

Zero-dependency capture SDKs for [Metergraph](https://github.com/metergraph/metergraph) — LLM cost tracking by function. Wrap your OpenAI, Anthropic, or Gemini client and every call is attributed to the application function that made it, with token counts (in/out, cached, reasoning), latency, and model — **never prompt or completion content** unless you explicitly opt in against the hosted service.

| Package | Registry | Source |
|---|---|---|
| `metergraph` | PyPI | [`python/`](python) |
| `metergraph` | npm | [`typescript/`](typescript) |

## Python

```bash
pip install metergraph
```

```python
import metergraph
from openai import OpenAI

metergraph.init()
client = metergraph.wrap(OpenAI())

@metergraph.track
def summarize_invoice(invoice):
    return client.chat.completions.create(model="gpt-5.6-luna", messages=[...])
```

Attribution is automatic in Python (stack-walk to the nearest app function); `@metergraph.track` pins an explicit, stable name. Also works with `Anthropic()` and `google-genai`'s `genai.Client()`, sync and async, streaming included.

## TypeScript / JavaScript

```bash
npm install metergraph
```

```ts
import OpenAI from "openai";
import * as mg from "metergraph";

mg.init();
const client = mg.wrap(new OpenAI());

const summarizeInvoice = mg.track("billing.summarize_invoice", async (invoice) => {
  return client.chat.completions.create({ model: "gpt-5.6-luna", messages: [...] });
});
```

In TypeScript use `track()` for attribution — it is reliable across bundlers and minifiers, where stack parsing is not. Works with `openai`, `@anthropic-ai/sdk`, and `@google/genai` (all optional peer dependencies; the SDK itself has zero runtime dependencies).

## Where the data goes

```bash
export METERGRAPH_INGEST_URL=http://localhost:8787   # your self-hosted server
export METERGRAPH_APP_TOKEN=<token>
```

Point `METERGRAPH_INGEST_URL` at a [self-hosted Metergraph server](https://github.com/metergraph/metergraph) or leave it unset for the hosted service. **Without a token, capture is disabled entirely** — the SDK never sends anything silently. The SDK is fail-open: transport problems never break or slow your LLM calls.

See [`examples/`](examples) for runnable per-provider examples, including an offline fake-provider demo that needs no API keys.

## License

[Apache-2.0](LICENSE)
