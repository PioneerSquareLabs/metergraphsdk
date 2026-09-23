"""The canonical tool-declaration view and the Anthropic tool-only content rule.

Two kinds of test live here. The target tests assert the new behaviour and fail
against the SDK as it was before this change, because the declaration view did
not exist and a tool-only Anthropic reply recorded its provider block list as
`content`. The regression controls assert behaviour this change must not move,
so they pass both before and after; they are grouped under their own heading.

The provider seams themselves (sync, async and both streaming shapes) are
exercised against the real Anthropic client in
`tests/integrations/providers/test_real_client_integration.py`.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from metergraph import _capture, _tool_definitions
from metergraph._capture import Options, Runtime


class Rows:
    def __init__(self):
        self.rows = []

    def enqueue(self, row):
        self.rows.append(row)
        return True


class Model:
    """A provider model object: attribute access plus a structured dump."""

    def __init__(self, **fields):
        for key, value in fields.items():
            setattr(self, key, value)

    def model_dump(self, mode=None, exclude_none=False):
        dumped = {}
        for key, value in self.__dict__.items():
            if exclude_none and value is None:
                continue
            dumped[key] = (
                value.model_dump(mode=mode, exclude_none=exclude_none)
                if isinstance(value, Model)
                else value
            )
        return dumped


def runtime(tmp_path, **options):
    rows = Rows()
    return rows, Runtime(rows, Options(app_root=str(tmp_path), capture_text=True, **options))


def capture(tmp_path, request, response=None, *, provider="anthropic",
            endpoint="messages", **finish):
    rows, run = runtime(tmp_path)
    state = run.call_state(provider, endpoint, request)
    state.finish(response, **finish)
    return rows.rows[0]


def declarations(row):
    return row["tool_definitions"]["declarations"]


# --- declaration view: dialects -------------------------------------------

ANTHROPIC_SCHEMA = {
    "type": "object",
    "properties": {
        "query": {"type": "string", "description": "what to rank"},
        "limit": {"type": "integer", "default": 5},
        "mode": {"type": "string", "enum": ["fast", "thorough"]},
        "filters": {"type": "array", "items": {"$ref": "#/$defs/filter"}},
    },
    "required": ["query"],
    "$defs": {"filter": {"type": "object", "properties": {"field": {"type": "string"}}}},
}


def test_anthropic_declaration_is_recorded_verbatim(tmp_path):
    request = {
        "model": "claude-test",
        "tools": [
            {"name": "rank", "description": "Rank experts.", "input_schema": ANTHROPIC_SCHEMA}
        ],
    }
    row = capture(tmp_path, request, Model(content=[], stop_reason="end_turn"))

    envelope = row["tool_definitions"]
    assert envelope["version"] == 1
    assert envelope["fidelity"] == "verbatim"
    assert envelope["scope"] == "effective"
    [record] = envelope["declarations"]
    assert record == {
        "index": 0,
        "container": "tools",
        "kind": "function",
        "dialect": "anthropic",
        "name": "rank",
        "description": "Rank experts.",
        "schema_key": "input_schema",
        "schema": ANTHROPIC_SCHEMA,
        "status": "declared",
    }
    # Nested structure survives exactly, including defaults, enums and $ref.
    assert record["schema"]["properties"]["limit"]["default"] == 5
    assert record["schema"]["properties"]["mode"]["enum"] == ["fast", "thorough"]
    assert record["schema"]["$defs"]["filter"]["properties"]["field"]["type"] == "string"


def test_openai_chat_and_responses_dialects(tmp_path):
    chat = capture(
        tmp_path,
        {
            "model": "gpt-test",
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "lookup",
                        "description": "Look one up.",
                        "parameters": {"type": "object", "properties": {}},
                    },
                }
            ],
        },
        SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="hi"))]),
        provider="openai",
        endpoint="chat.completions",
    )
    [record] = declarations(chat)
    assert (record["dialect"], record["schema_key"], record["status"]) == (
        "openai_chat", "parameters", "declared",
    )

    responses = capture(
        tmp_path,
        {
            "model": "gpt-test",
            "tools": [
                {"type": "function", "name": "flat", "parameters": {"type": "object"}}
            ],
        },
        SimpleNamespace(output_text="hi"),
        provider="openai",
        endpoint="responses",
    )
    [record] = declarations(responses)
    assert (record["dialect"], record["schema_key"], record["status"]) == (
        "openai_responses", "parameters", "declared",
    )


def test_gemini_declarations_in_config_are_read(tmp_path):
    """The existing e2e scenario passes tools inside `config`, where the old
    name-only reader saw nothing at all."""
    row = capture(
        tmp_path,
        {
            "model": "gemini-test",
            "config": {
                "tools": [
                    {
                        "function_declarations": [
                            {"name": "list_directory", "parameters": {"type": "object"}},
                            {"name": "read_file", "parameters": {"type": "object"}},
                        ]
                    }
                ]
            },
        },
        SimpleNamespace(candidates=[]),
        provider="google",
        endpoint="models.generate_content",
    )
    records = declarations(row)
    assert [record["name"] for record in records] == ["list_directory", "read_file"]
    assert [record["index"] for record in records] == [0, 1]
    assert {record["container"] for record in records} == {"config.tools"}
    assert row["tool_definitions"]["scope"] == "effective"


def test_gemini_json_schema_key_is_supported(tmp_path):
    row = capture(
        tmp_path,
        {
            "model": "gemini-test",
            "tools": [
                {
                    "function_declarations": [
                        {
                            "name": "search",
                            "parameters_json_schema": {"type": "object", "title": "Search"},
                        }
                    ]
                }
            ],
        },
        SimpleNamespace(candidates=[]),
        provider="google",
        endpoint="models.generate_content",
    )
    [record] = declarations(row)
    assert record["schema_key"] == "parameters_json_schema"
    assert record["status"] == "declared"
    assert record["schema"]["title"] == "Search"


def test_ai_sdk_input_schema_dialect(tmp_path):
    row = capture(
        tmp_path,
        {
            "model": "anthropic/claude",
            "tools": [
                {"type": "function", "name": "convert", "inputSchema": {"type": "object"}}
            ],
        },
        SimpleNamespace(content=[]),
        provider="anthropic",
        endpoint="ai.doGenerate",
    )
    [record] = declarations(row)
    assert (record["dialect"], record["schema_key"]) == ("ai_sdk", "inputSchema")


# --- declaration view: statuses and ordering ------------------------------

def test_every_declaration_status_is_reported(tmp_path):
    request = {
        "model": "claude-test",
        "tools": [
            {"name": "good", "input_schema": {"type": "object"}},
            {"name": "web_search", "type": "web_search_20250305"},
            {"name": "good", "input_schema": {"type": "object", "title": "second"}},
            {"type": "function", "parameters": {"type": "object"}},
            {"name": "bad", "input_schema": "not-a-schema"},
            {"name": 17, "input_schema": {"type": "object"}},
            "not-a-mapping",
        ],
    }
    records = declarations(capture(tmp_path, request, Model(content=[], stop_reason="end_turn")))

    assert [record["status"] for record in records] == [
        "declared", "provider_tool", "declared", "incomplete", "malformed", "malformed",
        "unsupported",
    ]
    assert [record["index"] for record in records] == list(range(7))
    assert records[1]["provider_type"] == "web_search_20250305"
    assert records[1]["dialect"] == "anthropic"
    assert records[2]["duplicate_of"] == 0
    assert records[2]["schema"]["title"] == "second", "a duplicate keeps its own schema"
    assert records[4]["schema"] is None and records[4]["schema_key"] is None
    assert records[5]["name"] is None


def test_unequal_schema_sources_are_ambiguous(tmp_path):
    row = capture(
        tmp_path,
        {
            "model": "gemini-test",
            "tools": [
                {
                    "function_declarations": [
                        {
                            "name": "search",
                            "parameters": {"type": "object", "title": "a"},
                            "parameters_json_schema": {"type": "object", "title": "b"},
                        }
                    ]
                }
            ],
        },
        SimpleNamespace(candidates=[]),
        provider="google",
        endpoint="models.generate_content",
    )
    [record] = declarations(row)
    assert record["status"] == "ambiguous"
    assert record["schema"] is None and record["schema_key"] is None


def test_equal_schema_sources_resolve_by_precedence(tmp_path):
    schema = {"type": "object"}
    row = capture(
        tmp_path,
        {
            "model": "gemini-test",
            "tools": [
                {
                    "function_declarations": [
                        {"name": "search", "parameters": schema, "parametersJsonSchema": schema}
                    ]
                }
            ],
        },
        SimpleNamespace(candidates=[]),
        provider="google",
        endpoint="models.generate_content",
    )
    [record] = declarations(row)
    assert record["status"] == "declared"
    assert record["schema_key"] == "parametersJsonSchema"


def test_competing_containers_are_inventory_not_effective(tmp_path):
    row = capture(
        tmp_path,
        {
            "model": "claude-test",
            "tools": [{"name": "top", "input_schema": {"type": "object"}}],
            "extra_body": {"tools": [{"name": "extra", "input_schema": {"type": "object"}}]},
        },
        Model(content=[], stop_reason="end_turn"),
    )
    assert row["tool_definitions"]["scope"] == "inventory"
    assert [record["container"] for record in declarations(row)] == [
        "tools", "extra_body.tools",
    ]


def test_non_list_container_is_reported_not_silently_dropped(tmp_path):
    row = capture(
        tmp_path, {"model": "claude-test", "tools": "auto"},
        Model(content=[], stop_reason="end_turn"),
    )
    [record] = declarations(row)
    assert (record["status"], record["kind"], record["name"]) == ("unsupported", "unknown", None)


def test_absent_and_empty_tools_omit_the_field(tmp_path):
    absent = capture(tmp_path, {"model": "claude-test"}, Model(content=[], stop_reason="end_turn"))
    empty = capture(
        tmp_path, {"model": "claude-test", "tools": []},
        Model(content=[], stop_reason="end_turn"),
    )
    assert "tool_definitions" not in absent
    assert "tool_definitions" not in empty


def test_provider_request_is_unchanged_by_the_addition(tmp_path):
    request = {
        "model": "claude-test",
        "tools": [{"name": "rank", "input_schema": ANTHROPIC_SCHEMA}],
    }
    row = capture(tmp_path, dict(request), Model(content=[], stop_reason="end_turn"))
    assert json.loads(row["request_json"])["tools"] == request["tools"]


def test_gemini_native_tool_is_a_provider_placeholder(tmp_path):
    row = capture(
        tmp_path,
        {"model": "gemini-test", "tools": [{"google_search": {}}]},
        SimpleNamespace(candidates=[]),
        provider="google",
        endpoint="models.generate_content",
    )
    [record] = declarations(row)
    assert (record["kind"], record["dialect"], record["status"], record["name"]) == (
        "provider", "gemini", "provider_tool", "google_search",
    )


# --- declaration view: content controls -----------------------------------

def test_capture_text_off_withholds_the_field(tmp_path):
    rows = Rows()
    run = Runtime(rows, Options(app_root=str(tmp_path), capture_text=False))
    state = run.call_state(
        "anthropic", "messages",
        {"model": "claude-test", "tools": [{"name": "rank", "input_schema": {"type": "object"}}]},
    )
    state.finish(Model(content=[], stop_reason="end_turn"))
    assert "tool_definitions" not in rows.rows[0]


def test_redaction_marks_the_view_filtered(tmp_path):
    def redact(value, kind):
        return value.replace("s3cret", "<redacted>")

    rows = Rows()
    run = Runtime(rows, Options(app_root=str(tmp_path), capture_text=True, redact=redact))
    state = run.call_state(
        "anthropic", "messages",
        {
            "model": "claude-test",
            "tools": [{"name": "rank", "description": "s3cret", "input_schema": {"type": "object"}}],
        },
    )
    state.finish(Model(content=[], stop_reason="end_turn"))
    envelope = rows.rows[0]["tool_definitions"]
    assert envelope["fidelity"] == "filtered"
    assert envelope["declarations"][0]["description"] == "<redacted>"
    assert "s3cret" not in json.dumps(envelope)


def test_redaction_returning_junk_omits_the_field_but_keeps_the_row(tmp_path):
    for broken in (lambda value, kind: "not json", lambda value, kind: '{"not": "an array"}',
                   lambda value, kind: '[{"index": "zero"}]'):
        rows = Rows()
        run = Runtime(rows, Options(app_root=str(tmp_path), capture_text=True, redact=broken))
        state = run.call_state(
            "anthropic", "messages",
            {"model": "claude-test",
             "tools": [{"name": "rank", "input_schema": {"type": "object"}}]},
        )
        state.finish(Model(content=[], stop_reason="end_turn"))
        assert "tool_definitions" not in rows.rows[0]
        assert rows.rows[0]["model"] == "claude-test", "the row still ships"


def _hostile_hook_row(tmp_path, mutate):
    """One captured row from a run whose redaction hook returns `mutate`'s output."""
    rows = Rows()
    run = Runtime(
        rows,
        Options(app_root=str(tmp_path), capture_text=True,
                redact=lambda value, kind: mutate(value)),
    )
    state = run.call_state(
        "anthropic", "messages",
        {"model": "claude-test",
         "tools": [{"name": "lookup", "input_schema": {"type": "object"}}]},
    )
    state.finish(Model(content=[], stop_reason="end_turn"))
    return rows.rows


