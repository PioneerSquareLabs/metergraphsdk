from __future__ import annotations

import json

import pytest

from metergraph._provider_batch import (
    ProviderBatchError,
    create_anthropic_batch_adapter,
    create_google_batch_adapter,
    create_openai_batch_adapter,
)


# ---------- OpenAI ----------

OPENAI_REQUEST = {"model": "gpt-5-mini", "input": "hello"}


class FakeOpenAIClient:
    def __init__(
        self,
        *,
        batch_status="completed",
        output_file_id="file_out_1",
        response_body=None,
        status_code=200,
    ):
        self.batch_status = batch_status
        self.output_file_id = output_file_id
        self.response_body = response_body or {"id": "resp_1", "model": "gpt-5-mini", "output": []}
        self.status_code = status_code
        self.uploaded_custom_id = None
        self.calls = {
            "files_create": [],
            "batches_create": [],
            "batches_retrieve": [],
            "files_content": [],
            "responses_create": [],
        }
        self.files = self._Files(self)
        self.batches = self._Batches(self)
        self.responses = self._Responses(self)

    class _Files:
        def __init__(self, outer):
            self.outer = outer

        def create(self, *, file, purpose):
            self.outer.calls["files_create"].append({"file": file, "purpose": purpose})
            _name, content, _content_type = file
            line = content.decode("utf-8").strip()
            self.outer.uploaded_custom_id = json.loads(line)["custom_id"]
            return {"id": "file_in_1"}

        def content(self, file_id):
            self.outer.calls["files_content"].append(file_id)
            body = {
                "custom_id": self.outer.uploaded_custom_id,
                "response": {"status_code": self.outer.status_code, "body": self.outer.response_body},
            }
            return type("Content", (), {"text": f"{json.dumps(body)}\n"})()

    class _Batches:
        def __init__(self, outer):
            self.outer = outer

        def create(self, **params):
            self.outer.calls["batches_create"].append(params)
            return {"id": "batch_1", "status": "validating"}

        def retrieve(self, batch_id):
            self.outer.calls["batches_retrieve"].append(batch_id)
            return {"id": batch_id, "status": self.outer.batch_status, "output_file_id": self.outer.output_file_id}

    class _Responses:
        def __init__(self, outer):
            self.outer = outer

        def create(self, **request):
            self.outer.calls["responses_create"].append(request)
            return {"id": "resp_direct_1", "model": request.get("model"), "output": []}


def test_openai_submit_one_uploads_one_jsonl_line_then_creates_the_batch():
    client = FakeOpenAIClient()
    adapter = create_openai_batch_adapter(client)

    handle = adapter.submit_one(OPENAI_REQUEST)

    assert len(client.calls["files_create"]) == 1
    assert client.calls["files_create"][0]["purpose"] == "batch"
    _name, content, content_type = client.calls["files_create"][0]["file"]
    assert content_type == "application/jsonl"
    lines = [line for line in content.decode("utf-8").split("\n") if line.strip()]
    assert len(lines) == 1
    line = json.loads(lines[0])
    assert line["method"] == "POST"
    assert line["url"] == "/v1/responses"
    assert line["body"] == OPENAI_REQUEST
    assert isinstance(line["custom_id"], str) and line["custom_id"]

    assert len(client.calls["batches_create"]) == 1
    assert client.calls["batches_create"][0]["input_file_id"] == "file_in_1"
    assert client.calls["batches_create"][0]["endpoint"] == "/v1/responses"
    assert client.calls["batches_create"][0]["completion_window"] == "24h"
    assert handle.provider_batch_id == "batch_1"


OPENAI_POLL_CASES = [
    ("validating", "pending"),
    ("in_progress", "pending"),
    ("finalizing", "pending"),
    ("cancelling", "pending"),
    ("completed", "completed"),
    ("expired", "expired"),
    ("failed", "failed"),
    ("cancelled", "failed"),
    ("some_future_status_openai_might_add", "pending"),
]


@pytest.mark.parametrize("wire_status,normalized", OPENAI_POLL_CASES)
def test_openai_poll_normalizes_status(wire_status, normalized):
    client = FakeOpenAIClient(batch_status=wire_status)
    adapter = create_openai_batch_adapter(client)
    handle = adapter.submit_one(OPENAI_REQUEST)

    result = adapter.poll(handle)

    assert result.status == normalized


