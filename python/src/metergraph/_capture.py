"""Provider-client wrapping and normalized record construction."""

from __future__ import annotations

import functools
import inspect
import json
import logging
import math
import os
import platform
import secrets
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit

from ._context import CaptureContext, snapshot
from ._template import scrub, template_hash
from ._version import SDK_VERSION


log = logging.getLogger("metergraph")
DEFAULT_TEXT_MAX_BYTES = 1024 * 1024


def _get(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _first(value: Any) -> Any:
    try:
        return value[0]
    except (IndexError, KeyError, TypeError):
        return None


def _int(value: Any) -> int | None:
    try:
        if value is None or isinstance(value, bool):
            return None
        if isinstance(value, int):
            return value if value >= 0 else None
        parsed = float(value)
        return (
            int(parsed)
            if math.isfinite(parsed) and parsed >= 0 and parsed.is_integer()
            else None
        )
    except (TypeError, ValueError, OverflowError):
        return None


def _usage(response: Any) -> dict[str, int | None]:
    usage = (
        _get(response, "usage")
        or _get(response, "usage_metadata")
        or _get(response, "usageMetadata")
    )
    prompt_details = _get(usage, "prompt_tokens_details") or _get(
        usage, "input_tokens_details"
    ) or _get(
        usage, "promptTokensDetails"
    ) or _get(
        usage, "inputTokensDetails"
    )
    completion_details = _get(usage, "completion_tokens_details") or _get(
        usage, "output_tokens_details"
    ) or _get(
        usage, "completionTokensDetails"
    ) or _get(
        usage, "outputTokensDetails"
    )
    cache_creation = _get(usage, "cache_creation") or _get(
        usage, "cacheCreation"
    )
    return {
        "input_tokens": _int(
            _get(
                usage,
                "prompt_tokens",
                _get(
                    usage,
                    "input_tokens",
                    _get(
                        usage,
                        "prompt_token_count",
                        _get(usage, "promptTokenCount"),
                    ),
                ),
            )
        ),
        "output_tokens": _int(
            _get(
                usage,
                "completion_tokens",
                _get(
                    usage,
                    "output_tokens",
                    _get(
                        usage,
                        "candidates_token_count",
                        _get(usage, "candidatesTokenCount"),
                    ),
                ),
            )
        ),
        "cache_read_tokens": _int(
            _get(
                usage,
                "cache_read_input_tokens",
                _get(
                    prompt_details,
                    "cached_tokens",
                    _get(
                        usage,
                        "cached_content_token_count",
                        _get(usage, "cachedContentTokenCount"),
                    ),
                ),
            )
        ),
        "cache_write_tokens": _int(
            _get(
                usage,
                "cache_creation_input_tokens",
                _get(
                    prompt_details,
                    "cache_write_tokens",
                    _get(
                        usage,
                        "cacheCreationInputTokens",
                        _get(prompt_details, "cacheWriteTokens"),
                    ),
                ),
            )
        ),
        "cache_write_5m_tokens": _int(
            _get(
                cache_creation,
                "ephemeral_5m_input_tokens",
                _get(cache_creation, "ephemeral5mInputTokens"),
            )
        ),
        "cache_write_1h_tokens": _int(
            _get(
                cache_creation,
                "ephemeral_1h_input_tokens",
                _get(cache_creation, "ephemeral1hInputTokens"),
            )
        ),
        "reasoning_tokens": _int(
            _get(
                completion_details,
                "reasoning_tokens",
                _get(
                    usage,
                    "thoughts_token_count",
                    _get(usage, "thoughtsTokenCount"),
                ),
            )
        ),
    }


def _response_text(response: Any) -> str | None:
    direct = _get(response, "output_text") or _get(response, "text")
    if isinstance(direct, str):
        return direct
    choice = _first(_get(response, "choices"))
    message = _get(choice, "message")
    content = _get(message, "content")
    if isinstance(content, str):
        return content
    blocks = _get(response, "content")
    if isinstance(blocks, list):
        texts = [_get(block, "text") for block in blocks]
        joined = "".join(text for text in texts if isinstance(text, str))
        if joined:
            return joined
    outputs = _get(response, "output")
    if isinstance(outputs, list):
        texts = []
        for output in outputs:
            content = _get(output, "content")
            if not isinstance(content, list):
                continue
            for block in content:
                text = _get(block, "text") or _get(block, "output_text")
                if isinstance(text, str):
                    texts.append(text)
        return "".join(texts) or None
    return None


def _chunk_text(chunk: Any) -> str | None:
    choice = _first(_get(chunk, "choices"))
    delta = _get(choice, "delta")
    content = _get(delta, "content")
    if isinstance(content, str):
        return content
    delta = _get(chunk, "delta")
    text = _get(delta, "text") or _get(chunk, "text")
    if isinstance(text, str):
        return text
    if _get(chunk, "type") == "content_block_delta":
        text = _get(_get(chunk, "delta"), "text")
        return text if isinstance(text, str) else None
    return None


def _chunk_has_output(chunk: Any) -> bool:
    """Recognize the first user-visible text, reasoning, or tool output."""
    if _chunk_text(chunk):
        return True
    for choice in _get(chunk, "choices", []) or []:
        if _get(_get(choice, "delta"), "tool_calls"):
            return True
    kind = _get(chunk, "type")
    delta = _get(chunk, "delta")
    if isinstance(delta, str) and "reasoning" in str(kind):
        return bool(delta)
    if _get(delta, "thinking") or _get(delta, "reasoning"):
        return True
    if kind == "content_block_start":
        return _get(_get(chunk, "content_block"), "type") == "tool_use"
    if kind == "content_block_delta":
        return _get(_get(chunk, "delta"), "type") == "input_json_delta"
    for candidate in _get(chunk, "candidates", []) or []:
        for part in _get(_get(candidate, "content"), "parts", []) or []:
            if _get(part, "function_call") or _get(part, "functionCall"):
                return True
    return False


def _usage_only_chunk(chunk: Any, call: "CallState") -> bool:
    return (
        call.endpoint == "chat.completions"
        and _get(chunk, "choices") == []
        and _get(chunk, "usage") is not None
    )


def _stop_reason(response: Any) -> str | None:
    direct = _get(response, "stop_reason") or _get(response, "status")
    if direct:
        return str(direct)
    choice = _first(_get(response, "choices"))
    reason = _get(choice, "finish_reason")
    return str(reason) if reason is not None else None


def _normalize_finish_reason(value: str) -> str:
    normalized = "-".join(value.strip().lower().replace("_", "-").split())
    if normalized in {"stop", "end-turn", "stop-sequence", "completed", "succeeded"}:
        return "stop"
    if normalized in {"length", "max-tokens", "max-output-tokens"}:
        return "length"
    if normalized in {"content-filter", "safety", "blocked"}:
        return "content-filter"
    if normalized in {"tool-calls", "tool-use", "function-call"}:
        return "tool-calls"
    if normalized in {"error", "failed"}:
        return "error"
    if normalized in {"other", "unknown"}:
        return "other"
    return normalized


def _finish_reason_details(response: Any) -> tuple[str | None, str | None]:
    response_status = _get(response, "status")
    incomplete_reason = (
        _get(_get(response, "incomplete_details"), "reason")
        if response_status == "incomplete"
        else None
    )
    value = (
        _get(response, "stop_reason")
        or incomplete_reason
        or response_status
        or _get(response, "finishReason")
        or _get(response, "finish_reason")
        or _get(_first(_get(response, "choices")), "finish_reason")
    )
    unified = _get(value, "unified")
    raw_value = _get(value, "raw") or (value if unified is None else None)
    source = unified if unified is not None else raw_value
    if source is None:
        return None, None
    finish_reason = _normalize_finish_reason(str(source))
    raw = str(raw_value) if raw_value is not None else None
    return finish_reason, raw if raw != finish_reason else None


def _request_id(response: Any) -> str | None:
    value = (
        _get(response, "_request_id")
        or _get(response, "response_id")
        or _get(response, "responseId")
        or _get(response, "id")
    )
    return str(value) if value is not None else None


def _response_content(response: Any, aggregate_text: str | None = None) -> Any:
    if aggregate_text is not None:
        return aggregate_text
    direct = _get(response, "output_text") or _get(response, "text")
    if direct is not None:
        return scrub(direct)
    choice = _first(_get(response, "choices"))
    message = _get(choice, "message")
    content = _get(message, "content")
    if content is not None:
        return scrub(content)
    parsed = _get(message, "parsed")
    if parsed is not None:
        return scrub(parsed)
    normalized_text = _response_text(response)
    if normalized_text is not None:
        return normalized_text
    blocks = _get(response, "content")
    if blocks is not None:
        return scrub(blocks)
    outputs = _get(response, "output")
    if outputs is not None:
        return scrub(outputs)
    candidates = _get(response, "candidates")
    if candidates is not None:
        return scrub(candidates)
    return None


def _response_envelope(
    response: Any,
    *,
    aggregate_text: str | None,
    tool_calls: list[dict] | None,
    error: BaseException | None,
    status: str,
) -> dict[str, Any]:
    envelope: dict[str, Any] = {
        "role": "assistant",
        "content": _response_content(response, aggregate_text),
        "tool_calls": tool_calls or [],
        "finish_reason": _stop_reason(response),
        "request_id": _request_id(response),
        "model": _get(response, "model")
        or _get(response, "model_version")
        or _get(response, "modelVersion"),
        "status": status,
    }
    if error is not None:
        envelope["error"] = {
            "type": type(error).__name__,
            "message": str(error),
        }
    return {key: value for key, value in envelope.items() if value is not None}


def _tool_names(request: Mapping[str, Any]) -> list[dict[str, str]] | None:
    tools = request.get("tools")
    if not isinstance(tools, list):
        return None
    names: list[dict[str, str]] = []
    for tool in tools:
        fn = _get(tool, "function")
        name = _get(fn, "name") or _get(tool, "name")
        if name:
            names.append({"name": str(name)})
    return names or None


def _tool_argument(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return scrub(value)


def _tool_policies(request: Mapping[str, Any]) -> dict[str, str]:
    policies: dict[str, str] = {}
    tools = request.get("tools")
    if not isinstance(tools, list):
        return policies
    for tool in tools:
        fn = _get(tool, "function")
        name = _get(fn, "name") or _get(tool, "name")
        policy = (
            _get(fn, "x-metergraph-idempotency")
            or _get(tool, "x-metergraph-idempotency")
            or _get(fn, "metergraph_idempotency")
            or _get(tool, "metergraph_idempotency")
        )
        if name:
            policies[str(name)] = (
                str(policy)
                if policy in {"idempotent", "non_idempotent"}
                else "non_idempotent"
            )
    return policies


def _tool_events(
    request: Mapping[str, Any],
    response: Any,
    stream_chunks: list[Any] | None = None,
) -> list[dict] | None:
    """Normalize completed history and newly requested provider tool calls."""
    policies = _tool_policies(request)
    calls: dict[str, dict] = {}
    order: list[str] = []
    pending_results: dict[str, tuple[Any, bool]] = {}

    def call(call_id: Any, name: Any, arguments: Any) -> None:
        if not name:
            return
        key = str(call_id or f"{name}:{len(order)}")
        if key not in calls:
            order.append(key)
        calls[key] = {
            "call_id": key,
            "name": str(name),
            "arguments": _tool_argument(arguments),
            "result": None,
            "status": "requested",
            "idempotency": policies.get(str(name), "non_idempotent"),
        }
        if key in pending_results:
            result, is_error = pending_results.pop(key)
            complete(key, result, is_error)

    def complete(call_id: Any, result: Any, is_error: bool = False) -> None:
        key = str(call_id or "")
        if key not in calls:
            pending_results[key] = (result, is_error)
            return
        calls[key]["result"] = _tool_argument(result)
        calls[key]["status"] = "error" if is_error else "completed"

    def content_blocks(value: Any) -> None:
        if not isinstance(value, list):
            return
        for block in value:
            kind = _get(block, "type")
            if kind == "tool_use":
                call(_get(block, "id"), _get(block, "name"), _get(block, "input"))
            elif kind == "tool_result":
                complete(
                    _get(block, "tool_use_id"),
                    _get(block, "content"),
                    bool(_get(block, "is_error", False)),
                )

    def gemini_parts(value: Any) -> None:
        parts = _get(value, "parts")
        if not isinstance(parts, list):
            return
        for part in parts:
            function_call = _get(part, "function_call") or _get(
                part, "functionCall"
            )
            if function_call is not None:
                call(
                    _get(function_call, "id"),
                    _get(function_call, "name"),
                    _get(function_call, "args")
                    or _get(function_call, "arguments"),
                )
            function_response = _get(part, "function_response") or _get(
                part, "functionResponse"
            )
            if function_response is not None:
                complete(
                    _get(function_response, "id")
                    or _get(function_response, "name"),
                    _get(function_response, "response"),
                    False,
                )

    history = request.get("messages")
    if not isinstance(history, list):
        history = request.get("input")
    if not isinstance(history, list):
        history = request.get("contents")
    if isinstance(history, list):
        for message in history:
            for tool_call in _get(message, "tool_calls", []) or []:
                fn = _get(tool_call, "function")
                call(
                    _get(tool_call, "id") or _get(tool_call, "call_id"),
                    _get(fn, "name") or _get(tool_call, "name"),
                    _get(fn, "arguments", _get(tool_call, "arguments")),
                )
            role = _get(message, "role")
            if role == "tool":
                complete(
                    _get(message, "tool_call_id") or _get(message, "call_id"),
                    _get(message, "content"),
                    bool(_get(message, "is_error", False)),
                )
            kind = _get(message, "type")
            if kind in {"function_call", "tool_call"}:
                call(
                    _get(message, "call_id") or _get(message, "id"),
                    _get(message, "name"),
                    _get(message, "arguments"),
                )
            elif kind in {"function_call_output", "tool_result"}:
                complete(
                    _get(message, "call_id") or _get(message, "tool_use_id"),
                    _get(message, "output", _get(message, "content")),
                    bool(_get(message, "is_error", False)),
                )
            content_blocks(_get(message, "content"))
            gemini_parts(message)

    choice = _first(_get(response, "choices"))
    response_message = _get(choice, "message")
    for tool_call in _get(response_message, "tool_calls", []) or []:
        fn = _get(tool_call, "function")
        call(
            _get(tool_call, "id") or _get(tool_call, "call_id"),
            _get(fn, "name") or _get(tool_call, "name"),
            _get(fn, "arguments", _get(tool_call, "arguments")),
        )
    content_blocks(_get(response, "content"))
    for output in _get(response, "output", []) or []:
        kind = _get(output, "type")
        if kind in {"function_call", "tool_call"}:
            call(
                _get(output, "call_id") or _get(output, "id"),
                _get(output, "name"),
                _get(output, "arguments"),
            )
    for candidate in _get(response, "candidates", []) or []:
        gemini_parts(_get(candidate, "content"))

    openai_deltas: dict[str, dict[str, str]] = {}
    anthropic_deltas: dict[str, dict[str, str]] = {}
    for chunk in stream_chunks or []:
        for choice in _get(chunk, "choices", []) or []:
            delta = _get(choice, "delta")
            for position, tool_call in enumerate(
                _get(delta, "tool_calls", []) or []
            ):
                key = str(
                    _get(tool_call, "index")
                    if _get(tool_call, "index") is not None
                    else position
                )
                item = openai_deltas.setdefault(
                    key, {"id": key, "name": "", "arguments": ""}
                )
                if _get(tool_call, "id"):
                    item["id"] = str(_get(tool_call, "id"))
                fn = _get(tool_call, "function")
                if _get(fn, "name"):
                    item["name"] += str(_get(fn, "name"))
                if _get(fn, "arguments"):
                    item["arguments"] += str(_get(fn, "arguments"))
        kind = _get(chunk, "type")
        if kind == "content_block_start":
            block = _get(chunk, "content_block")
            if _get(block, "type") == "tool_use":
                key = str(_get(chunk, "index", len(anthropic_deltas)))
                initial_input = scrub(_get(block, "input", {}))
                anthropic_deltas[key] = {
                    "id": str(_get(block, "id") or key),
                    "name": str(_get(block, "name") or ""),
                    "arguments": (
                        ""
                        if initial_input == {}
                        else json.dumps(initial_input, separators=(",", ":"))
                    ),
                }
        elif kind == "content_block_delta":
            delta = _get(chunk, "delta")
            if _get(delta, "type") == "input_json_delta":
                key = str(_get(chunk, "index", "0"))
                item = anthropic_deltas.setdefault(
                    key, {"id": key, "name": "", "arguments": ""}
                )
                item["arguments"] += str(_get(delta, "partial_json") or "")
        for candidate in _get(chunk, "candidates", []) or []:
            gemini_parts(_get(candidate, "content"))
    for item in [*openai_deltas.values(), *anthropic_deltas.values()]:
        if item["name"]:
            call(item["id"], item["name"], item["arguments"])

    return [calls[key] for key in order] or None


def _capture_frames(
    app_root: str, skip_frames: tuple[str, ...], repo_root: str | None = None
) -> tuple[str | None, str | None, list[dict]]:
    frames: list[dict] = []
    root = os.path.realpath(app_root)
    repo_root_real = os.path.realpath(repo_root) if repo_root else None
    frame = sys._getframe(2)
    while frame is not None and len(frames) < 5:
        filename = os.path.realpath(frame.f_code.co_filename)
        if filename.startswith(root) and not any(
            part in filename for part in skip_frames
        ):
            relative = os.path.relpath(filename, root)
            module = str(Path(relative).with_suffix("")).replace(os.sep, ".")
            qualname = getattr(frame.f_code, "co_qualname", frame.f_code.co_name)
            entry = {"m": module, "f": qualname, "l": frame.f_lineno}
            try:
                inside_repo = bool(
                    repo_root_real
                    and os.path.commonpath((filename, repo_root_real)) == repo_root_real
                )
            except ValueError:
                inside_repo = False
            if inside_repo:
                entry["p"] = os.path.relpath(filename, repo_root_real).replace(
                    os.sep, "/"
                )
            frames.append(entry)
        frame = frame.f_back
    if not frames:
        return None, None, []
    return f"{frames[0]['m']}:{frames[0]['f']}", frames[0]["m"], frames


@dataclass
class Options:
    capture_text: bool = True
    redact: Callable[[str, str], str] | None = None
    app_root: str = os.getcwd()
    repo_root: str | None = None
    skip_frames: tuple[str, ...] = ()
    environment: str | None = None
    text_max_bytes: int = DEFAULT_TEXT_MAX_BYTES


class Runtime:
    def __init__(self, writer: Any, options: Options) -> None:
        self.writer = writer
        self.options = options

    def call_state(
        self,
        provider: str,
        endpoint: str,
        request: Mapping[str, Any],
        *,
        context: CaptureContext | None = None,
    ) -> "CallState":
        context = context or snapshot()
        func, module, frames = _capture_frames(
            self.options.app_root,
            (
                "site-packages",
                "metergraph/_capture.py",
                "concurrent/futures",
                "threading.py",
                *self.options.skip_frames,
            ),
            self.options.repo_root,
        )
        return CallState(
            runtime=self,
            provider=provider,
            endpoint=endpoint,
            request=dict(request),
            context=context,
            started=time.perf_counter(),
            ts=datetime.now(timezone.utc).isoformat(),
            func=context.func_name or func,
            module=context.func_module or module,
            frames=frames,
            trace_id=context.trace_id or secrets.token_hex(16),
            span_id=secrets.token_hex(8),
            parent_span_id=context.parent_span_id,
            trace_name=context.trace_name
            or context.route
            or context.func_name
            or func
            or endpoint,
        )

    def _text(
        self, value: str | None, kind: str, *, enabled: bool
    ) -> tuple[str | None, bool]:
        if not enabled or value is None:
            return None, False
        if self.options.redact:
            try:
                value = self.options.redact(value, kind)
            except Exception:
                return "<redaction-failed>", False
        raw = value.encode()
        if len(raw) <= self.options.text_max_bytes:
            return value, False
        marker = "\n<metergraph:truncated>"
        clipped = raw[: max(0, self.options.text_max_bytes - len(marker))].decode(
            errors="ignore"
        )
        return clipped + marker, True


@dataclass
class CallState:
    runtime: Runtime
    provider: str
    endpoint: str
    request: dict[str, Any]
    context: CaptureContext
    started: float
    ts: str
    func: str | None
    module: str | None
    frames: list[dict]
    trace_id: str
    span_id: str
    parent_span_id: str | None
    trace_name: str
    done: bool = False

    def finish(
        self,
        response: Any = None,
        *,
        status: str | None = None,
        error: BaseException | None = None,
        stream: bool = False,
        ttft_ms: int | None = None,
        response_text: str | None = None,
        stream_chunks: list[Any] | None = None,
    ) -> None:
        if self.done:
            return
        self.done = True
        capture_text = (
            self.context.capture_text
            if self.context.capture_text is not None
            else self.runtime.options.capture_text
        )
        request_clean = scrub(self.request)
        request_json, request_truncated = self.runtime._text(
            json.dumps(request_clean, separators=(",", ":"), default=repr),
            "request",
            enabled=capture_text,
        )
        full_tool_calls = _tool_events(request_clean, response, stream_chunks)
        tool_calls = full_tool_calls
        tool_truncated = False
        if tool_calls and capture_text:
            encoded_tools, tool_truncated = self.runtime._text(
                json.dumps(tool_calls, separators=(",", ":"), default=repr),
                "response",
                enabled=True,
            )
            try:
                tool_calls = json.loads(encoded_tools) if encoded_tools else None
            except json.JSONDecodeError:
                tool_calls = None
        elif tool_calls:
            tool_calls = [
                {
                    "call_id": item["call_id"],
                    "name": item["name"],
                    "status": item["status"],
                    "idempotency": item["idempotency"],
                }
                for item in tool_calls
            ]
        tool_names = (
            list(dict.fromkeys(item["name"] for item in full_tool_calls))
            if full_tool_calls
            else None
        )
        effective_status = status or (
            "error" if error else _stop_reason(response) or "success"
        )
        finish_reason, finish_reason_raw = _finish_reason_details(response)
        status_code = (
            "error"
            if error or status == "error" or finish_reason == "error"
            else "unset"
        )
        response_json, response_truncated = self.runtime._text(
            json.dumps(
                _response_envelope(
                    response,
                    aggregate_text=response_text,
                    tool_calls=full_tool_calls if capture_text else None,
                    error=error,
                    status=effective_status,
                ),
                ensure_ascii=False,
                separators=(",", ":"),
                default=repr,
            ),
            "response",
            enabled=capture_text,
        )
        row: dict[str, Any] = {
            "ts": self.ts,
            "route": self.context.route,
            "provider": self.provider,
            "model": self.request.get("model"),
            **_usage(response),
            "latency_ms": round((time.perf_counter() - self.started) * 1000),
            "status": effective_status,
            "status_code": status_code,
            "finish_reason": finish_reason,
            "finish_reason_raw": finish_reason_raw,
            "session_id": self.context.session_id,
            "conversation_id": self.context.session_id,
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "parent_span_id": self.parent_span_id,
            "trace_name": self.trace_name,
            "template_hash": template_hash(self.request),
            "unit_name": self.context.unit_name,
            "unit_count": self.context.unit_count,
            "tool_calls": tool_calls,
            "tool_names": tool_names,
            "endpoint": self.endpoint,
            "request_id": _request_id(response),
            "batch": self.request.get("batch") is True,
            "batch_custom_id": self.request.get("batch_custom_id"),
            # An explicit false is the application-level sensitive-operation
            # opt-out. Hosted ingestion otherwise preserves available content.
            "content_opted_in": capture_text,
            "request_json": request_json,
            "response_text": response_json,
            "text_truncated": request_truncated or response_truncated or tool_truncated,
            "stream": stream,
            "ttft_ms": ttft_ms,
            "func": self.func,
            "module": self.module,
            "frames_json": self.frames,
            "tags": dict(self.context.tags),
            "environment": self.runtime.options.environment,
            "error": status_code == "error",
            "error_type": type(error).__name__ if error else None,
            "sdk": "python",
            "sdk_version": SDK_VERSION,
            "runtime": f"{platform.python_implementation().lower()}-{platform.python_version()}",
        }
        try:
            self.runtime.writer.enqueue(row)
        except Exception:
            pass


class _StreamState:
    def __init__(self, stream: Any, call: CallState) -> None:
        self.manager = stream
        self.stream = stream
        self.call = call
        self.iterator = None
        self.entered = False
        self.last = None
        self.parts: list[str] = []
        self.chunks: list[Any] = []
        self.ttft_ms: int | None = None

    def use_entered_stream(self, stream: Any) -> None:
        if stream is not None:
            self.stream = stream
            self.iterator = None
        self.entered = True

    def chunk(self, value: Any) -> Any:
        self.last = value
        self.chunks.append(value)
        text = _chunk_text(value)
        if self.ttft_ms is None and _chunk_has_output(value):
            self.ttft_ms = round((time.perf_counter() - self.call.started) * 1000)
        if text:
            self.parts.append(text)
        return value

    def safe_chunk(self, value: Any) -> Any:
        """Record a provider chunk, but return the raw provider value if
        MeterGraph's own normalization/bookkeeping fails. The caller passes the
        chunk it already obtained from the provider, so a telemetry error here
        never drops or corrupts iteration. Only MeterGraph's ``Exception`` is
        swallowed; a cancellation propagating through ``chunk`` is not."""
        try:
            return self.chunk(value)
        except Exception:
            return value

    def is_usage_only(self, value: Any) -> bool:
        """Classify a usage-only chunk, failing open. A genuine usage-only chunk
        is suppressed only when classification succeeds; if MeterGraph's own
        classification raises on a pathological/proxied chunk, treat it as an
        ordinary provider chunk so it is yielded, not dropped. Only MeterGraph's
        ``Exception`` is swallowed here."""
        try:
            return _usage_only_chunk(value, self.call)
        except Exception:
            return False

    def finish(
        self, status: str | None = None, error: BaseException | None = None
    ) -> None:
        response = self.last
        if not error:
            final = getattr(self.stream, "get_final_message", None)
            if callable(final):
                try:
                    response = final()
                    if inspect.isawaitable(response):
                        close = getattr(response, "close", None)
                        if close:
                            close()
                        response = self.last
                except Exception:
                    pass
        self.call.finish(
            response,
            status=status,
            error=error,
            stream=True,
            ttft_ms=self.ttft_ms,
            response_text="".join(self.parts) or None,
            stream_chunks=self.chunks,
        )

    async def finish_async(
        self, status: str | None = None, error: BaseException | None = None
    ) -> None:
        response = self.last
        if not error:
            final = getattr(self.stream, "get_final_message", None)
            if callable(final):
                try:
                    response = final()
                    if inspect.isawaitable(response):
                        response = await response
                except Exception:
                    response = self.last
        self.call.finish(
            response,
            status=status,
            error=error,
            stream=True,
            ttft_ms=self.ttft_ms,
            response_text="".join(self.parts) or None,
            stream_chunks=self.chunks,
        )

    def safe_finish(
        self, status: str | None = None, error: BaseException | None = None
    ) -> None:
        """Shared fail-open finalization for every sync lifecycle path (entry
        error, normal exhaustion, provider iteration error, context exit, and
        close). MeterGraph's own telemetry ``Exception`` is swallowed so a
        failed finish can never replace normal completion, the provider
        iteration exception, or a close; the trace may be lost, but application
        behavior is unchanged. Provider/application exceptions never reach
        here."""
        try:
            self.finish(status=status, error=error)
        except Exception:
            pass

    async def safe_finish_async(
        self, status: str | None = None, error: BaseException | None = None
    ) -> None:
        """Async counterpart to :meth:`safe_finish`, shared by every async
        lifecycle path (entry error, normal exhaustion, provider iteration
        error, context exit, and aclose)."""
        try:
            await self.finish_async(status=status, error=error)
        except Exception:
            pass


class SyncStream:
    def __init__(self, stream: Any, call: CallState) -> None:
        self._state = _StreamState(stream, call)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._state.stream, name)

    def __iter__(self) -> "SyncStream":
        if self._state.iterator is None:
            self._state.iterator = iter(self._state.stream)
        return self

    def __next__(self) -> Any:
        if self._state.iterator is None:
            self._state.iterator = iter(self._state.stream)
        while True:
            try:
                provider_chunk = next(self._state.iterator)
            except StopIteration:
                self._state.safe_finish()
                raise
            except BaseException as exc:
                self._state.safe_finish(error=exc)
                raise
            value = self._state.safe_chunk(provider_chunk)
            if self._state.is_usage_only(value):
                continue
            return value

    def __enter__(self) -> "SyncStream":
        enter = getattr(self._state.manager, "__enter__", None)
        if enter:
            try:
                self._state.use_entered_stream(enter())
            except BaseException as exc:
                self._state.safe_finish(error=exc)
                raise
        return self

    def __exit__(self, exc_type, exc, tb) -> Any:
        if exc:
            self._state.safe_finish(error=exc)
        elif not self._state.call.done:
            self._state.safe_finish(status=None if self._state.entered else "abandoned")
        exit_fn = getattr(self._state.manager, "__exit__", None)
        return exit_fn(exc_type, exc, tb) if exit_fn else None

    def close(self) -> None:
        close = getattr(self._state.stream, "close", None)
        if close:
            close()
        if not self._state.call.done:
            self._state.safe_finish(status="abandoned")

    def __del__(self) -> None:
        try:
            if not self._state.call.done:
                self._state.finish(status="abandoned")
        except Exception:
            pass


class AsyncStream:
    def __init__(self, stream: Any, call: CallState) -> None:
        self._state = _StreamState(stream, call)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._state.stream, name)

    def __aiter__(self) -> "AsyncStream":
        if self._state.iterator is None:
            self._state.iterator = self._state.stream.__aiter__()
        return self

    async def __anext__(self) -> Any:
        if self._state.iterator is None:
            self._state.iterator = self._state.stream.__aiter__()
        while True:
            try:
                provider_chunk = await self._state.iterator.__anext__()
            except StopAsyncIteration:
                await self._state.safe_finish_async()
                raise
            except BaseException as exc:
                await self._state.safe_finish_async(error=exc)
                raise
            value = self._state.safe_chunk(provider_chunk)
            if self._state.is_usage_only(value):
                continue
            return value

    async def __aenter__(self) -> "AsyncStream":
        enter = getattr(self._state.manager, "__aenter__", None)
        if enter:
            try:
                self._state.use_entered_stream(await enter())
            except BaseException as exc:
                await self._state.safe_finish_async(error=exc)
                raise
        return self

    async def __aexit__(self, exc_type, exc, tb) -> Any:
        if exc:
            await self._state.safe_finish_async(error=exc)
        elif not self._state.call.done:
            await self._state.safe_finish_async(
                status=None if self._state.entered else "abandoned"
            )
        exit_fn = getattr(self._state.manager, "__aexit__", None)
        return await exit_fn(exc_type, exc, tb) if exit_fn else None

    async def aclose(self) -> None:
        close = getattr(self._state.stream, "aclose", None)
        if close:
            await close()
        if not self._state.call.done:
            await self._state.safe_finish_async(status="abandoned")

    def __del__(self) -> None:
        try:
            if not self._state.call.done:
                self._state.call.finish(
                    self._state.last,
                    status="abandoned",
                    stream=True,
                    ttft_ms=self._state.ttft_ms,
                    response_text="".join(self._state.parts) or None,
                    stream_chunks=self._state.chunks,
                )
        except Exception:
            pass


