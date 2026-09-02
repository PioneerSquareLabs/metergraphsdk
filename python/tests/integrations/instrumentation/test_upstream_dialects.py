"""Ground-truth check: does the REAL Langfuse SDK still emit the attributes
the mapper reads?

Every other mapper test hand-writes span attributes, so it asserts only that
the mapper handles a vocabulary *we* wrote down. If Langfuse renames or drops
an attribute those tests stay green while live capture silently loses a field
-- exactly how a `langfuse.session.id` that never existed in any shipped SDK
survived review.

This module inverts the direction: it drives the real SDK, reads back whatever
attributes the installed version actually produced, and asserts on the
MappedCall they yield. It is meaningful only when its dependency floats, so it
runs from the scheduled `upstream-dialects` workflow (unpinned, weekly), not as
a required pull-request check.

No credentials and no network: the Langfuse client is given dummy keys and a
local TracerProvider.
"""

from __future__ import annotations

import datetime

import pytest

pytest.importorskip("langfuse", reason="upstream-dialects extras not installed")

from opentelemetry.sdk.trace import TracerProvider  # noqa: E402
from opentelemetry.sdk.trace.export import SimpleSpanProcessor  # noqa: E402
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (  # noqa: E402
    InMemorySpanExporter,
)

from metergraph._genai_attrs import SkipReason, map_span_attributes  # noqa: E402


def _memory_provider() -> tuple[InMemorySpanExporter, TracerProvider]:
    memory = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(memory))
    return memory, provider


# Module-scoped on purpose: the Langfuse client is a process-wide singleton, so
# a second Langfuse(...) returns the cached client still bound to the FIRST
# tracer provider, and a function-scoped fixture captures zero spans on every
# test after the first.
@pytest.fixture(scope="module")
def langfuse_spans():
    """Spans the Langfuse SDK really emits for a good and a failed generation."""
    from langfuse import Langfuse, propagate_attributes

    memory, provider = _memory_provider()
    client = Langfuse(
        public_key="pk-not-a-real-key",
        secret_key="sk-not-a-real-key",
        tracer_provider=provider,
        tracing_enabled=True,
    )
    # session/user/trace_name reach spans through baggage, not a setter.
    with propagate_attributes(
        session_id="sess-1", user_id="u-1", trace_name="my-trace"
    ):
        with client.start_as_current_observation(
            as_type="generation",
            name="good-generation",
            model="claude-opus-5",
            input=[{"role": "user", "content": "Synthetic input"}],
            model_parameters={"temperature": 0.2},
        ) as good:
            good.update(
                output={"role": "assistant", "content": "Synthetic reply"},
                usage_details={"input": 11, "output": 4, "total": 15},
                cost_details={"input": 0.001, "output": 0.002, "total": 0.003},
                completion_start_time=datetime.datetime(
                    2026, 8, 31, 12, 0, 0, tzinfo=datetime.timezone.utc
                ),
            )
        with client.start_as_current_observation(
            as_type="generation", name="failed-generation", model="claude-opus-5"
        ) as bad:
            bad.update(level="ERROR", status_message="upstream 500")
    return [
        (span.instrumentation_scope.name, dict(span.attributes or {}))
        for span in memory.get_finished_spans()
    ]


# --- Langfuse ---------------------------------------------------------------


def test_langfuse_scope_name_is_what_the_readme_documents(langfuse_spans):
    assert {scope for scope, _ in langfuse_spans} == {"langfuse-sdk"}


def test_langfuse_generation_maps_to_a_complete_call(langfuse_spans):
    _, attributes = langfuse_spans[0]
    mapped = map_span_attributes(attributes)
    assert not isinstance(mapped, SkipReason), "eligibility gate stopped matching"

    assert mapped.dialects == ("langfuse",)
    assert mapped.model == "claude-opus-5"
    assert mapped.response["usage"] == {"input_tokens": 11, "output_tokens": 4}
    assert mapped.response_text == "Synthetic reply"
    assert mapped.request["messages"] == [
        {"role": "user", "content": "Synthetic input"}
    ]
    assert mapped.request["parameters"] == {"temperature": 0.2}
    assert mapped.cost == 0.003
    assert mapped.cost_source == "langfuse.observation.cost_details.total"
    assert not mapped.parse_degraded
    assert not mapped.usage_absent


def test_langfuse_session_and_user_use_bare_otel_keys(langfuse_spans):
    """Both v3 and v4 spell these without the langfuse.* prefix.

    "user.id" is asserted present even though nothing consumes it yet: it is
    the key an importer would read, and its disappearance is drift worth
    hearing about.

    A prefixed langfuse.session.id has never shipped. If this fails because the
    bare keys vanished, check what replaced them before adding a fallback.
    """
    _, attributes = langfuse_spans[0]
    assert "session.id" in attributes
    assert "user.id" in attributes
    assert "langfuse.session.id" not in attributes
    assert "langfuse.user.id" not in attributes

    mapped = map_span_attributes(attributes)
    assert mapped.session_id == "sess-1"
    assert mapped.trace_name == "my-trace"


def test_langfuse_completion_start_time_survives_json_encoding(langfuse_spans):
    """Langfuse JSON-encodes the timestamp; the mapper must hand the exporter
    a bare ISO string or time-to-first-token is dropped for every span."""
    from metergraph.opentelemetry import _iso_epoch_seconds

    _, attributes = langfuse_spans[0]
    mapped = map_span_attributes(attributes)
    assert mapped.completion_start_time is not None
    assert not mapped.completion_start_time.startswith('"')
    assert _iso_epoch_seconds(mapped.completion_start_time) is not None


def test_langfuse_error_generation_carries_status_message(langfuse_spans):
    _, attributes = langfuse_spans[1]
    mapped = map_span_attributes(attributes)
    assert not isinstance(mapped, SkipReason)
    assert mapped.error_message == "upstream 500"
    # A failed call legitimately reports no usage.
    assert mapped.usage_absent