def _with_record_key(key, value):
    def mutate(text):
        records = json.loads(text)
        records[0][key] = value
        return json.dumps(records)

    return mutate


def _deeply_nested(levels=300):
    root: dict = {}
    node = root
    for _ in range(levels):
        node["a"] = {}
        node = node["a"]
    return root


@pytest.mark.parametrize("mutate,reason", [
    (_with_record_key("kind", {}), "an unhashable enum value"),
    (_with_record_key("kind", []), "an unhashable list enum value"),
    (_with_record_key("status", {"a": 1}), "a dict where a status belongs"),
    (_with_record_key("container", ["tools"]), "a list where a container belongs"),
    (_with_record_key("schema_key", {}), "a dict where a schema key belongs"),
    (_with_record_key("dialect", 7), "a number where a dialect belongs"),
    (_with_record_key("index", "zero"), "a string index"),
    (_with_record_key("name", "\ud800"), "a lone surrogate in a name"),
    (_with_record_key("description", "a\x00b"), "a NUL in a description"),
    (_with_record_key("schema", {"title": "\udfff"}), "a surrogate inside a schema"),
    (_with_record_key("schema", {"threshold": float("nan")}), "a non-finite schema value"),
    (_with_record_key("schema", _deeply_nested()), "a schema nested past the depth bound"),
    (_with_record_key("surprise", "extra"), "an added key"),
    (lambda text: "not json at all", "output that is not JSON"),
    (lambda text: "17", "output that is not an array"),
    (lambda text: 17, "output that is not text"),
    (lambda text: (_ for _ in ()).throw(RuntimeError("boom")), "a hook that raises"),
])
def test_a_malformed_hook_result_costs_the_field_and_nothing_else(tmp_path, mutate, reason):
    """The hook is caller-supplied, so its output is untrusted input.

    A membership test against a closed set raises `TypeError` for an unhashable
    value, which once escaped `finish` and dropped the whole row. Every case
    here must leave the row intact and omit only the declaration view.
    """
    rows = _hostile_hook_row(tmp_path, mutate)

    assert len(rows) == 1, f"the row was lost for {reason}"
    assert "tool_definitions" not in rows[0], f"a malformed view survived {reason}"
    assert rows[0]["model"] == "claude-test"
    assert rows[0]["provider"] == "anthropic"


