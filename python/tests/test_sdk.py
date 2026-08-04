from __future__ import annotations

import asyncio
import gzip
import json
import logging
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import metergraph
from metergraph import _capture
from metergraph._capture import Options, Runtime
from metergraph._config import ConfigPoller, choose_model
from metergraph._failure_log import FailureLogger
from metergraph._template import template_hash
from metergraph._transport import Writer


# wrap() auto-initializes from the environment; keep the suite hermetic.
os.environ.pop("METERGRAPH_APP_TOKEN", None)
os.environ.pop("METERGRAPH_INGEST_URL", None)


class Rows:
    def __init__(self):
        self.rows = []

    def enqueue(self, row):
        self.rows.append(row)
        return True


def test_hosted_default_is_https():
    assert metergraph.DEFAULT_INGEST_URL == "https://d2xus7mp8zdv6t.cloudfront.net"


def test_python_seam_endpoints_match_shared_fixture():
    fixture_path = Path(__file__).parent / "fixtures" / "seam_endpoints.json"
    expected = json.loads(fixture_path.read_text())
    actual = {
        "openai": sorted({seam.endpoint for seam in _capture.OPENAI_SEAMS}),
        "anthropic": sorted({seam.endpoint for seam in _capture.ANTHROPIC_SEAMS}),
        "google": sorted({seam.endpoint for seam in _capture.GOOGLE_SEAMS}),
    }
    for provider, endpoints in expected.items():
        assert actual[provider] == sorted(endpoints), provider


def response(text="done"):
    usage = SimpleNamespace(
        prompt_tokens=12,
        completion_tokens=4,
        prompt_tokens_details=SimpleNamespace(cached_tokens=3, cache_write_tokens=4),
    )
    message = SimpleNamespace(content=text)
    return SimpleNamespace(
        id="req_1",
        usage=usage,
        choices=[SimpleNamespace(message=message, finish_reason="stop")],
    )


def captured_response(row):
    return json.loads(row["response_text"])


def test_wrap_auto_init_does_not_latch_before_a_token_is_available():
    class Completions:
        def create(self, **kwargs):
            return response()

    client = SimpleNamespace(
        chat=SimpleNamespace(completions=Completions()), responses=None
    )
    assert metergraph.wrap(client, provider="openai") is client
    assert metergraph._writer is None  # no token: capture stays off
    metergraph.init(token="mg_test", ingest_url="http://127.0.0.1:9")
    assert metergraph._writer is not None  # a later explicit init still works
    metergraph.shutdown()


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
    assert row["cache_write_tokens"] == 4
    assert "cost_usd" not in row
    assert row["unit_name"] == "answer"
    assert row["conversation_id"] == "conversation-7"
    assert row["tool_calls"] is None
    assert row["content_opted_in"] is True
    assert row["request_json"]
    assert row["func"].endswith(
        ":test_wrap_sync_records_usage_context_and_preserves_response"
    )
    _capture.set_runtime(None)


def test_wrap_auto_detects_vercel_gateway_openai_protocol(tmp_path):
    rows = Rows()
    _capture.set_runtime(Runtime(rows, Options(app_root=str(tmp_path))))

    class Completions:
        def create(self, **kwargs):
            return response("gateway done")

    client = SimpleNamespace(
        base_url="https://ai-gateway.vercel.sh/v1/",
        chat=SimpleNamespace(completions=Completions()),
    )
    metergraph.wrap(client)
    result = client.chat.completions.create(
        model="anthropic/claude-sonnet-4.6",
        messages=[{"role": "user", "content": "hello"}],
    )

    assert result.choices[0].message.content == "gateway done"
    assert len(rows.rows) == 1
    row = rows.rows[0]
    assert row["provider"] == "anthropic"
    assert row["model"] == "anthropic/claude-sonnet-4.6"
    assert row["endpoint"] == "chat.completions"
    assert row["input_tokens"] == 12
    _capture.set_runtime(None)


