"""Desktop projects whose chats ChatGPT Desktop will not let continue.

A chat in a ChatGPT Desktop project starts in the project's primary folder.
When that folder is the disk root, the chat's working directory is "/", and
Desktop's composer treats workspace roots made only of "/" as no workspace at
all: every follow-up is refused with "Unable to send message / Select a
project to continue" (the submit block the app calls ``missing-workspace``).
The first message of a new chat still goes through, because until the chat
exists the composer falls back to the home folder. So a fresh chat appears to
fix it for one turn. Projectless chats are exempt, but they never live at
"/". Read from the ChatGPT 26.1007.21159 bundle; its "Edit project" dialog
can add a folder and make it primary, which is the user's fix.

Provider Hub neither causes nor works around this. Faking a folder would run
a full-access agent somewhere the user did not choose. It only reports the
affected projects, by name, so the hub can say so before the user meets the
dialog. Desktop keeps two records of each local project: the older
``local-projects`` map in the global-state JSON beside its configuration, and
the app-server's ``projects``/``project_roots`` tables in the newest
``state_*.sqlite``. Both are read read-only, with bounded time. A missing file,
schema drift or timeout gives an empty report rather than an error, because
this is advice, not a launch requirement. No chat, title or other path is read.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from pathlib import Path

GLOBAL_STATE = ".codex-global-state.json"
# Desktop rewrites this file whole; 8 MB has been seen on a tester's Mac.
# Something far past that is not worth parsing for advice.
_STATE_LIMIT = 64 * 1024 * 1024
_STATE_DATABASE = re.compile(r"state_(\d+)\.sqlite")
_UNNAMED = "Untitled project"


def _is_disk_root(path) -> bool:
    return isinstance(path, str) and path != "" and path.strip("/") == ""


def _name(value) -> str:
    return value.strip() if isinstance(value, str) and value.strip() else _UNNAMED


def _global_state_projects(home: Path) -> list[str]:
    path = home / GLOBAL_STATE
    try:
        if path.stat().st_size > _STATE_LIMIT:
            return []
        with path.open(encoding="utf-8") as stream:
            state = json.load(stream)
    except (OSError, ValueError):
        return []
    projects = state.get("local-projects") if isinstance(state, dict) else None
    if not isinstance(projects, dict):
        return []
    names = []
    for entry in projects.values():
        roots = entry.get("rootPaths") if isinstance(entry, dict) else None
        if isinstance(roots, list) and roots and _is_disk_root(roots[0]):
            names.append(_name(entry.get("name")))
    return names


def _app_server_projects(home: Path) -> list[str]:
    candidates = [(int(match.group(1)), path) for path in home.glob("state_*.sqlite")
                  if (match := _STATE_DATABASE.fullmatch(path.name))]
    if not candidates:
        return []
    connection = None
    try:
        # Only the newest schema is authoritative, as for the sidebar accents.
        database = max(candidates)[1].resolve()
        connection = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=0.2)
        deadline = time.monotonic() + 0.5
        connection.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
        # The primary folder is the lowest position; it is the one a chat
        # starts in.
        rows = connection.execute(
            "SELECT p.name, r.path FROM projects p JOIN project_roots r ON r.project_id = p.id "
            "WHERE r.position = (SELECT MIN(position) FROM project_roots WHERE project_id = p.id)").fetchall()
        return [_name(name) for name, path in rows if _is_disk_root(path)]
    except (OSError, sqlite3.Error):
        return []
    finally:
        if connection is not None:
            connection.close()


def disk_root_projects(config_home: Path | None = None) -> list[str]:
    """Names of local Desktop projects whose primary folder is "/".

    The two records describe the same projects, so names are merged; the
    list is sorted for a stable message.
    """
    home = Path(config_home) if config_home is not None else Path(
        os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
    names = set(_global_state_projects(home)) | set(_app_server_projects(home))
    return sorted(names, key=lambda name: (name.casefold(), name))
