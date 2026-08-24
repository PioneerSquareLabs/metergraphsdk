import { FailureLogger } from "./failure-log.js";
import type { SessionManager } from "./session.js";

export type TransportMode = "auto" | "background" | "buffered";
export type WaitUntil = (promise: Promise<unknown>) => void;
export const MAX_BATCH_BYTES = 4 * 1024 * 1024;

export interface TransportOptions {
  queueSize?: number;
  batchSize?: number;
  flushMs?: number;
  mode?: TransportMode;
  session?: SessionManager;
}

function detectedMode(): Exclude<TransportMode, "auto"> {
  const env = typeof process === "undefined" ? {} : process.env;
  const agent = typeof navigator === "undefined" ? "" : navigator.userAgent;
  return env?.AWS_LAMBDA_FUNCTION_NAME || env?.VERCEL || agent === "Cloudflare-Workers"
    ? "buffered"
    : "background";
}

async function gzipBody(body: ArrayBuffer): Promise<ArrayBuffer> {
  if (typeof CompressionStream === "undefined") return body;
  const source = new Blob([body]).stream();
  return new Response(source.pipeThrough(new CompressionStream("gzip"))).arrayBuffer();
}

export class Transport {
  private readonly queue: Record<string, unknown>[] = [];
  private readonly mode: Exclude<TransportMode, "auto">;
  private readonly queueSize: number;
  private readonly batchSize: number;
  private timer?: ReturnType<typeof setInterval>;
  private waitUntil?: WaitUntil;
  private scheduled = false;
  private inFlight: Promise<void> = Promise.resolve();
  private fatal = false;
  private dropped = 0;
  private errors = 0;
  private retryAt = 0;
  private backoffMs = 1_000;
  private readonly failureLog = new FailureLogger();
  private readonly session?: SessionManager;

  constructor(
    private readonly token: string,
    private readonly baseUrl: string,
    options: TransportOptions = {},
  ) {
    this.session = options.session;
    this.queueSize = Math.max(1, options.queueSize ?? 2_000);
    this.batchSize = Math.max(1, Math.min(1_000, options.batchSize ?? 100));
    this.mode = options.mode === "auto" || !options.mode ? detectedMode() : options.mode;
    if (this.mode === "background") {
      this.timer = setInterval(() => void this.flush(), Math.max(50, options.flushMs ?? 5_000));
      this.timer.unref?.();
    }
  }

  bindWaitUntil(value: { waitUntil(promise: Promise<unknown>): void } | WaitUntil): void {
    this.waitUntil = typeof value === "function" ? value : value.waitUntil.bind(value);
  }

  enqueue(row: Record<string, unknown>): boolean {
    if (this.fatal || this.queue.length >= this.queueSize) {
      this.dropped += 1;
      return false;
    }
    this.queue.push(row);
    if (this.queue.length >= this.batchSize) this.schedule();
    else if (this.mode === "buffered") this.schedule();
    return true;
  }

  private schedule(): void {
    if (this.scheduled) return;
    this.scheduled = true;
    const work = Promise.resolve().then(async () => {
      this.scheduled = false;
      await this.flush();
    });
    if (this.waitUntil) this.waitUntil(work);
    else if (this.mode === "buffered") void work;
  }

  async flush(timeoutMs = 3_000): Promise<boolean> {
    const deadline = Date.now() + Math.max(0, timeoutMs);
    const work = async () => {
      while (this.queue.length && Date.now() < deadline) {
        const batch = this.queue.splice(0, this.batchSize);
        await this.deliver(batch);
      }
    };
    this.inFlight = this.inFlight.then(work, work);
    await new Promise<void>((resolve) => {
      const timer = setTimeout(resolve, Math.max(0, deadline - Date.now()));
      timer.unref?.();
      void this.inFlight.finally(() => {
        clearTimeout(timer);
        resolve();
      });
    });
    return this.queue.length === 0;
  }

  private markTransientFailure(rowCount: number): void {
    this.errors += 1;
    this.dropped += rowCount;
    this.retryAt = Date.now() + this.backoffMs;
    this.backoffMs = Math.min(this.backoffMs * 2, 60_000);
  }

  private async deliver(rows: Record<string, unknown>[]): Promise<void> {
    if (this.fatal || Date.now() < this.retryAt) {
      this.dropped += rows.length;
      return;
    }
    const token = this.session ? await this.session.getToken() : this.token;
    if (token === undefined) {
      this.dropped += rows.length;
      return;
    }
    const encoded = new TextEncoder().encode(JSON.stringify({
      schema_version: 1,
      rows,
      meta: { dropped: this.dropped, transport_errors: this.errors },
    }));
    const raw = encoded.buffer.slice(
      encoded.byteOffset, encoded.byteOffset + encoded.byteLength,
    ) as ArrayBuffer;
    const compressed = raw.byteLength > 32 * 1024;
    const body = compressed ? await gzipBody(raw) : raw;
    if (body.byteLength > MAX_BATCH_BYTES) {
      if (rows.length === 1) {
        this.dropped += 1;
        return;
      }
      const midpoint = Math.floor(rows.length / 2);
      await this.deliver(rows.slice(0, midpoint));
      await this.deliver(rows.slice(midpoint));
      return;
    }
    try {
      const response = await fetch(`${this.baseUrl.replace(/\/$/, "")}/v1/ingest`, {
        method: "POST",
        headers: {
          Authorization: `Bearer ${token}`,
          "Content-Type": "application/json",
          ...(compressed && body !== raw ? { "Content-Encoding": "gzip" } : {}),
        },
        body: new Blob([body], { type: "application/json" }),
        keepalive: this.mode === "buffered" && body.byteLength <= 64 * 1024,
      });
      if (response.status === 401 || response.status === 403) {
        if (this.session) {
          this.session.invalidate();
          this.dropped += rows.length;
          return;
        }
        this.fatal = true;
        console.warn("Metergraph authentication failed; capture disabled for this process");
        return;
      }
      if (response.status === 413 && rows.length > 1) {
        const midpoint = Math.floor(rows.length / 2);
        await this.deliver(rows.slice(0, midpoint));
        await this.deliver(rows.slice(midpoint));
        return;
      }
      if ([400, 404, 413, 422].includes(response.status)) {
        this.dropped += rows.length;
        this.failureLog.report(
          "client_error",
          `ingest rejected batch with HTTP ${response.status} against ${this.baseUrl}; dropping this batch (payload-specific, not a process-wide failure)`,
        );
        return;
      }
      if (response.status !== 202) {
        this.markTransientFailure(rows.length);
        this.failureLog.report(
          "server_error",
          `ingest request failed with HTTP ${response.status} against ${this.baseUrl}`,
        );
        return;
      }
      this.backoffMs = 1_000;
      this.retryAt = 0;
    } catch (error) {
      this.markTransientFailure(rows.length);
      this.failureLog.report(
        "transport_error",
        `ingest request to ${this.baseUrl} failed: ${error instanceof Error ? error.message : String(error)}`,
      );
    }
  }

  async shutdown(): Promise<void> {
    if (this.timer) clearInterval(this.timer);
    await this.flush();
  }
}
