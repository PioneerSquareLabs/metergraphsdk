# Python Gemini: name workflows and workloads

This example shows how to make the dashboard match the way your product works.
It makes three Gemini calls for one haiku-writing request:

1. `haiku-draft` creates the first draft.
2. `haiku-review` reviews the draft.
3. `haiku-review` streams the final revision.

The calls share one `metergraph.trace("haiku-workflow")`, because they belong
to one user-visible workflow. The route changes at the product boundary:
`haiku-draft` is one workload and `haiku-review` is another. The dashboard can
therefore show two workloads even though one workflow made all three calls.
The SDK records each `route()` scope as an explicit developer-selected route,
so the workload classifier can preserve those boundaries.

## Run it

Install the example dependencies, then provide the Gemini and Metergraph
credentials:

```bash
python -m pip install metergraph google-genai
export GEMINI_API_KEY=<your-gemini-key>
export METERGRAPH_APP_TOKEN=<your-ingest-key>
export METERGRAPH_INGEST_URL=<your-metergraph-ingest-url>
python examples/python-gemini/main.py
```

For a local self-hosted Metergraph server, set
`METERGRAPH_INGEST_URL=http://localhost:8787` for the public OSS server, or
use the URL and ingest key issued by your customer-local deployment. The SDK
uses the same route and trace fields for both targets.

## Choose the scope that answers the question

- Use `route("name")` for a product surface you want to measure as a separate
  workload. Keep the name stable and meaningful to the customer, such as
  `invoice-summary` or `support-answer`.
- Use `trace("name")` for one logical workflow that may contain several
  workloads and provider calls, such as `checkout` or `haiku-workflow`.
- Use `track("name")` when you need a stable function name. It does not replace
  a route or a trace.

If two calls should be analyzed and compared together, put them under the same
route. If they are separate product surfaces, use separate routes and keep a
shared trace around the user-visible workflow.
