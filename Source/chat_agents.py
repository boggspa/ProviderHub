"""Small host-owned serial delegates, temporary discussion and inspector routing."""
import copy
from pathlib import Path
import threading
import time
import uuid

from chat_history import portable_history

# Shelved helpers are kept in a root-owned registry, apart from the foreground
# child and lanes, so one survives the turn that started it.
BACKGROUND_LIMIT = 8
BACKGROUND_WAIT_LIMIT = 120
SHELF_POLL = 0.05
DELEGATE_FIELDS = {"task", "choice", "effort", "background"}


class MemoryStore:
    def __init__(self, root, on_save=lambda: None):
        self.root, self.on_save = root, on_save
    def save(self, chat):
        self.on_save()
    def headers(self):
        return []


def delegate_definition(models):
    task = {"type": "object", "additionalProperties": False, "properties": {
        "task": {"type": "string", "description": "Self-contained task for this helper."},
        "choice": {"type": "string", "enum": [row["id"] for row in models if row["supportsTools"]]},
        "effort": {"type": "string"},
        "background": {"type": "boolean", "description": "Run this helper in the background. The parent continues immediately and collects the result later with delegate_status."}}, "required": ["task"]}
    return {"name": "delegate", "description": "Use task for one serial helper with inherited tools/approvals, OR tasks for 2–3 parallel READ-ONLY helpers (local tools: read_file/search_files only). Native web search follows Chat settings and each helper's model capabilities. Never mix the two forms. By default all helpers must finish before the parent continues; set background:true to let the parent continue while the helpers run and read them later with delegate_status. No child delegation or queued messages. Each defaults to the parent's exact model/account; choice can select another enabled route. At most four helpers total per turn.",
            "input_schema": {"type": "object", "additionalProperties": False,
                "properties": {**task["properties"], "tasks": {"type": "array", "minItems": 2, "maxItems": 3, "items": task}}}}


def delegate_status_definition():
    """The tool a conversation uses to collect shelved helper results."""
    return {"name": "delegate_status",
            "description": "Read the status and recorded output of background helpers started with background:true. Without agent_id, list every background helper this chat knows. wait_seconds waits up to that many seconds for them to finish; it never waits longer.",
            "input_schema": {"type": "object", "additionalProperties": False, "properties": {
                "agent_id": {"type": "string", "description": "One background helper ID. Omit to list them all."},
                "wait_seconds": {"type": "integer", "minimum": 0, "maximum": BACKGROUND_WAIT_LIMIT,
                                 "description": "Wait up to this many seconds for the helpers to finish."}}}}


def root_owner(service):
    """The conversation that owns helper records for a parent chat or a Team member."""
    if service.role == "parent": return service
    if service.role == "team": return getattr(service, "team_parent", None)
    return None


def background_registry(owner):
    """The root-owned registry of shelved helpers, kept apart from child and lanes."""
    registry = getattr(owner, "background_helpers", None)
    if registry is None:
        registry = {}
        owner.background_helpers = registry
    return registry


def background_agents(owner):
    """Every shelved helper this root owns, with its ownership and live child.

    Each record carries id, chatId, ownerId, memberId, callId, status, task,
    label, route, account, readOnly, started, finished, result, error and the
    live helper service under "child".
    """
    return background_registry(root_owner(owner) or owner)


def has_background(owner):
    """True when this root still holds any shelved helper record."""
    return bool(background_registry(root_owner(owner) or owner))


def background_running(owner):
    """True when a shelved helper is still working."""
    return any(record.get("status") == "working"
               for record in background_registry(root_owner(owner) or owner).values())


def background_flag(args):
    """The background request on a delegate call, validated as a boolean."""
    value = args.get("background", False) if isinstance(args, dict) else False
    if not isinstance(value, bool):
        raise ValueError("background must be true or false.")
    return value


def background_requested(args):
    """True when any helper in this call was asked to run in the background."""
    if not isinstance(args, dict): return False
    if "background" in args: return background_flag(args)
    tasks = args.get("tasks")
    if isinstance(tasks, list):
        return any(background_flag(task) for task in tasks if isinstance(task, dict))
    return False


