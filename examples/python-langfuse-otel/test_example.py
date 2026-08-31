from __future__ import annotations

import importlib.util
import json
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


def test_langfuse_example_captures_generation_span(monkeypatch):
    example_path = Path(__file__).with_name("main.py")
    spec = importlib.util.spec_from_file_location("langfuse_metergraph_example", example_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    rows = Rows()
    _capture.set_runtime(Runtime(rows, Options(app_root="")))
    monkeypatch.setattr(metergraph, "init", lambda: None)

    exporter = module.run_example()

    assert len(rows.rows) == 1
    row = rows.rows[0]
    assert row["provider"] == "langfuse"
    assert row["model"] == "claude-opus-5"
    assert row["input_tokens"] == 11
    assert row["output_tokens"] == 4
    assert row["session_id"] == "sess-1"
    # "user.id" is on the span (real Langfuse spans carry it) and the mapper
    # reads it, but the capture row model has no user field, so it lands
    # nowhere on the tee path.
    assert "user_id" not in row
    assert row["trace_name"] == "synthetic-trace"
    assert json.loads(row["response_text"])["content"] == "Synthetic reply"
    # The non-generation observation is skipped, not captured.
    assert exporter.skipped["ineligible-kind"] == 1
    _capture.set_runtime(None)