def test_a_hook_cannot_assert_its_own_fidelity(tmp_path):
    def forge(value, kind):
        records = json.loads(value)
        records[0]["description"] = "changed"
        return json.dumps(records)

    rows = Rows()
    run = Runtime(rows, Options(app_root=str(tmp_path), capture_text=True, redact=forge))
    state = run.call_state(
        "anthropic", "messages",
        {"model": "claude-test",
         "tools": [{"name": "rank", "description": "original", "input_schema": {"type": "object"}}]},
    )
    state.finish(Model(content=[], stop_reason="end_turn"))
    # The envelope is built outside the hook, so the change is reported.
    assert rows.rows[0]["tool_definitions"]["fidelity"] == "filtered"


def test_oversize_view_is_omitted_and_marks_the_row_truncated(tmp_path):
    rows = Rows()
    run = Runtime(rows, Options(app_root=str(tmp_path), capture_text=True, text_max_bytes=400))
    state = run.call_state(
        "anthropic", "messages",
        {
            "model": "claude-test",
            "tools": [
                {"name": f"tool_{index}", "input_schema": {"type": "object", "title": "x" * 40}}
                for index in range(20)
            ],
        },
    )
    state.finish(Model(content=[], stop_reason="end_turn"))
    assert "tool_definitions" not in rows.rows[0]
    assert rows.rows[0]["text_truncated"] is True