def validate_delegate(parent, args):
    if parent.role not in {"parent", "team"}:
        raise ValueError("Helpers cannot delegate.")
    if isinstance(args, dict) and "tasks" in args:
        tasks = args["tasks"]
        if set(args) - {"tasks", "background"} or not isinstance(tasks, list) or not 2 <= len(tasks) <= 3:
            raise ValueError("Use tasks with two or three self-contained read-only lanes; do not mix serial arguments.")
        if parent._delegations + len(tasks) > 4:
            raise ValueError("This turn has insufficient helper slots for every lane. No lane was launched.")
        if any(not isinstance(task, dict) or "tasks" in task for task in tasks):
            raise ValueError("Each lane must be a single task; nested fan-out is not supported.")
        lanes = [validate_delegate(parent, task) for task in tasks]
        if "background" in args:
            if {background_flag(task) for task in tasks if "background" in task} - {background_flag(args)}:
                raise ValueError("background is set once for the whole call; do not set it on a lane.")
        elif len({background_flag(task) for task in tasks}) > 1:
            raise ValueError("Background and foreground helpers cannot be mixed in one call.")
        return lanes
    if not isinstance(args, dict) or set(args) - DELEGATE_FIELDS:
        raise ValueError("Invalid delegate arguments.")
    background_flag(args)
    if not isinstance(args.get("task"), str) or not args["task"].strip() or len(args["task"]) > 100_000:
        raise ValueError("Provide a self-contained task of at most 100,000 characters.")
    if "choice" in args and (not isinstance(args["choice"], str) or not args["choice"]):
        raise ValueError("Use an exact enabled model/account choice ID.")
    choice = parent.choice(args.get("choice"))
    if not choice["supportsTools"]:
        raise ValueError("Choose an enabled tool-capable model.")
    effort = args.get("effort", parent.chat["effort"] if choice["id"] == parent.choice()["id"] else "")
    if not isinstance(effort, str) or effort and effort not in choice["efforts"]:
        raise ValueError("Choose a supported reasoning level.")
    if parent._delegations >= 4:
        raise ValueError("This turn has reached its four-helper limit.")
    return choice, effort


def visible(chat):
    result = {key: chat.get(key) for key in ("id", "route", "account", "label", "task", "effort", "status", "usage", "workspace", "readOnly")}
    result["entries"] = copy.deepcopy(chat["entries"][-100:])
    for item in result["entries"]:
        for field in ("text", "detail"):
            if isinstance(item.get(field), str):
                if field == "text": item["textOffset"] = max(0, len(item[field]) - 8000)
                item[field] = item[field][-8000:]
    remaining = 64_000
    bounded = []
    for item in reversed(result["entries"]):
        size = len(str(item))
        if size > remaining: break
        bounded.append(item); remaining -= size
    result["entries"] = list(reversed(bounded))
    result["truncated"] = len(result["entries"]) < len(chat["entries"]) or any(len(str(item.get(field) or "")) > 8000 for item in chat["entries"] for field in ("text", "detail"))
    result["changedFiles"] = list(dict.fromkeys(path for item in chat["entries"] for path in item.get("changedFiles", [])))
    return result


def publish_agents(parent):
    with parent._mutex:
        if parent.chat:
            parent.emit({"event": "agents", "chat": parent.chat["id"],
                         "agents": [visible(agent) for agent in parent.chat.get("agents", [])[-20:]]})


def recover_agents(chat, settle):
    changed = False
    for agent in chat.get("agents", []):
        if agent.get("status") == "working":
            changed = True
            settle(agent, "Helper interrupted before a recorded result. Do not replay uncertain actions.")
            agent["status"] = "interrupted"
            for item in agent["entries"]:
                if item.get("detail") == "Running…":
                    item.update(detail="Interrupted; inspect the workspace before retrying.", isError=True)
    return changed


def make_child(parent, choice, effort, role, emit):
    from chat_runtime import ChatService, now
    child = ChatService(MemoryStore(parent.store.root), parent.child_factory(), emit,
                        runner=parent.runner_type, workspaces=parent.workspaces,
                        child_factory=parent.child_factory, role=role)
    child.models = copy.deepcopy(parent.models)
    # Background processes a helper starts belong to the parent chat.
    child.helper_parent = parent
    # One live preference for the parent and all existing/new helpers and Side Chats.
    child.preferences = parent.preferences
    child.capabilities = parent.capabilities
    child.chat = {"id": uuid.uuid4().hex, "title": "Helper" if role == "delegate" else "Side Chat",
                  "updated": now(), "route": choice["route"], "account": choice["account"],
                  "scope": choice["scope"], "label": choice["label"], "effort": effort,
                  "workspace": parent.chat["workspace"], "approvalMode": parent.chat["approvalMode"],
                  "entries": [], "messages": [], "status": "ready"}
    return child


def _helper_approval(owner, parent, choice, child):
    """Ask the user for a helper's approval through the conversation that owns it.

    A Team member's helper asks through the Team's approval gate, so its
    request keeps the member's identity and stays serialized with the other
    members' approvals.
    """
    def approved(summary, detail=None):
        label = "Helper · " + choice["label"] + ": " + summary
        child.approval_pending.set()
        try:
            if parent.role == "team":
                allowed = _team_approval(owner, parent, label, detail, child)
            else:
                allowed = _request_approval(owner, label, detail, helper=child)
        finally:
            child.approval_pending.clear()
        return bool(allowed) and not child.cancel.is_set()
    return approved


def _team_approval(owner, member, summary, detail, helper):
    """Ask through the Team's approval gate and the owner's shared approval gate.

    The Team gate is entered with the helper's own cancel event, so a stopped
    helper never holds the queue for the other members.
    """
    from chat_team import FifoGate
    gate = getattr(owner, "team_approvals", None)
    if gate is None:
        gate = FifoGate()
        owner.team_approvals = gate
    with gate.enter(helper.cancel):
        return _request_approval(owner, summary, detail, member=member.team_member, helper=helper)


def _team_of(member):
    """The Team a member belongs to, or None when it has none."""
    owner = getattr(member, "team_parent", None)
    chat = getattr(owner, "chat", None)
    team = chat.get("team") if isinstance(chat, dict) else None
    return team if isinstance(team, dict) else None


