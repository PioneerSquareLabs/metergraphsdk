import { randomBytes } from "node:crypto";

import { contextSnapshot, type CaptureContext } from "./context.js";
import { gatewayEvidence } from "./gateway.js";
import { scrubRequest, templateHash } from "./template.js";
import {
  jsonEqual,
  readDeclarations,
  toolDefinitionsEnvelope,
  validDeclarations,
  type ToolDefinitions,
} from "./tool-definitions.js";
import type { Transport } from "./transport.js";
import { SDK_VERSION } from "./version.js";

export const DEFAULT_TEXT_MAX_BYTES = 1024 * 1024;

export interface RuntimeOptions {
  captureText: boolean;
  redact?: (text: string, kind: "request" | "response") => string;
  appRoot: string;
  repoRoot?: string;
  skipFrames: string[];
  environment?: string;
  textMaxBytes: number;
}

interface Frame {
  m: string;
  f: string;
  l: number;
  p?: string;
}

export interface CallState {
  provider: string;
  endpoint: string;
  request: Record<string, unknown>;
  context: CaptureContext;
  started: number;
  ts: string;
  frames: Frame[];
  traceId: string;
  spanId: string;
  parentSpanId?: string;
  traceName: string;
  gateway?: string;
  done: boolean;
}

function get(value: unknown, key: string): unknown {
  return value && typeof value === "object" ? (value as Record<string, unknown>)[key] : undefined;
}

function first(value: unknown): unknown {
  return Array.isArray(value) ? value[0] : undefined;
}

function number(value: unknown): number | undefined {
  if (typeof value === "boolean") return undefined;
  const parsed = Number(value);
  return value == null || !Number.isSafeInteger(parsed) || parsed < 0 ? undefined : parsed;
}

function usageValue(value: unknown): Record<string, number | undefined> {
  const prompt = get(value, "prompt_tokens_details") ?? get(value, "input_tokens_details")
    ?? get(value, "promptTokensDetails") ?? get(value, "inputTokensDetails");
  const completion = get(value, "completion_tokens_details") ?? get(value, "output_tokens_details")
    ?? get(value, "completionTokensDetails") ?? get(value, "outputTokensDetails");
  const aiInput = get(value, "inputTokens");
  const aiOutput = get(value, "outputTokens");
  const aiInputDetails = get(value, "inputTokenDetails");
  const aiOutputDetails = get(value, "outputTokenDetails");
  const cacheCreation = get(value, "cache_creation") ?? get(value, "cacheCreation");
  return {
    input_tokens: number(get(value, "prompt_tokens") ?? get(value, "input_tokens")
      ?? get(value, "promptTokenCount") ?? get(aiInput, "total") ?? aiInput),
    output_tokens: number(get(value, "completion_tokens") ?? get(value, "output_tokens")
      ?? get(value, "candidatesTokenCount") ?? get(aiOutput, "total") ?? aiOutput),
    cache_read_tokens: number(get(value, "cache_read_input_tokens")
      ?? get(prompt, "cached_tokens") ?? get(value, "cachedContentTokenCount")
      ?? get(aiInput, "cacheRead") ?? get(aiInputDetails, "cacheReadTokens")),
    cache_write_tokens: number(get(value, "cache_creation_input_tokens")
      ?? get(prompt, "cache_write_tokens") ?? get(value, "cacheCreationInputTokens")
      ?? get(prompt, "cacheWriteTokens") ?? get(aiInput, "cacheWrite")
      ?? get(aiInputDetails, "cacheWriteTokens")),
    cache_write_5m_tokens: number(get(cacheCreation, "ephemeral_5m_input_tokens")
      ?? get(cacheCreation, "ephemeral5mInputTokens")),
    cache_write_1h_tokens: number(get(cacheCreation, "ephemeral_1h_input_tokens")
      ?? get(cacheCreation, "ephemeral1hInputTokens")),
    reasoning_tokens: number(get(completion, "reasoning_tokens")
      ?? get(value, "thoughtsTokenCount") ?? get(aiOutput, "reasoning")
      ?? get(aiOutputDetails, "reasoningTokens")),
  };
}

