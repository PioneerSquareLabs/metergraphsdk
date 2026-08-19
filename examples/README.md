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
| `node-vercel-ai-factory/` | AI SDK 7 provider-registry and existing-factory patterns; see its README |

`node-vercel-ai/main.mjs` uses AI SDK 7 and therefore requires Node.js 22+. It
wraps a language model with `mg.vercelAISDKMiddleware()` instead of a provider
client. It calls a direct OpenAI model by default, or the Vercel AI Gateway
when `AI_GATEWAY_API_KEY` is set:

```bash
npm i metergraph ai @ai-sdk/openai
OPENAI_API_KEY=... node examples/node-vercel-ai/main.mjs
AI_GATEWAY_API_KEY=... node examples/node-vercel-ai/main.mjs
```

For production code that selects among several providers centrally, see
[`node-vercel-ai-factory/`](node-vercel-ai-factory/). It covers direct OpenAI
and Anthropic, Vercel AI Gateway, and OpenAI-compatible providers using either
the AI SDK provider registry or a controlled existing-factory exit.