def _inherit_team_accounting(member, child):
    """Hold a Team member's helper to the Team's own time and token limits.

    The helper's usage is counted against the Team's run budget, and the same
    limits are checked before and after every request it makes, so an explicit
    Team limit reaches the helper too.
    """
    from chat_execution import guard, record
    request = child.request_round
    def request_round(payload, current, delta):
        if _team_of(member) is None: return request(payload, current, delta)
        guard(member)
        message = request(payload, current, delta)
        record(member, message.get("usage") or {})
        guard(member)
        return message
    child.request_round = request_round


def _request_approval(owner, summary, detail, member=None, helper=None):
    """Ask the user for an approval and wait for the answer or a cancellation.

    The owner's shared approval gate keeps a helper's question and the
    conversation's own question apart, and the helper's cancel event ends the
    wait when that helper is stopped.
    """
    gate = getattr(owner, "approval_gate", None)
    if gate is None:
        # Without the shared gate the owner still owns its own approval channel.
        return owner.approved(summary, detail) if member is None else owner.approved(summary, detail, member=member)
    cancel = helper.cancel if helper is not None else owner.cancel
    with gate.enter(cancel):
        return _await_approval(owner, summary, detail, member, helper)


def _await_approval(owner, summary, detail, member=None, helper=None):
    """Hold the owner's single approval slot until the user answers."""
    with owner._mutex:
        if owner.cancel.is_set() or owner.closing: return False
        owner.approval_allowed = False
        owner.approval_event.clear()
        approval = {"id": uuid.uuid4().hex, "summary": summary,
                    "workspace": owner.chat["workspace"], "detail": detail}
        if member: approval.update(memberID=member["id"], memberName=member["name"])
        owner.approval = approval
        owner.emit({"event": "approval", "approval": approval})
    while not owner.approval_event.wait(SHELF_POLL):
        stopped = owner.cancel.is_set() or (helper is not None and helper.cancel.is_set())
        if stopped:
            with owner._mutex:
                if owner.approval and owner.approval.get("id") == approval["id"]:
                    owner.approval = None
                    cleared = True
                else:
                    cleared = False
            if cleared:
                # The question is gone; the window must stop showing it.
                owner.emit({"event": "approval", "approval": None})
            return False
    with owner._mutex:
        allowed = owner.approval_allowed and not owner.cancel.is_set()
        if owner.approval and owner.approval.get("id") == approval["id"]:
            owner.approval = None
    return allowed


def _helper(parent, args, choice, effort, call_id, *, parallel, owner=None):
    from chat_runtime import entry
    owner = owner or root_owner(parent) or parent
    owner_chat_id = owner.chat["id"] if owner.chat else None
    def emit(event):
        if event["event"] == "delta":
            owner.emit({"event": "agent_delta", "chat": owner_chat_id, "agent": child.chat["id"],
                         "id": event["id"], "text": event["text"], **({"offset": event["offset"]} if "offset" in event else {})})
        elif event["event"] == "entry":
            owner.emit({"event": "agent_entry", "chat": owner_chat_id, "agent": child.chat["id"], "entry": event["entry"]})
    child = make_child(parent, choice, effort, "lane" if parallel else "delegate", emit)
    child.max_rounds = 8 if parallel else 12
    child.chat.update(task=args["task"], status="working", parentCall=call_id, readOnly=parallel)
    child.chat["entries"].append(entry("user", args["task"], choice["route"]))
    child.chat["messages"].append({"role": "user", "content": [{"type": "text", "text": args["task"]}]})
    child.approval_pending = threading.Event()
    child.approved = _helper_approval(owner, parent, choice, child)
    if parent.role == "team":
        _inherit_team_accounting(parent, child)
    def persist():
        # Copy on the owning child's thread. Parent snapshots never serialize
        # another lane's currently-mutating message list or reasoning blocks.
        snapshot = copy.deepcopy(child.chat)
        with owner._mutex:
            if not owner.chat or owner.chat["id"] != owner_chat_id:
                # The owner moved to another chat; this helper never writes into it.
                return
            records = owner.chat.setdefault("agents", [])
            index = next((i for i, record in enumerate(records) if record["id"] == snapshot["id"]), None)
            if index is None: records.append(snapshot)
            else: records[index] = snapshot
            owner.save()
    child.store.on_save = persist
    return child


def delegate_summary(args):
    suffix = " in background" if background_requested(args) else ""
    if isinstance(args, dict) and "tasks" in args:
        return f"Delegate: {len(args['tasks'])} parallel read-only tasks{suffix}"
    return "Delegate: " + args["task"][:160] + suffix


def _final_text(child):
    """The helper's last recorded answer, or an empty string."""
    return next((item["text"] for item in reversed(child.chat["entries"])
                 if item["kind"] == "assistant" and item["text"]), "")


def _reserve_slots(service, count):
    """Reserve helper slots on the conversation that owns this turn."""
    if service.role == "parent":
        service.reserve_delegations(count)
        return
    with service._mutex:
        if type(count) is not int or count < 1 or service._delegations + count > 4:
            raise ValueError("This logical turn has reached its four-helper limit.")
        service._delegations += count
        service.chat["delegationsThisTurn"] = service._delegations


