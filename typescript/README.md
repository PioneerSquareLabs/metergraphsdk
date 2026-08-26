# metergraph (TypeScript)

Zero-runtime-dependency capture for OpenAI, Anthropic, and Gemini clients plus
provider-independent Vercel AI SDK language models on Node 18+, including AWS
Lambda. Worker-style `waitUntil` hooks are present but are not part of the v1
public-package qualification contract.

Initialize Metergraph once, then wrap each provider client. `init()` reads the
token and other omitted options from the environment.

Initialization is process-wide. The first `init()` configuration remains
active; later explicit calls are ignored and produce one generic warning
without option names, token values, or other secrets.

```ts
import { init, wrap, route, trace, modelFor, recordOutcome, withSession } from "metergraph";
import OpenAI from "openai";

init({ repository: "owner/repository" });
// new Anthropic() and new GoogleGenAI({}) wrap the same way.
const client = wrap(new OpenAI());

let model = "gpt-4.1-mini";
await withSession("ticket-123", async () => {
  await trace("ticket-workflow", () =>
    route("ticket-classifier", async () => {
      model = modelFor("ticket-classifier", { default: model });
      await client.chat.completions.create({ model, messages: [] });
    }, { unit: "answer" })
  );

  recordOutcome("ticket-classifier", {
    model,
    taskCompleted: true,
    feedbackScore: 1,
    turnsToResolution: 2,
    escalated: false,
  });
});
```

Use `withContext({ sessionId, tags }, fn)`, `withSession(sessionId, fn)`, or
`withTags(tags, fn)` for request and job identity. These callback scopes follow
Node's asynchronous execution and isolate concurrent jobs. Legacy
`setSession()` and `setTags()` remain available inside an active Metergraph
scope, but warn and do nothing when called outside one. Use `setDefaultTags()`
only for deliberate process-wide tags; sessions are always execution-scoped.

## OpenRouter

Point an OpenAI client at `https://openrouter.ai/api/v1` and `wrap()` auto-detects
the host, adding `served_model` and — when OpenRouter supplies a valid
`usage.cost` — `reported_cost_usd` to Chat Completions rows without changing the
requested model or the capture provider. A trusted custom domain uses
`wrap(client, { gateway: "openrouter" })`;
the existing `wrap(client, "openai")` form is unchanged. The runnable
[Node OpenRouter example](../examples/node-openrouter/) covers requested-vs-served
model, reported-vs-catalog cost, streaming usage, and the BYOK limitation.

## Vercel AI SDK

Use Metergraph as language-model middleware. It records each actual provider
request made by `generateText` or `streamText`, so multi-step tool loops produce
one costed span per model call rather than an inaccurate aggregate:

```ts
import { generateText, wrapLanguageModel } from "ai";
import { openai } from "@ai-sdk/openai";
import * as mg from "metergraph";

const model = wrapLanguageModel({
  model: openai("gpt-5.6-luna"),
  middleware: mg.vercelAISDKMiddleware({ repository: "owner/repository" }),
});

const result = await mg.trace("support-answer", () =>
  mg.route("support.answer", () =>
    generateText({ model, prompt: "Help this customer" })
  )
);
```

The middleware is provider-independent and retains the model/provider identity,
normalized input, output, cache, and reasoning tokens, response ID, finish
reason, latency, streaming TTFT, and tool calls. Vercel AI Gateway model IDs
such as `anthropic/claude-sonnet-5` are attributed to their upstream provider
for server-side pricing. Provider options, abort signals, and transport headers
are never serialized. Calls outside `trace()` remain valid one-span traces;
wrap a multi-call workflow in `trace()` to group its spans.

`vercelAISDKMiddleware()` initializes Metergraph itself, so the standard
integration needs only the middleware call. Pass any `init()` options there,
as shown above, or omit them when configuration comes from environment
variables. Applications that initialize Metergraph centrally may continue to
call `init()` first and then create the middleware without initialization
options.