function usage(response: unknown): Record<string, number | undefined> {
  const value = get(response, "usage") ?? get(response, "usage_metadata")
    ?? get(response, "usageMetadata");
  const normalized = usageValue(value);
  const raw = get(value, "raw");
  if (!raw || typeof raw !== "object") return normalized;

  // AI SDK v4 retains provider-native usage under `raw`. Standardized totals
  // and cache counts have consistent gateway semantics, while raw data fills
  // provider-only details such as Anthropic's cache-write TTL split.
  const provider = usageValue(raw);
  const providerSpecific = new Set([
    "cache_write_5m_tokens",
    "cache_write_1h_tokens",
  ]);
  return Object.fromEntries(
    Object.entries(normalized).map(([key, fallback]) => [
      key,
      providerSpecific.has(key)
        ? provider[key] ?? fallback
        : fallback ?? provider[key],
    ]),
  );
}

// Only a non-empty string is reply text. On an OpenAI Responses body `text` is
// the text *config*, and a function-call-only reply lives in `output`.
function directText(response: unknown): string | undefined {
  const outputText = get(response, "output_text");
  if (typeof outputText === "string" && outputText) return outputText;
  const text = get(response, "text");
  return typeof text === "string" ? text : undefined;
}

function responseText(response: unknown): string | undefined {
  const direct = directText(response);
  if (direct !== undefined) return direct;
  const message = get(first(get(response, "choices")), "message");
  const content = get(message, "content");
  if (typeof content === "string") return content;
  const blocks = get(response, "content");
  if (Array.isArray(blocks)) {
    const joined = blocks.map((block) => get(block, "text")).filter((item): item is string => typeof item === "string").join("");
    if (joined) return joined;
  }
  const outputs = get(response, "output");
  if (Array.isArray(outputs)) {
    const joined = outputs.flatMap((output) => {
      const content = get(output, "content");
      if (!Array.isArray(content)) return [];
      return content.map((block) => get(block, "text") ?? get(block, "output_text"));
    }).filter((item): item is string => typeof item === "string").join("");
    if (joined) return joined;
  }
  return undefined;
}

export function chunkText(chunk: unknown): string | undefined {
  const delta = get(first(get(chunk, "choices")), "delta") ?? get(chunk, "delta");
  if (typeof delta === "string" && get(chunk, "type") === "text-delta") return delta;
  const text = get(delta, "content") ?? get(delta, "text") ?? get(chunk, "text");
  return typeof text === "string" ? text : undefined;
}

function stopReason(response: unknown): string | undefined {
  const value = get(response, "stop_reason") ?? get(response, "status")
    ?? get(response, "finishReason") ?? get(response, "finish_reason")
    ?? get(first(get(response, "choices")), "finish_reason");
  const normalized = get(value, "unified") ?? get(value, "raw") ?? value;
  return normalized == null ? undefined : String(normalized);
}

function normalizeFinishReason(value: string): string {
  const normalized = value.trim().toLowerCase().replace(/[\s_]+/g, "-");
  if (["stop", "end-turn", "stop-sequence", "completed", "succeeded"].includes(normalized)) {
    return "stop";
  }
  if (["length", "max-tokens", "max-output-tokens"].includes(normalized)) return "length";
  if (["content-filter", "safety", "blocked"].includes(normalized)) return "content-filter";
  if (["tool-calls", "tool-use", "function-call"].includes(normalized)) return "tool-calls";
  if (["error", "failed"].includes(normalized)) return "error";
  if (["other", "unknown"].includes(normalized)) return "other";
  return normalized;
}

function finishReasonDetails(response: unknown): {
  finishReason?: string;
  finishReasonRaw?: string;
} {
  const responseStatus = get(response, "status");
  const incompleteReason = responseStatus === "incomplete"
    ? get(get(response, "incomplete_details"), "reason")
    : undefined;
  const value = get(response, "stop_reason") ?? incompleteReason ?? responseStatus
    ?? get(response, "finishReason") ?? get(response, "finish_reason")
    ?? get(first(get(response, "choices")), "finish_reason");
  const unified = get(value, "unified");
  const rawValue = get(value, "raw") ?? (unified === undefined ? value : undefined);
  const source = unified ?? rawValue;
  if (source == null) return {};
  const finishReason = normalizeFinishReason(String(source));
  const raw = rawValue == null ? undefined : String(rawValue);
  return {
    finishReason,
    finishReasonRaw: raw !== undefined && raw !== finishReason ? raw : undefined,
  };
}

function toolNames(request: Record<string, unknown>): { name: string }[] | undefined {
  if (!Array.isArray(request.tools)) return undefined;
  const names = request.tools.flatMap((tool) => {
    const name = get(get(tool, "function"), "name") ?? get(tool, "name");
    return name ? [{ name: String(name) }] : [];
  });
  return names.length ? names : undefined;
}

