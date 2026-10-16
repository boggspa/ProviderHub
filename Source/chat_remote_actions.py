"""Mac-owned execution for remote compose/history/Blackboard affordances."""
import copy
import json
from pathlib import Path

from chat_remote_files import RemoteFiles

GLOBAL = {"remote_options", "remote_create"}
ACTIONS = GLOBAL | {"remote_history", "remote_upload_begin", "remote_upload_chunk", "remote_upload_finish",
                    "remote_upload_drop", "remote_send", "remote_board_attach"}


def workspaces(host):
    with host.workspaces._lock:
        folders = list(host.workspaces.folders)
    return [{"path": path, "name": Path(path).name or path} for path in folders[:64]]


def handle(host, command):
    action = command.get("command")
    if action not in ACTIONS: return False, None
    if action == "remote_options":
        host.publish_workspaces()
        host.emit({"event": "remote_options", "workspaces": workspaces(host)})
        return True, {"version": 2}
    if action == "remote_create":
        # Remote creation must not invoke the desktop's select/create cursor.
        workspace = command.get("workspace")
        if workspace not in {row["path"] for row in workspaces(host)}: raise ValueError("Choose a registered Mac workspace")
        if not any(row["id"] == command.get("choice") for row in host.models): raise ValueError("Choose an enabled model/account")
        host.prune()
        if len(host.sessions) >= 16 and all(host.active(s) for s in host.sessions.values()): raise ValueError("Stop an active chat before creating another")
        service = host.new_session()
        service.create(command["choice"], workspace)
        identifier = service.chat["id"]
        host.sessions[identifier] = service
        host.publish_summaries()
        return True, {"chat": identifier}
    chat = command.get("chat")
    if chat not in host.headers: raise ValueError("Choose an existing chat")
    service = host.session(chat)
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
