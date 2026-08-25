import { type CallState, type CaptureRuntime, chunkText } from "./capture.js";
import { contextSnapshot, type CaptureContext } from "./context.js";

type AnyRecord = Record<PropertyKey, any>;

let runtime: CaptureRuntime | undefined;
const seenBatchItems = new Set<string>();
// Tracks which resource *owners* (e.g. a specific chat.completions instance)
// currently have a wrapped call in flight synchronously, so a delegating
// method (like openai's .parse() calling .create() on the same owner
// before any await) can be told apart from an unrelated wrapped call that
// merely happens to be nested inside another's synchronous execution.
// Scoped per-owner, not global, so two independent clients invoked in the
// same synchronous window are never mistaken for one delegating into the
// other.
const reentrantOwners = new WeakSet<object>();

export function setCaptureRuntime(value?: CaptureRuntime): void {
  runtime = value;
}

/** @internal Used by optional framework adapters without adding runtime dependencies. */
export function getCaptureRuntime(): CaptureRuntime | undefined {
  return runtime;
}

function requestFrom(args: unknown[]): Record<string, unknown> {
  const first = args[0];
  return first && typeof first === "object" ? { ...(first as Record<string, unknown>) } : {};
}

function get(value: unknown, key: string): any {
  return value && typeof value === "object" ? (value as AnyRecord)[key] : undefined;
}

// Capture is fail-open: a start() fault drops instrumentation for the call, a
// finish() fault drops the row. Neither may reach the provider path.
function startCapture(
  capture: CaptureRuntime,
  provider: string,
  endpoint: string,
  request: Record<string, unknown>,
  stack?: string,
): CallState | undefined {
  try {
    return capture.start(provider, endpoint, request, stack);
  } catch {
    return undefined;
  }
}

function finishCapture(
  capture: CaptureRuntime,
  state: CallState | undefined,
  response?: unknown,
  extra: Parameters<CaptureRuntime["finish"]>[2] = {},
): void {
  if (!state) return;
  try {
    capture.finish(state, response, extra);
  } catch {
    // Telemetry finalization is fail-open.
  }
}

function markBatchItem(key: string): boolean {
  if (seenBatchItems.has(key)) return false;
  if (seenBatchItems.size >= 100_000) seenBatchItems.clear();
  seenBatchItems.add(key);
  return true;
}

function captureOpenAIBatchItem(
  capture: CaptureRuntime,
  item: AnyRecord,
  sourceId: string,
  context: CaptureContext,
  stack?: string,
): void {
  const response = get(item, "response");
  const error = get(item, "error");
  if (response == null && error == null) return;
  const customId = String(get(item, "custom_id") ?? "");
  const itemId = String(get(item, "id") ?? customId);
  const responseId = String(get(response, "request_id") ?? itemId);
  if (!markBatchItem(`openai:${sourceId}:${itemId}:${responseId}`)) return;
  const body = get(response, "body") ?? {};
  const normalized = body && typeof body === "object"
    ? { ...body, _request_id: responseId }
    : body;
  const request = {
    model: get(body, "model"),
    batch: true,
    service_tier: "batch",
    batch_custom_id: customId || undefined,
    batch_item_id: itemId || undefined,
  };
  const endpoint = get(body, "object") === "response"
    ? "batch.responses"
    : "batch.chat.completions";
  const state = capture.start("openai", endpoint, request, stack, context);
  const statusCode = Number(get(response, "status_code"));
  capture.finish(state, normalized, {
    status: error != null || (Number.isFinite(statusCode) && statusCode >= 400)
      ? "error"
      : undefined,
  });
}

function captureOpenAIBatchContent(
  capture: CaptureRuntime,
  content: string,
  sourceId: string,
  context: CaptureContext,
  stack?: string,
): void {
  try {
    for (const line of content.split(/\r?\n/)) {
      if (!line.trim()) continue;
      try {
        const item = JSON.parse(line);
        if (item && typeof item === "object" && "custom_id" in item
          && ("response" in item || "error" in item)) {
          captureOpenAIBatchItem(capture, item, sourceId, context, stack);
        }
      } catch { /* a non-batch JSONL line is ignored */ }
    }
  } catch { /* capture cannot break file consumption */ }
}

