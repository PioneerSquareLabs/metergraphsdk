const sensitive = new Set([
  "api-key",
  "api_key",
  "apikey",
  "authorization",
  "client_secret",
  "cookie",
  "headers",
  "id_token",
  "password",
  "proxy-authorization",
  "refresh_token",
  "secret",
  "set-cookie",
  "token",
  "x-api-key",
]);

// Header and query containers carry transport credentials and nothing analysis
// reads, so they are dropped whole. Keys compare lower-cased, so camelCase
// client options match.
const transportContainers = new Set([
  "default_headers",
  "default_query",
  "defaultheaders",
  "defaultquery",
  "extra_headers",
  "extra_query",
  "extraheaders",
  "extraquery",
  "headers",
  "query",
]);

function credentialKey(key: string): boolean {
  const lower = key.trim().toLowerCase();
  return sensitive.has(lower) || transportContainers.has(lower);
}

function withoutCredentialKeys(value: Record<string, unknown>): Record<string, unknown> {
  return Object.fromEntries(Object.entries(value).filter(([key]) => !credentialKey(key)));
}

/**
 * The provider request as captured, without its credentials.
 *
 * Credentials travel as request-level parameters and in header or query
 * containers. `extra_body` is merged into the request body, so its keys are
 * request-level too. Messages, tools and schemas are kept exactly as sent,
 * even where a name matches a credential's (a tool parameter called `token`),
 * because they are the request analysis replays.
 */
export function scrubRequest(request: unknown): unknown {
  if (!request || typeof request !== "object" || Array.isArray(request)) return request;
  const clean = withoutCredentialKeys(request as Record<string, unknown>);
  for (const key of ["extra_body", "extraBody"]) {
    const body = clean[key];
    if (body && typeof body === "object" && !Array.isArray(body)) {
      clean[key] = withoutCredentialKeys(body as Record<string, unknown>);
    }
  }
  return clean;
}

function withoutCredentialNames(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(withoutCredentialNames);
  if (value && typeof value === "object") {
    return Object.fromEntries(
      Object.entries(value as Record<string, unknown>)
        .filter(([key]) => !sensitive.has(key.trim().toLowerCase()))
        .map(([key, item]) => [key, withoutCredentialNames(item)]),
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

// Credential names are left out at every depth. This hash routes unnamed
// workloads, so its input must stay stable across SDK versions.
export function templateHash(request: Record<string, unknown>): string {
  return fnv1a(JSON.stringify(normalized(withoutCredentialNames(request))));
}
