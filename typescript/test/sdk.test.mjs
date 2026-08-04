import assert from "node:assert/strict";
import { randomBytes } from "node:crypto";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";
import http from "node:http";
import test from "node:test";
import { gunzipSync } from "node:zlib";

import OpenAI from "openai";
import Anthropic from "@anthropic-ai/sdk";
import { GoogleGenAI } from "@google/genai";
import { generateText, streamText, wrapLanguageModel } from "ai";

import {
  DEFAULT_INGEST_URL,
  flush,
  init,
  modelFor,
  recordOutcome,
  route,
  setSession,
  shutdown,
  track,
  trace,
  vercelAISDKMiddleware,
  wrap,
} from "../dist/index.js";
import { CaptureRuntime } from "../dist/capture.js";
import { MAX_BATCH_BYTES, Transport } from "../dist/transport.js";
import { FailureLogger } from "../dist/failure-log.js";
import { ConfigPoller } from "../dist/config.js";
import { setCaptureRuntime, SEAM_TABLES, OPENAI_SEAMS, ANTHROPIC_SEAMS, GOOGLE_SEAMS } from "../dist/wrap.js";

function stubRuntime(rows, options = {}) {
  return new CaptureRuntime(
    { enqueue(row) { rows.push(row); return true; } },
    { captureText: true, appRoot: "", skipFrames: [], textMaxBytes: 100 * 1024, ...options },
  );
}

function capturedResponse(row) {
  return JSON.parse(row.response_text);
}

