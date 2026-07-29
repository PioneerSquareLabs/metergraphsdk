/**
 * Rate-limited failure logging. Logs the first occurrence of a failure kind
 * immediately, then suppresses repeats within a quiet window and reports how
 * many were suppressed on the next log line for that kind.
 */
export class FailureLogger {
  private readonly lastLogged = new Map<string, number>();
  private readonly suppressed = new Map<string, number>();

  constructor(
    private readonly quietMs: number = 60_000,
    private readonly clock: () => number = Date.now,
    private readonly sink: (message: string) => void = (m) => console.warn(m),
  ) {}

  report(kind: string, message: string): void {
    const now = this.clock();
    const last = this.lastLogged.get(kind);
    if (last !== undefined && now - last < this.quietMs) {
      this.suppressed.set(kind, (this.suppressed.get(kind) ?? 0) + 1);
      return;
    }
    const suppressedCount = this.suppressed.get(kind) ?? 0;
    this.suppressed.delete(kind);
    const suffix = suppressedCount
      ? ` (${suppressedCount} more suppressed in the last ${Math.round(this.quietMs / 1000)}s)`
      : "";
    this.sink(`metergraph: ${message}${suffix}`);
    this.lastLogged.set(kind, now);
  }
}
