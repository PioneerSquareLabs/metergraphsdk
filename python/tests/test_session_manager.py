"""Repository-aware ingest protocol v2: app-token -> session-token exchange."""

from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from metergraph._session import SessionManager


def _serve(handler_cls):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def _expires_in(seconds: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


def test_get_token_performs_exchange_and_caches_result():
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({
                "session_token": "session-abc",
                "expires_at": _expires_in(300),
                "repository_id": "repo_123",
            }).encode())

        def log_message(self, *args):
            pass

    server = _serve(Handler)
    manager = SessionManager(
        "app-token-secret",
        f"http://127.0.0.1:{server.server_port}",
        repository="owner/repo",
        sdk_version="0.4.0",
    )

    token = manager.get_token()
    server.shutdown()

    assert token == "session-abc"


def test_get_token_reuses_cached_token_without_a_second_request():
    calls = {"count": 0}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            calls["count"] += 1
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({
                "session_token": f"session-{calls['count']}",
                "expires_at": _expires_in(300),
                "repository_id": "repo_123",
            }).encode())

        def log_message(self, *args):
            pass

    server = _serve(Handler)
    manager = SessionManager(
        "app-token-secret",
        f"http://127.0.0.1:{server.server_port}",
        repository="owner/repo",
        sdk_version="0.4.0",
    )

    first = manager.get_token()
    second = manager.get_token()
    server.shutdown()

    assert first == second == "session-1"
    assert calls["count"] == 1


def test_exchange_request_has_the_agreed_shape():
    captured = {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            captured["path"] = self.path
            captured["headers"] = dict(self.headers)
            captured["body"] = json.loads(
                self.rfile.read(int(self.headers["Content-Length"]))
            )
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({
                "session_token": "session-abc",
                "expires_at": _expires_in(300),
                "repository_id": "repo_123",
            }).encode())

        def log_message(self, *args):
            pass

    server = _serve(Handler)
    manager = SessionManager(
        "app-token-secret",
        f"http://127.0.0.1:{server.server_port}",
        repository="owner/repo",
        sdk_version="0.4.0",
    )

    manager.get_token()
    server.shutdown()

    assert captured["path"] == "/v1/ingest/sessions"
    assert captured["headers"]["Authorization"] == "Bearer app-token-secret"
    assert captured["body"] == {
        "protocol_version": 2,
        "repository": "owner/repo",
        "sdk_version": "0.4.0",
    }


def test_get_token_returns_none_when_server_unreachable():
    manager = SessionManager(
        "app-token-secret",
        "http://127.0.0.1:1",
        repository="owner/repo",
        sdk_version="0.4.0",
        timeout_seconds=1.0,
    )

    assert manager.get_token() is None


def test_get_token_returns_none_when_response_is_missing_session_token():
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"repository_id": "repo_123"}).encode())

        def log_message(self, *args):
            pass

    server = _serve(Handler)
    manager = SessionManager(
        "app-token-secret",
        f"http://127.0.0.1:{server.server_port}",
        repository="owner/repo",
        sdk_version="0.4.0",
    )

    token = manager.get_token()
    server.shutdown()

    assert token is None


def test_failed_exchange_is_backed_off_instead_of_retried_per_batch():
    calls = {"count": 0}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            calls["count"] += 1
            self.send_response(500)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = _serve(Handler)
    manager = SessionManager(
        "app-token-secret",
        f"http://127.0.0.1:{server.server_port}",
        repository="owner/repo",
        sdk_version="0.4.0",
    )

    assert manager.get_token() is None
    assert manager.get_token() is None
    server.shutdown()

    assert calls["count"] == 1


def test_invalidate_forces_a_fresh_exchange_on_next_get_token():
    calls = {"count": 0}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            calls["count"] += 1
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({
                "session_token": f"session-{calls['count']}",
                "expires_at": _expires_in(300),
                "repository_id": "repo_123",
            }).encode())

        def log_message(self, *args):
            pass

    server = _serve(Handler)
    manager = SessionManager(
        "app-token-secret",
        f"http://127.0.0.1:{server.server_port}",
        repository="owner/repo",
        sdk_version="0.4.0",
    )

    first = manager.get_token()
    manager.invalidate()
    second = manager.get_token()
    server.shutdown()

    assert first == "session-1"
    assert second == "session-2"
    assert calls["count"] == 2


def test_get_token_refreshes_once_the_cached_token_is_near_expiry():
    calls = {"count": 0}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            calls["count"] += 1
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({
                # Well inside the manager's refresh margin -- the very next
                # get_token() call must treat this as already-stale.
                "session_token": f"session-{calls['count']}",
                "expires_at": _expires_in(1),
                "repository_id": "repo_123",
            }).encode())

        def log_message(self, *args):
            pass

    server = _serve(Handler)
    manager = SessionManager(
        "app-token-secret",
        f"http://127.0.0.1:{server.server_port}",
        repository="owner/repo",
        sdk_version="0.4.0",
    )

    first = manager.get_token()
    second = manager.get_token()
    server.shutdown()

    assert first == "session-1"
    assert second == "session-2"
    assert calls["count"] == 2


def test_stop_clears_cached_token_and_short_circuits_further_exchanges():
    calls = {"count": 0}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            calls["count"] += 1
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({
                "session_token": "session-abc",
                "expires_at": _expires_in(300),
                "repository_id": "repo_123",
            }).encode())

        def log_message(self, *args):
            pass

    server = _serve(Handler)
    manager = SessionManager(
        "app-token-secret",
        f"http://127.0.0.1:{server.server_port}",
        repository="owner/repo",
        sdk_version="0.4.0",
    )

    manager.get_token()
    manager.stop()
    token_after_stop = manager.get_token()
    server.shutdown()

    assert token_after_stop is None
    assert calls["count"] == 1


def test_after_fork_replaces_a_possibly_inherited_locked_lock():
    manager = SessionManager(
        "app-token-secret",
        "http://127.0.0.1:1",
        repository="owner/repo",
        sdk_version="0.4.0",
    )
    manager._lock.acquire()

    manager._after_fork()

    assert manager._lock.acquire(blocking=False)