interface ToolEvent {
  call_id: string;
  name: string;
  arguments?: unknown;
  result?: unknown;
  status: "requested" | "completed" | "error";
  idempotency: "idempotent" | "non_idempotent";
}

function toolArgument(value: unknown): unknown {
  if (typeof value === "string") {
    try { return JSON.parse(value); } catch { return value; }
  }
  return value;
}

function toolEvents(
  request: Record<string, unknown>,
  response: unknown,
  streamChunks: unknown[] = [],
): ToolEvent[] | undefined {
  const calls = new Map<string, ToolEvent>();
  const pending = new Map<string, { result: unknown; error: boolean }>();
  const policies = new Map<string, "idempotent" | "non_idempotent">();
  if (Array.isArray(request.tools)) {
    for (const tool of request.tools) {
      const fn = get(tool, "function");
      const name = get(fn, "name") ?? get(tool, "name");
      const policy = get(fn, "x-metergraph-idempotency")
        ?? get(tool, "x-metergraph-idempotency")
        ?? get(fn, "metergraph_idempotency")
        ?? get(tool, "metergraph_idempotency");
      if (name) policies.set(
        String(name),
        policy === "idempotent" ? "idempotent" : "non_idempotent",
      );
    }
  }
  const complete = (id: unknown, result: unknown, error = false) => {
    const key = String(id ?? "");
    const item = calls.get(key);
    if (!item) {
      pending.set(key, { result, error });
      return;
    }
    item.result = toolArgument(result);
    item.status = error ? "error" : "completed";
  };
  const add = (id: unknown, name: unknown, args: unknown) => {
    if (!name) return;
    const key = String(id ?? `${String(name)}:${calls.size}`);
    const item: ToolEvent = {
      call_id: key,
      name: String(name),
      arguments: toolArgument(args),
      status: "requested",
      idempotency: policies.get(String(name)) ?? "non_idempotent",
    };
    calls.set(key, item);
    const waiting = pending.get(key);
    if (waiting) {
      pending.delete(key);
      complete(key, waiting.result, waiting.error);
    }
  };
  const blocks = (value: unknown) => {
    if (!Array.isArray(value)) return;
    for (const block of value) {
      const kind = get(block, "type");
      if (kind === "tool_use") {
        add(get(block, "id"), get(block, "name"), get(block, "input"));
      } else if (kind === "tool_result") {
        complete(
          get(block, "tool_use_id"),
          get(block, "content"),
          Boolean(get(block, "is_error")),
        );
      } else if (kind === "tool-call") {
        add(
          get(block, "toolCallId") ?? get(block, "tool_call_id") ?? get(block, "id"),
          get(block, "toolName") ?? get(block, "tool_name") ?? get(block, "name"),
          get(block, "input") ?? get(block, "arguments"),
        );
      } else if (kind === "tool-result" || kind === "tool-error") {
        complete(
          get(block, "toolCallId") ?? get(block, "tool_call_id") ?? get(block, "id"),
          get(block, "output") ?? get(block, "result") ?? get(block, "error"),
          kind === "tool-error" || Boolean(get(block, "isError") ?? get(block, "is_error")),
        );
      }
    }
  };
  const geminiParts = (value: unknown) => {
    const parts = get(value, "parts");
    if (!Array.isArray(parts)) return;
    for (const part of parts) {
      const call = get(part, "function_call") ?? get(part, "functionCall");
      if (call) add(
        get(call, "id"),
        get(call, "name"),
        get(call, "args") ?? get(call, "arguments"),
      );
      const result = get(part, "function_response") ?? get(part, "functionResponse");
      if (result) complete(
        get(result, "id") ?? get(result, "name"),
        get(result, "response"),
      );
    }
  };
  const history = Array.isArray(request.messages)
    ? request.messages
    : Array.isArray(request.input)
      ? request.input
      : Array.isArray(request.contents)
        ? request.contents
        : Array.isArray(request.prompt)
          ? request.prompt
        : [];
  for (const message of history) {
    const messageCalls = get(message, "tool_calls");
    if (Array.isArray(messageCalls)) {
      for (const tool of messageCalls) {
        const fn = get(tool, "function");
        add(
          get(tool, "id") ?? get(tool, "call_id"),
          get(fn, "name") ?? get(tool, "name"),
          get(fn, "arguments") ?? get(tool, "arguments"),
        );
      }
    }
    if (get(message, "role") === "tool") {
      complete(
        get(message, "tool_call_id") ?? get(message, "call_id"),
        get(message, "content"),
        Boolean(get(message, "is_error")),
      );
    }
    const kind = get(message, "type");
    if (kind === "function_call" || kind === "tool_call") {
      add(
        get(message, "call_id") ?? get(message, "id"),
        get(message, "name"),
        get(message, "arguments"),
      );
    } else if (kind === "function_call_output" || kind === "tool_result") {
      complete(
        get(message, "call_id") ?? get(message, "tool_use_id"),
        get(message, "output") ?? get(message, "content"),
        Boolean(get(message, "is_error")),
      );
    }
    blocks(get(message, "content"));
    geminiParts(message);
  }
  const message = get(first(get(response, "choices")), "message");
  const responseCalls = get(message, "tool_calls");
  if (Array.isArray(responseCalls)) {
    for (const tool of responseCalls) {
      const fn = get(tool, "function");
      add(
        get(tool, "id") ?? get(tool, "call_id"),
        get(fn, "name") ?? get(tool, "name"),
        get(fn, "arguments") ?? get(tool, "arguments"),
      );
    }
  }
  blocks(get(response, "content"));
  const outputs = get(response, "output");
  if (Array.isArray(outputs)) {
    for (const output of outputs) {
      const kind = get(output, "type");
      if (kind === "function_call" || kind === "tool_call") {
        add(
          get(output, "call_id") ?? get(output, "id"),
          get(output, "name"),
          get(output, "arguments"),
        );
      }
    }
  }
  const candidates = get(response, "candidates");
  if (Array.isArray(candidates)) {
    for (const candidate of candidates) geminiParts(get(candidate, "content"));
  }
  const openAIDeltas = new Map<string, { id: string; name: string; arguments: string }>();
  const anthropicDeltas = new Map<string, { id: string; name: string; arguments: string }>();
  for (const chunk of streamChunks) {
    const choices = get(chunk, "choices");
    if (Array.isArray(choices)) {
      for (const choice of choices) {
        const tools = get(get(choice, "delta"), "tool_calls");
        if (!Array.isArray(tools)) continue;
        tools.forEach((tool, position) => {
          const key = String(get(tool, "index") ?? position);
          const item = openAIDeltas.get(key) ?? { id: key, name: "", arguments: "" };
          item.id = String(get(tool, "id") ?? item.id);
          const fn = get(tool, "function");
          if (get(fn, "name")) item.name += String(get(fn, "name"));
          if (get(fn, "arguments")) item.arguments += String(get(fn, "arguments"));
          openAIDeltas.set(key, item);
        });
      }
    }
    const kind = get(chunk, "type");
    if (kind === "tool-call") {
      add(
        get(chunk, "toolCallId") ?? get(chunk, "tool_call_id") ?? get(chunk, "id"),
        get(chunk, "toolName") ?? get(chunk, "tool_name") ?? get(chunk, "name"),
        get(chunk, "input") ?? get(chunk, "arguments"),
      );
    } else if (kind === "tool-result" || kind === "tool-error") {
      complete(
        get(chunk, "toolCallId") ?? get(chunk, "tool_call_id") ?? get(chunk, "id"),
        get(chunk, "output") ?? get(chunk, "result") ?? get(chunk, "error"),
        kind === "tool-error" || Boolean(get(chunk, "isError") ?? get(chunk, "is_error")),
      );
    } else if (kind === "content_block_start") {
      const block = get(chunk, "content_block");
      if (get(block, "type") === "tool_use") {
        const key = String(get(chunk, "index") ?? anthropicDeltas.size);
        const input = get(block, "input");
        anthropicDeltas.set(key, {
          id: String(get(block, "id") ?? key),
          name: String(get(block, "name") ?? ""),
          arguments: input && typeof input === "object"
            && Object.keys(input as Record<string, unknown>).length === 0
            ? ""
            : JSON.stringify(input ?? {}),
        });
      }
    } else if (kind === "content_block_delta") {
      const delta = get(chunk, "delta");
      if (get(delta, "type") === "input_json_delta") {
        const key = String(get(chunk, "index") ?? "0");
        const item = anthropicDeltas.get(key) ?? { id: key, name: "", arguments: "" };
        item.arguments += String(get(delta, "partial_json") ?? "");
        anthropicDeltas.set(key, item);
      }
    }
    const chunkCandidates = get(chunk, "candidates");
    if (Array.isArray(chunkCandidates)) {
      for (const candidate of chunkCandidates) geminiParts(get(candidate, "content"));
    }
  }
  for (const item of [...openAIDeltas.values(), ...anthropicDeltas.values()]) {
    if (item.name) add(item.id, item.name, item.arguments);
  }
  return calls.size ? [...calls.values()] : undefined;
}

