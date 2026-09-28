# LiteLLM OpenTelemetry GenAI export

Use this integration when LiteLLM already creates OpenTelemetry GenAI spans and
you want MeterGraph to receive the same completed traces. MeterGraph is installed
as LiteLLM's custom span exporter; individual `litellm.completion()` calls stay
unchanged and should not also be wrapped with `metergraph.wrap()`.

## Install

LiteLLM's programmatic OpenTelemetry callback requires its `proxy` extra:

```bash
python -m pip install 'metergraph[otel]' 'litellm[proxy]>=1.96.2,<2'
```

Configure the normal MeterGraph environment:

```bash
export METERGRAPH_APP_TOKEN=<your-metergraph-token>
export METERGRAPH_INGEST_URL=<your-metergraph-ingest-url>
export METERGRAPH_REPOSITORY=owner/repository
```

Then run the synthetic, offline-provider example:

```bash
python main.py
```

The example uses LiteLLM's `mock_response`, so it does not need or consume a
provider credential. Replace the model, messages, and `mock_response` with your
existing LiteLLM call when integrating an application.

## Supported GenAI span content

The exporter reads the OpenTelemetry GenAI attributes
`gen_ai.provider.name`, `gen_ai.request.model`, `gen_ai.response.model`,
`gen_ai.system_instructions`, `gen_ai.input.messages`,
`gen_ai.output.messages`, `gen_ai.usage.input_tokens`,
`gen_ai.usage.output_tokens`, and `gen_ai.response.finish_reasons`. It also
accepts LiteLLM's current legacy provider attribute, `gen_ai.system`.

On spans, instructions and messages may be JSON text. Ordered messages use
`{"role":"user|assistant","parts":[...]}` and a supported text part is
`{"type":"text","content":"..."}`. See the
[OpenTelemetry GenAI span conventions](https://opentelemetry.io/docs/specs/semconv/gen-ai/gen-ai-spans/).

The exporter also reads what LiteLLM writes outside those conventions:
`gen_ai.response.id` becomes the call's request id,
`llm.request.functions.<i>.*` becomes the request's tool definitions, and
`gen_ai.completion.<i>.function_call.*` becomes the response's tool calls. A
LiteLLM span named after the caller's `generation_name` sets the call's route.

## What MeterGraph adds

Open [`main.py`](main.py) and search for `MeterGraph integration`. The only
integration block adds `MetergraphGenAIExporter` to LiteLLM's existing
OpenTelemetry callback. LiteLLM logs message content on its spans by default,
and MeterGraph needs that content to analyse the calls:

- Leave out `capture_message_content`. Releases before 1.85 do not accept it.
- Do not set `litellm.turn_off_message_logging`. It removes messages, tool
  definitions and outputs from the spans.
- Name each call with `metadata={"generation_name": "<workload name>"}`, as
  the example does. Without it every call is stored under the route `chat`.
- Make the calls of one agent run inside one application span and set
  `USE_OTEL_LITELLM_REQUEST_SPAN=true` to keep them in one trace. Without that
  variable LiteLLM writes each call onto the application span itself, and only
  one of the calls is recorded.
- Pass a new `metadata` dict to each call. Reusing one dict across calls makes
  LiteLLM 1.103 drop spans.

MeterGraph currently retains text parts for replay analysis. Calls containing
other GenAI part types still retain their model, usage, timing, and status
metadata, but those parts are not replayable by the POC canonical trace
normalizer yet. Configure MeterGraph redaction and content limits as you would
for provider-client capture.
