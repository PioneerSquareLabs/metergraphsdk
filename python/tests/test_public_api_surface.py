from __future__ import annotations

import inspect

import metergraph

REQUIRED_EXPORTS = {
    "batch_first",
    "BatchFirstIneligibleError",
    "BatchFirstMetadata",
    "BatchFirstResult",
    "LateBatchInfo",
}

# Internal batch-adapter plumbing that must stay out of the package root —
# only the customer-facing batch_first() entry point and its supporting
# types are meant to be public.
FORBIDDEN_EXPORTS = {
    "run_batch_first",
    "BatchFirstClock",
    "create_openai_batch_adapter",
    "create_anthropic_batch_adapter",
    "create_google_batch_adapter",
    "ProviderBatchAdapter",
    "ProviderBatchEligibility",
    "ProviderBatchResult",
    "BatchHandle",
    "BatchPollResult",
    "ProviderBatchError",
}


def test_all_declares_the_required_batch_first_exports():
    assert REQUIRED_EXPORTS <= set(metergraph.__all__)


def test_all_excludes_internal_batch_adapter_machinery():
    assert not (FORBIDDEN_EXPORTS & set(metergraph.__all__))


def test_module_does_not_expose_forbidden_batch_adapter_names():
    exposed = {name for name in FORBIDDEN_EXPORTS if hasattr(metergraph, name)}
    assert not exposed


def test_batch_first_does_not_accept_polling_or_clock_mechanics():
    params = inspect.signature(metergraph.batch_first).parameters
    assert "poll_interval_seconds" not in params
    assert "clock" not in params
    assert "on_late_batch_settled" in params
