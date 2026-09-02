"""Gateway billing evidence capture; OpenRouter is the first qualified contract.

Naming stays provider-agnostic; OpenRouter appears only where a concrete host,
response field, or fixed source string is qualified.
"""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

import metergraph
from metergraph import _capture, _gateway
from metergraph._capture import Options, Runtime


# wrap() auto-initializes from the environment; keep the suite hermetic and rely
# on an explicitly installed runtime instead of a live writer.
os.environ.pop("METERGRAPH_APP_TOKEN", None)
os.environ.pop("METERGRAPH_INGEST_URL", None)

_OPENROUTER_MODEL = "anthropic/claude-sonnet-4.6"
_APP_ROOT = str(Path(__file__).parents[1])
_MISSING = object()


class Rows:
    def __init__(self) -> None:
        self.rows: list[dict] = []

    def enqueue(self, row: dict) -> bool:
        self.rows.append(row)
        return True


def install_runtime() -> Rows:
    rows = Rows()
    _capture.set_runtime(Runtime(rows, Options(app_root=_APP_ROOT)))
    return rows


def chat_usage(cost=0.00482, upstream=0.00482):
    usage = SimpleNamespace(prompt_tokens=920, completion_tokens=110)
    if cost is not _MISSING:
        usage.cost = cost
    if upstream is not _MISSING:
        usage.cost_details = {"upstream_inference_cost": upstream}
    return usage


def chat_response(model=_OPENROUTER_MODEL, cost=0.00482, upstream=0.00482):
    return SimpleNamespace(
        id="req_openrouter_1",
        model=model,
        usage=chat_usage(cost=cost, upstream=upstream),
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content="hi"), finish_reason="stop"
            )
        ],
    )


def openrouter_client(base_url="https://openrouter.ai/api/v1", create=None):
    completions = SimpleNamespace(create=create or (lambda **kw: chat_response()))
    return SimpleNamespace(
        base_url=base_url,
        api_key="sk-or-supersecret",
        chat=SimpleNamespace(completions=completions),
        responses=None,
    )


# Detection: exact HTTPS openrouter.ai only.

@pytest.mark.parametrize(
    "base_url",
    [
        "https://openrouter.ai",
        "https://openrouter.ai/api/v1",
        "https://openrouter.ai/api/v1/",
    ],
)
def test_detect_gateway_accepts_exact_https_host(base_url):
    client = SimpleNamespace(base_url=base_url, chat=SimpleNamespace())
    assert _gateway.detect_gateway(client) == "openrouter"


@pytest.mark.parametrize(
    "base_url",
    [
        "http://openrouter.ai/api/v1",            # not HTTPS
        "https://openrouter.ai.evil.com/api/v1",  # lookalike suffix
        "https://myopenrouter.ai/api/v1",         # substring / prefix
        "https://api.openrouter.ai/api/v1",       # subdomain, not exact
        "https://openrouter.ai.example/v1",       # different TLD lookalike
        "https://example.com/openrouter.ai",      # host is example.com
    ],
)
def test_detect_gateway_rejects_lookalikes_and_http(base_url):
    client = SimpleNamespace(base_url=base_url, chat=SimpleNamespace())
    assert _gateway.detect_gateway(client) is None


def test_detect_gateway_none_base_url_is_none():
    assert _gateway.detect_gateway(SimpleNamespace(chat=SimpleNamespace())) is None


# Override resolution.

def test_resolve_gateway_canonicalizes_case():
    assert _gateway.resolve_gateway("OpenRouter") == "openrouter"


def test_resolve_gateway_rejects_unsupported():
    with pytest.raises(ValueError):
        _gateway.resolve_gateway("litellm")


def test_resolve_gateway_message_excludes_caller_value():
    # An unsupported value may itself be a secret; it must not be echoed back in
    # any casing.
    secret = "sk-or-secret-vendor-xyz"
    with pytest.raises(ValueError) as excinfo:
        _gateway.resolve_gateway(secret)
    message = str(excinfo.value)
    assert secret not in message
    assert secret.upper() not in message
    assert "vendor-xyz" not in message
    assert "openrouter" in message  # supported canonical names are still listed


# Direct-provider rows stay unchanged.

