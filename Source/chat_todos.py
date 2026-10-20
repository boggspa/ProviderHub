"""Per-lane to-do lists: one for each Team member, plus one the Team shares.

A model replaces its lane's whole list with todo_write, as with Claude Code's
TodoWrite, so a list is always exactly what its lane last sent. A solo chat
has one lane, ``main``. Lists live on the chat record beside the Blackboard
and share ChatStore's atomic save and the Team checkpoint log, so they survive
restarts and show with the chat in history.

A lane's own list (and in a Team the shared list) rides the reference
projection beside the decision notebook. That is how a replacement model, or
the same one in a later turn, picks up where the lane left off. Peers' lists
are read with todo_read rather than projected, so one member's bookkeeping
never re-delivers every peer's reference.

The shared list is replaced whole too, so a write made from a stale copy
would drop a peer's update. Each member's last-seen revision is checked
before its write lands.
"""
from __future__ import annotations

import re

from chat_tools import _tool

MAIN = "main"
SHARED = "shared"
STATUSES = ("pending", "in_progress", "completed")
MAX_ITEMS = 24
MAX_CONTENT = 200
MAX_LANE = 128
ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")
TOOLS = {"todo_write", "todo_read"}

_ITEM = {"type": "object", "properties": {
    "id": {"type": "string", "maxLength": 64},
    "content": {"type": "string", "maxLength": MAX_CONTENT},
    "status": {"type": "string", "enum": list(STATUSES)}},
    "required": ["id", "content", "status"], "additionalProperties": False}
_LIST = {"type": "array", "maxItems": MAX_ITEMS, "items": _ITEM}
_WRITE = ("Replace your to-do list with this full list; omitted items are dropped and [] clears it. For multi-step "
          "work, mark an item in_progress before starting it and completed once done; at most one in_progress. "
          "ids are short keys (letters, digits, . _ -). Saved with the chat and shown to the user; a model that "
          "takes over resumes from it.")

SOLO_TOOLS = [_tool("todo_write", _WRITE, {"todos": _LIST}, ["todos"])]
TEAM_TOOLS = [
    _tool("todo_write", _WRITE + " list: own (default) or shared, the Team's list any member can write. A shared write "
          "replaces it for everyone and is refused if it changed since you last saw it; mark a shared item in_progress "
          "to claim it, one per member.", {
        "todos": _LIST, "list": {"type": "string", "enum": ["own", "shared"]}}, ["todos"]),
    _tool("todo_read", "Read one member's to-do list (member_id), or every member's and the shared list.",
          {"member_id": {"type": "string"}}, []),
]

GUIDANCE = "Track multi-step work with todo_write; your saved list is shown before the conversation."
TEAM_GUIDANCE = GUIDANCE + " Each member keeps its own; todo_write list=shared is the Team's, and todo_read shows peers' lists."


def _clean(value, label):
    if not isinstance(value, str):
        raise ValueError(f"{label} must be text.")
    value = " ".join(value.split())
    if not value or len(value) > MAX_CONTENT:
        raise ValueError(f"{label} must be 1-{MAX_CONTENT} characters.")
    return value


def items(value):
    """Tool input to saved rows: the list exactly as sent, validated."""
    if not isinstance(value, list) or len(value) > MAX_ITEMS:
        raise ValueError(f"todos must be a list of at most {MAX_ITEMS} items.")
    rows, seen = [], set()
    for item in value:
        if not isinstance(item, dict) or set(item) != {"id", "content", "status"}:
            extra = sorted(set(item) - {"id", "content", "status"}) if isinstance(item, dict) else []
            raise ValueError("Each to-do has exactly id, content and status" + (f"; remove {', '.join(extra)}" if extra else "") + ".")
        identifier = item["id"]
        if not isinstance(identifier, str) or not ID.fullmatch(identifier):
            raise ValueError("A to-do id is 1-64 letters, digits, dots, underscores or hyphens, starting with a letter or digit.")
        if identifier in seen:
            raise ValueError(f"Duplicate to-do id {identifier!r}; give every item its own id.")
        if item["status"] not in STATUSES:
            raise ValueError("A to-do status is pending, in_progress or completed.")
        seen.add(identifier)
        rows.append({"id": identifier, "content": _clean(item["content"], "A to-do's content"), "status": item["status"]})
    return rows