test("wrap captures usage/context and config assignment is sticky", async (t) => {
  assert.equal(DEFAULT_INGEST_URL, "https://d2xus7mp8zdv6t.cloudfront.net");
  const batches = [];
  const server = http.createServer(async (request, response) => {
    if (request.url === "/v1/config") {
      response.writeHead(200, { "content-type": "application/json", etag: '"v1"' });
      response.end(JSON.stringify({
        routes: {
          classify: {
            version: 1,
            incumbent_model: "model-a",
            challenger_model: "model-b",
            traffic_percent: 100,
          },
          "route-a": {
            version: 4,
            incumbent_model: "model-a",
            challenger_model: "model-b",
            traffic_percent: 35,
          },
        },
      }));
      return;
    }
    const chunks = [];
    for await (const chunk of request) chunks.push(chunk);
    let body = Buffer.concat(chunks.map((chunk) => Buffer.from(chunk)));
    if (request.headers["content-encoding"] === "gzip") body = gunzipSync(body);
    batches.push(JSON.parse(body.toString()));
    response.writeHead(202);
    response.end();
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  t.after(async () => {
    await shutdown();
    await new Promise((resolve) => server.close(resolve));
  });
  const address = server.address();
  init({
    token: "mg_test",
    ingestUrl: `http://127.0.0.1:${address.port}`,
    transport: "background",
    flushMs: 60_000,
    configPollMs: 60_000,
  });
  await new Promise((resolve) => setTimeout(resolve, 30));

  const client = wrap({
    chat: {
      completions: {
        async create() {
          return {
            id: "req_1",
            usage: {
              prompt_tokens: 8,
              completion_tokens: 3,
              prompt_tokens_details: { cached_tokens: 2, cache_write_tokens: 4 },
            },
            choices: [{ message: { content: "done" }, finish_reason: "stop" }],
          };
        },
      },
    },
  }, "openai");
  wrap(client, "openai"); // idempotent
  setSession("session-1");
  await route("classify", async () => {
    const result = await client.chat.completions.create({
      model: "model-a",
      messages: [{ role: "user", content: "classify 123" }],
    });
    assert.equal(result.id, "req_1");
  }, { unit: "answer", tags: { tier: "pro" }, captureText: true });

  await client.chat.completions.create({
    model: "metadata-model",
    messages: [{ role: "user", content: "private by default" }],
  });
  assert.equal(recordOutcome("classify", {
    model: "model-a",
    taskCompleted: true,
    feedbackScore: 0.8,
    turnsToResolution: 2,
    escalated: false,
    abandoned: false,
    editDistanceRatio: 0.1,
    regenerationCount: 0,
    eventId: "outcome-1",
  }), true);

  assert.equal(await flush(), true);
  assert.equal(batches.length, 1);
  assert.equal(batches[0].schema_version, 1);
  const row = batches[0].rows[0];
  assert.equal(row.route, "classify");
  assert.equal(row.session_id, "session-1");
  assert.equal(row.input_tokens, 8);
  assert.equal(row.cache_read_tokens, 2);
  assert.equal(row.cache_write_tokens, 4);
  assert.equal(row.cost_usd, undefined);
  assert.equal(row.unit_name, "answer");
  assert.equal(row.content_opted_in, true);
  assert.match(row.func, /sdk\.test\.mjs/);
  assert.equal(modelFor("route-a", { default: "fallback" }), "model-a"); // shared Py/TS test vector
  assert.throws(() => modelFor("route-a", {}), TypeError);
  assert.throws(() => modelFor("route-a", { default: "" }), TypeError);

  const streamClient = wrap({
    chat: {
      completions: {
        async create(request) {
          assert.deepEqual(request.stream_options, { include_usage: true });
          return {
            async *[Symbol.asyncIterator]() {
              yield { choices: [{ delta: { content: "hi" } }] };
              yield {
                choices: [{
                  delta: {
                    tool_calls: [{
                      index: 0,
                      id: "call_1",
                      function: { name: "lookup", arguments: "{\"id\":" },
                    }],
                  },
                }],
              };
              yield {
                choices: [{
                  delta: {
                    tool_calls: [{
                      index: 0,
                      function: { arguments: "\"ord_1\"}" },
                    }],
                  },
                }],
              };
              yield {
                choices: [],
                usage: {
                  prompt_tokens: 2,
                  completion_tokens: 1,
                  prompt_tokens_details: { cached_tokens: 1, cache_write_tokens: 2 },
                },
              };
            },
          };
        },
      },
    },
  }, "openai");
  const chunks = [];
  const stream = await route("stream", () => streamClient.chat.completions.create({
    model: "stream-model",
    messages: [{ role: "user", content: "x".repeat(40_000) }],
    stream: true,
  }), { captureText: true });
  for await (const chunk of stream) chunks.push(chunk);
  assert.equal(chunks.length, 3); // injected usage-only chunk stays invisible

  const openAIBatchClient = wrap({
    files: {
      async content() {
        return new Response(`${JSON.stringify({
          id: "batch_req_js_1",
          custom_id: "ticket-js-1",
          response: {
            status_code: 200,
            request_id: "req_batch_js_1",
            body: {
              id: "chatcmpl_batch_js_1",
              object: "chat.completion",
              model: "gpt-batch",
              choices: [{ message: { content: "batch answer" }, finish_reason: "stop" }],
              usage: { prompt_tokens: 11, completion_tokens: 3 },
            },
          },
          error: null,
        })}\n`);
      },
    },
  }, "openai");
  const outputFile = await route(
    "nightly-batch",
    () => openAIBatchClient.files.content("file-output-js-1"),
    { captureText: true },
  );
  assert.match(await outputFile.text(), /ticket-js-1/);

  const anthropicBatchItem = {
    custom_id: "ticket-js-2",
    result: {
      type: "succeeded",
      message: {
        id: "msg_batch_js_1",
        model: "claude-batch",
        content: [{ type: "text", text: "anthropic batch answer" }],
        usage: {
          input_tokens: 13,
          output_tokens: 5,
          cache_read_input_tokens: 3,
          cache_creation_input_tokens: 7,
          cache_creation: {
            ephemeral_5m_input_tokens: 2,
            ephemeral_1h_input_tokens: 5,
          },
        },
        stop_reason: "end_turn",
      },
    },
  };
  const anthropicBatchClient = wrap({
    messages: {
      batches: {
        async results() {
          return {
            async *[Symbol.asyncIterator]() { yield anthropicBatchItem; },
          };
        },
      },
    },
  }, "anthropic");
  const batchResults = await route(
    "nightly-batch",
    () => anthropicBatchClient.messages.batches.results("msgbatch-js-1"),
    { captureText: true },
  );
  const observedBatchItems = [];
  for await (const item of batchResults) observedBatchItems.push(item);
  assert.deepEqual(observedBatchItems, [anthropicBatchItem]);

  assert.equal(await flush(), true);
  const allRows = batches.flatMap((batch) => batch.rows);
  assert.equal(allRows.length, 6);
  const outcome = allRows.find((candidate) => candidate.event_type === "outcome");
  assert.equal(outcome.event_id, "outcome-1");
  assert.equal(outcome.session_id, "session-1");
  assert.equal(outcome.task_completed, true);
  assert.equal(outcome.request_json, undefined);
  const metadataOnly = allRows.find((candidate) => candidate.model === "metadata-model");
  assert.equal(metadataOnly.content_opted_in, true);
  assert.match(metadataOnly.request_json, /private by default/);
  assert.equal(capturedResponse(metadataOnly).content, "done");
  const streamed = allRows.find((candidate) => candidate.model === "stream-model");
  assert.equal(streamed.input_tokens, 2);
  assert.equal(streamed.cache_read_tokens, 1);
  assert.equal(streamed.cache_write_tokens, 2);
  assert.equal(streamed.cost_usd, undefined);
  assert.equal(capturedResponse(streamed).content, "hi");
  assert.deepEqual(capturedResponse(streamed).tool_calls[0], {
    call_id: "call_1",
    name: "lookup",
    arguments: { id: "ord_1" },
    status: "requested",
    idempotency: "non_idempotent",
  });
  const openAIBatch = allRows.find((candidate) => candidate.request_id === "req_batch_js_1");
  assert.equal(openAIBatch.route, "nightly-batch");
  assert.equal(openAIBatch.batch, true);
  assert.equal(openAIBatch.batch_custom_id, "ticket-js-1");
  assert.equal(openAIBatch.input_tokens, 11);
  assert.equal(capturedResponse(openAIBatch).content, "batch answer");
  const anthropicBatch = allRows.find((candidate) => candidate.model === "claude-batch");
  assert.equal(anthropicBatch.route, "nightly-batch");
  assert.equal(anthropicBatch.batch, true);
  assert.equal(anthropicBatch.batch_custom_id, "ticket-js-2");
  assert.equal(anthropicBatch.output_tokens, 5);
  assert.equal(anthropicBatch.cache_read_tokens, 3);
  assert.equal(anthropicBatch.cache_write_tokens, 7);
  assert.equal(anthropicBatch.cache_write_5m_tokens, 2);
  assert.equal(anthropicBatch.cache_write_1h_tokens, 5);
  assert.equal(anthropicBatch.cost_usd, undefined);
  assert.equal(capturedResponse(anthropicBatch).content, "anthropic batch answer");
});

test("anthropic streaming preserves aggregate and TTL-specific cache writes", async (t) => {
  const rows = [];
  setCaptureRuntime(stubRuntime(rows));
  t.after(() => setCaptureRuntime());
  const client = wrap({
    messages: {
      async stream() {
        return {
          async *[Symbol.asyncIterator]() {
            yield { type: "content_block_delta", delta: { text: "ok" } };
          },
          async finalMessage() {
            return {
              content: [{ type: "text", text: "ok" }],
              usage: {
                input_tokens: 6,
                output_tokens: 2,
                cache_read_input_tokens: 3,
                cache_creation_input_tokens: 5,
                cache_creation: {
                  ephemeral_5m_input_tokens: 2,
                  ephemeral_1h_input_tokens: 3,
                },
              },
            };
          },
        };
      },
    },
  }, "anthropic");

  const stream = await client.messages.stream({ model: "claude", messages: [] });
  for await (const _chunk of stream) { /* consume */ }

  assert.equal(rows.length, 1);
  assert.equal(rows[0].cache_read_tokens, 3);
  assert.equal(rows[0].cache_write_tokens, 5);
  assert.equal(rows[0].cache_write_5m_tokens, 2);
  assert.equal(rows[0].cache_write_1h_tokens, 3);
  assert.equal(rows[0].cost_usd, undefined);
});

test("wrap captures gemini usage from non-stream and cumulative stream responses", async (t) => {
  const rows = [];
  setCaptureRuntime(stubRuntime(rows));
  t.after(() => setCaptureRuntime());
  const client = wrap({
    models: {
      async generateContent() {
        return {
          text: "gemini done",
          responseId: "resp_g_1",
          candidates: [{
            content: {
              parts: [{
                functionCall: {
                  id: "call_g_1",
                  name: "lookup_order",
                  args: { order_id: "ord_g_1" },
                },
              }],
            },
          }],
          usageMetadata: {
            promptTokenCount: 100,
            candidatesTokenCount: 20,
            cachedContentTokenCount: 10,
            thoughtsTokenCount: 5,
          },
        };
      },
      async generateContentStream() {
        return {
          async *[Symbol.asyncIterator]() {
            yield { text: "par", usageMetadata: { promptTokenCount: 100, candidatesTokenCount: 8 } };
            yield {
              text: "tial",
              usageMetadata: {
                promptTokenCount: 100,
                candidatesTokenCount: 20,
                cachedContentTokenCount: 10,
                thoughtsTokenCount: 5,
              },
            };
          },
        };
      },
    },
  });
  wrap(client, "google"); // idempotent

  const result = await client.models.generateContent({ model: "gemini-test", contents: "hello" });
  assert.equal(result.text, "gemini done");
  const stream = await client.models.generateContentStream({ model: "gemini-test", contents: "hello" });
  const chunks = [];
  for await (const chunk of stream) chunks.push(chunk);
  assert.equal(chunks.length, 2);

  assert.equal(rows.length, 2);
  const [row, streamed] = rows;
  assert.equal(row.provider, "google");
  assert.equal(row.endpoint, "models.generate_content");
  assert.equal(row.model, "gemini-test");
  assert.equal(row.input_tokens, 100);
  assert.equal(row.output_tokens, 20);
  assert.equal(row.cache_read_tokens, 10);
  assert.equal(row.reasoning_tokens, 5);
  assert.equal(capturedResponse(row).content, "gemini done");
  assert.deepEqual(capturedResponse(row).tool_calls[0], {
    call_id: "call_g_1",
    name: "lookup_order",
    arguments: { order_id: "ord_g_1" },
    status: "requested",
    idempotency: "non_idempotent",
  });
  assert.equal(row.sdk_version, "0.3.0");
  assert.equal(streamed.provider, "google");
  assert.equal(streamed.endpoint, "models.generate_content.stream");
  assert.equal(streamed.stream, true);
  assert.notEqual(streamed.ttft_ms, undefined);
  assert.equal(streamed.input_tokens, 100);
  assert.equal(streamed.output_tokens, 20);
  assert.equal(streamed.cache_read_tokens, 10);
  assert.equal(capturedResponse(streamed).content, "partial");
});

test("trace groups spans, propagates manual ids, and permits scoped opt-out", async (t) => {
  const rows = [];
  setCaptureRuntime(stubRuntime(rows));
  t.after(() => setCaptureRuntime());
  const client = wrap({
    responses: {
      async create(request) {
        return {
          id: `response-${request.input}`,
          output_text: request.input,
          usage: { input_tokens: 1, output_tokens: 1 },
        };
      },
    },
  }, "openai");
  const traceId = "a".repeat(32);
  const parentSpanId = "b".repeat(16);
  assert.equal(trace("sync-return", () => 42), 42);

  await trace("checkout", async () => {
    await client.responses.create({ model: "test", input: "first" });
    await trace("nested-reuses-active", () => (
      client.responses.create({ model: "test", input: "second" })
    ));
    await trace("explicit-fork", () => (
      client.responses.create({ model: "test", input: "forked" })
    ), { traceId: "c".repeat(32) });
  }, { traceId, parentSpanId });
  await trace("metadata-only", () => (
    client.responses.create({ model: "test", input: "private" })
  ), { captureText: false });

  assert.deepEqual(new Set(rows.slice(0, 2).map((row) => row.trace_id)), new Set([traceId]));
  assert.deepEqual(new Set(rows.slice(0, 2).map((row) => row.trace_name)), new Set(["checkout"]));
  assert.deepEqual(new Set(rows.slice(0, 2).map((row) => row.parent_span_id)), new Set([parentSpanId]));
  assert.equal(new Set(rows.slice(0, 2).map((row) => row.span_id)).size, 2);
  assert.equal(rows[2].trace_id, "c".repeat(32));
  assert.equal(rows[2].trace_name, "explicit-fork");
  assert.equal(rows[3].content_opted_in, false);
  assert.equal(rows[3].request_json, undefined);
  assert.equal(rows[3].response_text, undefined);
});

test("capture scrubs credentials, applies redaction, and independently bounds UTF-8 fields", () => {
  const rows = [];
  const runtime = stubRuntime(rows, {
    redact(value, kind) {
      return value.replaceAll("customer-secret", `<redacted-${kind}>`);
    },
  });
  const state = runtime.start("openai", "responses", {
    model: "test",
    authorization: "Bearer provider-secret",
    headers: { "x-api-key": "provider-secret" },
    input: `customer-secret${"ü".repeat(80_000)}`,
  });
  runtime.finish(state, {
    id: "response-1",
    output_text: `customer-secret${"é".repeat(80_000)}`,
    status: "completed",
  });

  const row = rows[0];
  assert.doesNotMatch(row.request_json, /provider-secret|customer-secret/);
  assert.doesNotMatch(row.response_text, /customer-secret/);
  assert.ok(Buffer.byteLength(row.request_json) <= 100 * 1024);
  assert.ok(Buffer.byteLength(row.response_text) <= 100 * 1024);
  assert.equal(row.text_truncated, true);
  assert.match(row.request_json, /<metergraph:truncated>$/);
  assert.match(row.response_text, /<metergraph:truncated>$/);
});

test("captured tool calls use response redaction and the shared UTF-8 bound", () => {
  const rows = [];
  const runtime = stubRuntime(rows, {
    redact(value, kind) {
      return value.replaceAll("customer-secret", `<redacted-${kind}>`);
    },
  });
  const state = runtime.start("openai", "responses", {
    model: "test",
    input: "invoke the tool",
    tools: [{ type: "function", function: { name: "lookup_order" } }],
  });
  runtime.finish(state, {
    id: "response-tool-1",
    output: [{
      type: "function_call",
      call_id: "call_1",
      name: "lookup_order",
      arguments: JSON.stringify({
        account: "customer-secret",
        payload: "ü".repeat(80_000),
      }),
    }],
    status: "completed",
  });

  const row = rows[0];
  assert.doesNotMatch(row.response_text, /customer-secret/);
  assert.ok(Buffer.byteLength(row.response_text) <= 100 * 1024);
  assert.equal(row.tool_calls, undefined);
  assert.equal(row.text_truncated, true);

  const small = runtime.start("openai", "responses", {
    model: "test",
    input: "invoke the tool",
    tools: [{ type: "function", function: { name: "lookup_order" } }],
  });
  runtime.finish(small, {
    id: "response-tool-2",
    output: [{
      type: "function_call",
      call_id: "call_2",
      name: "lookup_order",
      arguments: JSON.stringify({ account: "customer-secret" }),
    }],
    status: "completed",
  });
  assert.doesNotMatch(JSON.stringify(rows[1].tool_calls), /customer-secret/);
  assert.match(JSON.stringify(rows[1].tool_calls), /redacted-response/);
});

test("async provider errors are captured without changing the thrown error", async (t) => {
  const rows = [];
  setCaptureRuntime(stubRuntime(rows));
  t.after(() => setCaptureRuntime());
  class ProviderUnavailableError extends Error {
    constructor(message) {
      super(message);
      this.name = "ProviderUnavailableError";
    }
  }
  const client = wrap({
    messages: {
      async create() {
        throw new ProviderUnavailableError("provider down");
      },
    },
  }, "anthropic");

  await assert.rejects(
    client.messages.create({ model: "claude-test", messages: [] }),
    (error) => error instanceof ProviderUnavailableError
      && error.message === "provider down",
  );
  assert.equal(rows[0].error, true);
  assert.equal(rows[0].error_type, "ProviderUnavailableError");
  assert.deepEqual(capturedResponse(rows[0]).error, {
    type: "ProviderUnavailableError",
    message: "provider down",
  });
});

test("Vercel AI SDK middleware captures generateText and streamText in one trace", async (t) => {
  const rows = [];
  setCaptureRuntime(stubRuntime(rows));
  t.after(() => setCaptureRuntime());

  const usage = {
    inputTokens: { total: 12, noCache: 8, cacheRead: 4, cacheWrite: 2 },
    outputTokens: { total: 5, text: 3, reasoning: 2 },
  };
  const baseModel = {
    specificationVersion: "v4",
    provider: "openai.responses",
    modelId: "gpt-5.6-luna",
    supportedUrls: {},
    async doGenerate(options) {
      assert.equal(options.headers.authorization, "Bearer provider-secret");
      return {
        content: [{ type: "text", text: "generated answer" }],
        finishReason: { unified: "stop", raw: "stop" },
        usage,
        warnings: [],
        response: {
          id: "ai-generate-1",
          timestamp: new Date(),
          modelId: "gpt-5.6-luna",
        },
      };
    },
    async doStream() {
      return {
        stream: new ReadableStream({
          start(controller) {
            controller.enqueue({ type: "stream-start", warnings: [] });
            controller.enqueue({
              type: "response-metadata",
              id: "ai-stream-1",
              timestamp: new Date(),
              modelId: "gpt-5.6-luna",
            });
            controller.enqueue({ type: "text-start", id: "text-1" });
            controller.enqueue({ type: "text-delta", id: "text-1", delta: "streamed " });
            controller.enqueue({ type: "text-delta", id: "text-1", delta: "answer" });
            controller.enqueue({ type: "text-end", id: "text-1" });
            controller.enqueue({
              type: "finish",
              finishReason: { unified: "stop", raw: "stop" },
              usage,
            });
            controller.close();
          },
        }),
      };
    },
  };
  const model = wrapLanguageModel({
    model: baseModel,
    middleware: vercelAISDKMiddleware(),
  });
  const traceId = "a".repeat(32);

  await trace("ai-workflow", async () => {
    await route("support.answer", async () => {
      const generated = await generateText({
        model,
        prompt: "help the customer",
        headers: { authorization: "Bearer provider-secret" },
        providerOptions: { openai: { apiKey: "provider-secret" } },
      });
      assert.equal(generated.text, "generated answer");

      const streamed = streamText({ model, prompt: "stream the answer" });
      assert.equal(await streamed.text, "streamed answer");
    });
  }, { traceId });

  assert.equal(rows.length, 2);
  assert.deepEqual(rows.map((row) => row.endpoint), ["ai.doGenerate", "ai.doStream"]);
  assert.deepEqual(new Set(rows.map((row) => row.trace_id)), new Set([traceId]));
  assert.deepEqual(new Set(rows.map((row) => row.trace_name)), new Set(["ai-workflow"]));
  assert.deepEqual(new Set(rows.map((row) => row.route)), new Set(["support.answer"]));
  for (const row of rows) {
    assert.equal(row.provider, "openai");
    assert.equal(row.model, "gpt-5.6-luna");
    assert.equal(row.input_tokens, 12);
    assert.equal(row.output_tokens, 5);
    assert.equal(row.cache_read_tokens, 4);
    assert.equal(row.cache_write_tokens, 2);
    assert.equal(row.reasoning_tokens, 2);
    assert.doesNotMatch(row.request_json, /provider-secret|authorization|apiKey/);
  }
  assert.equal(rows[0].request_id, "ai-generate-1");
  assert.equal(capturedResponse(rows[0]).content, "generated answer");
  assert.equal(rows[1].request_id, "ai-stream-1");
  assert.equal(rows[1].stream, true);
  assert.equal(capturedResponse(rows[1]).content, "streamed answer");
  assert.ok(rows[1].ttft_ms >= 0);
});

test("Vercel AI SDK middleware captures gateway tool calls and preserves failures", async (t) => {
  const rows = [];
  setCaptureRuntime(stubRuntime(rows));
  t.after(() => setCaptureRuntime());
  const middleware = vercelAISDKMiddleware({ specificationVersion: "v4" });
  const model = { provider: "gateway", modelId: "anthropic/claude-sonnet-5" };
  const params = {
    prompt: [{ role: "user", content: [{ type: "text", text: "look it up" }] }],
    tools: [{ type: "function", name: "lookup_order", inputSchema: { type: "object" } }],
  };
  const response = {
    content: [
      { type: "tool-call", toolCallId: "call-1", toolName: "lookup_order", input: "{\"id\":\"ord-1\"}" },
      { type: "tool-result", toolCallId: "call-1", toolName: "lookup_order", result: { found: true } },
    ],
    finishReason: { unified: "tool-calls", raw: "tool_use" },
    usage: {
      inputTokens: { total: 20, noCache: 20, cacheRead: 0, cacheWrite: 0 },
      outputTokens: { total: 7, text: 0, reasoning: 0 },
    },
    response: { id: "gateway-1", modelId: "claude-sonnet-5" },
  };

  assert.equal(await middleware.wrapGenerate({
    model,
    params,
    doGenerate: async () => response,
    doStream: async () => assert.fail("unexpected stream fallback"),
  }), response);

  class GatewayFailure extends Error {
    constructor(message) {
      super(message);
      this.name = "GatewayFailure";
    }
  }
  const failure = new GatewayFailure("gateway unavailable");
  await assert.rejects(
    middleware.wrapGenerate({
      model,
      params,
      doGenerate: async () => { throw failure; },
      doStream: async () => assert.fail("unexpected stream fallback"),
    }),
    (error) => error === failure,
  );

  assert.equal(rows[0].provider, "anthropic");
  assert.equal(rows[0].model, "anthropic/claude-sonnet-5");
  assert.deepEqual(rows[0].tool_names, ["lookup_order"]);
  assert.deepEqual(rows[0].tool_calls, [{
    call_id: "call-1",
    name: "lookup_order",
    arguments: { id: "ord-1" },
    result: { found: true },
    status: "completed",
    idempotency: "non_idempotent",
  }]);
  assert.equal(rows[1].error, true);
  assert.equal(rows[1].error_type, "GatewayFailure");
  assert.deepEqual(capturedResponse(rows[1]).error, {
    type: "GatewayFailure",
    message: "gateway unavailable",
  });
});

test("Vercel AI SDK middleware enriches normalized usage with raw TTL details", async (t) => {
  const rows = [];
  setCaptureRuntime(stubRuntime(rows));
  t.after(() => setCaptureRuntime());

  const middleware = vercelAISDKMiddleware({ specificationVersion: "v4" });
  const response = {
    content: [{ type: "text", text: "cached" }],
    finishReason: { unified: "stop", raw: "end_turn" },
    usage: {
      inputTokens: { total: 140, noCache: 90, cacheRead: 40, cacheWrite: 10 },
      outputTokens: { total: 12, text: 10, reasoning: 2 },
      raw: {
        input_tokens: 90,
        output_tokens: 12,
        cache_read_input_tokens: 40,
        cache_creation_input_tokens: 10,
        cache_creation: {
          ephemeral_5m_input_tokens: 7,
          ephemeral_1h_input_tokens: 3,
        },
      },
    },
  };

  await middleware.wrapGenerate({
    model: { provider: "gateway", modelId: "anthropic/claude-sonnet-5" },
    params: { prompt: [] },
    doGenerate: async () => response,
    doStream: async () => assert.fail("unexpected stream fallback"),
  });

  assert.equal(rows[0].input_tokens, 140);
  assert.equal(rows[0].output_tokens, 12);
  assert.equal(rows[0].cache_read_tokens, 40);
  assert.equal(rows[0].cache_write_tokens, 10);
  assert.equal(rows[0].cache_write_5m_tokens, 7);
  assert.equal(rows[0].cache_write_1h_tokens, 3);
  assert.equal(rows[0].reasoning_tokens, 2);
});

test("track attributes rows to the wrapped function name", async (t) => {
  const rows = [];
  setCaptureRuntime(stubRuntime(rows, { captureText: false }));
  t.after(() => setCaptureRuntime());
  const client = wrap({
    chat: {
      completions: {
        async create() {
          return {
            id: "req_track",
            usage: { prompt_tokens: 8, completion_tokens: 3 },
            choices: [{ message: { content: "done" }, finish_reason: "stop" }],
          };
        },
      },
    },
  }, "openai");

  async function summarizeTickets() {
    return client.chat.completions.create({ model: "m", messages: [] });
  }
  await track(summarizeTickets)();
  await track("billing.summarize", summarizeTickets)();
  const nested = track("outer.step", async () => track("inner.step", summarizeTickets)());
  await nested();
  await client.chat.completions.create({ model: "m", messages: [] });

  assert.equal(rows[0].func, "summarizeTickets");
  assert.equal(rows[1].func, "billing.summarize");
  assert.equal(rows[2].func, "inner.step");
  assert.match(rows[3].func, /sdk\.test\.mjs/);
});

test("wrap patches responses.parse and beta.responses.create", async (t) => {
  const rows = [];
  setCaptureRuntime(stubRuntime(rows));
  t.after(() => setCaptureRuntime());

  const parsedResult = {
    id: "req_responses",
    usage: { prompt_tokens: 8, completion_tokens: 3 },
    choices: [{ message: { content: "done" }, finish_reason: "stop" }],
  };
  const client = wrap({
    responses: {
      async create() { return parsedResult; },
      async parse() { return parsedResult; },
    },
    // client.beta.responses has .create but, as of openai>=4, no .parse —
    // verified directly against the installed SDK; do not add one here.
    beta: {
      responses: {
        async create() { return parsedResult; },
      },
    },
  }, "openai");

  await client.responses.create({ model: "m" });
  await client.responses.parse({ model: "m" });
  await client.beta.responses.create({ model: "m" });

  assert.deepEqual(rows.map((row) => row.endpoint), [
    "responses",
    "responses.parse",
    "responses",
  ]);
});

test("wrap patches create and parse on chat.completions", async (t) => {
  const rows = [];
  setCaptureRuntime(stubRuntime(rows));
  t.after(() => setCaptureRuntime());

  const parsedResult = {
    id: "req_parsed",
    usage: { prompt_tokens: 8, completion_tokens: 3 },
    choices: [{ message: { content: "done" }, finish_reason: "stop" }],
  };
  const client = wrap({
    chat: {
      completions: {
        async create() { return parsedResult; },
        async parse() { return parsedResult; },
      },
    },
  }, "openai");

  await client.chat.completions.create({ model: "m", messages: [] });
  await client.chat.completions.parse({ model: "m", messages: [] });

  assert.deepEqual(rows.map((row) => row.endpoint), [
    "chat.completions",
    "chat.completions.parse",
  ]);
});

test("wrap patches beta.chat.completions.parse for openai v4.x-shaped clients", async (t) => {
  const rows = [];
  setCaptureRuntime(stubRuntime(rows));
  t.after(() => setCaptureRuntime());

  const parsedResult = {
    id: "req_beta_parsed",
    usage: { prompt_tokens: 8, completion_tokens: 3 },
    choices: [{ message: { content: "done" }, finish_reason: "stop" }],
  };
  const client = wrap({
    beta: {
      chat: {
        completions: {
          async parse() { return parsedResult; },
        },
      },
    },
  }, "openai");

  await client.beta.chat.completions.parse({ model: "m", messages: [] });

  assert.deepEqual(rows.map((row) => row.endpoint), ["chat.completions.parse"]);
});

test("wrap skips one broken seam without affecting others", async (t) => {
  const rows = [];
  setCaptureRuntime(stubRuntime(rows));
  t.after(() => setCaptureRuntime());

  const client = {
    chat: {
      completions: {
        async create() {
          return {
            id: "req_ok",
            usage: { prompt_tokens: 1, completion_tokens: 1 },
            choices: [{ message: { content: "ok" }, finish_reason: "stop" }],
          };
        },
      },
    },
  };
  Object.defineProperty(client, "responses", {
    get() { throw new Error("boom"); },
  });

  wrap(client, "openai");
  await client.chat.completions.create({ model: "m" });

  assert.deepEqual(rows.map((row) => row.endpoint), ["chat.completions"]);
});

test("wrap captures an unrelated wrapped client invoked synchronously nested inside another", async (t) => {
  const rows = [];
  setCaptureRuntime(stubRuntime(rows));
  t.after(() => setCaptureRuntime());

  const resultA = {
    id: "req_a",
    usage: { prompt_tokens: 1, completion_tokens: 1 },
    choices: [{ message: { content: "a" }, finish_reason: "stop" }],
  };
  const resultB = {
    id: "req_b",
    usage: { prompt_tokens: 1, completion_tokens: 1 },
    choices: [{ message: { content: "b" }, finish_reason: "stop" }],
  };

  // Two independent, unrelated wrapped clients — not one delegating into
  // the other as an implementation detail (that's the parse -> create
  // case the reentrancy guard exists for). Client A's own "real"
  // implementation happens to call client B synchronously as part of its
  // own work; both are genuinely separate billable requests and must both
  // be captured. The reentrancy guard must be scoped to the specific
  // owner object being re-entered, not global, or this second, unrelated
  // call gets silently swallowed.
  const clientB = wrap({
    chat: { completions: { async create() { return resultB; } } },
  }, "openai");
  const clientA = wrap({
    chat: {
      completions: {
        async create() {
          await clientB.chat.completions.create({ model: "m", messages: [] });
          return resultA;
        },
      },
    },
  }, "openai");

  await clientA.chat.completions.create({ model: "m", messages: [] });

  assert.deepEqual(rows.map((row) => row.endpoint), ["chat.completions", "chat.completions"]);
});

test("wrap never throws even if client attribute access throws", async (t) => {
  const client = {};
  Object.defineProperty(client, "responses", {
    get() { throw new Error("boom: not ready yet"); },
  });

  const warnings = [];
  const originalWarn = console.warn;
  console.warn = (...args) => warnings.push(args.join(" "));
  try {
    const result = wrap(client); // no provider override: exercises auto-detection
    assert.equal(result, client);
  } finally {
    console.warn = originalWarn;
  }
  assert.ok(warnings.some((w) => w.includes("wrap() failed")));
});

test("transport splits wire batches at 512 KiB", async (t) => {
  const wireLengths = [];
  let deliveredRows = 0;
  const server = http.createServer(async (request, response) => {
    const chunks = [];
    for await (const chunk of request) chunks.push(chunk);
    let body = Buffer.concat(chunks.map((chunk) => Buffer.from(chunk)));
    wireLengths.push(body.byteLength);
    if (request.headers["content-encoding"] === "gzip") body = gunzipSync(body);
    deliveredRows += JSON.parse(body.toString()).rows.length;
    response.writeHead(202);
    response.end();
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  t.after(async () => {
    await new Promise((resolve) => server.close(resolve));
  });
  const address = server.address();
  const transport = new Transport(
    "mg_test",
    `http://127.0.0.1:${address.port}`,
    { mode: "background", batchSize: 100, flushMs: 60_000 },
  );
  for (let index = 0; index < 6; index += 1) {
    transport.enqueue({ index, payload: randomBytes(120_000).toString("hex") });
  }
  assert.equal(await transport.flush(10_000), true);
  await transport.shutdown();

  assert.ok(wireLengths.length > 1);
  assert.ok(Math.max(...wireLengths) <= MAX_BATCH_BYTES);
  assert.equal(deliveredRows, 6);
});

test("FailureLogger logs first occurrence and suppresses repeats", () => {
  const messages = [];
  let now = 0;
  const logger = new FailureLogger(60_000, () => now, (m) => messages.push(m));
  logger.report("transport_error", "boom 1");
  now = 10_000;
  logger.report("transport_error", "boom 2");
  now = 20_000;
  logger.report("transport_error", "boom 3");
  assert.equal(messages.length, 1);
  assert.match(messages[0], /boom 1/);
});

test("FailureLogger reports suppressed count after quiet window", () => {
  const messages = [];
  let now = 0;
  const logger = new FailureLogger(60_000, () => now, (m) => messages.push(m));
  logger.report("transport_error", "boom 1");
  now = 10_000;
  logger.report("transport_error", "boom 2");
  now = 70_000;
  logger.report("transport_error", "boom 3");
  assert.equal(messages.length, 2);
  assert.match(messages[1], /1 more suppressed/);
  assert.match(messages[1], /boom 3/);
});

test("FailureLogger tracks kinds independently", () => {
  const messages = [];
  let now = 0;
  const logger = new FailureLogger(60_000, () => now, (m) => messages.push(m));
  logger.report("transport_error", "t1");
  logger.report("client_error", "c1");
  assert.equal(messages.length, 2);
});

test("FailureLogger bounds log volume under sustained failure", () => {
  const messages = [];
  let now = 0;
  const logger = new FailureLogger(60_000, () => now, (m) => messages.push(m));
  for (let i = 0; i < 1000; i += 1) logger.report("transport_error", "boom");
  assert.equal(messages.length, 1);
});

test("transport auth failure is fatal and logged once", async (t) => {
  const server = http.createServer((request, response) => {
    request.resume();
    response.writeHead(401);
    response.end();
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  t.after(() => new Promise((resolve) => server.close(resolve)));
  const address = server.address();

  const warnings = [];
  const originalWarn = console.warn;
  console.warn = (...args) => warnings.push(args.join(" "));
  try {
    const transport = new Transport("mg_test", `http://127.0.0.1:${address.port}`, { mode: "buffered" });
    transport.enqueue({ payload: "one" });
    await transport.flush(2000);
    transport.enqueue({ payload: "two" });
    await transport.flush(2000);
  } finally {
    console.warn = originalWarn;
  }
  const authWarnings = warnings.filter((w) => w.includes("authentication failed"));
  assert.equal(authWarnings.length, 1);
});

test("transport permanent client error drops the batch but transport stays alive", async (t) => {
  let attempts = 0;
  const server = http.createServer((request, response) => {
    attempts += 1;
    request.resume();
    response.writeHead(400);
    response.end();
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  t.after(() => new Promise((resolve) => server.close(resolve)));
  const address = server.address();

  const warnings = [];
  const originalWarn = console.warn;
  console.warn = (...args) => warnings.push(args.join(" "));
  try {
    const transport = new Transport("mg_test", `http://127.0.0.1:${address.port}`, { mode: "buffered" });
    transport.enqueue({ payload: "one" });
    await transport.flush(2000);
    transport.enqueue({ payload: "two" });
    await transport.flush(2000);
  } finally {
    console.warn = originalWarn;
  }
  // A 400 is specific to the rejected batch, not the whole connection: the
  // transport must still attempt the second, unrelated batch.
  assert.equal(attempts, 2);
  assert.ok(warnings.some((w) => w.includes("HTTP 400")));
});

test("transport 413 splits an oversized batch and delivers the pieces", async (t) => {
  const receivedBatches = [];
  const server = http.createServer(async (request, response) => {
    const chunks = [];
    for await (const chunk of request) chunks.push(chunk);
    let body = Buffer.concat(chunks.map((chunk) => Buffer.from(chunk)));
    if (request.headers["content-encoding"] === "gzip") body = gunzipSync(body);
    const rows = JSON.parse(body.toString()).rows;
    receivedBatches.push(rows.length);
    if (rows.length > 1) {
      response.writeHead(413);
    } else {
      response.writeHead(202);
    }
    response.end();
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  t.after(() => new Promise((resolve) => server.close(resolve)));
  const address = server.address();

  const transport = new Transport("mg_test", `http://127.0.0.1:${address.port}`, { mode: "buffered" });
  for (let i = 0; i < 4; i += 1) transport.enqueue({ index: i });
  assert.equal(await transport.flush(10_000), true);

  assert.ok(receivedBatches.some((size) => size > 1)); // a multi-row batch hit 413 at least once
  assert.equal(receivedBatches.filter((size) => size === 1).length, 4); // every row eventually delivered on its own
});

test("transport server error retries and is not fatal", async (t) => {
  let attempts = 0;
  const server = http.createServer((request, response) => {
    request.resume();
    attempts += 1;
    response.writeHead(500);
    response.end();
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  t.after(() => new Promise((resolve) => server.close(resolve)));
  const address = server.address();

  const warnings = [];
  const originalWarn = console.warn;
  console.warn = (...args) => warnings.push(args.join(" "));
  try {
    const transport = new Transport("mg_test", `http://127.0.0.1:${address.port}`, { mode: "buffered" });
    transport.enqueue({ payload: "one" });
    await transport.flush(2000);
  } finally {
    console.warn = originalWarn;
  }
  assert.equal(attempts, 1);
  assert.ok(warnings.some((w) => w.includes("HTTP 500")));
});

test("config poller stops polling and logs once on auth failure", async (t) => {
  let attempts = 0;
  const server = http.createServer((request, response) => {
    attempts += 1;
    request.resume();
    response.writeHead(401);
    response.end();
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  t.after(() => new Promise((resolve) => server.close(resolve)));
  const address = server.address();

  const warnings = [];
  const originalWarn = console.warn;
  console.warn = (...args) => warnings.push(args.join(" "));
  let poller;
  try {
    poller = new ConfigPoller("mg_test", `http://127.0.0.1:${address.port}`, 60_000, 120_000);
    await poller.ready;
    await poller.poll();
    await poller.poll();
  } finally {
    console.warn = originalWarn;
    poller?.stop();
  }
  assert.equal(attempts, 1); // must stop after the first 401, not keep polling
  const authWarnings = warnings.filter((w) => w.includes("authentication failed"));
  assert.equal(authWarnings.length, 1);
});

test("config poller logs generic failures via FailureLogger", async (t) => {
  const server = http.createServer((request, response) => {
    request.resume();
    response.writeHead(500);
    response.end();
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  t.after(() => new Promise((resolve) => server.close(resolve)));
  const address = server.address();

  const warnings = [];
  const originalWarn = console.warn;
  console.warn = (...args) => warnings.push(args.join(" "));
  let poller;
  try {
    poller = new ConfigPoller("mg_test", `http://127.0.0.1:${address.port}`, 60_000, 120_000);
    const ok = await poller.poll();
    assert.equal(ok, false);
  } finally {
    console.warn = originalWarn;
    poller?.stop();
  }
  assert.ok(warnings.some((w) => w.includes("config poll to")));
});

function resolveSeam(client, path) {
  let obj = client;
  for (const part of path.split(".")) {
    obj = obj?.[part];
    if (obj == null) return undefined;
  }
  return obj;
}

function missingSeams(client, seams) {
  const missing = [];
  for (const seam of seams) {
    const owner = resolveSeam(client, seam.path);
    if (owner == null || typeof owner[seam.method] !== "function") {
      missing.push(`${seam.path}.${seam.method}`);
    }
  }
  return missing;
}

test("openai seams exist on the real SDK", () => {
  const client = new OpenAI({ apiKey: "test" });
  // beta.chat.completions.parse is intentionally v4.x-only (see the comment
  // on OPENAI_SEAMS and metergraph-internal#9) and is expected to be absent
  // on the latest openai package this test is pinned to — exempt it from
  // the "everything must exist" check rather than asserting it's missing.
  const seams = OPENAI_SEAMS.filter(
    (seam) => !(seam.path === "beta.chat.completions" && seam.method === "parse"),
  );
  assert.deepEqual(missingSeams(client, seams), []);
});

test("anthropic seams exist on the real SDK", () => {
  const client = new Anthropic({ apiKey: "test" });
  assert.deepEqual(missingSeams(client, ANTHROPIC_SEAMS), []);
});

test("google seams exist on the real SDK", () => {
  const client = new GoogleGenAI({ apiKey: "test" });
  assert.deepEqual(missingSeams(client, GOOGLE_SEAMS), []);
});

test("typescript seam endpoints match shared fixture", () => {
  const fixturePath = path.join(
    path.dirname(fileURLToPath(import.meta.url)),
    "..", "..", "python", "tests", "fixtures", "seam_endpoints.json",
  );
  const expected = JSON.parse(readFileSync(fixturePath, "utf8"));
  for (const [provider, endpoints] of Object.entries(expected)) {
    const actual = [...new Set(SEAM_TABLES[provider].map((seam) => seam.endpoint))].sort();
    assert.deepEqual(actual, [...endpoints].sort(), provider);
  }
});
