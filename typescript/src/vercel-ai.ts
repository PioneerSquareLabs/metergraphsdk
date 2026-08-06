import { chunkText, type CallState, type CaptureRuntime } from "./capture.js";
import { getCaptureRuntime } from "./wrap.js";

type AnyRecord = Record<PropertyKey, any>;
export type VercelAISDKSpecificationVersion = "v2" | "v3" | "v4";

export interface VercelAISDKMiddlewareVersionOptions {
  /**
   * Set to 5 when instrumenting AI SDK 5. Maps to middleware protocol v2.
   * Cannot be combined with specificationVersion.
   */
  aiSdkVersion: 5;
  specificationVersion?: never;
}

export interface VercelAISDKMiddlewareSpecificationOptions<
  TVersion extends VercelAISDKSpecificationVersion = "v3",
> {
  aiSdkVersion?: never;
  /**
   * Advanced option: the raw Vercel middleware protocol version. AI SDK 6
   * uses v3 middleware and AI SDK 7 accepts it for backwards compatibility.
   * Prefer aiSdkVersion for AI SDK 5.
   */
  specificationVersion?: TVersion;
}

export type VercelAISDKMiddlewareOptions<
  TVersion extends VercelAISDKSpecificationVersion = "v3",
> =
  | VercelAISDKMiddlewareVersionOptions
  | VercelAISDKMiddlewareSpecificationOptions<TVersion>;

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

const GATEWAY_PROVIDERS = new Set(["gateway", "vercel", "vercel-ai-gateway"]);

function canonicalProvider(provider: unknown, modelId: unknown): string {
  const raw = String(provider ?? "unknown").trim().toLowerCase();
  const modelParts = String(modelId ?? "").trim().toLowerCase().split("/");
  if (GATEWAY_PROVIDERS.has(raw) && modelParts.length > 1 && modelParts[0]) {
    return canonicalProvider(modelParts[0], "");
  }
  if (raw === "amazon-bedrock" || raw === "aws-bedrock" || raw === "aws") return "bedrock";
  if (raw === "gemini" || raw === "google-genai" || raw.startsWith("google.")) return "google";
  for (const known of ["openai", "anthropic", "google", "bedrock"]) {
    if (raw === known || raw.startsWith(`${known}.`) || raw.startsWith(`${known}-`)) {
      return known;
    }
  }
  return raw.split(/[.:]/, 1)[0] || "unknown";
}

function requestFrom(params: AnyRecord, model: AnyRecord): Record<string, unknown> {
  // Provider options and transport fields can contain credentials, callbacks,
  // or non-serializable values. The standardized prompt/settings are enough
  // for trace reconstruction and deterministic template hashing.
  const {
    abortSignal: _abortSignal,
    headers: _headers,
    providerOptions: _providerOptions,
    prompt,
    ...settings
  } = params;
  return {
    ...settings,
    model: String(model?.modelId ?? settings.model ?? "unknown"),
    messages: prompt ?? settings.messages,
  };
}

function startCapture(
  capture: CaptureRuntime,
  operation: "generate" | "stream",
  params: AnyRecord,
  model: AnyRecord,
): CallState | undefined {
  try {
    return capture.start(
      canonicalProvider(model?.provider, model?.modelId),
      `ai.${operation === "generate" ? "doGenerate" : "doStream"}`,
      requestFrom(params, model),
      new Error().stack,
    );
  } catch {
    return undefined;
  }
}

function finishCapture(
  capture: CaptureRuntime,
  state: CallState | undefined,
  response: unknown,
  extra: Parameters<CaptureRuntime["finish"]>[2] = {},
): void {
  if (!state) return;
  try {
    capture.finish(state, response, extra);
  } catch {
    // Framework instrumentation is always fail-open.
  }
}

function outputChunk(part: AnyRecord): boolean {
  return [
    "text-delta",
    "reasoning-delta",
    "tool-input-delta",
    "tool-call",
    "tool-result",
    "tool-error",
    "file",
    "source",
  ].includes(String(part?.type));
}

