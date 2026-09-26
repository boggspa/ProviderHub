"""Validated structured host handoffs for CLI-backed Messages requests.

The nested CLI generates an answer, but only the desktop host executes the
requested tools. Buffer and validate the whole answer before releasing any
part of a call batch. Malformed output must not become visible narration or
partially executed work.
"""
from __future__ import annotations

import json
import uuid

from cli_tool_call import (HOST_EXECUTION_NOTE, MAX_CALLS_PER_TURN,
                           MAX_ENVELOPE_BYTES, ToolCallError, validate_host_call)

MAX_REPLY_BYTES = 1024 * 1024


def reply_schema(tools, tool_choice=None):
    choice = tool_choice if isinstance(tool_choice, dict) else {}
    names = [tool["name"] for tool in tools]
    if choice.get("type") == "tool":
        names = [name for name in names if name == choice.get("name")]
        if not names:
            raise ToolCallError("the requested host tool was not offered")
    calls = {
        "type": "array",
        "items": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "enum": names},
                "arguments": {"type": "string", "description": "A JSON-encoded object matching the named host tool's input schema."},
            },
            "required": ["name", "arguments"],
            "additionalProperties": False,
        },
        "maxItems": 1 if choice.get("disable_parallel_tool_use") else MAX_CALLS_PER_TURN,
    }
    if choice.get("type") in {"any", "required", "tool"}:
        calls["minItems"] = 1
    return {
        "type": "object",
        "properties": {"text": {"type": "string"}, "tool_calls": calls},
        "required": ["text", "tool_calls"],
        "additionalProperties": False,
    }


def render_manifest(tools, tool_choice=None):
    lines = [HOST_EXECUTION_NOTE, "", "Host response protocol:",
             'Return the JSON object required by the output schema: {"text": "...", "tool_calls": [...]}.',
             'To request a host tool, add {"name": "EXACT_HOST_NAME", "arguments": "JSON-encoded input object"} to tool_calls.',
             "The text field is a brief explanation accompanying calls, or the final answer when no more work is needed.",
             "For actions, put the actual requests in tool_calls now. An intention or promise in text does not execute anything.",
             "Do not invoke the nested CLI's native tools, searches, or agents. Return the host requests as structured output and stop.",
             "Host results arrive in the next turn. Continue from those actual results; do not repeat completed actions."]
    choice = tool_choice if isinstance(tool_choice, dict) else {}
    if choice.get("type") in {"any", "required", "tool"}:
        lines.append("You must request at least one host tool in this reply.")
    if choice.get("type") == "tool":
        lines.append("Request only this tool: " + str(choice.get("name")))
    lines.append("\nHost tools available:")
    for tool in tools:
        # Context reduction: compact separators; the definition is unchanged.
        lines.append(json.dumps(tool, ensure_ascii=False, separators=(",", ":")))
    return "\n".join(lines)


def render_anchor():
    return ('<host_note>Use the structured response fields text and tool_calls. '
            'Request actions through tool_calls with a host tool name and JSON-encoded arguments; '
            'the host executes them. Do not use native CLI tools or end with a promise to act.</host_note>')


def _invalid_constant(_):
    raise ValueError("Non-finite JSON number")


def _decode(text):
    try:
        return json.loads(text, parse_constant=_invalid_constant)
    except (ValueError, TypeError, RecursionError) as exc:
        raise ToolCallError("structured reply contains invalid JSON") from exc


def _decode_reply(text):
    """The first complete JSON value in the stream; the rest is discarded.

    A CLI under an output schema is meant to be a model transport: one
    request, one answer, one host handoff. Muse instead keeps its own agent
    loop running after that answer and appends another schema-shaped value to
    the same output stream, with no separator between them (measured: about
    one turn in four, at every ``--max-model-steps`` budget that reliably
    terminates). Those later values are written as though the host had
    already executed the first one, so they narrate work that never happened
    - the second value "verifies" an edit the host was never asked to make.

    Only the first value was generated from the real host transcript, so it
    is the authoritative reply. Decoding the buffer whole instead would fail
    on the concatenation and reject a perfectly good handoff, which is what
    turned these turns into a retry, a repeated host action, or a stall.
    """
    decoder = json.JSONDecoder(parse_constant=_invalid_constant)
    start = 0
    while start < len(text) and text[start].isspace():
        start += 1
    try:
        reply, _ = decoder.raw_decode(text, start)
    except (ValueError, TypeError, RecursionError) as exc:
        raise ToolCallError("structured reply contains invalid JSON") from exc
    return reply


