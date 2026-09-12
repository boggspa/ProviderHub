"""Authenticated Cerebras reasoning replay through Anthropic thinking blocks.

Only this gateway's signatures are accepted.  Native Anthropic/provider
signatures, client annotations, and unsigned reasoning are never interpreted as
Cerebras Chat ``assistant.reasoning`` fields.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import hmac
import json


class CerebrasReplayError(ValueError):
    """A reasoning trace or streamed Cerebras response is not safe to replay."""


_SIGNATURE_PREFIX = "mb-cerebras-v1."
_KEY_CONTEXT = b"Mistral Bridge/Cerebras reasoning replay/signing key/v1"
_ENVELOPE_CONTEXT = b"Mistral Bridge/Cerebras reasoning replay/envelope/v1\0"


def _text(value, label: str, *, allow_empty: bool = True) -> str:
    if not isinstance(value, str) or (not allow_empty and not value):
        raise CerebrasReplayError(f"{label} must be text.")
    return value


def _token_bytes(gateway_token) -> bytes:
    if isinstance(gateway_token, str):
        value = gateway_token.encode("utf-8")
    elif isinstance(gateway_token, bytes):
        value = gateway_token
    else:
        raise CerebrasReplayError("Gateway token must be text or bytes.")
    if not value:
        raise CerebrasReplayError("Gateway token cannot be empty.")
    return value


def _canonical_json(value) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise CerebrasReplayError("Assistant content must be canonical JSON data.") from exc


def _canonical_assistant_content(assistant_content) -> list[dict]:
    if isinstance(assistant_content, str):
        assistant_content = [{"type": "text", "text": assistant_content}]
    if not isinstance(assistant_content, list):
        raise CerebrasReplayError("Assistant content must be text or a content-block array.")
    canonical = []
    for block in assistant_content:
        if not isinstance(block, dict):
            raise CerebrasReplayError("Assistant content blocks must be objects.")
        kind = block.get("type")
        if kind == "text":
            canonical.append({"type": "text", "text": _text(block.get("text", ""), "Assistant text")})
        elif kind == "tool_use":
            identifier = _text(block.get("id"), "Tool ID", allow_empty=False)
            name = _text(block.get("name"), "Tool name", allow_empty=False)
            arguments = block.get("input")
            if not isinstance(arguments, dict):
                raise CerebrasReplayError("Tool input must be an object.")
            # Round-trip through canonical JSON to reject non-JSON values and to
            # detach the signed representation from caller-owned mutable data.
            canonical_arguments = json.loads(_canonical_json(arguments))
            canonical.append({
                "type": "tool_use",
                "id": identifier,
                "name": name,
                "input": canonical_arguments,
            })
        else:
            raise CerebrasReplayError(
                f"Unsupported assistant block in a Cerebras reasoning envelope: {kind or 'missing type'}."
            )
    return canonical


def _signature(reasoning: str, assistant_content, upstream_model: str, scope: str, gateway_token) -> str:
    reasoning = _text(reasoning, "Cerebras reasoning", allow_empty=False)
    upstream_model = _text(upstream_model, "Upstream model", allow_empty=False)
    scope = _text(scope, "Cerebras connection scope", allow_empty=False)
    canonical_content = _canonical_assistant_content(assistant_content)
    envelope = {
        "version": 1,
        "provider": "cerebras",
        "model": upstream_model,
        "scope_sha256": hashlib.sha256(scope.encode("utf-8")).hexdigest(),
        "reasoning_sha256": hashlib.sha256(reasoning.encode("utf-8")).hexdigest(),
        "assistant_sha256": hashlib.sha256(_canonical_json(canonical_content)).hexdigest(),
    }
    signing_key = hmac.new(_token_bytes(gateway_token), _KEY_CONTEXT, hashlib.sha256).digest()
    digest = hmac.new(signing_key, _ENVELOPE_CONTEXT + _canonical_json(envelope), hashlib.sha256).digest()
    encoded = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return _SIGNATURE_PREFIX + encoded


def sign_thinking(reasoning, assistant_content, upstream_model: str, scope: str, gateway_token) -> dict | None:
    """Return a gateway-signed Anthropic thinking block, or ``None`` for no trace."""
    if reasoning is None or reasoning == "":
        return None
    reasoning = _text(reasoning, "Cerebras reasoning", allow_empty=False)
    return {
        "type": "thinking",
        "thinking": reasoning,
        "signature": _signature(reasoning, assistant_content, upstream_model, scope, gateway_token),
    }


def _message_content(message: dict) -> list[dict]:
    content = message.get("content", "")
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if not isinstance(content, list) or not all(isinstance(block, dict) for block in content):
        raise CerebrasReplayError("Assistant message content must be text or a content-block array.")
    return content


def validate_messages(
    messages,
    upstream_model: str,
    scope: str,
    gateway_token,
    require_tool_reasoning: bool = True,
) -> dict[int, str]:
    """Validate gateway-origin traces and return reasoning by original index.

    The input is inspected without mutation.  A tool-using assistant message
    requires exactly one valid gateway-signed Cerebras trace by default.
    """
    _text(upstream_model, "Upstream model", allow_empty=False)
    _text(scope, "Cerebras connection scope", allow_empty=False)
    _token_bytes(gateway_token)
    if not isinstance(messages, list):
        raise CerebrasReplayError("Messages must be an array.")
    result = {}
    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            raise CerebrasReplayError("Every message must be an object.")
        if message.get("role") != "assistant":
            continue
        content = _message_content(message)
        thinking = [block for block in content if block.get("type") in {"thinking", "redacted_thinking"}]
        visible = [block for block in content if block.get("type") not in {"thinking", "redacted_thinking"}]
        has_tools = any(block.get("type") == "tool_use" for block in visible)
        if len(thinking) > 1:
            raise CerebrasReplayError("A Cerebras assistant message must contain exactly one reasoning trace.")
        if not thinking:
            if require_tool_reasoning and has_tools:
                raise CerebrasReplayError("Cerebras tool output is missing its signed reasoning trace.")
            continue
        block = thinking[0]
        if content[0] is not block:
            raise CerebrasReplayError("Cerebras reasoning trace must be the first assistant content block.")
        if block.get("type") != "thinking":
            raise CerebrasReplayError("A redacted or foreign thinking block cannot be replayed as Cerebras reasoning.")
        reasoning = _text(block.get("thinking"), "Cerebras reasoning", allow_empty=False)
        supplied = block.get("signature")
        if not isinstance(supplied, str) or not supplied.startswith(_SIGNATURE_PREFIX):
            raise CerebrasReplayError("Thinking block is not signed by this Cerebras gateway.")
        expected = _signature(reasoning, visible, upstream_model, scope, gateway_token)
        if not hmac.compare_digest(supplied, expected):
            raise CerebrasReplayError("Cerebras reasoning signature does not match this model, scope, or assistant output.")
        result[index] = reasoning
    return result


class CerebrasStreamAdapter:
    """Buffer a Chat stream and emit a signed Anthropic reasoning envelope."""

    def __init__(
        self,
        requested_model: str,
        tool_name_map: dict,
        upstream_model: str,
        scope: str,
        gateway_token,
        *,
        require_tool_reasoning: bool = True,
        translator_factory=None,
    ):
        if translator_factory is None:
            # Delayed to avoid the protocol -> bridge_core -> providers import
            # cycle during worker startup.
            from protocol import StreamTranslator
            translator_factory = StreamTranslator
        self.delegate = translator_factory(requested_model, tool_name_map)
        self.upstream_model = _text(upstream_model, "Upstream model", allow_empty=False)
        self.scope = _text(scope, "Cerebras connection scope", allow_empty=False)
        self.gateway_token = gateway_token
        _token_bytes(gateway_token)
        self.require_tool_reasoning = bool(require_tool_reasoning)
        self.reasoning_parts = []
        self.buffered_events = []
        self.blocks = {}
        self.started = False
        self.ended = False

    @property
    def usage(self) -> dict:
        value = getattr(self.delegate, "usage", {})
        return copy.deepcopy(value) if isinstance(value, dict) else {}

    def start(self) -> list[dict]:
        if self.started:
            raise CerebrasReplayError("Cerebras stream has already started.")
        self.started = True
        events = self.delegate.start()
        if not isinstance(events, list):
            raise CerebrasReplayError("Chat stream translator returned invalid start events.")
        return events

    def _capture(self, events: list[dict]) -> None:
        for event in events:
            if not isinstance(event, dict):
                raise CerebrasReplayError("Chat stream translator returned an invalid event.")
            kind = event.get("type")
            if kind == "content_block_start":
                index = event.get("index")
                block = event.get("content_block")
                if type(index) is not int or not isinstance(block, dict) or index in self.blocks:
                    raise CerebrasReplayError("Chat stream returned an invalid content block start.")
                if block.get("type") == "text":
                    self.blocks[index] = {"type": "text", "text": ""}
                elif block.get("type") == "tool_use":
                    self.blocks[index] = {
                        "type": "tool_use",
                        "id": _text(block.get("id"), "Tool ID", allow_empty=False),
                        "name": _text(block.get("name"), "Tool name", allow_empty=False),
                        "partial_json": "",
                    }
                else:
                    raise CerebrasReplayError("Chat stream returned an unsupported content block.")
            elif kind == "content_block_delta":
                index = event.get("index")
                delta = event.get("delta")
                if type(index) is not int or index not in self.blocks or not isinstance(delta, dict):
                    raise CerebrasReplayError("Chat stream returned a delta without a content block.")
                if delta.get("type") == "text_delta" and self.blocks[index]["type"] == "text":
                    self.blocks[index]["text"] += _text(delta.get("text", ""), "Text delta")
                elif delta.get("type") == "input_json_delta" and self.blocks[index]["type"] == "tool_use":
                    self.blocks[index]["partial_json"] += _text(delta.get("partial_json", ""), "Tool JSON delta")
                else:
                    raise CerebrasReplayError("Chat stream returned an incompatible content delta.")

    def feed(self, chunk: dict) -> list[dict]:
        if not self.started or self.ended:
            raise CerebrasReplayError("Cerebras stream is not active.")
        if not isinstance(chunk, dict):
            raise CerebrasReplayError("Cerebras stream chunk must be an object.")
        choices = chunk.get("choices", [])
        if not isinstance(choices, list):
            raise CerebrasReplayError("Cerebras stream choices must be an array.")
        for choice in choices:
            if not isinstance(choice, dict) or choice.get("index", 0) != 0:
                continue
            delta = choice.get("delta") or {}
            if not isinstance(delta, dict):
                raise CerebrasReplayError("Cerebras stream delta must be an object.")
            reasoning = delta.get("reasoning")
            if reasoning is not None:
                self.reasoning_parts.append(_text(reasoning, "Cerebras reasoning delta"))
        events = self.delegate.feed(chunk)
        if not isinstance(events, list):
            raise CerebrasReplayError("Chat stream translator returned invalid events.")
        self._capture(events)
        self.buffered_events.extend(copy.deepcopy(events))
        return []

    def _assistant_content(self) -> list[dict]:
        result = []
        for index in sorted(self.blocks):
            block = self.blocks[index]
            if block["type"] == "text":
                result.append({"type": "text", "text": block["text"]})
                continue
            try:
                arguments = json.loads(block["partial_json"] or "{}")
            except ValueError as exc:
                raise CerebrasReplayError("Cerebras stream returned invalid tool arguments.") from exc
            if not isinstance(arguments, dict):
                raise CerebrasReplayError("Cerebras tool arguments must decode to an object.")
            result.append({
                "type": "tool_use",
                "id": block["id"],
                "name": block["name"],
                "input": arguments,
            })
        return result

    @staticmethod
    def _shift(events: list[dict], offset: int) -> list[dict]:
        shifted = copy.deepcopy(events)
        if not offset:
            return shifted
        for event in shifted:
            if event.get("type") in {"content_block_start", "content_block_delta", "content_block_stop"}:
                index = event.get("index")
                if type(index) is not int:
                    raise CerebrasReplayError("Chat stream returned a content event without an index.")
                event["index"] = index + offset
        return shifted

    def end(self) -> list[dict]:
        if not self.started or self.ended:
            raise CerebrasReplayError("Cerebras stream is not active.")
        self.ended = True
        try:
            ending = self.delegate.end()
        except Exception as exc:
            raise CerebrasReplayError(
                "Cerebras stream ended before a valid completion signal or with incomplete tool output."
            ) from exc
        if not isinstance(ending, list):
            raise CerebrasReplayError("Chat stream translator returned invalid end events.")
        assistant_content = self._assistant_content()
        has_tools = any(block["type"] == "tool_use" for block in assistant_content)
        reasoning = "".join(self.reasoning_parts)
        if has_tools and self.require_tool_reasoning and not reasoning:
            raise CerebrasReplayError("Cerebras tool output is missing required reasoning.")
        block = sign_thinking(
            reasoning or None,
            assistant_content,
            self.upstream_model,
            self.scope,
            self.gateway_token,
        )
        if block is None:
            return self._shift(self.buffered_events, 0) + self._shift(ending, 0)
        thinking_events = [
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "thinking", "thinking": "", "signature": ""},
            },
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "thinking_delta", "thinking": reasoning},
            },
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "signature_delta", "signature": block["signature"]},
            },
            {"type": "content_block_stop", "index": 0},
        ]
        return thinking_events + self._shift(self.buffered_events, 1) + self._shift(ending, 1)
