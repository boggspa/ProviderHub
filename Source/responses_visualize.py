"""Inline visualization references for Codex desktop.

The desktop expects a sentinel-wrapped reference on its own line. Some
provider output omits the sentinels and arrives as

    visualize{"path":"<thread visualization dir>/<title>.html"}

in the final reply. The installed skill and app bundle's line matcher use
these private-use Unicode delimiters:

    U+E200 visualize U+E202 {"path":"..."} U+E201

The reported Claude Fable reply (10 October 2026, ``busiest-days.html``)
omitted them; a prior Codex route reply preserved them. This is a narrow
compatibility repair, not evidence that a provider always omits them.
Already wrapped lines, code examples and malformed references stay intact.
"""
from __future__ import annotations

import json
import re

OPEN, SEPARATOR, CLOSE = "", "", ""
KEYWORD = "visualize"
HEAD = KEYWORD + "{"

#: The desktop's reference line without its sentinels: up to three leading
#: spaces, the keyword, one JSON object, trailing blanks.
BARE_LINE = re.compile(r"^( {0,3})" + KEYWORD + r"(\{.*\})([ \t]*)$")
#: A fenced-code opener. The fence closes on a line holding at least as many
#: of the same character and nothing else, exactly as the desktop checks.
FENCE_OPEN = re.compile(r"^ {0,3}(`{3,}|~{3,})")


def _invalid_json_constant(value):
    # JSON.parse in the desktop rejects Python's NaN/Infinity extension.
    raise ValueError(value)


def wrap_line(line):
    """Return ``line`` in sentinel form when it is a bare reference."""
    match = BARE_LINE.match(line)
    if match is None:
        return line
    indent, body, tail = match.groups()
    try:
        value = json.loads(body, parse_constant=_invalid_json_constant)
    except (ValueError, RecursionError):
        return line
    path = value.get("path") if isinstance(value, dict) else None
    if not isinstance(path, str) or not path.strip():
        return line
    return indent + OPEN + KEYWORD + SEPARATOR + body + CLOSE + tail


def wrap_text(text):
    """Wrap every bare reference line of one complete text block."""
    if not isinstance(text, str) or HEAD not in text:
        return text
    rewriter = VisualizeRewriter()
    return rewriter.feed(text) + rewriter.flush()


def wrap_message_item(item):
    """Wrap the text parts of one Responses message item, in place."""
    if (not isinstance(item, dict) or item.get("type") != "message"
            or item.get("role", "assistant") != "assistant"):
        return
    for part in item.get("content") or []:
        if (isinstance(part, dict) and part.get("type") == "output_text"
                and isinstance(part.get("text"), str)):
            part["text"] = wrap_text(part["text"])


def wrap_output(output):
    """Wrap the message items of a Responses ``output`` list, in place."""
    for item in output if isinstance(output, list) else []:
        wrap_message_item(item)


class VisualizeRewriter:
    """Line-buffered rewrite for streamed text.

    ``feed`` returns the text that may be emitted now; ``flush`` returns the
    remainder once the block ends. Only a line that could still become a
    bare reference is held back, so ordinary prose streams as it arrives,
    and however the block is chunked the emitted text adds up to
    ``wrap_text`` of the whole block.
    """

    def __init__(self):
        self.line = ""  # the current line so far, without its newline
        self.sent = 0  # how much of it has already been emitted
        self.fence = None  # (character, length) while inside fenced code

    def feed(self, text):
        out = []
        while text:
            cut = text.find("\n")
            if cut >= 0:
                self.line += text[:cut]
                text = text[cut + 1:]
                out.append(self._finish() + "\n")
                continue
            self.line += text
            text = ""
            if self.sent or not self._candidate():
                out.append(self.line[self.sent:])
                self.sent = len(self.line)
        return "".join(out)

    def flush(self):
        return self._finish() if self.line else ""

    def _candidate(self):
        """Could the partial line still become a bare reference?"""
        stripped = self.line.lstrip(" ")
        if len(self.line) - len(stripped) > 3:
            return False
        return stripped.startswith(HEAD) or HEAD.startswith(stripped)

    def _finish(self):
        """Complete the current line; return what has not been emitted."""
        line, sent = self.line, self.sent
        self.line, self.sent = "", 0
        body, ending = (line[:-1], "\r") if line.endswith("\r") else (line, "")
        if self.fence is not None:
            if self._closes(body):
                self.fence = None
            return line[sent:]
        if not sent:
            # A reference is held whole while it streams (every prefix of
            # the line is a candidate), so a partly emitted line is never one.
            wrapped = wrap_line(body)
            if wrapped != body:
                return wrapped + ending
        match = FENCE_OPEN.match(body)
        if match is not None:
            self.fence = (match.group(1)[0], len(match.group(1)))
        return line[sent:]

    def _closes(self, body):
        character, length = self.fence
        pattern = r" {0,3}" + re.escape(character) + "{" + str(length) + r",}[ \t]*"
        return re.fullmatch(pattern, body) is not None
