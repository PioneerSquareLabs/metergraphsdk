export interface RouteConfig {
  version?: number;
  enabled?: boolean;
  incumbent_model?: string;
  challenger_model?: string;
  model?: string;
  traffic_percent?: number;
  percentage?: number;
  allocation?: number;
  salt?: string;
}

function bucket(value: string): number {
  let hash = 0xcbf29ce484222325n;
  for (const byte of new TextEncoder().encode(value)) {
    hash ^= BigInt(byte);
    hash = BigInt.asUintN(64, hash * 0x100000001b3n);
  }
  return Number(hash) / 2 ** 64 * 100;
}

export function chooseModel(
  route: string,
  fallback: string,
  sessionKey: string | undefined,
  config: RouteConfig | undefined,
): string {
  if (!config || config.enabled === false) return fallback;
  const incumbent = config.incumbent_model ?? fallback;
  const challenger = config.challenger_model ?? config.model;
  if (!challenger || !sessionKey) return incumbent;
  const percentage = Math.max(
    0,
    Math.min(100, Number(config.traffic_percent ?? config.percentage ?? config.allocation ?? 0)),
  );
  const seed = `${route}:${config.version ?? ""}:${config.salt ?? ""}:${sessionKey}`;
  return bucket(seed) < percentage ? challenger : incumbent;
}

export class ConfigPoller {
  private etag?: string;
  private routes: Record<string, RouteConfig> = {};
  private lastSuccess = 0;
  private timer?: ReturnType<typeof setInterval>;

  constructor(
    private readonly token: string,
    private readonly baseUrl: string,
    private readonly pollMs = 30_000,
    private readonly hardTtlMs = 120_000,
  ) {
    void this.poll();
    this.timer = setInterval(() => void this.poll(), Math.max(1_000, pollMs));
    this.timer.unref?.();
  }

  async poll(): Promise<boolean> {
    try {
      const headers: Record<string, string> = { Authorization: `Bearer ${this.token}` };
      if (this.etag) headers["If-None-Match"] = this.etag;
      const response = await fetch(`${this.baseUrl.replace(/\/$/, "")}/v1/config`, { headers });
      if (response.status === 304) {
        this.lastSuccess = Date.now();
        return true;
      }
      if (!response.ok) return false;
      const body = await response.json() as { routes?: Record<string, RouteConfig> };
      this.routes = body.routes ?? {};
      this.etag = response.headers.get("etag") ?? undefined;
      this.lastSuccess = Date.now();
      return true;
    } catch {
      return false;
    }
  }

  modelFor(route: string, fallback: string, sessionKey?: string): string {
    if (!this.lastSuccess || Date.now() - this.lastSuccess > this.hardTtlMs) return fallback;
    return chooseModel(route, fallback, sessionKey, this.routes[route]);
  }

  stop(): void {
    if (this.timer) clearInterval(this.timer);
  }
}
