#!/usr/bin/env python3
"""work_guard — the between-commits half of the concurrent-work backstop.

WHY THIS EXISTS. Everything else that protects this shared checkout is
edge-triggered on `git commit`: the pre-commit hook, "raise your marker
before your first edit", "drop it in the same breath as your final commit".
On 2026-07-30 (in the repo this was ported from) all three failures happened
BETWEEN commits, where none of it runs — one session sat ~11 hours on 8,600
uncommitted lines without a single commit, so the hook was never invoked
once, and a visible bug shipped because its fix was sitting uncommitted the
whole time. That is one bug, not three: there was no clock.

This is the clock. Three jobs, none of which asks an agent to remember
anything:

  1. ORPHAN ALARM   Dirty paths that no LIVE marker claims, reported with
                    age. Deliberately needs no attribution — you never have
                    to work out whose file it is, only that nobody has
                    promised to finish it. That is derivable, and it is the
                    check that catches both of the above.

  2. SNAPSHOTS      The whole working tree into `refs/wip/`, out of band.
                    Makes losing work structurally impossible.

  3. HEARTBEAT      A `lastSeen` derived from mtimes of the dirty files a
                    marker claims, so a claim survives pid churn and a
                    badly-guessed `expires` instead of falsely decaying.

Everything here is READ-ONLY with respect to shared state. It never writes
the index, the working tree, another session's marker, or any branch, and it
never pushes. Local-only by design, so it does NOT belong in any CI chain:
CI checks out clean and has no sessions or markers to reason about.

  python3 scripts/work_guard.py status     human report (default)
  python3 scripts/work_guard.py status --hook   terse advisory for the hook
  python3 scripts/work_guard.py tick       heartbeat + snapshot + prune (timer)
  python3 scripts/work_guard.py snapshot   one-shot snapshot
  python3 scripts/work_guard.py check      exit 1 on aged orphans (ship gate)
  python3 scripts/work_guard.py timer      print a launchd plist to install
  python3 scripts/work_guard.py self-check prove current file parses + runs

This is a port of work-guard.cjs from the TaskWraith repo, adapted for this
Python codebase. Provenance (work-provenance.cjs, 1771 lines) was not ported:
it depends on TaskWraith broker receipts that don't exist here, and it is
explicitly additive assurance. The three load-bearing jobs above do not
need it.
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

# ── constants (must match .githooks/pre-commit MAX_LEASE_SECONDS) ──────────

HEARTBEAT_STALE_MS = 20 * 60 * 1000
"""A claim whose heartbeat is older than this is no longer self-evidently live."""

ORPHAN_WARN_MS = 45 * 60 * 1000
"""Unclaimed dirty work older than this is what we are actually hunting."""

MAX_LEASE_MS = 20 * 60 * 1000
"""
A manual claim's lease ceiling, renewed by hand. Must stay identical to
MAX_LEASE_SECONDS in `.githooks/pre-commit`: if these two drift, the hook
blocks on a claim this tool reports decayed, and a decayed claim is the one
AGENTS.md tells the next agent to harvest and delete.
"""

SNAPSHOT_KEEP_MS = 7 * 24 * 60 * 60 * 1000
SNAPSHOT_KEEP_MAX = 300

SIDECAR_DIR = ".work-guard"
SIDECAR_FILE = "heartbeat.json"
TICK_FILE = "tick.json"

TIMER_INTERVAL_SECONDS = 300
TICK_STALE_MS = 20 * 60 * 1000

# ── marker filename patterns ────────────────────────────────────────────────

# Full runtime markers carry pid + lockOwnerId + birthReceiptHash.
RUNTIME_MARKER_RE = re.compile(
    r"^\.WORK-IN-PROGRESS-taskwraith-runtime-[A-Za-z0-9_-]+-[a-f0-9]{64}\.md$"
)
# Contribution markers carry lockOwnerId + expires + paths, no pid.
CONTRIBUTION_MARKER_RE = re.compile(
    r"^\.WORK-IN-PROGRESS-taskwraith-contribution-[a-f0-9]{64}\.md$"
)
MARKER_RE = re.compile(
    r"^(?:\.WORK-IN-PROGRESS-.+\.md|SHIP-HOLD-.+\.md|SESSION-IN-PROGRESS-.+\.md)$"
)


def is_runtime_marker_name(name: str) -> bool:
    """Full runtime or contribution projection — not adoptable, owner-id-only."""
    return bool(RUNTIME_MARKER_RE.match(name) or CONTRIBUTION_MARKER_RE.match(name))


def is_human_claim_marker_name(name: str) -> bool:
    if not MARKER_RE.match(name):
        return False
    if name.startswith(".WORK-IN-PROGRESS-") and is_runtime_marker_name(name):
        return False
    return True


def is_marker_file(name: str) -> bool:
    return is_human_claim_marker_name(name) or is_runtime_marker_name(name)


def is_contribution_marker_name(name: str) -> bool:
    return bool(CONTRIBUTION_MARKER_RE.match(name))


# ── git plumbing ────────────────────────────────────────────────────────────

def _git(root: str, args: list[str], env: dict | None = None) -> str:
    result = subprocess.run(
        ["git", "-c", "core.fsmonitor=false", *args],
        cwd=root,
        capture_output=True,
        text=True,
        env={**os.environ, "GIT_OPTIONAL_LOCKS": "0", **(env or {})},
    )
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


def _git_quiet(root: str, args: list[str], env: dict | None = None) -> str | None:
    try:
        return _git(root, args, env)
    except Exception:
        return None


def repo_root() -> str | None:
    try:
        return subprocess.run(
            ["git", "-c", "core.fsmonitor=false", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
        ).stdout.strip()
    except Exception:
        return None


def dirty_entries(root: str) -> list[dict]:
    """Dirty paths with their statuses and mtimes.

    `-z` because filenames may contain anything; rename/copy statuses carry
    TWO NUL-separated paths and the destination is the one on disk, so the
    source entry is consumed and dropped.
    """
    raw = _git_quiet(root, ["status", "--porcelain", "-z", "--untracked-files=all"])
    if raw is None:
        return []
    fields = raw.split("\0")
    out = []
    i = 0
    while i < len(fields):
        field = fields[i]
        if not field or len(field) < 4:
            i += 1
            continue
        status = field[:2]
        file = field[3:]
        renamed_from = None
        if status[0] in ("R", "C"):
            renamed_from = fields[i + 1] if i + 1 < len(fields) else None
            i += 1  # next field is rename source; the slice above is destination
        if not file:
            i += 1
            continue
        mtime_ms = None
        try:
            mtime_ms = os.path.getmtime(os.path.join(root, file)) * 1000
        except OSError:
            pass  # deleted paths have no mtime; still dirty, just ageless
        entry = {"status": status, "path": file, "mtimeMs": mtime_ms}
        if renamed_from:
            entry["renamedFrom"] = renamed_from
        out.append(entry)
        i += 1
    return out


# ── marker parsing ──────────────────────────────────────────────────────────

def parse_iso_ms(value: str | None) -> float | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        ms = dt.timestamp() * 1000
        return ms if float("-inf") < ms < float("inf") else None
    except (ValueError, TypeError):
        return None


def _extract_frontmatter(text: str) -> str:
    """Claim fields may be read ONLY from the frontmatter block, never from
    the prose body. A marker's body routinely discusses these keys — a marker
    describing this very mechanism would name `lockOwnerId:` in a sentence —
    and before this gate that body text was parsed as a real field."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return ""
    body = []
    for line in lines[1:]:
        if line.strip() == "---":
            break
        body.append(line)
    return "\n".join(body)


