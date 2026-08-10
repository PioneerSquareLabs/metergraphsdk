/**
 * Repository-aware ingest protocol v2: app-token -> session-token exchange.
 *
 * The app token is sent only on POST /v1/ingest/sessions. The resulting
 * session token is cached in memory and reused until it's within
 * REFRESH_MARGIN_MS of expiry, at which point the next getToken() call
 * transparently re-exchanges it. Exchanges happen lazily, off the caller's
 * request path (Transport.deliver already runs asynchronously), so a slow
 * round trip here adds no latency to the customer's LLM call. Every failure
 * mode is fail-open: getToken() resolves to undefined and callers drop that
 * batch rather than ever sending the app token to /v1/ingest.
 */
import { FailureLogger } from "./failure-log.js";

export const DEFAULT_TIMEOUT_MS = 3_000;
export const DEFAULT_TTL_MS = 300_000;
export const REFRESH_MARGIN_MS = 30_000;
export const MAX_BACKOFF_MS = 60_000;

function parseExpiresAt(value: unknown): number | undefined {
  if (typeof value !== "string") return undefined;
  const parsed = Date.parse(value);
  return Number.isNaN(parsed) ? undefined : parsed;
}

export class SessionManager {
  private readonly url: string;
  private readonly failureLog = new FailureLogger();
  private token: string | undefined;
  private expiresAt = 0;
  private stopped = false;
  private retryAt = 0;
  private backoffMs = 1_000;

  constructor(
    private readonly appToken: string,
    baseUrl: string,
    private readonly repository: string,
    private readonly sdkVersion: string,
    private readonly timeoutMs: number = DEFAULT_TIMEOUT_MS,
  ) {
    this.url = `${baseUrl.replace(/\/$/, "")}/v1/ingest/sessions`;
  }

  async getToken(): Promise<string | undefined> {
    if (this.stopped) return undefined;
    if (this.token !== undefined && Date.now() < this.expiresAt - REFRESH_MARGIN_MS) {
      return this.token;
    }
    if (Date.now() < this.retryAt) return undefined;
    await this.exchange();
    return this.stopped ? undefined : this.token;
  }

  invalidate(): void {
    this.token = undefined;
    this.expiresAt = 0;
    this.retryAt = 0;
    this.backoffMs = 1_000;
  }

  stop(): void {
    this.stopped = true;
    this.token = undefined;
    this.expiresAt = 0;
  }

  private async exchange(): Promise<void> {
    try {
      const response = await fetch(this.url, {
        method: "POST",
        headers: {
          Authorization: `Bearer ${this.appToken}`,
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          protocol_version: 2,
          repository: this.repository,
          sdk_version: this.sdkVersion,
        }),
        signal: AbortSignal.timeout(this.timeoutMs),
      });
      if (!response.ok) {
        this.failureLog.report(
          "session_exchange_error",
          `session exchange to ${this.url} failed with HTTP ${response.status}`,
        );
        this.markFailed();
        return;
      }
      const doc = (await response.json()) as Record<string, unknown>;
      const token = doc?.session_token;
      if (typeof token !== "string" || !token) {
        this.failureLog.report(
          "session_exchange_error",
          `session exchange to ${this.url} returned no session_token`,
        );
        this.markFailed();
        return;
      }
      const expiresAt = parseExpiresAt(doc?.expires_at) ?? Date.now() + DEFAULT_TTL_MS;
      if (!this.stopped) {
        this.token = token;
        this.expiresAt = expiresAt;
        this.retryAt = 0;
        this.backoffMs = 1_000;
      }
    } catch (error) {
      this.failureLog.report(
        "session_exchange_error",
        `session exchange to ${this.url} failed: ${error instanceof Error ? error.message : String(error)}`,
      );
      this.markFailed();
    }
  }

  private markFailed(): void {
    this.retryAt = Date.now() + this.backoffMs;
    this.backoffMs = Math.min(this.backoffMs * 2, MAX_BACKOFF_MS);
  }
}
