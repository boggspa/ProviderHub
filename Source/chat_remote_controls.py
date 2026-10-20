"""Versioned, bounded control surface for an already-paired phone.

Only presentation fields cross this boundary. Commands name an exact chat and
call the same host handlers as the desktop; credentials and native result
injection are never accepted from a remote client.
"""
import json
import re

VERSION = 3
FIELDS = {
    "team": ("enabled", "status", "activeMemberID", "activeMemberIDs", "execution", "runUsage", "limitReason", "members"),
    "member": ("id", "name", "label", "choice", "route", "effort", "responsibility", "status", "nextStep", "contributions", "contributionID", "usage", "context", "failureReason", "waitReason", "pendingChange", "memberStoppedByUser"),
    "model": ("id", "route", "label", "provider", "accountLabel", "efforts", "context", "supportsTools", "presentation", "connectionPresentation"),
    "side": ("id", "label", "route", "effort", "status", "busy", "notice", "entries", "truncated"),
    "process": ("id", "pid", "command", "status", "code", "output", "started", "ended", "memberID", "route", "truncated"),
    "file": ("path", "oldPath", "status", "added", "deleted", "binary", "diff"),
    "commit": ("hash", "subject", "author", "time", "files", "truncated"),
}


def pick(value, fields):
    return {key: value[key] for key in fields if key in value} if isinstance(value, dict) else {}


def presentation(value):
    result = pick(value, ("runtimeProvider", "displayProvider", "accent", "shortCode", "modelLabel"))
    logo = pick(value.get("logo"), ("light", "dark", "template", "tint", "scale", "leadingMarkAspectRatio", "trailingMarkAspectRatio")) if isinstance(value, dict) else {}
    # Remote branding references bundled assets only, never external paths.
    if logo and all(not isinstance(logo.get(k), str) or re.fullmatch(r"provider-logos/provider-logo-[a-z0-9-]+\.png", logo[k]) for k in ("light", "dark")):
        result["logo"] = logo
    return result