function responseContent(response: unknown, aggregate?: string): unknown {
  if (aggregate !== undefined) return aggregate;
  const direct = directText(response);
  if (direct !== undefined) return direct;
  const message = get(first(get(response, "choices")), "message");
  const content = get(message, "content");
  if (content !== undefined) return content;
  const parsed = get(message, "parsed");
  if (parsed !== undefined) return parsed;
  const normalizedText = responseText(response);
  if (normalizedText !== undefined) return normalizedText;
  const blocks = get(response, "content");
  if (blocks !== undefined) return blocks;
  const output = get(response, "output");
  if (output !== undefined) return output;
  const candidates = get(response, "candidates");
  return candidates;
}

// Restrict tool-only detection to direct Anthropic response shapes.
const ANTHROPIC_ENDPOINTS = new Set(["messages", "messages.stream"]);
// Extra block fields would be lost when represented only by the canonical event.
const TOOL_BLOCK_KEYS = new Set(["type", "id", "name", "input", "caller"]);

function plainMapping(value: unknown): Record<string, unknown> | undefined {
  if (typeof value !== "object" || value === null || Array.isArray(value)) return undefined;
  const out: Record<string, unknown> = {};
  for (const [key, item] of Object.entries(value as Record<string, unknown>)) {
    if (item === undefined) continue;
    out[key] = item;
  }
  return out;
}