# --- the Anthropic tool-only content rule ---------------------------------

def tool_block(block_id="toolu_1", name="rank", arguments=None, caller="direct", **extra):
    fields = {"type": "tool_use", "id": block_id, "name": name,
              "input": {"a": 1} if arguments is None else arguments}
    if caller is not None:
        fields["caller"] = Model(type=caller)
    fields.update(extra)
    return Model(**fields)


def message(blocks, stop_reason="tool_use"):
    return Model(content=blocks, stop_reason=stop_reason, id="msg_1", model="claude-test")


TOOL_REQUEST = {
    "model": "claude-test",
    "tools": [{"name": "rank", "input_schema": {"type": "object"}}],
    "tool_choice": {"type": "tool", "name": "rank"},
}


def reply(tmp_path, response, **finish):
    row = capture(tmp_path, dict(TOOL_REQUEST), response, **finish)
    return json.loads(row["response_text"]), row


def test_tool_only_reply_records_explicit_null_content(tmp_path):
    envelope, row = reply(tmp_path, message([tool_block()]))

    assert "content" in envelope and envelope["content"] is None
    [call] = envelope["tool_calls"]
    assert (call["call_id"], call["name"], call["arguments"]) == ("toolu_1", "rank", {"a": 1})
    assert row["tool_names"] == ["rank"]


