"""Exact host-history receipts for a scoped, streaming Claude conversation.

Only the newly appended user blocks go to stdin. Native assistant messages
and MCP tool results already belong to the CLI's conversation; replaying them
as text would change its cacheable prefix. No authorization is inferred from
the contents of a message, and user instructions never become tool results.
"""
from copy import deepcopy

from cli_images import CliImageError, normalize_image, image_label
from cli_live_session import continuation
from codex_session_pool import digest, history_blocks


def _canonical(blocks):
    result = []
    for block in blocks:
        kind = block.get("type")
        if kind in {"text", "thinking"}:
            value = block.get(kind, "")
            if not isinstance(value, str): return None
            if not value: continue
            if result and result[-1].get("type") == kind:
                result[-1][kind] += value
            else:
                result.append({"type": kind, kind: value})
        elif kind == "tool_use":
            result.append({key: block.get(key) for key in ("type", "id", "name", "input")})
        else:
            return None
    return result


class ReplyReceipt:
    """The host-visible reply, excluding usage and transport metadata."""
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
        elif kind in {"web_search", "image_generation", "error"}:
            # The outer relay assigns server-tool identities. Until those can
            # be proved here, the next query must reconstruct portable history.
            self.supported = False

    def completed(self, history):
        blocks = _canonical(self.blocks)
        if not self.supported or not blocks: return None
        prefix = history_blocks(history)
        return {"completed": True, "prefix": digest(prefix), "prefix_count": len(prefix),
                "reply": digest(blocks)}


def advance(lease, history):
    """Return (pending MCP results, new user content), or refuse changed history.

    A pending handoff can include extra genuine user messages after all of its
    results. A completed native query requires the exact reply delivered to
    the host before any subsequent user content is accepted.
    """
    pending = lease.pending
    if not pending or not isinstance(history, list): return None
    blocks = history_blocks(history)
    count = pending.get("prefix_count", -1)
    if count < 0 or len(blocks) <= count or digest(blocks[:count]) != pending.get("prefix"):
        return None
    cursor = count
    replies = []
    while cursor < len(blocks) and blocks[cursor][0] == "assistant":
        replies.append(blocks[cursor][1]); cursor += 1
    if pending.get("completed"):
        canonical = _canonical(replies)
        if canonical is None or digest(canonical) != pending["reply"]: return None
        results = {}
    else:
        if "reply" not in pending or digest(_canonical(replies)) != pending["reply"]:
            return None
        while cursor < len(blocks) and blocks[cursor][0] == "user" and blocks[cursor][1].get("type") == "tool_result":
            cursor += 1
        prefix = [{"role": role, "content": [block]} for role, block in blocks[:cursor]]
        results = continuation(lease, prefix)
        if results is None: return None
    content = []
    image_index = 0
    for role, block in blocks[cursor:]:
        if role != "user": return None
        if block.get("type") == "text" and isinstance(block.get("text"), str):
            content.append({"type": "text", "text": block["text"]})
        elif block.get("type") in {"image", "input_image"}:
            try:
                image = normalize_image(block)
                image_index += 1
                content.extend([{"type": "text", "text": image_label(image, image_index) + "."},
                                {"type": "image", "source": image["source"]}])
            except CliImageError: return None
        else:
            return None
    if pending.get("completed") and not content: return None
    return results, deepcopy(content)
