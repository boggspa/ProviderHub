"""Per-provider subagent spawn-depth enforcement for Responses requests.

A parent that may spawn, whose children may also spawn, fans out without
bound under multi_agent_v2: every grandchild is another full-context
streaming request against the same provider quota. This module caps the
recursion by removing the collaboration spawn tool from requests that
already run below the top level.

Depth is read from the request's own typed history, not from text, so
model echoes of delegation markers can never trigger it:

- a delegation-task item (multi_agent_call, subagent_call, agent_message)
  means this requester was itself tasked by someone;
- a spawn_agent function_call in the same history means this requester
  has already spawned, i.e. it is acting as a parent.

With ``spawn_depth_limit == 1`` the tool is stripped only when the first
holds without the second: fresh parents and spawning parents keep the
tool, tasked children lose it, and grandchildren become unreachable
(because a stripped child can never emit the first spawn call that would
exempt it). ``0`` strips unconditionally (delegation fully disabled);
absent/None leaves the request untouched.

Deeper limits cannot be expressed from a single request's history: a
depth-1 and a depth-2 requester that have not spawned yet are
indistinguishable. Only 0 and 1 are accepted; anything else is rejected
at settings validation with an explanation.

Enforcement runs as gateway middleware before the native Responses
handler, so the typed items are still intact. Every abnormal shape is
fail-open: the request passes through byte-identical and downstream
validation reports as it does today.
"""
from __future__ import annotations

import io
import json

from hub_config import split_route
from responses_tools import qualified_name, tool_name


#: Mirrors the gateway/Responses request-body bound so oversized bodies
#: are left for downstream rejection instead of being buffered here.
MAX_BODY = 32 * 1024 * 1024

SPAWN_NAMESPACE = "collaboration"
SPAWN_TOOL = "spawn_agent"
#: The exact flattened identities our own tool catalogue produces, kept in
#: sync by construction. History items are matched by exact equality only:
#: substring matching would false-positive on unrelated user tools. Both
#: forms are recognised because the preferred flattened name is the bare
#: leaf and register() only falls back to the qualified form when some
#: other tool in the same request already claimed it.
FLATTENED_SPAWN_TOOL = tool_name(SPAWN_NAMESPACE, SPAWN_TOOL)
QUALIFIED_SPAWN_TOOL = qualified_name(SPAWN_NAMESPACE, SPAWN_TOOL)

#: History item types that record a delegation task flowing down to this
#: requester. Completion records (*_output) are deliberately excluded: a
#: bare result says a delegation finished, not that this requester was
#: tasked, and counting it would misclassify supervising parents.
TASK_ITEM_TYPES = frozenset({"multi_agent_call", "subagent_call", "agent_message"})


def is_spawn_tool_reference(namespace, name):
    """True when (namespace, name) identifies our collaboration spawn tool."""
    if namespace == SPAWN_NAMESPACE and name == SPAWN_TOOL:
        return True
    return namespace is None and name in (SPAWN_TOOL, FLATTENED_SPAWN_TOOL, QUALIFIED_SPAWN_TOOL)


def _is_spawn_call(item):
    return (isinstance(item, dict) and item.get("type") == "function_call"
            and is_spawn_tool_reference(item.get("namespace"), item.get("name")))


def _is_task_item(item):
    return isinstance(item, dict) and item.get("type") in TASK_ITEM_TYPES


#: The spawn_agent argument naming the model a sub-agent runs on. Codex
#: documents it as "Omit unless an explicit override is needed", and omitting
#: it makes the child inherit its parent.
SPAWN_MODEL_ARGUMENT = "model"


def apply_subagent_model(item, route):
    """Fill in the model a spawned sub-agent should run on.

    Codex has a configuration key for this - default_subagent_model - and on
    26.908 it is inert: driving a real spawn with it set, the child thread
    still arrives on the parent's model. The argument on the spawn call is
    honoured, so that is where the choice has to be written.

    Only an absent model is filled in. A model the parent named itself is an
    explicit decision by the running agent, and overwriting it would turn a
    default into a cage - the setting picks who answers when nobody asked for
    anyone in particular.

    Returns True when the call was rewritten, so the caller can hold back the
    argument deltas that still spell the original JSON.
    """
    if not route or not isinstance(item, dict) or item.get("type") != "function_call":
        return False
    if not is_spawn_tool_reference(item.get("namespace"), item.get("name")):
        return False
    raw = item.get("arguments")
    if not isinstance(raw, str):
        return False
    try:
        arguments = json.loads(raw)
    except ValueError:
        return False
    # Anything but an object is a malformed call; leave it for Codex to
    # reject as it would have, rather than replacing it with one that works.
    if not isinstance(arguments, dict) or arguments.get(SPAWN_MODEL_ARGUMENT):
        return False
    arguments[SPAWN_MODEL_ARGUMENT] = route
    item["arguments"] = json.dumps(arguments, ensure_ascii=False)
    return True