def _parse_scalar(frontmatter: str, key: str) -> str | None:
    pattern = re.compile(rf"^{re.escape(key)}:[ \t]*(.+)$", re.MULTILINE)
    m = pattern.search(frontmatter)
    if not m:
        return None
    raw = m.group(1).strip()
    # Decode the JSON-quoted dialect emitted by TaskWraith's projector.
    if len(raw) >= 2 and raw[0] in '"\'' and raw[-1] == raw[0]:
        try:
            value = json.loads(raw) if raw[0] == '"' else raw[1:-1]
        except json.JSONDecodeError:
            value = raw[1:-1]
    else:
        value = raw
    value = str(value).strip()
    return value or None


def _parse_list(frontmatter: str, key: str) -> list[str]:
    lines = frontmatter.splitlines()
    out = []
    in_list = False
    list_re = re.compile(rf"^{re.escape(key)}:[ \t]*$")
    item_re = re.compile(r"^[ \t]*-[ \t]+(.+)$")
    for line in lines:
        if list_re.match(line):
            in_list = True
            continue
        if not in_list:
            continue
        m = item_re.match(line)
        if m:
            claim = _normalise_claim(m.group(1))
            if claim:
                out.append(claim)
            continue
        if line.strip() == "":
            continue
        in_list = False
    return out


