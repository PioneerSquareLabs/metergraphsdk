# Vercel AI SDK Compatibility and Examples Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Metergraph's Vercel AI SDK integration compile and run across AI SDK 5, 6, and 7, with an understandable API and customer-ready documentation and examples.

**Architecture:** Keep the structural, dependency-free middleware adapter. Add a customer-facing `aiSdkVersion: 5` option that maps to Vercel middleware protocol `v2`, while retaining `specificationVersion` for backward compatibility. Qualify each supported framework/runtime pair in CI, and document the same contract in the root README, TypeScript README, and example catalog.

**Tech Stack:** TypeScript, Node.js test runner, Vercel AI SDK, GitHub Actions, ESM JavaScript examples.

---

## File map

- Modify `typescript/src/vercel-ai.ts`: public compatibility option and native Promise declarations.
- Modify `typescript/test/edge-cases.test.mjs`: option mapping and conflict behavior tests.
- Create `typescript/test/ai-sdk-v5.types.ts`: AI SDK 5 assignment fixture.
- Create `typescript/test/ai-sdk-current.types.ts`: AI SDK 6/7 assignment fixture.
- Modify `typescript/package.json`: local current-version type-check script.
- Modify `.github/workflows/ci.yml`: explicit AI SDK/Node compatibility matrix.
- Create `examples/node-vercel-ai/main.mjs`: direct OpenAI and Vercel Gateway modes.
- Modify `examples/README.md`, `README.md`, and `typescript/README.md`: setup, examples, and support matrix.

### Task 1: Add the customer-facing AI SDK version option

**Files:**
- Modify: `typescript/test/edge-cases.test.mjs`
- Modify: `typescript/src/vercel-ai.ts`

- [ ] **Step 1: Write failing option-mapping tests**

Add assertions to the existing middleware-version test:

```js
assert.equal(
  createVercelAISDKMiddleware({ aiSdkVersion: 5 }).specificationVersion,
  "v2",
);
assert.throws(
  () => createVercelAISDKMiddleware({
    aiSdkVersion: 5,
    specificationVersion: "v3",
  }),
  /aiSdkVersion.*specificationVersion/,
);
```

- [ ] **Step 2: Run the focused test and verify RED**

Run:

```bash
cd typescript
npm run build
node --test --test-name-pattern="middleware versions" test/edge-cases.test.mjs
```

Expected: FAIL because `aiSdkVersion` is ignored and conflicting options are accepted.

- [ ] **Step 3: Implement the minimal API and Promise compatibility fix**

Update the options and middleware declarations:

```ts
export interface VercelAISDKMiddlewareOptions<
  TVersion extends VercelAISDKSpecificationVersion = "v3",
> {
  aiSdkVersion?: 5;
  specificationVersion?: TVersion;
}

export interface VercelAISDKMiddleware<
  TVersion extends VercelAISDKSpecificationVersion = "v3",
> {
  readonly specificationVersion: TVersion;
  wrapGenerate(options: {
    doGenerate: () => PromiseLike<any>;
    doStream: () => PromiseLike<any>;
    params: AnyRecord;
    model: AnyRecord;
  }): Promise<any>;
  wrapStream(options: {
    doGenerate: () => PromiseLike<any>;
    doStream: () => PromiseLike<any>;
    params: AnyRecord;
    model: AnyRecord;
  }): Promise<any>;
}
```

Resolve the version before returning the middleware:

```ts
if (options.aiSdkVersion === 5 && options.specificationVersion !== undefined) {
  throw new TypeError("aiSdkVersion and specificationVersion cannot be combined");
}
const specificationVersion = (
  options.aiSdkVersion === 5 ? "v2" : options.specificationVersion ?? "v3"
) as TVersion;
```

Add an overload (or equivalent generic typing) so `{ aiSdkVersion: 5 }` returns `VercelAISDKMiddleware<"v2">`, while the no-argument call returns `VercelAISDKMiddleware<"v3">`.

- [ ] **Step 4: Run the focused test and build and verify GREEN**

Run the Step 2 command again, followed by `npm run build`.

Expected: focused test passes and TypeScript exits 0.

- [ ] **Step 5: Commit**

```bash
git add typescript/src/vercel-ai.ts typescript/test/edge-cases.test.mjs
git commit -m "Support AI SDK 5 with a framework-version option"
```

### Task 2: Qualify AI SDK 5, 6, and 7

**Files:**
- Create: `typescript/test/ai-sdk-v5.types.ts`
- Create: `typescript/test/ai-sdk-current.types.ts`
- Modify: `typescript/package.json`
- Modify: `.github/workflows/ci.yml`

- [ ] **Step 1: Add the AI SDK 5 type fixture**

```ts
import type { LanguageModelMiddleware } from "ai";
import { vercelAISDKMiddleware } from "../dist/index.js";

const metergraphMiddleware = vercelAISDKMiddleware({ aiSdkVersion: 5 });
const middleware: LanguageModelMiddleware = metergraphMiddleware;
const protocol: "v2" = metergraphMiddleware.specificationVersion;
void middleware;
void protocol;
```

- [ ] **Step 2: Add the AI SDK 6/7 type fixture**

```ts
import type { LanguageModelMiddleware } from "ai";
import { vercelAISDKMiddleware } from "../dist/index.js";

const middleware: LanguageModelMiddleware = vercelAISDKMiddleware();
void middleware;
```

- [ ] **Step 3: Verify the AI SDK 5 fixture fails before the implementation is present**

In a clean temporary checkout at the pre-Task-1 commit, install `ai@5`, build, and run:

```bash
npx tsc --noEmit --strict --skipLibCheck --target ES2022 \
  --module NodeNext --moduleResolution NodeNext test/ai-sdk-v5.types.ts
```