def parse_reply(text, tools, tool_choice=None):
    reply = _decode_reply(text)
    if not isinstance(reply, dict) or set(reply) != {"text", "tool_calls"} \
            or not isinstance(reply["text"], str) or not isinstance(reply["tool_calls"], list):
        raise ToolCallError("structured reply must contain text and tool_calls")
    choice = tool_choice if isinstance(tool_choice, dict) else {}
    limit = 1 if choice.get("disable_parallel_tool_use") else MAX_CALLS_PER_TURN
    if len(reply["tool_calls"]) > limit:
        raise ToolCallError("too many tool calls in one structured reply")
    calls = []
    for item in reply["tool_calls"]:
        if not isinstance(item, dict) or set(item) != {"name", "arguments"} \
                or not isinstance(item["arguments"], str):
            raise ToolCallError("structured tool call must contain name and JSON-encoded arguments")
        if len(item["arguments"].encode("utf-8")) > MAX_ENVELOPE_BYTES:
            raise ToolCallError("structured tool arguments exceed the size limit")
        call = validate_host_call({"id": "toolu_" + uuid.uuid4().hex[:22],
                                   "name": item["name"], "input": _decode(item["arguments"])}, tools)
        if choice.get("type") == "tool" and call["name"] != choice.get("name"):
            raise ToolCallError("the model did not call the requested host tool")
        calls.append(call)
    if choice.get("type") in {"any", "required", "tool"} and not calls:
        raise ToolCallError("the model did not make the required host tool call")
    if not calls and not reply["text"].strip():
        raise ToolCallError("the structured reply contained neither an answer nor a tool call")
    return reply["text"], calls


class _ObjectBoundary:
    """Find the first object in linear time, including split JSON escapes."""

    def __init__(self):
        self.started = False
        self.depth = 0
        self.quoted = False
        self.escaped = False

    def feed(self, text):
        for index, char in enumerate(text):
            if not self.started:
                if char.isspace():
                    continue
                if char != "{":
                    raise ToolCallError("structured reply must start with an object")
                self.started = True
            if self.quoted:
                if self.escaped:
                    self.escaped = False
                elif char == "\\":
                    self.escaped = True
                elif char == '"':
                    self.quoted = False
            elif char == '"':
                self.quoted = True
            elif char in "{[":
                self.depth += 1
            elif char in "}]":
                self.depth -= 1
                if self.depth == 0:
                    return index + 1
        return None


def parse_stream(events, *, tools, tool_choice=None, stop_after_object=False):
    chunks = []
    size = 0
    boundary = _ObjectBoundary() if stop_after_object else None
    try:
        for event in events:
            kind = event.get("type")
            if kind == "text_delta":
                text = event.get("text") or ""
                end = boundary.feed(text) if boundary else None
                if end is not None:
                    text = text[:end]
                size += len(text.encode("utf-8"))
                if size > MAX_REPLY_BYTES:
                    raise ToolCallError("structured reply exceeds the size limit")
                if not chunks:
                    yield {"type": "ping"}
                chunks.append(text)
                if end is not None:
                    # Muse's first fully validated object is its handoff. Later
                    # model steps cannot know the host result and are discarded.
                    answer, calls = parse_reply("".join(chunks), tools, tool_choice)
                    if answer:
                        yield {"type": "text_delta", "text": answer}
                    for call in calls:
                        yield {"type": "tool_call", **call}
                    yield {"type": "message_stop", "stop_reason": "tool_use" if calls else "end_turn"}
                    return
            elif kind == "tool_call":
                raise ToolCallError("the CLI used a native tool instead of the structured host response")
            elif kind == "message_stop":
                if event.get("stop_reason") not in {"completed", "success", "stop", "end_turn"}:
                    yield {"type": "error", "message": "The structured CLI reply did not complete successfully."}
                    return
                text, calls = parse_reply("".join(chunks), tools, tool_choice)
                if text:
                    yield {"type": "text_delta", "text": text}
                for call in calls:
                    yield {"type": "tool_call", **call}
                yield {"type": "message_stop", "stop_reason": "tool_use" if calls else "end_turn"}
                return
            else:
                yield event
                if kind == "error":
                    return
        raise ToolCallError("the CLI stream ended before completing its structured reply")
    except ToolCallError as exc:
        yield {"type": "error", "code": "invalid_cli_tool_call",
               "message": f"Invalid CLI host tool call: {exc}."}
    finally:
        close = getattr(events, "close", None)
        if callable(close):
            close()
