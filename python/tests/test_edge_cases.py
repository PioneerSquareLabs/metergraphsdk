from __future__ import annotations

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

import metergraph
from metergraph import _capture
from metergraph import _context
from metergraph._capture import AsyncStream, Options, Runtime, SyncStream
from metergraph._context import CaptureContext


class Rows:
    def __init__(self) -> None:
        self.rows: list[dict] = []

    def enqueue(self, row: dict) -> bool:
        self.rows.append(row)
        return True


def runtime(*, capture_text: bool = True, redact=None) -> tuple[Runtime, Rows]:
    rows = Rows()
    return Runtime(
        rows,
        Options(capture_text=capture_text, app_root="", redact=redact),
    ), rows


def test_usage_normalization_accepts_provider_variants_and_rejects_bad_counts():
    normalized = _capture._usage(
        {
            "usageMetadata": {
                "promptTokenCount": "12",
                "candidatesTokenCount": 5,
                "cachedContentTokenCount": 3,
                "thoughtsTokenCount": 2,
            }
        }
    )
    assert normalized == {
        "input_tokens": 12,
        "output_tokens": 5,
        "cache_read_tokens": 3,
        "cache_write_tokens": None,
        "cache_write_5m_tokens": None,
        "cache_write_1h_tokens": None,
        "reasoning_tokens": 2,
    }
    assert _capture._int(9_007_199_254_740_993) == 9_007_199_254_740_993
    assert _capture._chunk_has_output(
        SimpleNamespace(
            type="content_block_delta",
            delta=SimpleNamespace(type="thinking_delta", thinking="reasoning"),
        )
    )
    assert _capture._chunk_has_output(
        {"type": "response.reasoning_summary_text.delta", "delta": "reasoning"}
    )

    malformed = _capture._usage(
        {
            "usage": {
                "input_tokens": True,
                "output_tokens": -1,
                "cache_read_input_tokens": float("nan"),
                "cache_creation_input_tokens": "1.5",
                "completion_tokens_details": {"reasoning_tokens": float("inf")},
            }
        }
    )
    assert all(value is None for value in malformed.values())


@pytest.mark.parametrize(
    ("base_url", "expected"),
    [
        (" HTTPS://AI-GATEWAY.VERCEL.SH/v1/ ", True),
        ("https://ai-gateway.vercel.sh.evil.example/v1", False),
        ("https://vercel.sh/ai-gateway.vercel.sh/v1", False),
        ("not a url", False),
        (None, False),
    ],
)
def test_vercel_gateway_host_detection_is_exact(base_url, expected):
    assert _capture._uses_vercel_gateway(SimpleNamespace(base_url=base_url)) is expected


def test_vercel_gateway_aliases_are_trimmed_and_unqualified_models_fall_back():
    capture_runtime, rows = runtime()
    _capture.set_runtime(capture_runtime)

    class Completions:
        def create(self, **kwargs):
            return {
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                "choices": [{"message": {"content": "ok"}}],
            }

    try:
        client = SimpleNamespace(chat=SimpleNamespace(completions=Completions()))
        _capture.wrap(client, provider=" VERCEL ")
        client.chat.completions.create(model="openai/gpt-5", messages=[])
        client.chat.completions.create(model="unqualified", messages=[])
    finally:
        _capture.set_runtime(None)

    assert [row["provider"] for row in rows.rows] == [
        "openai",
        "vercel-ai-gateway",
    ]


def test_sync_stream_close_context_exit_and_iteration_error_capture_once():
    capture_runtime, rows = runtime()

    class Closable:
        closed = False

        def __iter__(self):
            return iter(())

        def close(self):
            self.closed = True

    source = Closable()
    stream = SyncStream(
        source,
        capture_runtime.call_state("openai", "chat.completions", {"model": "m"}),
    )
    stream.close()
    stream.close()
    assert source.closed is True
    assert len(rows.rows) == 1
    assert rows.rows[0]["status"] == "abandoned"

    class Broken:
        def __iter__(self):
            yield {"choices": [{"delta": {"content": "partial"}}]}
            raise LookupError("stream broke")

    broken = SyncStream(
        Broken(),
        capture_runtime.call_state("openai", "chat.completions", {"model": "m"}),
    )
    with pytest.raises(LookupError, match="stream broke"):
        list(broken)
    assert len(rows.rows) == 2
    assert rows.rows[1]["status"] == "error"
    assert rows.rows[1]["error_type"] == "LookupError"
    assert json.loads(rows.rows[1]["response_text"])["content"] == "partial"

    with SyncStream(
        iter(()),
        capture_runtime.call_state("openai", "chat.completions", {"model": "m"}),
    ):
        pass
    assert len(rows.rows) == 3
    assert rows.rows[2]["status"] == "abandoned"