def test_direct_openai_row_has_no_gateway_fields():
    rows = install_runtime()
    try:
        client = SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(create=lambda **kw: chat_response())
            ),
            responses=None,
        )
        metergraph.wrap(client, provider="openai")
        client.chat.completions.create(
            model="gpt-test", messages=[{"role": "user", "content": "hi"}]
        )
        assert len(rows.rows) == 1
        row = rows.rows[0]
        assert row["provider"] == "openai"
        assert row["model"] == "gpt-test"
        for field in (
            "gateway",
            "served_model",
            "reported_cost_usd",
            "reported_upstream_cost_usd",
            "reported_cost_source",
            "reported_upstream_cost_source",
        ):
            assert field not in row
    finally:
        _capture.set_runtime(None)


# Non-streaming Chat Completions: full evidence.

def test_nonstream_openrouter_emits_full_evidence():
    rows = install_runtime()
    try:
        client = openrouter_client()
        result = metergraph.wrap(client)
        assert result is client
        response = client.chat.completions.create(
            model=_OPENROUTER_MODEL, messages=[{"role": "user", "content": "hi"}]
        )
        assert response.id == "req_openrouter_1"
        row = rows.rows[0]
        assert row["provider"] == "openai"        # capture flavor unchanged
        assert row["model"] == _OPENROUTER_MODEL   # requested model preserved
        assert row["gateway"] == "openrouter"
        assert row["served_model"] == _OPENROUTER_MODEL
        assert row["reported_cost_usd"] == 0.00482
        assert row["reported_upstream_cost_usd"] == 0.00482
        assert row["reported_cost_source"] == "openrouter.usage.cost"
        assert (
            row["reported_upstream_cost_source"]
            == "openrouter.usage.cost_details.upstream_inference_cost"
        )
        assert row["input_tokens"] == 920
        assert row["output_tokens"] == 110
        assert "served_provider" not in row  # never inferred from a model prefix
    finally:
        _capture.set_runtime(None)


def test_zero_reported_cost_is_emitted():
    rows = install_runtime()
    try:
        client = openrouter_client(create=lambda **kw: chat_response(cost=0, upstream=0))
        metergraph.wrap(client)
        client.chat.completions.create(model=_OPENROUTER_MODEL, messages=[])
        row = rows.rows[0]
        assert row["reported_cost_usd"] == 0
        assert row["reported_cost_source"] == "openrouter.usage.cost"
        assert row["reported_upstream_cost_usd"] == 0
    finally:
        _capture.set_runtime(None)


def test_missing_cost_omits_cost_but_keeps_gateway_and_served_model():
    rows = install_runtime()
    try:
        client = openrouter_client(
            create=lambda **kw: chat_response(cost=_MISSING, upstream=_MISSING)
        )
        metergraph.wrap(client)
        client.chat.completions.create(model=_OPENROUTER_MODEL, messages=[])
        row = rows.rows[0]
        assert row["gateway"] == "openrouter"
        assert row["served_model"] == _OPENROUTER_MODEL
        for field in (
            "reported_cost_usd",
            "reported_cost_source",
            "reported_upstream_cost_usd",
            "reported_upstream_cost_source",
        ):
            assert field not in row
    finally:
        _capture.set_runtime(None)


def test_missing_served_model_is_omitted():
    rows = install_runtime()
    try:
        client = openrouter_client(create=lambda **kw: chat_response(model=None))
        metergraph.wrap(client)
        client.chat.completions.create(model=_OPENROUTER_MODEL, messages=[])
        row = rows.rows[0]
        assert row["gateway"] == "openrouter"
        assert "served_model" not in row
    finally:
        _capture.set_runtime(None)


@pytest.mark.parametrize("bad", [-1, -0.5, float("nan"), float("inf"), True, False, "0.5", None])
def test_malformed_cost_values_are_omitted(bad):
    rows = install_runtime()
    try:
        client = openrouter_client(
            create=lambda **kw: chat_response(cost=bad, upstream=bad)
        )
        metergraph.wrap(client)
        client.chat.completions.create(model=_OPENROUTER_MODEL, messages=[])
        row = rows.rows[0]
        assert row["gateway"] == "openrouter"
        assert "reported_cost_usd" not in row
        assert "reported_cost_source" not in row
        assert "reported_upstream_cost_usd" not in row
        assert "reported_upstream_cost_source" not in row
    finally:
        _capture.set_runtime(None)