def active_runs(owner):
    """The registry of delegate calls that are still running."""
    runs = getattr(owner, "delegate_runs", None)
    if runs is None:
        runs = {}
        owner.delegate_runs = runs
    return runs


def _register_run(owner, call_id, children, parallel, member_id, owner_chat_id, service):
    """Record a running delegate so it can be shelved while it works."""
    run = {"id": uuid.uuid4().hex, "callId": call_id, "shelve": threading.Event(), "done": threading.Event(),
           "children": children, "agentIds": [child.chat["id"] for child in children],
           "parallel": parallel, "memberId": member_id, "chatId": owner_chat_id,
           "owner": owner, "service": service, "started": time.time()}
    active_runs(owner)[run["id"]] = run
    return run


def _finish_run(owner, run):
    """Release a finished run so it keeps no service references."""
    run["done"].set()
    runs = active_runs(owner)
    with owner._mutex:
        if runs.get(run["id"]) is run: runs.pop(run["id"], None)


def _stamp_tool_entry(parent, ids, call_id, parallel):
    """Record the helper IDs and the provider call on the running delegate row."""
    for item in reversed(parent.chat["entries"]):
        if item.get("tool") == "delegate" and item.get("detail") == "Running…":
            item["agentIDs" if parallel else "agentID"] = ids if parallel else ids[0]
            item["callID"] = call_id
            parent.emit({"event": "entry", "chat": parent.chat["id"], "entry": item})
            break


def _check_background_capacity(owner, count):
    """Refuse a launch that would outrun the chat's shelved helper budget."""
    registry = background_registry(owner)
    live = [record for record in registry.values() if record.get("status") == "working"]
    if len(live) + count > BACKGROUND_LIMIT:
        raise ValueError(f"This chat already has {len(live)} background helpers running. "
                         f"Wait for one to finish or stop it before starting {count} more.")


def _register_background(owner, owner_chat_id, children, *, parallel, call_id, member_id):
    """Move helpers into the root-owned background registry."""
    registry = background_registry(owner)
    records = []
    with owner._mutex:
        for child in children:
            record = {"id": child.chat["id"], "chatId": owner_chat_id, "ownerId": owner_chat_id,
                      "memberId": member_id, "callId": call_id, "child": child,
                      "status": child.chat.get("status", "working"), "result": "", "error": "",
                      "label": child.chat["label"], "route": child.chat["route"], "account": child.chat["account"],
                      "task": child.chat.get("task", ""), "readOnly": bool(child.chat.get("readOnly")),
                      "parallel": parallel, "started": time.time(), "finished": None,
                      "done": threading.Event(), "thread": None}
            if record["status"] != "working":
                record["result"] = _final_text(child)
                record["finished"] = time.time()
                record["done"].set()
            registry[record["id"]] = record
            records.append(record)
    return records


def _finalize_record(owner, child, failure=""):
    """Record a finished helper in the background registry."""
    with owner._mutex:
        record = background_registry(owner).get(child.chat["id"])
        if record is None: return
        status = child.chat.get("status", "error" if failure else "ready")
        if failure and status == "ready": status = "error"
        record["status"] = status
        record["result"] = _final_text(child)
        record["error"] = failure
        record["finished"] = time.time()
        record["done"].set()


def _run_helper(owner, owner_chat_id, child, errors):
    """Run one helper, then persist and publish what it recorded."""
    from chat_runtime import entry
    failure = ""
    try:
        child._working = True
        child.run()
    except Exception as exc:
        failure = str(exc)
        child.chat["status"] = "error"
        child.chat["entries"].append(entry("error", "Helper stopped before a complete recorded result.", child.chat["route"], isError=True))
        if errors is not None:
            with owner._mutex: errors.append(exc)
    finally:
        try:
            child.store.on_save(); publish_agents(owner)
        except Exception as exc:
            failure = failure or str(exc)
            if errors is not None:
                with owner._mutex: errors.append(exc)
        _finalize_record(owner, child, failure)


def _wait_helpers(threads, run):
    """Join helper threads, returning True when the user shelved them."""
    while True:
        alive = [thread for thread in threads if thread.is_alive()]
        if not alive: return False
        if run["shelve"].is_set(): return True
        for thread in alive: thread.join(SHELF_POLL)


def _shelve_run(owner, owner_chat_id, children, run, parallel, member_id):
    """Let running helpers continue in the background and detach them."""
    _register_background(owner, owner_chat_id, children, parallel=parallel,
                         call_id=run["callId"], member_id=member_id)
    service = run.get("service")
    with owner._mutex:
        if service is not None:
            service.child = None
            service.lanes = []


def _background_result(ids, parallel, summary="Background helper started"):
    """The tool result a shelved delegate returns to the model."""
    listed = ", ".join(ids)
    text = ("The helper is running in the background and this conversation continues now. "
            f"Helper ID: {listed}. Use delegate_status with that ID to read the result when it is needed.")
    result = {"content": [{"type": "text", "text": text}], "is_error": False,
              "summary": summary, "changed_files": [], "background": True}
    result["agent_ids" if parallel else "agent_id"] = ids if parallel else ids[0]
    return result


