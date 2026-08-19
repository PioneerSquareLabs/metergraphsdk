# Python BatchFirst example

Use this example when one non-streaming request may run through the provider's
discounted Batch API, but the application needs a direct fallback after a
deadline. This example uses OpenAI; MeterGraph also supports synchronous
Anthropic and Google clients through the same `batch_first()` API.

## Important behavior

BatchFirst always submits the batch request first. If it is still pending after
the configured deadline, MeterGraph makes one direct request while the batch
continues running. The two provider paths can therefore **execute and bill the
same request twice**. The required `accept_duplicate_provider_execution=True` argument makes
that trade-off explicit.

BatchFirst is an execution API separate from `wrap()` and MeterGraph telemetry.
The BatchFirst request is **not captured** or sent to the MeterGraph server;
instead, the result source and execution metadata are returned to your code.

Do not use this path for streaming. Requests with tools also require
`allow_duplicate_tool_call_plans=True`, because the batch and direct executions
can independently produce different tool-call plans. Neither plan is executed
automatically.

## What MeterGraph changes

Open [`main.py`](main.py) and search for `MeterGraph integration`. Your existing
OpenAI client and request remain ordinary provider code. MeterGraph adds:

1. `import metergraph`.
2. One `metergraph.batch_first(...)` call around the request.
3. The explicit duplicate-execution acknowledgement and deadline.

The client passed to `batch_first()` is intentionally not wrapped with
`metergraph.wrap()`; BatchFirst drives the provider's batch and direct methods
itself.

## Run it

Create a virtual environment if desired, then install the two packages:

```bash
python -m pip install metergraph openai
export OPENAI_API_KEY=<your-key>
python main.py
```

The script prints whether the returned result came from `batch` or `direct`,
the batch status at return time, and the provider result. Batch success returns
decoded JSON, while a direct fallback returns the provider SDK's response
object. This is a live example and can incur both batch and direct-request
charges.

See the [Python SDK BatchFirst reference](../../python/README.md#batch-first-execution-opt-in)
for late-batch callbacks, provider details, and current limitations.
