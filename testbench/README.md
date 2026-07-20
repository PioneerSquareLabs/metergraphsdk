# Metergraph live cost test bench

This bench makes one real call from each of the Python and TypeScript SDKs for
OpenAI, Anthropic, and Gemini. Every call is wrapped and route-tagged by the
local Metergraph SDK, then the bench polls each Metergraph target for the six
stored rows and verifies:

- provider/model aggregates plus language, environment, and route attribution;
- nonzero input and output usage;
- catalog-derived `priced` status with canonical model and price lineage;
- zero SDK-reported or unpriced calls; and
- `cost_usd` equals an independent cache-aware token calculation to eight
  decimals, with fixed two-call totals for every provider.

The fixed low-cost models are `gpt-5.6-luna`,
`claude-haiku-4-5-20251001`, and `gemini-2.5-flash`. Prompt and completion text
capture is explicitly disabled.

## Run

Set `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, and `GOOGLE_GENAI_API_KEY` in the
environment or in the repository's ignored `.env` file. Use `--env-file` (or
`METERGRAPH_BENCH_ENV_FILE`) to read a different dotenv file. Metergraph
credentials must be supplied separately:

```sh
export METERGRAPH_BENCH_AWS_TOKEN='<hosted Metergraph ingest+read token>'
export METERGRAPH_BENCH_OSS_TOKEN='dev-token'

python testbench/run.py --targets aws,oss \
  --output testbench/results/latest.json
```

Defaults:

- AWS: `https://d2xus7mp8zdv6t.cloudfront.net`
- OSS: `http://localhost:8787`

Override them with `--aws-url` / `--oss-url`. Use `--targets aws` or
`--targets oss` for only one server. The first run installs isolated testbench
dependencies and compiles both the Metergraph TypeScript SDK and the
TypeScript provider runner; later runs may use `--skip-setup`.

To re-check already captured rows without spending on new provider calls, pass
their report's `run_id` with `--verify-run-id`.

Start the current OSS server from the adjacent `metergraph` repository:

```sh
cd ../metergraph
MG_TOKENS=dev-token docker compose up -d --build
```

The command exits nonzero when a provider call fails, a captured row never
arrives through `/v1/calls`, usage or catalog lineage is missing, the row is not
fully priced, any reported/unpriced calls exist, or the stored cost differs
from the independent calculation and fixed provider total. JSON reports contain
no API keys or Metergraph tokens. A `/v1/calls` failure is fatal; the bench does
not fall back to aggregate reporting.