def _normalise_claim(raw: str) -> str:
    claim = raw.strip()
    # Strip trailing parenthetical scope notes.
    claim = re.sub(r"\s*\([^)]*\)\s*$", "", claim)
    if claim.startswith('"') and claim.endswith('"'):
        try:
            claim = json.loads(claim)
        except json.JSONDecodeError:
            claim = claim[1:-1]
    elif claim.startswith("'") and claim.endswith("'"):
        claim = claim[1:-1]
    return claim.strip()


def _claim_to_matcher(claim: str) -> Callable[[str], bool]:
    if claim.endswith("/"):
        prefix = claim
        return lambda f: f == claim[:-1] or f.startswith(prefix)
    if "*" in claim:
        return lambda f: fnmatch.fnmatchcase(f, claim)
    # A bare path may name a file or a directory without a trailing slash.
    return lambda f: f == claim or f.startswith(f"{claim}/")


def parse_marker(root: str, filename: str) -> dict | None:
    try:
        text = Path(root, filename).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    fm = _extract_frontmatter(text)
    if not fm:
        return None
    paths = _parse_list(fm, "paths")
    trees = _parse_list(fm, "trees")
    workspace_wide = re.match(r"^true$", (_parse_scalar(fm, "workspaceWide") or ""), re.I) is not None
    derived = re.match(r"^true$", (_parse_scalar(fm, "derived") or ""), re.I) is not None
    pid_raw = _parse_scalar(fm, "pid")
    pid = int(pid_raw) if pid_raw and pid_raw.isdigit() else None
    expires_str = _parse_scalar(fm, "expires")
    claims = paths + trees
    return {
        "file": filename,
        "session": _parse_scalar(fm, "session"),
        "taskId": _parse_scalar(fm, "taskId"),
        "task": _parse_scalar(fm, "task"),
        "agent": _parse_scalar(fm, "agent"),
        "owner": _parse_scalar(fm, "owner"),
        "runId": _parse_scalar(fm, "runId"),
        "provider": _parse_scalar(fm, "provider"),
        "participantId": _parse_scalar(fm, "participantId"),
        "laneId": _parse_scalar(fm, "laneId"),
        "lockOwnerId": _parse_scalar(fm, "lockOwnerId"),
        "authorityInstanceId": _parse_scalar(fm, "authorityInstanceId"),
        "processBirthReceiptHash": _parse_scalar(fm, "birthReceiptHash"),
        "started": _parse_scalar(fm, "started"),
        "worktree": _parse_scalar(fm, "worktree"),
        "pid": pid,
        "expires": expires_str,
        "expiresMs": parse_iso_ms(expires_str),
        "derived": derived,
        "workspaceWide": workspace_wide,
        "paths": claims,
        "matchers": ([lambda f: True] if workspace_wide
                     else [_claim_to_matcher(c) for c in claims]),
    }


