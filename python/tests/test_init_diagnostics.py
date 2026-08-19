from __future__ import annotations

import logging

import metergraph


def _reset() -> None:
    metergraph.shutdown()
    metergraph._initialized = False
    metergraph._warned_no_token = False
    metergraph._warned_no_repository = False
    metergraph._warned_repeated_init = False


def test_repeated_explicit_init_warns_once_without_configuration_details(
    caplog, tmp_path
):
    _reset()
    caplog.set_level(logging.WARNING, logger="metergraph")
    initial = {
        "token": "secret-initial",
        "ingest_url": "http://127.0.0.1:9",
        "repository": "acme/widgets",
        "environment": "staging",
        "capture_text": False,
        "app_root": str(tmp_path),
    }

    metergraph.init(**initial)
    metergraph.init(**initial)
    metergraph.init(
        **{
            **initial,
            "token": "secret-conflict",
            "repository": "other/widgets",
            "environment": "prod",
        }
    )
    metergraph.init(
        **{**initial, "token": "secret-third", "repository": "third/widgets"}
    )
    metergraph.wrap(object(), provider="openai")

    warnings = [r.message for r in caplog.records if "called more than once" in r.message]
    assert warnings == [
        "Metergraph init() was called more than once; "
        "the first configuration remains active."
    ]
    assert "secret-initial" not in warnings[0]
    assert "secret-conflict" not in warnings[0]
    assert "secret-third" not in warnings[0]
    assert "token" not in warnings[0]
    assert "repository" not in warnings[0]
    assert "environment" not in warnings[0]
    _reset()


def test_repeated_environment_based_init_uses_the_same_generic_warning(
    caplog, monkeypatch, tmp_path
):
    _reset()
    caplog.set_level(logging.WARNING, logger="metergraph")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("METERGRAPH_APP_TOKEN", "secret-env-initial")
    monkeypatch.setenv("METERGRAPH_INGEST_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("METERGRAPH_REPOSITORY", "acme/widgets")
    monkeypatch.setenv("METERGRAPH_ENV", "staging")

    metergraph.init()
    monkeypatch.setenv("METERGRAPH_APP_TOKEN", "secret-env-conflict")
    monkeypatch.setenv("METERGRAPH_REPOSITORY", "other/widgets")
    monkeypatch.setenv("METERGRAPH_ENV", "production")
    metergraph.init()

    warnings = [r.message for r in caplog.records if "called more than once" in r.message]
    assert warnings == [
        "Metergraph init() was called more than once; "
        "the first configuration remains active."
    ]
    assert "secret-env-initial" not in warnings[0]
    assert "secret-env-conflict" not in warnings[0]
    assert "token" not in warnings[0]
    assert "repository" not in warnings[0]
    assert "environment" not in warnings[0]
    _reset()
