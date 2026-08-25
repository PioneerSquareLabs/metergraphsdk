"""Wrap the real, unmodified provider SDK clients and drive a call through
their actual request-building/response-parsing code — with the network
replaced by a mocked transport, not the SDK itself.

test_seam_reality.py proves a seam *exists* on the real client. The
behavioral tests in test_sdk.py prove wrap() *works*, but only against a
hand-built fake that mimics the real client's shape. Neither proves that
wrapping the real client and calling a real method actually produces a
captured row — which is exactly the gap that let the original
chat.completions.parse capture bug ship unnoticed. These tests close it,
without needing live API keys or network access.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import metergraph
from anthropic import AsyncAnthropic
from google import genai
from metergraph import _capture
from metergraph._capture import Options, Runtime
from openai import AsyncOpenAI
from pydantic import BaseModel

try:
    import httpx2 as anthropic_httpx
except ImportError:
    # Anthropic <1 uses httpx; Anthropic 1.0+ uses its API-compatible httpx2.
    anthropic_httpx = httpx


class Rows:
    def __init__(self):
        self.rows = []

    def enqueue(self, row):
        self.rows.append(row)
        return True


class Answer(BaseModel):
    text: str


def test_wrap_captures_openai_parse_through_a_real_client(tmp_path):
    """.parse() is the exact seam the original production bug missed."""
    rows = Rows()
    _capture.set_runtime(Runtime(rows, Options(app_root=str(tmp_path))))

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-test",
                "object": "chat.completion",
                "created": 0,
                "model": "gpt-4o-mini",
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": json.dumps({"text": "hi"}),
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
            },
        )

    client = AsyncOpenAI(
        api_key="test",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    metergraph.wrap(client, provider="openai")

    async def run():
        with metergraph.route("real-client-test"):
            return await client.chat.completions.parse(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": "hi"}],
                response_format=Answer,
            )

    response = asyncio.run(run())

    assert response.choices[0].message.parsed == Answer(text="hi")
    assert len(rows.rows) == 1
    row = rows.rows[0]
    assert row["endpoint"] == "chat.completions.parse"
    assert row["provider"] == "openai"
    assert row["model"] == "gpt-4o-mini"
    assert row["input_tokens"] == 5
    assert row["output_tokens"] == 2
    _capture.set_runtime(None)


def test_wrap_captures_openai_create_through_a_real_client(tmp_path):
    rows = Rows()
    _capture.set_runtime(Runtime(rows, Options(app_root=str(tmp_path))))

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-test2",
                "object": "chat.completion",
                "created": 0,
                "model": "gpt-4o-mini",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "hi"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 4, "completion_tokens": 1, "total_tokens": 5},
            },
        )

    client = AsyncOpenAI(
        api_key="test",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    metergraph.wrap(client, provider="openai")

    async def run():
        return await client.chat.completions.create(
            model="gpt-4o-mini", messages=[{"role": "user", "content": "hi"}]
        )

    response = asyncio.run(run())

    assert response.choices[0].message.content == "hi"
    assert len(rows.rows) == 1
    assert rows.rows[0]["endpoint"] == "chat.completions"
    _capture.set_runtime(None)


def test_wrap_captures_vercel_gateway_through_real_async_openai_client(tmp_path):
    rows = Rows()
    _capture.set_runtime(Runtime(rows, Options(app_root=str(tmp_path))))

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "ai-gateway.vercel.sh"
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-gateway",
                "object": "chat.completion",
                "created": 0,
                "model": "anthropic/claude-sonnet-4.6",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "hi"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 11,
                    "completion_tokens": 3,
                    "total_tokens": 14,
                    "prompt_tokens_details": {"cached_tokens": 5},
                },
            },
        )

    client = AsyncOpenAI(
        api_key="test",
        base_url="https://ai-gateway.vercel.sh/v1",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    metergraph.wrap(client)

    async def run():
        return await client.chat.completions.create(
            model="anthropic/claude-sonnet-4.6",
            messages=[{"role": "user", "content": "hi"}],
        )

    response = asyncio.run(run())

    assert response.choices[0].message.content == "hi"
    assert len(rows.rows) == 1
    row = rows.rows[0]
    assert row["provider"] == "anthropic"
    assert row["model"] == "anthropic/claude-sonnet-4.6"
    assert row["input_tokens"] == 11
    assert row["output_tokens"] == 3
    assert row["cache_read_tokens"] == 5
    _capture.set_runtime(None)


def test_wrap_captures_anthropic_messages_create_through_a_real_client(tmp_path):
    rows = Rows()
    _capture.set_runtime(Runtime(rows, Options(app_root=str(tmp_path))))

    def handler(request: anthropic_httpx.Request) -> anthropic_httpx.Response:
        return anthropic_httpx.Response(
            200,
            json={
                "id": "msg_test",
                "type": "message",
                "role": "assistant",
                "model": "claude-haiku-4-5-20251001",
                "content": [{"type": "text", "text": "hi"}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 5, "output_tokens": 2},
            },
        )

    client = AsyncAnthropic(
        api_key="test",
        http_client=anthropic_httpx.AsyncClient(
            transport=anthropic_httpx.MockTransport(handler)
        ),
    )
    metergraph.wrap(client, provider="anthropic")

    async def run():
        return await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=10,
            messages=[{"role": "user", "content": "hi"}],
        )

    response = asyncio.run(run())

    assert response.content[0].text == "hi"
    assert len(rows.rows) == 1
    row = rows.rows[0]
    assert row["endpoint"] == "messages"
    assert row["provider"] == "anthropic"
    assert row["input_tokens"] == 5
    assert row["output_tokens"] == 2
    _capture.set_runtime(None)


def test_wrap_detects_vercel_gateway_through_real_async_anthropic_client(tmp_path):
    rows = Rows()
    _capture.set_runtime(Runtime(rows, Options(app_root=str(tmp_path))))

    def handler(request: anthropic_httpx.Request) -> anthropic_httpx.Response:
        assert request.url.host == "ai-gateway.vercel.sh"
        return anthropic_httpx.Response(
            200,
            json={
                "id": "msg_gateway",
                "type": "message",
                "role": "assistant",
                "model": "anthropic/claude-sonnet-4.6",
                "content": [{"type": "text", "text": "hi"}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 7, "output_tokens": 2},
            },
        )

    client = AsyncAnthropic(
        api_key="test",
        base_url="https://ai-gateway.vercel.sh",
        http_client=anthropic_httpx.AsyncClient(
            transport=anthropic_httpx.MockTransport(handler)
        ),
    )
    metergraph.wrap(client)

    async def run():
        return await client.messages.create(
            model="anthropic/claude-sonnet-4.6",
            max_tokens=10,
            messages=[{"role": "user", "content": "hi"}],
        )

    response = asyncio.run(run())

    assert response.content[0].text == "hi"
    assert len(rows.rows) == 1
    row = rows.rows[0]
    assert row["provider"] == "anthropic"
    assert row["model"] == "anthropic/claude-sonnet-4.6"
    assert row["endpoint"] == "messages"
    assert row["input_tokens"] == 7
    assert row["output_tokens"] == 2
    _capture.set_runtime(None)


def test_wrap_captures_google_generate_content_through_a_real_client(tmp_path):
    """genai.Client has no constructor hook for a custom transport, so the
    mocked httpx client is swapped in on the internal _api_client after
    construction — everything from there on (request building, response
    parsing) is the real SDK's own code."""
    rows = Rows()
    _capture.set_runtime(Runtime(rows, Options(app_root=str(tmp_path))))

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {
                        "content": {"parts": [{"text": "hi"}], "role": "model"},
                        "finishReason": "STOP",
                    }
                ],
                "usageMetadata": {
                    "promptTokenCount": 5,
                    "candidatesTokenCount": 2,
                    "totalTokenCount": 7,
                },
                "modelVersion": "gemini-2.5-flash",
            },
        )

    client = genai.Client(api_key="test")
    client._api_client._async_httpx_client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    )
    metergraph.wrap(client, provider="google")

    async def run():
        return await client.aio.models.generate_content(
            model="gemini-2.5-flash", contents="hi"
        )

    response = asyncio.run(run())

    assert response.text == "hi"
    assert len(rows.rows) == 1
    row = rows.rows[0]
    assert row["endpoint"] == "models.generate_content"
    assert row["provider"] == "google"
    _capture.set_runtime(None)