function representableToolBlock(block: unknown): Record<string, unknown> | undefined {
  const mapping = plainMapping(block);
  if (!mapping || mapping.type !== "tool_use") return undefined;
  if (Object.keys(mapping).some((key) => !TOOL_BLOCK_KEYS.has(key))) return undefined;
  const caller = mapping.caller;
  if (caller !== undefined && caller !== null) {
    const declared = plainMapping(caller);
    if (!declared) return undefined;
    const meaningful = Object.entries(declared).filter(([, value]) => value !== null);
    if (meaningful.length !== 1 || meaningful[0]![0] !== "type" || meaningful[0]![1] !== "direct") {
      return undefined;
    }
  }
  return mapping;
}

/** Check that every reply block has one equivalent event. */
function matchedEvents(
  blocks: Record<string, unknown>[],
  events: ToolEvent[] | undefined,
  compareArguments = true,
): boolean {
  const ids = blocks.map((block) => block.id);
  if (ids.some((id) => typeof id !== "string" || !id)) return false;
  if (new Set(ids).size !== ids.length) return false;
  for (const block of blocks) {
    const matches = (events ?? []).filter((event) => event.call_id === block.id);
    if (matches.length !== 1) return false;
    const event = matches[0]!;
    if (event.name !== block.name) return false;
    // Streamed arguments are validated after delta accumulation.
    if (compareArguments && !jsonEqual(event.arguments, toolArgument(block.input))) {
      return false;
    }
  }
  return true;
}

function argumentsEqual(accumulated: string, eventArguments: unknown): boolean {
  try {
    const expected = accumulated.trim() ? JSON.parse(accumulated) : {};
    let actual = eventArguments;
    if (typeof actual === "string") actual = actual.trim() ? JSON.parse(actual) : {};
    else if (actual === undefined || actual === null) actual = {};
    return jsonEqual(expected, actual);
  } catch {
    return false;
  }
}