function wrapOpenAIFileResponse(
  response: AnyRecord,
  capture: CaptureRuntime,
  sourceId: string,
  context: CaptureContext,
  stack?: string,
): AnyRecord {
  if (!response || typeof response !== "object") return response;
  return new Proxy(response, {
    get(target, property) {
      if (property === "text" && typeof target.text === "function") {
        return async () => {
          const text = await target.text();
          captureOpenAIBatchContent(capture, text, sourceId, context, stack);
          return text;
        };
      }
      if (property === "arrayBuffer" && typeof target.arrayBuffer === "function") {
        return async () => {
          const value = await target.arrayBuffer();
          captureOpenAIBatchContent(
            capture, new TextDecoder().decode(value), sourceId, context, stack,
          );
          return value;
        };
      }
      if (property === "blob" && typeof target.blob === "function") {
        return async () => {
          const value = await target.blob();
          captureOpenAIBatchContent(capture, await value.text(), sourceId, context, stack);
          return value;
        };
      }
      const value = Reflect.get(target, property, target);
      return typeof value === "function" ? value.bind(target) : value;
    },
  });
}

function captureAnthropicBatchItem(
  capture: CaptureRuntime,
  item: AnyRecord,
  batchId: string,
  context: CaptureContext,
  stack?: string,
): void {
  const result = get(item, "result");
  const resultType = String(get(result, "type") ?? "");
  const customId = String(get(item, "custom_id") ?? "");
  if (!resultType || !markBatchItem(`anthropic:${batchId}:${customId}:${resultType}`)) return;
  const message = get(result, "message") ?? {};
  const state = capture.start("anthropic", "batch.messages", {
    model: get(message, "model"),
    batch: true,
    service_tier: "batch",
    batch_custom_id: customId || undefined,
    batch_id: batchId || undefined,
  }, stack, context);
  capture.finish(state, message, {
    status: resultType === "succeeded" ? undefined : "error",
  });
}

function wrapAnthropicBatchResults(
  result: AnyRecord,
  capture: CaptureRuntime,
  batchId: string,
  context: CaptureContext,
  stack?: string,
): AnyRecord {
  if (!result || typeof result[Symbol.asyncIterator] !== "function") return result;
  return new Proxy(result, {
    get(target, property, receiver) {
      if (property === Symbol.asyncIterator) {
        return async function* () {
          for await (const item of target as AsyncIterable<AnyRecord>) {
            try { captureAnthropicBatchItem(capture, item, batchId, context, stack); }
            catch { /* capture cannot break result iteration */ }
            yield item;
          }
        };
      }
      const value = Reflect.get(target, property, receiver);
      return typeof value === "function" ? value.bind(target) : value;
    },
  });
}

function patchOpenAIBatchContent(owner: AnyRecord | undefined, method: string): boolean {
  if (!owner || typeof owner[method] !== "function") return false;
  if (owner[method].__metergraph_batch__) return true;
  const original = owner[method];
  const wrapped = function (this: unknown, ...args: unknown[]) {
    const capture = runtime;
    if (!capture) return original.apply(owner, args);
    const sourceId = String(args[0] ?? "unknown");
    const context = contextSnapshot();
    const stack = new Error().stack;
    const result = original.apply(owner, args);
    if (result && typeof result.then === "function") {
      return result.then((response: AnyRecord) => (
        wrapOpenAIFileResponse(response, capture, sourceId, context, stack)
      ));
    }
    return wrapOpenAIFileResponse(result, capture, sourceId, context, stack);
  };
  wrapped.__metergraph_batch__ = true;
  owner[method] = wrapped;
  return true;
}