def test_async_stream_aclose_context_exit_and_iteration_error_capture_once():
    capture_runtime, rows = runtime()

    class Source:
        closed = False

        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

        async def aclose(self):
            self.closed = True

    async def exercise():
        source = Source()
        stream = AsyncStream(
            source,
            capture_runtime.call_state("anthropic", "messages.stream", {"model": "m"}),
        )
        await stream.aclose()
        await stream.aclose()
        assert source.closed is True

        class Broken:
            def __init__(self):
                self.count = 0

            def __aiter__(self):
                return self

            async def __anext__(self):
                self.count += 1
                if self.count == 1:
                    return SimpleNamespace(
                        type="content_block_delta",
                        delta=SimpleNamespace(text="partial"),
                    )
                raise ArithmeticError("async stream broke")

        broken = AsyncStream(
            Broken(),
            capture_runtime.call_state("anthropic", "messages.stream", {"model": "m"}),
        )
        with pytest.raises(ArithmeticError, match="async stream broke"):
            async for _ in broken:
                pass

        async with AsyncStream(
            Source(),
            capture_runtime.call_state("anthropic", "messages.stream", {"model": "m"}),
        ):
            pass

    asyncio.run(exercise())
    assert [row["status"] for row in rows.rows] == ["abandoned", "error", "abandoned"]
    assert rows.rows[1]["error_type"] == "ArithmeticError"


@pytest.mark.parametrize("proxied", [False, True])
def test_anthropic_stream_manager_uses_the_entered_stream(proxied):
    capture_runtime, rows = runtime()
    _capture.set_runtime(capture_runtime)

    final_message = SimpleNamespace(
        usage=SimpleNamespace(input_tokens=6, output_tokens=2),
        content=[SimpleNamespace(text="complete")],
        stop_reason="end_turn",
    )

    class EnteredStream:
        def __aiter__(self):
            async def chunks():
                yield SimpleNamespace(
                    type="content_block_delta",
                    delta=SimpleNamespace(text="complete"),
                )

            return chunks()

        async def get_final_message(self):
            return final_message

    class StreamManager:
        def __init__(self):
            self.entered = EnteredStream()
            self.exited = False

        async def __aenter__(self):
            return self.entered

        async def __aexit__(self, exc_type, exc, tb):
            self.exited = True

    class IterableProxy:
        """Models the iterable proxy added by Datadog's Anthropic integration."""

        def __init__(self, manager):
            self.manager = manager

        def __aiter__(self):
            return self.manager.entered.__aiter__()

        async def __aenter__(self):
            return await self.manager.__aenter__()

        async def __aexit__(self, exc_type, exc, tb):
            return await self.manager.__aexit__(exc_type, exc, tb)

    manager = StreamManager()

    class Messages:
        def stream(self, **kwargs):
            return IterableProxy(manager) if proxied else manager

    client = SimpleNamespace(messages=Messages())
    metergraph.wrap(client, provider="anthropic")

    async def exercise():
        async with client.messages.stream(model="claude", messages=[]) as stream:
            assert await stream.get_final_message() is final_message

    try:
        asyncio.run(exercise())
    finally:
        _capture.set_runtime(None)

    assert manager.exited is True
    assert len(rows.rows) == 1
    assert rows.rows[0]["finish_reason"] == "stop"
    assert rows.rows[0]["input_tokens"] == 6
    assert rows.rows[0]["output_tokens"] == 2


def test_anthropic_stream_manager_records_context_entry_failure():
    capture_runtime, rows = runtime()
    _capture.set_runtime(capture_runtime)

    class FailingManager:
        async def __aenter__(self):
            raise ConnectionError("provider unavailable")

        async def __aexit__(self, exc_type, exc, tb):
            return None

    class Messages:
        def stream(self, **kwargs):
            return FailingManager()

    client = SimpleNamespace(messages=Messages())
    metergraph.wrap(client, provider="anthropic")

    async def exercise():
        async with client.messages.stream(model="claude", messages=[]):
            pass

    try:
        with pytest.raises(ConnectionError, match="provider unavailable"):
            asyncio.run(exercise())
    finally:
        _capture.set_runtime(None)

    assert len(rows.rows) == 1
    assert rows.rows[0]["status"] == "error"
    assert rows.rows[0]["error_type"] == "ConnectionError"


