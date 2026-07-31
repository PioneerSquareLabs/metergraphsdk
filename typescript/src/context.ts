import { AsyncLocalStorage } from "node:async_hooks";
import { randomBytes } from "node:crypto";

export interface CaptureContext {
  route?: string;
  sessionId?: string;
  tags: Record<string, string>;
  unitName?: string;
  unitCount?: number;
  captureText?: boolean;
  funcName?: string;
  traceId?: string;
  traceName?: string;
  parentSpanId?: string;
}
export interface RouteOptions {
  unit?: string;
  unitCount?: number;
  tags?: Record<string, unknown>;
  captureText?: boolean;
}
export interface TraceOptions {
  traceId?: string;
  parentSpanId?: string;
  captureText?: boolean;
}

const storage = new AsyncLocalStorage<CaptureContext>();
let ambient: CaptureContext = { tags: {} };

export function contextSnapshot(): CaptureContext {
  const value = storage.getStore() ?? ambient;
  return { ...value, tags: { ...value.tags } };
}

export function runWithContext<T>(context: CaptureContext, fn: () => T): T {
  return storage.run(context, fn);
}

export async function route<T>(
  name: string,
  fn: () => T | Promise<T>,
  options: RouteOptions = {},
): Promise<T> {
  const parent = contextSnapshot();
  const child: CaptureContext = {
    ...parent,
    route: name,
    tags: {
      ...parent.tags,
      ...Object.fromEntries(
        Object.entries(options.tags ?? {}).map(([key, value]) => [key, String(value)]),
      ),
    },
    unitName: options.unit ?? parent.unitName,
    unitCount: options.unit ? (options.unitCount ?? 1) : parent.unitCount,
    captureText: options.captureText ?? parent.captureText,
  };
  return storage.run(child, fn);
}

export function trace<T>(
  name: string,
  fn: () => T | Promise<T>,
  options: TraceOptions = {},
): T | Promise<T> {
  const parent = contextSnapshot();
  const requested = options.traceId?.trim();
  const reuse = parent.traceId !== undefined
    && (requested === undefined || requested === parent.traceId);
  const child: CaptureContext = {
    ...parent,
    traceId: reuse ? parent.traceId : requested || randomBytes(16).toString("hex"),
    traceName: reuse ? parent.traceName : String(name),
    parentSpanId: options.parentSpanId ?? (reuse ? parent.parentSpanId : undefined),
    captureText: options.captureText ?? parent.captureText,
  };
  return storage.run(child, fn);
}

export function setSession(sessionId?: string): void {
  const store = storage.getStore();
  if (store) store.sessionId = sessionId;
  else ambient = { ...ambient, sessionId };
}

export function setTags(tags: Record<string, unknown>): void {
  const normalized = Object.fromEntries(
    Object.entries(tags).map(([key, value]) => [key, String(value)]),
  );
  const store = storage.getStore();
  if (store) store.tags = { ...store.tags, ...normalized };
  else ambient = { ...ambient, tags: { ...ambient.tags, ...normalized } };
}
