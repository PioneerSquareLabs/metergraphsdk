// Reusable protocol doubles and helpers for the instrumentation behavioral
// parity suite. These characterize application-visible behavior of wrap() and
// the Vercel AI SDK middleware, so the doubles expose the exact objects they
// emit (results, errors, chunks) and the tests assert those references survive
// instrumentation unchanged.

import assert from "node:assert/strict";

import { CaptureRuntime, DEFAULT_TEXT_MAX_BYTES } from "../../dist/capture.js";
import { setCaptureRuntime, wrap } from "../../dist/wrap.js";

// A CaptureRuntime whose transport records enqueued rows in memory. Callers
// inspect `rows` for lifecycle assertions (status, at-most-one capture).
export function recordingRuntime(options = {}) {
  const rows = [];
  const runtime = new CaptureRuntime(
    { enqueue(row) { rows.push(row); return true; } },
    { captureText: true, appRoot: "", skipFrames: [], textMaxBytes: DEFAULT_TEXT_MAX_BYTES, ...options },
  );
  return { runtime, rows };
}

// Installs a recording runtime as the active capture runtime for the duration
// of a test and returns the shared rows array.
export function installRuntime(t, options = {}) {
  const { runtime, rows } = recordingRuntime(options);
  setCaptureRuntime(runtime);
  t.after(() => setCaptureRuntime());
  return rows;
}

function setPath(root, path, value) {
  const parts = path.split(".");
  let node = root;
  for (const part of parts.slice(0, -1)) {
    node[part] ??= {};
    node = node[part];
  }
  node[parts.at(-1)] = value;
  return root;
}

export function getPath(root, path) {
  return path.split(".").reduce((node, part) => node?.[part], root);
}

// Builds a provider client shaped so wrap() patches a single seam, wraps it,
// and returns a bound caller for that seam. `impl` receives the request the
// instrumented method forwards, so tests can observe request forwarding.
export function seamDouble({ provider, path, method, impl }) {
  const client = setPath({}, path, { [method]: impl });
  wrap(client, provider);
  const call = (...args) => getPath(client, path)[method](...args);
  return { client, call };
}

// An async-iterable provider stream double. Yields each chunk by reference,
// then optionally throws `throwAfter`. Optional finalMessage/close/abort model
// the Anthropic streaming surface. `proxy` wraps the double in a pass-through
// Proxy to exercise nested-proxy stream handling.
export function asyncIterableStream(chunks, opts = {}) {
  const base = {
    async *[Symbol.asyncIterator]() {
      for (const chunk of chunks) yield chunk;
      if (opts.throwAfter) throw opts.throwAfter;
    },
    ...opts.extra,
  };
  if ("finalMessage" in opts) base.finalMessage = async () => opts.finalMessage;
  if (opts.onClose) base.close = (...args) => opts.onClose(...args);
  if (opts.onAbort) base.abort = (...args) => opts.onAbort(...args);
  return opts.proxy
    ? new Proxy(base, { get: (target, property, receiver) => Reflect.get(target, property, receiver) })
    : base;
}

// A reader over `parts` (by reference, in order). After the parts it rejects
// with opts.error when set, otherwise reports done. cancel() records its reason
// via opts.onCancel. Models the reader the Vercel adapter drives internally.
export function readerFromParts(parts, opts = {}) {
  let index = 0;
  return {
    read() {
      if (index < parts.length) return Promise.resolve({ done: false, value: parts[index++] });
      if (opts.error) return Promise.reject(opts.error);
      return Promise.resolve({ done: true, value: undefined });
    },
    cancel(reason) {
      opts.onCancel?.(reason);
      return Promise.resolve();
    },
  };
}

// A doStream() result whose `stream` yields via the given reader. `response`
// (when provided) rides alongside, mirroring the AI SDK doStream contract.
export function streamResultFromReader(reader, response) {
  return {
    ...(response ? { response } : {}),
    stream: { getReader: () => reader },
  };
}

export async function drainIterable(iterable) {
  const out = [];
  for await (const chunk of iterable) out.push(chunk);
  return out;
}

export async function drainReadable(stream) {
  const reader = stream.getReader();
  const out = [];
  for (;;) {
    const { done, value } = await reader.read();
    if (done) return out;
    out.push(value);
  }
}

// Asserts same length and element-by-element reference identity.
export function assertSameSequence(actual, expected) {
  assert.equal(actual.length, expected.length);
  actual.forEach((item, index) => assert.equal(item, expected[index], `element ${index} identity`));
}