Expected: FAIL because the old API lacks `aiSdkVersion` and returns `PromiseLike`.

- [ ] **Step 4: Make AI SDK 6 the baseline development dependency**

Change `test:types` to compile `test/ai-sdk-current.types.ts`, and set the `ai`
development dependency to the current AI SDK 6 release. This keeps normal
installation and the complete suite valid on Metergraph's Node.js 18 floor;
the dedicated matrix installs AI SDK 7 only on Node.js 22.

- [ ] **Step 5: Add an explicit compatibility matrix job**

Add a `typescript-ai-sdk` job with these matrix entries:

```yaml
include:
  - node: 18
    ai: 5
    fixture: ai-sdk-v5.types.ts
  - node: 18
    ai: 6
    fixture: ai-sdk-current.types.ts
  - node: 22
    ai: 7
    fixture: ai-sdk-current.types.ts
```

For each entry: install repository dependencies, install `ai@${{ matrix.ai }}` without saving, build, compile the selected fixture, and run the real Vercel tests:

```yaml
- run: npm install
  working-directory: typescript
- run: npm install --no-save ai@${{ matrix.ai }}
  working-directory: typescript
- run: npm run build
  working-directory: typescript
- run: npx tsc --noEmit --strict --skipLibCheck --target ES2022 --module NodeNext --moduleResolution NodeNext test/${{ matrix.fixture }}
  working-directory: typescript
- run: node --test --test-name-pattern="Vercel" test/*.test.mjs
  working-directory: typescript
```

Keep the normal TypeScript suite on Node 18, 20, and 22 using the declared AI
SDK 6 development dependency. The compatibility job separately replaces it
with AI SDK 5 or 7 for those qualification entries.

- [ ] **Step 6: Run each matrix combination locally**

For AI SDK 5, 6, and 7, run the same build, selected fixture, and Vercel-filtered test commands as CI. Expected: all commands exit 0.

- [ ] **Step 7: Commit**

```bash
git add typescript/test/ai-sdk-v5.types.ts typescript/test/ai-sdk-current.types.ts \
  typescript/package.json .github/workflows/ci.yml
git commit -m "Test Vercel AI SDK compatibility matrix"
```

### Task 3: Add a runnable Vercel AI SDK example

**Files:**
- Create: `examples/node-vercel-ai/main.mjs`
- Modify: `examples/README.md`

- [ ] **Step 1: Create the example**

Use AI SDK 7 as the example baseline. Select Gateway when `AI_GATEWAY_API_KEY` is present and direct OpenAI otherwise:

```js
import { gateway, generateText, wrapLanguageModel } from "ai";
import { openai } from "@ai-sdk/openai";
import * as mg from "metergraph";

mg.init({ environment: "example" });

const usingGateway = Boolean(process.env.AI_GATEWAY_API_KEY);
const baseModel = usingGateway
  ? gateway("anthropic/claude-sonnet-4.5")
  : openai("gpt-5-mini");
const model = wrapLanguageModel({
  model: baseModel,
  middleware: mg.vercelAISDKMiddleware(),
});

await mg.trace("haiku-workflow", () =>
  mg.route("haiku.write", async () => {
    const { text } = await generateText({
      model,
      prompt: "Write a haiku about metered clouds.",
    });
    console.log(text);
  }),
);
await mg.flush();
await mg.shutdown();
```

- [ ] **Step 2: Document installation and both modes**

Add the example to `examples/README.md` with:

```bash
npm i metergraph ai @ai-sdk/openai
OPENAI_API_KEY=... node examples/node-vercel-ai/main.mjs
AI_GATEWAY_API_KEY=... node examples/node-vercel-ai/main.mjs
```

State that the example uses AI SDK 7 and therefore Node.js 22+.

- [ ] **Step 3: Syntax-check the example**

Run `node --check examples/node-vercel-ai/main.mjs`.

Expected: exit 0.

- [ ] **Step 4: Commit**

```bash
git add examples/node-vercel-ai/main.mjs examples/README.md
git commit -m "Add Vercel AI SDK example"
```

### Task 4: Make Vercel AI SDK support prominent and unambiguous

**Files:**
- Modify: `README.md`
- Modify: `typescript/README.md`

- [ ] **Step 1: Add the root README quick start**

Include a `Vercel AI SDK` section with the `wrapLanguageModel` example, direct provider setup, and Gateway model replacement.

- [ ] **Step 2: Add the compatibility table to both READMEs**

Use the same table in each:

| Vercel AI SDK | Metergraph middleware | Node.js |
|---|---|---|
| 5 | `vercelAISDKMiddleware({ aiSdkVersion: 5 })` | 18+ |
| 6 | `vercelAISDKMiddleware()` | 18+ |
| 7 | `vercelAISDKMiddleware()` | 22+ |

Explain that Metergraph itself supports Node.js 18+, while the selected AI SDK version may impose a newer runtime requirement. Retain `specificationVersion` only in an advanced/backward-compatibility note.

- [ ] **Step 3: Check documentation consistency**

Run:

```bash
rg -n "aiSdkVersion|specificationVersion|AI SDK 7|Node.js 22|node-vercel-ai" \
  README.md typescript/README.md examples/README.md
```

Expected: customer setup uses `aiSdkVersion: 5`; no primary setup recommends `specificationVersion: "v2"`; all three files agree on the runtime matrix.

- [ ] **Step 4: Run final verification**

Run the complete TypeScript suite against the declared development dependencies, then repeat the compatibility matrix commands from Task 2. Run `git diff --check`.

Expected: all tests and type fixtures pass for AI SDK 5, 6, and 7; no whitespace errors.

- [ ] **Step 5: Commit**

```bash
git add README.md typescript/README.md
git commit -m "Document Vercel AI SDK support matrix"
```
