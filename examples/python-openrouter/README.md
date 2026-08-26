# Python + OpenRouter

Capture OpenRouter usage and billing evidence with an ordinary `openai` client.
[`main.py`](main.py) runs one non-streaming and one streaming Chat Completions
call. Comments mark each MeterGraph integration line against existing application
code.

## Setup and run

```bash
pip install metergraph openai
export OPENROUTER_API_KEY=sk-or-...            # your OpenRouter key
export METERGRAPH_APP_TOKEN=dev-token          # one of your server's MG_TOKENS
export METERGRAPH_INGEST_URL=http://localhost:8787   # your self-hosted server
python examples/python-openrouter/main.py
```

The integration is one line:

```python
client = metergraph.wrap(OpenAI(api_key=..., base_url="https://openrouter.ai/api/v1"))
```

## Auto-detection and the custom-domain override

An OpenAI-compatible client whose base URL is exactly `https://openrouter.ai` is
detected automatically — no declaration needed. For a trusted custom domain or
reverse proxy, pass the override:

```python
client = metergraph.wrap(openai_client, gateway="openrouter")
```

The example picks the branch from `OPENROUTER_BASE_URL`, so setting it to a
custom host exercises the override. `provider=` and `gateway=` cannot be mixed
except for a consistent `provider="openai"`.

## What is captured

- **Requested vs served model.** The row keeps your requested `model` and adds
  `served_model` from the response. After routing or fallback they can differ.
- **Gateway-reported vs catalog cost.** `reported_cost_usd` comes from
  `usage.cost` (the OpenRouter account charge) with source
  `openrouter.usage.cost`; `reported_upstream_cost_usd` from
  `usage.cost_details.upstream_inference_cost`. These are gateway evidence, not a
  MeterGraph catalog price — the server keeps both and does not overwrite one
  with the other. The SDK adds no pricing logic.
- **Final streaming usage.** OpenRouter sends the final usage event
  automatically. MeterGraph does not inject `stream_options` and does not hide or
  reorder any chunk, so your loop still sees the usage event; capture reads the
  cost from it once.

Malformed or absent evidence is omitted, never invented, and never affects the
provider result, error, or stream.

## BYOK limitation

`reported_cost_usd` is the amount charged to the OpenRouter account. Under BYOK
the upstream provider may bill you separately, so this value can be less than the
total economic cost of the call. MeterGraph reports the documented account charge
and does not reconcile BYOK billing.
