"""Ground-truth check: do the REAL instrumentation libraries still emit the
attributes the mapper reads?

Every other mapper test hand-writes span attributes, so it asserts that the
mapper handles a vocabulary *we* wrote down. If Phoenix, Langfuse or LangSmith
renames or drops an attribute, those tests stay green while live capture
silently loses a field -- exactly how a `langfuse.session.id` that never
existed in any shipped SDK survived review.

This module inverts the direction: it drives the real libraries, reads back
whatever attributes that installed version actually produced, and asserts on
the MappedCall they yield. It is meaningful only when its dependencies float,
so it runs from the scheduled `upstream-dialects` workflow (unpinned, weekly),
not as a required pull-request check.

No credentials and no network: the OpenAI call goes through an httpx
MockTransport, and the Langfuse and LangSmith clients are given dummy keys and
a local TracerProvider.
"""

from __future__ import annotations

import datetime

import pytest

httpx = pytest.importorskip("httpx", reason="upstream-dialects extras not installed")
openai = pytest.importorskip("openai", reason="upstream-dialects extras not installed")
pytest.importorskip(
    "openinference.instrumentation.openai",
    reason="upstream-dialects extras not installed",
)
pytest.importorskip("langfuse", reason="upstream-dialects extras not installed")
pytest.importorskip("langsmith", reason="upstream-dialects extras not installed")

from openinference.instrumentation.openai import OpenAIInstrumentor  # noqa: E402
from opentelemetry.sdk.trace import TracerProvider  # noqa: E402
from opentelemetry.sdk.trace.export import SimpleSpanProcessor  # noqa: E402
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (  # noqa: E402
    InMemorySpanExporter,
)

from metergraph._genai_attrs import SkipReason, map_span_attributes  # noqa: E402

CHAT_RESPONSE = {
    "id": "chatcmpl-upstream",
    "object": "chat.completion",
    "created": 1735689600,
    "model": "gpt-4o-mini-2024-07-18",
    "choices": [
        {
            "index": 0,
            "message": {"role": "assistant", "content": "Synthetic result"},
            "finish_reason": "stop",
        }
    ],
    "usage": {
        "prompt_tokens": 30,
        "completion_tokens": 12,
        "total_tokens": 42,
        "prompt_tokens_details": {"cached_tokens": 5},
        "completion_tokens_details": {"reasoning_tokens": 7},
    },
}


def _memory_provider() -> tuple[InMemorySpanExporter, TracerProvider]:
    memory = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(memory))
    return memory, provider


# Both fixtures are module-scoped on purpose. OpenAIInstrumentor patches the
# openai package globally, and the Langfuse client is a process-wide singleton:
# a second Langfuse(...) returns the cached client still bound to the FIRST
# tracer provider, so a function-scoped fixture would capture zero spans on
# every test after the first.
@pytest.fixture(scope="module")
def openinference_spans():
    """Spans openinference-instrumentation-openai really emits for one call."""
    memory, provider = _memory_provider()
    OpenAIInstrumentor().instrument(tracer_provider=provider)
    try:
        client = openai.OpenAI(
            api_key="sk-not-a-real-key",
            http_client=httpx.Client(
                transport=httpx.MockTransport(
                    lambda request: httpx.Response(200, json=CHAT_RESPONSE)
                )
            ),
        )
        client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "You are terse."},
                {"role": "user", "content": "Synthetic input"},
            ],
            temperature=0.2,
        )
    finally:
        OpenAIInstrumentor().uninstrument()
    return [
        (span.instrumentation_scope.name, dict(span.attributes or {}))
        for span in memory.get_finished_spans()
    ]


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


@pytest.fixture(scope="module")
def langsmith_spans():
    """Spans the LangSmith SDK really emits for an llm, a chain and a tool run.

    tracing_mode="otel" is what makes LangSmith export OTel spans at all; its
    default mode posts runs to the LangSmith API and emits none. The client
    reuses the already-installed global provider, which is the attachment the
    README documents.
    """
    import os

    from opentelemetry import trace as otel_trace

    memory, provider = _memory_provider()
    otel_trace.set_tracer_provider(provider)
    os.environ.update(
        LANGSMITH_TRACING="true",
        LANGSMITH_TRACING_MODE="otel",
        LANGSMITH_API_KEY="lsv2-not-a-real-key",
        LANGSMITH_PROJECT="upstream-dialects",
    )

    from langsmith import traceable
    from langsmith.run_trees import get_cached_client

    client = get_cached_client()

    @traceable(
        run_type="llm",
        name="good-generation",
        metadata={"ls_model_name": "gpt-4o-mini", "ls_provider": "openai"},
    )
    def generation(messages, model="gpt-4o-mini"):
        return CHAT_RESPONSE | {
            "usage_metadata": {
                "input_tokens": 30,
                "output_tokens": 12,
                "total_tokens": 42,
                "input_token_details": {"cache_read": 5},
                "output_token_details": {"reasoning": 7},
            }
        }

    # The chain carries the model name its child run also sees: this is the
    # shape that double-counts if span-kind stops being read.
    @traceable(
        run_type="chain", name="outer-chain", metadata={"ls_model_name": "gpt-4o-mini"}
    )
    def outer(question):
        return generation([{"role": "user", "content": question}])

    @traceable(
        run_type="tool", name="a-tool", metadata={"ls_model_name": "gpt-4o-mini"}
    )
    def a_tool(argument):
        return "tool result"

    outer("Synthetic input")
    a_tool("Synthetic input")
    client.flush()
    provider.force_flush()
    spans = {}
    for span in memory.get_finished_spans():
        attributes = dict(span.attributes or {})
        kind = attributes.get("langsmith.span.kind")
        spans[kind] = (span.instrumentation_scope.name, attributes)
    return spans


