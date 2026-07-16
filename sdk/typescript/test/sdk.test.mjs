import assert from "node:assert/strict";
import { randomBytes } from "node:crypto";
import http from "node:http";
import test from "node:test";
import { gunzipSync } from "node:zlib";

import {
  DEFAULT_INGEST_URL,
  flush,
  init,
  modelFor,
  recordOutcome,
  route,
  setSession,
  shutdown,
  wrap,
} from "../dist/index.js";
import { MAX_BATCH_BYTES, Transport } from "../dist/transport.js";

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
            usage: { prompt_tokens: 8, completion_tokens: 3 },
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
  assert.equal(row.unit_name, "answer");
  assert.equal(row.content_opted_in, true);
  assert.match(row.func, /sdk\.test\.mjs/);
  assert.equal(modelFor("route-a", "fallback"), "model-a"); // shared Py/TS test vector

  const streamClient = wrap({
    chat: {
      completions: {
        async create(request) {
          assert.deepEqual(request.stream_options, { include_usage: true });
          return {
            async *[Symbol.asyncIterator]() {
              yield { choices: [{ delta: { content: "hi" } }] };
              yield { choices: [], usage: { prompt_tokens: 2, completion_tokens: 1 } };
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
  assert.equal(chunks.length, 1); // injected usage-only chunk stays invisible

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
        usage: { input_tokens: 13, output_tokens: 5 },
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
  assert.equal(metadataOnly.content_opted_in, false);
  assert.equal(metadataOnly.request_json, undefined);
  assert.equal(metadataOnly.response_text, undefined);
  const streamed = allRows.find((candidate) => candidate.model === "stream-model");
  assert.equal(streamed.input_tokens, 2);
  assert.equal(streamed.response_text, "hi");
  const openAIBatch = allRows.find((candidate) => candidate.request_id === "req_batch_js_1");
  assert.equal(openAIBatch.route, "nightly-batch");
  assert.equal(openAIBatch.batch, true);
  assert.equal(openAIBatch.batch_custom_id, "ticket-js-1");
  assert.equal(openAIBatch.input_tokens, 11);
  assert.equal(openAIBatch.response_text, "batch answer");
  const anthropicBatch = allRows.find((candidate) => candidate.model === "claude-batch");
  assert.equal(anthropicBatch.route, "nightly-batch");
  assert.equal(anthropicBatch.batch, true);
  assert.equal(anthropicBatch.batch_custom_id, "ticket-js-2");
  assert.equal(anthropicBatch.output_tokens, 5);
  assert.equal(anthropicBatch.response_text, "anthropic batch answer");
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