_runtime: Runtime | None = None
_seen_batch_items: set[str] = set()


def set_runtime(runtime: Runtime | None) -> None:
    global _runtime
    _runtime = runtime


def _get_runtime() -> Runtime | None:
    """Return the active runtime to internal capture integrations."""
    return _runtime


def _request(args: tuple, kwargs: dict) -> dict[str, Any]:
    request: dict[str, Any] = {}
    if args and isinstance(args[0], Mapping):
        request.update(args[0])
    request.update(kwargs)
    return request


def _mark_batch_item(key: str) -> bool:
    if key in _seen_batch_items:
        return False
    if len(_seen_batch_items) >= 100_000:
        _seen_batch_items.clear()
    _seen_batch_items.add(key)
    return True


def _capture_openai_batch_item(
    runtime: Runtime,
    item: Any,
    *,
    source_id: str,
    context: CaptureContext,
) -> None:
    response = _get(item, "response")
    error = _get(item, "error")
    custom_id = str(_get(item, "custom_id") or "")
    item_id = str(_get(item, "id") or custom_id)
    if response is None and error is None:
        return
    response_id = str(_get(response, "request_id") or item_id)
    if not _mark_batch_item(f"openai:{source_id}:{item_id}:{response_id}"):
        return
    body = _get(response, "body") or {}
    normalized = dict(body) if isinstance(body, Mapping) else body
    if isinstance(normalized, dict) and response_id:
        normalized = {**normalized, "_request_id": response_id}
    request = {
        "model": _get(body, "model"),
        "batch": True,
        "service_tier": "batch",
        "batch_custom_id": custom_id or None,
        "batch_item_id": item_id or None,
    }
    object_type = str(_get(body, "object") or "")
    endpoint = "batch.responses" if object_type == "response" else "batch.chat.completions"
    call = runtime.call_state("openai", endpoint, request, context=context)
    status_code = _int(_get(response, "status_code"))
    failed = error is not None or (status_code is not None and status_code >= 400)
    call.finish(normalized, status="error" if failed else None)


