"""Ground-truth check: do the REAL upstream libraries still emit the attributes
the mapper reads?

Every other mapper test hand-writes span attributes, so it asserts only that
the mapper handles a vocabulary *we* wrote down. If Langfuse or OpenInference
renames or drops an attribute those tests stay green while live capture
silently loses a field -- exactly how a `langfuse.session.id` that never
existed in any shipped SDK survived review.

This module inverts the direction: it drives the real libraries -- the Langfuse
SDK, and openinference-instrumentation-openai over an httpx MockTransport --
reads back whatever attributes the installed versions actually produced, and
asserts on the MappedCall they yield. It is meaningful only when its
dependencies float, so it runs from the scheduled `upstream-dialects` workflow
(unpinned, weekly), not as a required pull-request check.

No credentials and no network. Dummy keys, a mock transport and a local
TracerProvider are not enough on their own -- the Langfuse client also installs
its own OTLP processor on whatever provider it is handed, which used to export
to cloud.langfuse.com and print `401 Unauthorized` after these assertions had
already passed. Its transport is replaced with a local exporter, and both
fixtures run with every outbound connection blocked and recorded, so a future
version of either library that reaches the network turns this job red instead
of depending on what a remote host answers.
"""
from __future__ import annotations

import contextlib
import datetime
import socket
from dataclasses import dataclass
from typing import Iterator, Sequence

import pytest

httpx = pytest.importorskip("httpx", reason="upstream-dialects extras not installed")
openai = pytest.importorskip("openai", reason="upstream-dialects extras not installed")
pytest.importorskip(
    "openinference.instrumentation.openai",
    reason="upstream-dialects extras not installed",
)
pytest.importorskip("langfuse", reason="upstream-dialects extras not installed")

from openinference.instrumentation.openai import OpenAIInstrumentor  # noqa: E402
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider  # noqa: E402
from opentelemetry.sdk.trace.export import (  # noqa: E402
    SimpleSpanProcessor,
    SpanExporter,
    SpanExportResult,
)
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


class _LocalSpanExporter(SpanExporter):
    """Stands in for Langfuse's OTLP transport: accepts batches, sends nothing.

    Langfuse builds an OTLPSpanExporter aimed at its cloud endpoint unless it
    is handed one, and installs it on the provider given to the constructor --
    including ours. Supplying this leaves the in-memory processor untouched
    while giving the Langfuse processor nowhere to send.
    """

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        return SpanExportResult.SUCCESS

    def shutdown(self) -> None:
        return None

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        return True


# Loopback stays reachable so a genuinely local service is not misreported as
# an escape; nothing in this module needs even that.
_LOOPBACK = frozenset({"localhost", "127.0.0.1", "::1"})


@contextlib.contextmanager
def _blocked_network() -> Iterator[list[str]]:
    """Refuse and record every attempt to leave the machine.

    Both layers matter: requests/urllib3 and httpx resolve through
    ``socket.getaddrinfo`` before connecting, and a literal IP skips resolution
    entirely. Recording rather than only raising is what lets a test assert on
    the attempt -- the export runs on a background thread, where a raised
    AssertionError would otherwise be swallowed into a log line.
    """
    attempts: list[str] = []
    real_getaddrinfo = socket.getaddrinfo
    real_connect = socket.socket.connect

    def escapes(host: object) -> bool:
        if isinstance(host, str) and host not in _LOOPBACK:
            attempts.append(host)
            return True
        return False

    def guarded_getaddrinfo(host, port, *args, **kwargs):
        if escapes(host):
            raise AssertionError(f"upstream drift watch resolved {host!r}")
        return real_getaddrinfo(host, port, *args, **kwargs)

    def guarded_connect(self, address):
        if isinstance(address, tuple) and escapes(address[0]):
            raise AssertionError(f"upstream drift watch dialed {address[0]!r}")
        return real_connect(self, address)

    socket.getaddrinfo = guarded_getaddrinfo
    socket.socket.connect = guarded_connect
    try:
        yield attempts
    finally:
        socket.getaddrinfo = real_getaddrinfo
        socket.socket.connect = real_connect


@dataclass(frozen=True)
class _DialectRun:
    spans: list[tuple[str, dict]]
    outbound: tuple[str, ...]


# Both fixtures are module-scoped on purpose. OpenAIInstrumentor patches the
# openai package globally, and the Langfuse client is a process-wide singleton:
# a second Langfuse(...) returns the cached client still bound to the FIRST
# tracer provider, so a function-scoped fixture would capture zero spans on
# every test after the first.
@pytest.fixture(scope="module")
def openinference_run() -> _DialectRun:
    """Drive the real instrumentor once, under a closed network, and keep the spans."""
    memory, provider = _memory_provider()
    with _blocked_network() as outbound:
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
    return _DialectRun(
        spans=[
            (span.instrumentation_scope.name, dict(span.attributes or {}))
            for span in memory.get_finished_spans()
        ],
        outbound=tuple(outbound),
    )


@pytest.fixture(scope="module")
def openinference_spans(openinference_run: _DialectRun) -> list[tuple[str, dict]]:
    """Spans openinference-instrumentation-openai really emits for one call."""
    return openinference_run.spans


@pytest.fixture(scope="module")
def langfuse_run() -> _DialectRun:
    """Drive the real SDK once, under a closed network, and keep what it emitted."""
    from langfuse import Langfuse, propagate_attributes

    memory, provider = _memory_provider()
    with _blocked_network() as outbound:
        client = Langfuse(
            public_key="pk-not-a-real-key",
            secret_key="sk-not-a-real-key",
            tracer_provider=provider,
            tracing_enabled=True,
            span_exporter=_LocalSpanExporter(),
        )
        _emit_observations(client, propagate_attributes)
        # Inside the guard on purpose: shutdown() force-flushes the Langfuse
        # processor and joins its threads, so any export this version attempts
        # happens here rather than at interpreter exit, unguarded and after
        # the assertions have already reported green.
        client.shutdown()
    return _DialectRun(
        spans=[
            (span.instrumentation_scope.name, dict(span.attributes or {}))
            for span in memory.get_finished_spans()
        ],
        outbound=tuple(outbound),
    )


@pytest.fixture(scope="module")
def langfuse_spans(langfuse_run: _DialectRun) -> list[tuple[str, dict]]:
    """Spans the Langfuse SDK really emits for a good and a failed generation."""
    return langfuse_run.spans


def _emit_observations(client, propagate_attributes) -> None:
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


# --- OpenInference (Arize Phoenix) -----------------------------------------


def test_openinference_stays_off_the_network(openinference_run):
    """The MockTransport carries the call; nothing may reach a real endpoint.

    Asserted rather than assumed: the instrumentor and the OpenAI client are
    both unpinned here, and a version that adds its own outbound request has
    to fail this weekly job rather than quietly depend on a remote host.
    """
    assert openinference_run.outbound == ()


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


def test_langfuse_sdk_stays_off_the_network(langfuse_run):
    """The drift watch must observe the SDK, never a Langfuse deployment.

    It runs weekly, unpinned and unattended, so a version that starts
    exporting again has to fail here. Before this guard existed the assertions
    below all passed and the run then printed `401 Unauthorized` from a
    background export nobody was watching.
    """
    assert langfuse_run.outbound == ()


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