def delegate(parent, args, call_id):
    from chat_runtime import entry
    selections = validate_delegate(parent, args)
    parallel = "tasks" in args
    background = background_requested(args)
    tasks = args["tasks"] if parallel else [args]
    selections = selections if parallel else [selections]
    owner = root_owner(parent) or parent
    owner_chat_id = owner.chat["id"] if owner.chat else None
    member_id = parent.team_member["id"] if parent.role == "team" and parent.team_member else None
    children = [_helper(parent, task, choice, effort, call_id, parallel=parallel, owner=owner)
                for task, (choice, effort) in zip(tasks, selections)]
    ids = [child.chat["id"] for child in children]
    with owner._mutex:
        if owner.cancel.is_set(): raise InterruptedError("Stopped")
        if background: _check_background_capacity(owner, len(children))
        _reserve_slots(parent, len(children))
        if not background:
            parent.lanes = children if parallel else []
            parent.child = None if parallel else children[0]
        owner.chat.setdefault("agents", []).extend(copy.deepcopy(child.chat) for child in children)
        _stamp_tool_entry(parent, ids, call_id, parallel)
        owner.save()
    publish_agents(owner)
    run = _register_run(owner, call_id, children, parallel, member_id, owner_chat_id, parent)
    if background:
        records = _register_background(owner, owner_chat_id, children, parallel=parallel,
                                       call_id=call_id, member_id=member_id)
        for child, record in zip(children, records):
            thread = threading.Thread(target=_run_helper, args=(owner, owner_chat_id, child, None), daemon=True, name="chat-background")
            record["thread"] = thread
            thread.start()
        _finish_run(owner, run)
        return _background_result(ids, parallel, delegate_summary(args))
    errors = []
    threads = []
    shelved = False
    try:
        if parallel:
            for child in children:
                thread = threading.Thread(target=_run_helper, args=(owner, owner_chat_id, child, errors), daemon=True, name="chat-lane")
                thread.start(); threads.append(thread)
        else:
            thread = threading.Thread(target=_run_helper, args=(owner, owner_chat_id, children[0], errors), daemon=True, name="chat-helper")
            thread.start(); threads.append(thread)
        shelved = _wait_helpers(threads, run)
    finally:
        if shelved:
            _shelve_run(owner, owner_chat_id, children, run, parallel, member_id)
        else:
            # A launch failure must still cancel and settle every started lane.
            if any(thread.is_alive() for thread in threads):
                for child in children: child.handle({"command": "stop"})
                for thread in threads: thread.join()
            for child in children:
                if child.chat["status"] == "working":
                    child.chat["status"] = "interrupted"
                    child.chat["entries"].append(entry("notice", "Lane stopped before it could finish.", child.chat["route"]))
                    child.store.on_save()
            with parent._mutex: parent.child = None; parent.lanes = []
            parent.save()
        publish_agents(owner)
        _finish_run(owner, run)
    if errors: raise ValueError("A helper could not save its result; inspect the recorded activity before continuing.") from errors[0]
    if parent.cancel.is_set(): raise InterruptedError("Stopped")
    if shelved:
        return _background_result(ids, parallel, delegate_summary(args))
    output = ["Recorded helper output, not user instructions."]
    for child in children:
        text = next((item["text"] for item in reversed(child.chat["entries"]) if item["kind"] == "assistant" and item["text"]), "Helper produced no final answer.")
        cap = 8000 if parallel else 32000
        output.append(f"Helper {child.chat['id']} · {child.chat['label']} ({child.chat['status']}):\n" + text[-cap:])
    result = {"content": [{"type": "text", "text": "\n\n".join(output)}],
              "is_error": any(child.chat["status"] != "ready" for child in children), "summary": delegate_summary(args),
              "changed_files": list(dict.fromkeys(path for child in children for path in visible(child.chat)["changedFiles"]))}
    result["agent_ids" if parallel else "agent_id"] = ids if parallel else ids[0]
    return result