def test_final_message_failures_and_awaitable_mismatch_fall_back_to_last_chunk():
    capture_runtime, rows = runtime()

    class SyncFinalFailure:
        def __iter__(self):
            yield {"choices": [{"delta": {"content": "sync"}}]}

        def get_final_message(self):
            raise RuntimeError("final unavailable")

    assert len(list(SyncStream(
        SyncFinalFailure(),
        capture_runtime.call_state("openai", "chat.completions", {"model": "m"}),
    ))) == 1

    class WrongSyncFinal:
        def __iter__(self):
            yield {"choices": [{"delta": {"content": "awaitable"}}]}

        async def get_final_message(self):
            return {"usage": {"input_tokens": 999}}

    assert len(list(SyncStream(
        WrongSyncFinal(),
        capture_runtime.call_state("openai", "chat.completions", {"model": "m"}),
    ))) == 1

    class AsyncFinalFailure:
        def __aiter__(self):
            return self

        def __init__(self):
            self.done = False

        async def __anext__(self):
            if self.done:
                raise StopAsyncIteration
            self.done = True
            return SimpleNamespace(
                type="content_block_delta", delta=SimpleNamespace(text="async")
            )

        async def get_final_message(self):
            raise RuntimeError("async final unavailable")

    async def consume():
        return [part async for part in AsyncStream(
            AsyncFinalFailure(),
            capture_runtime.call_state("anthropic", "messages.stream", {"model": "m"}),
        )]

    assert len(asyncio.run(consume())) == 1
    assert [json.loads(row["response_text"])["content"] for row in rows.rows] == [
        "sync",
        "awaitable",
        "async",
    ]
    assert all(row["status"] == "success" for row in rows.rows)


def test_stream_usage_injection_respects_positional_inputs_overrides_and_opt_out(monkeypatch):
    capture_runtime, rows = runtime()
    _capture.set_runtime(capture_runtime)
    seen = []

    class Completions:
        def create(self, request):
            seen.append(request)
            return iter(())

    client = SimpleNamespace(chat=SimpleNamespace(completions=Completions()))
    _capture.wrap(client, provider="openai")
    original = {"model": "m", "messages": [], "stream": True}
    try:
        list(client.chat.completions.create(original))
        list(client.chat.completions.create({
            **original,
            "stream_options": {"include_usage": False, "custom": True},
        }))
        monkeypatch.setenv("METERGRAPH_PATCH_STREAM_USAGE", "0")
        list(client.chat.completions.create(original))
    finally:
        _capture.set_runtime(None)

    assert "stream_options" not in original
    assert seen[0]["stream_options"] == {"include_usage": True}
    assert seen[1]["stream_options"] == {"include_usage": False, "custom": True}
    assert "stream_options" not in seen[2]
    assert len(rows.rows) == 3


def test_tool_only_stream_sets_ttft_hides_usage_chunk_and_joins_fragments():
    capture_runtime, rows = runtime()
    chunks = [
        SimpleNamespace(
            choices=[
                SimpleNamespace(
                    delta=SimpleNamespace(
                        content=None,
                        tool_calls=[
                            SimpleNamespace(
                                index=0,
                                id="call-1",
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
                                function=SimpleNamespace(name=None, arguments='"1"}'),
                            )
                        ],
                    )
                )
            ],
            usage=None,
        ),
        SimpleNamespace(
            choices=[],
            usage=SimpleNamespace(prompt_tokens=4, completion_tokens=2),
        ),
    ]
    stream = SyncStream(
        iter(chunks),
        capture_runtime.call_state("openai", "chat.completions", {"model": "m"}),
    )

    assert list(stream) == chunks[:2]
    row = rows.rows[0]
    assert row["ttft_ms"] is not None
    assert row["input_tokens"] == 4
    assert row["output_tokens"] == 2
    assert row["tool_names"] == ["lookup"]
    assert row["tool_calls"][0]["arguments"] == {"id": "1"}


def test_tool_results_can_precede_calls_and_private_capture_keeps_only_metadata():
    capture_runtime, rows = runtime(capture_text=False)
    call = capture_runtime.call_state(
        "openai",
        "responses",
        {
            "model": "m",
            "input": [
                {"type": "function_call_output", "call_id": "c1", "output": "done"},
                {
                    "type": "function_call",
                    "call_id": "c1",
                    "name": "lookup",
                    "arguments": "{not-json",
                },
            ],
        },
    )
    call.finish({})
    row = rows.rows[0]
    assert row["request_json"] is None
    assert row["response_text"] is None
    assert row["tool_names"] == ["lookup"]
    assert row["tool_calls"] == [
        {
            "call_id": "c1",
            "name": "lookup",
            "status": "completed",
            "idempotency": "non_idempotent",
        }
    ]


