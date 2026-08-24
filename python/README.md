# metergraph (Python)

Zero-runtime-dependency capture for OpenAI, Anthropic, Gemini, and Python
Vercel AI Gateway clients.
Initialize Metergraph once, then wrap each provider client. `init()` reads the
token and other omitted options from the environment.

Initialization is process-wide. The first `init()` configuration remains
active; later explicit calls are ignored and produce one generic warning
without option names, token values, or other secrets.

```python
import metergraph
from openai import OpenAI

metergraph.init(repository="owner/repository")
# Anthropic() and google-genai's genai.Client() wrap the same way.
client = metergraph.wrap(OpenAI())

with metergraph.context(
    session_id="ticket-123",
    tags={"customer": "acme"},
):
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

Use `metergraph.context()` for request or job identity. It follows async work
created inside the scope and is restored afterward, so concurrent and reused
workers cannot leak session IDs or tags into one another. The narrower
`metergraph.session()` and `metergraph.tags()` scopes compose with it.
`metergraph.set_default_tags()` sets process-wide service metadata. Legacy
`set_session()` and `set_tags()` calls only update an active Metergraph scope;
outside one they warn once and do nothing.

Vercel's supported Python surface is AI Gateway through the OpenAI or
Anthropic SDK. Point either client at the public gateway and `wrap()` detects
it automatically:

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

Creator-qualified model IDs are normalized for gateway catalog pricing. Sync,
async, streaming, tool calls, and OpenAI Responses API calls are captured. Use
`metergraph.wrap(client, provider="vercel")` only when a compatible client is
behind a custom gateway URL that cannot be detected automatically.

## LiteLLM OpenTelemetry export

If LiteLLM already creates OpenTelemetry GenAI spans, install the optional
integration and configure MeterGraph as LiteLLM's custom exporter. Existing
LiteLLM call sites remain unchanged and must not also be wrapped.

```bash
python -m pip install 'metergraph[otel]' 'litellm[proxy]>=1.96.2,<2'
```

```python
import litellm
from litellm.integrations.opentelemetry import OpenTelemetry, OpenTelemetryConfig
from metergraph.opentelemetry import MetergraphGenAIExporter

litellm.callbacks.append(OpenTelemetry(OpenTelemetryConfig(
    exporter=MetergraphGenAIExporter(),
    capture_message_content="SPAN_ONLY",
)))
```

The exporter preserves OpenTelemetry trace identity, model/provider metadata,
token usage, latency, system instructions, ordered messages, and text output.
Message content is explicitly enabled because it may be sensitive. Text parts
are retained and replayable in the current POC pipeline. Calls containing other
part types retain their model, usage, timing, and status metadata, but those
parts are not replayable yet. See the runnable
[`python-litellm-otel` example](../examples/python-litellm-otel/).

Configuration:

- `METERGRAPH_APP_TOKEN` — required bearer token
- `METERGRAPH_INGEST_URL` — optional override; defaults to the hosted HTTPS endpoint
- `METERGRAPH_REPOSITORY` — optional `owner/repository` identity; used by [MeterGraph Bot](https://github.com/apps/metergraph)
- `METERGRAPH_CAPTURE_TEXT=0` — opt out of content capture globally
- `METERGRAPH_TEXT_MAX_BYTES` — per-field content limit; defaults to 1 MiB
- `METERGRAPH_DISABLED=1` — process kill switch
- `METERGRAPH_QUEUE_SIZE`, `METERGRAPH_BATCH_SIZE`, `METERGRAPH_FLUSH_SECONDS`

Repository identity enables repository-level attribution and
[MeterGraph Bot](https://github.com/apps/metergraph). Choose any one of these
options; each is sufficient on its own:

- Pass `repository="owner/repository"` to `metergraph.init()`.
- Set `METERGRAPH_REPOSITORY=owner/repository` in the environment.
- Store the identity with the source code in `.metergraph/config.json`:

```json
{"repository":"owner/repository"}
```

Resolution order is the explicit option, environment variable, then
configuration file. The SDK treats the file as read-only. Without repository
identity, it warns once and continues capture without repository attribution.

Delivery is bounded and off the request path. Queue overflow or a collector
outage drops capture and increments internal counters; it never changes the
provider call. Each wire batch is bounded to 4 MiB after optional gzip.
By default, Metergraph captures the scrubbed provider request and a normalized
response envelope, including assistant content and tool calls. Provider
credentials and transport headers are removed. Request and response are each
limited to 1 MiB of UTF-8 by default with an explicit truncation marker. Set
`METERGRAPH_TEXT_MAX_BYTES` or initialize with `text_max_bytes=...` to raise
the per-field limit for larger prompts and responses.
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

## Batch-first execution (opt-in)

`metergraph.batch_first()` is a separate, explicitly opt-in code path from `wrap()`/capture: submit one request through a provider's Batch API, wait up to a caller-chosen deadline, and fall back to exactly one direct call if the batch hasn't finished in time. It is synchronous/blocking, matching this SDK's own background-work model — a daemon thread, not asyncio.

```python
import metergraph
from openai import OpenAI