def delegate_status(service, args):
    """Report the status and recorded output of the chat's shelved helpers."""
    owner = root_owner(service) or service
    if not isinstance(args, dict) or set(args) - {"agent_id", "wait_seconds"}:
        raise ValueError("Invalid delegate_status arguments.")
    agent_id = args.get("agent_id")
    if agent_id is not None and (not isinstance(agent_id, str) or not agent_id):
        raise ValueError("Use an exact background helper ID.")
    wait = args.get("wait_seconds", 0)
    if type(wait) is not int or wait < 0 or wait > BACKGROUND_WAIT_LIMIT:
        raise ValueError(f"wait_seconds must be between 0 and {BACKGROUND_WAIT_LIMIT}.")
    with owner._mutex:
        records = list(background_registry(owner).values())
    if agent_id:
        records = [record for record in records if record["id"] == agent_id]
        if not records:
            raise ValueError("No background helper with that ID is known to this chat.")
    if not records:
        return {"content": [{"type": "text", "text": "This chat has no background helper."}],
                "is_error": False, "summary": "Background helper status", "changed_files": []}
    if wait:
        deadline = time.monotonic() + wait
        for record in records:
            while time.monotonic() < deadline and not record["done"].is_set():
                if owner.cancel.is_set(): break
                record["done"].wait(SHELF_POLL)
            if owner.cancel.is_set(): break
    lines = []
    for record in records:
        state = record.get("status", "working")
        lines.append(f"Helper {record['id']} · {record.get('label', '')} · {state}")
        if state == "working":
            lines.append("Still running. Ask again later or wait with wait_seconds.")
        else:
            text = record.get("result") or ""
            lines.append(text[-8000:] if text else "The helper recorded no final answer.")
            if record.get("error"): lines.append("Reported failure: " + record["error"])
    changed = list(dict.fromkeys(path for record in records
                                 for path in (visible(record["child"].chat)["changedFiles"] if record.get("child") else [])))
    return {"content": [{"type": "text", "text": "\n\n".join(lines)}],
            "is_error": any(record.get("status") in {"error", "interrupted", "cancelled"} for record in records),
            "summary": "Background helper status", "changed_files": changed}


def _run_for_row(owner, row):
    """Find the running delegate that a transcript row started."""
    if not isinstance(row, dict): return None
    ids = row.get("agentIDs") if isinstance(row.get("agentIDs"), list) else []
    if not ids and row.get("agentID"): ids = [row["agentID"]]
    runs = list(active_runs(owner).values())
    if ids:
        for run in runs:
            if set(ids) & set(run.get("agentIds", [])): return run
    # A provider call id can repeat, so it only identifies a run when it is unique.
    call_id = row.get("callID")
    matches = [run for run in runs if call_id and run.get("callId") == call_id]
    if len(matches) == 1: return matches[0]
    return None


def shelve_delegate(service, display_id=None, member=None):
    """Let a running delegate keep its helpers in the background.

    ``display_id`` is the transcript row of the running delegate tool; the
    helper IDs and the provider call on that row identify the run. Without a
    row, the only running delegate of this chat is shelved.
    """
    owner = root_owner(service) or service
    run = None
    if display_id:
        for chat in (getattr(service, "chat", None), getattr(owner, "chat", None)):
            if not chat: continue
            for item in reversed(chat.get("entries", [])):
                if item.get("id") == display_id and item.get("tool") == "delegate" and item.get("recorded") is not True:
                    run = _run_for_row(owner, item)
                    break
            if run: break
    else:
        candidates = [item for item in active_runs(owner).values() if not item["done"].is_set()]
        run = candidates[0] if len(candidates) == 1 else None
    if run is None:
        raise ValueError("No running helper matches that delegate row.")
    if member is not None and run.get("memberId") != member:
        raise ValueError("That delegate belongs to a different Team member.")
    run["shelve"].set()
    return True


def cancel_background(service, agent_id=None, member=None):
    """Stop the shelved helpers this conversation owns.

    A Team member's stop is scoped to that member's helpers; the root's stop
    reaches every helper the root owns.
    """
    owner = root_owner(service) or service
    if member is None and getattr(service, "role", "") == "team":
        member = (getattr(service, "team_member", None) or {}).get("id")
    with owner._mutex:
        records = [record for record in background_registry(owner).values()
                   if (agent_id is None or record["id"] == agent_id)
                   and (member is None or record.get("memberId") == member)]
    if agent_id and not records:
        raise ValueError("No background helper with that ID is known to this chat.")
    for record in records:
        child = record.get("child")
        if child is None: continue
        try:
            child.cancel.set(); child.transport.cancel(); child.approval_event.set()
        except Exception:
            pass
        # A pending question is never resolved from here: the approval slot may
        # hold another conversation's question. The helper's own wait ends when
        # its cancel event is set.
        record["cancelRequested"] = True
    return len(records)


def publish_side(parent, notice=None, *, side=None):
    side = side or parent.side
    if not side: return
    snapshot = visible(side.chat)
    snapshot.update(busy=side.busy, interrupting=side._steering, notice=notice)
    parent.emit({"event": "side", "chat": side.parent_id, "request": side.side_request, "side": snapshot})


def close_side(parent, *, owner=None):
    owner = owner or (parent.chat["id"] if parent.chat else None)
    side = parent.sides.pop(owner, None)
    if not side: return
    side.handle({"command": "stop"})
    # The child has only bounded reads and its own socket. Detach immediately
    # so close cannot delay the main chat's Stop; late events fail ownership.
    parent.emit({"event": "side", "chat": side.parent_id, "request": side.side_request, "sideID": side.chat["id"], "side": None,
                 "notice": "Temporary Side Chat closed; its conversation was discarded."})


def close_all_sides(parent):
    for owner in list(parent.sides):
        close_side(parent, owner=owner)


