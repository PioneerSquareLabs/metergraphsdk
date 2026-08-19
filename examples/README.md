# Examples

Each example wraps a real provider client, attributes calls to a function with `track`, and sends usage metadata to your Metergraph server.

Setup for all of them:

```bash
export METERGRAPH_INGEST_URL=http://localhost:8787   # your self-hosted server
export METERGRAPH_APP_TOKEN=dev-token                # one of MG_TOKENS
```

| Example | Needs |
|---|---|
| `fake-providers/run_e2e.py` | nothing — offline demo traffic |
| `python-openai/main.py` | `pip install metergraph openai`, `OPENAI_API_KEY` |
| `python-anthropic/main.py` | `pip install metergraph anthropic`, `ANTHROPIC_API_KEY` |
| `python-gemini/main.py` | `pip install metergraph google-genai`, `GEMINI_API_KEY` |
| `node-openai/main.mjs` | `npm i metergraph openai`, `OPENAI_API_KEY` |
| `node-anthropic/main.mjs` | `npm i metergraph @anthropic-ai/sdk`, `ANTHROPIC_API_KEY` |
| `node-gemini/main.mjs` | `npm i metergraph @google/genai`, `GEMINI_API_KEY` |
| `node-vercel-ai/main.mjs` | `npm i metergraph ai @ai-sdk/openai`, `OPENAI_API_KEY` or `AI_GATEWAY_API_KEY` |
| `node-vercel-ai-registry/` | New or adaptable multi-provider AI SDK applications |
| `node-vercel-ai-existing-factory/` | Applications that already have a custom model factory |

`node-vercel-ai/main.mjs` uses AI SDK 7 and therefore requires Node.js 22+. It
wraps a language model with `mg.vercelAISDKMiddleware()` instead of a provider
client. It calls a direct OpenAI model by default, or the Vercel AI Gateway
when `AI_GATEWAY_API_KEY` is set:

```bash
npm i metergraph ai @ai-sdk/openai
OPENAI_API_KEY=... node examples/node-vercel-ai/main.mjs
AI_GATEWAY_API_KEY=... node examples/node-vercel-ai/main.mjs
```

## Choose a multi-provider example

| Your application | Start here | MeterGraph integration point |
|---|---|---|
| Calls one provider or model directly | [`node-vercel-ai/`](node-vercel-ai/) | Wrap that model with `vercelAISDKMiddleware()` |
| Uses or can adopt `createProviderRegistry()` | [`node-vercel-ai-registry/`](node-vercel-ai-registry/) | Add middleware once to the registry |
| Already has a custom model factory | [`node-vercel-ai-existing-factory/`](node-vercel-ai-existing-factory/) | Wrap the factory's controlled exit |

Use the provider registry for new multi-provider integrations. Do not create a
custom factory solely for MeterGraph. Each detailed README identifies the
original application code and every MeterGraph addition; the runnable source
uses matching `MeterGraph integration` comments.