function wrapStreamResult(
  result: AnyRecord,
  capture: CaptureRuntime,
  state: CallState,
  started: number,
): AnyRecord {
  const source = result?.stream;
  if (!source || typeof source.getReader !== "function") {
    finishCapture(capture, state, result, { stream: true });
    return result;
  }

  let reader: ReadableStreamDefaultReader<any>;
  try {
    reader = source.getReader();
  } catch (error) {
    finishCapture(capture, state, undefined, { error, stream: true });
    return result;
  }

  let text = "";
  let firstOutputAt: number | undefined;
  let finishPart: AnyRecord | undefined;
  let streamError: unknown;
  const content: unknown[] = [];
  const responseMetadata: Record<string, unknown> = {};

  const observe = (part: AnyRecord) => {
    if (firstOutputAt === undefined && outputChunk(part)) firstOutputAt = performance.now();
    const delta = chunkText(part);
    if (delta !== undefined) text += delta;
    if (["tool-call", "tool-result", "tool-error", "file", "source"].includes(part?.type)) {
      content.push(part);
    } else if (part?.type === "response-metadata") {
      Object.assign(responseMetadata, part);
    } else if (part?.type === "finish") {
      finishPart = part;
    } else if (part?.type === "error") {
      streamError = part.error ?? new Error("AI SDK stream error");
    }
  };

  const finish = (status?: string, error: unknown = streamError) => {
    if (text) content.unshift({ type: "text", text });
    const response = {
      content,
      usage: finishPart?.usage,
      finishReason: finishPart?.finishReason,
      response: {
        ...responseMetadata,
        ...(result.response ?? {}),
      },
    };
    finishCapture(capture, state, response, {
      error,
      status,
      stream: true,
      responseText: text,
      ttftMs: firstOutputAt === undefined ? undefined : Math.round(firstOutputAt - started),
    });
  };

  const stream = new ReadableStream<any>({
    async pull(controller) {
      try {
        const next = await reader.read();
        if (next.done) {
          finish();
          controller.close();
          return;
        }
        observe(next.value);
        controller.enqueue(next.value);
      } catch (error) {
        finish("error", error);
        controller.error(error);
      }
    },
    async cancel(reason) {
      try {
        await reader.cancel(reason);
      } finally {
        finish("abandoned");
      }
    },
  });

  return { ...result, stream };
}

export function createVercelAISDKMiddleware(
  options: VercelAISDKMiddlewareVersionOptions,
): VercelAISDKMiddleware<"v2">;
export function createVercelAISDKMiddleware<
  TVersion extends VercelAISDKSpecificationVersion = "v3",
>(
  options?: VercelAISDKMiddlewareSpecificationOptions<TVersion>,
): VercelAISDKMiddleware<TVersion>;
export function createVercelAISDKMiddleware(
  options: VercelAISDKMiddlewareOptions<any> = {},
): VercelAISDKMiddleware<any> {
  if (options.aiSdkVersion === 5 && options.specificationVersion !== undefined) {
    throw new TypeError("aiSdkVersion and specificationVersion cannot be combined");
  }
  const specificationVersion = (
    options.aiSdkVersion === 5 ? "v2" : options.specificationVersion ?? "v3"
  );
  return {
    specificationVersion,
    async wrapGenerate({ doGenerate, params, model }) {
      const capture = getCaptureRuntime();
      if (!capture) return await doGenerate();
      const state = startCapture(capture, "generate", params, model);
      try {
        const result = await doGenerate();
        finishCapture(capture, state, result);
        return result;
      } catch (error) {
        finishCapture(capture, state, undefined, { error });
        throw error;
      }
    },
    async wrapStream({ doStream, params, model }) {
      const capture = getCaptureRuntime();
      if (!capture) return await doStream();
      const started = performance.now();
      const state = startCapture(capture, "stream", params, model);
      try {
        const result = await doStream();
        return state ? wrapStreamResult(result, capture, state, started) : result;
      } catch (error) {
        finishCapture(capture, state, undefined, { error, stream: true });
        throw error;
      }
    },
  };
}
