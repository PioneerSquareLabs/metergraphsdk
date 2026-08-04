# metergraph (Python)

Zero-runtime-dependency capture for OpenAI, Anthropic, Gemini, and Python
Vercel AI Gateway clients.
`wrap()` initializes capture from the environment, so setup is one line per
client; call `metergraph.init(...)` before the first `wrap()` only to pass
options in code.

```python
import metergraph
from openai import OpenAI

# Anthropic() and google-genai's genai.Client() wrap the same way.
client = metergraph.wrap(OpenAI())
metergraph.set_session("ticket-123")

with metergraph.trace("ticket-workflow"):
    with metergraph.route("ticket-classifier", unit="answer"):
        model = metergraph.model_for("ticket-classifier", default="gpt-4.1-mini")
        client.chat.completions.create(model=model, messages=[...])

# Emit this after the user-visible task resolves. It shares the bounded async
# transport and contains no prompt or output content.
metergraph.record_outcome(
    "ticket-classifier",
    model=model,
    task_completed=True,
    feedback_score=1,
    turns_to_resolution=2,
    escalated=False,
)
```

Vercel's supported Python surface is AI Gateway through the OpenAI or
Anthropic SDK. Point either client at the public gateway and `wrap()` detects
it automatically:

```python
import os
import metergraph
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

Creator-qualified model IDs are normalized for gateway catalog pricing. Sync,
async, streaming, tool calls, and OpenAI Responses API calls are captured. Use
`metergraph.wrap(client, provider="vercel")` only when a compatible client is
behind a custom gateway URL that cannot be detected automatically.

Configuration:

- `METERGRAPH_APP_TOKEN` — required bearer token
- `METERGRAPH_INGEST_URL` — optional override; defaults to the hosted HTTPS endpoint
- `METERGRAPH_CAPTURE_TEXT=0` — opt out of content capture globally
- `METERGRAPH_DISABLED=1` — process kill switch
- `METERGRAPH_QUEUE_SIZE`, `METERGRAPH_BATCH_SIZE`, `METERGRAPH_FLUSH_SECONDS`

Delivery is bounded and off the request path. Queue overflow or a collector
outage drops capture and increments internal counters; it never changes the
provider call. Each wire batch is bounded to 512 KiB after optional gzip.
SDK 0.3 captures the scrubbed provider request and a normalized response
envelope, including assistant content and tool calls, by default. Provider
credentials and transport headers are removed. Request and response are each
limited to 100 KiB of UTF-8 with an explicit truncation marker.
`capture_text=False` on `route()` or `trace()` overrides the global content
policy for a sensitive operation. The equivalent initialization option is
`metergraph.init(capture_text=False)`. The public open-source server continues
to discard content even when the SDK sends it; the hosted dashboard retains
content under the workspace retention period.

`metergraph.trace(name, trace_id=..., parent_span_id=...)` is a sync/async
context manager and decorator. Calls inside one trace share a trace ID and
receive distinct span IDs. Calls outside a trace become deterministic
single-span traces after ingestion. Manual IDs can join work across process
boundaries; automatic W3C HTTP propagation is not included.

Config reads are ETag-aware and fail open to the default model.
`record_outcome` requires a stable session ID and the model actually used so a
session-sticky canary can compare task completion and optional feedback,
turn-count, escalation, abandonment, edit-distance, and regeneration signals.

OpenAI Batch API output JSONL is captured per inference when a wrapped
`client.files.content()` / `retrieve_content()` result is read. Anthropic
message batches are captured per inference while iterating a wrapped
`client.messages.batches.results()` result. Run result consumption inside a
`route()` context so the asynchronous batch retains its product route. Batch
rows carry real per-result usage and the batch pricing flag; job-management
polls themselves are not miscounted as model calls.

## Set up with an AI coding agent

Paste this into Claude Code, Codex, Cursor, or any coding agent inside the
codebase you want instrumented:

```text
Instrument this codebase's LLM API costs with the `metergraph` PyPI package
(https://github.com/PioneerSquareLabs/metergraphsdk): pip install metergraph,
then wrap every OpenAI()/AsyncOpenAI(), Anthropic()/AsyncAnthropic(), and
genai.Client() construction in place, e.g. client = metergraph.wrap(OpenAI()).
OpenAI or Anthropic clients pointed at https://ai-gateway.vercel.sh are Vercel
AI Gateway clients and are detected automatically; keep their creator/model ID
and AI_GATEWAY_API_KEY / VERCEL_OIDC_TOKEN configuration unchanged.
wrap() returns the same client and initializes itself from the environment:
METERGRAPH_APP_TOKEN is required (capture is silently off without it) and
METERGRAPH_INGEST_URL is only for self-hosted servers. Add both to
.env.example, and never commit a real token. SDK 0.3 captures scrubbed provider
requests and normalized responses by default for the hosted dashboard; use
METERGRAPH_CAPTURE_TEXT=0 or capture_text=False around sensitive operations.
Provider credentials and transport headers must never be captured. Capture is
fail-open, so do not change call sites, arguments, or error handling; sync,
async, and streaming work unchanged. Use metergraph.trace("stable-name") to
group multi-call workflows. Attribution to the calling function is automatic;
optionally pin stable names on key LLM-calling functions with
@metergraph.track. On
serverless, call metergraph.flush() before the handler returns. When done,
list every client you wrapped and flag LLM calls made outside the official
openai / anthropic / google-genai SDKs, since those are not captured.
```