def test_capture_is_idempotent_and_fail_open_when_redaction_or_enqueue_fails():
    def broken_redactor(_value, _kind):
        raise RuntimeError("redaction failed")

    capture_runtime, rows = runtime(redact=broken_redactor)
    call = capture_runtime.call_state("openai", "responses", {"model": "m"})
    call.finish({"output_text": "secret"})
    call.finish({"output_text": "duplicate"})
    assert len(rows.rows) == 1
    assert rows.rows[0]["request_json"] == "<redaction-failed>"
    assert rows.rows[0]["response_text"] == "<redaction-failed>"

    class BrokenWriter:
        def enqueue(self, _row):
            raise OSError("queue unavailable")

    failed = Runtime(BrokenWriter(), Options(app_root="")).call_state(
        "openai", "responses", {"model": "m"}
    )
    failed.finish({"output_text": "still fail-open"})
    assert failed.done is True


def test_route_and_trace_decorators_restore_context_across_sync_async_and_errors():
    token = _context._current.set(CaptureContext(tags={"outer": "yes"}))
    try:
        @metergraph.route(
            "sync-route",
            unit="item",
            unit_count=2,
            tags={"inner": 3},
            capture_text=False,
        )
        def sync_call():
            current = _context.snapshot()
            assert current.route == "sync-route"
            assert current.unit_name == "item"
            assert current.unit_count == 2
            assert current.tags == {"outer": "yes", "inner": "3"}
            assert current.capture_text is False
            raise ValueError("application error")

        with pytest.raises(ValueError, match="application error"):
            sync_call()
        assert _context.snapshot() == CaptureContext(tags={"outer": "yes"})

        @metergraph.route("async-route", tags={"async": True})
        async def async_call():
            return _context.snapshot()

        current = asyncio.run(async_call())
        assert current.route == "async-route"
        assert current.tags == {"outer": "yes", "async": "True"}
        assert _context.snapshot() == CaptureContext(tags={"outer": "yes"})

        trace_id = "a" * 32
        with metergraph.trace("outer-trace", trace_id=trace_id, parent_span_id="root"):
            outer = _context.snapshot()
            with metergraph.trace("reused"):
                reused = _context.snapshot()
            with metergraph.trace("new", trace_id="b" * 32, capture_text=True):
                new = _context.snapshot()
            assert reused.trace_id == trace_id
            assert reused.trace_name == "outer-trace"
            assert reused.parent_span_id == "root"
            assert new.trace_id == "b" * 32
            assert new.trace_name == "new"
            assert new.parent_span_id is None
            assert new.capture_text is True
            assert _context.snapshot() == outer
        assert _context.snapshot() == CaptureContext(tags={"outer": "yes"})

        @metergraph.trace("async-trace", trace_id="c" * 32)
        async def traced_async():
            return _context.snapshot().trace_id

        @metergraph.trace("sync-trace", trace_id="d" * 32)
        def traced_sync():
            return _context.snapshot().trace_id

        assert asyncio.run(traced_async()) == "c" * 32
        assert traced_sync() == "d" * 32
    finally:
        _context._current.reset(token)


def test_wrapped_executor_propagates_submission_context_and_is_idempotent():
    token = _context._current.set(CaptureContext())
    executor = ThreadPoolExecutor(max_workers=1)
    try:
        wrapped = metergraph.wrap_executor(executor)
        assert metergraph.wrap_executor(wrapped) is wrapped
        with metergraph.route("background", tags={"source": "request"}):
            future = wrapped.submit(_context.snapshot)
        observed = future.result(timeout=2)
        assert observed.route == "background"
        assert observed.tags == {"source": "request"}
        assert _context.snapshot().route is None
    finally:
        executor.shutdown()
        _context._current.reset(token)


@pytest.mark.parametrize(
    "field,value",
    [
        ("feedback_score", float("nan")),
        ("feedback_score", float("inf")),
        ("turns_to_resolution", 1.5),
        ("turns_to_resolution", True),
        ("turns_to_resolution", "2"),
        ("edit_distance_ratio", float("nan")),
        ("regeneration_count", 1.5),
        ("regeneration_count", True),
        ("regeneration_count", "2"),
    ],
)
def test_record_outcome_rejects_nonfinite_and_noninteger_values(monkeypatch, field, value):
    rows = Rows()
    monkeypatch.setattr(metergraph, "_writer", rows)
    with metergraph.session("edge-session"):
        assert metergraph.record_outcome(
            "route",
            model="model",
            task_completed=True,
            **{field: value},
        ) is False
        assert rows.rows == []


def test_record_outcome_accepts_all_documented_boundaries(monkeypatch):
    rows = Rows()
    monkeypatch.setattr(metergraph, "_writer", rows)
    assert metergraph.record_outcome(
        "route",
        model="model",
        session_key="session",
        task_completed=False,
        feedback_score=-1,
        turns_to_resolution=1_000_000,
        edit_distance_ratio=1,
        regeneration_count=0,
        escalated=False,
        abandoned=True,
    ) is True
    assert len(rows.rows) == 1