def test_wrap_vercel_override_supports_custom_anthropic_protocol_url(tmp_path):
    rows = Rows()
    _capture.set_runtime(Runtime(rows, Options(app_root=str(tmp_path))))

    class Messages:
        def create(self, **kwargs):
            return SimpleNamespace(
                content=[SimpleNamespace(text="gateway done")],
                usage=SimpleNamespace(input_tokens=8, output_tokens=3),
                stop_reason="end_turn",
            )

    client = SimpleNamespace(
        base_url="https://gateway.example.test",
        messages=Messages(),
    )
    metergraph.wrap(client, provider="vercel")
    client.messages.create(
        model="openai/gpt-5.4",
        max_tokens=20,
        messages=[{"role": "user", "content": "hello"}],
    )

    assert len(rows.rows) == 1
    row = rows.rows[0]
    assert row["provider"] == "openai"
    assert row["model"] == "openai/gpt-5.4"
    assert row["endpoint"] == "messages"
    assert row["input_tokens"] == 8
    _capture.set_runtime(None)


def test_wrap_patches_create_and_parse_on_both_chat_and_beta_chat(tmp_path):
    rows = Rows()
    _capture.set_runtime(
        Runtime(rows, Options(app_root=str(Path(__file__).parents[1])))
    )

    class Completions:
        def create(self, **kwargs):
            return response()

        def parse(self, **kwargs):
            return response()

    client = SimpleNamespace(
        chat=SimpleNamespace(completions=Completions()),
        beta=SimpleNamespace(chat=SimpleNamespace(completions=Completions())),
        responses=None,
    )
    metergraph.wrap(client, provider="openai")

    client.chat.completions.create(model="gpt-test", messages=[])
    client.chat.completions.parse(model="gpt-test", messages=[])
    client.beta.chat.completions.parse(model="gpt-test", messages=[])

    assert [row["endpoint"] for row in rows.rows] == [
        "chat.completions",
        "chat.completions.parse",
        "chat.completions.parse",
    ]
    _capture.set_runtime(None)


def test_wrap_patches_responses_parse_and_beta_responses_create(tmp_path):
    rows = Rows()
    _capture.set_runtime(
        Runtime(rows, Options(app_root=str(Path(__file__).parents[1])))
    )

    class Responses:
        def create(self, **kwargs):
            return response()

        def parse(self, **kwargs):
            return response()

    class BetaResponses:
        # client.beta.responses has .create but, as of openai>=1.x, no .parse —
        # verified directly against the installed SDK; do not add a .parse here.
        def create(self, **kwargs):
            return response()

    client = SimpleNamespace(
        responses=Responses(),
        beta=SimpleNamespace(responses=BetaResponses()),
    )
    metergraph.wrap(client, provider="openai")

    client.responses.create(model="gpt-test")
    client.responses.parse(model="gpt-test")
    client.beta.responses.create(model="gpt-test")

    assert [row["endpoint"] for row in rows.rows] == [
        "responses",
        "responses.parse",
        "responses",
    ]
    _capture.set_runtime(None)


def test_wrap_skips_missing_nested_attribute_without_raising():
    client = SimpleNamespace(beta=SimpleNamespace())  # beta.responses does not exist
    result = metergraph.wrap(client, provider="openai")
    assert result is client


def test_wrap_skips_one_broken_seam_without_affecting_others(tmp_path):
    rows = Rows()
    _capture.set_runtime(
        Runtime(rows, Options(app_root=str(Path(__file__).parents[1])))
    )

    class Completions:
        def create(self, **kwargs):
            return response()

    class Chat:
        completions = Completions()

    class Client:
        chat = Chat()

        @property
        def responses(self):
            raise RuntimeError("boom: not ready yet")

    client = Client()
    metergraph.wrap(client, provider="openai")
    client.chat.completions.create(model="gpt-test")

    assert [row["endpoint"] for row in rows.rows] == ["chat.completions"]
    _capture.set_runtime(None)