/** Check whether a completed Anthropic stream contains only tool calls. */
function streamToolOnly(chunks: unknown[], events: ToolEvent[] | undefined): boolean {
  const started = new Map<string, Record<string, unknown>>();
  const args = new Map<string, string>();
  const stopped = new Set<string>();
  const stopReasons: string[] = [];
  let sawMessageStop = false;
  for (const chunk of chunks) {
    if (chunkText(chunk)) return false;
    const kind = get(chunk, "type");
    const delta = get(chunk, "delta");
    if (typeof delta === "string" && String(kind).includes("reasoning")) return false;
    if (get(delta, "thinking") || get(delta, "reasoning")) return false;
    if (kind === "content_block_start") {
      const mapping = representableToolBlock(get(chunk, "content_block"));
      if (!mapping) return false;
      const key = String(get(chunk, "index") ?? started.size);
      started.set(key, mapping);
      const initial = mapping.input;
      const empty = initial === undefined || initial === null
        || (typeof initial === "object" && Object.keys(initial as object).length === 0);
      args.set(key, empty ? "" : JSON.stringify(initial));
    } else if (kind === "content_block_delta") {
      if (get(delta, "type") !== "input_json_delta") return false;
      const key = String(get(chunk, "index") ?? "0");
      args.set(key, (args.get(key) ?? "") + String(get(delta, "partial_json") ?? ""));
    } else if (kind === "content_block_stop") {
      stopped.add(String(get(chunk, "index") ?? "0"));
    } else if (kind === "message_delta") {
      const reason = get(delta, "stop_reason");
      if (reason !== undefined && reason !== null) stopReasons.push(String(reason));
    } else if (kind === "message_stop") {
      sawMessageStop = true;
    }
  }
  if (!started.size || !sawMessageStop) return false;
  for (const key of started.keys()) if (!stopped.has(key)) return false;
  if (stopReasons.some((reason) => reason !== "tool_use")) return false;
  const blocks = [...started.values()];
  if (!matchedEvents(blocks, events, false)) return false;
  for (const [key, mapping] of started) {
    const matches = (events ?? []).filter((event) => event.call_id === mapping.id);
    if (matches.length !== 1 || !argumentsEqual(args.get(key) ?? "", matches[0]!.arguments)) {
      return false;
    }
  }
  return true;
}

/** Check for a losslessly represented Anthropic tool-only reply. */
function anthropicToolOnly(
  response: unknown,
  options: {
    provider?: string;
    endpoint?: string;
    events?: ToolEvent[];
    aggregate?: string;
    chunks?: unknown[];
    completed?: boolean;
  },
): boolean {
  if (!options.completed || options.aggregate !== undefined) return false;
  if (options.provider !== "anthropic" || !ANTHROPIC_ENDPOINTS.has(options.endpoint ?? "")) {
    return false;
  }
  if (!options.events || !options.events.length) return false;
  const blocks = get(response, "content");
  if (Array.isArray(blocks)) {
    if (!blocks.length) return false;
    // Other stop reasons may indicate an unfinished tool call.
    const reason = get(response, "stop_reason");
    if (reason !== undefined && reason !== null && String(reason) !== "tool_use") return false;
    const mapped = blocks.map((block) => representableToolBlock(block));
    if (mapped.some((block) => block === undefined)) return false;
    return matchedEvents(mapped as Record<string, unknown>[], options.events);
  }
  return streamToolOnly(options.chunks ?? [], options.events);
}

function responseEnvelope(
  response: unknown,
  aggregate: string | undefined,
  tools: ToolEvent[] | undefined,
  error: unknown,
  status: string,
  recognition: {
    provider?: string;
    endpoint?: string;
    events?: ToolEvent[];
    chunks?: unknown[];
    completed?: boolean;
  } = {},
): Record<string, unknown> {
  const responseMetadata = get(response, "response");
  let toolOnly = false;
  try {
    toolOnly = anthropicToolOnly(response, { ...recognition, aggregate });
  } catch {
    toolOnly = false;
  }
  const envelope: Record<string, unknown> = {
    role: "assistant",
    content: toolOnly ? null : responseContent(response, aggregate),
    tool_calls: tools ?? [],
    finish_reason: stopReason(response),
    request_id: get(response, "_request_id") ?? get(response, "response_id")
      ?? get(response, "responseId") ?? get(response, "id") ?? get(responseMetadata, "id"),
    model: get(response, "model") ?? get(response, "model_version")
      ?? get(response, "modelVersion") ?? get(responseMetadata, "modelId"),
    status,
  };
  if (error !== undefined) {
    envelope.error = {
      type: error instanceof Error ? error.name : "Error",
      message: error instanceof Error ? error.message : String(error),
    };
  }
  // Explicit null distinguishes tool-only output from missing content data.
  return Object.fromEntries(
    Object.entries(envelope).filter(([, value]) => value !== undefined),
  );
}

