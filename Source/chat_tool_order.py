"""Keep OpenAI/Mistral tool messages immediately after their assistant calls."""
from __future__ import annotations

import json


def repair_openai_tool_order(messages: list) -> list:
    """Move tool results to follow the assistant tool_calls they belong to.

    Claude Desktop often sends user text before tool_result in the same turn.
    Chat Completions then emit role=user followed by role=tool, which Mistral
    rejects: Unexpected role 'tool' after role 'user'.
    """
    if not any(isinstance(message, dict) and message.get("role") == "tool" for message in messages):
        return messages
    owners = {}
    last_tool_assistant = None
    for index, message in enumerate(messages):
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        calls = message.get("tool_calls")
        if not isinstance(calls, list) or not calls:
            continue
        last_tool_assistant = index
        for call in calls:
            if isinstance(call, dict) and isinstance(call.get("id"), str):
                owners[call["id"]] = index
    buckets = {}
    orphans = []
    for message in messages:
        if not isinstance(message, dict) or message.get("role") != "tool":
            continue
        owner = owners.get(message.get("tool_call_id"), last_tool_assistant)
        if owner is None:
            orphans.append(message)
            continue
        buckets.setdefault(owner, []).append(message)
    repaired = []
    for index, message in enumerate(messages):
        if isinstance(message, dict) and message.get("role") == "tool":
            continue
        repaired.append(message)
        repaired.extend(buckets.get(index, []))
    for message in orphans:
        content = message.get("content", "")
        if not isinstance(content, str):
            content = json.dumps(content, ensure_ascii=False)
        repaired.append({"role": "user", "content": content})
    return repaired