def test_openai_read_result_returns_matching_lines_response_body():
    response_body = {"id": "resp_1", "model": "gpt-5-mini", "output": []}
    client = FakeOpenAIClient(response_body=response_body)
    adapter = create_openai_batch_adapter(client)
    handle = adapter.submit_one(OPENAI_REQUEST)

    outcome = adapter.read_result(handle)

    assert len(client.calls["files_content"]) == 1
    assert client.calls["files_content"][0] == "file_out_1"
    assert outcome.result == response_body
    assert outcome.contained_tool_call_plan is False


def test_openai_read_result_reports_contained_tool_call_plan():
    response_body = {
        "id": "resp_1",
        "output": [{"type": "function_call", "name": "lookup", "call_id": "call_1", "arguments": "{}"}],
    }
    client = FakeOpenAIClient(response_body=response_body)
    adapter = create_openai_batch_adapter(client)
    handle = adapter.submit_one(OPENAI_REQUEST)

    outcome = adapter.read_result(handle)

    assert outcome.contained_tool_call_plan is True


def test_openai_read_result_never_exposes_raw_error_body():
    client = FakeOpenAIClient(
        status_code=400,
        response_body={"error": {"message": "some verbose provider error text that must never leak"}},
    )
    adapter = create_openai_batch_adapter(client)
    handle = adapter.submit_one(OPENAI_REQUEST)

    with pytest.raises(ProviderBatchError) as excinfo:
        adapter.read_result(handle)
    assert "must never leak" not in str(excinfo.value)


def test_openai_direct_calls_responses_create_with_exact_request():
    client = FakeOpenAIClient()
    adapter = create_openai_batch_adapter(client)

    outcome = adapter.direct(OPENAI_REQUEST)

    assert len(client.calls["responses_create"]) == 1
    assert client.calls["responses_create"][0] == OPENAI_REQUEST
    assert outcome.result["id"] == "resp_direct_1"
    assert outcome.contained_tool_call_plan is False


def test_openai_direct_reports_contained_tool_call_plan():
    client = FakeOpenAIClient()
    client.responses.create = lambda **request: {
        "id": "resp_direct_1",
        "output": [{"type": "function_call", "name": "lookup", "call_id": "call_1", "arguments": "{}"}],
    }
    adapter = create_openai_batch_adapter(client)

    outcome = adapter.direct(OPENAI_REQUEST)

    assert outcome.contained_tool_call_plan is True


def test_openai_eligibility_rejects_streaming():
    adapter = create_openai_batch_adapter(FakeOpenAIClient())
    result = adapter.eligibility({**OPENAI_REQUEST, "stream": True})
    assert result.eligible is False
    assert "stream" in result.reason


def test_openai_eligibility_accepts_plain_request():
    adapter = create_openai_batch_adapter(FakeOpenAIClient())
    assert adapter.eligibility(OPENAI_REQUEST).eligible is True


# ---------- Anthropic ----------

ANTHROPIC_REQUEST = {
    "model": "claude-opus-4-8",
    "max_tokens": 256,
    "messages": [{"role": "user", "content": "hello"}],
}


class FakeAnthropicClient:
    def __init__(
        self,
        *,
        processing_status="ended",
        request_counts=None,
        result_type="succeeded",
        message_body=None,
        error_body=None,
        omit_result_item=False,
    ):
        self.processing_status = processing_status
        self.request_counts = (
            request_counts
            if request_counts is not None
            else {"processing": 0, "succeeded": 1, "errored": 0, "canceled": 0, "expired": 0}
        )
        self.result_type = result_type
        self.message_body = message_body or {"id": "msg_1", "role": "assistant", "content": [{"type": "text", "text": "hi"}]}
        self.error_body = error_body or {
            "type": "invalid_request",
            "message": "some verbose provider error text that must never leak",
        }
        self.omit_result_item = omit_result_item
        self.submitted_custom_id = None
        self.calls = {"batches_create": [], "batches_retrieve": [], "batches_results": [], "messages_create": []}
        self.messages = self._Messages(self)

    class _Messages:
        def __init__(self, outer):
            self.outer = outer
            self.batches = FakeAnthropicClient._Batches(outer)

        def create(self, **request):
            self.outer.calls["messages_create"].append(request)
            return {"id": "msg_direct_1", "role": "assistant", "content": []}

    class _Batches:
        def __init__(self, outer):
            self.outer = outer

        def create(self, **params):
            self.outer.calls["batches_create"].append(params)
            self.outer.submitted_custom_id = params["requests"][0]["custom_id"]
            return {"id": "batch_1", "processing_status": "in_progress"}

        def retrieve(self, batch_id):
            self.outer.calls["batches_retrieve"].append(batch_id)
            return {
                "id": batch_id,
                "processing_status": self.outer.processing_status,
                "request_counts": self.outer.request_counts,
            }

        def results(self, batch_id):
            self.outer.calls["batches_results"].append(batch_id)
            if self.outer.omit_result_item:
                return iter(())
            if self.outer.result_type == "succeeded":
                item = {
                    "custom_id": self.outer.submitted_custom_id,
                    "result": {"type": "succeeded", "message": self.outer.message_body},
                }
            else:
                item = {
                    "custom_id": self.outer.submitted_custom_id,
                    "result": {"type": self.outer.result_type, "error": self.outer.error_body},
                }
            return iter([item])


