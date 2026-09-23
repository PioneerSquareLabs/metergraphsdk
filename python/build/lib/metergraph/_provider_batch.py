"""Structural provider Batch API adapters that batch_first() drives.

Deliberately duck-typed against each provider's real Python SDK shape (see
create_openai_batch_adapter / create_anthropic_batch_adapter /
create_google_batch_adapter) rather than importing those packages —
openai, anthropic, and google-genai are dev-only extras of this package
(see pyproject.toml's [project.optional-dependencies].dev), and
attribute/key-based access already lets a real client instance satisfy
these adapters without any import, type-only or otherwise.

Every adapter method here returns only bounded, structural information —
a status, a result the caller explicitly asked for, and a boolean noting
whether that result happened to contain a tool-call plan. No adapter
method inspects or logs a provider error body, a credential, or a raw
prompt.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any, Mapping


def _get(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


class ProviderBatchError(Exception):
    """A bounded, sanitized batch failure — never carries the provider's
    own error text or body."""


@dataclass(frozen=True)
class BatchHandle:
    """The provider's own batch identifier — never returned to a caller of
    batch_first() or logged; adapters use it only to poll/read their own
    batch."""

    provider_batch_id: str


@dataclass(frozen=True)
class BatchPollResult:
    status: str  # "pending" | "completed" | "failed" | "expired"


@dataclass(frozen=True)
class ProviderBatchResult:
    result: Any
    # Reported as a boolean only, never by exposing the call's arguments or
    # the surrounding response through this flag.
    contained_tool_call_plan: bool = False


@dataclass(frozen=True)
class ProviderBatchEligibility:
    eligible: bool
    reason: str | None = None


class ProviderBatchAdapter:
    """The seam run_batch_first()/batch_first() drive. An adapter submits exactly
    one request per batch and knows how to poll it, read its terminal
    result, and run the same request directly (bypassing batch entirely)
    as a fallback.

    A duck-typed shape, not an enforced interface — any object exposing
    these five methods satisfies it, matching this module's own adapters
    below.
    """

    def eligibility(self, request: Mapping[str, Any]) -> ProviderBatchEligibility:
        raise NotImplementedError

    def submit_one(self, request: Mapping[str, Any]) -> BatchHandle:
        raise NotImplementedError

    def poll(self, handle: BatchHandle) -> BatchPollResult:
        raise NotImplementedError

    def read_result(self, handle: BatchHandle) -> ProviderBatchResult:
        raise NotImplementedError

    def direct(self, request: Mapping[str, Any]) -> ProviderBatchResult:
        raise NotImplementedError


def _random_custom_id() -> str:
    return f"batch-first-{uuid.uuid4().hex}"


def _is_streaming(request: Mapping[str, Any]) -> bool:
    return request.get("stream") is True


def _streaming_ineligible() -> ProviderBatchEligibility:
    return ProviderBatchEligibility(eligible=False, reason="streaming requests are direct-only")


# ---------- OpenAI ----------

_OPENAI_BATCH_ENDPOINT = "/v1/responses"
_OPENAI_COMPLETION_WINDOW = "24h"

# "validating" | "in_progress" | "finalizing" | "cancelling" all fall
# through to "pending" below, alongside any future status OpenAI adds —
# only a recognized terminal status is ever treated as terminal.
_OPENAI_FAILED_STATUSES = {"failed", "cancelled"}


@dataclass(frozen=True)
class _OpenAIBatchHandle(BatchHandle):
    custom_id: str


def _has_openai_function_call(body: Any) -> bool:
    output = _get(body, "output")
    if not isinstance(output, list):
        return False
    return any(_get(item, "type") == "function_call" for item in output)


class _OpenAIBatchAdapter(ProviderBatchAdapter):
    """Duck-typed against the `openai` package's client shape: a real
    `OpenAI` client instance (openai>=2.50) satisfies this without any
    import — see files.create/files.content/batches.create/
    batches.retrieve/responses.create."""

    def __init__(self, client: Any) -> None:
        self._client = client

    def eligibility(self, request: Mapping[str, Any]) -> ProviderBatchEligibility:
        if _is_streaming(request):
            return _streaming_ineligible()
        return ProviderBatchEligibility(eligible=True)

    def submit_one(self, request: Mapping[str, Any]) -> BatchHandle:
        custom_id = _random_custom_id()
        line = json.dumps(
            {
                "custom_id": custom_id,
                "method": "POST",
                "url": _OPENAI_BATCH_ENDPOINT,
                "body": dict(request),
            }
        )
        uploaded = self._client.files.create(
            file=(f"{custom_id}.jsonl", f"{line}\n".encode("utf-8"), "application/jsonl"),
            purpose="batch",
        )
        batch = self._client.batches.create(
            input_file_id=_get(uploaded, "id"),
            endpoint=_OPENAI_BATCH_ENDPOINT,
            completion_window=_OPENAI_COMPLETION_WINDOW,
        )
        return _OpenAIBatchHandle(provider_batch_id=_get(batch, "id"), custom_id=custom_id)

    def poll(self, handle: BatchHandle) -> BatchPollResult:
        batch = self._client.batches.retrieve(handle.provider_batch_id)
        status = _get(batch, "status") or ""
        if status == "completed":
            return BatchPollResult(status="completed")
        if status == "expired":
            return BatchPollResult(status="expired")
        if status in _OPENAI_FAILED_STATUSES:
            return BatchPollResult(status="failed")
        return BatchPollResult(status="pending")

    def read_result(self, handle: BatchHandle) -> ProviderBatchResult:
        assert isinstance(handle, _OpenAIBatchHandle)
        batch = self._client.batches.retrieve(handle.provider_batch_id)
        output_file_id = _get(batch, "output_file_id")
        if not output_file_id:
            raise ProviderBatchError("openai batch has no output file to read")
        file_content = self._client.files.content(output_file_id)
        text = file_content.text if hasattr(file_content, "text") else str(file_content)
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except (TypeError, ValueError):
                continue
            if _get(item, "custom_id") != handle.custom_id:
                continue
            response = _get(item, "response")
            status_code = _get(response, "status_code")
            body = _get(response, "body")
            error = _get(item, "error")
            if error is not None or (
                isinstance(status_code, (int, float)) and status_code >= 400
            ):
                # Never surface the provider's own error text — only that
                # this one item failed.
                raise ProviderBatchError("openai batch item returned an error response")
            return ProviderBatchResult(
                result=body, contained_tool_call_plan=_has_openai_function_call(body)
            )
        raise ProviderBatchError("openai batch output did not contain our submitted item")

    def direct(self, request: Mapping[str, Any]) -> ProviderBatchResult:
        response = self._client.responses.create(**dict(request))
        return ProviderBatchResult(
            result=response, contained_tool_call_plan=_has_openai_function_call(response)
        )


def create_openai_batch_adapter(client: Any) -> ProviderBatchAdapter:
    return _OpenAIBatchAdapter(client)


# ---------- Anthropic ----------

_ANTHROPIC_ENDED_STATUS = "ended"


@dataclass(frozen=True)
class _AnthropicBatchHandle(BatchHandle):
    custom_id: str


def _has_tool_use(message: Any) -> bool:
    content = _get(message, "content")
    if not isinstance(content, list):
        return False
    return any(_get(block, "type") == "tool_use" for block in content)


class _AnthropicBatchAdapter(ProviderBatchAdapter):
    """Duck-typed against the `anthropic` package's client shape: a real
    `Anthropic` client instance (anthropic>=0.40) satisfies this without
    any import — see messages.batches.create/retrieve/results and
    messages.create. Corroborated by this SDK's own wrap() support for
    messages.batches.results() (see _capture._patch_anthropic_batch_results),
    which already confirms the real per-item {custom_id, result: {type,
    message}} shape against anthropic>=0.40."""

    def __init__(self, client: Any) -> None:
        self._client = client

    def eligibility(self, request: Mapping[str, Any]) -> ProviderBatchEligibility:
        if _is_streaming(request):
            return _streaming_ineligible()
        return ProviderBatchEligibility(eligible=True)

    def submit_one(self, request: Mapping[str, Any]) -> BatchHandle:
        custom_id = _random_custom_id()
        batch = self._client.messages.batches.create(
            requests=[{"custom_id": custom_id, "params": dict(request)}]
        )
        return _AnthropicBatchHandle(provider_batch_id=_get(batch, "id"), custom_id=custom_id)

    def poll(self, handle: BatchHandle) -> BatchPollResult:
        batch = self._client.messages.batches.retrieve(handle.provider_batch_id)
        # Only "ended" is terminal — and it's a one-way signal: there is no
        # un-ending it, so any request_counts bucket shape we don't
        # recognize below is bounded to "failed" rather than left pending
        # forever.
        if _get(batch, "processing_status") != _ANTHROPIC_ENDED_STATUS:
            return BatchPollResult(status="pending")
        counts = _get(batch, "request_counts") or {}
        if (_get(counts, "succeeded") or 0) > 0:
            return BatchPollResult(status="completed")
        if (_get(counts, "expired") or 0) > 0:
            return BatchPollResult(status="expired")
        return BatchPollResult(status="failed")

    def read_result(self, handle: BatchHandle) -> ProviderBatchResult:
        assert isinstance(handle, _AnthropicBatchHandle)
        for item in self._client.messages.batches.results(handle.provider_batch_id):
            if _get(item, "custom_id") != handle.custom_id:
                continue
            result = _get(item, "result")
            if _get(result, "type") != "succeeded":
                # Never surface the provider's own error text — only that
                # this one item failed.
                raise ProviderBatchError("anthropic batch item did not succeed")
            message = _get(result, "message")
            return ProviderBatchResult(
                result=message, contained_tool_call_plan=_has_tool_use(message)
            )
        raise ProviderBatchError("anthropic batch results did not contain our submitted item")

    def direct(self, request: Mapping[str, Any]) -> ProviderBatchResult:
        response = self._client.messages.create(**dict(request))
        return ProviderBatchResult(
            result=response, contained_tool_call_plan=_has_tool_use(response)
        )


def create_anthropic_batch_adapter(client: Any) -> ProviderBatchAdapter:
    return _AnthropicBatchAdapter(client)


# ---------- Google ----------

# Deliberately conservative: only these four wire states are recognized as
# terminal. Everything else — JOB_STATE_QUEUED/PENDING/RUNNING, and also
# JOB_STATE_PARTIALLY_SUCCEEDED/PAUSED/UPDATING/CANCELLING, which a
# single-item batch should never actually reach in a way we'd need to
# resolve — stays "pending" until the deadline, never a wrong canonical
# result.
_GOOGLE_TERMINAL_STATUS = {
    "JOB_STATE_SUCCEEDED": "completed",
    "JOB_STATE_FAILED": "failed",
    "JOB_STATE_CANCELLED": "failed",
    "JOB_STATE_EXPIRED": "expired",
}


def _has_function_call_part(response: Any) -> bool:
    candidates = _get(response, "candidates")
    if not isinstance(candidates, list):
        return False
    for candidate in candidates:
        parts = _get(_get(candidate, "content"), "parts")
        if not isinstance(parts, list):
            continue
        for part in parts:
            if _get(part, "function_call") is not None or _get(part, "functionCall") is not None:
                return True
    return False


class _GoogleBatchAdapter(ProviderBatchAdapter):
    """Duck-typed against the `google-genai` package's client shape: a real
    `genai.Client` instance (google-genai>=1) satisfies this without any
    import — see batches.create/batches.get and models.generate_content.
    """

    def __init__(self, client: Any) -> None:
        self._client = client

    def eligibility(self, request: Mapping[str, Any]) -> ProviderBatchEligibility:
        if _is_streaming(request):
            return _streaming_ineligible()
        return ProviderBatchEligibility(eligible=True)

    def submit_one(self, request: Mapping[str, Any]) -> BatchHandle:
        model = request.get("model")
        if not isinstance(model, str) or not model.strip():
            # Bounded and raised before any provider call — submit_one is
            # never wrapped by run_batch_first, so this propagates exactly
            # like any other "no batch was ever created" failure.
            raise ProviderBatchError(
                "google batch requests require a non-blank request['model']"
            )
        inline_request = {key: value for key, value in request.items() if key != "model"}
        job = self._client.batches.create(model=model, src=[inline_request])
        return BatchHandle(provider_batch_id=_get(job, "name"))

    def poll(self, handle: BatchHandle) -> BatchPollResult:
        job = self._client.batches.get(name=handle.provider_batch_id)
        state = _get(job, "state")
        return BatchPollResult(status=_GOOGLE_TERMINAL_STATUS.get(state, "pending"))

    def read_result(self, handle: BatchHandle) -> ProviderBatchResult:
        job = self._client.batches.get(name=handle.provider_batch_id)
        entries = _get(_get(job, "dest"), "inlined_responses")
        # A single-item batch is unambiguous by construction: index 0 is
        # "our" item, the same way a custom_id is for the other two
        # providers — there is no cross-item confusion possible here.
        entry = entries[0] if isinstance(entries, list) and entries else None
        if entry is None:
            raise ProviderBatchError("google batch produced no inline response")
        if _get(entry, "error") is not None:
            # Never surface the provider's own error text — only that this
            # one item failed.
            raise ProviderBatchError("google batch item returned an error response")
        response = _get(entry, "response")
        if response is None:
            raise ProviderBatchError("google batch item had no response body")
        return ProviderBatchResult(
            result=response, contained_tool_call_plan=_has_function_call_part(response)
        )

    def direct(self, request: Mapping[str, Any]) -> ProviderBatchResult:
        response = self._client.models.generate_content(**dict(request))
        return ProviderBatchResult(
            result=response, contained_tool_call_plan=_has_function_call_part(response)
        )


def create_google_batch_adapter(client: Any) -> ProviderBatchAdapter:
    return _GoogleBatchAdapter(client)
