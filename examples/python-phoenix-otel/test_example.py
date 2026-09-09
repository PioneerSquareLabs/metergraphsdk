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


def test_phoenix_example_captures_openinference_llm_span(monkeypatch):
    example_path = Path(__file__).with_name("main.py")
    spec = importlib.util.spec_from_file_location("phoenix_metergraph_example", example_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    rows = Rows()
    _capture.set_runtime(Runtime(rows, Options(app_root="")))
    monkeypatch.setattr(metergraph, "init", lambda: None)

    exporter = module.run_example()

    # Attribute-level mapping is owned by the mapper and exporter tests; this
    # asserts only that the example's wiring delivers a row at all and routes
    # the two span kinds differently.
    assert len(rows.rows) == 1
    row = rows.rows[0]
    assert row["provider"] == "openai"
    assert row["model"] == "gpt-5-mini"
    assert json.loads(row["response_text"])["content"] == "Synthetic result"
    # The chain span on the same provider is skipped, not captured.
    assert exporter.skipped["ineligible-kind"] == 1
    _capture.set_runtime(None)
