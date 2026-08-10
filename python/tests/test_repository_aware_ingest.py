"""Repository-aware ingest protocol v2: repo config discovery/detection/write."""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path

from metergraph._repo_config import (
    discover_repo_config,
    ensure_repo_config,
    normalize_github_remote,
)


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _init_repo_with_origin(root: Path, origin: str) -> None:
    _git("init", cwd=root)
    _git("config", "user.email", "test@example.com", cwd=root)
    _git("config", "user.name", "Test", cwd=root)
    _git("remote", "add", "origin", origin, cwd=root)


# --- normalize_github_remote -------------------------------------------------


def test_normalize_github_remote_handles_ssh_and_https_forms():
    cases = {
        "git@github.com:owner/repo.git": "owner/repo",
        "git@github.com:owner/repo": "owner/repo",
        "https://github.com/owner/repo.git": "owner/repo",
        "https://github.com/owner/repo": "owner/repo",
        "https://github.com/owner/repo/": "owner/repo",
        "ssh://git@github.com/owner/repo.git": "owner/repo",
    }
    for url, expected in cases.items():
        assert normalize_github_remote(url) == expected


def test_normalize_github_remote_returns_none_for_non_github_hosts():
    assert normalize_github_remote("git@gitlab.com:owner/repo.git") is None
    assert normalize_github_remote("not a url") is None


# --- discover_repo_config (pure, read-only) ----------------------------------


def test_discover_repo_config_finds_file_at_app_root(tmp_path):
    (tmp_path / ".metergraph").mkdir()
    (tmp_path / ".metergraph" / "config.json").write_text(
        json.dumps({"version": 2, "repository": "acme/widgets"})
    )

    config = discover_repo_config(str(tmp_path))

    assert config is not None
    assert config.repository == "acme/widgets"
    assert config.repo_root == str(tmp_path.resolve())


def test_discover_repo_config_walks_up_from_a_nested_app_root(tmp_path):
    (tmp_path / ".metergraph").mkdir()
    (tmp_path / ".metergraph" / "config.json").write_text(
        json.dumps({"version": 2, "repository": "acme/monorepo"})
    )
    nested = tmp_path / "services" / "backend"
    nested.mkdir(parents=True)

    config = discover_repo_config(str(nested))

    assert config is not None
    assert config.repository == "acme/monorepo"
    assert config.repo_root == str(tmp_path.resolve())


def test_discover_repo_config_returns_none_and_logs_nothing_when_absent(tmp_path, caplog):
    with caplog.at_level(logging.WARNING, logger="metergraph"):
        config = discover_repo_config(str(tmp_path))

    assert config is None
    assert caplog.records == []


def test_discover_repo_config_ignores_malformed_json_and_warns(tmp_path, caplog):
    (tmp_path / ".metergraph").mkdir()
    (tmp_path / ".metergraph" / "config.json").write_text("{not json")

    with caplog.at_level(logging.WARNING, logger="metergraph"):
        config = discover_repo_config(str(tmp_path))

    assert config is None
    assert any("could not read it" in r.getMessage() for r in caplog.records)


def test_discover_repo_config_ignores_unsupported_version_and_warns(tmp_path, caplog):
    (tmp_path / ".metergraph").mkdir()
    (tmp_path / ".metergraph" / "config.json").write_text(
        json.dumps({"version": 99, "repository": "acme/widgets"})
    )

    with caplog.at_level(logging.WARNING, logger="metergraph"):
        config = discover_repo_config(str(tmp_path))

    assert config is None
    assert any("unsupported schema version" in r.getMessage() for r in caplog.records)


def test_discover_repo_config_works_without_a_git_directory(tmp_path):
    (tmp_path / ".metergraph").mkdir()
    (tmp_path / ".metergraph" / "config.json").write_text(
        json.dumps({"version": 2, "repository": "acme/widgets"})
    )
    assert not (tmp_path / ".git").exists()

    config = discover_repo_config(str(tmp_path))

    assert config is not None
    assert config.repository == "acme/widgets"