const SDK_DIR = (() => {
  const frame = new Error().stack?.split("\n")[1] ?? "";
  const match = frame.match(/^\s*at\s+(?:.*?\s+\()?(.+?)[/\\][^/\\:]+:\d+:\d+\)?$/);
  return match?.[1]?.replace(/^file:\/\//, "") ?? "metergraph-sdk-dir-unavailable";
})();

function frames(stack: string | undefined, appRoot: string, skip: string[], repoRoot?: string): Frame[] {
  if (!stack) return [];
  const result: Frame[] = [];
  for (const line of stack.split("\n").slice(1)) {
    const match = line.match(/^\s*at\s+(?:(.*?)\s+\()?(.+?):(\d+):\d+\)?$/);
    if (!match) continue;
    const [, fn = "<anonymous>", file, lineNo] = match;
    if (!file || !lineNo || !file.includes(appRoot)) continue;
    if (["node_modules", "node:internal", SDK_DIR, ...skip].some((value) => file.includes(value))) continue;
    const entry: Frame = {
      m: file.slice(file.indexOf(appRoot) + appRoot.length).replace(/^\//, ""),
      f: fn,
      l: Number(lineNo),
    };
    const normalizedFile = file.replaceAll("\\", "/").replace(/^file:\/\//, "");
    const normalizedRepoRoot = repoRoot?.replaceAll("\\", "/").replace(/\/$/, "");
    if (normalizedRepoRoot && normalizedFile.startsWith(`${normalizedRepoRoot}/`)) {
      entry.p = normalizedFile.slice(normalizedRepoRoot.length + 1);
    }
    result.push(entry);
    if (result.length === 5) break;
  }
  return result;
}

export class CaptureRuntime {
  constructor(
    readonly transport: Transport,
    readonly options: RuntimeOptions,
  ) {}

  start(
    provider: string,
    endpoint: string,
    request: Record<string, unknown>,
    stack?: string,
    context: CaptureContext = contextSnapshot(),
  ): CallState {
    return {
      provider,
      endpoint,
      request,
      context,
      started: performance.now(),
      ts: new Date().toISOString(),
      frames: frames(stack, this.options.appRoot, this.options.skipFrames, this.options.repoRoot),
      traceId: context.traceId ?? randomBytes(16).toString("hex"),
      spanId: randomBytes(8).toString("hex"),
      parentSpanId: context.parentSpanId,
      traceName: context.traceName ?? context.route ?? context.funcName
        ?? endpoint,
      done: false,
    };
  }

  finish(
    state: CallState,
    response?: unknown,
    extra: {
      error?: unknown;
      status?: string;
      stream?: boolean;
      ttftMs?: number;
      responseText?: string;
      responseChunks?: unknown[];
    } = {},
  ): void {
    if (state.done) return;
    state.done = true;
    const captureText = state.context.captureText ?? this.options.captureText;
    const text = (value: string | undefined, kind: "request" | "response") => {
      if (!captureText || value == null) return { value: undefined, truncated: false };
      try {
        value = this.options.redact ? this.options.redact(value, kind) : value;
      } catch {
        return { value: "<redaction-failed>", truncated: false };
      }
      const bytes = new TextEncoder().encode(value);
      if (bytes.byteLength <= this.options.textMaxBytes) return { value, truncated: false };
      const marker = "\n<metergraph:truncated>";
      const markerBytes = new TextEncoder().encode(marker).byteLength;
      let clipped = new TextDecoder().decode(
        bytes.slice(0, Math.max(0, this.options.textMaxBytes - markerBytes)),
      );
      while (
        clipped
        && new TextEncoder().encode(`${clipped}${marker}`).byteLength
          > this.options.textMaxBytes
      ) {
        clipped = clipped.slice(0, -1);
      }
      return { value: `${clipped}${marker}`, truncated: true };
    };
    const capturedRequest = scrubRequest(state.request);
    const request = text(JSON.stringify(capturedRequest), "request");
    // Tool definitions follow request text, redaction, and size controls.
    let toolDefinitions: ToolDefinitions | undefined;
    let toolDefinitionsTruncated = false;
    if (captureText) {
      try {
        const read = readDeclarations(state.request as Record<string, unknown>, state.provider);
        if (read) {
          let records = read.declarations;
          let fidelity = "verbatim";
          const encoded = JSON.stringify(records);
          if (this.options.redact) {
            let parsed: unknown;
            try {
              parsed = JSON.parse(this.options.redact(encoded, "request"));
            } catch {
              parsed = undefined;
            }
            if (validDeclarations(parsed)) {
              if (!jsonEqual(parsed, records)) fidelity = "filtered";
              records = parsed;
            } else {
              records = [];
            }
          }
          if (records.length) {
            const envelope = toolDefinitionsEnvelope(records, read.scope, fidelity);
            const serialized = JSON.stringify(envelope);
            if (new TextEncoder().encode(serialized).byteLength > this.options.textMaxBytes) {
              toolDefinitionsTruncated = true;
            } else {
              toolDefinitions = envelope;
            }
          }
        }
      } catch {
        toolDefinitions = undefined;
      }
    }
    const fullTools = toolEvents(
      capturedRequest as Record<string, unknown>,
      response,
      extra.responseChunks,
    );
    const status = extra.status ?? (
      extra.error ? "error" : stopReason(response) ?? "success"
    );
    const { finishReason, finishReasonRaw } = finishReasonDetails(response);
    const statusCode = extra.error || extra.status === "error" || finishReason === "error"
      ? "error"
      : "unset";
    const output = text(
      JSON.stringify(
        responseEnvelope(
          response,
          extra.responseText,
          fullTools,
          extra.error,
          status,
          {
            provider: state.provider,
            endpoint: state.endpoint,
            events: fullTools,
            chunks: extra.responseChunks,
            completed: extra.error === undefined
              && extra.status !== "error"
              && extra.status !== "abandoned"
              && statusCode !== "error"
              && finishReason !== "error",
          },
        ),
      ),
      "response",
    );
    let toolTruncated = false;
    let persistedTools: ToolEvent[] | undefined;
    if (fullTools && captureText) {
      const encodedTools = text(JSON.stringify(fullTools), "response");
      toolTruncated = encodedTools.truncated;
      try {
        persistedTools = encodedTools.value
          ? JSON.parse(encodedTools.value) as ToolEvent[]
          : undefined;
      } catch {
        // A truncated JSON payload is intentionally omitted. The bounded,
        // redacted response envelope still describes the call and the row's
        // text_truncated flag tells ingestion why this field is unavailable.
        persistedTools = undefined;
      }
    } else if (fullTools) {
      persistedTools = fullTools.map((item) => ({
        call_id: item.call_id,
        name: item.name,
        status: item.status,
        idempotency: item.idempotency,
      }));
    }
    const firstFrame = state.frames[0];
    const observedToolNames = fullTools
      ? [...new Set(fullTools.map((item) => item.name))]
      : undefined;
    this.transport.enqueue({
      ts: state.ts,
      route: state.context.route,
      provider: state.provider,
      model: state.request.model,
      ...usage(response),
      ...gatewayEvidence(state.gateway, state.endpoint, response),
      latency_ms: Math.round(performance.now() - state.started),
      status,
      status_code: statusCode,
      finish_reason: finishReason,
      finish_reason_raw: finishReasonRaw,
      session_id: state.context.sessionId,
      conversation_id: state.context.sessionId,
      trace_id: state.traceId,
      span_id: state.spanId,
      parent_span_id: state.parentSpanId,
      trace_name: state.traceName,
      template_hash: templateHash(state.request),
      unit_name: state.context.unitName,
      unit_count: state.context.unitCount,
      tool_calls: fullTools ? persistedTools : toolNames(state.request),
      tool_names: observedToolNames?.length ? observedToolNames : undefined,
      endpoint: state.endpoint,
      request_id: get(response, "_request_id") ?? get(response, "response_id")
        ?? get(response, "responseId") ?? get(response, "id")
        ?? get(get(response, "response"), "id"),
      batch: state.request.batch === true,
      batch_custom_id: state.request.batch_custom_id,
      // Explicit false is the sensitive-operation opt-out; hosted ingestion
      // otherwise preserves content that is present.
      content_opted_in: captureText,
      request_json: request.value,
      response_text: output.value,
      text_truncated: request.truncated || output.truncated || toolTruncated
        || toolDefinitionsTruncated,
      // Absence distinguishes tool-free calls from invalid declarations.
      ...(toolDefinitions ? { tool_definitions: toolDefinitions } : {}),
      stream: extra.stream ?? false,
      ttft_ms: extra.ttftMs,
      func: state.context.funcName
        ?? (firstFrame ? `${firstFrame.m}:${firstFrame.f}:${firstFrame.l}` : undefined),
      module: firstFrame?.m,
      frames_json: state.frames,
      tags: state.context.tags,
      environment: this.options.environment,
      error: statusCode === "error",
      error_type: extra.error instanceof Error ? extra.error.name : undefined,
      sdk: "js",
      sdk_version: SDK_VERSION,
      runtime: typeof process === "undefined" ? "edge" : `node-${process.versions.node}`,
    });
  }
}