Applications with several providers should use the [examples
chooser](../examples/README.md): apply MeterGraph once through the AI SDK
provider registry, or wrap the controlled exit of a custom factory the
application already has. The separate examples mark original application code
and every MeterGraph addition.

| Vercel AI SDK | Metergraph middleware | Node.js |
|---|---|---|
| 5 | `vercelAISDKMiddleware({ aiSdkVersion: 5 })` | 18+ |
| 6 | `vercelAISDKMiddleware()` | 18+ |
| 7 | `vercelAISDKMiddleware()` | 22+ |

Metergraph itself supports Node.js 18+; the AI SDK version you choose may
impose a newer runtime requirement — AI SDK 7 requires Node.js 22+.

Advanced/backward compatibility: the middleware also accepts a raw
`specificationVersion` (`"v2"` | `"v3"` | `"v4"`) instead of `aiSdkVersion`,
for callers pinning the underlying Vercel middleware protocol directly.
`aiSdkVersion` and `specificationVersion` cannot be combined.

`modelFor(routeName, { default, sessionKey })` takes an options object.
**Breaking change from `metergraph@0.1.0`**, which took positional
`modelFor(routeName, fallback, sessionKey?)` arguments; `default` is
required and throws if missing so a caller error surfaces immediately
instead of an `undefined` model reaching a provider call.

