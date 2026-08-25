"""Assert every seam-table entry resolves against the real, installed
provider SDKs — not just against hand-built fakes, which can only ever
verify the code against what its author believed the SDK looked like."""

from __future__ import annotations

from anthropic import Anthropic
from google import genai
from openai import OpenAI

from metergraph._capture import ANTHROPIC_SEAMS, GOOGLE_SEAMS, OPENAI_SEAMS, _resolve


def _missing_seams(client, seams) -> list[str]:
    missing = []
    for seam in seams:
        owner = _resolve(client, seam.path)
        if owner is None or not callable(getattr(owner, seam.method, None)):
            missing.append(f"{seam.path}.{seam.method}")
    return missing


def test_openai_seams_exist_on_the_real_sdk():
    client = OpenAI(api_key="test")
    assert _missing_seams(client, OPENAI_SEAMS) == []


def test_anthropic_seams_exist_on_the_real_sdk():
    client = Anthropic(api_key="test")
    assert _missing_seams(client, ANTHROPIC_SEAMS) == []


def test_google_seams_exist_on_the_real_sdk():
    client = genai.Client(api_key="test")
    assert _missing_seams(client, GOOGLE_SEAMS) == []