def list_markers(root: str) -> list[dict]:
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return []
    markers = []
    for name in names:
        if not is_marker_file(name):
            continue
        m = parse_marker(root, name)
        if m:
            markers.append(m)
    return markers


def pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # EPERM means the pid exists but belongs to another user


# ── heartbeat sidecar ───────────────────────────────────────────────────────

def _sidecar_path(root: str) -> Path:
    return Path(root, SIDECAR_DIR, SIDECAR_FILE)


def read_sidecar(root: str) -> dict:
    try:
        data = json.loads(_sidecar_path(root).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def write_sidecar(root: str, data: dict) -> None:
    d = Path(root, SIDECAR_DIR)
    try:
        d.mkdir(parents=True, exist_ok=True)
        tmp = d / f".{SIDECAR_FILE}.{os.getpid()}.tmp"
        tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        tmp.replace(_sidecar_path(root))
    except OSError:
        pass  # a sidecar we cannot write only costs the heartbeat upgrade


# ── timer liveness ──────────────────────────────────────────────────────────

def _tick_record_path(root: str) -> Path:
    return Path(root, SIDECAR_DIR, TICK_FILE)


def read_tick_record(root: str) -> dict | None:
    try:
        data = json.loads(_tick_record_path(root).read_text(encoding="utf-8"))
        last_tick_ms = data.get("lastTickMs")
        if not isinstance(last_tick_ms, (int, float)):
            return None
        return {
            "lastTickMs": float(last_tick_ms),
            "node": data.get("node") if isinstance(data.get("node"), str) else None,
            "parseOk": data.get("parseOk") is True or (data.get("parseOk") is None),
        }
    except (OSError, json.JSONDecodeError):
        return None


def write_tick_record(root: str, now_ms: float) -> None:
    try:
        Path(root, SIDECAR_DIR).mkdir(parents=True, exist_ok=True)
        tmp = Path(root, SIDECAR_DIR, f".{TICK_FILE}.{os.getpid()}.tmp")
        tmp.write_text(
            json.dumps(
                {"lastTickMs": now_ms, "node": sys.version, "parseOk": True},
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        tmp.replace(_tick_record_path(root))
    except OSError:
        pass


def timer_health(root: str, now_ms: float) -> dict:
    record = read_tick_record(root)
    if not record:
        return {"everRan": False, "stale": True, "ageMs": None, "node": None, "parseOk": None}
    age_ms = now_ms - record["lastTickMs"]
    return {
        "everRan": True,
        "stale": age_ms > TICK_STALE_MS,
        "ageMs": age_ms,
        "node": record["node"],
        "parseOk": record["parseOk"],
    }


# ── heartbeat derivation ────────────────────────────────────────────────────

def advance_heartbeats(root: str, markers: list[dict], dirty: list[dict], now_ms: float) -> dict:
    """Derive `lastSeen` from the newest mtime among a marker's own dirty
    paths, plus a live pid. Lives in a sidecar, never inside the marker."""
    side = read_sidecar(root)
    schema_v2 = side.get("schemaVersion") == 2
    markers_map = side.get("markers", {}) if schema_v2 else {}
    if not schema_v2:
        markers_map = {k: v for k, v in side.items() if isinstance(v, dict)}

    next_map = dict(markers_map)
    for marker in markers:
        entry = next_map.get(marker["file"], {})
        last_seen = entry.get("lastSeen")
        # Find newest mtime among this marker's claimed dirty paths.
        for d in dirty:
            if is_marker_file(d["path"]):
                continue
            if any(matcher(d["path"]) for matcher in marker["matchers"]):
                if d["mtimeMs"] and (last_seen is None or d["mtimeMs"] > last_seen):
                    last_seen = d["mtimeMs"]
        # A live pid also counts, so a thinking session does not decay.
        if pid_alive(marker["pid"]):
            if last_seen is None or now_ms - last_seen > HEARTBEAT_STALE_MS:
                last_seen = now_ms
        if last_seen is not None:
            next_map[marker["file"]] = {**entry, "lastSeen": last_seen}

    return {"schemaVersion": 2, "markers": next_map}


def liveness(marker: dict, side: dict, now_ms: float) -> dict:
    markers_map = side.get("markers", {}) if side.get("schemaVersion") == 2 else side
    entry = markers_map.get(marker["file"], {}) if isinstance(markers_map, dict) else {}
    last_seen = entry.get("lastSeen")
    last_seen = last_seen if isinstance(last_seen, (int, float)) else None
    heartbeat_fresh = last_seen is not None and (now_ms - last_seen) < HEARTBEAT_STALE_MS
    alive = pid_alive(marker["pid"])

    # Lease ceiling: clamped, not voided, anchored to `started`.
    started_ms = parse_iso_ms(marker["started"])
    if marker["expiresMs"] is None:
        capped_expiry = None
    elif started_ms is None:
        capped_expiry = None if (now_ms + MAX_LEASE_MS < marker["expiresMs"]) else marker["expiresMs"]
    else:
        capped_expiry = min(marker["expiresMs"], started_ms + MAX_LEASE_MS)
    expired = capped_expiry is None or now_ms > capped_expiry

    lease_held = marker["expiresMs"] is not None and not expired
    valid_opaque = bool(marker["lockOwnerId"]) and bool(
        re.match(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
                 str(marker["lockOwnerId"]), re.I)
    )
    owner_held = valid_opaque and lease_held

    return {
        "live": heartbeat_fresh or (alive and not expired) or owner_held,
        "heartbeatFresh": heartbeat_fresh,
        "alive": alive,
        "ownerHeld": owner_held,
        "expired": expired,
        "lastSeen": last_seen,
    }


# ── snapshots ───────────────────────────────────────────────────────────────

def take_snapshot(root: str, label: str | None = None) -> dict:
    """Capture the working tree into the object database without touching the
    shared index. `git stash create` is WRONG: it does not capture untracked
    files. Building a tree through a throwaway GIT_INDEX_FILE captures tracked
    modifications, another session's staged work, and untracked files, while
    leaving `.git/index` byte-identical."""
    head = _git_quiet(root, ["rev-parse", "--verify", "HEAD"])
    if head is None:
        return {"ok": False, "reason": "no HEAD"}
    head = head.strip()

    import tempfile
    tmp_fd, tmp_index = tempfile.mkstemp(prefix="work-guard-index-", suffix=".idx")
    os.close(tmp_fd)
    env = {"GIT_INDEX_FILE": tmp_index}
    try:
        if _git_quiet(root, ["read-tree", "HEAD"], env) is None:
            return {"ok": False, "reason": "read-tree failed"}
        if _git_quiet(root, ["add", "-A"], env) is None:
            return {"ok": False, "reason": "add failed"}
        # Markers are gitignored, so `-A` skips them — but they are the only
        # record of what a dead session was doing. Force them in.
        try:
            marker_files = [f for f in os.listdir(root) if is_marker_file(f)]
            if marker_files:
                _git_quiet(root, ["add", "-f", "--", *marker_files], env)
        except OSError:
            pass
        tree = _git_quiet(root, ["write-tree"], env)
        if tree is None:
            return {"ok": False, "reason": "write-tree failed"}
        tree_sha = tree.strip()

        # Skip identical trees so an idle machine does not accumulate refs.
        snaps = list_snapshots(root)
        if snaps:
            prior = _git_quiet(root, ["rev-parse", f"{snaps[0]['ref']}^{{tree}}"])
            if prior and prior.strip() == tree_sha:
                return {"ok": True, "skipped": True, "ref": snaps[0]["ref"]}

        commit = _git_quiet(
            root,
            ["commit-tree", tree_sha, "-p", head, "-m", label or "work-guard snapshot"],
            env,
        )
        if commit is None:
            return {"ok": False, "reason": "commit-tree failed"}
        sha = commit.strip()
        # Second-resolution stamps need the sha to disambiguate same-second snaps.
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        ref = f"refs/wip/{stamp}-{sha[:7]}"
        if _git_quiet(root, ["update-ref", ref, sha]) is None:
            return {"ok": False, "reason": "update-ref failed"}
        return {"ok": True, "ref": ref, "commit": sha}
    finally:
        try:
            os.unlink(tmp_index)
        except OSError:
            pass


def list_snapshots(root: str) -> list[dict]:
    raw = _git_quiet(root, [
        "for-each-ref",
        "--sort=-committerdate",
        "--format=%(refname) %(committerdate:unix)",
        "refs/wip/",
    ])
    if not raw:
        return []
    out = []
    for line in raw.strip().splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        ref, unix = parts[0], parts[1]
        try:
            at_ms = int(unix) * 1000
        except ValueError:
            continue
        out.append({"ref": ref, "atMs": at_ms})
    return out


def prune_snapshots(root: str, now_ms: float) -> int:
    all_snaps = list_snapshots(root)
    doomed = [
        s for i, s in enumerate(all_snaps)
        if i >= SNAPSHOT_KEEP_MAX or (now_ms - s["atMs"]) > SNAPSHOT_KEEP_MS
    ]
    for entry in doomed:
        if all_snaps and entry["ref"] == all_snaps[0]["ref"]:
            continue  # always leave newest
        _git_quiet(root, ["update-ref", "-d", entry["ref"]])
    return len(doomed)


# ── evaluation ──────────────────────────────────────────────────────────────

def _realpath_probe(candidate: str) -> str | None:
    try:
        return os.path.realpath(candidate)
    except OSError:
        return None


def marker_targets_root(root: str, marker: dict) -> bool:
    declared = marker["worktree"] or ""
    target_root = os.path.join(root, declared) if declared else root
    return (_realpath_probe(target_root) or os.path.abspath(target_root)) == (
        _realpath_probe(root) or os.path.abspath(root)
    )


def evaluate(root: str, now_ms: float) -> dict:
    markers = list_markers(root)
    dirty = dirty_entries(root)
    side = read_sidecar(root)
    assessed = [
        {"marker": m, "state": liveness(m, side, now_ms)}
        for m in markers
    ]
    live_markers = [
        entry["marker"] for entry in assessed
        if entry["state"]["live"] and marker_targets_root(root, entry["marker"])
    ]
    orphans = []
    for d in dirty:
        if is_marker_file(d["path"]):
            continue
        if any(matcher(d["path"]) for m in live_markers for matcher in m["matchers"]):
            continue
        age_ms = d["mtimeMs"] - now_ms if d["mtimeMs"] else None
        orphans.append({**d, "ageMs": age_ms})
    orphans.sort(key=lambda o: o["ageMs"] or 0, reverse=True)
    return {
        "markers": assessed,
        "dirty": dirty,
        "orphans": orphans,
        "aged": [o for o in orphans if (o["ageMs"] or 0) > ORPHAN_WARN_MS],
    }


def human_age(ms: float | None) -> str:
    if ms is None or not float("-inf") < ms < float("inf"):
        return "unknown age"
    mins = int(ms / 60000)
    if mins < 60:
        return f"{mins}m"
    hours = mins // 60
    if hours < 48:
        return f"{hours}h{mins % 60:02d}m"
    return f"{hours // 24}d"


# ── commands ────────────────────────────────────────────────────────────────

def cmd_status(root: str, now_ms: float, json_out: bool, hook: bool) -> int:
    result = evaluate(root, now_ms)
    if hook:
        if result["aged"]:
            oldest = result["aged"][0]
            print(
                f"  note: {len(result['aged'])} dirty path(s) claimed by nobody, "
                f"oldest {human_age(oldest['ageMs'])} — {oldest['path']}\n"
                f"        `python3 scripts/work_guard.py status` lists them."
            )
        timer = timer_health(root, now_ms)
        if timer["everRan"] and timer["stale"]:
            print(
                f"  note: work-guard timer looks dead — last tick "
                f"{human_age(timer['ageMs'])} ago. Snapshots are NOT being taken."
            )
        return 0

    if json_out:
        print(json.dumps({
            "markers": [
                {
                    "file": e["marker"]["file"],
                    "agent": e["marker"]["agent"],
                    "pid": e["marker"]["pid"],
                    **e["state"],
                }
                for e in result["markers"]
            ],
            "orphans": result["orphans"],
            "agedOrphanCount": len(result["aged"]),
        }, indent=2))
        return 0

    lines = ["CLAIMS"]
    if not result["markers"]:
        lines.append("  (none)")
    for entry in result["markers"]:
        m, s = entry["marker"], entry["state"]
        if s["heartbeatFresh"]:
            why = f"heartbeat {human_age(now_ms - s['lastSeen'])} ago"
        elif s["alive"]:
            why = f"pid {m['pid']} alive"
        else:
            why = "no live signal"
        pid_dead = f", pid {m['pid']} dead" if m['pid'] and not s['alive'] else ""
        lines.append(
            f"  {'LIVE   ' if s['live'] else 'DECAYED'} {m['file']}\n"
            f"          {m['agent'] or 'unknown agent'}\n"
            f"          {why}{', expires PASSED' if s['expired'] else ''}{pid_dead}"
        )
    lines.append("")
    lines.append(f"UNCLAIMED DIRTY PATHS ({len(result['orphans'])})")
    if not result["orphans"]:
        lines.append("  (none — every dirty path is claimed by a live session)")
    for orphan in result["orphans"][:40]:
        flag = "  <-- aged" if (orphan["ageMs"] or 0) > ORPHAN_WARN_MS else ""
        lines.append(f"  {orphan['status']}  {human_age(orphan['ageMs']):>7}  {orphan['path']}{flag}")
    if len(result["orphans"]) > 40:
        lines.append(f"  … and {len(result['orphans']) - 40} more")

    snaps = list_snapshots(root)
    lines.append("")
    lines.append(
        f"SNAPSHOTS  {len(snaps)} in refs/wip/"
        + (f", newest {human_age(now_ms - snaps[0]['atMs'])} ago ({snaps[0]['ref']})"
           if snaps else "")
    )
    timer = timer_health(root, now_ms)
    if not timer["everRan"]:
        lines.append("TIMER      never run — `python3 scripts/work_guard.py timer` prints the launchd agent")
    elif timer["stale"]:
        lines.append(f"TIMER      STALE, last tick {human_age(timer['ageMs'])} ago — NOT snapshotting")
    else:
        node_info = f" under {timer['node']}" if timer["node"] else ""
        parse_info = " (parse ok)" if timer["parseOk"] else ""
        lines.append(f"TIMER      healthy, last tick {human_age(timer['ageMs'])} ago{node_info}{parse_info}")
    print("\n".join(lines))
    return 0


def cmd_tick(root: str, now_ms: float) -> int:
    markers = list_markers(root)
    dirty = dirty_entries(root)
    sidecar = advance_heartbeats(root, markers, dirty, now_ms)
    label = f"work-guard snapshot — {len(dirty)} dirty, {len(markers)} claim(s)"
    snap = take_snapshot(root, label)
    write_sidecar(root, sidecar)
    prune_snapshots(root, now_ms)
    write_tick_record(root, now_ms)
    result = evaluate(root, now_ms)
    if result["aged"]:
        snap_ref = f" — snapshot {snap['ref']}" if snap.get("ok") and snap.get("ref") else ""
        print(
            f"work-guard: {len(result['aged'])} unclaimed dirty path(s), "
            f"oldest {human_age(result['aged'][0]['ageMs'])} "
            f"({result['aged'][0]['path']}){snap_ref}"
        )
    return 0


def cmd_snapshot(root: str) -> int:
    snap = take_snapshot(root, "work-guard snapshot (manual)")
    if snap["ok"]:
        if snap.get("skipped"):
            print(f"work-guard: tree unchanged since {snap['ref']}")
        else:
            print(f"work-guard: {snap['ref']}")
        return 0
    print(f"work-guard: snapshot failed ({snap.get('reason')})")
    return 1


def cmd_check(root: str, now_ms: float) -> int:
    result = evaluate(root, now_ms)
    if not result["aged"]:
        print("work-guard: no aged unclaimed work.")
        return 0
    print(
        f"work-guard: {len(result['aged'])} dirty path(s) older than "
        f"{int(ORPHAN_WARN_MS / 60000)}m that no live claim covers:",
        file=sys.stderr,
    )
    for orphan in result["aged"][:20]:
        print(f"  {human_age(orphan['ageMs']):>7}  {orphan['path']}", file=sys.stderr)
    print("Commit it, claim it, or confirm it is disposable before shipping.", file=sys.stderr)
    return 1


def _stable_python_path() -> str:
    """The launchd plist must not name a versioned interpreter path. Homebrew
    Cellar paths are deleted on upgrade; prefer a stable symlink."""
    exec_path = sys.executable
    target = _realpath_probe(exec_path)
    if not target:
        return exec_path
    for candidate in ("/opt/homebrew/bin/python3", "/usr/local/bin/python3", "/usr/bin/python3"):
        if candidate == exec_path:
            continue
        if _realpath_probe(candidate) == target:
            return candidate
    return exec_path


def build_timer_plist(root: str, python_bin: str) -> str:
    """ProcessType is deliberately NOT `Background`. That is launchd's lowest
    band: CPU and I/O are throttled and the interval timer is aggressively
    coalesced. `Standard` asks launchd not to defer a job that runs for a
    second or two every five minutes."""
    label = "com.mistral-bridge.work-guard"
    log_dir = os.path.join(root, SIDECAR_DIR)
    script = os.path.join(root, "scripts", "work_guard.py")
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{label}</string>
  <key>ProgramArguments</key>
  <array>
    <string>{python_bin}</string>
    <string>{script}</string>
    <string>tick</string>
  </array>
  <key>WorkingDirectory</key><string>{root}</string>
  <key>StartInterval</key><integer>{TIMER_INTERVAL_SECONDS}</integer>
  <key>RunAtLoad</key><true/>
  <key>ProcessType</key><string>Standard</string>
  <key>StandardOutPath</key><string>{os.path.join(log_dir, 'tick.log')}</string>
  <key>StandardErrorPath</key><string>{os.path.join(log_dir, 'tick.log')}</string>
</dict>
</plist>
"""


def cmd_timer(root: str) -> int:
    print(build_timer_plist(root, _stable_python_path()))
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Between-commits concurrent-work guard.")
    parser.add_argument("command", nargs="?", default="status")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--hook", action="store_true")
    args = parser.parse_args(argv)

    if args.command == "self-check":
        print(f"work-guard: self-check ok under {sys.version} (parse+load succeeded)")
        return 0

    root = repo_root()
    if not root:
        if args.hook:
            return 0
        print("work-guard: not inside a git repository.", file=sys.stderr)
        return 1

    now_ms = time.time() * 1000

    if args.command == "status":
        return cmd_status(root, now_ms, args.json, args.hook)
    if args.command == "tick":
        return cmd_tick(root, now_ms)
    if args.command == "snapshot":
        return cmd_snapshot(root)
    if args.command == "check":
        return cmd_check(root, now_ms)
    if args.command == "timer":
        return cmd_timer(root)
    print(f"work-guard: unknown command '{args.command}'", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
