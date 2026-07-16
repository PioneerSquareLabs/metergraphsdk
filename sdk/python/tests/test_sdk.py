from __future__ import annotations

import asyncio
import gzip
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import metergraph
from metergraph import _capture
from metergraph._capture import Options, Runtime
from metergraph._config import choose_model
from metergraph._template import template_hash
from metergraph._transport import Writer


class Rows:
    def __init__(self):
        self.rows = []

    def enqueue(self, row):
        self.rows.append(row)
        return True


def test_hosted_default_is_https():
    assert metergraph.DEFAULT_INGEST_URL == "https://d2xus7mp8zdv6t.cloudfront.net"


def response(text="done"):
    usage = SimpleNamespace(
        prompt_tokens=12,
        completion_tokens=4,
        prompt_tokens_details=SimpleNamespace(cached_tokens=3),
    )
    message = SimpleNamespace(content=text)
    return SimpleNamespace(
        id="req_1",
        usage=usage,
        choices=[SimpleNamespace(message=message, finish_reason="stop")],
    )


def test_wrap_sync_records_usage_context_and_preserves_response(tmp_path):
    rows = Rows()
    _capture.set_runtime(
        Runtime(rows, Options(app_root=str(Path(__file__).parents[1])))
    )

    class Completions:
        def create(self, **kwargs):
            return response()

    client = SimpleNamespace(
        chat=SimpleNamespace(completions=Completions()), responses=None
    )
    metergraph.wrap(client, provider="openai")
    metergraph.wrap(client, provider="openai")  # idempotent
    metergraph.set_session("conversation-7")
    with metergraph.route(
        "ticket-classifier",
        unit="answer",
        tags={"tier": "pro"},
        capture_text=True,
    ):
        result = client.chat.completions.create(
            model="gpt-test",
            messages=[{"role": "user", "content": "classify ticket 123"}],
            tools=[{"type": "function", "function": {"name": "lookup"}}],
        )

    assert result.id == "req_1"
    assert len(rows.rows) == 1
    row = rows.rows[0]
    assert row["route"] == "ticket-classifier"
    assert row["session_id"] == "conversation-7"
    assert row["input_tokens"] == 12
    assert row["cache_read_tokens"] == 3
    assert row["unit_name"] == "answer"
    assert row["conversation_id"] == "conversation-7"
    assert row["tool_calls"] is None
    assert row["content_opted_in"] is True
    assert row["request_json"]
    assert row["func"].endswith(
        ":test_wrap_sync_records_usage_context_and_preserves_response"
    )
    _capture.set_runtime(None)


def test_openai_completed_tool_history_is_replay_grade(tmp_path):
    rows = Rows()
    runtime = Runtime(rows, Options(app_root=str(tmp_path), capture_text=True))
    call = runtime.call_state(
        "openai",
        "chat.completions",
        {
            "model": "gpt-test",
            "tools": [
                {
                    "type": "function",
                    "x-metergraph-idempotency": "idempotent",
                    "function": {"name": "lookup_order"},
                }
            ],
            "messages": [
                {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {
                                "name": "lookup_order",
                                "arguments": '{"order_id":"ord_1"}',
                            },
                        }
                    ],
                },
                {
                    "role": "tool",
                    "tool_call_id": "call_1",
                    "content": '{"status":"shipped"}',
                },
                {"role": "user", "content": "When does it arrive?"},
            ],
        },
    )

    call.finish(response())

    assert rows.rows[0]["tool_calls"] == [
        {
            "call_id": "call_1",
            "name": "lookup_order",
            "arguments": {"order_id": "ord_1"},
            "result": {"status": "shipped"},
            "status": "completed",
            "idempotency": "idempotent",
        }
    ]


