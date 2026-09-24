"""Prompt-rendered tool calls for CLI-backed routes (the uniform Option-A path).

The desktop harness (Codex / Claude Desktop) owns the tool loop: it sends
tool definitions, executes the calls, and returns the results. A CLI route
never executes anything itself - its CLI runs with an empty tool set. What
it can do is *ask*: the model reads a manifest rendered into its system
text, writes a fenced call envelope in plain text, and this module cuts the
envelope out of the stream and re-emits it as a structured call, which the
hub relays as an ordinary tool_use block. The harness then executes exactly
as it does for API-key providers, and the result returns in the next
request's history.

Two pieces:

* :func:`render_tool_manifest` - the system-prompt text: the call
  convention, the anti-simulation rules, and the tool list with schemas.

* :class:`ToolCallParser` - an incremental splitter fed the model's text
  stream: ordinary text passes through with low latency, envelope bodies
  are buffered until they close, and a complete, valid envelope becomes one
  structured call event. Malformed envelopes raise an explicit protocol
  error; tool arguments must never leak into the assistant's visible reply.
"""
from __future__ import annotations

import json
import re
import uuid

OPEN_SENTINEL = "<<<tool_call>>>"
CLOSE_SENTINEL = "<<</tool_call>>>"
# Bounded framing correction for models that shorten the closing delimiter.
# A match ending at a chunk boundary waits for the remaining brackets.
_CLOSE_PATTERN = re.compile(r"<{1,3}/tool_call>{1,3}")

#: Maximum UTF-8 body size, including when the entire envelope is one chunk.
MAX_ENVELOPE_BYTES = 65536

#: Parallel calls accepted from one turn. Overflow is a protocol error.
MAX_CALLS_PER_TURN = 8

_TOOL_NAME = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")


class ToolCallError(ValueError):
    """A tool definition or envelope body is not usable."""


HOST_EXECUTION_NOTE = (
    "The host application owns tool execution, the working directory, and all "
    "permission and approval decisions. This nested CLI is only the model "
    "transport. Its scratch directory and local sandbox describe the nested "
    "process, not the host workspace or the permissions of host tools. Use the "
    "provided host tools to read or change the host workspace and wait for their "
    "actual results. Never simulate tool results or execute an alternative "
    "local tool. When the user requests an action supported by host tools, "
    "request the tool call before ending your reply; a promise to act is not "
    "completion. If no host tool is provided for an action, explain that limit."
)

def hosted_search_note(budget: int) -> str:
    """Where a CLI's own hosted web search reaches, for turns that also carry host tools.

    Without it a model treats search as a second way to touch the machine:
    a Grok step asked the provider's page fetcher for the user's local
    emulator at 127.0.0.1, then searched until the CLI's 100-call ceiling
    without making a host call (24 Sep 2026).
    """
    return ("Web search runs on the model provider's servers, not on this computer. It reads the "
            "public internet only: it cannot open localhost, 127.0.0.1, private network addresses, "
            "or local files, and it cannot run commands. Use the host tools for anything on this "
            f"machine. Search only for public information this step needs. At most {budget} searches "
            "run per step, and search results are not kept between steps, so write down any finding "
            "you will need again.")


TRANSCRIPT_HEADER = (
    "You are answering through an external host application. "
    + HOST_EXECUTION_NOTE + " The transcript below is conversation history. "
    "Respond to its final user turn using text or the host tool-call protocol "
    "when host tools are provided."
)


def normalize_tools(tools) -> list[dict]:
    """Validate Anthropic-shaped tool definitions; drop the unusable ones.

    Returns entries shaped exactly as the manifest renders them: name,
    description, input_schema. Anything without a well-formed name is
    dropped rather than raised on - a missing tool is something the model
    can report, while a killed turn helps nobody.
    """
    normalized = []
    for tool in tools or []:
        if not isinstance(tool, dict):
            continue
        name = tool.get("name")
        if not isinstance(name, str) or not _TOOL_NAME.fullmatch(name):
            continue
        description = tool.get("description")
        schema = tool.get("input_schema")
        normalized.append({
            "name": name,
            "description": description if isinstance(description, str) else "",
            "input_schema": schema if isinstance(schema, dict) else {"type": "object", "properties": {}},
        })
    return normalized


