"""Capture OpenRouter usage and billing evidence with an ordinary OpenAI client.

Run: see this folder's README.md. Uses placeholder credentials and synthetic
prompts; point OPENROUTER_BASE_URL at a local fake to run without a real call.
"""

from __future__ import annotations

import os
from urllib.parse import urlsplit

from openai import OpenAI

import metergraph


def build_client():
    api_key = os.environ.get("OPENROUTER_API_KEY", "sk-or-REPLACE_WITH_YOUR_KEY")
    base_url = os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
    # Application code: an ordinary OpenAI client pointed at OpenRouter.
    client = OpenAI(api_key=api_key, base_url=base_url)
    # MeterGraph integration (wrap): the public openrouter.ai host over HTTPS is
    # auto-detected; any other base URL uses the explicit gateway override.
    parsed = urlsplit(base_url)
    if parsed.scheme == "https" and parsed.hostname == "openrouter.ai":
        return metergraph.wrap(client)
    return metergraph.wrap(client, gateway="openrouter")


def main():
    # MeterGraph integration (init): identify captured calls; token and ingest URL
    # come from METERGRAPH_APP_TOKEN / METERGRAPH_INGEST_URL.
    metergraph.init(repository="owner/repository", environment="example")
    client = build_client()
    requested_model = "anthropic/claude-sonnet-4.6"
    try:
        # Application code: an ordinary non-streaming call.
        response = client.chat.completions.create(
            model=requested_model,
            messages=[{"role": "user", "content": "Explain cache-aware pricing in one sentence."}],
        )
        nonstream = {
            "served_model": response.model,
            "content": response.choices[0].message.content,
        }

        # Application code: an ordinary streaming call and its iteration.
        # OpenRouter sends the final usage event and MeterGraph leaves it visible.
        stream = client.chat.completions.create(
            model=requested_model,
            messages=[{"role": "user", "content": "Stream a short note about metered clouds."}],
            stream=True,
        )
        parts = []
        chunk_count = 0
        served_model = None
        for chunk in stream:
            chunk_count += 1
            served_model = served_model or getattr(chunk, "model", None)
            choices = getattr(chunk, "choices", None) or []
            if choices and getattr(choices[0].delta, "content", None):
                parts.append(choices[0].delta.content)
        stream_summary = {
            "served_model": served_model,
            "content": "".join(parts),
            "chunk_count": chunk_count,
        }
        return {"nonstream": nonstream, "stream": stream_summary}
    finally:
        # MeterGraph integration (shutdown): deliver captured rows and stop
        # background work.
        metergraph.shutdown()


if __name__ == "__main__":
    result = main()
    print(
        "non-streaming "
        f"served_model={result['nonstream']['served_model']}"
    )
    print(
        "streaming "
        f"served_model={result['stream']['served_model']} "
        f"chunks={result['stream']['chunk_count']}"
    )