def test_anthropic_submit_one_creates_batch_with_one_request():
    client = FakeAnthropicClient()
    adapter = create_anthropic_batch_adapter(client)

    handle = adapter.submit_one(ANTHROPIC_REQUEST)

    assert len(client.calls["batches_create"]) == 1
    requests = client.calls["batches_create"][0]["requests"]
    assert len(requests) == 1
    assert isinstance(requests[0]["custom_id"], str) and requests[0]["custom_id"]
    assert requests[0]["params"] == ANTHROPIC_REQUEST
    assert handle.provider_batch_id == "batch_1"


ANTHROPIC_POLL_CASES = [
    ("in_progress", {"succeeded": 0}, "pending"),
    ("canceling", {"succeeded": 0}, "pending"),
    ("ended", {"succeeded": 1, "errored": 0, "canceled": 0, "expired": 0}, "completed"),
    ("ended", {"succeeded": 0, "errored": 1, "canceled": 0, "expired": 0}, "failed"),
    ("ended", {"succeeded": 0, "errored": 0, "canceled": 1, "expired": 0}, "failed"),
    ("ended", {"succeeded": 0, "errored": 0, "canceled": 0, "expired": 1}, "expired"),
    ("ended", {}, "failed"),
    ("some_future_status_anthropic_might_add", {"succeeded": 1}, "pending"),
]


@pytest.mark.parametrize("wire_status,request_counts,normalized", ANTHROPIC_POLL_CASES)
def test_anthropic_poll_normalizes_status(wire_status, request_counts, normalized):
    client = FakeAnthropicClient(processing_status=wire_status, request_counts=request_counts)
    adapter = create_anthropic_batch_adapter(client)
    handle = adapter.submit_one(ANTHROPIC_REQUEST)

    result = adapter.poll(handle)

    assert result.status == normalized


def test_anthropic_read_result_returns_matching_items_message():
    message_body = {"id": "msg_1", "role": "assistant", "content": [{"type": "text", "text": "hi"}]}
    client = FakeAnthropicClient(message_body=message_body)
    adapter = create_anthropic_batch_adapter(client)
    handle = adapter.submit_one(ANTHROPIC_REQUEST)

    outcome = adapter.read_result(handle)

    assert client.calls["batches_results"] == ["batch_1"]
    assert outcome.result == message_body
    assert outcome.contained_tool_call_plan is False


def test_anthropic_read_result_reports_contained_tool_call_plan():
    message_body = {
        "id": "msg_1",
        "role": "assistant",
        "content": [{"type": "tool_use", "id": "toolu_1", "name": "lookup", "input": {"q": "x"}}],
    }
    client = FakeAnthropicClient(message_body=message_body)
    adapter = create_anthropic_batch_adapter(client)
    handle = adapter.submit_one(ANTHROPIC_REQUEST)

    outcome = adapter.read_result(handle)

    assert outcome.contained_tool_call_plan is True


def test_anthropic_read_result_never_exposes_raw_error_body():
    client = FakeAnthropicClient(result_type="errored")
    adapter = create_anthropic_batch_adapter(client)
    handle = adapter.submit_one(ANTHROPIC_REQUEST)

    with pytest.raises(ProviderBatchError) as excinfo:
        adapter.read_result(handle)
    assert "must never leak" not in str(excinfo.value)


