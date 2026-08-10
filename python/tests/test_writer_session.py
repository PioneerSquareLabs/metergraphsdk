"""Repository-aware ingest protocol v2: Writer sends session token only."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from metergraph._transport import Writer


def _serve(handler_cls):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


class FakeSession:
    def __init__(self, token):
        self.token = token
        self.invalidated = 0

    def get_token(self):
        return self.token

    def invalidate(self):
        self.invalidated += 1
        self.token = None


def test_writer_sends_session_token_and_never_the_app_token():
    received = {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            received["auth"] = self.headers.get("Authorization")
            self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(202)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = _serve(Handler)
    session = FakeSession("session-abc")
    writer = Writer(
        "app-token-secret",
        f"http://127.0.0.1:{server.server_port}",
        session=session,
        flush_seconds=5,
    )
    writer.enqueue({"payload": "x"})
    assert writer.flush(2)
    writer.shutdown()
    server.shutdown()

    assert received["auth"] == "Bearer session-abc"


def test_writer_drops_batch_without_a_request_when_session_has_no_token_yet():
    called = {"count": 0}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            called["count"] += 1
            self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(202)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = _serve(Handler)
    session = FakeSession(None)
    writer = Writer(
        "app-token-secret",
        f"http://127.0.0.1:{server.server_port}",
        session=session,
        flush_seconds=5,
    )
    writer.enqueue({"payload": "x"})
    assert writer.flush(2)
    writer.shutdown()
    server.shutdown()

    assert called["count"] == 0
    assert writer.dropped == 1


def test_writer_invalidates_session_on_401_instead_of_going_fatal():
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(401)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = _serve(Handler)
    session = FakeSession("session-abc")
    writer = Writer(
        "app-token-secret",
        f"http://127.0.0.1:{server.server_port}",
        session=session,
        flush_seconds=5,
    )
    writer.enqueue({"payload": "x"})
    assert writer.flush(2)
    writer.shutdown()
    server.shutdown()

    assert session.invalidated == 1
    assert writer._fatal is False


def test_writer_invalidates_session_on_403_instead_of_going_fatal():
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(403)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = _serve(Handler)
    session = FakeSession("session-abc")
    writer = Writer(
        "app-token-secret",
        f"http://127.0.0.1:{server.server_port}",
        session=session,
        flush_seconds=5,
    )
    writer.enqueue({"payload": "x"})
    assert writer.flush(2)
    writer.shutdown()
    server.shutdown()

    assert session.invalidated == 1
    assert writer._fatal is False
