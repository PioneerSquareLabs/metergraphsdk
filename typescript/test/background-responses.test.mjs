// Background-mode Responses: create({ background: true }) returns a queued
// response, and output and usage arrive on a later retrieve() or cancel().
// The call must be recorded once, from the response that first reports a
// terminal status. A call whose final result is never observed is recorded as
// "abandoned" with unknown usage, never as a completed call with zero usage.
// The network is a mocked fetch; no provider is called.

import assert from "node:assert/strict";
import test from "node:test";

import OpenAI from "openai";

import { shutdown, wrap } from "../dist/index.js";
import { CaptureRuntime } from "../dist/capture.js";
import { finishBackgroundCalls, setCaptureRuntime } from "../dist/wrap.js";

function stubRuntime(rows) {
  return new CaptureRuntime(
    { enqueue(row) { rows.push(row); return true; } },
    { captureText: true, appRoot: "", skipFrames: [], textMaxBytes: 100_000 },
  );
}

const USAGE = {
  input_tokens: 120,
  output_tokens: 45,
  total_tokens: 165,
  input_tokens_details: { cached_tokens: 0 },
  output_tokens_details: { reasoning_tokens: 0 },
};

function body(id, status, extra = {}) {
  return {
    id,
    object: "response",
    created_at: 0,
    model: "gpt-4o-mini",
    background: true,
    parallel_tool_calls: true,
    tool_choice: "auto",
    tools: [],
    status,
    output: [],
    usage: null,
    ...extra,
  };
}

function finished(id, status) {
  return body(id, status, {
    usage: USAGE,
    output: [{
      type: "message",
      id: "msg_1",
      role: "assistant",
      status: "completed",
      content: [{ type: "output_text", text: "done", annotations: [] }],
    }],
  });
}

function json(value) {
  return new Response(JSON.stringify(value), {
    status: 200,
    headers: { "content-type": "application/json" },
  });
}

// POST /responses is the create; GET /responses/{id} polls; POST .../cancel cancels.
function client(id, polls, cancelStatus = "cancelled") {
  const queue = [...polls];
  return wrap(new OpenAI({
    apiKey: "test",
    fetch: async (url, init) => {
      const path = new URL(String(url)).pathname;
      if (path.endsWith("/cancel")) return json(finished(id, cancelStatus));
      if ((init?.method ?? "GET") === "POST") return json(body(id, "queued"));
      const status = queue.shift();
      return json(["queued", "in_progress"].includes(status) ? body(id, status) : finished(id, status));
    },
  }), "openai");
}

function setup(t) {
  const rows = [];
  setCaptureRuntime(stubRuntime(rows));
  finishBackgroundCalls();
  rows.length = 0;
  t.after(() => {
    finishBackgroundCalls();
    setCaptureRuntime();
  });
  return rows;
}

test("a background response is recorded once from the completed retrieve", async (t) => {
  const rows = setup(t);
  const openai = client("resp_bg1", ["in_progress", "completed", "completed"]);
  const queued = await openai.responses.create({ model: "gpt-4o-mini", input: "hi", background: true });
  assert.equal(queued.status, "queued");
  assert.equal((await openai.responses.retrieve(queued.id)).status, "in_progress");
  assert.equal(rows.length, 0);
  assert.equal((await openai.responses.retrieve(queued.id)).status, "completed");
  await openai.responses.retrieve(queued.id);

  assert.equal(rows.length, 1);
  const [row] = rows;
  assert.equal(row.endpoint, "responses");
  assert.equal(row.status, "completed");
  assert.equal(row.input_tokens, 120);
  assert.equal(row.output_tokens, 45);
  assert.equal(row.request_id, "resp_bg1");
});

test("cancel records the cancelled terminal response", async (t) => {
  const rows = setup(t);
  const openai = client("resp_bg2", []);
  const queued = await openai.responses.create({ model: "gpt-4o-mini", input: "hi", background: true });
  await openai.responses.cancel(queued.id);
  assert.equal(rows.length, 1);
  assert.equal(rows[0].status, "cancelled");
  assert.equal(rows[0].input_tokens, 120);
});

test("a failed background response is an error", async (t) => {
  const rows = setup(t);
  const openai = client("resp_bg3", ["failed"]);
  const queued = await openai.responses.create({ model: "gpt-4o-mini", input: "hi", background: true });
  await openai.responses.retrieve(queued.id);
  assert.equal(rows.length, 1);
  assert.equal(rows[0].status, "failed");
  assert.equal(rows[0].error, true);
});

