/**
 * The narrow adapter boundary deferred() drives, plus a worker-only OpenAI
 * Batch API implementation. Deliberately duck-typed against the openai
 * package's shape (see OpenAIBatchCapableClient) rather than importing it —
 * `openai` is an optional peer dependency of this package, and structural
 * typing already lets a real `OpenAI` client instance satisfy this
 * interface without any import, type-only or otherwise.
 *
 * Every method here returns only bounded, structural information — a
 * status, a result the caller explicitly asked for, and a boolean noting
 * whether that result happened to contain a tool-call plan. No adapter
 * method inspects or logs a provider error body, a credential, or a raw
 * prompt.
 */

/** A caller's own provider request body (e.g. an OpenAI Responses payload).
 * Only `stream` and `tools` are inspected by this module; every other
 * field passes through untouched. */
export type DeferredRequest = Record<string, unknown> & {
  stream?: boolean;
  tools?: unknown[];
};

export interface BatchHandle {
  /** The provider's own batch identifier — never returned to a caller of
   * deferred() or logged; adapters use it only to poll/read their own
   * batch. */
  providerBatchId: string;
}

export type BatchPollStatus = "pending" | "completed" | "failed" | "expired";

export interface BatchPollResult {
  status: BatchPollStatus;
}

export interface ProviderBatchResult<TResult = unknown> {
  result: TResult;
  /** Whether this result's own output happens to include a tool/function
   * call — reported as a boolean only, never by exposing the call's
   * arguments or the surrounding response through this flag. */
  containedToolCallPlan: boolean;
}

export interface ProviderBatchEligibility {
  eligible: boolean;
  reason?: string;
}

/**
 * The seam deferred() is written against. An adapter submits exactly one
 * request per batch and knows how to poll it, read its terminal result,
 * and run the same request directly (bypassing batch entirely) as a
 * fallback.
 */
export interface ProviderBatchAdapter<TResult = unknown> {
  eligibility(request: DeferredRequest): ProviderBatchEligibility;
  submitOne(request: DeferredRequest): Promise<BatchHandle>;
  poll(handle: BatchHandle): Promise<BatchPollResult>;
  readResult(handle: BatchHandle): Promise<ProviderBatchResult<TResult>>;
  direct(request: DeferredRequest): Promise<ProviderBatchResult<TResult>>;
}

// ---------- OpenAI ----------

/** The minimal slice of the `openai` package's client shape this adapter
 * calls. A real `OpenAI` client instance satisfies this structurally. */
export interface OpenAIBatchCapableClient {
  files: {
    create(params: { file: Blob; purpose: string }): Promise<{ id: string }>;
    content(fileId: string): Promise<{ text(): Promise<string> }>;
  };
  batches: {
    create(params: {
      input_file_id: string;
      endpoint: string;
      completion_window: string;
    }): Promise<{ id: string; status?: string }>;
    retrieve(batchId: string): Promise<{
      id: string;
      status?: string;
      output_file_id?: string | null;
    }>;
  };
  responses: {
    create(request: Record<string, unknown>): Promise<Record<string, unknown>>;
  };
}

const OPENAI_BATCH_ENDPOINT = "/v1/responses";
const OPENAI_COMPLETION_WINDOW = "24h";

// "validating" | "in_progress" | "finalizing" | "cancelling" all fall
// through to "pending" below, alongside any future status OpenAI adds —
// only a recognized terminal status is ever treated as terminal.
const OPENAI_FAILED_STATUSES = new Set(["failed", "cancelled"]);

interface OpenAIBatchHandle extends BatchHandle {
  customId: string;
}

function get(value: unknown, key: string): unknown {
  return value && typeof value === "object" ? (value as Record<string, unknown>)[key] : undefined;
}

function hasFunctionCall(body: unknown): boolean {
  const output = get(body, "output");
  if (!Array.isArray(output)) return false;
  return output.some((item) => get(item, "type") === "function_call");
}

function randomCustomId(): string {
  if (typeof globalThis.crypto?.randomUUID === "function") return globalThis.crypto.randomUUID();
  return `deferred-${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
}

export class ProviderBatchError extends Error {}

export function createOpenAIBatchAdapter(
  client: OpenAIBatchCapableClient,
): ProviderBatchAdapter {
  return {
    eligibility(request) {
      if (request.stream === true) {
        return { eligible: false, reason: "streaming requests are direct-only" };
      }
      return { eligible: true };
    },

    async submitOne(request) {
      const customId = randomCustomId();
      const line = JSON.stringify({
        custom_id: customId,
        method: "POST",
        url: OPENAI_BATCH_ENDPOINT,
        body: request,
      });
      const file = new Blob([`${line}\n`], { type: "application/jsonl" });
      const uploaded = await client.files.create({ file, purpose: "batch" });
      const batch = await client.batches.create({
        input_file_id: uploaded.id,
        endpoint: OPENAI_BATCH_ENDPOINT,
        completion_window: OPENAI_COMPLETION_WINDOW,
      });
      const handle: OpenAIBatchHandle = { providerBatchId: batch.id, customId };
      return handle;
    },

    async poll(handle) {
      const batch = await client.batches.retrieve(handle.providerBatchId);
      const status = batch.status ?? "";
      if (status === "completed") return { status: "completed" };
      if (status === "expired") return { status: "expired" };
      if (OPENAI_FAILED_STATUSES.has(status)) return { status: "failed" };
      return { status: "pending" };
    },

    async readResult(handle) {
      const openaiHandle = handle as OpenAIBatchHandle;
      const batch = await client.batches.retrieve(handle.providerBatchId);
      const outputFileId = batch.output_file_id;
      if (!outputFileId) {
        throw new ProviderBatchError("openai batch has no output file to read");
      }
      const file = await client.files.content(outputFileId);
      const text = await file.text();
      for (const rawLine of text.split(/\r?\n/)) {
        const line = rawLine.trim();
        if (!line) continue;
        let item: unknown;
        try {
          item = JSON.parse(line);
        } catch {
          continue;
        }
        if (get(item, "custom_id") !== openaiHandle.customId) continue;
        const response = get(item, "response");
        const statusCode = Number(get(response, "status_code"));
        const body = get(response, "body");
        const error = get(item, "error");
        if (error != null || (Number.isFinite(statusCode) && statusCode >= 400)) {
          // Never surface the provider's own error text — only that this
          // one item failed.
          throw new ProviderBatchError("openai batch item returned an error response");
        }
        return { result: body as unknown, containedToolCallPlan: hasFunctionCall(body) };
      }
      throw new ProviderBatchError("openai batch output did not contain our submitted item");
    },

    async direct(request) {
      const response = await client.responses.create(request);
      return { result: response, containedToolCallPlan: hasFunctionCall(response) };
    },
  };
}