def test_anthropic_response_tool_use_is_requested_not_replayable(tmp_path):
    rows = Rows()
    runtime = Runtime(rows, Options(app_root=str(tmp_path), capture_text=True))
    call = runtime.call_state(
        "anthropic",
        "messages",
        {
            "model": "claude-test",
            "tools": [{"name": "create_refund", "input_schema": {}}],
            "messages": [{"role": "user", "content": "Refund it"}],
        },
    )
    result = SimpleNamespace(
        content=[
            SimpleNamespace(
                type="tool_use",
                id="toolu_1",
                name="create_refund",
                input={"invoice_id": "inv_1"},
            )
        ],
        usage=SimpleNamespace(input_tokens=10, output_tokens=5),
        stop_reason="tool_use",
    )

    call.finish(result)

    assert rows.rows[0]["tool_calls"] == [
        {
            "call_id": "toolu_1",
            "name": "create_refund",
            "arguments": {"invoice_id": "inv_1"},
            "result": None,
            "status": "requested",
            "idempotency": "non_idempotent",
        }
    ]


def test_openai_batch_output_file_captures_each_inference(tmp_path):
    rows = Rows()
    _capture.set_runtime(Runtime(rows, Options(app_root=str(tmp_path))))
    output = {
        "id": "batch_req_1",
        "custom_id": "ticket-1",
        "response": {
            "status_code": 200,
            "request_id": "req_batch_1",
            "body": {
                "id": "chatcmpl_batch_1",
                "object": "chat.completion",
                "model": "gpt-batch",
                "choices": [
                    {
                        "message": {"content": "batch answer"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 11, "completion_tokens": 3},
            },
        },
        "error": None,
    }

    class Files:
        def content(self, file_id):
            return SimpleNamespace(content=(json.dumps(output) + "\n").encode())

    client = SimpleNamespace(files=Files())
    metergraph.wrap(client, provider="openai")
    with metergraph.route("nightly-batch", capture_text=True):
        result = client.files.content("file-output-1")
    assert result.content

    row = rows.rows[0]
    assert row["route"] == "nightly-batch"
    assert row["provider"] == "openai"
    assert row["model"] == "gpt-batch"
    assert row["batch"] is True
    assert row["batch_custom_id"] == "ticket-1"
    assert row["input_tokens"] == 11
    assert row["output_tokens"] == 3
    assert row["response_text"] == "batch answer"
    assert row["request_id"] == "req_batch_1"

    # Re-reading the same output file in one process cannot double count it.
    client.files.content("file-output-1")
    assert len(rows.rows) == 1
    _capture.set_runtime(None)


def test_anthropic_batch_results_capture_usage_without_changing_iteration(tmp_path):
    rows = Rows()
    _capture.set_runtime(Runtime(rows, Options(app_root=str(tmp_path))))
    item = SimpleNamespace(
        custom_id="ticket-2",
        result=SimpleNamespace(
            type="succeeded",
            message=SimpleNamespace(
                id="msg_batch_1",
                model="claude-batch",
                content=[SimpleNamespace(text="anthropic batch answer")],
                usage=SimpleNamespace(input_tokens=13, output_tokens=5),
                stop_reason="end_turn",
            ),
        ),
    )

    class Batches:
        def results(self, batch_id):
            return [item]

    client = SimpleNamespace(messages=SimpleNamespace(batches=Batches()))
    metergraph.wrap(client, provider="anthropic")
    with metergraph.route("nightly-batch", capture_text=True):
        result = client.messages.batches.results("msgbatch-1")
    assert list(result) == [item]

    row = rows.rows[0]
    assert row["route"] == "nightly-batch"
    assert row["provider"] == "anthropic"
    assert row["model"] == "claude-batch"
    assert row["batch"] is True
    assert row["batch_custom_id"] == "ticket-2"
    assert row["input_tokens"] == 13
    assert row["output_tokens"] == 5
    assert row["response_text"] == "anthropic batch answer"
    _capture.set_runtime(None)


def test_content_defaults_off_and_route_can_override_global_consent(tmp_path):
    rows = Rows()
    runtime = Runtime(rows, Options(app_root=str(tmp_path)))
    call = runtime.call_state(
        "openai", "responses", {"model": "test", "input": "private"}
    )
    call.finish(response("private output"))

    assert rows.rows[0]["content_opted_in"] is False
    assert rows.rows[0]["request_json"] is None
    assert rows.rows[0]["response_text"] is None

    _capture.set_runtime(runtime)

    class Responses:
        def create(self, **kwargs):
            return response("consented output")

    client = SimpleNamespace(responses=Responses())
    metergraph.wrap(client, provider="openai")
    with metergraph.route("consented", capture_text=True):
        client.responses.create(model="test", input="consented input")

    assert rows.rows[1]["content_opted_in"] is True
    assert "consented input" in rows.rows[1]["request_json"]
    assert rows.rows[1]["response_text"] == "consented output"
    _capture.set_runtime(None)


def test_route_opt_out_overrides_global_content_capture(tmp_path):
    rows = Rows()
    runtime = Runtime(rows, Options(app_root=str(tmp_path), capture_text=True))
    _capture.set_runtime(runtime)

    class Responses:
        def create(self, **kwargs):
            return response("private output")

    client = SimpleNamespace(responses=Responses())
    metergraph.wrap(client, provider="openai")
    with metergraph.route("metadata-only", capture_text=False):
        client.responses.create(model="test", input="private input")

    assert rows.rows[0]["content_opted_in"] is False
    assert rows.rows[0]["request_json"] is None
    assert rows.rows[0]["response_text"] is None
    _capture.set_runtime(None)


def test_wrap_async_errors_are_recorded_and_original_error_is_raised(tmp_path):
    rows = Rows()
    _capture.set_runtime(
        Runtime(rows, Options(app_root=str(tmp_path), capture_text=True))
    )

    class Messages:
        async def create(self, **kwargs):
            raise ValueError("provider down")

    client = SimpleNamespace(messages=Messages())
    metergraph.wrap(client, provider="anthropic")

    async def run():
        try:
            await client.messages.create(model="claude-test", messages=[])
        except ValueError as exc:
            assert str(exc) == "provider down"
        else:
            raise AssertionError("original exception was not raised")

    asyncio.run(run())
    assert rows.rows[0]["error"] is True
    assert rows.rows[0]["error_type"] == "ValueError"
    _capture.set_runtime(None)


def test_stream_records_ttft_and_final_usage(tmp_path):
    rows = Rows()
    _capture.set_runtime(
        Runtime(rows, Options(app_root=str(tmp_path), capture_text=True))
    )

    class Completions:
        def create(self, **kwargs):
            return iter(
                [
                    SimpleNamespace(
                        choices=[SimpleNamespace(delta=SimpleNamespace(content="hi"))],
                        usage=None,
                    ),
                    SimpleNamespace(
                        choices=[],
                        usage=SimpleNamespace(prompt_tokens=2, completion_tokens=1),
                    ),
                ]
            )

    client = SimpleNamespace(chat=SimpleNamespace(completions=Completions()))
    metergraph.wrap(client, provider="openai")
    chunks = list(
        client.chat.completions.create(model="gpt-test", messages=[], stream=True)
    )
    # The SDK-added OpenAI usage-only chunk is consumed for metering but is
    # not exposed to an application that did not ask for it.
    assert len(chunks) == 1
    assert rows.rows[0]["stream"] is True
    assert rows.rows[0]["ttft_ms"] is not None
    assert rows.rows[0]["input_tokens"] == 2
    assert rows.rows[0]["response_text"] == "hi"
    assert rows.rows[0]["request_json"].find("include_usage") >= 0
    _capture.set_runtime(None)


def test_async_stream_awaits_anthropic_final_message(tmp_path):
    rows = Rows()
    _capture.set_runtime(
        Runtime(rows, Options(app_root=str(tmp_path), capture_text=True))
    )

    class Stream:
        def __aiter__(self):
            async def chunks():
                yield SimpleNamespace(
                    type="content_block_delta", delta=SimpleNamespace(text="ok")
                )

            return chunks()

        async def get_final_message(self):
            return SimpleNamespace(
                usage=SimpleNamespace(input_tokens=6, output_tokens=2),
                content=[SimpleNamespace(text="ok")],
                stop_reason="end_turn",
            )

    class Messages:
        def stream(self, **kwargs):
            return Stream()

    client = SimpleNamespace(messages=Messages())
    metergraph.wrap(client, provider="anthropic")

    async def run():
        return [
            chunk async for chunk in client.messages.stream(model="claude", messages=[])
        ]

    assert len(asyncio.run(run())) == 1
    assert rows.rows[0]["input_tokens"] == 6
    assert rows.rows[0]["response_text"] == "ok"
    _capture.set_runtime(None)


def test_template_hash_strips_common_interpolated_values():
    first = {"messages": [{"content": "ticket 123 for a@example.com"}]}
    second = {"messages": [{"content": "ticket 987 for b@example.com"}]}
    assert template_hash(first) == template_hash(second)


def test_canary_assignment_is_sticky_and_fail_open():
    config = {
        "enabled": True,
        "version": 4,
        "incumbent_model": "model-a",
        "challenger_model": "model-b",
        "traffic_percent": 35,
    }
    choices = [
        choose_model("route-a", "fallback", "session-1", config) for _ in range(5)
    ]
    assert choices == ["model-a"] * 5  # shared Py/TS FNV-1a/64 test vector
    assert choose_model("route-a", "fallback", None, config) == "model-a"
    assert choose_model("route-a", "fallback", "session-1", None) == "fallback"


def test_record_outcome_uses_the_async_content_free_channel(monkeypatch):
    rows = Rows()
    monkeypatch.setattr(metergraph, "_writer", rows)
    metergraph.set_session("outcome-session")

    assert metergraph.record_outcome(
        "ticket-classifier",
        model="deepseek/v3.2",
        task_completed=True,
        feedback_score=0.8,
        turns_to_resolution=2,
        escalated=False,
        abandoned=False,
        edit_distance_ratio=0.1,
        regeneration_count=0,
        event_id="outcome-1",
    )
    row = rows.rows[0]
    assert row["event_type"] == "outcome"
    assert row["event_id"] == "outcome-1"
    assert row["route"] == "ticket-classifier"
    assert row["session_id"] == "outcome-session"
    assert row["model"] == "deepseek/v3.2"
    assert row["task_completed"] is True
    assert row["feedback_score"] == 0.8
    assert "request_json" not in row
    assert "response_text" not in row
    assert not metergraph.record_outcome(
        "ticket-classifier",
        model="deepseek/v3.2",
        task_completed=True,
        feedback_score=2,
    )


def test_writer_gzips_large_batches_and_flushes():
    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            if self.headers.get("Content-Encoding") == "gzip":
                body = gzip.decompress(body)
            received.append((self.headers, json.loads(body)))
            self.send_response(202)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    writer = Writer(
        "mg_test", f"http://127.0.0.1:{server.server_port}", flush_seconds=5
    )
    writer.enqueue({"payload": "x" * 40_000})
    assert writer.flush(2)
    writer.shutdown()
    server.shutdown()

    assert received[0][0]["Content-Encoding"] == "gzip"
    assert received[0][1]["schema_version"] == 1
    assert received[0][1]["rows"][0]["payload"].startswith("x")


def test_writer_splits_wire_batches_at_512_kib():
    wire_lengths = []
    delivered_rows = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            wire_lengths.append(len(body))
            if self.headers.get("Content-Encoding") == "gzip":
                body = gzip.decompress(body)
            delivered_rows.extend(json.loads(body)["rows"])
            self.send_response(202)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    writer = Writer(
        "mg_test",
        f"http://127.0.0.1:{server.server_port}",
        batch_size=100,
        flush_seconds=5,
    )
    for index in range(6):
        writer.enqueue({"index": index, "payload": os.urandom(120_000).hex()})
    assert writer.flush(10)
    writer.shutdown()
    server.shutdown()

    assert len(wire_lengths) > 1
    assert max(wire_lengths) <= 512 * 1024
    assert sorted(row["index"] for row in delivered_rows) == list(range(6))