def test_wrap_never_raises_even_if_client_attribute_access_raises(caplog):
    class Explosive:
        @property
        def responses(self):
            raise RuntimeError("boom: not ready yet")

    client = Explosive()
    with caplog.at_level(logging.WARNING, logger="metergraph"):
        result = metergraph.wrap(client)  # no provider override: exercises auto-detection
    assert result is client
    assert any("wrap() failed" in r.getMessage() for r in caplog.records)


def gemini_response(text="gemini done"):
    return SimpleNamespace(
        text=text,
        response_id="resp_g_1",
        model_version="gemini-test-001",
        usage_metadata=SimpleNamespace(
            prompt_token_count=100,
            candidates_token_count=20,
            cached_content_token_count=10,
            thoughts_token_count=5,
        ),
    )


def test_wrap_google_records_usage_and_endpoint(tmp_path):
    rows = Rows()
    _capture.set_runtime(
        Runtime(rows, Options(app_root=str(tmp_path), capture_text=True))
    )

    class Models:
        def generate_content(self, **kwargs):
            return gemini_response()

    client = SimpleNamespace(models=Models())
    metergraph.wrap(client)
    metergraph.wrap(client, provider="google")  # idempotent
    result = client.models.generate_content(
        model="gemini-test", contents="describe ticket 123"
    )

    assert result.text == "gemini done"
    assert len(rows.rows) == 1
    row = rows.rows[0]
    assert row["provider"] == "google"
    assert row["endpoint"] == "models.generate_content"
    assert row["model"] == "gemini-test"
    assert row["input_tokens"] == 100
    assert row["output_tokens"] == 20
    assert row["cache_read_tokens"] == 10
    assert row["reasoning_tokens"] == 5
    assert captured_response(row)["content"] == "gemini done"
    assert row["request_id"] == "resp_g_1"
    assert row["sdk_version"] == metergraph.__version__
    _capture.set_runtime(None)


def test_wrap_google_stream_takes_usage_from_cumulative_last_chunk(tmp_path):
    rows = Rows()
    _capture.set_runtime(
        Runtime(rows, Options(app_root=str(tmp_path), capture_text=True))
    )

    class Models:
        def generate_content_stream(self, **kwargs):
            return iter(
                [
                    SimpleNamespace(
                        text="par",
                        usage_metadata=SimpleNamespace(
                            prompt_token_count=100, candidates_token_count=8
                        ),
                    ),
                    SimpleNamespace(
                        text="tial",
                        usage_metadata=SimpleNamespace(
                            prompt_token_count=100,
                            candidates_token_count=20,
                            cached_content_token_count=10,
                            thoughts_token_count=5,
                        ),
                    ),
                ]
            )

    client = SimpleNamespace(models=Models())
    metergraph.wrap(client, provider="google")
    chunks = list(
        client.models.generate_content_stream(model="gemini-test", contents="x")
    )

    assert len(chunks) == 2
    row = rows.rows[0]
    assert row["endpoint"] == "models.generate_content.stream"
    assert row["stream"] is True
    assert row["ttft_ms"] is not None
    assert row["input_tokens"] == 100
    assert row["output_tokens"] == 20
    assert row["cache_read_tokens"] == 10
    assert captured_response(row)["content"] == "partial"
    _capture.set_runtime(None)