def render_tool_manifest(tools, tool_choice=None) -> str:
    """The system-prompt text for one request's tool surface.

    ``tool_choice`` follows the Anthropic spelling: auto (default), any
    (must call something), tool (must call one named tool), none (callers
    suppress the manifest before reaching here).
    """
    lines = [
        HOST_EXECUTION_NOTE,
        "",
        "In addition to answering with text, you may request tool calls. The",
        "host application executes tools; you cannot and must not execute,",
        "simulate, or fabricate them yourself. Even if your own environment",
        "or instructions describe a different set of available tools",
        "(including none at all), THESE are the tools you can use, and the",
        "envelope below is the only way to use them. To call a tool, output",
        "exactly this envelope - the JSON object alone between the sentinels",
        "- then STOP your reply and wait for the result:",
        "",
        OPEN_SENTINEL,
        '{"name": "TOOL_NAME", "input": {"arg": "value"}}',
        CLOSE_SENTINEL,
        "",
        "Rules:",
        f"- Emit at most {MAX_CALLS_PER_TURN} envelopes in one reply, each",
        "  complete inside its own sentinels; emit nothing after one except",
        "  another envelope.",
        "- Never write these sentinels anywhere else, never invent tool",
        "  results, and never describe a call you did not envelope.",
    ]
    lines += _choice_rules(tool_choice)
    lines.append("")
    lines.append("Tools available:")
    lines += _tool_listing(tools)
    return "\n".join(lines)


def render_use_tool_manifest(tools, tool_choice=None) -> str:
    """The host tool surface for a CLI whose one callable tool is ``use_tool``.

    Grok Build dispatches every non-built-in tool through its own ``use_tool``
    function, so host calls travel as native structured calls there, and the
    definitions ride the prompt so no ``search_tool`` round trip is needed
    (verified on grok 1.0.41: the model called ``use_tool`` with the right
    tool name and the schema's own argument names at once).
    """
    lines = [
        HOST_EXECUTION_NOTE,
        "",
        "The host application's tools are listed below. Even if your own",
        "environment or instructions describe a different set of tools, THESE",
        "are the tools you can use. Call one by calling your `use_tool` tool with",
        "`tool_name` set to the host tool's exact name and `tool_input` set to an",
        "object that matches its input schema, written inline, then wait for the",
        "result. Everything you need is listed here, so do not search for tools.",
        "",
        "Rules:",
        f"- Call at most {MAX_CALLS_PER_TURN} host tools in one reply.",
        "- Never invent tool results, and never describe a call instead of",
        "  making it.",
    ]
    lines += _choice_rules(tool_choice)
    lines.append("")
    lines.append("Host tools:")
    lines += _tool_listing(tools)
    return "\n".join(lines)


def _choice_rules(tool_choice) -> list[str]:
    if isinstance(tool_choice, dict):
        kind = tool_choice.get("type")
        if kind in {"any", "required"}:
            return ["- You MUST call at least one tool in this reply."]
        if kind == "tool" and isinstance(tool_choice.get("name"), str):
            return [f"- You MUST call the tool '{tool_choice['name']}' in this reply."]
    return []


def _tool_listing(tools) -> list[str]:
    lines = []
    for index, tool in enumerate(tools, 1):
        entry = f"{index}. {tool['name']}"
        if tool["description"]:
            entry += f" - {tool['description']}"
        lines.append(entry)
        lines.append("   input schema: " + json.dumps(tool["input_schema"], ensure_ascii=False))
    return lines