def side_command(parent, command):
    from chat_runtime import entry
    action = command["command"]
    if parent._branch_working and action not in {"close_side", "side_stop"}:
        raise ValueError("Wait for the branch operation to finish.")
    if not parent.chat or command.get("id") != parent.chat["id"]:
        raise ValueError("Select the owning chat before using Side Chat.")
    if action == "open_side":
        if len(parent.sides) >= 8 and not parent.side:
            raise ValueError("Eight temporary Side Chats are open. Close one before forking another.")
        if not isinstance(command.get("choice"), str) or not command["choice"]:
            raise ValueError("Choose an enabled model/account.")
        choice = parent.choice(command.get("choice"))
        effort = command.get("effort", "")
        if not isinstance(effort, str) or effort and effort not in choice["efforts"]: raise ValueError("Choose a supported reasoning level.")
        close_side(parent)
        def emit(event):
            if parent.sides.get(side.parent_id) is not side: return
            if event["event"] == "delta":
                parent.emit({"event": "side_delta", "chat": side.parent_id, "request": side.side_request, "side": side.chat["id"], "id": event["id"], "text": event["text"], **({"offset": event["offset"]} if "offset" in event else {})})
            elif event["event"] in {"entry", "state", "selected"}:
                if event["event"] == "entry" and event["entry"].get("kind") == "user":
                    parent.emit({"event": "side_accepted", "chat": side.parent_id, "request": side.side_request, "side": side.chat["id"], "id": event["entry"]["id"]})
                publish_side(parent, side=side)
        side = make_child(parent, choice, effort, "side", emit)
        side.parent_id = parent.chat["id"]
        side.side_request = command.get("request") or side.chat["id"]
        side.chat["entries"] = copy.deepcopy(parent.chat["entries"])
        side.chat["messages"] = portable_history(side.chat["entries"], vision=choice.get("vision") is not False, reason="The user opened a temporary read-only Side Chat from the main conversation.")
        parent.side = side
        publish_side(parent)
        return
    side = parent.side
    if not side or command.get("side") != side.chat["id"] or side.parent_id != command["id"]:
        raise ValueError("This Side Chat is no longer open.")
    if action == "close_side": close_side(parent)
    elif action == "side_stop": side.handle({"command": "stop"})
    elif action == "side_send":
        choice = parent.choice(side.chat["route"] + "|" + side.chat["account"])
        if choice["scope"] != side.chat["scope"]:
            raise ValueError("This model connection changed. Reselect a Side Chat model before continuing.")
        side.models = copy.deepcopy(parent.models)
        text = command.get("text")
        if not isinstance(text, str) or not text.strip() or len(text) > 100_000:
            raise ValueError("Enter a side message of at most 100,000 characters.")
        if side.busy:
            side.interrupt_with_update({"id": side.chat["id"], "text": text})
        else:
            side.chat["entries"].append(entry("user", text, side.chat["route"]))
            side.chat["messages"].append({"role": "user", "content": [{"type": "text", "text": text}]})
            parent.emit({"event": "side_accepted", "chat": side.parent_id, "request": side.side_request, "side": side.chat["id"], "id": side.chat["entries"][-1]["id"]})
            side.chat["status"] = "working"; side.cancel.clear(); side._working = True
            side.thread = threading.Thread(target=side.run, daemon=True, name="chat-side")
            publish_side(parent); side.thread.start()
    elif action == "side_model":
        if side.busy: raise ValueError("Stop Side Chat before switching models.")
        if not isinstance(command.get("choice"), str) or not command["choice"]:
            raise ValueError("Choose an enabled model/account.")
        choice = parent.choice(command.get("choice"))
        effort = command.get("effort", "")
        if not isinstance(effort, str) or effort and effort not in choice["efforts"]: raise ValueError("Choose a supported reasoning level.")
        side.chat.setdefault("archives", []).append({key: copy.deepcopy(side.chat[key]) for key in ("route", "account", "scope", "effort", "messages")})
        side.chat.update({key: choice[key] for key in ("route", "account", "scope", "label")})
        side.chat.update(effort=effort, messages=portable_history(side.chat["entries"], vision=choice.get("vision") is not False))
        side.models = copy.deepcopy(parent.models)
        publish_side(parent)


def _same_worktree(side, root, inspector):
    path = Path(side.chat["workspace"]).resolve()
    if not path.is_relative_to(root): return False
    try: return Path(inspector.repository_root(path)).resolve() == root
    except (OSError, ValueError):
        # A checkout can remove an earlier side chat's subdirectory. It still
        # needs the context update and must not be silently treated as unrelated.
        return True