def test_wrap_google_patches_async_models(tmp_path):
    rows = Rows()
    _capture.set_runtime(
        Runtime(rows, Options(app_root=str(tmp_path), capture_text=True))
    )

    class Models:
        def generate_content(self, **kwargs):
            return gemini_response()

    class AioModels:
        async def generate_content(self, **kwargs):
            return gemini_response("gemini async done")

        async def generate_content_stream(self, **kwargs):
            async def chunks():
                yield SimpleNamespace(
                    text="gemini async stream",
                    usage_metadata=SimpleNamespace(
                        prompt_token_count=100, candidates_token_count=20
                    ),
                )

            return chunks()

    client = SimpleNamespace(models=Models(), aio=SimpleNamespace(models=AioModels()))
    metergraph.wrap(client)

    async def run():
        result = await client.aio.models.generate_content(
            model="gemini-test", contents="x"
        )
        assert result.text == "gemini async done"
        stream = await client.aio.models.generate_content_stream(
            model="gemini-test", contents="x"
        )
        return [chunk async for chunk in stream]

    assert len(asyncio.run(run())) == 1
    assert rows.rows[0]["provider"] == "google"
    assert rows.rows[0]["endpoint"] == "models.generate_content"
    assert captured_response(rows.rows[0])["content"] == "gemini async done"
    assert rows.rows[1]["endpoint"] == "models.generate_content.stream"
    assert rows.rows[1]["stream"] is True
    assert rows.rows[1]["input_tokens"] == 100
    assert captured_response(rows.rows[1])["content"] == "gemini async stream"
    _capture.set_runtime(None)


def test_track_prefers_explicit_attribution_over_stack_walk(tmp_path):
    rows = Rows()
    _capture.set_runtime(
        Runtime(rows, Options(app_root=str(Path(__file__).parents[1])))
    )

    class Completions:
        def create(self, **kwargs):
            return response()

    client = SimpleNamespace(chat=SimpleNamespace(completions=Completions()))
    metergraph.wrap(client, provider="openai")

    @metergraph.track
    def derived_name_call():
        return client.chat.completions.create(model="gpt-test", messages=[])

    @metergraph.track("billing.summarize")
    def explicit_name_call():
        return client.chat.completions.create(model="gpt-test", messages=[])

    derived_name_call()
    explicit_name_call()
    with metergraph.track("adhoc.step"):
        client.chat.completions.create(model="gpt-test", messages=[])
    client.chat.completions.create(model="gpt-test", messages=[])

    expected = f"{derived_name_call.__module__}:{derived_name_call.__qualname__}"
    assert rows.rows[0]["func"] == expected
    assert rows.rows[0]["module"] == derived_name_call.__module__
    assert rows.rows[0]["frames_json"]
    assert rows.rows[1]["func"] == "billing.summarize"
    assert rows.rows[2]["func"] == "adhoc.step"
    assert rows.rows[3]["func"].endswith(
        ":test_track_prefers_explicit_attribution_over_stack_walk"
    )
    _capture.set_runtime(None)


def test_track_nested_wins_and_async_functions_propagate(tmp_path):
    rows = Rows()
    _capture.set_runtime(Runtime(rows, Options(app_root=str(tmp_path))))

    class Completions:
        def create(self, **kwargs):
            return response()

    class Messages:
        async def create(self, **kwargs):
            return response()

    client = SimpleNamespace(chat=SimpleNamespace(completions=Completions()))
    metergraph.wrap(client, provider="openai")
    async_client = SimpleNamespace(messages=Messages())
    metergraph.wrap(async_client, provider="anthropic")

    @metergraph.track("outer.step")
    def outer():
        with metergraph.track("inner.step"):
            client.chat.completions.create(model="gpt-test", messages=[])
        return client.chat.completions.create(model="gpt-test", messages=[])

    outer()

    @metergraph.track("async.step")
    async def run():
        return await async_client.messages.create(model="claude-test", messages=[])

    asyncio.run(run())

    assert rows.rows[0]["func"] == "inner.step"
    assert rows.rows[1]["func"] == "outer.step"
    assert rows.rows[2]["func"] == "async.step"
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
    assert captured_response(rows.rows[0])["tool_calls"] == rows.rows[0]["tool_calls"]