client = OpenAI()  # unwrapped — batch_first() drives it directly, not through wrap()

outcome = metergraph.batch_first(
    client, "openai",
    {"model": "gpt-5-mini", "input": "Summarize this document."},
    deadline_seconds=60,
    accept_duplicate_provider_execution=True,  # required: a missed deadline can execute the request twice
    on_late_batch_settled=lambda info: None,   # fires later, in the background, only for a losing batch
)

outcome.source                  # "batch" | "direct"
outcome.result                  # the provider response
outcome.metadata.batch_outcome  # "completed" | "failed" | "expired" | "pending_at_deadline"
```

`provider` is `"openai" | "anthropic" | "google"`, matching `wrap()`'s own explicit-provider option — never inferred from the client instance. A request with `stream=True` is rejected before any provider call. A request carrying `tools` is rejected unless `allow_duplicate_tool_call_plans=True` is also set, acknowledging that the batch result and the direct fallback are independent provider executions that may each choose a different tool-call plan. `accept_duplicate_provider_execution` must be exactly `True` — there is no default and no environment-variable override, and a missed deadline can execute (and bill) the same request twice. Neither the batch nor the direct path executes a tool call automatically; the caller receives the tool-call plan in `outcome.result` and is responsible for executing it, exactly as with a normal (non-batch-first) provider response.

`batch_first()` is not integrated with `wrap()`'s capture/telemetry pipeline; its result and metadata are returned directly to the caller, never enqueued for delivery. The background poll that watches a losing batch for late telemetry runs on a daemon thread, which does not keep the process alive — a short-lived script may exit before `on_late_batch_settled` ever fires, silently dropping that signal. This milestone's adapters call their provider client's methods synchronously; `AsyncOpenAI`, `AsyncAnthropic`, and google-genai's `.aio` namespace are not supported.

OpenAI, Anthropic, and Google Gemini all have adapters (`create_openai_batch_adapter`, `create_anthropic_batch_adapter`, `create_google_batch_adapter`), built against each provider's real SDK method signatures — verified by inspecting `openai`, `anthropic`, and `google-genai` as installed from this package's own dev extras, not by a live call — but **none has been exercised against a live provider Batch API from this SDK**. Treat this as a beta-quality, code-reviewed-but-not-live-verified surface.

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
Before wrapping, call metergraph.init(repository="owner/repository") using the
actual GitHub owner and repository name. METERGRAPH_APP_TOKEN is required; the
SDK warns and disables capture when it is missing. METERGRAPH_INGEST_URL is
only for self-hosted servers. Document variable names with placeholders in
.env.example, and put real values only in deployment configuration. Metergraph
captures scrubbed provider requests and normalized responses by default for
the hosted dashboard; use
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