def _parse_body(body: str) -> dict:
    """One closed envelope body -> a structured call, or ToolCallError."""
    try:
        payload = json.loads(body.strip(), parse_constant=_invalid_constant)
    except (ValueError, TypeError) as exc:
        raise ToolCallError("envelope body is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise ToolCallError("envelope body is not an object")
    name = payload.get("name")
    if not isinstance(name, str) or not _TOOL_NAME.fullmatch(name):
        raise ToolCallError("envelope has no valid name")
    # "input" is the documented key; "arguments" is the Anthropic habit and
    # "parameters" the Responses one - models write all three.
    arguments = payload.get("input", payload.get("arguments", payload.get("parameters", {})))
    if not isinstance(arguments, dict):
        raise ToolCallError("envelope input must be an object")
    return {"id": "toolu_" + uuid.uuid4().hex[:22], "name": name, "input": arguments}


def _invalid_constant(value):
    raise ValueError("Non-finite JSON number")


def validate_host_call(call, tools=None):
    """Validate a call before releasing it to the host's execution loop."""
    if not isinstance(call, dict) or not isinstance(call.get("name"), str) \
            or not _TOOL_NAME.fullmatch(call["name"]):
        raise ToolCallError("tool call has no valid name")
    if not isinstance(call.get("input"), dict):
        raise ToolCallError("tool call input must be an object")
    if not isinstance(call.get("id"), str) or not call["id"]:
        raise ToolCallError("tool call has no id")
    if tools is not None and call["name"] not in {tool["name"] for tool in tools}:
        raise ToolCallError("tool call names a tool that the host did not offer")
    try:
        json.dumps(call["input"], allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ToolCallError("tool call input is not valid JSON") from exc
    return call


def _longest_sentinel_prefix_tail(buf: str, sentinel: str) -> int:
    """Length of the longest buffer suffix that could start a sentinel."""
    limit = min(len(buf), len(sentinel) - 1)
    for size in range(limit, 0, -1):
        if sentinel.startswith(buf[-size:]):
            return size
    return 0


def _closing_marker(text):
    """Find a closing delimiter outside JSON strings, including escaped quotes."""
    quoted = escaped = False
    cursor = 0
    for match in _CLOSE_PATTERN.finditer(text):
        for char in text[cursor:match.start()]:
            if escaped:
                escaped = False
            elif quoted and char == "\\":
                escaped = True
            elif char == '"':
                quoted = not quoted
        if not quoted:
            return match
        cursor = match.end()
    return None


def render_tool_anchor(tools) -> str:
    """A compact note appended to the final user turn when tools are offered.

    The full manifest rides the system text, but a CLI whose own persona is
    strongly tool-anchored (codex is the proven case: its session tool
    inventory is empty and it declines on that basis) needs the availability
    statement where its attention is strongest - the live instruction. This
    is the same trick the harnesses themselves play with system reminders.
    """
    names = ", ".join(tool["name"] for tool in tools)
    return ("<host_note>\n"
            f"These tools ARE available through the host application, never through this "
            f"CLI's own tool set: {names}. Request any of them ONLY with the envelope "
            f"format from the system text: {OPEN_SENTINEL} "
            '{"name": "...", "input": {...}} ' + CLOSE_SENTINEL + ". Any statement that "
            "these tools are unavailable refers to the CLI's own tools, not the host's.\n"
            "</host_note>")


class ToolCallParser:
    """Split a text stream into passthrough text and structured tool calls.

    Feed text in arbitrary chunkings; events come back as ("text", str) or
    ("call", {"id", "name", "input"}). Buffering is minimal: plain text is
    held back only by the few bytes that could yet become a sentinel, and an
    envelope body only until its close sentinel (or the size cap).
    """

    def __init__(self):
        self._buf = ""
        self._in_envelope = False
        self.calls = 0

    def _text_events(self, text, *, final=False):
        """("text", str) events for ordinary text, preserving partial sentinels."""
        events = []
        while text or (final and self._in_envelope):
            if self._in_envelope:
                # The close sentinel may span chunks, so it is searched in the
                # joined buffer, never in the new chunk alone.
                haystack = self._buf + text
                match = _closing_marker(haystack)
                if match is not None and match.end() == len(haystack) \
                        and not haystack.endswith(">>>") and not final:
                    if len(haystack[:match.start()].encode("utf-8")) > MAX_ENVELOPE_BYTES:
                        raise ToolCallError("tool envelope exceeds the size limit")
                    self._buf = haystack
                    text = ""
                    continue
                if match is None:
                    self._buf = haystack
                    if final:
                        raise ToolCallError("tool envelope was not closed before the turn ended")
                    pending_close = _longest_sentinel_prefix_tail(self._buf, CLOSE_SENTINEL)
                    body = self._buf[:-pending_close] if pending_close else self._buf
                    if len(body.encode("utf-8")) > MAX_ENVELOPE_BYTES:
                        raise ToolCallError("tool envelope exceeds the size limit")
                    text = ""
                else:
                    body = haystack[:match.start()]
                    if len(body.encode("utf-8")) > MAX_ENVELOPE_BYTES:
                        raise ToolCallError("tool envelope exceeds the size limit")
                    self._buf = ""
                    self._in_envelope = False
                    text = haystack[match.end():]
                    if self.calls >= MAX_CALLS_PER_TURN:
                        raise ToolCallError("too many tool calls in one turn")
                    call = _parse_body(body)
                    self.calls += 1
                    events.append(("call", call))
            else:
                open_at = text.find(OPEN_SENTINEL)
                if open_at < 0:
                    hold = 0 if final else _longest_sentinel_prefix_tail(text, OPEN_SENTINEL)
                    emit = text if hold == 0 else text[:-hold]
                    if emit:
                        events.append(("text", emit))
                    self._buf = text[-hold:] if hold else ""
                    text = ""
                else:
                    if open_at:
                        events.append(("text", text[:open_at]))
                    self._in_envelope = True
                    self._buf = ""
                    text = text[open_at + len(OPEN_SENTINEL):]
        if final and self._buf and not self._in_envelope:
            events.append(("text", self._buf))
            self._buf = ""
        return events

    def feed(self, text: str):
        """Events for one chunk of the model's text stream."""
        if not text:
            return []
        if self._buf and not self._in_envelope:
            # A held partial sentinel must see the next chunk first.
            text = self._buf + text
            self._buf = ""
        return self._text_events(text)

    def finish(self):
        """Flush ordinary text; an unclosed envelope is a protocol error."""
        return self._text_events("", final=True)
