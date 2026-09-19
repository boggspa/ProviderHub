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
  structured call event. Anything malformed fails OPEN to plain text, so a
  model that misuses the convention degrades to a weird paragraph, never to
  a dropped turn.
"""
from __future__ import annotations

import json
import re
import uuid

OPEN_SENTINEL = "<<<tool_call>>>"
CLOSE_SENTINEL = "<<</tool_call>>>"

#: One envelope's maximum body. Past this the buffer fails open to text:
#: a runaway envelope is a model misreading the convention, not a big call.
MAX_ENVELOPE_BYTES = 65536

#: Parallel calls accepted from one turn. Past this, further envelopes pass
#: through as text: an eighth-plus envelope is almost always the model
#: looping on the convention rather than a real plan.
MAX_CALLS_PER_TURN = 8

_TOOL_NAME = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")


class ToolCallError(ValueError):
    """A tool definition or envelope body is not usable."""


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
        "In addition to answering with text, you may request tool calls. The",
        "host application executes tools; you cannot and must not execute,",
        "simulate, or fabricate them yourself. To call a tool, output exactly",
        "this envelope - the JSON object alone between the sentinels - then",
        "STOP your reply and wait for the result:",
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
    if isinstance(tool_choice, dict):
        kind = tool_choice.get("type")
        if kind in {"any", "required"}:
            lines.append("- You MUST call at least one tool in this reply.")
        elif kind == "tool" and isinstance(tool_choice.get("name"), str):
            lines.append(f"- You MUST call the tool '{tool_choice['name']}' in this reply.")
    lines.append("")
    lines.append("Tools available:")
    for index, tool in enumerate(tools, 1):
        entry = f"{index}. {tool['name']}"
        if tool["description"]:
            entry += f" - {tool['description']}"
        lines.append(entry)
        lines.append("   input schema: " + json.dumps(tool["input_schema"], ensure_ascii=False))
    return "\n".join(lines)


def _parse_body(body: str) -> dict:
    """One closed envelope body -> a structured call, or ToolCallError."""
    try:
        payload = json.loads(body.strip())
    except (ValueError, TypeError) as exc:
        raise ToolCallError(f"envelope body is not JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ToolCallError("envelope body is not an object")
    name = payload.get("name")
    if not isinstance(name, str) or not _TOOL_NAME.fullmatch(name):
        raise ToolCallError("envelope has no valid name")
    arguments = payload.get("input", payload.get("arguments", {}))
    if not isinstance(arguments, dict):
        raise ToolCallError("envelope input must be an object")
    return {"id": "toolu_" + uuid.uuid4().hex[:22], "name": name, "input": arguments}


def _longest_sentinel_prefix_tail(buf: str, sentinel: str) -> int:
    """Length of the longest buffer suffix that could start a sentinel."""
    limit = min(len(buf), len(sentinel) - 1)
    for size in range(limit, 0, -1):
        if sentinel.startswith(buf[-size:]):
            return size
    return 0


class ToolCallParser:
    """Split a text stream into passthrough text and structured tool calls.

    Feed text in arbitrary chunkings; events come back as ("text", str) or
    ("call", {"id", "name", "input"}). Buffering is minimal: plain text is
    held back only by the few bytes that could yet become a sentinel, and an
    envelope body only until its close sentinel (or the fail-open cap).
    """

    def __init__(self):
        self._buf = ""
        self._in_envelope = False
        self.calls = 0

    def _text_events(self, text, *, final=False):
        """("text", str) events for ordinary text, preserving partial sentinels."""
        events = []
        while text:
            if self._in_envelope:
                # The close sentinel may span chunks, so it is searched in the
                # joined buffer, never in the new chunk alone.
                haystack = self._buf + text
                close = haystack.find(CLOSE_SENTINEL)
                if close < 0:
                    self._buf = haystack
                    if len(self._buf) > MAX_ENVELOPE_BYTES:
                        events.append(("text", self._buf))
                        self._buf = ""
                        self._in_envelope = False
                    text = ""
                else:
                    body = haystack[:close]
                    self._buf = ""
                    self._in_envelope = False
                    text = haystack[close + len(CLOSE_SENTINEL):]
                    if self.calls >= MAX_CALLS_PER_TURN:
                        events.append(("text", OPEN_SENTINEL + body + CLOSE_SENTINEL))
                        continue
                    try:
                        call = _parse_body(body)
                    except ToolCallError:
                        # Fail open: the convention was misread, so the text
                        # stands as written rather than vanishing.
                        events.append(("text", OPEN_SENTINEL + body + CLOSE_SENTINEL))
                    else:
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
        """Flush at stream end; an unclosed envelope fails open as written."""
        if self._in_envelope:
            buffered = OPEN_SENTINEL + self._buf
            self._buf = ""
            self._in_envelope = False
            return [("text", buffered)]
        return self._text_events("", final=True)
