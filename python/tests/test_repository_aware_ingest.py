"""Read-only repository identity discovery."""

import json
import logging

import metergraph._repo_config as repo_config_module
from metergraph._repo_config import discover_repo_config


def test_repo_config_module_has_no_detection_or_write_api():
    assert not hasattr(repo_config_module, "ensure_repo_config")
    assert not hasattr(repo_config_module, "_write_config_atomically")


def _write_config(root, document):
    (root / ".metergraph").mkdir()
    (root / ".metergraph" / "config.json").write_text(json.dumps(document))


def test_discover_repo_config_finds_file_and_defaults_missing_version(tmp_path):
    _write_config(tmp_path, {"repository": "acme/widgets"})
    config = discover_repo_config(str(tmp_path))
    assert config is not None
    assert config.repository == "acme/widgets"
    assert config.repo_root == str(tmp_path.resolve())


def test_discover_repo_config_walks_up_without_git_metadata(tmp_path):
    _write_config(tmp_path, {"version": 2, "repository": "acme/monorepo"})
    nested = tmp_path / "services" / "backend"
    nested.mkdir(parents=True)
    assert not (tmp_path / ".git").exists()

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
    assert any("could not read it" in record.getMessage() for record in caplog.records)


def test_discover_repo_config_ignores_unsupported_version_and_warns(tmp_path, caplog):
    _write_config(tmp_path, {"version": 99, "repository": "acme/widgets"})
    with caplog.at_level(logging.WARNING, logger="metergraph"):
        config = discover_repo_config(str(tmp_path))
    assert config is None
    assert any(
        "unsupported schema version" in record.getMessage()
        for record in caplog.records
    )