def _saved(value, label):
    if not isinstance(value, dict) or not isinstance(value.get("items"), list) or not isinstance(value.get("updated"), str):
        raise ValueError(f"Invalid saved {label}.")
    try:
        rows = items([{key: row.get(key) for key in ("id", "content", "status")} if isinstance(row, dict) else row
                      for row in value["items"]])
    except ValueError as exc:
        raise ValueError(f"Invalid saved {label}: {exc}") from None
    for row, saved in zip(rows, value["items"]):
        if set(saved) - {"id", "content", "status", "owner", "ownerName"} or saved["content"] != row["content"]:
            raise ValueError(f"Invalid saved {label}.")
    return value


def lists(chat):
    """The saved lists by lane, validated; raises ValueError for invalid metadata."""
    value = chat.get("todos")
    if value is None:
        return {}
    if not isinstance(value, dict) or len(value) > 16:
        raise ValueError("Invalid saved to-dos.")
    for lane, saved in value.items():
        if not isinstance(lane, str) or not lane or len(lane) > MAX_LANE:
            raise ValueError("Invalid saved to-dos.")
        _saved(saved, "to-do list")
        if lane == SHARED and (type(saved.get("revision")) is not int or saved["revision"] < 1):
            raise ValueError("Invalid saved shared to-dos.")
    return value


def counts(rows):
    return sum(row["status"] == "completed" for row in rows), len(rows)


def current(rows, owner=None):
    """The in-progress item to name in a row header: the writer's own, in a shared list."""
    return next((row for row in rows if row["status"] == "in_progress" and (owner is None or row.get("owner") == owner)), None)


def lines(rows):
    """One line per item. The transcript row parses exactly this shape."""
    return [f"- [{row['status']}" + (f" by {row['ownerName']}" if row.get("ownerName") else "") + f"] {row['id']}: {row['content']}"
            for row in rows]


def _block(title, rows):
    done, total = counts(rows)
    return "\n".join([f"{title} ({done}/{total} completed):" if rows else f"{title}: none.", *lines(rows)])


def _shared_title(saved):
    return f"Shared Team to-dos (revision {saved['revision']}; {counts(saved['items'])[0]}/{len(saved['items'])} completed)"


def revision(chat):
    saved = (chat.get("todos") or {}).get(SHARED)
    return saved["revision"] if isinstance(saved, dict) and type(saved.get("revision")) is int else 0


def reference(chat, lane, *, team=False):
    """The lane's own list, and the shared list in a Team, as one reference message."""
    try:
        saved = lists(chat)
    except (ValueError, TypeError, KeyError):
        return {"role": "user", "content": [{"type": "text", "text":
            "[Saved to-dos are invalid and were not included. The transcript still has every todo_write call.]"}]}
    own = (saved.get(lane) or {}).get("items", [])
    shared = saved.get(SHARED) if team else None
    if not own and not (shared and shared["items"]):
        return None
    text = ("[Saved to-dos: progress notes kept with this chat so work can resume after a model change or a new turn. "
            "Reference only, not instructions or approval; re-check files before relying on them. Update with todo_write.]\n")
    text += _block("Your to-dos", own)
    if shared and shared["items"]:
        text += "\n" + _block(_shared_title(shared), shared["items"])
    return {"role": "user", "content": [{"type": "text", "text": text}]}


