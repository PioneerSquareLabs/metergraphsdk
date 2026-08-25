"""Real-package composition: Datadog ``ddtrace`` Anthropic instrumentation and
MeterGraph over a single async, context-managed ``messages.stream`` call.

This test runs the ddtrace 4.13.1 Anthropic patch against an
``AsyncAnthropic`` 0.125.0 client, with only the HTTP/SSE transport mocked — no
credentials, no live network, no billable call.

Installation order is the customer-relevant one. Datadog auto-instruments at
startup (``ddtrace-run`` / ``patch_all()``), monkeypatching the anthropic
classes; the application then calls ``metergraph.wrap(client)`` on its own
instance. Because an instance attribute shadows the class method, MeterGraph
is always the **outer** layer and Datadog's ``TracedAsyncStream`` proxy the
**inner** one, regardless of import order.

The parity harness covers the same behavior: Datadog's ``.stream()`` proxy is a
context manager whose
``__aenter__`` returns a *distinct* entered stream (a fresh ``TracedAsyncStream``
wrapping the real ``AsyncMessageStream``). MeterGraph must delegate iteration
and ``get_final_message()`` to that entered object, not to the manager.

The reverse order (MeterGraph inner, Datadog outer) is deliberately *not* a
permanent real-package test: MeterGraph patches the bound instance method and
captures the pre-patch original, so patching the anthropic classes afterward is
simply bypassed on that instance — an unstable artifact of global class-patch
state, not a real composition. That direction is covered generically, without
ddtrace's global patch, by ``tests/contracts/stream_lifecycle`` (wrapper-inside
a distinct-entered-stream manager).
"""

from __future__ import annotations

import asyncio
import contextlib
import json

import pytest

# The normal development environment does not install ddtrace. The isolated
# CI job (`python-ddtrace-anthropic`) installs the pinned version.
ddtrace = pytest.importorskip(
    "ddtrace",
    reason="ddtrace is not a dev dependency; covered by the isolated CI job",
)

import httpx
import metergraph
from anthropic import AsyncAnthropic
from metergraph import _capture
from metergraph._capture import Options, Runtime

try:  # Anthropic <1 uses httpx; 1.0+ uses its API-compatible httpx2.
    import httpx2 as anthropic_httpx
except ImportError:
    anthropic_httpx = httpx


# A complete, valid Anthropic streaming SSE sequence: two ordered text deltas
# ("he", "llo") building the final message "hello", end_turn, usage 6->2.
STREAM_EVENTS = [
    (
        "message_start",
        {
            "type": "message_start",
            "message": {
                "id": "msg_compose",
                "type": "message",
                "role": "assistant",
                "model": "claude-haiku-4-5-20251001",
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": 6, "output_tokens": 1},
            },
        },
    ),
    (
        "content_block_start",
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "text", "text": ""},
        },
    ),
    (
        "content_block_delta",
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "text_delta", "text": "he"},
        },
    ),
    (
        "content_block_delta",
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "text_delta", "text": "llo"},
        },
    ),
    ("content_block_stop", {"type": "content_block_stop", "index": 0}),
    (
        "message_delta",
        {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn", "stop_sequence": None},
            "usage": {"output_tokens": 2},
        },
    ),
    ("message_stop", {"type": "message_stop"}),
]


def _sse(events) -> bytes:
    return "".join(
        f"event: {etype}\ndata: {json.dumps(data)}\n\n" for etype, data in events
    ).encode()


class Rows:
    def __init__(self):
        self.rows = []

    def enqueue(self, row):
        self.rows.append(row)
        return True


@pytest.fixture
def datadog_anthropic_patch():
    """Enable Datadog's real Anthropic instrumentation through the documented
    public ``ddtrace.patch(anthropic=True)`` API, with the tracer disabled so no
    span is ever flushed to a Datadog agent. The streaming proxy that this test
    exercises is installed by that patch and runs independently of whether the
    tracer emits, so composition is still driven end to end."""
    from ddtrace.trace import tracer

    previously_enabled = tracer.enabled
    tracer.enabled = False  # never contact an external Datadog agent
    ddtrace.patch(anthropic=True)  # public instrumentation entrypoint
    try:
        yield
    finally:
        # ddtrace exposes no public per-integration unpatch (see
        # ``ddtrace.__all__``: only patch / patch_all). The integration's
        # internal ``unpatch`` is used here *solely* for deterministic test
        # cleanup — it restores the monkeypatched anthropic classes so this
        # process-global patch cannot leak into other tests.
        from ddtrace.contrib.internal.anthropic.patch import unpatch

        unpatch()
        tracer.enabled = previously_enabled


def test_datadog_and_metergraph_compose_over_streamed_messages(
    datadog_anthropic_patch, tmp_path
):
    rows = Rows()
    _capture.set_runtime(Runtime(rows, Options(app_root=str(tmp_path))))

    responses: list = []

    def handler(request: anthropic_httpx.Request) -> anthropic_httpx.Response:
        response = anthropic_httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=_sse(STREAM_EVENTS),
        )
        responses.append(response)
        return response

    async def drive():
        """Own the client for the whole loop lifetime: build it, drive one
        streamed call, then close it *inside* this event loop. On the failure
        path the client is still closed but a close error never masks the
        original one; on the success path a real close error is allowed to
        surface."""
        client = AsyncAnthropic(
            api_key="test",
            http_client=anthropic_httpx.AsyncClient(
                transport=anthropic_httpx.MockTransport(handler)
            ),
        )
        # Customer order: Datadog has already patched the anthropic classes; the
        # app now wraps its own client instance, so MeterGraph is the outer layer.
        metergraph.wrap(client, provider="anthropic")

        text_chunks: list[str] = []
        try:
            async with client.messages.stream(
                model="claude-haiku-4-5-20251001",
                max_tokens=16,
                messages=[{"role": "user", "content": "hi"}],
            ) as stream:
                async for event in stream:
                    if event.type == "content_block_delta" and (
                        event.delta.type == "text_delta"
                    ):
                        text_chunks.append(event.delta.text)
                final = await stream.get_final_message()
        except BaseException:
            # Release the HTTP resources without masking the in-flight error.
            with contextlib.suppress(BaseException):
                await client.close()
            raise
        await client.close()  # success path: surface any real close error
        return final, text_chunks

    # Reset the process-global MeterGraph runtime no matter what the loop did,
    # so a failure here cannot leak instrumentation into later tests.
    try:
        final, text_chunks = asyncio.run(drive())
    finally:
        _capture.set_runtime(None)

    # --- Application-visible behavior: ordered text deltas + final message ---
    assert text_chunks == ["he", "llo"]
    assert final.content[0].text == "hello"
    assert final.stop_reason == "end_turn"
    assert final.usage.input_tokens == 6
    assert final.usage.output_tokens == 2

    # --- Provider invocation: exactly one mocked HTTP request ---
    assert len(responses) == 1

    # --- Provider context exit: the manager's __aexit__ closed the stream, so
    # the provider's own HTTP response is released after the `async with`. ---
    assert responses[0].is_closed is True

    # --- MeterGraph: exactly one row, with usage and a non-error finish ---
    assert len(rows.rows) == 1
    row = rows.rows[0]
    assert row["provider"] == "anthropic"
    assert row["endpoint"] == "messages.stream"
    assert row["stream"] is True
    assert row["input_tokens"] == 6
    assert row["output_tokens"] == 2
    assert row["status"] == "end_turn"
    assert row["finish_reason"] == "stop"
    assert row["status_code"] == "unset"
    assert row["error"] is False
