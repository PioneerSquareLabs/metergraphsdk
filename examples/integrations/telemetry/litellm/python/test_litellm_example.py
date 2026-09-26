from __future__ import annotations

import importlib.util
import json
import time
from pathlib import Path

import metergraph
from metergraph import _capture
from metergraph._capture import Options, Runtime


class Rows:
    def __init__(self) -> None:
        self.rows: list[dict] = []

    def enqueue(self, row: dict) -> bool:
        self.rows.append(row)
        return True


def test_litellm_example_emits_replayable_text_trace(monkeypatch):
    example_path = Path(__file__).with_name("main.py")
    spec = importlib.util.spec_from_file_location("litellm_metergraph_example", example_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    rows = Rows()
    _capture.set_runtime(Runtime(rows, Options(app_root="")))
    monkeypatch.setattr(metergraph, "init", lambda: None)

    result = module.run_example()
    deadline = time.monotonic() + 5
    while not rows.rows and time.monotonic() < deadline:
        time.sleep(0.01)

    assert result == "Synthetic LiteLLM response"
    assert len(rows.rows) == 1
    row = rows.rows[0]
    assert row["provider"] == "openai"
    assert row["model"] == "gpt-5-mini"
    request = json.loads(row["request_json"])
    assert json.loads(request["system_instructions"]) == [
        {"type": "text", "content": "Use synthetic data only."}
    ]
    assert json.loads(request["messages"]) == [
        {
            "role": "user",
            "parts": [{"type": "text", "content": "Return a synthetic response."}],
        }
    ]
    assert json.loads(row["response_text"])["content"] == result
    _capture.set_runtime(None)
