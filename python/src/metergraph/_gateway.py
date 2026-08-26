"""Generic OpenAI-compatible gateway detection and billing-evidence extraction.

A gateway is described by a trusted host set, the endpoints whose responses are
qualified for cost evidence, and the fixed provenance strings the SDK stamps on
that evidence. OpenRouter is the first qualified contract.

The SDK only transports a small allowlist of scalars from a response it already
observes; it never calculates cost, fetches catalog data, or infers a served
provider.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping
from urllib.parse import urlsplit


@dataclass(frozen=True)
class GatewayContract:
    """A single OpenAI-compatible gateway's qualified response contract."""

    name: str
    # Hostnames that trigger automatic detection. Matched exactly against a
    # parsed HTTPS URL's hostname — never by substring.
    hosts: frozenset[str]
    # Endpoints whose responses may carry qualified billing evidence.
    qualified_endpoints: frozenset[str]
    # Fixed, SDK-controlled provenance strings (never gateway-provided).
    cost_source: str
    upstream_cost_source: str


OPENROUTER = GatewayContract(
    name="openrouter",
    hosts=frozenset({"openrouter.ai"}),
    qualified_endpoints=frozenset({"chat.completions"}),
    cost_source="openrouter.usage.cost",
    upstream_cost_source="openrouter.usage.cost_details.upstream_inference_cost",
)

# Registry of supported gateways, keyed by canonical name.
GATEWAYS: dict[str, GatewayContract] = {OPENROUTER.name: OPENROUTER}

# Model/provider identifiers are bounded to the same practical limit the rest of
# the SDK applies to those strings.
_MAX_IDENTIFIER_LEN = 512


def _get(value: Any, name: str) -> Any:
    """Read ``name`` from a mapping or an attribute-bearing object.

    OpenAI-compatible SDKs materialize extra fields differently (an attribute on
    a model object, or a key in a plain dict), so both shapes are supported.
    """
    if isinstance(value, Mapping):
        return value.get(name)
    return getattr(value, name, None)


def detect_gateway(client: Any) -> str | None:
    """Return the canonical gateway name for a trusted HTTPS base URL, else None.

    Detection uses URL parsing, not substring matching: only an exact hostname
    over HTTPS qualifies. Paths (e.g. ``/api/v1``) do not affect detection. The
    caller is responsible for restricting auto-detection to OpenAI-compatible
    clients.
    """
    base_url = getattr(client, "base_url", None)
    if base_url is None:
        base_url = getattr(client, "_base_url", None)
    if base_url is None:
        return None
    try:
        parts = urlsplit(str(base_url).strip())
    except (TypeError, ValueError):
        return None
    if parts.scheme != "https":
        return None
    host = parts.hostname or ""
    for contract in GATEWAYS.values():
        if host in contract.hosts:
            return contract.name
    return None


def resolve_gateway(value: Any) -> str:
    """Validate an explicit gateway override, returning its canonical name.

    The caller value is never interpolated into the error: an unsupported value
    may itself be a secret.
    """
    name = str(value).strip().lower()
    if name not in GATEWAYS:
        supported = ", ".join(sorted(GATEWAYS))
        raise ValueError(
            "metergraph.wrap() received an unsupported gateway; "
            f"supported gateways are: {supported}"
        )
    return name


def _billing_amount(value: Any) -> float | None:
    """Return a finite, non-negative billing amount, or None.

    Booleans, non-numeric values, negatives, and non-finite values are rejected
    so malformed telemetry is omitted rather than transported.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number) or number < 0:
        return None
    return number


def gateway_evidence(
    gateway: str | None, endpoint: str, response: Any
) -> dict[str, Any]:
    """Extract supported scalar evidence for a detected gateway call.

    Gateway identity and served model are generic observed identity, emitted for
    any detected gateway call. Cost evidence and its fixed source strings are
    emitted only on the gateway's qualified endpoints. A provenance source
    appears only alongside the value it describes. The served provider is never
    inferred.
    """
    if gateway is None:
        return {}
    contract = GATEWAYS.get(gateway)
    if contract is None:
        return {}

    evidence: dict[str, Any] = {"gateway": contract.name}

    served_model = _get(response, "model")
    if isinstance(served_model, str) and served_model.strip():
        evidence["served_model"] = served_model[:_MAX_IDENTIFIER_LEN]

    if endpoint in contract.qualified_endpoints:
        usage = _get(response, "usage")
        cost = _billing_amount(_get(usage, "cost"))
        if cost is not None:
            evidence["reported_cost_usd"] = cost
            evidence["reported_cost_source"] = contract.cost_source

        cost_details = _get(usage, "cost_details")
        upstream = _billing_amount(_get(cost_details, "upstream_inference_cost"))
        if upstream is not None:
            evidence["reported_upstream_cost_usd"] = upstream
            evidence["reported_upstream_cost_source"] = contract.upstream_cost_source

    return evidence
