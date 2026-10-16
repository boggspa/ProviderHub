"""Mac-owned execution for remote compose/history/Blackboard affordances."""
import copy
import json
from pathlib import Path

from chat_remote_files import RemoteFiles

GLOBAL = {"remote_options", "remote_models", "remote_create"}
ACTIONS = GLOBAL | {"remote_history", "remote_upload_begin", "remote_upload_chunk", "remote_upload_finish",
                    "remote_upload_drop", "remote_send", "remote_board_attach", "remote_attachment_read", "remote_attachment_thumbnail"}


def activity(service):
    """The newest visible work for each current contribution, in roster order."""
    chat = service.chat or {}
    team = chat.get("team") or {}
    entries = chat.get("entries", [])
    if not service.busy: return []
    members = team.get("members", []) if team.get("enabled") else []
    def visible(entry):
        return not (entry.get("kind") == "tool" and entry.get("tool") == "react_to_message" and not entry.get("isError"))
    def row(identifier, name, route, status, latest=None, reason="", stopping=False):
        latest = latest or {}
        return {"id": identifier, "name": str(name)[:120], "route": str(route)[:160], "status": status,
                "entryID": str(latest.get("id") or "")[:128], "kind": str(latest.get("kind") or "")[:40],
                "tool": str(latest.get("tool") or "")[:100],
                "summary": str(latest.get("summary") or latest.get("text") or "")[:500] if latest.get("kind") == "tool" else "",
                "runningTool": latest.get("kind") == "tool" and latest.get("detail") == "Running…",
                "streaming": latest.get("kind") == "assistant" and latest.get("recorded") is False and bool(latest.get("text", "").strip()),
                "reason": str(reason)[:500], "stopping": stopping}
    if members:
        active = [m for m in members if m.get("status") in {"working", "continuing", "waiting"}]
        wanted = {(m["id"], m.get("contributionID")) for m in active}
        latest = {}
        for entry in reversed(entries):
            key = (entry.get("memberID"), entry.get("contributionID"))
            if key in wanted and key not in latest and visible(entry): latest[key] = entry
            if len(latest) == len(wanted): break
        return [row(m["id"], m["name"], m.get("route", ""), m["status"], latest.get((m["id"], m.get("contributionID"))),
                    m.get("waitReason") or m.get("nextStep", ""), m.get("memberStoppedByUser") is True) for m in active]
    latest = next((e for e in reversed(entries) if visible(e)), {})
    choice = next((r for r in service.models if r.get("route") == chat.get("route") and r.get("account") == chat.get("account")), {})
    return [row("solo-" + chat.get("route", ""), choice.get("label", "Assistant"), chat.get("route", ""), "working", latest)]


def turn_changes(entries):
    """Mirror ChatTurnChanges.collect over the full current turn, not its tail."""
    start = next((i for i in range(len(entries) - 1, -1, -1) if entries[i].get("kind") == "user"), -1)
    rows = {}
    def file(path, member):
        if path not in rows: rows[path] = {"path": path, "members": [], "added": 0, "removed": 0, "diff": ""}
        if member not in rows[path]["members"]: rows[path]["members"].append(member)
        return rows[path]
    for entry in entries[start + 1:]:
        if not entry.get("changedFiles"): continue
        member = entry.get("memberName") or "Chat"
        for path in entry["changedFiles"]:
            if isinstance(path, str) and path: file(path, member)
        if entry.get("tool") != "apply_patch": continue
        lines = (entry.get("detail") or entry.get("text") or "").split("\n")
        current = None; i = 0
        while i < len(lines):
            line = lines[i]
            if line.startswith("--- ") and i + 1 < len(lines) and lines[i + 1].startswith("+++ "):
                old, new = line[4:].split("\t")[0], lines[i + 1][4:].split("\t")[0]
                path = new[2:] if new.startswith("b/") else new
                if new == "/dev/null": path = old[2:] if old.startswith("a/") else old
                current = file(path, member)
                if len(current["diff"]) < 1000: current["diff"] += ("\n" if current["diff"] else "") + line + "\n" + lines[i + 1]
                i += 2; continue
            if current:
                if line.startswith("+"): current["added"] += 1
                elif line.startswith("-"): current["removed"] += 1
                if len(current["diff"]) < 1000: current["diff"] += "\n" + line
            i += 1
    # Identity/counts survive even when inline diff excerpts use their budget.
    output = []; budget = 32000
    for row in list(rows.values())[:256]:
        row["path"] = row["path"][:1024]
        row["members"] = [str(m)[:120] for m in row["members"][:8]]
        if len(json.dumps(row, ensure_ascii=False).encode()) > budget: row["diff"] = ""
        size = len(json.dumps(row, ensure_ascii=False).encode())
        if size > budget: break
        output.append(row); budget -= size
    return {"files": output, "totalFiles": len(rows), "truncated": len(output) < len(rows),
            "turnID": entries[start].get("id", "initial") if start >= 0 else "initial",
            "entryIDs": [e.get("id") for e in entries[start + 1:] if e.get("changedFiles") and e.get("id")][-512:]}