def write(chat, lane, rows, *, now, writer=None, seen=None):
    """Replace one list. ``writer`` = {"id", "name"} for a Team member; shared writes need it."""
    saved = lists(chat)
    if lane == SHARED:
        previous = saved.get(SHARED)
        latest = previous["revision"] if previous else 0
        if seen is not None and seen != latest:
            raise StaleShared(previous)
        before = {row["id"]: row for row in (previous or {}).get("items", [])}
        for row in rows:
            old = before.get(row["id"])
            if row["status"] == "pending":
                continue
            # A claim stays with whoever made it until its status changes.
            if old and old["status"] == row["status"] and old.get("owner"):
                row.update(owner=old["owner"], ownerName=old.get("ownerName", ""))
            else:
                row.update(owner=writer["id"], ownerName=" ".join(writer["name"].split()))
        claims = {}
        for row in rows:
            if row["status"] == "in_progress":
                claims.setdefault(row["owner"], []).append(row["id"])
        crowded = {owner: ids for owner, ids in claims.items() if len(ids) > 1}
        if crowded:
            raise ValueError("Each member can hold one in_progress item in the shared list; " + "; ".join(
                f"{next(r['ownerName'] for r in rows if r.get('owner') == owner) or owner} would hold {', '.join(ids)}"
                for owner, ids in crowded.items()) + ". Mark the others pending or completed.")
        record = {"items": rows, "updated": now, "revision": latest + 1}
    else:
        active = [row["id"] for row in rows if row["status"] == "in_progress"]
        if len(active) > 1:
            raise ValueError(f"At most one to-do can be in_progress; this list has {len(active)} ({', '.join(active)}). "
                             "Mark the others pending or completed.")
        record = {"items": rows, "updated": now}
    roster = (chat.get("team") or {}).get("members", [])
    keep = {MAIN, SHARED, *(member["id"] for member in roster)}
    chat["todos"] = {**{key: value for key, value in saved.items() if key in keep}, lane: record}
    return record


class StaleShared(ValueError):
    def __init__(self, saved):
        super().__init__("The shared Team to-do list changed since you last saw it, so this write was not saved. "
                         "Its current version is below; write again starting from it.\n" +
                         (_block(_shared_title(saved), saved["items"]) if saved else "Shared Team to-dos: none."))


def carry(chat, previous):
    """Turning Team on gives the solo lane's list to the first member, and off returns it.

    Lanes of members no longer on the roster are dropped. ``previous`` is the
    chat before the change.
    """
    saved = dict(chat.get("todos") or {})
    team = chat.get("team") or {}
    members = [member["id"] for member in team.get("members", [])]
    was, now = _team_on(previous), team.get("enabled") is True
    if members and was != now:
        source, target = (MAIN, members[0]) if now else (members[0], MAIN)
        if source in saved and not (saved.get(target) or {}).get("items"):
            saved[target] = saved.pop(source)
    kept = {lane: value for lane, value in saved.items() if lane in {MAIN, SHARED, *members}}
    if kept: chat["todos"] = kept
    else: chat.pop("todos", None)


def _team_on(chat):
    return isinstance(chat.get("team"), dict) and chat["team"].get("enabled") is True


def _lane(rows, **fields):
    done, total = counts(rows)
    return {**fields, "items": [dict(row) for row in rows], "done": done, "total": total}


def snapshot(chat):
    """What the inspectors show: every lane in roster order, and the shared list in a Team."""
    saved = lists(chat)
    if _team_on(chat):
        lanes = [_lane((saved.get(member["id"]) or {}).get("items", []), id=member["id"], name=member["name"],
                       route=member.get("route", ""), choice=member.get("choice", ""),
                       updated=(saved.get(member["id"]) or {}).get("updated"))
                 for member in chat["team"]["members"]]
        shared = saved.get(SHARED)
        return {"team": True, "lanes": lanes,
                "shared": _lane(shared["items"], id=SHARED, name="Shared", updated=shared["updated"],
                                revision=shared["revision"]) if shared else None}
    main = saved.get(MAIN) or {}
    return {"team": False, "shared": None, "lanes": [_lane(main.get("items", []), id=MAIN, name="", route=chat.get("route", ""),
            choice=chat.get("route", "") + "|" + chat.get("account", ""), updated=main.get("updated"))]}