Set `METERGRAPH_APP_TOKEN`; `METERGRAPH_INGEST_URL` is only needed to override
the hosted HTTPS endpoint. Repository identity enables repository-level
attribution and [MeterGraph Bot](https://github.com/apps/metergraph). Choose any
one of these options; each is sufficient on its own:

- Pass `{ repository: "owner/repository" }` to `mg.init()`.
- Set `METERGRAPH_REPOSITORY=owner/repository` in the environment.
- Store the identity with the source code in `.metergraph/config.json`:

```json
{"repository":"owner/repository"}
```

Resolution order is the explicit option, environment variable, then
configuration file. The SDK treats the file as read-only. Without repository
identity, it warns once and continues capture without repository attribution.
Metergraph captures the scrubbed provider request and a
normalized response envelope, including assistant content and tool calls, by
default. Provider credentials and transport headers are removed. Request and
response are each limited to 1 MiB of UTF-8 by default with an explicit
truncation marker. Set `METERGRAPH_TEXT_MAX_BYTES` or initialize with
`textMaxBytes` to raise the per-field limit. Set `METERGRAPH_CAPTURE_TEXT=0`,
initialize with `captureText: false`, or set `captureText: false` on an
individual `route()` or `trace()` to opt out for sensitive operations. The
public open-source server still discards content; hosted workspaces retain it
under their metadata retention period.

`trace(name, fn, { traceId, parentSpanId, captureText })` groups calls into a
logical trace using `AsyncLocalStorage`. Nested traces reuse the active trace
unless given a different trace ID. Calls outside a trace become one-span
traces. Manual IDs can join work across process boundaries; automatic W3C HTTP
propagation is not included.

Actual wire batches never exceed 4 MiB after optional gzip.
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

## Batch-first execution (opt-in)

`batchFirst()` is a separate, explicitly opt-in code path from `wrap()`/capture: submit one request through a provider's Batch API, wait up to a caller-chosen deadline, and fall back to exactly one direct call if the batch hasn't finished in time.

```ts
import { batchFirst } from "metergraph";
import OpenAI from "openai";

const client = new OpenAI(); // unwrapped — batchFirst() drives it directly, not through wrap()

const outcome = await batchFirst(
  client,
  "openai",
  { model: "gpt-5-mini", input: "Summarize this document." },
  {
    deadlineMs: 60_000,
    acceptDuplicateProviderExecution: true, // required: a missed deadline can execute the request twice
    onLateBatchSettled: (info) => {
      // Fires later, asynchronously, only if a losing batch eventually
      // settles. Never blocks batchFirst()'s own return, and never exposes
      // the late result's content — only whether it contained a
      // tool-call plan.
    },
  },
);

outcome.source;                 // "batch" | "direct"
outcome.result;                 // the provider response
outcome.metadata.batch_outcome; // "completed" | "failed" | "expired" | "pending_at_deadline"
```

`provider` is `"openai" | "anthropic" | "google"`, matching `wrap()`'s own explicit-provider option — never inferred from the client instance. A request with `stream: true` is rejected before any provider call. A request carrying `tools` is rejected unless `allowDuplicateToolCallPlans: true` is also set, acknowledging that the batch result and the direct fallback are independent provider executions that may each choose a different tool-call plan. `acceptDuplicateProviderExecution` must be exactly `true` — there is no default and no environment-variable override, and a missed deadline can execute (and bill) the same request twice. Neither the batch nor the direct path executes a tool call automatically; the caller receives the tool-call plan in `outcome.result` and is responsible for executing it, exactly as with a normal (non-batch-first) provider response.

`batchFirst()` is not integrated with `wrap()`'s capture/telemetry pipeline; its result and metadata are returned directly to the caller, never enqueued for delivery. The background poll that watches a losing batch for late telemetry uses an ordinary `setTimeout`, not an unref'd one — after a deadline-triggered direct fallback it keeps the Node process alive until the batch reaches a terminal state, which can take up to 24 hours; this is currently unmitigated, so a short-lived script or CLI process should not assume it exits promptly after a fallback.

OpenAI, Anthropic, and Google Gemini all have adapters (`createOpenAIBatchAdapter`, `createAnthropicBatchAdapter`, `createGoogleBatchAdapter`), built against each provider's documented Batch API shape and covered by fake-client contract tests, but **none has been exercised against a live provider Batch API from this SDK** — treat this as a beta-quality, code-reviewed-but-not-live-verified surface.

## Set up with an AI coding agent

Paste this into Claude Code, Codex, Cursor, or any coding agent inside the
codebase you want instrumented:

```text
Instrument this codebase's LLM API costs with the `metergraph` npm package
(https://github.com/PioneerSquareLabs/metergraphsdk): npm install metergraph,
then wrap every new OpenAI(), new Anthropic(), and new GoogleGenAI()
construction in place, e.g. const client = mg.wrap(new OpenAI()) after
import * as mg from "metergraph". wrap() returns the same client and
initializes itself from the environment. Before wrapping, call
mg.init({ repository: "owner/repository" }) using the actual GitHub owner and
repository name. METERGRAPH_APP_TOKEN is required; the SDK warns and disables
capture when it is missing. METERGRAPH_INGEST_URL is only for self-hosted
servers. Document variable names with placeholders in .env.example, and put
real values only in deployment configuration. Metergraph captures scrubbed
provider requests and normalized responses by default for the hosted dashboard;
use METERGRAPH_CAPTURE_TEXT=0 or
captureText: false around sensitive operations. Provider credentials and
transport headers must never be captured. Capture is fail-open, so do not
change call sites, arguments, or error handling; async and streaming work
unchanged. Use mg.trace("stable-name", fn) to group multi-call workflows. Wrap
each function that calls a
wrapped client with mg.track("stable.name", fn), because stack-based
attribution is unreliable under bundlers. On serverless (Lambda / Workers /
Vercel), ensure delivery before the runtime freezes: mg.wrapHandler(handler),
or mg.bindWaitUntil(ctx) once per request, or await mg.flush() before
returning; long-running servers need nothing extra. For Vercel AI SDK calls,
wrap each language model with wrapLanguageModel({ model, middleware:
mg.vercelAISDKMiddleware() }); do not also wrap its internal provider transport.
When done, list every client and AI SDK model you instrumented, and flag LLM
calls made outside these supported paths.
```
