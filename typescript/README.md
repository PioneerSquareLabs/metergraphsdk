# metergraph (TypeScript)

Zero-runtime-dependency capture for OpenAI, Anthropic, and Gemini clients on
Node 18+, including AWS Lambda. Worker-style `waitUntil` hooks are present but
are not part of the v1 public-package qualification contract.

`wrap()` initializes capture from the environment, so setup is one line per
client; call `init(...)` before the first `wrap()` only to pass options in
code.

```ts
import { wrap, route, modelFor, recordOutcome, setSession } from "metergraph";
import OpenAI from "openai";

// new Anthropic() and new GoogleGenAI({}) wrap the same way.
const client = wrap(new OpenAI());
setSession("ticket-123");

let model = "gpt-4.1-mini";
await route("ticket-classifier", async () => {
  model = modelFor("ticket-classifier", model);
  await client.chat.completions.create({ model, messages: [] });
}, { unit: "answer", captureText: true });

recordOutcome("ticket-classifier", {
  model,
  taskCompleted: true,
  feedbackScore: 1,
  turnsToResolution: 2,
  escalated: false,
});
```

Set `METERGRAPH_APP_TOKEN`; `METERGRAPH_INGEST_URL` is only needed to override
the hosted HTTPS endpoint. Content is metadata-only by default; set
`METERGRAPH_CAPTURE_TEXT=1` globally or `captureText: true` on an individual
route to opt in. Actual wire batches never exceed 512 KiB after optional gzip.
Long-running Node processes use an unref'd
background timer. On Workers or Vercel, call
`bindWaitUntil(ctx)` once per request. On Lambda, use `wrapHandler(handler)`
or `await flush()` before returning.

`recordOutcome` uses the same bounded asynchronous channel and sends no prompt
or output content. A stable session ID and the model actually used let a
session-sticky canary compare task completion and optional feedback,
turn-count, escalation, abandonment, edit-distance, and regeneration signals.

OpenAI Batch API output JSONL is captured per inference when the `Response`
from a wrapped `client.files.content()` is consumed with `text()`,
`arrayBuffer()`, or `blob()`. Anthropic message batches are captured per
inference while iterating a wrapped `client.messages.batches.results()` result.
Consume results inside `route()` so the asynchronous batch retains its product
route. Job-management polls are deliberately not counted as model calls.

## Set up with an AI coding agent

Paste this into Claude Code, Codex, Cursor, or any coding agent inside the
codebase you want instrumented:

```text
Instrument this codebase's LLM API costs with the `metergraph` npm package
(https://github.com/PioneerSquareLabs/metergraphsdk): npm install metergraph,
then wrap every new OpenAI(), new Anthropic(), and new GoogleGenAI()
construction in place, e.g. const client = mg.wrap(new OpenAI()) after
import * as mg from "metergraph". wrap() returns the same client and
initializes itself from the environment: METERGRAPH_APP_TOKEN is required
(capture is silently off without it) and METERGRAPH_INGEST_URL is only for
self-hosted servers — add both to .env.example, never commit a real token.
Capture is metadata-only (tokens, latency, model — no prompt/completion
content) and fail-open, so do not change call sites, arguments, or error
handling; async and streaming work unchanged. Wrap each function that calls a
wrapped client with mg.track("stable.name", fn) — stack-based attribution is
unreliable under bundlers. On serverless (Lambda / Workers / Vercel), ensure
delivery before the runtime freezes: mg.wrapHandler(handler), or
mg.bindWaitUntil(ctx) once per request, or await mg.flush() before returning;
long-running servers need nothing extra. When done, list every client you
wrapped and flag LLM calls made outside the official openai /
@anthropic-ai/sdk / @google/genai SDKs — those are not captured.
```