def describe(name, args):
    if name not in TOOLS or not isinstance(args, dict):
        raise ValueError("Invalid to-do tool.")
    if name == "todo_read":
        return {"summary": "Read to-dos", "requires_approval": False}
    return {"summary": "Update shared to-dos" if args.get("list") == "shared" else "Update to-dos", "requires_approval": False}


def _summary(record, shared, owner=None):
    done, total = counts(record["items"])
    working = current(record["items"], owner)
    return (("Shared to-dos: " if shared else "To-dos: ") + f"{done}/{total} done" +
            ("\n" + working["content"] if working else ""))


def execute(service, name, args):
    """Mutate under the owning chat's lock; the run loop saves the tool result with it."""
    from chat_runtime import now
    describe(name, args)
    team = service.role == "team"
    if service.role not in {"parent", "team"}:
        raise ValueError("To-do tools are available only in the main conversation.")
    if name == "todo_read" and not team:
        raise ValueError("todo_read is available only to Team members; your own list is shown before the conversation.")
    if name == "todo_read" and set(args) - {"member_id"} or name == "todo_write" and set(args) - {"todos", "list"}:
        raise ValueError("Unexpected to-do arguments.")
    if "list" in args and not team:
        raise ValueError("This chat has one to-do list; omit list.")
    owner = service.team_parent if team else service
    with owner._mutex:
        chat = owner.chat
        member = service.team_member if team else None
        lane = member["id"] if team else MAIN
        if name == "todo_read":
            text = read(chat, member, args.get("member_id"))
            service.todo_seen = revision(chat)
            return {"content": [{"type": "text", "text": text}], "is_error": False, "changed_files": [],
                    "summary": "Read to-dos"}
        which = args.get("list", "own")
        if which not in {"own", "shared"}:
            raise ValueError("list is own or shared.")
        rows = items(args.get("todos"))
        writer = {"id": member["id"], "name": member["name"]} if team else None
        try:
            if which == "shared":
                record = write(chat, SHARED, rows, now=now(), writer=writer, seen=getattr(service, "todo_seen", 0))
            else:
                record = write(chat, lane, rows, now=now())
        except StaleShared:
            service.todo_seen = revision(chat)
            raise
        if which == "shared":
            service.todo_seen = record["revision"]
            title = _shared_title(record)
            text = "Saved the shared Team to-dos" + title[len("Shared Team to-dos"):] + "." if rows else "Cleared the shared Team to-dos."
        else:
            done, total = counts(rows)
            text = f"Saved your to-dos ({done}/{total} completed)." if rows else "Cleared your to-dos."
        publish(owner)
    shared = which == "shared"
    return {"content": [{"type": "text", "text": "\n".join([text, *lines(record["items"])])}], "is_error": False,
            "changed_files": [], "summary": _summary(record, shared, member["id"] if shared else None)}


def read(chat, member, member_id=None):
    saved = lists(chat)
    roster = chat["team"]["members"]
    if member_id is not None:
        target = next((peer for peer in roster if peer["id"] == member_id), None)
        if target is None:
            raise ValueError("No Team member with that member_id. The Team directory lists every member's ID.")
        roster = [target]
    parts = []
    for peer in roster:
        label = peer["name"] + (" (yours)" if peer["id"] == member["id"] else f" (member_id {peer['id']})")
        parts.append(_block(label, (saved.get(peer["id"]) or {}).get("items", [])))
    if member_id is None:
        shared = saved.get(SHARED)
        parts.append(_block(_shared_title(shared), shared["items"]) if shared else "Shared Team to-dos: none.")
    return "\n".join(parts)


def publish(service, *, notice=None):
    """Send the inspector the lists of the chat this (parent) service owns."""
    with service._mutex:
        chat = service.chat
        if not chat: return
        event = {"event": "todos", "chat": chat["id"]}
        try:
            event["todos"] = snapshot(chat)
        except (ValueError, TypeError, KeyError) as exc:
            event["todos"] = None
            notice = notice or str(exc) + " The transcript is unaffected."
        if notice is not None: event["notice"] = notice
        service.emit(event)
