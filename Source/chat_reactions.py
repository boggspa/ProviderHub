"""Emoji reactions on transcript messages, iMessage tapback style.

A Team member reacts with ``react_to_message``; the user reacts from a
message's context menu (the ``react`` host command). A reaction is one emoji
per author per message: a later one replaces it, ``remove`` withdraws it.
Reactions live on the entry itself (``reactions``), so they share the chat's
atomic save and the Team checkpoint log and reach the window through the
ordinary entry event. They are presentation only: never projected into model
context, never read as approval, status or instructions.
"""
from __future__ import annotations

import copy
import json

from chat_tools import _tool

MAX_REACTIONS = 16
# A bound on the structure walk below, not a grapheme rule: a ZWJ family with
# four skin tones runs to eleven code points, so the limit sits well above.
MAX_CODEPOINTS = 32
TOOLS = {"react_to_message"}
COMMANDS = {"react"}
USER = {"id": "user", "name": "You", "route": ""}
TAPBACKS = ("❤️", "👍", "👎", "😂", "‼️", "❓")

TOOL_DEFINITIONS = [
    _tool("react_to_message", "React to the user's message or a peer's reply with one emoji, iMessage style "
          "(❤️ 👍 👎 😂 ‼️ ❓ or any single emoji). Optional and occasional: an acknowledgement, agreement, amusement, "
          "mild annoyance or a fun flourish, never a substitute for doing the work, answering, or recording a decision. "
          "Defaults to the user's latest message; pass entry_id for another visible user or peer message (a message ID "
          "from search_history or a recorded Team output source). One reaction per message per member: a new emoji "
          "replaces yours, remove withdraws it.", {
        "emoji": {"type": "string"}, "entry_id": {"type": "string"}, "remove": {"type": "boolean"}}, ["emoji"]),
]

GUIDANCE = """You may react to the user's message or a peer's reply with one emoji through
react_to_message, as a person would in iMessage: a thumbs up or heart to
acknowledge, a laugh, a raised eyebrow, or a fun flourish. Do it now and again
when it fits, not on every message, and never instead of working, answering or
recording a decision. Reactions are presentation only: nobody reads them as
approval, status or instructions.
"""

# Emoji-bearing blocks: pictographs, symbols, arrows, enclosed marks. Followed
# by modifiers (variation selectors, skin tones, keycap, tags) and ZWJ joins.
_BASE_RANGES = ((0x1F000, 0x1FAFF), (0x2600, 0x27BF), (0x2300, 0x23FF), (0x2B00, 0x2BFF), (0x2190, 0x21FF),
                (0x203C, 0x203C), (0x2049, 0x2049),
                (0x25AA, 0x25FE), (0x2934, 0x2935), (0x3030, 0x303D), (0x3297, 0x3299),
                (0x00A9, 0x00A9), (0x00AE, 0x00AE), (0x2122, 0x2122), (0x2139, 0x2139), (0x24C2, 0x24C2))
_MODIFIERS = {0xFE0E, 0xFE0F, 0x20E3} | set(range(0x1F3FB, 0x1F400)) | set(range(0xE0020, 0xE0080))
_ZWJ, _FLAG = 0x200D, (0x1F1E6, 0x1F1FF)


def _base(point):
    return any(low <= point <= high for low, high in _BASE_RANGES)


def normalize_emoji(value):
    """One emoji (a single grapheme such as 👍🏽, 👩‍💻, 🇬🇧 or 1️⃣), or ValueError."""
    if not isinstance(value, str):
        raise ValueError("emoji must be a single emoji.")
    text = value.strip()
    points = [ord(char) for char in text]
    if not 1 <= len(points) <= MAX_CODEPOINTS:
        raise ValueError("emoji must be exactly one emoji, such as 👍 or ❤️.")
    rest = points[1:]
    if _FLAG[0] <= points[0] <= _FLAG[1]:
        if not (len(points) == 2 and _FLAG[0] <= points[1] <= _FLAG[1]):
            raise ValueError("A flag is two regional indicator letters.")
        return text
    if text[0] in "#*0123456789":
        if len(points) < 2 or points[-1] != 0x20E3 or any(p != 0xFE0F for p in points[1:-1]):
            raise ValueError("emoji must be exactly one emoji, such as 👍 or ❤️.")
        return text
    if not _base(points[0]):
        raise ValueError("emoji must be exactly one emoji, such as 👍 or ❤️; letters and words are not reactions.")
    joined = False
    for point in rest:
        if point == _ZWJ:
            joined = True
        elif joined:
            if not _base(point):
                raise ValueError("emoji must be exactly one emoji, such as 👍 or ❤️.")
            joined = False
        elif point not in _MODIFIERS:
            raise ValueError("emoji must be exactly one emoji, such as 👍 or ❤️; send one reaction per call.")
    if joined:
        raise ValueError("emoji must be exactly one emoji, such as 👍 or ❤️.")
    return text


def messages(chat):
    """Entries a reaction can sit on: the user's messages and recorded replies."""
    return [item for item in chat.get("entries", []) if isinstance(item, dict) and item.get("id")
            and (item.get("kind") == "user" or (item.get("kind") == "assistant" and item.get("text")))]