def publish_turn_changes(host, service, *, through=None):
    entries = service.chat["entries"][:through]
    # Include earlier turns visible in the phone's tail. Each summary travels
    # independently so combined history never exceeds the encrypted frame cap.
    ends = [len(entries)] + [i for i in range(len(entries) - 1, -1, -1) if entries[i].get("kind") == "user"][:4]
    for end in reversed(ends):
        summary = turn_changes(entries[:end])
        if summary["files"]: host.emit({"event": "remote_turn_changes", "chat": service.chat["id"], "changes": summary})


def workspaces(host):
    with host.workspaces._lock:
        folders = list(host.workspaces.folders)
    return [{"path": path, "name": Path(path).name or path} for path in folders[:64]]


def handle(host, command):
    action = command.get("command")
    if action not in ACTIONS: return False, None
    if action == "remote_options":
        host.publish_workspaces()
        try: application = json.loads((Path(__file__).parent.parent / "build-manifest.json").read_text())["application"]
        except (OSError, ValueError, KeyError): application = {"version": "development", "build": "source"}
        host.emit({"event": "remote_options", "workspaces": workspaces(host), "host": application})
        return True, {"version": 3}
    if action == "remote_models":
        from chat_remote_controls import FIELDS, pick, presentation
        offset = command.get("offset", 0)
        if type(offset) is not int or not 0 <= offset <= len(host.models): raise ValueError("Invalid model page")
        rows = []
        for row in host.models[offset:offset + 20]:
            safe = pick(row, FIELDS["model"])
            for key in ("presentation", "connectionPresentation"):
                if key in safe: safe[key] = presentation(safe[key])
            rows.append(safe)
        end = offset + len(rows)
        return True, {"models": rows, "nextOffset": end if end < len(host.models) else None}
    if action == "remote_create":
        # Remote creation must not invoke the desktop's select/create cursor.
        workspace = command.get("workspace")
        if workspace not in {row["path"] for row in workspaces(host)}: raise ValueError("Choose a registered Mac workspace")
        if not any(row["id"] == command.get("choice") for row in host.models): raise ValueError("Choose an enabled model/account")
        host.prune()
        if len(host.sessions) >= 16 and all(host.active(s) for s in host.sessions.values()): raise ValueError("Stop an active chat before creating another")
        service = host.new_session()
        service.create(command["choice"], workspace, effort=command.get("effort", ""))
        identifier = service.chat["id"]
        host.sessions[identifier] = service
        host.publish_summaries()
        return True, {"chat": identifier}
    chat = command.get("chat")
    if chat not in host.headers: raise ValueError("Choose an existing chat")
    service = host.session(chat)
    if action in {"remote_attachment_read", "remote_attachment_thumbnail"}:
        from chat_remote_files import read_attachment
        with service._mutex:
            return True, read_attachment(service.chat, host.store.root, command.get("attachment"), command.get("offset", 0), expected=command.get("revision"), thumbnail=action == "remote_attachment_thumbnail")
    if action == "remote_history":
        from chat_remote import project_entry
        before = command.get("before")
        with service._mutex:
            entries = service.chat["entries"]
            index = next((i for i, entry in enumerate(entries) if entry.get("id") == before), None)
            if index is None: raise ValueError("History changed; reconnect to refresh this chat")
            page, size = [], 0
            for i in range(index - 1, max(-1, index - 31), -1):
                row = project_entry(entries[i], "history-" + str(i))
                length = len(json.dumps(row, ensure_ascii=False).encode())
                if page and size + length > 48000: break
                page.append(row); size += length
            page.reverse()
        host.emit({"event": "remote_history", "chat": chat, "before": before, "request": command.get("request"),
                   "entries": page, "hasMore": index > len(page)})
        publish_turn_changes(host, service, through=index)
        return True, {"count": len(page)}
    if not hasattr(host, "remote_files"): host.remote_files = RemoteFiles(host.store.root)
    files = host.remote_files
    token = command.get("upload")
    if action == "remote_upload_begin": return True, files.begin(chat, token, command.get("name"), command.get("size"), command.get("sha256"))
    if action == "remote_upload_chunk": return True, files.chunk(chat, token, command.get("offset"), command.get("data"))
    if action == "remote_upload_finish": return True, files.finish(chat, token)
    if action == "remote_upload_drop":
        files.drop(chat, token)
        return True, {}
    if action == "remote_send":
        if host.active(service): raise ValueError("Wait for this chat and Side Chat to stop before sending")
        inputs = files.inputs(chat, command.get("uploads", []))
        text = command.get("text", "")
        if not isinstance(text, str) or (not text.strip() and not inputs) or len(text) > 100000: raise ValueError("Message or attachment required")
        host.handle({"command": "send", "id": chat, "chat": chat, "text": text, "attachments": inputs, "request": command.get("request")})
        # The normal prepare_update path has copied/validated every original.
        for token in command.get("uploads", []): files.drop(chat, token)
        return True, {}
    if action == "remote_board_attach":
        tokens = command.get("uploads", [])
        inputs = files.inputs(chat, tokens)
        if not inputs: raise ValueError("Attach a file first")
        import chat_blackboard
        before = copy.deepcopy(service.chat.get("blackboard"))
        chat_blackboard.handle(service, {"command": "blackboard_attach", "id": chat,
            "paths": [item["path"] for item in inputs], "request": command.get("request"), "_remoteControl": True})
        if before == service.chat.get("blackboard"): raise ValueError("Blackboard attachment was not accepted; inspect the notice")
        for token in tokens: files.drop(chat, token)
        return True, {}
    return False, None
