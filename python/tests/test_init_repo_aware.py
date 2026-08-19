"""Repository-aware ingest protocol v2: init()/shutdown() wiring."""

from __future__ import annotations

import json
import subprocess

import metergraph
from metergraph import _capture


def _git(*args: str, cwd) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _init_repo_with_origin(root, origin: str) -> None:
    _git("init", cwd=root)
    _git("config", "user.email", "test@example.com", cwd=root)
    _git("config", "user.name", "Test", cwd=root)
    _git("remote", "add", "origin", origin, cwd=root)


def _reset_metergraph_state():
    metergraph.shutdown()
    metergraph._initialized = False
    metergraph._warned_no_token = False
    metergraph._warned_no_repository = False


def test_init_uses_explicit_repository_without_writing_config(tmp_path):
    _reset_metergraph_state()
    _init_repo_with_origin(tmp_path, "https://github.com/acme/widgets.git")

    metergraph.init(
        token="mg_test",
        ingest_url="http://127.0.0.1:9",
        app_root=str(tmp_path),
        repository="acme/widgets",
    )

    assert metergraph._session_manager is not None
    assert _capture._runtime.options.repo_root == str(tmp_path.resolve())
    assert not (tmp_path / ".metergraph").exists()

    _reset_metergraph_state()


def test_init_falls_back_to_v1_when_no_repo_config_or_git(tmp_path):
    _reset_metergraph_state()

    metergraph.init(
        token="mg_test", ingest_url="http://127.0.0.1:9", app_root=str(tmp_path)
    )

    assert metergraph._session_manager is None
    assert _capture._runtime.options.repo_root is None
    assert not (tmp_path / ".metergraph").exists()

    _reset_metergraph_state()


def test_repository_env_precedes_existing_file(monkeypatch, tmp_path):
    _reset_metergraph_state()
    (tmp_path / ".metergraph").mkdir()
    (tmp_path / ".metergraph" / "config.json").write_text(
        json.dumps({"version": 2, "repository": "acme/from-file"})
    )
    monkeypatch.setenv("METERGRAPH_REPOSITORY", "acme/from-env")

    metergraph.init(
        token="mg_test", ingest_url="http://127.0.0.1:9", app_root=str(tmp_path)
    )

    assert metergraph._session_manager._repository == "acme/from-env"
    _reset_metergraph_state()


def test_explicit_repository_precedes_environment(monkeypatch, tmp_path):
    _reset_metergraph_state()
    monkeypatch.setenv("METERGRAPH_REPOSITORY", "acme/from-env")

    metergraph.init(
        token="mg_test",
        ingest_url="http://127.0.0.1:9",
        app_root=str(tmp_path),
        repository="acme/explicit",
    )

    assert metergraph._session_manager._repository == "acme/explicit"
    _reset_metergraph_state()


def test_missing_repository_warns_once_and_never_writes(caplog, tmp_path):
    _reset_metergraph_state()
    _init_repo_with_origin(tmp_path, "https://github.com/acme/widgets.git")

    metergraph.init(
        token="mg_test", ingest_url="http://127.0.0.1:9", app_root=str(tmp_path)
    )

    assert metergraph._session_manager is None
    assert not (tmp_path / ".metergraph").exists()
    assert sum("repository identity is not configured" in r.message for r in caplog.records) == 1
    _reset_metergraph_state()


def test_init_uses_existing_committed_config_without_needing_git(tmp_path):
    _reset_metergraph_state()
    (tmp_path / ".metergraph").mkdir()
    (tmp_path / ".metergraph" / "config.json").write_text(
        json.dumps({"version": 2, "repository": "acme/widgets"})
    )

    metergraph.init(
        token="mg_test", ingest_url="http://127.0.0.1:9", app_root=str(tmp_path)
    )

    assert metergraph._session_manager is not None
    assert _capture._runtime.options.repo_root == str(tmp_path.resolve())

    _reset_metergraph_state()


def test_shutdown_stops_the_session_manager(tmp_path):
    _reset_metergraph_state()
    _init_repo_with_origin(tmp_path, "https://github.com/acme/widgets.git")

    metergraph.init(
        token="mg_test",
        ingest_url="http://127.0.0.1:9",
        app_root=str(tmp_path),
        repository="acme/widgets",
    )
    session_manager = metergraph._session_manager
    assert session_manager is not None

    metergraph.shutdown()

    assert metergraph._session_manager is None
    assert session_manager.get_token() is None  # stopped: short-circuits

    _reset_metergraph_state()


def test_shutdown_flushes_writer_before_stopping_session_manager(monkeypatch):
    events = []

    class FakeWriter:
        def shutdown(self):
            events.append("writer")

    class FakeSession:
        def stop(self):
            events.append("session")

    monkeypatch.setattr(metergraph, "_writer", FakeWriter())
    monkeypatch.setattr(metergraph, "_session_manager", FakeSession())

    metergraph.shutdown()

    assert events == ["writer", "session"]
