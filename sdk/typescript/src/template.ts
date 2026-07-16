const sensitive = new Set(["api_key", "apikey", "authorization", "headers", "token", "secret"]);

export function scrub(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(scrub);
  if (value && typeof value === "object") {
    return Object.fromEntries(
      Object.entries(value as Record<string, unknown>)
        .filter(([key]) => !sensitive.has(key.toLowerCase()))
        .map(([key, item]) => [key, scrub(item)]),
    );
  }
  return value;
}

function normalized(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(normalized);
  if (value && typeof value === "object") {
    return Object.fromEntries(
      Object.entries(value as Record<string, unknown>)
        .sort(([a], [b]) => a.localeCompare(b))
        .map(([key, item]) => [key, normalized(item)]),
    );
  }
  if (typeof value !== "string") return value;
  return value
    .replace(/\b[0-9a-f]{8}-[0-9a-f-]{27,}\b/gi, "<uuid>")
    .replace(/\b[^\s@]+@[^\s@]+\.[^\s@]+\b/g, "<email>")
    .replace(/\bhttps?:\/\/\S+/g, "<url>")
    .replace(/\b[A-Za-z0-9_-]{24,}\b/g, "<token>")
    .replace(/(^|[^A-Za-z])[-+]?\d+(?:\.\d+)?(?=$|[^A-Za-z])/g, "$1<n>")
    .replace(/\s+/g, " ")
    .trim();
}

function fnv1a(value: string): string {
  let hash = 0xcbf29ce484222325n;
  for (let index = 0; index < value.length; index += 1) {
    hash ^= BigInt(value.charCodeAt(index));
    hash = BigInt.asUintN(64, hash * 0x100000001b3n);
  }
  return hash.toString(16).padStart(16, "0");
}

export function templateHash(request: Record<string, unknown>): string {
  return fnv1a(JSON.stringify(normalized(scrub(request))));
}
