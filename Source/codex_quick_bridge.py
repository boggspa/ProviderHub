"""Bounded, read-only preview polling over the desktop's private DevTools pipe.

Submission stays in the renderer's Desktop action adapter. The worker only
reads the exact recent-thread identities reported by an open quick composer.
No transcript text is written to helper events or persistent diagnostics.
"""
from __future__ import annotations

import json
import re
import time


_UUID = re.compile(r"^[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$")
_GUARD = "if (window !== window.top || !/^app:\\/\\/-\\//.test(String(location.href))) return null; "
# The read poll also takes the composer's last send report and any request
# for the separate window, so one evaluate per cycle lists the rows (while
# the popover or that window is open), records how the last send ended and
# hears the launcher. Older composers without pollHost return a bare list.
_READ = ("const composer = window.__providerHubQuickComposer; if (!composer) return null; "
         "if (composer.pollHost) return composer.pollHost({ windowOpen: %s }); "
         "return { targets: composer.recentThreadTargets?.(), report: composer.takeSendReport?.() ?? null }; ")
_REPORT_STRINGS = ("outcome", "code", "observedBundle", "moduleError", "scopeError")


def _targets(value):
    if isinstance(value, dict):
        value = value.get("targets")
    if not isinstance(value, list):
        return []
    result, seen = [], set()
    for item in value[:10]:
        if not isinstance(item, dict):
            continue
        thread_id = item.get("threadId")
        if (item.get("hostId") != "local" or item.get("kind") != "local"
                or not isinstance(thread_id, str) or not _UUID.fullmatch(thread_id)):
            continue
        identity = ("local", "local", thread_id)
        if identity in seen:
            continue
        seen.add(identity)
        result.append({"threadId": thread_id, "hostId": "local", "kind": "local"})
    return result


def _rows(value):
    """The first ten rows as the page described them, with title and activity."""
    if isinstance(value, dict):
        value = value.get("targets")
    if not isinstance(value, list):
        return []
    rows, seen = [], set()
    for item in value[:10]:
        if not isinstance(item, dict):
            continue
        thread_id, host_id, kind = item.get("threadId"), item.get("hostId"), item.get("kind")
        if not all(isinstance(part, str) for part in (thread_id, host_id, kind)):
            continue
        identity = (kind, host_id, thread_id)
        if identity in seen:
            continue
        seen.add(identity)
        title = item.get("title")
        accent = item.get("activeAccent")
        rows.append({"threadId": thread_id, "hostId": host_id, "kind": kind,
                     "title": title if isinstance(title, str) and title else "Untitled chat",
                     "supported": item.get("supported") is True and host_id == "local" and kind == "local"
                     and bool(_UUID.fullmatch(thread_id)),
                     "active": item.get("active") is True,
                     "activeAccent": accent if isinstance(accent, str) and re.fullmatch(r"#[0-9a-fA-F]{6}", accent) else None})
    return rows


def _report(value):
    """The content-free send report a read poll carried, or None.

    Only known diagnostic fields are kept, strings are capped, and the
    thread identity must be a UUID; prompts and previews never travel here.
    """
    if not isinstance(value, dict):
        return None
    report = value.get("report")
    if not isinstance(report, dict) or report.get("outcome") not in ("sent", "failed"):
        return None
    event = {}
    for name in _REPORT_STRINGS:
        item = report.get(name)
        if isinstance(item, str):
            event[name] = item[:200]
    thread_id = report.get("threadId")
    if isinstance(thread_id, str) and _UUID.fullmatch(thread_id):
        event["threadId"] = thread_id
    search = report.get("scopeSearch")
    if isinstance(search, dict):
        fibers = search.get("fibers")
        event["scopeSearch"] = {"fibers": fibers if isinstance(fibers, int) and not isinstance(fibers, bool) else None,
                                "truncated": search.get("truncated") is True}
    at = report.get("at")
    if isinstance(at, (int, float)) and not isinstance(at, bool):
        event["at"] = int(at)
    return event


class QuickComposerBridge:
    def __init__(self, pipe, previews, emit=None, window=None):
        self.pipe = pipe
        self.previews = previews
        self.emit = emit or (lambda event: None)
        self.window = window
        self.pending = {}
        self.sessions = set()
        self.next_poll = 0.0

    def _evaluate(self, session_id, method, argument, kind):
        if kind == "read":
            window_open = self.window is not None and self.window.is_open()
            body = _READ % ("true" if window_open else "false")
        else:
            body = f"return window.__providerHubQuickComposer?.{method}?.({argument}); "
        expression = "(() => { " + _GUARD + body + "})()"
        identifier = self.pipe.send("Runtime.evaluate", {"expression": expression,
                                    "returnByValue": True, "timeout": 1000}, session_id=session_id)
        self.pending[identifier] = (kind, session_id, time.monotonic())

    def refresh(self, sessions):
        self.sessions = set(sessions)
        now = time.monotonic()
        for identifier, (_, session_id, sent) in list(self.pending.items()):
            if session_id not in self.sessions or now - sent > 10.0:
                del self.pending[identifier]
        if now < self.next_poll:
            return
        self.next_poll = now + 2.0
        for session_id in sorted(self.sessions):
            if any(owner == session_id for _, owner, _ in self.pending.values()):
                continue
            self._evaluate(session_id, "recentThreadTargets", "", "read")

    def handle(self, message):
        identifier = message.get("id")
        pending = self.pending.pop(identifier, None)
        if pending is None:
            return False
        kind, session_id, _ = pending
        if session_id not in self.sessions:
            return True
        result = message.get("result") or {}
        if message.get("error") or result.get("exceptionDetails"):
            # Only stage and session are diagnostic. Exceptions returned by
            # renderer actions can contain user text and are never logged here.
            self.emit({"event": "quick-composer", "stage": kind, "session": session_id, "available": False})
            return True
        if kind == "write":
            return True
        value = (result.get("result") or {}).get("value")
        report = _report(value)
        if report is not None:
            self.emit({"event": "quick-composer", "stage": "send", "session": session_id, **report})
        if self.window is not None and isinstance(value, dict) and value.get("windowRequest") is True:
            self.window.open()
        targets = _targets(value)
        if not targets:
            if self.window is not None and self.window.is_open() and isinstance(value, dict):
                self.window.update(session_id, [])
            return True  # Closed or no supported recent rows: no content read.
        records = []
        try:
            records = self.previews(targets)
        except Exception:
            self.emit({"event": "quick-composer", "stage": "previews", "session": session_id, "available": False})
        wanted = {(item["kind"], item["hostId"], item["threadId"]) for item in targets}
        found = {}
        for record in records if isinstance(records, list) else []:
            if not isinstance(record, dict):
                continue
            identity = (record.get("kind"), record.get("hostId"), record.get("threadId"))
            if any(not isinstance(part, str) for part in identity) or identity not in wanted:
                continue
            preview = record.get("preview")
            found[identity] = " ".join(preview.split())[:512] if isinstance(preview, str) else None
        payload = [{**item, "preview": found.get((item["kind"], item["hostId"], item["threadId"]))}
                   for item in targets]
        self._evaluate(session_id, "setThreadPreviews", json.dumps(payload), "write")
        if self.window is not None and self.window.is_open():
            # The window shows the same rows with their titles and activity,
            # which only the page knows; the previews come from the worker.
            rows = _rows(value)
            self.window.update(session_id, [{**row, "preview": found.get((row["kind"], row["hostId"], row["threadId"]))}
                                             for row in rows])
        return True