function patchAnthropicBatchResults(owner: AnyRecord | undefined): boolean {
  if (!owner || typeof owner.results !== "function") return false;
  if (owner.results.__metergraph_batch__) return true;
  const original = owner.results;
  const wrapped = function (this: unknown, ...args: unknown[]) {
    const capture = runtime;
    if (!capture) return original.apply(owner, args);
    const batchId = String(args[0] ?? "unknown");
    const context = contextSnapshot();
    const stack = new Error().stack;
    const result = original.apply(owner, args);
    if (result && typeof result.then === "function") {
      return result.then((resolved: AnyRecord) => (
        wrapAnthropicBatchResults(resolved, capture, batchId, context, stack)
      ));
    }
    return wrapAnthropicBatchResults(result, capture, batchId, context, stack);
  };
  wrapped.__metergraph_batch__ = true;
  owner.results = wrapped;
  return true;
}

function streamProxy(stream: AnyRecord, state: CallState, capture: CaptureRuntime): AnyRecord {
  let last: unknown;
  let ttftMs: number | undefined;
  const parts: string[] = [];
  const chunks: unknown[] = [];
  const hasOutput = (chunk: unknown) => {
    if (chunkText(chunk)) return true;
    const value = chunk as AnyRecord;
    if (Array.isArray(value?.choices)
      && value.choices.some((choice: AnyRecord) => choice?.delta?.tool_calls?.length)) {
      return true;
    }
    if (typeof value?.delta === "string" && String(value?.type).includes("reasoning")) {
      return value.delta.length > 0;
    }
    if (value?.delta?.thinking || value?.delta?.reasoning) return true;
    if (value?.type === "content_block_start") {
      return value.content_block?.type === "tool_use";
    }
    if (value?.type === "content_block_delta") {
      return value.delta?.type === "input_json_delta";
    }
    return Array.isArray(value?.candidates)
      && value.candidates.some((candidate: AnyRecord) => candidate?.content?.parts?.some(
        (part: AnyRecord) => part?.function_call || part?.functionCall,
      ));
  };
  const observe = (chunk: unknown) => {
    last = chunk;
    chunks.push(chunk);
    const text = chunkText(chunk);
    if (ttftMs === undefined && hasOutput(chunk)) {
      ttftMs = Math.round(performance.now() - state.started);
    }
    if (text) {
      parts.push(text);
    }
    return chunk;
  };
  const finish = (response: unknown = last, error?: unknown, status?: string) => {
    finishCapture(capture, state, response, {
      error,
      status,
      stream: true,
      ttftMs,
      responseText: parts.join("") || undefined,
      responseChunks: chunks,
    });
  };

  if (typeof stream.on === "function") {
    stream.on("error", (error: unknown) => finish(last, error));
  }

  return new Proxy(stream, {
    get(target, property, receiver) {
      if (property === Symbol.asyncIterator && typeof target[Symbol.asyncIterator] === "function") {
        return async function* () {
          try {
            for await (const chunk of target as AsyncIterable<unknown>) {
              // Telemetry boundary: observation and usage-only classification.
              // An ordinary telemetry fault yields the raw chunk and continues.
              let usageOnly = false;
              try {
                observe(chunk);
                usageOnly = state.provider === "openai"
                  && state.endpoint === "chat.completions"
                  && Array.isArray((chunk as AnyRecord)?.choices)
                  && (chunk as AnyRecord).choices.length === 0
                  && (chunk as AnyRecord).usage != null;
              } catch {
                yield chunk;
                continue;
              }
              if (!usageOnly) yield chunk;
            }
          } catch (error) {
            // Provider iteration error: finalize fail-open, keep its identity.
            finish(last, error);
            throw error;
          }
          // Exhaustion: enrich with finalMessage when present, then finalize.
          let final = last;
          if (typeof target.finalMessage === "function") {
            try { final = await target.finalMessage(); } catch { /* enrichment is best-effort */ }
          }
          finish(final);
        };
      }
      if (property === "finalMessage" && typeof target.finalMessage === "function") {
        return async (...args: unknown[]) => {
          let final: unknown;
          try {
            final = await target.finalMessage(...args);
          } catch (error) {
            finish(last, error);
            throw error;
          }
          finish(final);
          return final;
        };
      }
      if (property === "close" || property === "abort") {
        const original = Reflect.get(target, property, receiver);
        if (typeof original !== "function") return original;
        return (...args: unknown[]) => {
          let returned: unknown;
          let error: unknown;
          let threw = false;
          try {
            returned = original.apply(target, args);
          } catch (caught) {
            threw = true;
            error = caught;
          }
          finish(last, undefined, "abandoned");
          if (threw) throw error;
          return returned;
        };
      }
      const value = Reflect.get(target, property, receiver);
      return typeof value === "function" ? value.bind(target) : value;
    },
  });
}