def test_envelope_keys_stay_inside_the_downstream_contract(tmp_path):
    """The pipeline normalizer refuses an envelope carrying an unknown key."""
    envelope, _ = reply(tmp_path, message([tool_block()]))
    allowed = {"content", "finish_reason", "model", "request_id", "role", "status", "tool_calls"}
    assert set(envelope) <= allowed


def test_block_without_caller_is_recognized(tmp_path):
    envelope, _ = reply(tmp_path, message([tool_block(caller=None)]))
    assert envelope["content"] is None


def test_caller_with_null_siblings_is_recognized(tmp_path):
    block = Model(type="tool_use", id="toolu_1", name="rank", input={"a": 1},
                  caller=Model(type="direct", detail=None))
    envelope, _ = reply(tmp_path, message([block]))
    assert envelope["content"] is None


@pytest.mark.parametrize("stop_reason", ["max_tokens", "end_turn", "stop_sequence", "refusal"])
def test_a_reply_stopped_for_another_reason_keeps_its_content(tmp_path, stop_reason):
    """A truncated tool call is not a finished turn, so it is left alone."""
    envelope, _ = reply(tmp_path, message([tool_block()], stop_reason=stop_reason))
    assert isinstance(envelope["content"], list)


def test_non_direct_caller_keeps_its_content(tmp_path):
    envelope, _ = reply(tmp_path, message([tool_block(caller="skill")]))
    assert isinstance(envelope["content"], list)