def test_gemini_response_normalizes_function_calls(tmp_path):
    rows = Rows()
    runtime = Runtime(rows, Options(app_root=str(tmp_path)))
    call = runtime.call_state(
        "google",
        "models.generate_content",
        {
            "model": "gemini-test",
            "contents": [{"role": "user", "parts": [{"text": "Find order"}]}],
            "tools": [{"name": "lookup_order"}],
        },
    )
    result = SimpleNamespace(
        candidates=[
            SimpleNamespace(
                content=SimpleNamespace(
                    parts=[
                        SimpleNamespace(
                            functionCall=SimpleNamespace(
                                id="call_g_1",
                                name="lookup_order",
                                args={"order_id": "ord_g_1"},
                            )
                        )
                    ]
                )
            )
        ],
        usage_metadata=SimpleNamespace(
            prompt_token_count=10, candidates_token_count=2
        ),
    )

    call.finish(result)

    assert rows.rows[0]["tool_calls"] == [
        {
            "call_id": "call_g_1",
            "name": "lookup_order",
            "arguments": {"order_id": "ord_g_1"},
            "result": None,
            "status": "requested",
            "idempotency": "non_idempotent",
        }
    ]
    assert captured_response(rows.rows[0])["tool_calls"] == rows.rows[0]["tool_calls"]


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
    assert captured_response(row)["content"] == "batch answer"
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
                usage=SimpleNamespace(
                    input_tokens=13,
                    output_tokens=5,
                    cache_read_input_tokens=3,
                    cache_creation_input_tokens=7,
                    cache_creation=SimpleNamespace(
                        ephemeral_5m_input_tokens=2,
                        ephemeral_1h_input_tokens=5,
                    ),
                ),
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
    assert row["cache_read_tokens"] == 3
    assert row["cache_write_tokens"] == 7
    assert row["cache_write_5m_tokens"] == 2
    assert row["cache_write_1h_tokens"] == 5
    assert "cost_usd" not in row
    assert captured_response(row)["content"] == "anthropic batch answer"
    _capture.set_runtime(None)


def test_content_defaults_on_and_route_can_override_capture(tmp_path):
    rows = Rows()
    runtime = Runtime(rows, Options(app_root=str(tmp_path)))
    call = runtime.call_state(
        "openai", "responses", {"model": "test", "input": "private"}
    )
    call.finish(response("private output"))

    assert rows.rows[0]["content_opted_in"] is True
    assert "private" in rows.rows[0]["request_json"]
    assert captured_response(rows.rows[0])["content"] == "private output"

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
    assert captured_response(rows.rows[1])["content"] == "consented output"
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


def test_trace_groups_spans_propagates_ids_and_supports_decorators(tmp_path):
    rows = Rows()
    runtime = Runtime(rows, Options(app_root=str(tmp_path)))
    _capture.set_runtime(runtime)

    class Responses:
        def create(self, **kwargs):
            return response(kwargs["input"])

    client = SimpleNamespace(responses=Responses())
    metergraph.wrap(client, provider="openai")
    manual_trace_id = "a" * 32
    parent_span_id = "b" * 16

    with metergraph.trace(
        "checkout", trace_id=manual_trace_id, parent_span_id=parent_span_id
    ):
        client.responses.create(model="test", input="first")
        with metergraph.trace("nested-reuses-active"):
            client.responses.create(model="test", input="second")
        with metergraph.trace("explicit-fork", trace_id="c" * 32):
            client.responses.create(model="test", input="forked")

    @metergraph.trace("async-checkout")
    async def traced_async():
        await asyncio.sleep(0)
        client.responses.create(model="test", input="third")
        client.responses.create(model="test", input="fourth")

    asyncio.run(traced_async())

    assert {row["trace_id"] for row in rows.rows[:2]} == {manual_trace_id}
    assert {row["trace_name"] for row in rows.rows[:2]} == {"checkout"}
    assert {row["parent_span_id"] for row in rows.rows[:2]} == {parent_span_id}
    assert len({row["span_id"] for row in rows.rows[:2]}) == 2
    assert rows.rows[2]["trace_id"] == "c" * 32
    assert rows.rows[2]["trace_name"] == "explicit-fork"
    assert rows.rows[3]["trace_id"] == rows.rows[4]["trace_id"]
    assert rows.rows[3]["trace_name"] == "async-checkout"
    assert rows.rows[3]["trace_id"] != manual_trace_id
    _capture.set_runtime(None)


