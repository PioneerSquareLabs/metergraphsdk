# metergraph (Python)

Zero-runtime-dependency capture for OpenAI, Anthropic, and Gemini clients.
`wrap()` initializes capture from the environment, so setup is one line per
client; call `metergraph.init(...)` before the first `wrap()` only to pass
options in code.

```python
import metergraph
from openai import OpenAI

# Anthropic() and google-genai's genai.Client() wrap the same way.
client = metergraph.wrap(OpenAI())
metergraph.set_session("ticket-123")

with metergraph.route("ticket-classifier", unit="answer", capture_text=True):
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

Configuration:

- `METERGRAPH_APP_TOKEN` — required bearer token
- `METERGRAPH_INGEST_URL` — optional override; defaults to the hosted HTTPS endpoint
- `METERGRAPH_CAPTURE_TEXT=1` — opt in to content capture globally; default is metadata-only
- `METERGRAPH_DISABLED=1` — process kill switch
- `METERGRAPH_QUEUE_SIZE`, `METERGRAPH_BATCH_SIZE`, `METERGRAPH_FLUSH_SECONDS`

Delivery is bounded and off the request path. Queue overflow or a collector
outage drops capture and increments internal counters; it never changes the
provider call. Each wire batch is bounded to 512 KiB after optional gzip.
`capture_text=True` or `False` on `route()` overrides the global content policy
for that route. Config reads are ETag-aware and fail open to the default model.
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
