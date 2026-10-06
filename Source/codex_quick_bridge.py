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
# The read poll also takes the composer's last send report, so one evaluate
# per cycle both lists the open popover's rows and records how the last
# send ended. Older composers without the method return a bare list.
_READ = ("const composer = window.__providerHubQuickComposer; if (!composer) return null; "
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
    def __init__(self, pipe, previews, emit=None):
        self.pipe = pipe
        self.previews = previews
        self.emit = emit or (lambda event: None)
        self.pending = {}
        self.sessions = set()
        self.next_poll = 0.0

    def _evaluate(self, session_id, method, argument, kind):
        body = _READ if kind == "read" else f"return window.__providerHubQuickComposer?.{method}?.({argument}); "
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
        targets = _targets(value)
        if not targets:
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
        return True