def _capture_openai_batch_content(
    runtime: Runtime,
    result: Any,
    *,
    source_id: str,
    context: CaptureContext,
) -> None:
    try:
        content = result if isinstance(result, (str, bytes, bytearray)) else _get(result, "content")
        if isinstance(content, (bytes, bytearray)):
            content = bytes(content).decode("utf-8")
        if not isinstance(content, str):
            return
        for line in content.splitlines():
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except (TypeError, json.JSONDecodeError):
                continue
            if isinstance(item, Mapping) and "custom_id" in item and (
                "response" in item or "error" in item
            ):
                _capture_openai_batch_item(
                    runtime, item, source_id=source_id, context=context
                )
    except Exception:
        return


def _capture_anthropic_batch_item(
    runtime: Runtime,
    item: Any,
    *,
    batch_id: str,
    context: CaptureContext,
) -> None:
    result = _get(item, "result")
    result_type = str(_get(result, "type") or "")
    message = _get(result, "message")
    custom_id = str(_get(item, "custom_id") or "")
    key = f"anthropic:{batch_id}:{custom_id}:{result_type}"
    if not result_type or not _mark_batch_item(key):
        return
    request = {
        "model": _get(message, "model"),
        "batch": True,
        "service_tier": "batch",
        "batch_custom_id": custom_id or None,
        "batch_id": batch_id or None,
    }
    call = runtime.call_state("anthropic", "batch.messages", request, context=context)
    call.finish(message or {}, status=None if result_type == "succeeded" else "error")


