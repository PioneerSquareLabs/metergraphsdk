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