# --- OpenInference (Arize Phoenix) -----------------------------------------


def test_openinference_scope_name_is_what_the_readme_documents(openinference_spans):
    assert [scope for scope, _ in openinference_spans] == [
        "openinference.instrumentation.openai"
    ]


def test_openinference_llm_span_maps_to_a_complete_call(openinference_spans):
    (_, attributes), = openinference_spans
    mapped = map_span_attributes(attributes)
    assert not isinstance(mapped, SkipReason), "eligibility gate stopped matching"

    assert mapped.dialects == ("openinference",)
    # llm.request.model_name is not emitted by this instrumentor; the mapper
    # falls back to llm.model_name, which carries the RESOLVED model.
    assert mapped.model == "gpt-4o-mini-2024-07-18"
    assert mapped.provider == "openai"
    assert mapped.response["usage"] == {
        "input_tokens": 30,
        "output_tokens": 12,
        "cache_read_input_tokens": 5,
        "completion_tokens_details": {"reasoning_tokens": 7},
    }
    assert mapped.response["finish_reason"] == "stop"
    assert mapped.response_text == "Synthetic result"
    assert mapped.request["messages"] == [
        {"role": "system", "content": "You are terse."},
        {"role": "user", "content": "Synthetic input"},
    ]
    assert mapped.request["parameters"]["temperature"] == 0.2
    assert not mapped.parse_degraded
    assert not mapped.usage_absent


def test_openinference_still_reports_no_cost(openinference_spans):
    """Phoenix computes cost server-side, so llm.cost.total is absent.

    If this ever starts failing the instrumentor began reporting cost and the
    OPENINFERENCE_SPAN_COST evidence contract goes live -- update the docs,
    which currently say Phoenix cost evidence does not flow on the tee.
    """
    (_, attributes), = openinference_spans
    assert "llm.cost.total" not in attributes
    mapped = map_span_attributes(attributes)
    assert mapped.cost is None
    assert mapped.cost_source is None


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


# --- LangSmith ---------------------------------------------------------------


def test_langsmith_scope_name_is_what_the_readme_documents(langsmith_spans):
    assert {scope for scope, _ in langsmith_spans.values()} == {"langsmith"}


def test_langsmith_llm_run_maps_to_a_complete_call(langsmith_spans):
    _, attributes = langsmith_spans["llm"]
    mapped = map_span_attributes(attributes)
    assert not isinstance(mapped, SkipReason), "eligibility gate stopped matching"

    assert mapped.dialects == ("langsmith", "gen_ai")
    assert mapped.model == "gpt-4o-mini"
    assert mapped.provider == "openai"
    assert mapped.response["usage"] == {
        "input_tokens": 30,
        "output_tokens": 12,
        "cache_read_input_tokens": 5,
        "completion_tokens_details": {"reasoning_tokens": 7},
    }
    assert mapped.response_text == "Synthetic result"
    assert mapped.request["messages"] == [
        {"role": "user", "content": "Synthetic input"}
    ]
    assert mapped.trace_name == "good-generation"
    assert not mapped.parse_degraded
    assert not mapped.usage_absent


def test_langsmith_still_emits_gen_ai_on_non_llm_runs(langsmith_spans):
    """The premise the span-kind gate exists for. If this ever fails LangSmith
    stopped labelling chains and tools as gen_ai spans, and the veto could be
    relaxed -- but do not relax it while this passes."""
    for kind in ("chain", "tool"):
        _, attributes = langsmith_spans[kind]
        assert attributes.get("gen_ai.operation.name") is not None


def test_langsmith_chain_and_tool_runs_are_not_billable_calls(langsmith_spans):
    """Both carry a model name and the chain carries its child's tokens, so
    only langsmith.span.kind keeps them from being counted as calls."""
    for kind in ("chain", "tool"):
        _, attributes = langsmith_spans[kind]
        assert map_span_attributes(attributes) is SkipReason.INELIGIBLE_KIND


def test_langsmith_token_details_are_still_repr_encoded(langsmith_spans):
    """_load_repr_mapping exists only because these are str(dict), not JSON.
    If this starts failing upstream switched to JSON and the fallback can go."""
    import json as _json

    _, attributes = langsmith_spans["llm"]
    raw = attributes["gen_ai.usage.input_token_details"]
    assert isinstance(raw, str)
    with pytest.raises(ValueError):
        _json.loads(raw)


def test_langsmith_still_reports_no_cost(langsmith_spans):
    """LangSmith prices runs server-side, like Phoenix. If this fails the SDK
    began putting cost on spans and a cost evidence contract can be added."""
    _, attributes = langsmith_spans["llm"]
    mapped = map_span_attributes(attributes)
    assert mapped.cost is None
    assert mapped.cost_source is None