def react(chat, author, *, emoji, entry_id=None, remove=False, now):
    """Set or withdraw ``author``'s reaction on one message.

    Returns (entry, status, previous): ``previous`` is the message's reaction
    list before the change (None when it had none), for a rollback if the
    save that follows fails.
    """
    candidates = messages(chat)
    if entry_id is None:
        item = next((item for item in reversed(candidates) if item["kind"] == "user"), None)
        if item is None:
            raise ValueError("There is no user message to react to yet.")
    else:
        if not isinstance(entry_id, str) or not entry_id or len(entry_id) > 128:
            raise ValueError("entry_id must be a visible message entry ID.")
        item = next((item for item in candidates if item["id"] == entry_id), None)
        if item is None:
            raise ValueError("No visible user or assistant message with that ID in this chat. Reactions go on messages, "
                             "not on tool rows or notices; find message IDs with search_history.")
    if author["id"] != "user" and item.get("memberID") == author["id"]:
        raise ValueError("React to the user's message or a peer's reply, not to your own.")
    previous = copy.deepcopy(item.get("reactions"))
    reactions = [row for row in item.get("reactions") or [] if isinstance(row, dict)]
    mine = next((row for row in reactions if row.get("author") == author["id"]), None)
    if remove:
        if mine is None:
            raise ValueError("You have no reaction on that message to withdraw.")
        reactions.remove(mine)
        status = "removed"
    else:
        symbol = normalize_emoji(emoji)
        if mine is None and len(reactions) >= MAX_REACTIONS:
            raise ValueError(f"That message already carries {MAX_REACTIONS} reactions.")
        row = {"emoji": symbol, "author": author["id"], "authorName": author["name"],
               "route": author.get("route", ""), "time": now}
        if mine is None:
            reactions.append(row)
            status = "added"
        else:
            status = "kept" if mine.get("emoji") == symbol else "replaced"
            reactions[reactions.index(mine)] = row
    if reactions:
        item["reactions"] = reactions
    else:
        item.pop("reactions", None)
    return item, status, previous


def _record(parent, item, previous, *, dirty=None):
    """Persist and publish a changed message; the caller holds the parent lock.

    A failed save restores the message's earlier reactions before raising,
    so a reaction the caller is told did not succeed cannot ride a later
    save.
    """
    import chat_team
    try:
        if not parent.busy:
            parent.save()
        elif chat_team.enabled(parent.chat):
            chat_team.checkpoint(parent, None, {item["id"]: item})
    except BaseException:
        if previous is None:
            item.pop("reactions", None)
        else:
            item["reactions"] = previous
        raise
    if dirty is not None:
        dirty[item["id"]] = item
    parent.emit({"event": "entry", "chat": parent.chat["id"], "entry": item})


def _listed(item):
    return [{"emoji": row["emoji"], "by": row.get("authorName", "")} for row in item.get("reactions") or []]


def describe(name, args):
    if name not in TOOLS or not isinstance(args, dict):
        raise ValueError("Invalid reaction tool.")
    if set(args) - {"emoji", "entry_id", "remove"}:
        raise ValueError("react_to_message takes emoji, entry_id and remove only.")
    if "remove" in args and type(args["remove"]) is not bool:
        raise ValueError("remove must be true or false.")
    emoji = args.get("emoji")
    summary = "Withdraw reaction" if args.get("remove") else "React " + (emoji.strip() if isinstance(emoji, str) else "")
    return {"summary": summary.strip(), "requires_approval": False}


def execute(service, name, args):
    """A Team member's reaction, recorded under the parent lock."""
    from chat_runtime import now
    describe(name, args)
    if service.role != "team":
        raise ValueError("Reactions are available only to Team members.")
    parent, member = service.team_parent, service.team_member
    author = {"id": member["id"], "name": member["name"], "route": member.get("route", "")}
    with parent._mutex:
        item, status, previous = react(parent.chat, author, emoji=args.get("emoji"), entry_id=args.get("entry_id"),
                                       remove=bool(args.get("remove", False)), now=now())
        _record(parent, item, previous, dirty=getattr(service.store, "dirty", None))
        output = {"status": status, "entry_id": item["id"], "reactions": _listed(item)}
    mine = next((row for row in item.get("reactions") or [] if row.get("author") == member["id"]), None)
    summary = "Withdrew reaction" if status == "removed" else "Reacted " + mine["emoji"]
    return {"content": [{"type": "text", "text": json.dumps(output, ensure_ascii=False)}],
            "is_error": False, "summary": summary, "changed_files": []}


def handle(service, command):
    """The user's reaction from the transcript. Returns False for other commands."""
    if command.get("command") not in COMMANDS:
        return False
    from chat_runtime import now
    with service._mutex:
        if not service.chat or command.get("id") != service.chat["id"]:
            raise ValueError("Select the chat before reacting to one of its messages.")
        item, _, previous = react(service.chat, USER, emoji=command.get("emoji"), entry_id=command.get("entry"),
                                  remove=bool(command.get("remove", False)), now=now())
        _record(service, item, previous)
    return True
