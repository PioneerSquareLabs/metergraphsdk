"""Offline contract test for the runnable examples/python-openrouter example.

A local HTTP server plays two roles: an OpenRouter-compatible Chat Completions
endpoint that a real ``openai`` client talks to, and the MeterGraph ingest that
receives the captured wire rows. No network or billable calls are made.
"""

from __future__ import annotations

import importlib.util
import json
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

import metergraph
from metergraph import _capture

SERVED_MODEL = "anthropic/claude-sonnet-4.6"
REPORTED_COST = 0.00482
UPSTREAM_COST = 0.00131
EXAMPLE_MAIN = (
    Path(__file__).resolve().parents[2] / "examples" / "python-openrouter" / "main.py"
)


def _completion(model):
    return {
        "id": "gen-nonstream",
        "object": "chat.completion",
        "created": 0,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "Cache-aware pricing keeps repeated context cheap."},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 920,
            "completion_tokens": 110,
            "total_tokens": 1030,
            "cost": REPORTED_COST,
            "cost_details": {"upstream_inference_cost": UPSTREAM_COST},
        },
    }


def _stream_body(model):
    def chunk(delta, finish=None, usage=None):
        payload = {
            "id": "gen-stream",
            "object": "chat.completion.chunk",
            "created": 0,
            "model": model,
            "choices": [] if usage else [{"index": 0, "delta": delta, "finish_reason": finish}],
        }
        if usage is not None:
            payload["usage"] = usage
        return f"data: {json.dumps(payload)}\n\n"

    events = [
        chunk({"role": "assistant", "content": "Streaming "}),
        chunk({"content": "keeps "}),
        chunk({"content": "usage final."}, finish="stop"),
        # OpenRouter supplies the final usage event automatically.
        chunk(
            {},
            usage={
                "prompt_tokens": 920,
                "completion_tokens": 110,
                "total_tokens": 1030,
                "cost": REPORTED_COST,
                "cost_details": {"upstream_inference_cost": UPSTREAM_COST},
            },
        ),
        "data: [DONE]\n\n",
    ]
    return "".join(events).encode()


def _make_handler(batches):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            if self.path.startswith("/v1/config"):
                self._json(200, {"routes": {}}, extra={"ETag": '"v1"'})
            else:
                self._json(404, {})

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            if self.path == "/api/v1/chat/completions":
                request = json.loads(raw or b"{}")
                if request.get("stream"):
                    self._raw(200, _stream_body(SERVED_MODEL), "text/event-stream")
                else:
                    self._json(200, _completion(SERVED_MODEL))
                return
            if self.path == "/v1/ingest/sessions":
                self._json(
                    200,
                    {
                        "session_token": "session-fixture",
                        "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
                        "repository_id": "repo_fixture",
                    },
                )
                return
            # Any other POST is a MeterGraph ingest batch.
            body = raw
            if self.headers.get("Content-Encoding") == "gzip":
                import gzip

                body = gzip.decompress(body)
            try:
                batches.append(json.loads(body))
            except json.JSONDecodeError:
                pass
            self._json(202, {})

        def _json(self, status, payload, extra=None):
            self._raw(status, json.dumps(payload).encode(), "application/json", extra)

        def _raw(self, status, body, content_type, extra=None):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            for key, value in (extra or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)

    return Handler


def _reset_metergraph():
    metergraph.shutdown()
    metergraph._initialized = False
    metergraph._warned_no_token = False
    metergraph._warned_no_repository = False
    metergraph._warned_repeated_init = False
    _capture.set_runtime(None)


def _load_example():
    spec = importlib.util.spec_from_file_location("openrouter_example_main", EXAMPLE_MAIN)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_python_openrouter_example_offline_contract(monkeypatch):
    pytest.importorskip("openai")
    batches: list[dict] = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(batches))
    import threading

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_port
    try:
        monkeypatch.setenv("OPENROUTER_BASE_URL", f"http://127.0.0.1:{port}/api/v1")
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-PLACEHOLDER")
        monkeypatch.setenv("METERGRAPH_APP_TOKEN", "mg_test")
        monkeypatch.setenv("METERGRAPH_INGEST_URL", f"http://127.0.0.1:{port}")
        monkeypatch.setenv("METERGRAPH_FLUSH_SECONDS", "1")
        monkeypatch.setenv("METERGRAPH_CONFIG_POLL_SECONDS", "60")
        _reset_metergraph()
        example = _load_example()
        summary = example.main()

        # Native OpenAI results survive the wrapper unchanged.
        assert summary["nonstream"]["served_model"] == SERVED_MODEL
        assert "Cache-aware" in summary["nonstream"]["content"]
        assert summary["nonstream"]["reported_cost_usd"] == REPORTED_COST
        assert summary["stream"]["served_model"] == SERVED_MODEL
        assert "Streaming keeps usage final." == summary["stream"]["content"]
        assert summary["stream"]["chunk_count"] >= 3
        assert summary["stream"]["reported_cost_usd"] == REPORTED_COST

        # Captured wire rows carry the gateway evidence.
        rows = [row for batch in batches for row in batch.get("rows", [])]
        gateway_rows = [r for r in rows if r.get("gateway") == "openrouter"]
        # Exactly two: one non-stream, one stream. No duplicate capture.
        assert len(gateway_rows) == 2
        assert {bool(r["stream"]) for r in gateway_rows} == {False, True}
        for row in gateway_rows:
            assert row["provider"] == "openai"
            assert row["model"] == SERVED_MODEL
            assert row["served_model"] == SERVED_MODEL
            assert row["reported_cost_usd"] == REPORTED_COST
            assert row["reported_cost_source"] == "openrouter.usage.cost"
            assert row["reported_upstream_cost_usd"] == UPSTREAM_COST
    finally:
        _reset_metergraph()
        server.shutdown()
        server.server_close()
