"""Repository-aware ingest protocol v2: repo-relative frame path ("p" key)."""

from __future__ import annotations

from pathlib import Path
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


def _make_client():
    class Completions:
        def create(self, **kwargs):
            return SimpleNamespace(
                id="req_1",
                usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(content="ok"), finish_reason="stop"
                    )
                ],
            )

    return SimpleNamespace(chat=SimpleNamespace(completions=Completions()))


def test_frames_include_repo_relative_path_when_under_repo_root():
    rows = Rows()
    app_root = str(Path(__file__).parents[1])  # python/
    repo_root = str(Path(__file__).parents[2])  # worktree root
    _capture.set_runtime(Runtime(rows, Options(app_root=app_root, repo_root=repo_root)))

    client = _make_client()
    metergraph.wrap(client, provider="openai")
    client.chat.completions.create(model="gpt-test", messages=[])
    _capture.set_runtime(None)

    frames = rows.rows[0]["frames_json"]
    assert frames
    this_frame = next(f for f in frames if f["m"].endswith("test_capture_repo_root"))
    assert this_frame["p"] == "python/tests/test_capture_repo_root.py"


def test_frames_have_no_p_key_when_repo_root_is_not_set():
    rows = Rows()
    app_root = str(Path(__file__).parents[1])
    _capture.set_runtime(Runtime(rows, Options(app_root=app_root)))

    client = _make_client()
    metergraph.wrap(client, provider="openai")
    client.chat.completions.create(model="gpt-test", messages=[])
    _capture.set_runtime(None)

    frames = rows.rows[0]["frames_json"]
    assert frames
    assert all("p" not in frame for frame in frames)


def test_frames_have_no_p_key_for_frames_outside_repo_root(tmp_path):
    rows = Rows()
    app_root = str(Path(__file__).parents[1])
    unrelated_repo_root = str(tmp_path)
    _capture.set_runtime(
        Runtime(rows, Options(app_root=app_root, repo_root=unrelated_repo_root))
    )

    client = _make_client()
    metergraph.wrap(client, provider="openai")
    client.chat.completions.create(model="gpt-test", messages=[])
    _capture.set_runtime(None)

    frames = rows.rows[0]["frames_json"]
    assert frames
    assert all("p" not in frame for frame in frames)


def test_repo_root_requires_directory_containment_not_a_string_prefix():
    rows = Rows()
    app_root = str(Path(__file__).parents[1])
    repo_root_prefix_only = str(Path(__file__).parents[2])[:-1]
    _capture.set_runtime(
        Runtime(rows, Options(app_root=app_root, repo_root=repo_root_prefix_only))
    )

    client = _make_client()
    metergraph.wrap(client, provider="openai")
    client.chat.completions.create(model="gpt-test", messages=[])
    _capture.set_runtime(None)

    assert all("p" not in frame for frame in rows.rows[0]["frames_json"])
