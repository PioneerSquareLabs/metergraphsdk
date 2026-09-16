from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from metergraph._capture import Options, Runtime
from metergraph._template import scrub, template_hash
from metergraph.scrub import (
    DEFAULT_CATEGORIES,
    SENSITIVE_KEYS,
    remove_sensitive_keys,
    scrub_text,
    scrub_value,
)


class Rows:
    def __init__(self) -> None:
        self.rows: list[dict] = []

    def enqueue(self, row: dict) -> bool:
        self.rows.append(row)
        return True


def _cases() -> list[dict]:
    path = Path(__file__).parents[2] / "spec" / "scrub-cases.json"
    return json.loads(path.read_text())["cases"]


def test_shared_fixture_cases() -> None:
    for case in _cases():
        categories = case.get("categories", DEFAULT_CATEGORIES)
        assert scrub_text(case["input"], categories) == case["expected"], case["name"]
    json.loads(next(case["expected"] for case in _cases() if case["name"] == "JSON remains valid"))


def test_scrub_text_validates_input_and_categories() -> None:
    with pytest.raises(TypeError):
        scrub_text(123)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        scrub_text("hello", ["unknown"])


def test_scrub_value_walks_nested_values_without_touching_keys() -> None:
    value = {
        "email": "a@example.com",
        "nested": ["206-555-0100", ("sk-proj-abcdefghijklmnopqrstuvwxyz", 3)],
    }
    scrubbed = scrub_value(value)
    assert scrubbed == {
        "email": "<email>",
        "nested": ["<phone>", ["<secret>", 3]],
    }
    assert value["email"] == "a@example.com"


def test_remove_sensitive_keys_preserves_existing_behavior_and_alias() -> None:
    value = {"Authorization": "secret", "keep": {"token": "hidden", "text": "ok"}}
    assert scrub is remove_sensitive_keys
    assert remove_sensitive_keys(value) == {"keep": {"text": "ok"}}
    assert isinstance(SENSITIVE_KEYS, frozenset)

    class Model:
        def model_dump(self, **_kwargs):
            return {"password": "hidden", "value": "ok"}

    assert remove_sensitive_keys(Model()) == {"value": "ok"}


def _capture_row(*, scrub_enabled: bool, capture_enabled: bool = True, stream=False) -> dict:
    rows = Rows()
    runtime = Runtime(
        rows,
        Options(
            app_root="",
            capture_text=capture_enabled,
            scrub_text=scrub_enabled,
        ),
    )
    request = {
        "model": "test-model",
        "messages": [{"role": "user", "content": "email a@example.com phone 206-555-0100 key sk-proj-abcdefghijklmnopqrstuvwxyz"}],
        "tools": [{"type": "function", "function": {"name": "lookup"}}],
    }
    response = SimpleNamespace(
        id="response-1",
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(
                    content="email b@example.com phone +1 (206) 555-0100 key sk-ant-api03-abcdefghijklmnop",
                    tool_calls=[
                        SimpleNamespace(
                            id="call-1",
                            function=SimpleNamespace(
                                name="lookup",
                                arguments=json.dumps({"email": "c@example.com", "phone": "206-555-0100", "key": "sk-proj-abcdefghijklmnopqrstuvwxyz"}),
                            ),
                        )
                    ],
                ),
                finish_reason="stop",
            )
        ],
    )
    call = runtime.call_state("openai", "responses", request)
    call.finish(response, response_text="stream e@example.com 206-555-0100 sk-proj-abcdefghijklmnopqrstuvwxyz" if stream else None, stream=stream)
    return rows.rows[0]


def test_capture_scrubs_opt_in_request_response_tools_and_streams() -> None:
    row = _capture_row(scrub_enabled=True, stream=True)
    for field in ("request_json", "response_text", "tool_calls"):
        encoded = json.dumps(row[field], default=repr)
        assert "@example.com" not in encoded
        assert "206-555-0100" not in encoded
        assert "sk-proj-" not in encoded
    assert "<email>" in row["request_json"]
    assert "<phone>" in row["response_text"]
    assert "<secret>" in row["response_text"]
    assert "<secret>" in json.dumps(row["tool_calls"])
    assert row["template_hash"] == template_hash({
        "model": "test-model",
        "messages": [{"role": "user", "content": "email a@example.com phone 206-555-0100 key sk-proj-abcdefghijklmnopqrstuvwxyz"}],
        "tools": [{"type": "function", "function": {"name": "lookup"}}],
    })


def test_capture_is_unchanged_by_default_and_empty_when_content_is_off() -> None:
    default = _capture_row(scrub_enabled=False)
    assert "a@example.com" in default["request_json"]
    assert "206-555-0100" in default["response_text"]
    metadata_only = _capture_row(scrub_enabled=True, capture_enabled=False)
    assert metadata_only["request_json"] is None
    assert metadata_only["response_text"] is None
    assert metadata_only["tool_calls"][0].keys() == {"call_id", "name", "status", "idempotency"}


def _plain_response_row(*, scrub_enabled: bool) -> dict:
    rows = Rows()
    runtime = Runtime(
        rows,
        Options(app_root="", capture_text=True, scrub_text=scrub_enabled),
    )
    call = runtime.call_state(
        "openai",
        "responses",
        {"model": "test-model", "messages": [{"role": "user", "content": "hello"}]},
    )
    call.finish(
        SimpleNamespace(
            id="response-plain",
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content="hello"),
                    finish_reason="stop",
                )
            ],
        )
    )
    return rows.rows[0]


def test_scrubbing_plain_responses_preserves_an_empty_tool_call_envelope() -> None:
    scrubbed = _plain_response_row(scrub_enabled=True)
    unchanged = _plain_response_row(scrub_enabled=False)
    assert json.loads(scrubbed["response_text"])["tool_calls"] == []
    assert json.loads(unchanged["response_text"])["tool_calls"] == []
    assert scrubbed["tool_calls"] == unchanged["tool_calls"]


def test_default_response_keeps_tool_argument_keys() -> None:
    rows = Rows()
    runtime = Runtime(rows, Options(app_root="", capture_text=True, scrub_text=False))
    response = SimpleNamespace(
        id="response-token",
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(
                    content="ok",
                    tool_calls=[
                        SimpleNamespace(
                            id="call-token",
                            function=SimpleNamespace(
                                name="lookup",
                                arguments=json.dumps({"token": "present"}),
                            ),
                        )
                    ],
                ),
                finish_reason="stop",
            )
        ],
    )
    call = runtime.call_state("openai", "responses", {"model": "test-model"})
    call.finish(response)

    assert '"token":"present"' in rows.rows[0]["response_text"]


def test_capture_scrubs_values_after_serialization() -> None:
    class ReprOnly:
        def __repr__(self) -> str:
            return "opaque a@example.com"

    rows = Rows()
    runtime = Runtime(rows, Options(app_root="", capture_text=True, scrub_text=True))
    scrubbed, failed = runtime._scrub_value({"value": ReprOnly()}, enabled=True)

    assert not failed
    assert scrubbed == {"value": "opaque <email>"}
