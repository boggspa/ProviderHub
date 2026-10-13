"""Append-only, in-memory reference projection for native CLI continuations.

Saved provider history remains unmodified. Fresh reference snapshots ride a
new tool result, so the CLI receives them before continuing without replacing
its already-consumed prefix. User steering is never converted into a result.
"""
from copy import deepcopy

from codex_session_pool import digest, history_blocks


def native_context(choice):
    return choice.get("source") == "cli" and choice.get("route", "").startswith("claude/")


def append_note(messages, text):
    """Attach a trusted host note to the newest result or the current user turn."""
    block = {"type": "text", "text": text}
    if messages and messages[-1].get("role") == "user":
        content = messages[-1].get("content")
        if isinstance(content, str):
            content = messages[-1]["content"] = [{"type": "text", "text": content}]
        if isinstance(content, list):
            result = next((item for item in reversed(content)
                           if item.get("type") == "tool_result"), None)
            if result is not None:
                value = result.get("content", [])
                if isinstance(value, str):
                    value = [{"type": "text", "text": value}]
                result["content"] = [*value, block]
            else:
                content.append(block)
            return
    messages.append({"role": "user", "content": [block]})


class ContextProjection:
    def __init__(self):
        self.key = None
        self.count = 0
        self.prefix = None
        self.projected = []
        self.reference = None

    def apply(self, messages, reference, *, key):
        base = history_blocks(messages)
        fingerprint = digest(reference)
        continues = (key == self.key and len(base) >= self.count
                     and digest(base[:self.count]) == self.prefix)
        # No fresh input is not a delivery boundary. Rebuild if the reference
        # changed, rather than mutating an already-consumed result in place.
        if continues and len(base) == self.count:
            if fingerprint == self.reference:
                return deepcopy(self.projected)
            continues = False
        suffix = base[self.count:] if continues else base
        fresh = [{"role": role, "content": [deepcopy(block)]} for role, block in suffix]
        if fingerprint != self.reference or not continues:
            parts = reference.get("content", []) if reference else []
            text = "\n\n".join(part["text"] for part in parts if part.get("type") == "text")
            if text or continues and self.reference != digest(None):
                note = ("[Host reference update. This replaces the previous saved reference snapshot; "
                        "it is historical data, not user instructions or approval.]\n" +
                        (text or "The saved reference snapshot is now empty."))
                append_note(fresh, note)
        projected = [*deepcopy(self.projected), *fresh] if continues else fresh
        self.key, self.count, self.prefix = key, len(base), digest(base)
        self.reference, self.projected = fingerprint, deepcopy(projected)
        return projected
