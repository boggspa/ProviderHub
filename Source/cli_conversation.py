"""Exact host-history receipts for provider-specific completed-turn transports.

This module matches history only; it never resumes a process or executes tools.
Native protocols and permission checks stay in their respective adapters.
"""
from copy import deepcopy

from codex_session_pool import digest, history_blocks


def scope(request):
    value = request.get("conversation")
    return value if isinstance(value, str) and 0 < len(value) <= 200 else None


def canonical(blocks, *, generated_ids=False):
    result = []
    for block in blocks:
        kind = block.get("type")
        if kind in {"text", "thinking"}:
            text = block.get(kind)
            if not isinstance(text, str): return None
            if not text: continue
            if result and result[-1].get("type") == kind: result[-1][kind] += text
            else: result.append({"type": kind, kind: text})
        elif kind == "tool_use":
            keys = ("type", "name", "input") if generated_ids else ("type", "id", "name", "input")
            result.append({key: block.get(key) for key in keys})
        else: return None
    return result


class Receipt:
    def __init__(self):
        self.blocks = []
        self.supported = True

    def observe(self, event):
        kind = event.get("type")
        if kind in {"text_delta", "thinking_delta"}:
            field = "text" if kind == "text_delta" else "thinking"
            self.blocks.append({"type": field, field: event.get("text", "")})
        elif kind == "tool_call":
            self.blocks.append({"type": "tool_use", **{key: event.get(key) for key in ("id", "name", "input")}})
        elif kind in {"error", "web_search", "image_generation"}: self.supported = False

    def completed(self, history, *, generated_ids=False):
        blocks = canonical(self.blocks, generated_ids=generated_ids)
        if not self.supported or not blocks: return None
        prefix = history_blocks(history)
        return {"completed": True, "prefix": digest(prefix), "prefix_count": len(prefix),
                "reply": digest(blocks), "generated_ids": generated_ids}


def completed_delta(lease, history):
    pending = lease.pending
    if not pending or not pending.get("completed") or not isinstance(history, list): return None
    blocks = history_blocks(history)
    count = pending["prefix_count"]
    if len(blocks) <= count or digest(blocks[:count]) != pending["prefix"]: return None
    cursor = count
    reply = []
    while cursor < len(blocks) and blocks[cursor][0] == "assistant":
        reply.append(blocks[cursor][1]); cursor += 1
    actual = canonical(reply, generated_ids=pending.get("generated_ids", False))
    if actual is None or digest(actual) != pending["reply"]: return None
    calls = [b.get("id") for b in reply if b.get("type") == "tool_use"]
    if any(not isinstance(i, str) or not i for i in calls) or len(set(calls)) != len(calls): return None
    results = set()
    suffix = []
    for role, block in blocks[cursor:]:
        if role != "user" or block.get("type") not in {"text", "tool_result"}: return None
        if block.get("type") == "tool_result":
            identifier = block.get("tool_use_id")
            if identifier not in calls or identifier in results: return None
            value = block.get("content", "")
            if not isinstance(value, str) and not (isinstance(value, list) and all(
                    isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str)
                    for part in value)): return None
            results.add(identifier)
        suffix.append({"role": role, "content": [deepcopy(block)]})
    if not suffix or results != set(calls): return None
    return suffix