class _SyncBatchResults:
    def __init__(
        self, result: Any, runtime: Runtime, batch_id: str, context: CaptureContext
    ) -> None:
        self._result = result
        self._runtime = runtime
        self._batch_id = batch_id
        self._context = context

    def __getattr__(self, name: str) -> Any:
        return getattr(self._result, name)

    def __iter__(self):
        for item in self._result:
            try:
                _capture_anthropic_batch_item(
                    self._runtime,
                    item,
                    batch_id=self._batch_id,
                    context=self._context,
                )
            except Exception:
                pass
            yield item


class _AsyncBatchResults:
    def __init__(
        self, result: Any, runtime: Runtime, batch_id: str, context: CaptureContext
    ) -> None:
        self._result = result
        self._runtime = runtime
        self._batch_id = batch_id
        self._context = context

    def __getattr__(self, name: str) -> Any:
        return getattr(self._result, name)

    def __aiter__(self):
        return self

    async def __anext__(self):
        item = await self._result.__anext__()
        try:
            _capture_anthropic_batch_item(
                self._runtime,
                item,
                batch_id=self._batch_id,
                context=self._context,
            )
        except Exception:
            pass
        return item


def _wrap_anthropic_batch_results(
    result: Any, runtime: Runtime, batch_id: str, context: CaptureContext
) -> Any:
    if hasattr(result, "__aiter__"):
        iterator = result.__aiter__()
        return _AsyncBatchResults(iterator, runtime, batch_id, context)
    if hasattr(result, "__iter__") and not isinstance(result, (str, bytes, bytearray)):
        return _SyncBatchResults(result, runtime, batch_id, context)
    return result