def test_unexpected_block_key_keeps_its_content(tmp_path):
    envelope, _ = reply(tmp_path, message([tool_block(cache_control={"type": "ephemeral"})]))
    assert isinstance(envelope["content"], list)


def test_duplicate_or_missing_block_ids_keep_their_content(tmp_path):
    duplicated, _ = reply(tmp_path, message([tool_block(), tool_block()]))
    assert isinstance(duplicated["content"], list)
    missing, _ = reply(tmp_path, message([tool_block(block_id="")]))
    assert isinstance(missing["content"], list)


def test_a_block_with_no_matching_event_keeps_its_content(tmp_path):
    """The second block is dropped from the event list, so the reply is not
    fully represented and the conversion is abandoned."""
    rows, run = runtime(tmp_path)
    state = run.call_state("anthropic", "messages", dict(TOOL_REQUEST))
    original = _capture._tool_events

    def one_event_only(request, response, chunks=None):
        events = original(request, response, chunks)
        return events[:1] if events else events

    _capture._tool_events = one_event_only
    try:
        state.finish(message([tool_block(), tool_block(block_id="toolu_2")]))
    finally:
        _capture._tool_events = original
    assert isinstance(json.loads(rows.rows[0]["response_text"])["content"], list)


def test_history_events_alone_do_not_trigger_the_conversion(tmp_path):
    """Events from the request history cover a different id than the reply's."""
    request = dict(TOOL_REQUEST)
    request["messages"] = [
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "toolu_history", "name": "rank", "input": {"a": 0}}]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "toolu_history", "content": "done"}]},
    ]
    row = capture(tmp_path, request, message([tool_block(block_id="toolu_new")]))
    envelope = json.loads(row["response_text"])
    # The current block matches its own event, so the reply still converts, and
    # the history event is retained beside it.
    assert envelope["content"] is None
    assert {call["call_id"] for call in envelope["tool_calls"]} == {"toolu_history", "toolu_new"}


def test_mixed_text_and_thinking_replies_are_untouched(tmp_path):
    mixed, _ = reply(tmp_path, message([Model(type="text", text="thinking aloud"), tool_block()]))
    assert mixed["content"] == "thinking aloud"

    thinking, _ = reply(tmp_path, message(
        [Model(type="thinking", thinking="...", signature="sig"), tool_block()]))
    assert isinstance(thinking["content"], list)


@pytest.mark.parametrize("block_type", [
    "server_tool_use", "web_search_tool_result", "code_execution_tool_result",
    "mcp_tool_use", "mcp_tool_result", "container_upload", "something_new",
])
def test_other_block_types_are_untouched(tmp_path, block_type):
    envelope, _ = reply(tmp_path, message([Model(type=block_type, id="b1", name="x", input={})]))
    assert isinstance(envelope["content"], list)


def test_errors_and_abandoned_streams_are_untouched(tmp_path):
    errored, _ = reply(tmp_path, message([tool_block()]), error=RuntimeError("boom"))
    assert isinstance(errored["content"], list)
    abandoned, _ = reply(tmp_path, message([tool_block()]), status="abandoned", stream=True)
    assert isinstance(abandoned["content"], list)


