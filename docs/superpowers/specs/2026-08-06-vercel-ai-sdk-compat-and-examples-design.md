# Vercel AI SDK compatibility and examples

## Goal

Make the existing Vercel AI SDK integration safe to give to customers by documenting it prominently, adding a runnable direct-provider/Gateway example, and qualifying the advertised AI SDK 5, 6, and 7 support.

## Public API and compatibility

Metergraph keeps one package and one API:

- AI SDK 5 uses `vercelAISDKMiddleware({ aiSdkVersion: 5 })`.
- AI SDK 6 and 7 use `vercelAISDKMiddleware()`.
- Middleware hooks return native `Promise` values so their declarations satisfy AI SDK 5 as well as later versions.

`aiSdkVersion` describes the framework version customers know. Metergraph maps
`aiSdkVersion: 5` to Vercel's required middleware protocol value `v2`
internally, so the public API does not expose that implementation detail. The
existing `specificationVersion` option remains accepted for backward
compatibility but is no longer the recommended setup.

Metergraph continues to require Node.js 18 or newer. Documentation will make the independent framework constraint explicit: AI SDK 7 requires Node.js 22 or newer; AI SDK 5 and 6 are the choices for supported older Node.js applications.

## Documentation and example

The root README will include a copy-paste Vercel AI SDK example, Gateway configuration, and a compact compatibility table. The TypeScript README will use the same support language.

`examples/node-vercel-ai/main.mjs` will be runnable in two modes without code edits:

- Direct OpenAI provider when `AI_GATEWAY_API_KEY` is absent.
- Vercel AI Gateway when `AI_GATEWAY_API_KEY` is present.

The example will wrap the selected language model with Metergraph middleware, attribute the call with `trace` and `route`, flush telemetry, and shut down cleanly. `examples/README.md` will list its install command and environment variables.

## Qualification

CI will use explicit framework/runtime pairs rather than installing AI SDK 7 on every Node version:

- AI SDK 5 on Node.js 18, using `aiSdkVersion: 5` and verifying that the
  returned middleware exposes Vercel protocol specification `v2`.
- AI SDK 6 on Node.js 18, using the default middleware.
- AI SDK 7 on Node.js 22, using the default middleware.

Each matrix entry must build Metergraph, compile an adapter assignment against that installed AI SDK version, and exercise real `wrapLanguageModel` calls through both `generateText` and `streamText`. Existing provider and transport tests remain in the normal TypeScript test job.

Version-specific type fixtures will avoid asserting that AI SDK 5's `v2` middleware is assignable to the `v3`/`v4` type exported by later framework versions.

## Non-goals

- No separate Metergraph packages or version-named middleware APIs.
- No fetch-level interception API.
- No OpenTelemetry integration or compliance claim.
- No unrelated Gateway attribution or pricing changes.

## Acceptance criteria

1. A TypeScript project using AI SDK 5 can assign the middleware returned by
   `vercelAISDKMiddleware({ aiSdkVersion: 5 })` without a compiler error, and
   the middleware exposes Vercel's required `specificationVersion: "v2"`.
2. AI SDK 5, 6, and 7 each pass their real type and runtime compatibility check on the documented Node version.
3. The root and TypeScript READMEs clearly describe setup, Gateway use, and the runtime matrix.
4. The example documents all required dependencies and supports direct-provider and Gateway execution.
5. The existing TypeScript suite remains green.
