export const SENSITIVE_KEYS: ReadonlySet<string> = new Set([
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

export const DEFAULT_SCRUB_CATEGORIES = [
  "secret",
  "profile_url",
  "email",
  "phone",
] as const;

export type ScrubCategory = (typeof DEFAULT_SCRUB_CATEGORIES)[number];

const categorySet = new Set<string>(DEFAULT_SCRUB_CATEGORIES);
const PEM_PRIVATE_KEY = /-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----/g;
const SCHEME_CREDENTIAL = /\b(bearer|basic)([ \t\r\n\f\v]+)([A-Za-z0-9._~+/=-]{8,})/gi;
const KEY_VALUE_CREDENTIAL = new RegExp(
  String.raw`(?<![A-Za-z0-9_])(?:api[_-]?key|access[_-]?key|secret[_-]?key|client[_-]?secret|private[_-]?key|password|passwd|token|secret|authorization)(?![A-Za-z0-9_])(["']?[ \t\r\n\f\v]*[:=][ \t\r\n\f\v]*["']?)([^ \t\r\n\f\v"',;<>]{6,})`,
  "gi",
);
const PROVIDER_TOKEN = /(?<![A-Za-z0-9])(?:sk-[A-Za-z0-9_-]{8,}|AKIA[0-9A-Z]{16}|gh[pousr]_[A-Za-z0-9]{36,}|xox[abprs]-[A-Za-z0-9-]{10,}|AIza[0-9A-Za-z_-]{35}|eyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,})/g;
const PROFILE_URL = /https?:\/\/([a-z]{2,3}\.)?(www\.)?linkedin\.com\/in\/[A-Za-z0-9_.%-]+\/?/gi;
const EMAIL = /(?<![A-Za-z0-9.+_-])[A-Za-z0-9.!#$%&*+/=?^_`{|}~-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}/g;
const PHONE = /(?<![A-Za-z0-9_+])(?:\+?[0-9]{1,3}[ .-]?)?(?:\([0-9]{2,4}\)[ .-]?|[0-9]{2,4}[ .-])(?:[0-9]{2,4}[ .-])*[0-9]{3,4}(?![A-Za-z0-9_])/g;

export function removeSensitiveKeys(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(removeSensitiveKeys);
  if (value && typeof value === "object") {
    return Object.fromEntries(
      Object.entries(value as Record<string, unknown>)
        .filter(([key]) => !SENSITIVE_KEYS.has(key.trim().toLowerCase()))
        .map(([key, item]) => [key, removeSensitiveKeys(item)]),
    );
  }
  return value;
}

function categoriesOf(categories: Iterable<string>): string[] {
  const selected = [...categories];
  const unknown = selected.find((category) => !categorySet.has(category));
  if (unknown !== undefined) throw new ValueError(`unknown scrub category: ${unknown}`);
  return selected;
}

class ValueError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "ValueError";
  }
}

export function scrubText(text: string, categories: Iterable<string> = DEFAULT_SCRUB_CATEGORIES): string {
  if (typeof text !== "string") throw new TypeError("scrubText() requires a string");
  let result = text;
  for (const category of categoriesOf(categories)) {
    if (category === "secret") {
      result = result.replace(PEM_PRIVATE_KEY, "<secret>");
      result = result.replace(
        SCHEME_CREDENTIAL,
        (match: string, scheme: string, whitespace: string, candidate: string) => {
          const suspicious = /[0-9._~+/=-]/.test(candidate)
            || /[A-Z]/.test(candidate.slice(1));
          return suspicious ? `${scheme}${whitespace}<secret>` : match;
        },
      );
      result = result.replace(KEY_VALUE_CREDENTIAL, (match: string, _prefix: string, value: string, offset: number, source: string) => {
        if (["bearer", "basic"].includes(value.toLowerCase())
          && source.slice(offset + match.length).startsWith(" <secret>")) {
          return match;
        }
        return `${match.slice(0, match.length - value.length)}<secret>`;
      });
      result = result.replace(PROVIDER_TOKEN, "<secret>");
    } else if (category === "profile_url") {
      result = result.replace(PROFILE_URL, "<profile-url>");
    } else if (category === "email") {
      result = result.replace(EMAIL, "<email>");
    } else if (category === "phone") {
      result = result.replace(PHONE, (match: string) => {
        const digits = (match.match(/[0-9]/g) ?? []).length;
        if (digits < 10 || digits > 15 || /^[0-9]{1,3}(?:\.[0-9]{1,3}){3}$/.test(match)) {
          return match;
        }
        return "<phone>";
      });
    }
  }
  return result;
}

export function scrubValue(value: unknown, categories: Iterable<string> = DEFAULT_SCRUB_CATEGORIES): unknown {
  const selected = categoriesOf(categories);
  if (typeof value === "string") return scrubText(value, selected);
  if (Array.isArray(value)) return value.map((item) => scrubValue(item, selected));
  if (value && typeof value === "object") {
    return Object.fromEntries(
      Object.entries(value as Record<string, unknown>)
        .map(([key, item]) => [key, scrubValue(item, selected)]),
    );
  }
  return value;
}
