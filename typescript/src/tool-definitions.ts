/**
 * The canonical view of the tool declarations a request carried.
 *
 * A provider request states its tools in one of five dialects, and MeterGraph
 * captured only their names. A name is not a schema: it cannot tell an analysis
 * what the model was allowed to ask for, so a tool-using call arrived
 * unreplayable. This module reads every declaration a request carries and
 * records it once, in one shape, preserving order and the declared schema
 * exactly.
 *
 * Where a declaration is unreadable, incomplete, duplicated, or where two
 * schema keys disagree, the record says so rather than inventing a schema. The
 * envelope's `scope` says whether the declarations came from one container or
 * several, because nothing in the capture reveals the precedence a provider
 * applies across competing containers.
 */

export const TOOL_DEFINITIONS_VERSION = 1;

// Read in this order, so a request carrying tools in more than one place
// produces the same records every time.
const CONTAINERS: [string, string[]][] = [
  ["tools", ["tools"]],
  ["config.tools", ["config", "tools"]],
  ["extra_body.tools", ["extra_body", "tools"]],
];

// Precedence within one Gemini declaration. Listed once, applied everywhere.
const SCHEMA_KEYS = ["parameters_json_schema", "parametersJsonSchema", "parameters"];

const GEMINI_NATIVE_TOOLS = new Set([
  "google_search",
  "google_search_retrieval",
  "code_execution",
  "url_context",
]);

const IDENTITY_MAX_BYTES = 512;
const INDEX_MAX = 10_000;

const RECORD_KEYS = [
  "index",
  "container",
  "kind",
  "dialect",
  "name",
  "description",
  "schema_key",
  "schema",
  "status",
];
const OPTIONAL_RECORD_KEYS = ["provider_type", "duplicate_of"];
const KINDS = new Set(["function", "provider", "unknown"]);
const DIALECTS = new Set([
  "anthropic",
  "openai_chat",
  "openai_responses",
  "gemini",
  "ai_sdk",
  "unknown",
]);
const STATUSES = new Set([
  "declared",
  "incomplete",
  "malformed",
  "unsupported",
  "provider_tool",
  "ambiguous",
]);
const CONTAINER_NAMES = new Set(CONTAINERS.map(([name]) => name));
const SCHEMA_KEY_NAMES = new Set([...SCHEMA_KEYS, "input_schema", "inputSchema"]);

// A provider-native tool declares a type, not a schema, so its dialect is only
// known where the captured provider is itself unambiguous. OpenAI stays unknown:
// `{"type": "web_search_preview"}` is the same entry on Chat and on Responses.
const PROVIDER_DIALECTS: Record<string, string> = { anthropic: "anthropic", google: "gemini" };

export interface ToolDeclaration {
  index: number;
  container: string;
  kind: string;
  dialect: string;
  name: string | null;
  description: string | null;
  schema_key: string | null;
  schema: Record<string, unknown> | null;
  status: string;
  provider_type?: string;
  duplicate_of?: number;
}

export interface ToolDefinitions {
  version: number;
  fidelity: string;
  scope: string;
  declarations: ToolDeclaration[];
}

function attribute(value: unknown, name: string): unknown {
  if (value == null || typeof value !== "object") return undefined;
  return (value as Record<string, unknown>)[name];
}

function has(value: unknown, name: string): boolean {
  if (value == null || typeof value !== "object") return false;
  return name in (value as Record<string, unknown>)
    && (value as Record<string, unknown>)[name] !== undefined;
}