def test_upstream_source_only_when_upstream_emitted():
    rows = install_runtime()
    try:
        client = openrouter_client(
            create=lambda **kw: chat_response(cost=0.01, upstream=_MISSING)
        )
        metergraph.wrap(client)
        client.chat.completions.create(model=_OPENROUTER_MODEL, messages=[])
        row = rows.rows[0]
        assert row["reported_cost_usd"] == 0.01
        assert row["reported_cost_source"] == "openrouter.usage.cost"
        assert "reported_upstream_cost_usd" not in row
        assert "reported_upstream_cost_source" not in row
    finally:
        _capture.set_runtime(None)


# Responses API: gateway identity and served_model are generic observed identity
# and are emitted; cost evidence is not qualified there.

def test_responses_api_emits_identity_without_cost():
    rows = install_runtime()
    try:
        def create(**kw):
            return SimpleNamespace(
                id="resp_1",
                model=_OPENROUTER_MODEL,
                usage=SimpleNamespace(input_tokens=10, output_tokens=2, cost=0.99),
                status="completed",
            )

        client = SimpleNamespace(
            base_url="https://openrouter.ai/api/v1",
            chat=SimpleNamespace(
                completions=SimpleNamespace(create=lambda **kw: chat_response())
            ),
            responses=SimpleNamespace(create=create),
        )
        metergraph.wrap(client)
        client.responses.create(model=_OPENROUTER_MODEL, input="hi")
        row = rows.rows[0]
        assert row["endpoint"] == "responses"
        assert row["gateway"] == "openrouter"
        assert row["served_model"] == _OPENROUTER_MODEL
        for field in (
            "reported_cost_usd",
            "reported_cost_source",
            "reported_upstream_cost_usd",
            "reported_upstream_cost_source",
        ):
            assert field not in row
    finally:
        _capture.set_runtime(None)


# Streaming: every caller-visible chunk is preserved by identity and order,
# including the final usage-only event OpenRouter supplies; evidence still lands.

def test_stream_preserves_all_chunks_and_captures_final_usage_evidence():
    rows = install_runtime()
    try:
        chunk_a = SimpleNamespace(
            model=_OPENROUTER_MODEL,
            choices=[SimpleNamespace(delta=SimpleNamespace(content="Hel"), finish_reason=None)],
        )
        chunk_b = SimpleNamespace(
            model=_OPENROUTER_MODEL,
            choices=[SimpleNamespace(delta=SimpleNamespace(content="lo"), finish_reason="stop")],
        )
        usage_chunk = SimpleNamespace(
            model=_OPENROUTER_MODEL,
            choices=[],
            usage=SimpleNamespace(
                prompt_tokens=920,
                completion_tokens=110,
                cost=0.00482,
                cost_details={"upstream_inference_cost": 0.001},
            ),
        )

        def create(**kw):
            return iter([chunk_a, chunk_b, usage_chunk])

        client = openrouter_client(create=create)
        metergraph.wrap(client)
        seen = list(
            client.chat.completions.create(
                model=_OPENROUTER_MODEL,
                messages=[{"role": "user", "content": "hi"}],
                stream=True,
            )
        )
        # The gateway's usage-only final event is a real caller-visible chunk and
        # is neither withheld nor reordered.
        assert seen == [chunk_a, chunk_b, usage_chunk]
        assert seen[0] is chunk_a
        assert seen[1] is chunk_b
        assert seen[2] is usage_chunk

        row = rows.rows[0]
        assert row["stream"] is True
        assert row["gateway"] == "openrouter"
        assert row["served_model"] == _OPENROUTER_MODEL
        assert row["reported_cost_usd"] == 0.00482
        assert row["reported_upstream_cost_usd"] == 0.001
        assert row["input_tokens"] == 920
    finally:
        _capture.set_runtime(None)


# Custom-domain override.

def test_custom_domain_override_enables_extraction():
    rows = install_runtime()
    try:
        client = openrouter_client(base_url="https://llm.internal.example/v1")
        assert _gateway.detect_gateway(client) is None  # host would not auto-detect
        metergraph.wrap(client, gateway="openrouter")
        client.chat.completions.create(model=_OPENROUTER_MODEL, messages=[])
        row = rows.rows[0]
        assert row["gateway"] == "openrouter"
        assert row["reported_cost_usd"] == 0.00482
        assert row["provider"] == "openai"
    finally:
        _capture.set_runtime(None)