# --- ensure_repo_config (discovery + git detection + atomic write) ----------


def test_ensure_repo_config_writes_config_for_https_origin(tmp_path):
    _init_repo_with_origin(tmp_path, "https://github.com/acme/widgets.git")

    config = ensure_repo_config(str(tmp_path))

    assert config is not None
    assert config.repository == "acme/widgets"
    assert config.repo_root == str(tmp_path.resolve())
    written = json.loads((tmp_path / ".metergraph" / "config.json").read_text())
    assert written == {"version": 2, "repository": "acme/widgets"}


def test_ensure_repo_config_writes_config_for_ssh_origin(tmp_path):
    _init_repo_with_origin(tmp_path, "git@github.com:acme/widgets.git")

    config = ensure_repo_config(str(tmp_path))

    assert config is not None
    assert config.repository == "acme/widgets"


def test_ensure_repo_config_writes_at_git_top_level_from_a_nested_app_root(tmp_path):
    _init_repo_with_origin(tmp_path, "https://github.com/acme/monorepo.git")
    nested = tmp_path / "services" / "backend"
    nested.mkdir(parents=True)

    config = ensure_repo_config(str(nested))

    assert config is not None
    assert config.repo_root == str(tmp_path.resolve())
    assert (tmp_path / ".metergraph" / "config.json").exists()
    assert not (nested / ".metergraph").exists()


def test_ensure_repo_config_prefers_an_existing_config_over_git_detection(tmp_path):
    _init_repo_with_origin(tmp_path, "https://github.com/acme/from-git.git")
    (tmp_path / ".metergraph").mkdir()
    existing_path = tmp_path / ".metergraph" / "config.json"
    existing_path.write_text(json.dumps({"version": 2, "repository": "acme/from-file"}))
    mtime_before = existing_path.stat().st_mtime_ns

    config = ensure_repo_config(str(tmp_path))

    assert config is not None
    assert config.repository == "acme/from-file"
    assert existing_path.stat().st_mtime_ns == mtime_before


def test_ensure_repo_config_is_idempotent(tmp_path):
    _init_repo_with_origin(tmp_path, "https://github.com/acme/widgets.git")
    first = ensure_repo_config(str(tmp_path))
    config_path = tmp_path / ".metergraph" / "config.json"
    written_at = config_path.stat().st_mtime_ns

    second = ensure_repo_config(str(tmp_path))

    assert second == first
    assert config_path.stat().st_mtime_ns == written_at


def test_ensure_repo_config_returns_none_when_no_git_repo(tmp_path):
    assert ensure_repo_config(str(tmp_path)) is None
    assert not (tmp_path / ".metergraph").exists()


def test_ensure_repo_config_returns_none_when_no_origin_remote(tmp_path):
    _git("init", cwd=tmp_path)

    assert ensure_repo_config(str(tmp_path)) is None
    assert not (tmp_path / ".metergraph").exists()


def test_ensure_repo_config_returns_none_when_origin_is_not_github(tmp_path):
    _init_repo_with_origin(tmp_path, "git@gitlab.com:acme/widgets.git")

    assert ensure_repo_config(str(tmp_path)) is None
    assert not (tmp_path / ".metergraph").exists()


def test_ensure_repo_config_never_overwrites_a_file_written_between_discovery_and_write(tmp_path):
    """Simulates a race: by the time the write primitive runs, another
    process has already created the file. The winner's content must stick."""
    from metergraph._repo_config import _write_config_atomically

    config_dir = tmp_path / ".metergraph"
    config_dir.mkdir()
    winner_path = config_dir / "config.json"
    winner_path.write_text(json.dumps({"version": 2, "repository": "acme/winner"}) + "\n")

    result = _write_config_atomically(str(tmp_path), "acme/loser")

    assert result is not None
    assert result.repository == "acme/winner"
    assert json.loads(winner_path.read_text())["repository"] == "acme/winner"