function isMapping(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** A declared identity string, or null when it is absent or unusable. */
function identity(value: unknown): string | null {
  if (typeof value !== "string") return null;
  return new TextEncoder().encode(value).byteLength <= IDENTITY_MAX_BYTES ? value : null;
}

/** JSON data, or undefined where a value cannot be represented as JSON. */
function jsonValue(value: unknown): unknown {
  if (value === null) return null;
  const kind = typeof value;
  if (kind === "string" || kind === "boolean") return value;
  if (kind === "number") return Number.isFinite(value as number) ? value : undefined;
  if (kind === "bigint" || kind === "function" || kind === "symbol" || kind === "undefined") {
    return undefined;
  }
  if (Array.isArray(value)) {
    const items: unknown[] = [];
    for (const item of value) {
      const converted = jsonValue(item);
      if (converted === undefined) return undefined;
      items.push(converted);
    }
    return items;
  }
  if (isMapping(value)) {
    const entries: [string, unknown][] = [];
    for (const [key, item] of Object.entries(value)) {
      if (item === undefined) continue;
      const converted = jsonValue(item);
      if (converted === undefined) return undefined;
      entries.push([key, converted]);
    }
    // `Object.fromEntries` defines own properties, so a schema carrying a
    // legal `__proto__` key keeps it instead of rewriting the clone's
    // prototype and losing the declaration it claims to copy verbatim.
    return Object.fromEntries(entries);
  }
  return undefined;
}

/**
 * Semantic JSON equality: object key order ignored, array order kept, and
 * booleans distinguished from numbers. `JSON.stringify` compares key insertion
 * order, which would call two identical schemas different.
 */
export function jsonEqual(left: unknown, right: unknown): boolean {
  if (typeof left === "boolean" || typeof right === "boolean") return left === right;
  if (Array.isArray(left) || Array.isArray(right)) {
    if (!Array.isArray(left) || !Array.isArray(right) || left.length !== right.length) {
      return false;
    }
    return left.every((item, index) => jsonEqual(item, right[index]));
  }
  if (isMapping(left) || isMapping(right)) {
    if (!isMapping(left) || !isMapping(right)) return false;
    const leftKeys = Object.keys(left);
    const rightKeys = Object.keys(right);
    if (leftKeys.length !== rightKeys.length) return false;
    return leftKeys.every(
      (key) => Object.prototype.hasOwnProperty.call(right, key) && jsonEqual(left[key], right[key]),
    );
  }
  return left === right;
}

function record(
  index: number,
  container: string,
  fields: {
    kind: string;
    dialect: string;
    status: string;
    name?: unknown;
    description?: unknown;
    schemaKey?: string | null;
    schema?: unknown;
    providerType?: unknown;
  },
): ToolDeclaration {
  const entry: ToolDeclaration = {
    index,
    container,
    kind: fields.kind,
    dialect: fields.dialect,
    name: identity(fields.name),
    description: typeof fields.description === "string" ? fields.description : null,
    schema_key: fields.schemaKey ?? null,
    schema: (fields.schema ?? null) as Record<string, unknown> | null,
    status: fields.status,
  };
  const providerType = identity(fields.providerType);
  if (providerType !== null) entry.provider_type = providerType;
  return entry;
}



/** One caller-authored function declaration, read without normalization. */
function functionRecord(
  index: number,
  container: string,
  dialect: string,
  entry: unknown,
  schemaKeys: string[],
): ToolDeclaration {
  const name = attribute(entry, "name");
  const description = attribute(entry, "description");
  const converted: [string, unknown][] = [];
  for (const key of schemaKeys) {
    if (!has(entry, key)) continue;
    const value = jsonValue(attribute(entry, key));
    if (value === undefined) {
      // A schema the request serializer cannot hold is not a schema this view
      // can carry. `request_json` still shows whatever was sent.
      return record(index, container, {
        kind: "function", dialect, status: "unsupported", name, description,
      });
    }
    converted.push([key, value]);
  }
  if (converted.length > 1) {
    const [, first] = converted[0]!;
    if (converted.slice(1).some(([, value]) => !jsonEqual(value, first))) {
      // Which key the provider honors is not knowable from the capture, so
      // neither is the effective schema.
      return record(index, container, {
        kind: "function", dialect, status: "ambiguous", name, description,
      });
    }
  }
  const [schemaKey, schema] = converted.length ? converted[0]! : [null, null];
  const nameMalformed = name !== undefined && name !== null && identity(name) === null;
  const schemaMalformed = converted.length > 0 && !isMapping(schema);
  if (nameMalformed || schemaMalformed) {
    return record(index, container, {
      kind: "function",
      dialect,
      status: "malformed",
      name: nameMalformed ? null : name,
      description,
      schemaKey: schemaMalformed ? null : (schemaKey as string | null),
      schema: schemaMalformed ? null : schema,
    });
  }
  if (name === undefined || name === null || converted.length === 0) {
    return record(index, container, {
      kind: "function",
      dialect,
      status: "incomplete",
      name,
      description,
      schemaKey: schemaKey as string | null,
      schema,
    });
  }
  return record(index, container, {
    kind: "function",
    dialect,
    status: "declared",
    name,
    description,
    schemaKey: schemaKey as string | null,
    schema,
  });
}

/** Every record one container entry produces, in declaration order. */
function entryRecords(
  index: number,
  container: string,
  entry: unknown,
  provider?: string,
): ToolDeclaration[] {
  if (!isMapping(entry)) {
    return [record(index, container, { kind: "unknown", dialect: "unknown", status: "unsupported" })];
  }
  const declarations = attribute(entry, "function_declarations")
    ?? attribute(entry, "functionDeclarations");
  if (Array.isArray(declarations)) {
    const records = declarations.map((declaration, position) =>
      functionRecord(index + position, container, "gemini", declaration, SCHEMA_KEYS));
    return records.length
      ? records
      : [record(index, container, { kind: "unknown", dialect: "unknown", status: "unsupported" })];
  }
  const fn = attribute(entry, "function");
  if (isMapping(fn)) {
    return [functionRecord(index, container, "openai_chat", fn, ["parameters"])];
  }
  if (has(entry, "input_schema")) {
    return [functionRecord(index, container, "anthropic", entry, ["input_schema"])];
  }
  if (has(entry, "inputSchema")) {
    return [functionRecord(index, container, "ai_sdk", entry, ["inputSchema"])];
  }
  const declaredType = attribute(entry, "type");
  if (declaredType === "function") {
    // A function stays a function even with no name: it is incomplete, not a
    // provider-native tool.
    return [functionRecord(index, container, "openai_responses", entry, ["parameters"])];
  }
  if (typeof declaredType === "string") {
    return [record(index, container, {
      kind: "provider",
      dialect: PROVIDER_DIALECTS[provider ?? ""] ?? "unknown",
      status: "provider_tool",
      name: attribute(entry, "name"),
      providerType: declaredType,
    })];
  }
  const keys = Object.keys(entry);
  if (keys.length === 1) {
    const key = keys[0]!;
    if (GEMINI_NATIVE_TOOLS.has(key) && isMapping(entry[key])) {
      return [record(index, container, {
        kind: "provider", dialect: "gemini", status: "provider_tool", name: key,
      })];
    }
  }
  return [record(index, container, { kind: "unknown", dialect: "unknown", status: "unsupported" })];
}

function containerValue(request: Record<string, unknown>, path: string[]): [boolean, unknown] {
  let value: unknown = request;
  for (const name of path) {
    if (!has(value, name)) return [false, undefined];
    value = attribute(value, name);
  }
  return [true, value];
}

/**
 * The declarations a request carries, and whether one container supplied them.
 * Returns undefined when the request declares no tools at all, so an absent
 * field and an empty declaration list are never confused.
 */
export function readDeclarations(
  request: Record<string, unknown>,
  provider?: string,
): { declarations: ToolDeclaration[]; scope: string } | undefined {
  const records: ToolDeclaration[] = [];
  let contributors = 0;
  for (const [container, path] of CONTAINERS) {
    const [present, value] = containerValue(request, path);
    if (!present) continue;
    const before = records.length;
    if (!Array.isArray(value)) {
      // A tools key that is not a list still happened. Saying so is not the
      // same as saying no tools were declared.
      records.push(record(records.length, container, {
        kind: "unknown", dialect: "unknown", status: "unsupported",
      }));
    } else {
      for (const entry of value) {
        records.push(...entryRecords(records.length, container, entry, provider));
      }
    }
    if (records.length > before) contributors += 1;
  }
  if (!records.length || records.length > INDEX_MAX) return undefined;
  const seen = new Map<string, number>();
  for (const entry of records) {
    if (entry.name === null) continue;
    const first = seen.get(entry.name);
    if (first === undefined) seen.set(entry.name, entry.index);
    else entry.duplicate_of = first;
  }
  return { declarations: records, scope: contributors <= 1 ? "effective" : "inventory" };
}

/**
 * Whether a declarations array still matches the contract. Applied to the
 * redaction hook's output, which is caller-supplied and may return anything.
 */
export function validDeclarations(value: unknown): value is ToolDeclaration[] {
  if (!Array.isArray(value) || !value.length) return false;
  for (const entry of value) {
    if (!isMapping(entry)) return false;
    const keys = Object.keys(entry);
    if (!RECORD_KEYS.every((key) => keys.includes(key))) return false;
    if (keys.some((key) => !RECORD_KEYS.includes(key) && !OPTIONAL_RECORD_KEYS.includes(key))) {
      return false;
    }
    const index = entry.index;
    if (typeof index !== "number" || !Number.isInteger(index) || index < 0 || index >= INDEX_MAX) {
      return false;
    }
    if ("duplicate_of" in entry) {
      const duplicate = entry.duplicate_of;
      if (
        typeof duplicate !== "number" || !Number.isInteger(duplicate)
        || duplicate < 0 || duplicate >= INDEX_MAX
      ) return false;
    }
    if (typeof entry.kind !== "string" || !KINDS.has(entry.kind)) return false;
    if (typeof entry.dialect !== "string" || !DIALECTS.has(entry.dialect)) return false;
    if (typeof entry.status !== "string" || !STATUSES.has(entry.status)) return false;
    if (typeof entry.container !== "string" || !CONTAINER_NAMES.has(entry.container)) return false;
    if (entry.schema_key !== null
      && (typeof entry.schema_key !== "string" || !SCHEMA_KEY_NAMES.has(entry.schema_key))) {
      return false;
    }
    for (const key of ["name", "provider_type"]) {
      const text = (entry as Record<string, unknown>)[key];
      if (text !== undefined && text !== null && identity(text) === null) return false;
    }
    if (entry.description !== null && typeof entry.description !== "string") return false;
    if (entry.schema !== null && !isMapping(entry.schema)) return false;
  }
  return true;
}

export function toolDefinitionsEnvelope(
  declarations: ToolDeclaration[],
  scope: string,
  fidelity: string,
): ToolDefinitions {
  return { version: TOOL_DEFINITIONS_VERSION, fidelity, scope, declarations };
}