def _patch_openai_batch_content(owner: Any, method_name: str) -> bool:
    original = getattr(owner, method_name, None)
    if not callable(original):
        return False
    if getattr(original, "__metergraph_batch__", False):
        return True

    @functools.wraps(original)
    def wrapped(*args, **kwargs):
        runtime = _runtime
        if runtime is None:
            return original(*args, **kwargs)
        source_id = str(args[0] if args else kwargs.get("file_id") or "unknown")
        context = snapshot()
        result = original(*args, **kwargs)
        if inspect.isawaitable(result):

            async def await_result():
                resolved = await result
                _capture_openai_batch_content(
                    runtime, resolved, source_id=source_id, context=context
                )
                return resolved

            return await_result()
        _capture_openai_batch_content(
            runtime, result, source_id=source_id, context=context
        )
        return result

    wrapped.__metergraph_batch__ = True  # type: ignore[attr-defined]
    try:
        setattr(owner, method_name, wrapped)
    except Exception:
        return False
    return True


def _patch_anthropic_batch_results(owner: Any) -> bool:
    original = getattr(owner, "results", None)
    if not callable(original):
        return False
    if getattr(original, "__metergraph_batch__", False):
        return True

    @functools.wraps(original)
    def wrapped(*args, **kwargs):
        runtime = _runtime
        if runtime is None:
            return original(*args, **kwargs)
        batch_id = str(
            args[0]
            if args
            else kwargs.get("message_batch_id") or kwargs.get("batch_id") or "unknown"
        )
        context = snapshot()
        result = original(*args, **kwargs)
        if inspect.isawaitable(result):

            async def await_result():
                resolved = await result
                return _wrap_anthropic_batch_results(
                    resolved, runtime, batch_id, context
                )

            return await_result()
        return _wrap_anthropic_batch_results(result, runtime, batch_id, context)

    wrapped.__metergraph_batch__ = True  # type: ignore[attr-defined]
    try:
        setattr(owner, "results", wrapped)
    except Exception:
        return False
    return True