def should_strip(body_input, limit):
    """Pure depth decision: True when the spawn tool must go."""
    if limit == 0:
        return True
    if limit != 1 or not isinstance(body_input, list):
        return False
    tasked = any(_is_task_item(item) for item in body_input)
    return tasked and not any(_is_spawn_call(item) for item in body_input)


def strip_spawn_tools(tools):
    """Return (tools, removed) with every spawn-tool entry dropped.

    Both the namespaced collaboration form and a bare spawn_agent entry
    are removed; emptied namespaces are dropped with them. The input is
    never mutated. Non-list tool payloads pass through untouched.
    """
    if not isinstance(tools, list):
        return tools, 0
    kept, removed = [], 0
    for tool in tools:
        if not isinstance(tool, dict):
            kept.append(tool)
            continue
        if tool.get("type") == "namespace" and tool.get("name") == SPAWN_NAMESPACE:
            children = tool.get("tools")
            if not isinstance(children, list):
                kept.append(tool)
                continue
            survivors = [child for child in children
                         if not (isinstance(child, dict) and child.get("type") == "function"
                                 and child.get("name") == SPAWN_TOOL)]
            removed += len(children) - len(survivors)
            if survivors:
                clone = dict(tool)
                clone["tools"] = survivors
                kept.append(clone)
            continue
        if tool.get("type") == "function" and is_spawn_tool_reference(
                tool.get("namespace"), tool.get("name")):
            removed += 1
            continue
        kept.append(tool)
    return kept, removed


def apply_spawn_depth_limit(body, limit):
    """Apply the depth rule to a parsed Responses body.

    Returns (body, stripped). The same object is returned when nothing
    changed, so callers can skip re-encoding; otherwise a copy with the
    spawn tools removed.
    """
    if limit is None:
        return body, 0
    if not isinstance(body, dict) or not should_strip(body.get("input"), limit):
        return body, 0
    tools, stripped = strip_spawn_tools(body.get("tools"))
    if not stripped:
        return body, 0
    edited = dict(body)
    edited["tools"] = tools
    return edited, stripped


def filter_spawn_tools(handler):
    """Gateway middleware: enforce the request provider's depth flag.

    Reads the pending request body, strips spawn tools when the depth
    rule fires, and swaps the body back for the downstream handler.
    Returns the number of tools stripped. Any unreadable shape passes
    through byte-identical (returns 0) for downstream validation.
    """
    headers = handler.headers
    if headers.get("Transfer-Encoding"):
        return 0
    try:
        size = int(headers.get("Content-Length", "0"))
    except (TypeError, ValueError):
        return 0
    if not 0 < size <= MAX_BODY:
        return 0
    try:
        raw = handler.rfile.read(size)
    except OSError:
        return 0
    if len(raw) != size:
        return 0

    def restore(payload):
        handler.rfile = io.BytesIO(payload)
        if "Content-Length" in headers:
            del headers["Content-Length"]
        headers["Content-Length"] = str(len(payload))

    try:
        body = json.loads(raw)
    except ValueError:
        restore(raw)
        return 0
    if not isinstance(body, dict):
        restore(raw)
        return 0
    try:
        provider_id = split_route(body.get("model"))[0]
    except (ValueError, TypeError, AttributeError):
        restore(raw)
        return 0
    settings = handler.runtime.settings or {}
    providers = settings.get("providers") or {}
    entry = providers.get(provider_id) or {}
    limit = entry.get("spawn_depth_limit")
    edited, stripped = apply_spawn_depth_limit(body, limit)
    if not stripped:
        restore(raw)
        return 0
    restore(json.dumps(edited, ensure_ascii=False).encode())
    handler.runtime.record("spawn_stripped", body.get("model"))
    return stripped
