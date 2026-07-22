# Security Policy

This repository is the client SDK (`metergraph` on PyPI and npm) — the code
that runs inside your application and wraps your OpenAI, Anthropic, or Gemini
client. It does not include the ingest server, price catalog, or dashboard;
those live in [PioneerSquareLabs/metergraph](https://github.com/PioneerSquareLabs/metergraph),
which has its own security policy covering storage, retention, and
server-side access control.

## Supported Versions

This project has one active release line (`0.x`, pre-1.0). Security fixes
land on the latest published release of each package. There is no long-term
support branch yet.

## Reporting a Vulnerability

Please report suspected vulnerabilities privately through
[GitHub Security Advisories](https://github.com/PioneerSquareLabs/metergraphsdk/security/advisories/new)
rather than opening a public issue. Include the affected version, a
reproduction if you have one, and the impact as you understand it. We'll
acknowledge new reports and work with you on a fix and coordinated
disclosure timeline.

## Threat Model

**What the SDK sends, and when.** With no `METERGRAPH_APP_TOKEN` set, the SDK
sends nothing — capture is off by default until explicitly configured. Once
configured, every wrapped call produces one row of usage metadata (tokens,
latency, model, route/session/tag labels, a structural template hash) sent to
`METERGRAPH_INGEST_URL`. Prompt and completion content is **never** included
unless `capture_text`/`captureText` is explicitly turned on, globally or per
route. Even then, known-sensitive request keys (`api_key`, `authorization`,
`headers`, `token`, `secret`) are stripped before anything is serialized —
see `scrub()` in [`python/src/metergraph/_template.py`](python/src/metergraph/_template.py)
and [`typescript/src/template.ts`](typescript/src/template.ts).

**Fail-open by design.** Transport, DNS, or ingest failures are swallowed and
never raise, block, or slow down the wrapped LLM call — this is a deliberate
availability-over-completeness tradeoff. The practical implication: a
network position that can black-hole traffic to the ingest endpoint can
silently suppress your cost telemetry, but it cannot use that position to
degrade or crash the host application.

**Auth model.** A single per-app bearer token (`METERGRAPH_APP_TOKEN`) is
sent as `Authorization: Bearer <token>` on every ingest and config-poll
request. The hosted default endpoint is HTTPS. If you self-host and point
`METERGRAPH_INGEST_URL` at a plain `http://` endpoint (as local development
setups typically do), the token and any captured metadata — including opted-in
content — travel in plaintext on that path. Treat the token like any other
API credential: scope it per application, and don't commit it.

**Blast radius of a compromised or malicious ingest endpoint.** Beyond
reading whatever rows the SDK sends (and being able to replay the bearer
token against that endpoint), a malicious or compromised endpoint controls
the responses to the SDK's periodic config poll (`GET /v1/config`). That
config directly drives `model_for()`/`modelFor()`'s return value — see
`choose_model()` in [`python/src/metergraph/_config.py`](python/src/metergraph/_config.py)
and [`typescript/src/config.ts`](typescript/src/config.ts) — which callers
are expected to pass straight into their provider's `model` parameter. A
malicious config response can therefore steer which model an application
actually calls. Nothing in the SDK executes or evaluates response content as
code, so this is a model-selection integrity risk, not a code-execution one —
but it's worth knowing if you're deciding how much to trust a
self-hosted or third-party ingest endpoint.

**Supply chain.** Both packages have zero runtime dependencies. Provider
SDKs (`openai`, `@anthropic-ai/sdk`, `@google/genai`) are optional peer
dependencies you already depend on directly; this SDK does not pin or
install them.