function patch(owner: AnyRecord | undefined, method: string, provider: string, endpoint: string): boolean {
  if (!owner || typeof owner[method] !== "function") return false;
  if (owner[method].__metergraph__) return true;
  const original = owner[method];
  const wrapped = function (this: unknown, ...args: unknown[]) {
    const capture = runtime;
    if (!capture) return original.apply(owner, args);
    if (reentrantOwners.has(owner)) {
      // Some SDK methods delegate to another already-wrapped method on the
      // *same owner* synchronously as part of building their return value
      // (e.g. openai's chat.completions.parse calls .create(...) — on the
      // same chat.completions instance — internally, then immediately
      // chains its proprietary ._thenUnwrap() onto the result). Capturing
      // this nested call too would both double-count a single underlying
      // request and, worse, replace .create()'s return value with a plain
      // Promise that lacks ._thenUnwrap, breaking the outer method
      // entirely. Pass straight through untouched.
      //
      // Scoped to this specific owner (not a global flag): an unrelated
      // wrapped call — a different resource, or a different client's
      // instance entirely — invoked synchronously inside this one's
      // execution must still be captured independently.
      return original.apply(owner, args);
    }
    const incoming = requestFrom(args);
    const patchUsage = typeof process === "undefined"
      || process.env.METERGRAPH_PATCH_STREAM_USAGE !== "0";
    if (
      provider === "openai"
      && endpoint === "chat.completions"
      && incoming.stream === true
      && incoming.stream_options == null
      && patchUsage
    ) {
      args = [{ ...incoming, stream_options: { include_usage: true } }, ...args.slice(1)];
    }
    const state = startCapture(capture, provider, endpoint, requestFrom(args), new Error().stack);
    if (!state) {
      // start faulted: invoke the provider once, return its result unwrapped.
      reentrantOwners.add(owner);
      try {
        return original.apply(owner, args);
      } finally {
        reentrantOwners.delete(owner);
      }
    }
    let result: unknown;
    reentrantOwners.add(owner);
    try {
      result = original.apply(owner, args);
    } catch (error) {
      finishCapture(capture, state, undefined, { error });
      throw error;
    } finally {
      reentrantOwners.delete(owner);
    }
    const complete = (response: any) => {
      const request = requestFrom(args);
      const streaming = endpoint.endsWith(".stream") || request.stream === true;
      if (streaming && response && (response[Symbol.asyncIterator] || response.finalMessage)) {
        return streamProxy(response, state, capture);
      }
      finishCapture(capture, state, response);
      return response;
    };
    if (result && typeof (result as Promise<unknown>).then === "function") {
      return (result as Promise<unknown>).then(complete, (error) => {
        finishCapture(capture, state, undefined, { error });
        throw error;
      });
    }
    return complete(result);
  };
  wrapped.__metergraph__ = true;
  owner[method] = wrapped;
  return true;
}

export interface Seam {
  path: string;
  method: string;
  endpoint: string;
}