def test_anthropic_read_result_raises_when_no_item_matches_custom_id():
    client = FakeAnthropicClient(omit_result_item=True)
    adapter = create_anthropic_batch_adapter(client)
    handle = adapter.submit_one(ANTHROPIC_REQUEST)

    with pytest.raises(ProviderBatchError):
        adapter.read_result(handle)


def test_anthropic_direct_calls_messages_create_with_exact_request():
    client = FakeAnthropicClient()
    adapter = create_anthropic_batch_adapter(client)

    outcome = adapter.direct(ANTHROPIC_REQUEST)

    assert len(client.calls["messages_create"]) == 1
    assert client.calls["messages_create"][0] == ANTHROPIC_REQUEST
    assert outcome.result["id"] == "msg_direct_1"
    assert outcome.contained_tool_call_plan is False


def test_anthropic_direct_reports_contained_tool_call_plan():
    client = FakeAnthropicClient()
    client.messages.create = lambda **request: {
        "id": "msg_direct_1",
        "role": "assistant",
        "content": [{"type": "tool_use", "id": "toolu_1", "name": "lookup", "input": {}}],
    }
    adapter = create_anthropic_batch_adapter(client)

    outcome = adapter.direct(ANTHROPIC_REQUEST)

    assert outcome.contained_tool_call_plan is True


def test_anthropic_eligibility_rejects_streaming():
    adapter = create_anthropic_batch_adapter(FakeAnthropicClient())
    result = adapter.eligibility({**ANTHROPIC_REQUEST, "stream": True})
    assert result.eligible is False
    assert "stream" in result.reason


def test_anthropic_eligibility_accepts_plain_request():
    adapter = create_anthropic_batch_adapter(FakeAnthropicClient())
    assert adapter.eligibility(ANTHROPIC_REQUEST).eligible is True


# ---------- Google ----------

GOOGLE_REQUEST = {"model": "gemini-2.5-flash", "contents": [{"role": "user", "parts": [{"text": "hello"}]}]}


class FakeGoogleClient:
    def __init__(
        self,
        *,
        state="JOB_STATE_SUCCEEDED",
        inlined_responses=None,
        generate_content_response=None,
        omit_dest=False,
    ):
        self.state = state
        self.inlined_responses = (
            inlined_responses
            if inlined_responses is not None
            else [{"response": {"candidates": [{"content": {"parts": [{"text": "hi"}]}}]}}]
        )
        self.generate_content_response = generate_content_response or {
            "candidates": [{"content": {"parts": [{"text": "hi"}]}}]
        }
        self.omit_dest = omit_dest
        self.calls = {"batches_create": [], "batches_get": [], "generate_content": []}
        self.models = self._Models(self)
        self.batches = self._Batches(self)

    class _Models:
        def __init__(self, outer):
            self.outer = outer

        def generate_content(self, **request):
            self.outer.calls["generate_content"].append(request)
            return self.outer.generate_content_response

    class _Batches:
        def __init__(self, outer):
            self.outer = outer

        def create(self, **params):
            self.outer.calls["batches_create"].append(params)
            return {"name": "batches/batch_1", "state": "JOB_STATE_PENDING"}

        def get(self, *, name):
            self.outer.calls["batches_get"].append(name)
            job = {"name": name, "state": self.outer.state}
            if not self.outer.omit_dest:
                job["dest"] = {"inlined_responses": self.outer.inlined_responses}
            return job


def test_google_submit_one_puts_model_outer_and_omits_it_from_inline_request():
    client = FakeGoogleClient()
    adapter = create_google_batch_adapter(client)

    handle = adapter.submit_one(GOOGLE_REQUEST)

    assert len(client.calls["batches_create"]) == 1
    assert client.calls["batches_create"][0]["model"] == "gemini-2.5-flash"
    src = client.calls["batches_create"][0]["src"]
    assert len(src) == 1
    assert "model" not in src[0]
    assert src[0] == {"contents": GOOGLE_REQUEST["contents"]}
    assert handle.provider_batch_id == "batches/batch_1"


@pytest.mark.parametrize("invalid_model", [None, "", "   ", 42, {}])
def test_google_submit_one_rejects_non_blank_string_model(invalid_model):
    client = FakeGoogleClient()
    adapter = create_google_batch_adapter(client)

    with pytest.raises(ProviderBatchError):
        adapter.submit_one({**GOOGLE_REQUEST, "model": invalid_model})
    assert client.calls["batches_create"] == []