# Configuration combinations.

def test_provider_openai_with_gateway_is_allowed():
    rows = install_runtime()
    try:
        client = openrouter_client(base_url="https://llm.internal.example/v1")
        # A consistent provider alongside the gateway override is accepted.
        metergraph.wrap(client, provider="openai", gateway="openrouter")
        client.chat.completions.create(model=_OPENROUTER_MODEL, messages=[])
        row = rows.rows[0]
        assert row["gateway"] == "openrouter"
        assert row["reported_cost_usd"] == 0.00482
    finally:
        _capture.set_runtime(None)


@pytest.mark.parametrize("provider", ["anthropic", "google", "vercel"])
def test_contradictory_provider_with_gateway_rejected(provider):
    client = openrouter_client()
    with pytest.raises(ValueError):
        metergraph.wrap(client, provider=provider, gateway="openrouter")


def test_unsupported_gateway_override_raises():
    client = openrouter_client()
    with pytest.raises(ValueError):
        metergraph.wrap(client, gateway="portkey")


def test_gateway_override_requires_openai_compatible_client():
    anthropic_like = SimpleNamespace(
        base_url="https://llm.internal.example",
        api_key="sk-secret",
        messages=SimpleNamespace(create=lambda **kw: SimpleNamespace()),
    )
    with pytest.raises(ValueError):
        metergraph.wrap(anthropic_like, gateway="openrouter")


def test_rejection_message_hides_secrets():
    secret_url = "https://tenant-abc.secret-host.example/v1"
    secret_key = "sk-or-THIS-IS-SECRET"
    client = SimpleNamespace(
        base_url=secret_url,
        api_key=secret_key,
        messages=SimpleNamespace(create=lambda **kw: SimpleNamespace()),
    )
    with pytest.raises(ValueError) as excinfo:
        metergraph.wrap(client, gateway="openrouter")
    message = str(excinfo.value)
    assert secret_url not in message
    assert secret_key not in message


# Fail-open: a fault while extracting gateway evidence never alters the provider
# result and never breaks the base row.

def test_gateway_extraction_fault_is_fail_open():
    rows = install_runtime()
    try:
        class ExplodingUsage:
            prompt_tokens = 920
            completion_tokens = 110

            @property
            def cost(self):
                raise RuntimeError("telemetry fault")

        sentinel = SimpleNamespace(
            id="req_fault",
            model=_OPENROUTER_MODEL,
            usage=ExplodingUsage(),
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content="ok"), finish_reason="stop"
                )
            ],
        )
        client = openrouter_client(create=lambda **kw: sentinel)
        metergraph.wrap(client)
        result = client.chat.completions.create(model=_OPENROUTER_MODEL, messages=[])
        assert result is sentinel
        row = rows.rows[0]
        assert row["input_tokens"] == 920
        assert "reported_cost_usd" not in row
    finally:
        _capture.set_runtime(None)


# Evidence-only cost contracts: vendor-reported cost is transported with an
# SDK-fixed provenance string and never asserts gateway identity.

_LF_SPAN_SOURCE = "langfuse.observation.cost_details.total"


def test_evidence_langfuse_float_cost_lands_without_gateway_key():
    evidence = _gateway.evidence_for_mapped_cost(0.0125, _LF_SPAN_SOURCE)
    assert evidence == {
        "reported_cost_usd": 0.0125,
        "reported_cost_source": _LF_SPAN_SOURCE,
    }
    assert "gateway" not in evidence


def test_evidence_langfuse_span_cost_source_is_fixed():
    evidence = _gateway.evidence_for_mapped_cost(0.5, _LF_SPAN_SOURCE)
    assert evidence == {
        "reported_cost_usd": 0.5,
        "reported_cost_source": _LF_SPAN_SOURCE,
    }


@pytest.mark.parametrize(
    "bad",
    [
        "abc",
        "-1",
        "",
        "nan",
        "inf",
        -0.5,
        -1,
        float("nan"),
        float("inf"),
        True,
        False,
        None,
        [0.1],
        {"total": 0.1},
    ],
)
@pytest.mark.parametrize("source", [_LF_SPAN_SOURCE])
def test_evidence_malformed_costs_are_omitted(bad, source):
    assert _gateway.evidence_for_mapped_cost(bad, source) == {}