def bounded(value, limit=10000):
    """Bound serialized UTF-8, including escapes; retain valid JSON throughout."""
    remaining = [limit]
    def take(item, depth=0):
        if remaining[0] < 64 or depth > 10: return None
        if isinstance(item, dict):
            result = {}
            for key, val in list(item.items())[:64]:
                if remaining[0] < 128: break
                remaining[0] -= len(json.dumps(str(key)).encode()) + 4
                result[str(key)] = take(val, depth + 1)
            return result
        if isinstance(item, list):
            result = []
            for val in item[:256]:
                if remaining[0] < 128: break
                result.append(take(val, depth + 1)); remaining[0] -= 2
            return result
        if isinstance(item, str):
            item = item[:4000]
            while len(json.dumps(item, ensure_ascii=False).encode()) > remaining[0] // 2:
                item = item[:len(item) // 2]
        elif item is not None and type(item) not in (bool, int, float): item = None
        remaining[0] -= len(json.dumps(item, ensure_ascii=False).encode()) + 2
        return item
    return take(value)


def project(event, entry_projector):
    kind, value = event.get("event"), None
    panel = kind
    if kind == "catalogue":
        panel = "models"
        value = [pick(row, FIELDS["model"]) for row in event.get("models", []) if isinstance(row, dict)]
        for row in value:
            if "presentation" in row: row["presentation"] = presentation(row["presentation"])
            if "connectionPresentation" in row: row["connectionPresentation"] = presentation(row["connectionPresentation"])
    elif kind == "remote_options":
        return {"event": "control", "chat": None, "panel": "options", "value": {"version": VERSION, "workspaces": event.get("workspaces", []), "host": event.get("host", {})}, "notice": ""}
    elif kind == "blackboard":
        board = event.get("blackboard") or {}
        panel = "blackboard"
        # The board's post text already has a 24 KB cap. Preserve every post
        # rather than truncating its identity or full edit body.
        value = {"posts": [pick(row, ("id", "author", "authorName", "route", "key", "body", "category", "created", "updated", "quote")) for row in board.get("posts", [])[:48]],
                 "attachments": [pick(row, ("id", "name", "kind", "url", "size", "authorName", "source")) for row in board.get("attachments", [])[:64]],
                 "limits": pick(board.get("limits"), ("posts", "bodyBytes", "boardBytes", "usedBytes", "attachments"))}
        return {"event": "control", "chat": event.get("chat"), "panel": panel, "value": value, "notice": str(event.get("notice") or "")[:500]}
    elif kind == "todos":
        todos = event.get("todos")
        value = None
        if isinstance(todos, dict):
            lanes = [row for row in todos.get("lanes", [])[:8] if isinstance(row, dict)]
            shared = todos.get("shared") if isinstance(todos.get("shared"), dict) else None
            # Each list gets its own share of the frame, so one long list cannot
            # hide the lanes after it. Progress counts come from the host.
            share = 20000 // max(1, len(lanes) + (1 if shared else 0))
            def listed(row, keys):
                result = pick(row, keys)
                result["items"] = [pick(item, ("id", "content", "status", "ownerName"))
                                   for item in row.get("items", [])[:24] if isinstance(item, dict)]
                return bounded(result, share)
            value = {"team": todos.get("team") is True,
                     "lanes": [listed(row, ("id", "name", "route", "choice", "done", "total", "updated")) for row in lanes],
                     "shared": listed(shared, ("id", "name", "done", "total", "updated", "revision")) if shared else None}
        return {"event": "control", "chat": event.get("chat"), "panel": "todos", "value": value,
                "notice": str(event.get("notice") or "")[:500]}
    elif kind == "remote_history":
        return {"event": "history", "chat": event.get("chat"), "before": event.get("before"), "request": event.get("request"),
                "entries": event.get("entries", []), "hasMore": event.get("hasMore", False)}
    elif kind == "remote_turn_changes":
        return {"event": "control", "chat": event.get("chat"), "panel": "turnChanges", "value": event.get("changes", {}), "notice": ""}
    elif kind == "remote_activity":
        # Each record is already built from the owning service's live state.
        # No provider history, input arguments or process environment are used.
        return {"event": "control", "chat": event.get("chat"), "panel": "activity",
                "value": {"busy": event.get("busy") is True, "interrupting": event.get("interrupting") is True,
                          "status": str(event.get("status") or "")[:160],
                          "rows": [bounded(pick(row, ("id", "name", "route", "status", "entryID", "kind", "tool", "summary", "runningTool", "streaming", "reason", "stopping")), 2400)
                                   for row in event.get("rows", [])[:8] if isinstance(row, dict)]}, "notice": ""}
    elif kind == "team":
        team = event.get("team")
        if isinstance(team, dict):
            value = pick(team, FIELDS["team"])
            value["members"] = [pick(row, FIELDS["member"]) for row in team.get("members", [])[:8]]
            # Pending edits contain configuration, never provider context.
            for member in value["members"]:
                if isinstance(member.get("pendingChange"), dict):
                    member["pendingChange"] = pick(member["pendingChange"], ("remove", "name", "label", "choice", "effort", "responsibility"))
                for key in ("nextStep", "failureReason", "waitReason"):
                    if isinstance(member.get(key), str): member[key] = member[key][:240]
            # Reserve room for every roster identity before verbose member
            # text, so eight long responsibilities cannot hide later members.
            # Editable configuration must round-trip losslessly.
            for member in value["members"]:
                limits = {"id": 128, "name": 120, "choice": 512, "effort": 40, "responsibility": 2000}
                exact = {key: str(member[key])[:limit] for key, limit in limits.items() if key in member and member[key] is not None}
                display = bounded(member, 1400)
                member.clear(); member.update(display); member.update(exact)
            return {"event": "control", "chat": event.get("chat"), "panel": "team", "value": value,
                    "notice": str(event.get("notice") or "")[:500]}
    elif kind in {"side", "agents"}:
        def discussion(row):
            result = pick(row, FIELDS["side"])
            result["entries"] = [entry_projector(e) for e in row.get("entries", [])[-12:] if isinstance(e, dict)]
            return result
        value = discussion(event["side"]) if kind == "side" and isinstance(event.get("side"), dict) else None
        if kind == "agents": value = [discussion(row) for row in event.get("agents", [])[-8:]]
    elif kind == "processes":
        value = [pick(row, FIELDS["process"]) for row in event.get("processes", [])[:32]]
    elif kind == "git_changes":
        changes = event.get("changes") or {}
        panel = "changes"
        value = {"files": [pick(row, FIELDS["file"]) for row in changes.get("files", [])[:64]],
                 "commits": [{**pick(row, FIELDS["commit"]), "files": [pick(f, FIELDS["file"]) for f in row.get("files", [])[:8]]} for row in changes.get("commits", [])[:8]],
                 "truncated": changes.get("truncated", False)}
    elif kind == "plugins":
        panel, value = "browserEnabled", event.get("browserUse") is True
    elif kind == "remote_configuration":
        panel, value = "configuration", pick(event, ("choice", "effort", "approvalMode", "workspace", "busy", "runBusy", "interrupting", "status"))
    elif kind == "side_delta":
        return {"event": "side_delta", "chat": event.get("chat"), "side": event.get("side"),
                "id": event.get("id"), "text": str(event.get("text") or "")[:4000],
                "offset": max(0, event.get("offset", 0)) if type(event.get("offset", 0)) is int else 0}
    elif kind == "remote_browser":
        panel, value = "browser", pick(event, ("summary", "text", "details", "isError"))
    elif kind == "remote_browser_preview":
        image = event.get("image")
        if not isinstance(image, str) or len(image) > 44000 or not re.fullmatch(r"[A-Za-z0-9+/]*={0,2}", image): return None
        return {"event": "control", "chat": event.get("chat"), "panel": "browserPreview", "value": image, "notice": ""}
    else: return None
    budget = 16000 if panel in {"models", "browser", "team"} else 10000
    return {"event": "control", "chat": event.get("chat"), "panel": panel,
            "value": bounded(value, budget), "notice": str(event.get("notice") or "")[:500]}


COMMANDS = {
    "remote_options": (), "remote_models": ("offset",), "remote_create": ("choice", "workspace", "effort"),
    "remote_history": ("before",), "remote_upload_begin": ("upload", "name", "size", "sha256"),
    "remote_attachment_read": ("attachment", "offset", "revision"),
    "remote_attachment_thumbnail": ("attachment",),
    "remote_upload_chunk": ("upload", "offset", "data"), "remote_upload_finish": ("upload",), "remote_upload_drop": ("upload",),
    "remote_send": ("text", "uploads", "mentions", "steer"), "remote_board_attach": ("uploads",),
    "blackboard": (), "blackboard_post": ("key", "body", "category", "quote"),
    "blackboard_remove": ("post",), "blackboard_attach": ("url", "title"), "blackboard_detach": ("attachment",),
    "remote_inspect": (), "inspect_git": (), "processes": (), "clear_processes": (),
    "configure": ("choice", "effort", "approvalMode", "expectedWorkspace"),
    "configure_team": ("enabled", "members", "execution"),
    "team_resume": ("member",), "team_member_start": ("member",), "team_member_stop": ("member",),
    "stop_process": ("process",), "open_side": ("choice", "effort"),
    "side_send": ("side", "text"), "side_model": ("side", "choice", "effort"),
    "side_stop": ("side",), "close_side": ("side",),
    "remote_browser": ("tool", "arguments"),
}


def command(params, request_id):
    action, args, chat = params.get("action"), params.get("arguments", {}), params.get("chat")
    if not isinstance(action, str) or action not in COMMANDS: raise ValueError("Unsupported control action")
    if action not in {"remote_options", "remote_models", "remote_create"} and (not isinstance(chat, str) or not re.fullmatch(r"[0-9a-f]{32}", chat)): raise ValueError("Exact chat required")
    if not isinstance(args, dict) or set(args) - set(COMMANDS[action]): raise ValueError("Unexpected control fields")
    byte_limit = 180000 if action in {"configure_team", "remote_send"} else 32000
    if len(json.dumps(args, ensure_ascii=False).encode()) > byte_limit: raise ValueError("Control request too large")
    if action == "remote_create" and (not isinstance(args.get("choice"), str) or not isinstance(args.get("workspace"), str)): raise ValueError("Choose a model and Mac workspace")
    if action == "remote_history" and (not isinstance(args.get("before"), str) or len(args["before"]) > 128): raise ValueError("History cursor required")
    if action in {"remote_send", "remote_board_attach"}:
        uploads = args.get("uploads", [])
        if not isinstance(uploads, list) or len(uploads) > 8 or any(not isinstance(u, str) or not re.fullmatch(r"[0-9a-f]{32}", u) for u in uploads): raise ValueError("Invalid attachment handles")
        if "steer" in args and not isinstance(args["steer"], bool): raise ValueError("Steer must be true or false")
    if action == "configure":
        if not args or not isinstance(args.get("expectedWorkspace"), str): raise ValueError("Refresh this chat before changing its configuration")
        if "approvalMode" in args and args["approvalMode"] not in ("manual", "accept_edits", "yolo"): raise ValueError("Invalid approval mode")
    if action in {"side_send", "side_model", "side_stop", "close_side"}:
        if not isinstance(args.get("side"), str) or not re.fullmatch(r"[0-9a-f]{32}", args["side"]): raise ValueError("Exact Side Chat required")
    if action in {"team_member_start", "team_member_stop"} and not isinstance(args.get("member"), str): raise ValueError("Exact member required")
    if action == "stop_process" and not isinstance(args.get("process"), str): raise ValueError("Exact process required")
    if action == "side_send" and (not isinstance(args.get("text"), str) or not args["text"].strip()): raise ValueError("Message required")
    if action == "remote_browser":
        if args.get("tool") not in {"browser_open", "browser_navigate", "browser_list", "browser_observe", "browser_act", "browser_close", "browser_remote_preview"}: raise ValueError("Unsupported browser action")
        if not isinstance(args.get("arguments", {}), dict): raise ValueError("Invalid browser arguments")
    return {"command": action, "id": chat, "chat": chat, "request": request_id, **args,
            **({"_remoteControl": True} if action.startswith("blackboard") or action == "configure_team" else {})}