def test_google_submit_one_rejects_missing_model():
    client = FakeGoogleClient()
    adapter = create_google_batch_adapter(client)
    request = dict(GOOGLE_REQUEST)
    del request["model"]

    with pytest.raises(ProviderBatchError):
        adapter.submit_one(request)
    assert client.calls["batches_create"] == []


GOOGLE_POLL_CASES = [
    ("JOB_STATE_PENDING", "pending"),
    ("JOB_STATE_RUNNING", "pending"),
    ("JOB_STATE_QUEUED", "pending"),
    ("JOB_STATE_SUCCEEDED", "completed"),
    ("JOB_STATE_FAILED", "failed"),
    ("JOB_STATE_CANCELLED", "failed"),
    ("JOB_STATE_EXPIRED", "expired"),
    ("JOB_STATE_PARTIALLY_SUCCEEDED", "pending"),
    ("JOB_STATE_SOME_FUTURE_STATE", "pending"),
]


@pytest.mark.parametrize("wire_state,normalized", GOOGLE_POLL_CASES)
def test_google_poll_normalizes_job_state(wire_state, normalized):
    client = FakeGoogleClient(state=wire_state)
    adapter = create_google_batch_adapter(client)
    handle = adapter.submit_one(GOOGLE_REQUEST)

    result = adapter.poll(handle)

    assert result.status == normalized


def test_google_read_result_returns_sole_inlined_response():
    response = {"candidates": [{"content": {"parts": [{"text": "hi"}]}}]}
    client = FakeGoogleClient(inlined_responses=[{"response": response}])
    adapter = create_google_batch_adapter(client)
    handle = adapter.submit_one(GOOGLE_REQUEST)

    outcome = adapter.read_result(handle)

    assert len(client.calls["batches_get"]) == 1
    assert outcome.result == response
    assert outcome.contained_tool_call_plan is False


def test_google_read_result_reports_contained_tool_call_plan():
    response = {"candidates": [{"content": {"parts": [{"function_call": {"name": "lookup", "args": {}}}]}}]}
    client = FakeGoogleClient(inlined_responses=[{"response": response}])
    adapter = create_google_batch_adapter(client)
    handle = adapter.submit_one(GOOGLE_REQUEST)

    outcome = adapter.read_result(handle)

    assert outcome.contained_tool_call_plan is True


def test_google_read_result_never_exposes_raw_error_body():
    client = FakeGoogleClient(
        inlined_responses=[{"error": {"code": 400, "message": "some verbose provider error text that must never leak"}}]
    )
    adapter = create_google_batch_adapter(client)
    handle = adapter.submit_one(GOOGLE_REQUEST)

    with pytest.raises(ProviderBatchError) as excinfo:
        adapter.read_result(handle)
    assert "must never leak" not in str(excinfo.value)


def test_google_read_result_raises_when_no_inlined_responses_at_all():
    client = FakeGoogleClient(omit_dest=True)
    adapter = create_google_batch_adapter(client)
    handle = adapter.submit_one(GOOGLE_REQUEST)

    with pytest.raises(ProviderBatchError):
        adapter.read_result(handle)


def test_google_direct_calls_generate_content_with_exact_request():
    client = FakeGoogleClient()
    adapter = create_google_batch_adapter(client)

    outcome = adapter.direct(GOOGLE_REQUEST)

    assert len(client.calls["generate_content"]) == 1
    assert client.calls["generate_content"][0] == GOOGLE_REQUEST
    assert outcome.contained_tool_call_plan is False


def test_google_direct_reports_contained_tool_call_plan():
    client = FakeGoogleClient(
        generate_content_response={
            "candidates": [{"content": {"parts": [{"function_call": {"name": "lookup", "args": {}}}]}}]
        }
    )
    adapter = create_google_batch_adapter(client)

    outcome = adapter.direct(GOOGLE_REQUEST)

    assert outcome.contained_tool_call_plan is True


def test_google_eligibility_rejects_streaming():
    adapter = create_google_batch_adapter(FakeGoogleClient())
    result = adapter.eligibility({**GOOGLE_REQUEST, "stream": True})
    assert result.eligible is False
    assert "stream" in result.reason


def test_google_eligibility_accepts_plain_request():
    adapter = create_google_batch_adapter(FakeGoogleClient())
    assert adapter.eligibility(GOOGLE_REQUEST).eligible is True
