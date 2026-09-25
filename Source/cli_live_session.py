"""A vendor CLI kept waiting inside its host tool calls between host requests.

Shared by the CLI routes that bridge their host tools (claude_cli_agent,
grok_cli_agent); cli_host_bridge is the socket the waiting calls sit on. A
live session is a CLI process whose MCP server forwarded the model's host
calls to the hub and is blocked on them. Its pool lease records exactly what
was handed to the host. The host's next request continues that process only
when it is that handoff's continuation and nothing else. The adapter then
answers the waiting calls with the results and streams on from the same
process. Everything here is decided before an event reaches the client, so a
request that cannot continue a session replays the conversation into a
fresh process instead.
"""
from __future__ import annotations

import shutil
import time

from cli_images import CliImageError, normalize_image
from cli_lifecycle import cleanup_after_exit
from cli_tool_call import ToolCallError
from codex_session_pool import digest, history_blocks


class LiveSession:
    """A CLI process that waits inside its host tool calls between host requests.

    A pool lease owns it: ``returncode`` lets the pool retire a CLI that has
    exited, and ``close`` is the lease's disposal. ``interrupt`` is the
    adapter's SIGTERM for its child.
    """

    def __init__(self, session, workspace, bridge, state, *, interrupt):
        self.session = session
        self.workspace = workspace
        self.bridge = bridge
        self.state = state
        self.interrupt = interrupt
        self.closed = False

    @property
    def returncode(self):
        return getattr(self.session, "returncode", None)

    def resume(self, results) -> bool:
        """Answer every waiting call with the host's result; False when the CLI cannot take them."""
        if self.returncode is not None:
            return False
        self.state.next_leg()
        return all(self.bridge.deliver(identifier, result) for identifier, result in results.items())

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        # The CLI goes first, so the bridge dropping its calls can never reach
        # the model as tool results it would act on.
        self.interrupt(self.session)
        self.bridge.close()
        try:
            self.session.close()
        except Exception:  # noqa: BLE001 - disposal must never raise
            pass
        cleanup_after_exit(self.session, self._release)

    def _release(self) -> None:
        if self.state.stderr_handle is not None:
            self.state.stderr_handle.close()
        shutil.rmtree(self.workspace, ignore_errors=True)


def dispose(lease) -> None:
    """A SessionPool disposal for leases that hold a LiveSession."""
    if lease.session is not None:
        lease.session.close()


def mcp_result(block, *, images=True) -> dict | None:
    """A host tool_result as the MCP result its waiting call returns, or None if it cannot be one.

    ``images`` False refuses image content, for a CLI whose MCP client has
    not been shown to pass images to its model.
    """
    content = block.get("content")
    items = []
    if isinstance(content, str):
        if content:
            items.append({"type": "text", "text": content})
    elif isinstance(content, list):
        for part in content:
            kind = part.get("type") if isinstance(part, dict) else None
            if kind == "text":
                if part.get("text"):
                    items.append({"type": "text", "text": str(part["text"])})
            elif kind in {"image", "input_image"} and images:
                try:
                    source = normalize_image(part)["source"]
                except CliImageError:
                    return None
                items.append({"type": "image", "data": source["data"], "mimeType": source["media_type"]})
            else:
                return None
    elif content is not None:
        return None
    return {"content": items, "isError": block.get("is_error") is True}


def continuation(lease, history, *, images=True) -> dict | None:
    """The host's results for a waiting CLI, when ``history`` continues exactly from its handoff.

    The history up to that handoff must be unchanged. After it come the
    handoff's own message - any text or reasoning, and each call handed over
    once - and then one result per call and nothing else. New user input goes
    to a fresh turn instead, where it arrives as the user's own words rather
    than inside a tool result.
    """
    pending = lease.pending
    if not pending or "prefix" not in pending or not isinstance(history, list):
        return None
    blocks = history_blocks(history)
    count = pending["prefix_count"]
    if len(blocks) <= count or digest(blocks[:count]) != pending["prefix"]:
        return None
    calls = pending["calls"]
    made, results = set(), {}
    for role, block in blocks[count:]:
        if not isinstance(block, dict):
            return None
        if role == "assistant":
            if results:
                return None
            if block.get("type") == "tool_use":
                identifier = block.get("id")
                call = calls.get(identifier)
                if call is None or identifier in made or block.get("name") != call["name"]:
                    return None
                made.add(identifier)
            continue
        if role != "user" or block.get("type") != "tool_result":
            return None
        identifier = block.get("tool_use_id")
        if identifier not in calls or identifier in results:
            return None
        result = mcp_result(block, images=images)
        if result is None:
            return None
        results[identifier] = result
    if made != set(calls) or set(results) != set(calls):
        return None
    return results


def answers_itself(payload, calls) -> bool:
    """Whether a stream line is the CLI's own tool result for one of ``calls``."""
    if not isinstance(payload, dict) or payload.get("type") != "user":
        return False
    content = (payload.get("message") or {}).get("content")
    return isinstance(content, list) and any(
        isinstance(block, dict) and block.get("type") == "tool_result" and block.get("tool_use_id") in calls
        for block in content)


def confirm(live, deadline, *, events, translate, seconds) -> bool:
    """Whether the CLI is waiting in the bridge on exactly the calls its message made.

    The message has already ended, so the lines read meanwhile only settle its
    record (``translate`` is the adapter's, and its output is dropped). If the
    CLI answers a call itself, exits, or is not waiting on every call within
    ``seconds``, the handoff goes ahead without a live session.
    """
    state, session = live.state, live.session
    limit = min(deadline, time.monotonic() + seconds)
    while True:
        verdict = live.bridge.check(state.host_calls)
        if verdict != "waiting":
            return verdict == "ready"
        if time.monotonic() >= limit or live.returncode is not None:
            return False
        for kind, payload in events(session, timeout=0.05):
            if kind == "raw":
                state.raw_lines.append(payload)
            elif answers_itself(payload, state.host_calls):
                return False
            else:
                try:
                    for _ in translate(payload, state):
                        pass
                except ToolCallError:
                    return False
                if state.failure:
                    return False
            break


def hold(live, lease, history, deadline, **confirming) -> bool:
    """At a host handoff: keep the CLI waiting in its calls when the bridge shows exactly them."""
    if not confirm(live, deadline, **confirming):
        return False
    blocks = history_blocks(history)
    lease.pending = {"calls": {identifier: dict(call) for identifier, call in live.state.host_calls.items()},
                     "prefix": digest(blocks), "prefix_count": len(blocks)}
    return True
