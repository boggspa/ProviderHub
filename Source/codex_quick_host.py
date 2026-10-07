"""The Hub-facing side of the quick composer.

Provider Hub shows the recent-chat composer in a floating window of its own.
The helper that holds Desktop's DevTools pipe is the only process that can
read the sidebar rows and perform a send, so the Hub talks to it over the
helper's own stdio: commands arrive as JSON lines on stdin, events leave as
JSON lines on stdout. Rows (titles and previews) and send outcomes are
marked private so the helper's log never receives them; prompts only ever
travel inwards and are never echoed.

Commands:  {"command":"hello"}                      the Hub is listening
           {"command":"quick-watch","active":bool}  the panel is (not) showing
           {"command":"quick-send","requestId":s,"threadId":uuid,"prompt":s}
Events:    {"event":"host","connected":true}       (logged)
           {"event":"quick-open"}                   the launcher was clicked
           {"event":"quick-rows","rows":[...]}      (private)
           {"event":"quick-result",...}             (private)
"""
from __future__ import annotations

import json
import os
import re
import select

from codex_quick_window import send_from_host_expression

_UUID = re.compile(r"^[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$")
_PROMPT_LIMIT = 65536
_LINE_LIMIT = 1 << 20

#: Events whose payload is content from the user's chats: stdout only.
PRIVATE_EVENTS = frozenset({"quick-rows", "quick-result"})


class HostInput:
    """JSON lines from a descriptor, read without ever blocking the pipe loop."""

    def __init__(self, fd: int = 0):
        self.fd = fd
        self.buffer = b""
        self.closed = False

    def poll(self) -> list[dict]:
        if self.closed:
            return []
        try:
            ready, _, _ = select.select([self.fd], [], [], 0)
        except (OSError, ValueError):
            self.closed = True
            return []
        if not ready:
            return []
        try:
            chunk = os.read(self.fd, 65536)
        except OSError:
            chunk = b""
        if not chunk:
            self.closed = True
            return self._drain(final=True)
        self.buffer += chunk
        if len(self.buffer) > _LINE_LIMIT:
            self.buffer = b""
            return []
        return self._drain()

    def _drain(self, final: bool = False) -> list[dict]:
        messages = []
        while b"\n" in self.buffer:
            raw, self.buffer = self.buffer.split(b"\n", 1)
            message = _parse(raw)
            if message is not None:
                messages.append(message)
        if final and self.buffer:
            message = _parse(self.buffer)
            self.buffer = b""
            if message is not None:
                messages.append(message)
        return messages


def _parse(raw: bytes):
    try:
        value = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    return value if isinstance(value, dict) else None


class QuickHost:
    """Serve the Hub's panel: rows while it watches, sends on request."""

    def __init__(self, emit=None, pipe=None):
        self.emit = emit or (lambda event: None)
        self.pipe = pipe
        self.connected = False
        self.watching = False
        self.main_sessions: set[str] = set()
        self.composer_session: str | None = None
        self.pending: dict[int, tuple[str, str]] = {}
        self.last_rows: str | None = None

    # -- wiring from the accent bridge -------------------------------------
    def attach_main(self, session_id: str) -> None:
        self.main_sessions.add(session_id)

    def detach(self, session_id: str) -> None:
        self.main_sessions.discard(session_id)
        if session_id == self.composer_session:
            self.composer_session = None

    def is_watching(self) -> bool:
        return self.connected and self.watching

    def request_open(self) -> bool:
        """The launcher was clicked: the Hub shows its panel if it is there."""
        if not self.connected:
            return False
        self.emit({"event": "quick-open"})
        return True

    def update(self, session_id: str, rows: list) -> None:
        self.composer_session = session_id
        if not self.is_watching():
            return
        key = json.dumps(rows, sort_keys=True)
        if key == self.last_rows:
            return
        self.last_rows = key
        self.emit({"event": "quick-rows", "rows": rows})

    def disconnected(self) -> None:
        """The Hub's end of stdin closed: fall back to helper-only behaviour."""
        if self.connected:
            self.emit({"event": "host", "connected": False})
        self.connected = False
        self.watching = False
        self.last_rows = None

    # -- commands ----------------------------------------------------------
    def command(self, message: dict) -> None:
        kind = message.get("command")
        if kind == "hello":
            self.connected = True
            self.emit({"event": "host", "connected": True})
        elif kind == "quick-watch":
            self.watching = message.get("active") is True
            if not self.watching:
                self.last_rows = None
            elif self.connected:
                self.last_rows = None  # resend the rows the panel missed
        elif kind == "quick-send":
            self._send(message)

    def _send(self, message: dict) -> None:
        request_id = message.get("requestId")
        thread_id = message.get("threadId")
        prompt = message.get("prompt")
        if not isinstance(request_id, str) or not request_id:
            return
        if not isinstance(thread_id, str) or not _UUID.fullmatch(thread_id):
            self._result(request_id, str(thread_id) if isinstance(thread_id, str) else "", False,
                         "A valid local thread is required.")
            return
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > _PROMPT_LIMIT:
            self._result(request_id, thread_id, False, "A non-empty prompt of at most 64 KiB is required.")
            return
        session = self.composer_session or next(iter(sorted(self.main_sessions)), None)
        if session is None or self.pipe is None:
            self._result(request_id, thread_id, False, "Codex's window is not available. Your draft is kept.")
            return
        identifier = self.pipe.send("Runtime.evaluate", {"expression": send_from_host_expression(thread_id, prompt),
                                                         "returnByValue": True, "awaitPromise": True, "timeout": 60000},
                                    session_id=session)
        self.pending[identifier] = (request_id, thread_id)

    def handle(self, message: dict) -> bool:
        identifier = message.get("id")
        if identifier not in self.pending:
            return False
        request_id, thread_id = self.pending.pop(identifier)
        error = message.get("error")
        result = message.get("result") or {}
        value = ((result.get("result") or {}).get("value")) if not error else None
        ok = isinstance(value, dict) and value.get("ok") is True
        reason = value.get("reason") if isinstance(value, dict) and isinstance(value.get("reason"), str) else None
        if not ok and not reason:
            reason = ("Sending is unavailable in this Desktop version. Draft kept." if value is None and not error
                      else "Send failed. Your draft is kept.")
        self._result(request_id, thread_id, ok, reason)
        return True

    def _result(self, request_id: str, thread_id: str, ok: bool, reason: str | None) -> None:
        self.emit({"event": "quick-result", "requestId": request_id, "threadId": thread_id, "ok": ok, "reason": reason})


__all__ = ["HostInput", "QuickHost", "PRIVATE_EVENTS"]