test("an unobserved background call is recorded as abandoned, not zero", async (t) => {
  const rows = setup(t);
  const openai = client("resp_bg4", ["in_progress"]);
  const queued = await openai.responses.create({ model: "gpt-4o-mini", input: "hi", background: true });
  await openai.responses.retrieve(queued.id);
  assert.equal(rows.length, 0);
  assert.equal(finishBackgroundCalls(), 1);
  assert.equal(rows.length, 1);
  assert.equal(rows[0].status, "abandoned");
  assert.equal(rows[0].input_tokens ?? null, null);
  assert.equal(rows[0].output_tokens ?? null, null);
  assert.equal(rows[0].finish_reason, "in-progress");
});

test("shutdown records unobserved background calls", async (t) => {
  const rows = setup(t);
  const openai = client("resp_bg5", []);
  await openai.responses.create({ model: "gpt-4o-mini", input: "hi", background: true });
  const runtime = stubRuntime(rows);
  setCaptureRuntime(runtime);
  await shutdown();
  assert.deepEqual(rows.map((row) => row.status), ["abandoned"]);
});

test("foreground responses are still recorded immediately", async (t) => {
  const rows = setup(t);
  const openai = wrap(new OpenAI({
    apiKey: "test",
    fetch: async () => json({ ...finished("resp_fg", "completed"), background: false }),
  }), "openai");
  await openai.responses.create({ model: "gpt-4o-mini", input: "hi" });
  assert.equal(rows.length, 1);
  assert.equal(rows[0].status, "completed");
  assert.equal(finishBackgroundCalls(), 0);
});

function sse(events) {
  const text = events
    .map((event) => `event: ${event.type}\ndata: ${JSON.stringify(event)}\n\n`)
    .join("");
  return new Response(text, { status: 200, headers: { "content-type": "text/event-stream" } });
}

test("a streamed resume is observed through its terminal event", async (t) => {
  const rows = setup(t);
  const id = "resp_bg6";
  const openai = wrap(new OpenAI({
    apiKey: "test",
    fetch: async (url, init) => {
      if ((init?.method ?? "GET") === "POST") return json(body(id, "queued"));
      assert.equal(new URL(String(url)).searchParams.get("stream"), "true");
      return sse([
        { type: "response.in_progress", sequence_number: 1, response: body(id, "in_progress") },
        { type: "response.completed", sequence_number: 2, response: finished(id, "completed") },
      ]);
    },
  }), "openai");
  const queued = await openai.responses.create({ model: "gpt-4o-mini", input: "hi", background: true });
  const seen = [];
  for await (const event of await openai.responses.retrieve(queued.id, { stream: true })) seen.push(event.type);
  assert.deepEqual(seen, ["response.in_progress", "response.completed"]);
  assert.equal(rows.length, 1);
  assert.equal(rows[0].status, "completed");
  assert.equal(rows[0].output_tokens, 45);
});

test("a background parse is held until the terminal retrieve", async (t) => {
  const rows = setup(t);
  const openai = client("resp_bg7", ["completed"]);
  const queued = await openai.responses.parse({ model: "gpt-4o-mini", input: "hi", background: true });
  assert.equal(rows.length, 0);
  await openai.responses.retrieve(queued.id);
  assert.equal(rows.length, 1);
  assert.equal(rows[0].endpoint, "responses.parse");
  assert.equal(rows[0].status, "completed");
});

test("withResponse() on retrieve still observes the terminal response", async (t) => {
  const rows = setup(t);
  const openai = client("resp_bg8", ["completed"]);
  const queued = await openai.responses.create({ model: "gpt-4o-mini", input: "hi", background: true });
  const { data, response } = await openai.responses.retrieve(queued.id).withResponse();
  assert.equal(data.status, "completed");
  assert.equal(response.status, 200);
  assert.equal(rows.length, 1);
  assert.equal(rows[0].status, "completed");
});

test("a result that cannot be inspected never breaks the caller", async (t) => {
  const rows = setup(t);
  const broken = { get status() { throw new Error("broken status"); }, id: "resp_bg9" };
  const fake = wrap({
    responses: {
      create: async () => broken,
      retrieve: async () => broken,
    },
  }, "openai");
  assert.equal(await fake.responses.create({ model: "m", input: "hi", background: true }), broken);
  assert.equal(await fake.responses.retrieve("resp_bg9"), broken);
  assert.equal(finishBackgroundCalls(), 0);
  assert.ok(rows.length <= 1);
});