def test_trace_capture_override_scrubbing_redaction_and_utf8_limits(tmp_path):
    rows = Rows()

    def redact(value, kind):
        return value.replace("customer-secret", f"<redacted-{kind}>")

    runtime = Runtime(
        rows,
        Options(
            app_root=str(tmp_path),
            capture_text=True,
            redact=redact,
            text_max_bytes=100 * 1024,
        ),
    )
    hidden = runtime.call_state(
        "openai", "responses", {"model": "test", "input": "hidden"}
    )
    with metergraph.trace("sensitive", capture_text=False):
        # CaptureContext is read when the call starts.
        hidden = runtime.call_state(
            "openai", "responses", {"model": "test", "input": "hidden"}
        )
        hidden.finish(response("hidden output"))
    assert rows.rows[0]["content_opted_in"] is False
    assert rows.rows[0]["request_json"] is None
    assert rows.rows[0]["response_text"] is None

    call = runtime.call_state(
        "openai",
        "responses",
        {
            "model": "test",
            "authorization": "Bearer provider-secret",
            "headers": {"x-api-key": "provider-secret"},
            "input": "customer-secret" + ("ü" * 80_000),
        },
    )
    call.finish(response("customer-secret" + ("é" * 80_000)))
    row = rows.rows[1]
    assert "provider-secret" not in row["request_json"]
    assert "customer-secret" not in row["request_json"]
    assert "customer-secret" not in row["response_text"]
    assert len(row["request_json"].encode()) <= 100 * 1024
    assert len(row["response_text"].encode()) <= 100 * 1024
    assert row["text_truncated"] is True
    assert row["request_json"].endswith("<metergraph:truncated>")
    assert row["response_text"].endswith("<metergraph:truncated>")


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
    assert captured_response(rows.rows[0])["error"] == {
        "type": "ValueError",
        "message": "provider down",
    }
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
                        choices=[
                            SimpleNamespace(
                                delta=SimpleNamespace(
                                    content=None,
                                    tool_calls=[
                                        SimpleNamespace(
                                            index=0,
                                            id="call_1",
                                            function=SimpleNamespace(
                                                name="lookup", arguments='{"id":'
                                            ),
                                        )
                                    ],
                                )
                            )
                        ],
                        usage=None,
                    ),
                    SimpleNamespace(
                        choices=[
                            SimpleNamespace(
                                delta=SimpleNamespace(
                                    content=None,
                                    tool_calls=[
                                        SimpleNamespace(
                                            index=0,
                                            id=None,
                                            function=SimpleNamespace(
                                                name=None, arguments='"ord_1"}'
                                            ),
                                        )
                                    ],
                                )
                            )
                        ],
                        usage=None,
                    ),
                    SimpleNamespace(
                        choices=[],
                        usage=SimpleNamespace(
                            prompt_tokens=2,
                            completion_tokens=1,
                            prompt_tokens_details=SimpleNamespace(
                                cached_tokens=1, cache_write_tokens=2
                            ),
                        ),
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
    assert len(chunks) == 3
    assert rows.rows[0]["stream"] is True
    assert rows.rows[0]["ttft_ms"] is not None
    assert rows.rows[0]["input_tokens"] == 2
    assert rows.rows[0]["cache_read_tokens"] == 1
    assert rows.rows[0]["cache_write_tokens"] == 2
    assert "cost_usd" not in rows.rows[0]
    assert captured_response(rows.rows[0])["content"] == "hi"
    assert captured_response(rows.rows[0])["tool_calls"][0] == {
        "call_id": "call_1",
        "name": "lookup",
        "arguments": {"id": "ord_1"},
        "result": None,
        "status": "requested",
        "idempotency": "non_idempotent",
    }
    assert rows.rows[0]["request_json"].find("include_usage") >= 0
    _capture.set_runtime(None)


def test_vercel_gateway_stream_preserves_usage_and_creator(tmp_path):
    rows = Rows()
    _capture.set_runtime(
        Runtime(rows, Options(app_root=str(tmp_path), capture_text=True))
    )

    class Completions:
        def create(self, **kwargs):
            assert kwargs["stream_options"] == {"include_usage": True}
            return iter(
                [
                    SimpleNamespace(
                        choices=[SimpleNamespace(delta=SimpleNamespace(content="ok"))],
                        usage=None,
                    ),
                    SimpleNamespace(
                        choices=[],
                        usage=SimpleNamespace(
                            prompt_tokens=9,
                            completion_tokens=2,
                            prompt_tokens_details=SimpleNamespace(cached_tokens=4),
                        ),
                    ),
                ]
            )

    client = SimpleNamespace(
        base_url="https://ai-gateway.vercel.sh/v1",
        chat=SimpleNamespace(completions=Completions()),
    )
    metergraph.wrap(client)
    chunks = list(
        client.chat.completions.create(
            model="xai/grok-4.3",
            messages=[],
            stream=True,
        )
    )

    assert len(chunks) == 1
    row = rows.rows[0]
    assert row["provider"] == "xai"
    assert row["model"] == "xai/grok-4.3"
    assert row["stream"] is True
    assert row["input_tokens"] == 9
    assert row["output_tokens"] == 2
    assert row["cache_read_tokens"] == 4
    assert captured_response(row)["content"] == "ok"
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
                usage=SimpleNamespace(
                    input_tokens=6,
                    output_tokens=2,
                    cache_read_input_tokens=3,
                    cache_creation_input_tokens=5,
                    cache_creation=SimpleNamespace(
                        ephemeral_5m_input_tokens=2,
                        ephemeral_1h_input_tokens=3,
                    ),
                ),
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
    assert rows.rows[0]["cache_read_tokens"] == 3
    assert rows.rows[0]["cache_write_tokens"] == 5
    assert rows.rows[0]["cache_write_5m_tokens"] == 2
    assert rows.rows[0]["cache_write_1h_tokens"] == 3
    assert "cost_usd" not in rows.rows[0]
    assert captured_response(rows.rows[0])["content"] == "ok"
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


def test_config_poller_logs_generic_failures_via_failure_logger(caplog):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(500)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    poller = ConfigPoller(
        "mg_test", f"http://127.0.0.1:{server.server_port}",
        poll_seconds=60, hard_ttl_seconds=120,
    )
    with caplog.at_level(logging.WARNING, logger="metergraph"):
        ok = poller.poll_once()
    poller.stop()
    server.shutdown()

    assert ok is False
    assert any("config poll to" in r.getMessage() for r in caplog.records)


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


def test_failure_logger_logs_first_occurrence_and_suppresses_repeats(caplog):
    now = [0.0]
    logger = FailureLogger(quiet_seconds=60.0, clock=lambda: now[0])
    with caplog.at_level(logging.WARNING, logger="metergraph"):
        logger.report("transport_error", "boom 1")
        now[0] = 10.0
        logger.report("transport_error", "boom 2")
        now[0] = 20.0
        logger.report("transport_error", "boom 3")
    messages = [r.getMessage() for r in caplog.records]
    assert len(messages) == 1
    assert "boom 1" in messages[0]


def test_failure_logger_reports_suppressed_count_after_quiet_window(caplog):
    now = [0.0]
    logger = FailureLogger(quiet_seconds=60.0, clock=lambda: now[0])
    with caplog.at_level(logging.WARNING, logger="metergraph"):
        logger.report("transport_error", "boom 1")
        now[0] = 10.0
        logger.report("transport_error", "boom 2")  # suppressed
        now[0] = 70.0
        logger.report("transport_error", "boom 3")
    messages = [r.getMessage() for r in caplog.records]
    assert len(messages) == 2
    assert "1 more suppressed" in messages[1]
    assert "boom 3" in messages[1]


def test_failure_logger_tracks_kinds_independently(caplog):
    now = [0.0]
    logger = FailureLogger(quiet_seconds=60.0, clock=lambda: now[0])
    with caplog.at_level(logging.WARNING, logger="metergraph"):
        logger.report("transport_error", "t1")
        logger.report("client_error", "c1")
    assert len(caplog.records) == 2


def test_failure_logger_bounds_log_volume_under_sustained_failure(caplog):
    now = [0.0]
    logger = FailureLogger(quiet_seconds=60.0, clock=lambda: now[0])
    with caplog.at_level(logging.WARNING, logger="metergraph"):
        for _ in range(1000):
            logger.report("transport_error", "boom")
    assert len(caplog.records) == 1


def test_writer_auth_failure_is_fatal_and_logged_once(caplog):
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(401)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    writer = Writer("mg_test", f"http://127.0.0.1:{server.server_port}", flush_seconds=5)
    with caplog.at_level(logging.WARNING, logger="metergraph"):
        writer.enqueue({"payload": "one"})
        writer.flush(2)
        writer.enqueue({"payload": "two"})
        writer.flush(2)
    writer.shutdown()
    server.shutdown()

    auth_warnings = [r for r in caplog.records if "authentication failed" in r.getMessage()]
    assert len(auth_warnings) == 1
    assert writer.dropped >= 1


def test_writer_permanent_client_error_drops_batch_but_writer_stays_alive(caplog):
    attempts = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            attempts.append(1)
            self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(400)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    writer = Writer("mg_test", f"http://127.0.0.1:{server.server_port}", flush_seconds=5)
    with caplog.at_level(logging.WARNING, logger="metergraph"):
        writer.enqueue({"payload": "one"})
        writer.flush(2)
        writer.enqueue({"payload": "two"})
        writer.flush(2)
    writer.shutdown()
    server.shutdown()

    # A 400 is specific to the rejected batch, not the whole connection: the
    # writer must still attempt the second, unrelated batch.
    assert len(attempts) == 2
    assert writer._fatal is False
    assert writer.dropped >= 2
    assert any("HTTP 400" in r.getMessage() for r in caplog.records)


def test_writer_413_splits_oversized_batch_and_delivers_the_pieces():
    received_batches = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            if self.headers.get("Content-Encoding") == "gzip":
                body = gzip.decompress(body)
            rows = json.loads(body)["rows"]
            received_batches.append(len(rows))
            if len(rows) > 1:
                self.send_response(413)
            else:
                self.send_response(202)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    writer = Writer("mg_test", f"http://127.0.0.1:{server.server_port}", flush_seconds=5)
    for i in range(4):
        writer.enqueue({"index": i})
    assert writer.flush(10)
    writer.shutdown()
    server.shutdown()

    assert any(size > 1 for size in received_batches)  # a multi-row batch hit 413 at least once
    assert received_batches.count(1) == 4  # every row was eventually delivered as its own batch
    assert writer.dropped == 0
    assert writer._fatal is False


def test_writer_server_error_retries_and_is_not_fatal(caplog):
    attempts = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            attempts.append(1)
            self.send_response(500)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    writer = Writer("mg_test", f"http://127.0.0.1:{server.server_port}", flush_seconds=5)
    with caplog.at_level(logging.WARNING, logger="metergraph"):
        writer.enqueue({"payload": "one"})
        writer.flush(2)
    writer.shutdown()
    server.shutdown()

    assert len(attempts) == 1
    assert writer._fatal is False
    assert any("HTTP 500" in r.getMessage() for r in caplog.records)