def _patch(
    owner: Any,
    method_name: str,
    provider: str,
    endpoint: str,
    *,
    gateway: bool = False,
) -> bool:
    original = getattr(owner, method_name, None)
    if not callable(original):
        return False
    if getattr(original, "__metergraph__", False):
        return True

    @functools.wraps(original)
    def wrapped(*args, **kwargs):
        runtime = _runtime
        if runtime is None:
            return original(*args, **kwargs)
        if (
            provider == "openai"
            and endpoint == "chat.completions"
            and os.getenv("METERGRAPH_PATCH_STREAM_USAGE", "1") != "0"
        ):
            incoming = _request(args, kwargs)
            if incoming.get("stream") is True and "stream_options" not in incoming:
                if args and isinstance(args[0], Mapping):
                    first = {**args[0], "stream_options": {"include_usage": True}}
                    args = (first, *args[1:])
                else:
                    kwargs = {**kwargs, "stream_options": {"include_usage": True}}
        try:
            request = _request(args, kwargs)
            capture_provider = _gateway_provider(request) if gateway else provider
            call = runtime.call_state(capture_provider, endpoint, request)
        except Exception:
            # Capture-state creation is telemetry-only. If it fails, invoke the
            # provider exactly once and return/await its native result with no
            # MeterGraph wrapping — the caller observes an uninstrumented call.
            return original(*args, **kwargs)
        try:
            result = original(*args, **kwargs)
        except BaseException as exc:
            _safe_finish(call, error=exc)
            raise

        if inspect.isawaitable(result):

            async def await_result():
                try:
                    resolved = await result
                except BaseException as exc:
                    _safe_finish(call, error=exc)
                    raise
                return _finish_or_stream(resolved, call, endpoint, request)

            return await_result()
        return _finish_or_stream(result, call, endpoint, request)

    wrapped.__metergraph__ = True  # type: ignore[attr-defined]
    try:
        setattr(owner, method_name, wrapped)
    except Exception:
        return False
    return True