def test_other_providers_and_endpoints_are_untouched(tmp_path):
    bedrock = capture(
        tmp_path, dict(TOOL_REQUEST), message([tool_block()]),
        provider="bedrock", endpoint="messages",
    )
    assert isinstance(json.loads(bedrock["response_text"])["content"], list)

    gateway = capture(
        tmp_path, dict(TOOL_REQUEST), message([tool_block()]),
        provider="anthropic", endpoint="chat.completions",
    )
    assert isinstance(json.loads(gateway["response_text"])["content"], list)


def test_openai_chat_tool_call_reply_keeps_its_null_content(tmp_path):
    row = capture(
        tmp_path,
        {"model": "gpt-test", "tools": [{"type": "function", "function": {
            "name": "rank", "parameters": {"type": "object"}}}]},
        SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
            content=None,
            tool_calls=[SimpleNamespace(
                id="call_1", function=SimpleNamespace(name="rank", arguments='{"a":1}'))],
        ), finish_reason="tool_calls")]),
        provider="openai",
        endpoint="chat.completions",
    )
    envelope = json.loads(row["response_text"])
    assert "content" not in envelope, "unchanged from the pre-existing behaviour"
    assert envelope["tool_calls"][0]["call_id"] == "call_1"


def test_openai_responses_function_call_reply_still_reads_output(tmp_path):
    row = capture(
        tmp_path,
        {"model": "gpt-test", "tools": [
            {"type": "function", "name": "rank", "parameters": {"type": "object"}}]},
        SimpleNamespace(
            output_text="",
            text=SimpleNamespace(format="json"),
            output=[SimpleNamespace(type="function_call", call_id="call_1", name="rank",
                                    arguments='{"a":1}')],
        ),
        provider="openai",
        endpoint="responses",
    )
    envelope = json.loads(row["response_text"])
    assert envelope["content"] != ""
    assert envelope["tool_calls"][0]["call_id"] == "call_1"


# --- rule B: a completed raw event stream ---------------------------------

def stream_chunks(*, message_stop=True, block_stop=True, stop_reason="tool_use",
                  partial='{"a":1}', extra=()):
    chunks = [
        Model(type="message_start"),
        # A real stream opens the block with an empty input; the arguments
        # arrive as input_json_delta chunks.
        Model(type="content_block_start", index=0,
              content_block=tool_block(block_id="toolu_s", arguments={})),
        Model(type="content_block_delta", index=0,
              delta=Model(type="input_json_delta", partial_json=partial)),
    ]
    chunks.extend(extra)
    if block_stop:
        chunks.append(Model(type="content_block_stop", index=0))
    if stop_reason:
        chunks.append(Model(type="message_delta", delta=Model(stop_reason=stop_reason)))
    if message_stop:
        chunks.append(Model(type="message_stop"))
    return chunks


def raw_stream(tmp_path, chunks, **finish):
    row = capture(
        tmp_path, dict(TOOL_REQUEST), chunks[-1],
        endpoint="messages", stream=True, stream_chunks=chunks, **finish,
    )
    return json.loads(row["response_text"])


def test_completed_raw_stream_records_explicit_null(tmp_path):
    envelope = raw_stream(tmp_path, stream_chunks())
    assert "content" in envelope and envelope["content"] is None
    [call] = envelope["tool_calls"]
    assert (call["call_id"], call["name"], call["arguments"]) == ("toolu_s", "rank", {"a": 1})


@pytest.mark.parametrize("chunks,reason", [
    (stream_chunks(message_stop=False), "no message_stop"),
    (stream_chunks(block_stop=False), "no content_block_stop"),
    (stream_chunks(stop_reason="max_tokens"), "truncated generation"),
    (stream_chunks(partial='{"a":'), "unparseable arguments"),
    (stream_chunks(extra=[Model(type="content_block_delta", index=1,
                                delta=Model(type="text_delta", text="hi"))]), "text delta"),
])
def test_incomplete_streams_keep_their_content(tmp_path, chunks, reason):
    envelope = raw_stream(tmp_path, chunks)
    assert envelope.get("content") != None or "content" not in envelope, reason
    assert not ("content" in envelope and envelope["content"] is None), reason


