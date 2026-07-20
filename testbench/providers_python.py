#!/usr/bin/env python3
"""Issue one real, Metergraph-wrapped call through each Python provider SDK."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


SDK_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SDK_ROOT / "python" / "src"))

import metergraph  # noqa: E402
from anthropic import Anthropic  # noqa: E402
from google import genai  # noqa: E402
from google.genai import types  # noqa: E402
from openai import OpenAI  # noqa: E402


MODELS = {
    "openai": "gpt-5.6-luna",
    "anthropic": "claude-haiku-4-5-20251001",
    "google": "gemini-2.5-flash",
}
PROMPT = "Reply with exactly OK."


def required(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"missing required environment variable {name}")
    return value


def route_name(provider: str) -> str:
    return ":".join(
        (
            "live-bench",
            required("METERGRAPH_BENCH_RUN_ID"),
            required("METERGRAPH_BENCH_TARGET"),
            "python",
            provider,
        )
    )


def call_openai() -> None:
    client = metergraph.wrap(
        OpenAI(api_key=required("OPENAI_API_KEY")), provider="openai"
    )
    with metergraph.route(route_name("openai")):
        client.chat.completions.create(
            model=MODELS["openai"],
            messages=[{"role": "user", "content": PROMPT}],
            max_completion_tokens=32,
        )


def call_anthropic() -> None:
    client = metergraph.wrap(
        Anthropic(api_key=required("ANTHROPIC_API_KEY")), provider="anthropic"
    )
    with metergraph.route(route_name("anthropic")):
        client.messages.create(
            model=MODELS["anthropic"],
            max_tokens=16,
            messages=[{"role": "user", "content": PROMPT}],
        )


def call_google() -> None:
    client = metergraph.wrap(
        genai.Client(api_key=required("GOOGLE_GENAI_API_KEY")), provider="google"
    )
    with metergraph.route(route_name("google")):
        client.models.generate_content(
            model=MODELS["google"],
            contents=PROMPT,
            config=types.GenerateContentConfig(
                max_output_tokens=16,
                thinking_config=types.ThinkingConfig(thinking_budget=0),
            ),
        )


def main() -> int:
    metergraph.init(
        token=required("METERGRAPH_APP_TOKEN"),
        ingest_url=required("METERGRAPH_INGEST_URL"),
        capture_text=False,
        environment=required("METERGRAPH_ENV"),
        app_root=str(Path(__file__).resolve().parent),
    )
    outcomes = []
    for provider, function in (
        ("openai", call_openai),
        ("anthropic", call_anthropic),
        ("google", call_google),
    ):
        try:
            function()
            outcomes.append(
                {"provider": provider, "model": MODELS[provider], "ok": True}
            )
        except Exception as exc:  # continue so one provider cannot hide the others
            outcomes.append(
                {
                    "provider": provider,
                    "model": MODELS[provider],
                    "ok": False,
                    "error": f"{type(exc).__name__}: {exc}"[:1000],
                }
            )
    flushed = metergraph.flush(timeout=15)
    metergraph.shutdown()
    print(json.dumps({"language": "python", "flushed": flushed, "calls": outcomes}))
    return 0 if flushed and all(item["ok"] for item in outcomes) else 1


if __name__ == "__main__":
    raise SystemExit(main())