def _safe_finish(call: CallState, response: Any = None, **kwargs: Any) -> None:
    """Fail-open finalization for a non-streaming call. MeterGraph's own
    telemetry ``Exception`` is swallowed so a failed ``CallState.finish`` can
    never replace the provider result or the provider exception the caller must
    observe. Only telemetry runs here — the provider result/exception is passed
    in, never produced — so a cancellation is never wrongly swallowed."""
    try:
        call.finish(response, **kwargs)
    except Exception:
        pass


def _finish_or_stream(
    result: Any, call: CallState, endpoint: str, request: Mapping[str, Any]
):
    is_stream = endpoint.endswith(".stream") or bool(request.get("stream"))
    if is_stream and (
        hasattr(result, "__aiter__")
        or (hasattr(result, "__aenter__") and hasattr(result, "__aexit__"))
    ):
        return AsyncStream(result, call)
    if is_stream and (
        hasattr(result, "__iter__")
        or (hasattr(result, "__enter__") and hasattr(result, "__exit__"))
    ):
        return SyncStream(result, call)
    _safe_finish(call, result)
    return result


@dataclass(frozen=True)
class Seam:
    """A single instrumentable method, addressed by a dotted attribute path
    from the client root (e.g. "beta.chat.completions")."""

    path: str
    method: str
    endpoint: str


