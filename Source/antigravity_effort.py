"""Thinking effort for Hub models in Antigravity, remembered per conversation.

Antigravity's language server discovers at most 32 models and drops every
effort field the Gemini model list could carry, so the Hub advertises one row
per model and adds its own "Thinking effort" submenu to the picker
(``antigravity_accent``). A pick there is recorded here, keyed by the
conversation (Antigravity's cascade id, which the language server sends as
``labels.cascade_id`` with every turn) and by the model's flat id. The gateway
reads it back when a turn arrives (``gemini_bridge.serve``).

The record is small and private to the Hub's state directory::

    {"conversations": {"<cascade id>": {"<model id>": "<effort>"}},
     "last": {"<model id>": "<effort>"}}

``last`` is the most recent pick for a model anywhere. A conversation that
has no pick of its own adopts it on its first turn, so a choice made on the
new-chat page applies to the chat it starts and later picks elsewhere do not
change a conversation already under way.
"""
from __future__ import annotations

import fcntl
import os
import re
from contextlib import contextmanager
from pathlib import Path

from bridge_core import BridgeError, atomic_json, read_json

STORE = "antigravity-effort.json"
LOCK = ".antigravity-effort.lock"
#: Conversations remembered; the least recently touched go first.
LIMIT = 400
_CASCADE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


def valid_cascade(value) -> bool:
    return isinstance(value, str) and bool(_CASCADE.fullmatch(value))


def _empty() -> dict:
    return {"conversations": {}, "last": {}}


def _normalise(value) -> dict:
    state = _empty()
    if not isinstance(value, dict):
        return state
    conversations = value.get("conversations")
    if isinstance(conversations, dict):
        for cascade, picks in conversations.items():
            if valid_cascade(cascade) and isinstance(picks, dict):
                kept = {model: effort for model, effort in picks.items()
                        if isinstance(model, str) and isinstance(effort, str)}
                if kept:
                    state["conversations"][cascade] = kept
    last = value.get("last")
    if isinstance(last, dict):
        state["last"] = {model: effort for model, effort in last.items()
                         if isinstance(model, str) and isinstance(effort, str)}
    return state


@contextmanager
def _locked(root: Path):
    """One writer at a time across the gateway's threads and the helper."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(root / LOCK, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


def load(root: Path) -> dict:
    try:
        return _normalise(read_json(Path(root) / STORE))
    except BridgeError:
        return _empty()


def _touch(state: dict, cascade: str, model: str, effort: str) -> None:
    conversations = state["conversations"]
    picks = conversations.pop(cascade, {})
    picks[model] = effort
    conversations[cascade] = picks  # most recently touched last
    while len(conversations) > LIMIT:
        conversations.pop(next(iter(conversations)))


def record(root: Path, events, choices: dict[str, set[str]]) -> dict:
    """Apply the picker's picks; ``choices`` maps model ids to their levels.

    A pick for a model or level the catalogue does not offer, or with a
    conversation id that is not one, is ignored.
    """
    with _locked(root):
        state = load(root)
        changed = False
        for event in events if isinstance(events, list) else []:
            if not isinstance(event, dict):
                continue
            model, effort, cascade = event.get("model"), event.get("effort"), event.get("cascade")
            if not isinstance(model, str) or not isinstance(effort, str) or effort not in choices.get(model, ()):
                continue
            if cascade is not None and not valid_cascade(cascade):
                continue
            state["last"][model] = effort
            if cascade is not None:
                _touch(state, cascade, model, effort)
            changed = True
        if changed:
            atomic_json(Path(root) / STORE, state)
        return state


def resolve(root: Path, cascade, row: dict) -> str | None:
    """The level one turn of a conversation runs at, or None for no override.

    Turns without a conversation id (Antigravity's title requests) keep
    Antigravity's own thinking config. A conversation without a pick of its
    own adopts the most recent pick for the model, else the model's default.
    """
    modes = row.get("effort_modes") or []
    if not modes or not valid_cascade(cascade):
        return None
    model = row["model_name"]
    default = row.get("default_effort") if row.get("default_effort") in modes else modes[-1]
    try:
        with _locked(root):
            state = load(root)
            chosen = state["conversations"].get(cascade, {}).get(model)
            if chosen in modes:
                return chosen
            chosen = state["last"].get(model)
            effort = chosen if chosen in modes else default
            _touch(state, cascade, model, effort)
            atomic_json(Path(root) / STORE, state)
            return effort
    except (OSError, BridgeError):
        # The turn still runs at a sensible level when the record cannot be
        # written; it simply is not remembered.
        return default
