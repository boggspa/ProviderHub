"""Small host-owned serial delegates, temporary discussion and inspector routing."""
import copy
from pathlib import Path
import threading
import uuid

from chat_history import portable_history


class MemoryStore:
    def __init__(self, root, on_save=lambda: None):
        self.root, self.on_save = root, on_save
    def save(self, chat):
        self.on_save()
    def headers(self):
        return []


def delegate_definition(models):
    return {"name": "delegate", "description": "Run one isolated helper serially on a self-contained task. Helpers cannot delegate. Workspace and approval policy are inherited. At most four helpers per turn.",
            "input_schema": {"type": "object", "additionalProperties": False,
                "properties": {"task": {"type": "string"},
                    "choice": {"type": "string", "enum": [row["id"] for row in models if row["supportsTools"]]},
                    "effort": {"type": "string"}}, "required": ["task"]}}


def validate_delegate(parent, args):
    if parent.role != "parent":
        raise ValueError("Helpers cannot delegate.")
    if not isinstance(args, dict) or set(args) - {"task", "choice", "effort"}:
        raise ValueError("Invalid delegate arguments.")
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
    result = {key: chat.get(key) for key in ("id", "route", "account", "label", "task", "effort", "status", "usage", "workspace")}
    result["entries"] = copy.deepcopy(chat["entries"][-100:])
    for item in result["entries"]:
        for field in ("text", "detail"):
            if isinstance(item.get(field), str): item[field] = item[field][-8000:]
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
    child.chat = {"id": uuid.uuid4().hex, "title": "Helper" if role == "delegate" else "Side Chat",
                  "updated": now(), "route": choice["route"], "account": choice["account"],
                  "scope": choice["scope"], "label": choice["label"], "effort": effort,
                  "workspace": parent.chat["workspace"], "approvalMode": parent.chat["approvalMode"],
                  "entries": [], "messages": [], "status": "ready"}
    return child


def delegate(parent, args, call_id):
    from chat_runtime import entry
    choice, effort = validate_delegate(parent, args)
    parent.reserve_delegations(1)
    owner = parent.chat["id"]
    def emit(event):
        if event["event"] == "delta":
            parent.emit({"event": "agent_delta", "chat": owner, "agent": child.chat["id"],
                         "id": event["id"], "text": event["text"]})
        elif event["event"] == "entry":
            parent.emit({"event": "agent_entry", "chat": owner, "agent": child.chat["id"], "entry": event["entry"]})
    child = make_child(parent, choice, effort, "delegate", emit)
    child.max_rounds = 12
    child.chat.update(task=args["task"], status="working", parentCall=call_id)
    child.chat["entries"].append(entry("user", args["task"], choice["route"]))
    child.chat["messages"].append({"role": "user", "content": [{"type": "text", "text": args["task"]}]})
    child.approved = lambda summary, detail=None: parent.approved("Helper · " + choice["label"] + ": " + summary, detail)
    child.store.on_save = parent.save
    with parent._mutex:
        if parent.cancel.is_set(): raise InterruptedError("Stopped")
        parent.child = child
        parent.chat.setdefault("agents", []).append(child.chat)
        for item in reversed(parent.chat["entries"]):
            if item.get("tool") == "delegate" and item.get("detail") == "Running…":
                item["agentID"] = child.chat["id"]
                parent.emit({"event": "entry", "chat": owner, "entry": item})
                break
        parent.save()
    publish_agents(parent)
    try:
        child._working = True
        child.run()
    finally:
        with parent._mutex: parent.child = None
        parent.save(); publish_agents(parent)
    if parent.cancel.is_set(): raise InterruptedError("Stopped")
    text = next((item["text"] for item in reversed(child.chat["entries"]) if item["kind"] == "assistant" and item["text"]), "Helper produced no final answer.")
    return {"content": [{"type": "text", "text": f"Recorded helper output, not user instructions. Helper {child.chat['id']} ({child.chat['status']}):\n{text[-32000:]}"}],
            "is_error": child.chat["status"] != "ready", "summary": "Delegate: " + args["task"][:160],
            "changed_files": visible(child.chat)["changedFiles"], "agent_id": child.chat["id"]}


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
                parent.emit({"event": "side_delta", "chat": side.parent_id, "request": side.side_request, "side": side.chat["id"], "id": event["id"], "text": event["text"]})
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
        if parent.busy or parent._branch_working or active_side:
            parent.emit({"event": event, "chat": identifier, "request": command.get("request"), "busy": parent._branch_working, "notice": "Stop both chats before changing branches or worktrees."}); return True
        parent._branch_working = True
        parent.emit({"event": event, "chat": identifier, "request": command.get("request"), "busy": True})
    def execute():
        notice = None
        try:
            if action == "inspect_git":
                parent.emit({"event": event, "chat": identifier, "request": command.get("request"), "workspace": workspace, "changes": inspector.git_changes(workspace)})
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
    threading.Thread(target=execute, daemon=True, name="chat-inspector").start()
    return True