def test_evidence_unknown_source_emits_nothing():
    # Provenance strings are SDK-fixed; arbitrary caller strings emit nothing.
    assert _gateway.evidence_for_mapped_cost(0.5, "vendor.custom.cost") == {}
    assert _gateway.evidence_for_mapped_cost(0.5, None) == {}


@pytest.mark.parametrize("endpoint", ["inference", "chat", "spans.export", "anything"])
def test_evidence_contract_qualifies_every_endpoint(endpoint):
    evidence = _gateway.gateway_evidence(
        _LF_SPAN_SOURCE, endpoint, SimpleNamespace(cost=0.25)
    )
    assert evidence == {
        "reported_cost_usd": 0.25,
        "reported_cost_source": _LF_SPAN_SOURCE,
    }
    assert "gateway" not in evidence


def test_evidence_contract_row_through_call_state_seam():
    rows = Rows()
    runtime = Runtime(rows, Options(app_root=_APP_ROOT))
    call = runtime.call_state(
        "openai", "inference", {"model": "gpt-test"}, gateway=_LF_SPAN_SOURCE
    )
    call.finish(SimpleNamespace(cost=0.001234, model="gpt-test"))
    row = rows.rows[0]
    assert row["reported_cost_usd"] == 0.001234
    assert row["reported_cost_source"] == _LF_SPAN_SOURCE
    assert "gateway" not in row
    assert "served_model" not in row


def test_evidence_contract_without_cost_emits_no_cost_fields():
    assert _gateway.gateway_evidence(_LF_SPAN_SOURCE, "inference", SimpleNamespace()) == {}


@pytest.mark.parametrize(
    "name",
    ["langfuse", "openinference", "Langfuse", _LF_SPAN_SOURCE],
)
def test_evidence_contract_names_are_not_gateways(name):
    with pytest.raises(ValueError) as excinfo:
        _gateway.resolve_gateway(name)
    message = str(excinfo.value)
    assert "openrouter" in message       # accepted set unchanged
    assert "langfuse" not in message     # evidence names never enumerated
    assert "openinference" not in message


def test_wrap_rejects_evidence_contract_name_like_any_unknown_gateway():
    client = openrouter_client()
    with pytest.raises(ValueError):
        metergraph.wrap(client, gateway="langfuse")


def test_evidence_contracts_do_not_auto_detect():
    client = SimpleNamespace(
        base_url="https://cloud.langfuse.com/api", chat=SimpleNamespace()
    )
    assert _gateway.detect_gateway(client) is None


# OpenRouter regression: the qualified gateway contract is unchanged — float
# cost still lands, string cost is still rejected there.

def test_openrouter_float_cost_still_lands_and_string_cost_still_rejected():
    ok = _gateway.gateway_evidence(
        "openrouter",
        "chat.completions",
        SimpleNamespace(model="m", usage=SimpleNamespace(cost=0.5)),
    )
    assert ok["gateway"] == "openrouter"
    assert ok["reported_cost_usd"] == 0.5
    assert ok["reported_cost_source"] == "openrouter.usage.cost"

    stringy = _gateway.gateway_evidence(
        "openrouter",
        "chat.completions",
        SimpleNamespace(model="m", usage=SimpleNamespace(cost="0.5")),
    )
    assert stringy["gateway"] == "openrouter"
    assert "reported_cost_usd" not in stringy
    assert "reported_cost_source" not in stringy


def test_every_cost_source_the_mapper_emits_is_a_registered_contract():
    """The mapper writes provenance strings; _gateway decides which are real.

    They are deliberately not shared by import -- _genai_attrs is a leaf that
    the capture path must be able to load on its own -- so this guards the
    seam instead. Without it, editing a string on one side silently returns {}
    from evidence_for_mapped_cost and cost vanishes from rows with no error.
    """
    import re
    from pathlib import Path

    from metergraph import _genai_attrs

    emitted = set(
        re.findall(
            r'cost_source = "([^"]+)"',
            Path(_genai_attrs.__file__).read_text(),
        )
    )
    assert emitted, "expected the mapper to emit at least one cost source"
    assert emitted <= _gateway.EVIDENCE_COST_SOURCES
