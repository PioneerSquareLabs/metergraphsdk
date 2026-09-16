from __future__ import annotations

import os
from types import SimpleNamespace

import metergraph
from metergraph import _capture
from metergraph._capture import Options, Runtime


class Rows:
    def __init__(self):
        self.rows = []

    def enqueue(self, row):
        self.rows.append(row)
        return True


def response(search_context_size=None, *, raising=False):
    usage = SimpleNamespace(prompt_tokens=1, completion_tokens=1)
    if raising:
        class RaisingUsage:
            prompt_tokens = 1
            completion_tokens = 1

            @property
            def search_context_size(self):
                raise RuntimeError("provider property failed")

        usage = RaisingUsage()
    elif search_context_size is not None:
        usage.search_context_size = search_context_size
    return SimpleNamespace(
        id="request-1",
        usage=usage,
        choices=[SimpleNamespace(message=SimpleNamespace(content="ok"), finish_reason="stop")],
    )


def client(base_url=None, create=None):
    value = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create or (lambda **_: response())))
    )
    if base_url is not None:
        value.base_url = base_url
    return value


def install_runtime():
    rows = Rows()
    _capture.set_runtime(Runtime(rows, Options(app_root=os.getcwd())))
    return rows


def test_request_search_context_size_normalizes_all_tiers():
    rows = install_runtime()
    try:
        for expected in ("low", "medium", "high"):
            wrapped = client(create=lambda **_: response())
            metergraph.wrap(wrapped, provider="openai")
            wrapped.chat.completions.create(
                model="sonar",
                messages=[],
                web_search_options={"search_context_size": f"  {expected.upper()}  "},
            )
        assert [row["search_context_size"] for row in rows.rows] == ["low", "medium", "high"]
    finally:
        _capture.set_runtime(None)


def test_response_search_context_size_wins_and_invalid_values_are_absent():
    rows = install_runtime()
    try:
        wrapped = client(create=lambda **_: response(" HIGH "))
        metergraph.wrap(wrapped, provider="openai")
        wrapped.chat.completions.create(
            model="sonar", messages=[], web_search_options={"search_context_size": "low"}
        )
        assert rows.rows[0]["search_context_size"] == "high"

        for value in (None, "", "unknown", 1):
            wrapped = client(create=lambda value=value, **_: response(value))
            metergraph.wrap(wrapped, provider="openai")
            wrapped.chat.completions.create(
                model="sonar", messages=[], web_search_options={"search_context_size": value}
            )
        assert all("search_context_size" not in row for row in rows.rows[1:])
    finally:
        _capture.set_runtime(None)


def test_stream_uses_last_chunk_with_usage_and_request_fallback():
    rows = install_runtime()
    try:
        chunk_a = SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content="ok"))])
        usage_chunk = SimpleNamespace(
            choices=[], usage=SimpleNamespace(search_context_size=" MEDIUM ")
        )
        earlier_usage_chunk = SimpleNamespace(
            choices=[], usage=SimpleNamespace(search_context_size=" LOW ")
        )
        final_chunk = SimpleNamespace(
            choices=[SimpleNamespace(delta=SimpleNamespace(content=""), finish_reason="stop")]
        )
        wrapped = client(create=lambda **_: iter([
            chunk_a, earlier_usage_chunk, usage_chunk, final_chunk
        ]))
        metergraph.wrap(wrapped, provider="openai")
        list(wrapped.chat.completions.create(model="sonar", messages=[], stream=True))
        assert rows.rows[0]["search_context_size"] == "medium"

        wrapped = client(create=lambda **_: iter([chunk_a, final_chunk]))
        metergraph.wrap(wrapped, provider="openai")
        list(wrapped.chat.completions.create(
            model="sonar",
            messages=[],
            stream=True,
            web_search_options={"search_context_size": " HIGH "},
        ))
        assert rows.rows[1]["search_context_size"] == "high"
    finally:
        _capture.set_runtime(None)


def test_raising_search_context_property_does_not_drop_row():
    rows = install_runtime()
    try:
        wrapped = client(create=lambda **_: response(raising=True))
        metergraph.wrap(wrapped, provider="openai")
        wrapped.chat.completions.create(model="gpt-test", messages=[])
        assert len(rows.rows) == 1
        assert "search_context_size" not in rows.rows[0]
    finally:
        _capture.set_runtime(None)


def test_perplexity_host_is_auto_labeled_but_lookalikes_and_explicit_openai_are_not():
    rows = install_runtime()
    try:
        for base_url, expected in (
            ("https://api.perplexity.ai", "perplexity"),
            ("https://api.perplexity.ai.evil.com/v1", "openai"),
            ("http://api.perplexity.ai/v1", "openai"),
        ):
            wrapped = client(base_url=base_url)
            metergraph.wrap(wrapped)
            wrapped.chat.completions.create(model="sonar", messages=[])
            assert rows.rows[-1]["provider"] == expected

        wrapped = client(base_url="https://api.perplexity.ai/v1")
        metergraph.wrap(wrapped, provider="openai")
        wrapped.chat.completions.create(model="sonar", messages=[])
        assert rows.rows[-1]["provider"] == "openai"

        wrapped = client()
        metergraph.wrap(wrapped, provider="openai")
        wrapped.chat.completions.create(model="gpt-test", messages=[])
        assert rows.rows[-1]["provider"] == "openai"
        assert "search_context_size" not in rows.rows[-1]
    finally:
        _capture.set_runtime(None)