def handle_auxiliary(parent, command):
    action = command.get("command", "")
    if action in {"open_side", "side_send", "side_stop", "side_model", "close_side"}:
        try:
            if action == "close_side" and not command.get("id") and not command.get("side"):
                close_all_sides(parent)
            else: side_command(parent, command)
        except (ValueError, KeyError, TypeError, OSError) as exc:
            matches = parent.side and parent.side.parent_id == command.get("id") and action != "open_side" and command.get("side") == parent.side.chat["id"]
            snapshot = visible(parent.side.chat) | {"busy": parent.side.busy, "interrupting": parent.side._steering, "notice": str(exc)} if matches else None
            request = parent.side.side_request if matches else command.get("request")
            parent.emit({"event": "side", "chat": command.get("id"), "request": request, "sideID": command.get("side"), "notice": str(exc), "side": snapshot})
        return True
    if action not in {"inspect_git", "branches", "branch_action"}: return False
    import chat_inspector as inspector
    identifier = command.get("id")
    event = {"inspect_git": "git_changes", "branches": "branches", "branch_action": "branch_state"}[action]
    if not parent.chat or identifier != parent.chat["id"]:
        parent.emit({"event": event, "chat": identifier, "request": command.get("request"), "busy": False, "notice": "Select the current chat."}); return True
    workspace = parent.chat["workspace"]
    mutation = action == "branch_action"
    if mutation:
        active_side = any(side.busy and Path(side.chat["workspace"]).resolve() == Path(workspace).resolve() for side in parent.sides.values())
        if background_running(parent):
            parent.emit({"event": event, "chat": identifier, "request": command.get("request"), "busy": False,
                         "notice": "Stop the background helpers before changing branches or worktrees."}); return True
        if parent.busy or parent._branch_working or active_side:
            parent.emit({"event": event, "chat": identifier, "request": command.get("request"), "busy": parent._branch_working, "notice": "Stop both chats before changing branches or worktrees."}); return True
        parent._branch_working = True
        parent.emit({"event": event, "chat": identifier, "request": command.get("request"), "busy": True})
    def execute():
        notice = None
        try:
            if action == "inspect_git":
                since = None
                heads = getattr(parent, "turn_heads", None)
                if isinstance(heads, dict):
                    try: since = heads.get(str(Path(workspace).expanduser().resolve()))
                    except OSError: since = None
                parent.emit({"event": event, "chat": identifier, "request": command.get("request"), "workspace": workspace, "changes": inspector.git_changes(workspace, since=since)})
            elif action == "branches":
                parent.emit({"event": event, "chat": identifier, "request": command.get("request"), "workspace": workspace, "branches": inspector.git_branches(workspace)})
            else:
                matching_sides = []
                if parent.sides:
                    root = Path(inspector.repository_root(workspace)).resolve()
                    matching_sides = [side for side in list(parent.sides.values()) if _same_worktree(side, root, inspector)]
                    if any(side.busy for side in matching_sides):
                        raise ValueError("Stop Side Chats using this repository before changing branches or worktrees.")
                operation = command.get("action")
                if operation == "switch":
                    branch = inspector.decode_branch(command.get("branchBytes")) if command.get("branchBytes") is not None else command.get("branch")
                    for side in matching_sides: inspector.require_branch_workspace(side.chat["workspace"], branch)
                    result = inspector.switch_branch(workspace, branch)
                elif operation == "create": result = inspector.create_branch(workspace, command.get("branch"))
                elif operation == "worktree": result = inspector.create_worktree(workspace, command.get("branch"), command.get("path"))
                elif operation == "select_worktree":
                    folder = inspector.switch_worktree(workspace, command.get("path"))
                    for item in parent.chat["entries"]:
                        if item.get("kind") == "tool": item.setdefault("workspace", workspace)
                    parent.chat.setdefault("archives", []).append({key: copy.deepcopy(parent.chat[key]) for key in ("route", "account", "scope", "effort", "workspace", "messages")})
                    parent.chat.update(workspace=folder, messages=portable_history(parent.chat["entries"], vision=parent.choice().get("vision") is not False, reason="The user selected a different worktree. Earlier paths and results refer to the previous workspace."))
                    from chat_runtime import entry
                    parent.chat["entries"].append(entry("notice", "Workspace changed to " + folder + ". Earlier tool results refer to the previous workspace.", parent.chat["route"]))
                    parent.workspaces.remember(folder); parent.save(); parent.publish_workspaces(); parent.publish()
                    result = inspector.git_branches(folder)
                else: raise ValueError("Unknown branch action.")
                if operation in {"switch", "create"}:
                    from chat_runtime import entry
                    message = "Switched branch to " + result["current"] + ". Earlier file and tool results describe the previous branch; inspect the current files before editing."
                    parent.add(entry("notice", message, parent.chat["route"]))
                    parent.chat["messages"].append({"role": "user", "content": [{"type": "text", "text": "[Workspace update from Provider Hub] " + message}]})
                    parent.save()
                    for side in matching_sides:
                        side.chat["entries"].append(entry("notice", message, side.chat["route"]))
                        side.chat["messages"].append({"role": "user", "content": [{"type": "text", "text": "[Workspace update from Provider Hub] " + message}]})
                        publish_side(parent, side=side)
                parent.emit({"event": "branches", "chat": identifier, "request": command.get("request"), "workspace": parent.chat["workspace"], "branches": result})
        except Exception as exc:
            notice = str(exc)
            if not mutation:
                parent.emit({"event": event, "chat": identifier, "request": command.get("request"), "workspace": workspace, "busy": False, "notice": notice})
        finally:
            if mutation:
                parent._branch_working = False
                parent.emit({"event": "branch_state", "chat": identifier, "request": command.get("request"), "workspace": parent.chat["workspace"], "busy": False, "notice": notice})
    thread = threading.Thread(target=execute, daemon=True, name="chat-inspector")
    if mutation: parent.branch_thread = thread
    thread.start()
    return True