def test_abandoned_stream_keeps_its_content(tmp_path):
    envelope = raw_stream(tmp_path, stream_chunks(), status="abandoned")
    assert not ("content" in envelope and envelope["content"] is None)


def test_arguments_that_differ_only_by_type_keep_their_content(tmp_path):
    """`True` and `1` are different JSON values, so the block and its event
    disagree and the conversion is abandoned."""
    rows, run = runtime(tmp_path)
    state = run.call_state("anthropic", "messages", dict(TOOL_REQUEST))
    original = _capture._tool_events

    def retyped(request, response, chunks=None):
        events = original(request, response, chunks)
        return [dict(event, arguments={"a": True}) for event in events]

    _capture._tool_events = retyped
    try:
        state.finish(message([tool_block(arguments={"a": 1})]))
    finally:
        _capture._tool_events = original
    assert isinstance(json.loads(rows.rows[0]["response_text"])["content"], list)


def test_competing_schema_sources_distinguish_booleans_from_numbers(tmp_path):
    row = capture(
        tmp_path,
        {
            "model": "gemini-test",
            "tools": [{"function_declarations": [{
                "name": "search",
                "parameters": {"default": 1},
                "parameters_json_schema": {"default": True},
            }]}],
        },
        SimpleNamespace(candidates=[]),
        provider="google",
        endpoint="models.generate_content",
    )
    assert declarations(row)[0]["status"] == "ambiguous"


def test_reordered_object_keys_are_equal_but_reordered_arrays_are_not(tmp_path):
    reordered = capture(
        tmp_path,
        {"model": "gemini-test", "tools": [{"function_declarations": [{
            "name": "search",
            "parameters": {"a": 1, "b": 2},
            "parameters_json_schema": {"b": 2, "a": 1},
        }]}]},
        SimpleNamespace(candidates=[]),
        provider="google",
        endpoint="models.generate_content",
    )
    assert declarations(reordered)[0]["status"] == "declared"

    arrays = capture(
        tmp_path,
        {"model": "gemini-test", "tools": [{"function_declarations": [{
            "name": "search",
            "parameters": {"x": [1, 2]},
            "parameters_json_schema": {"x": [2, 1]},
        }]}]},
        SimpleNamespace(candidates=[]),
        provider="google",
        endpoint="models.generate_content",
    )
    assert declarations(arrays)[0]["status"] == "ambiguous"


def test_a_non_finite_schema_value_is_unsupported_not_verbatim(tmp_path):
    """NaN is not JSON, so the record must not claim to carry the schema."""
    row = capture(
        tmp_path,
        {"model": "claude-test", "tools": [
            {"name": "rank", "input_schema": {"threshold": float("nan")}}]},
        Model(content=[], stop_reason="end_turn"),
    )
    [record] = declarations(row)
    assert record["status"] == "unsupported"
    assert record["schema"] is None


# --- module-level contract ------------------------------------------------

def test_valid_declarations_rejects_smuggled_payloads():
    records = _tool_definitions.declarations(
        {"tools": [{"name": "rank", "input_schema": {"type": "object"}}]}
    )[0]
    assert _tool_definitions.valid_declarations(records)

    for mutation in (
        {"name": {"nested": "object"}},
        {"index": "zero"},
        {"status": "invented"},
        {"container": "elsewhere"},
        {"schema": ["not", "an", "object"]},
        {"name": "x" * 600},
        {"surprise": "extra key"},
    ):
        broken = [dict(records[0], **mutation)]
        assert not _tool_definitions.valid_declarations(broken), mutation