export const OPENAI_SEAMS: Seam[] = [
  { path: "chat.completions", method: "create", endpoint: "chat.completions" },
  { path: "chat.completions", method: "parse", endpoint: "chat.completions.parse" },
  // beta.chat.completions.parse is v4.x-only — removed at the v4->v5
  // boundary when .parse() moved to the stable chat.completions/responses
  // namespaces (see metergraph-internal#9). Kept here anyway: a seam that
  // doesn't exist on the installed client resolves to undefined and is
  // silently skipped (see resolveSeam/applySeams below), so keeping this
  // entry costs nothing on openai>=5 and restores real instrumentation for
  // any consumer still on openai v4.
  { path: "beta.chat.completions", method: "parse", endpoint: "chat.completions.parse" },
  { path: "responses", method: "create", endpoint: "responses" },
  { path: "responses", method: "stream", endpoint: "responses.stream" },
  { path: "responses", method: "parse", endpoint: "responses.parse" },
  { path: "beta.responses", method: "create", endpoint: "responses" },
  // Note: as of openai (npm) v7.x, client.beta.responses has no .parse
  // method. This table's reality-check test is pinned to latest openai, so
  // beta.chat.completions.parse above is exempted from that check since it
  // will correctly show as absent there — see the test for details.
];

export const ANTHROPIC_SEAMS: Seam[] = [
  { path: "messages", method: "create", endpoint: "messages" },
  { path: "messages", method: "stream", endpoint: "messages.stream" },
];

export const GOOGLE_SEAMS: Seam[] = [
  { path: "models", method: "generateContent", endpoint: "models.generate_content" },
  { path: "models", method: "generateContentStream", endpoint: "models.generate_content.stream" },
  // Note: unlike the Python google-genai client, the JS/TS @google/genai
  // client has no .aio namespace at all (verified directly against the
  // installed package — client.aio is undefined; JS methods are already
  // Promise-based) — do not add aio.models entries here.
];

export const SEAM_TABLES: Record<string, Seam[]> = {
  openai: OPENAI_SEAMS,
  anthropic: ANTHROPIC_SEAMS,
  google: GOOGLE_SEAMS,
};

function detectProvider(client: AnyRecord): "openai" | "anthropic" | "google" {
  if (client.models?.generateContent) return "google";
  if (client.chat || client.responses) return "openai";
  return "anthropic";
}

function resolveSeam(client: AnyRecord, path: string): AnyRecord | undefined {
  let obj: any = client;
  for (const part of path.split(".")) {
    obj = obj?.[part];
    if (obj === undefined || obj === null) return undefined;
  }
  return obj;
}

function applySeams(client: AnyRecord, provider: string): string[] {
  const patched: string[] = [];
  for (const seam of SEAM_TABLES[provider] ?? []) {
    let owner: AnyRecord | undefined;
    try {
      owner = resolveSeam(client, seam.path);
    } catch {
      continue; // a pathological client property must never break wrap()
    }
    if (owner !== undefined && patch(owner, seam.method, provider, seam.endpoint)) {
      patched.push(`${seam.path}.${seam.method}`);
    }
  }
  return patched;
}

function applyBatchExtras(client: AnyRecord, provider: string): number {
  let patched = 0;
  if (provider === "openai") {
    patched += Number(patchOpenAIBatchContent(client.files, "content"));
    patched += Number(patchOpenAIBatchContent(client.files, "retrieveContent"));
  } else if (provider === "anthropic") {
    patched += Number(patchAnthropicBatchResults(client.messages?.batches));
    patched += Number(patchAnthropicBatchResults(client.beta?.messages?.batches));
  }
  return patched;
}

export function wrap<T extends AnyRecord>(client: T, provider?: "openai" | "anthropic" | "google"): T {
  try {
    const name = provider ?? detectProvider(client);
    const patched = applySeams(client, name);
    const patchedCount = patched.length + applyBatchExtras(client, name);
    if (!patchedCount) {
      console.warn(`Metergraph found no supported methods on ${name} client`);
    } else {
      console.info(`Metergraph patched ${patchedCount} seam(s) on ${name} client: ${patched.join(", ") || "(batch-only)"}`);
    }
  } catch (error) {
    console.warn("Metergraph wrap() failed; client is unmodified and uninstrumented", error);
  }
  return client;
}

export const wrapClient = wrap;