OPENAI_SEAMS: tuple[Seam, ...] = (
    Seam("chat.completions", "create", "chat.completions"),
    Seam("chat.completions", "parse", "chat.completions.parse"),
    Seam("beta.chat.completions", "parse", "chat.completions.parse"),
    Seam("responses", "create", "responses"),
    Seam("responses", "stream", "responses.stream"),
    Seam("responses", "parse", "responses.parse"),
    Seam("beta.responses", "create", "responses"),
    # Note: client.beta.responses has no .parse method (verified against
    # openai==2.50.0) — do not add one here without re-verifying first.
)

ANTHROPIC_SEAMS: tuple[Seam, ...] = (
    Seam("messages", "create", "messages"),
    Seam("messages", "stream", "messages.stream"),
)

GOOGLE_SEAMS: tuple[Seam, ...] = (
    Seam("models", "generate_content", "models.generate_content"),
    Seam("models", "generate_content_stream", "models.generate_content.stream"),
    Seam("aio.models", "generate_content", "models.generate_content"),
    Seam("aio.models", "generate_content_stream", "models.generate_content.stream"),
)

SEAM_TABLES: dict[str, tuple[Seam, ...]] = {
    "openai": OPENAI_SEAMS,
    "anthropic": ANTHROPIC_SEAMS,
    "google": GOOGLE_SEAMS,
}


def _detect_provider(client: Any) -> str:
    if hasattr(getattr(client, "models", None), "generate_content"):
        return "google"
    if hasattr(client, "chat") or hasattr(client, "responses"):
        return "openai"
    return "anthropic"


def _resolve(client: Any, path: str) -> Any:
    obj = client
    for part in path.split("."):
        obj = getattr(obj, part, None)
        if obj is None:
            return None
    return obj


_VERCEL_GATEWAY_HOST = "ai-gateway.vercel.sh"
_VERCEL_PROVIDER_ALIASES = {"gateway", "vercel", "vercel-ai-gateway"}


def _uses_vercel_gateway(client: Any) -> bool:
    """Recognize the public AI Gateway URL exposed by supported Python SDKs."""
    base_url = getattr(client, "base_url", None) or getattr(
        client, "_base_url", None
    )
    if base_url is None:
        return False
    try:
        return urlsplit(str(base_url).strip()).hostname == _VERCEL_GATEWAY_HOST
    except (TypeError, ValueError):
        return False


def _gateway_provider(request: Mapping[str, Any]) -> str:
    """Use the creator in Vercel's required `creator/model` identifier."""
    model = str(request.get("model") or "").strip().lower()
    creator, separator, _ = model.partition("/")
    return creator if separator and creator else "vercel-ai-gateway"


def _apply_seams(client: Any, provider: str, *, gateway: bool = False) -> list[str]:
    patched: list[str] = []
    for seam in SEAM_TABLES.get(provider, ()):
        try:
            owner = _resolve(client, seam.path)
        except Exception:
            continue  # a pathological client property must never break wrap()
        if owner is not None and _patch(
            owner,
            seam.method,
            provider,
            seam.endpoint,
            gateway=gateway,
        ):
            patched.append(f"{seam.path}.{seam.method}")
    return patched


def _apply_batch_extras(client: Any, provider: str) -> int:
    patched = 0
    if provider == "openai":
        files = getattr(client, "files", None)
        if files is not None:
            patched += int(_patch_openai_batch_content(files, "content"))
            patched += int(_patch_openai_batch_content(files, "retrieve_content"))
    elif provider == "anthropic":
        messages = getattr(client, "messages", None)
        batch_owners = [getattr(messages, "batches", None)]
        beta_messages = getattr(getattr(client, "beta", None), "messages", None)
        batch_owners.append(getattr(beta_messages, "batches", None))
        patched += sum(
            int(_patch_anthropic_batch_results(owner))
            for owner in batch_owners
            if owner is not None
        )
    return patched


def wrap(client: Any, *, provider: str | None = None) -> Any:
    """Patch supported resource methods on an OpenAI, Anthropic, or Google client.

    OpenAI and Anthropic clients pointed at Vercel AI Gateway are recognized
    automatically. ``provider="vercel"`` can force gateway attribution for a
    compatible client whose public base URL has been customized.

    Never raises: an unrecognized client shape, or an exception while probing
    it, results in an unmodified, uninstrumented client — not a crash.
    """
    try:
        gateway = (
            provider.strip().lower() in _VERCEL_PROVIDER_ALIASES
            if isinstance(provider, str)
            else _uses_vercel_gateway(client)
        )
        resolved_provider = (
            _detect_provider(client)
            if gateway
            else provider or _detect_provider(client)
        )
        patched = _apply_seams(client, resolved_provider, gateway=gateway)
        patched_count = len(patched)
        if not gateway:
            patched_count += _apply_batch_extras(client, resolved_provider)
        client_label = (
            f"Vercel AI Gateway via {resolved_provider}"
            if gateway
            else resolved_provider
        )
        if not patched_count:
            log.warning(
                "Metergraph found no supported methods on %s client", client_label
            )
        else:
            log.info(
                "Metergraph patched %d seam(s) on %s client: %s",
                patched_count,
                client_label,
                ", ".join(patched) or "(batch-only)",
            )
    except Exception:
        log.warning(
            "Metergraph wrap() failed; client is unmodified and uninstrumented",
            exc_info=True,
        )
    return client
