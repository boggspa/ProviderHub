"""Bounded local Git inspection and explicit, non-forcing workspace actions.

All returned paths are repository-relative except root/worktree paths. Counts
describe the combined HEAD-to-working-tree diff, not a sum of index changes.
"""
import difflib
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import selectors
import subprocess
import time

MAX_OUTPUT = 2_000_000
MAX_DIFF = 64_000
MAX_TOTAL = 512_000
MAX_FILES = 100


def _git(workspace, *args, limit=MAX_OUTPUT, timeout=10, allow_failure=False):
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(GIT_TERMINAL_PROMPT="0", GIT_OPTIONAL_LOCKS="0", GIT_LITERAL_PATHSPECS="1", LC_ALL="C")
    command = ["git", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false",
               "-c", "core.untrackedCache=false", "-c", "color.ui=false", "-C", str(workspace), *args]
    process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, env=env)
    chunks = {process.stdout: bytearray(), process.stderr: bytearray()}
    total, truncated = 0, False
    deadline = time.monotonic() + timeout
    try:
        with selectors.DefaultSelector() as selector:
            for stream in chunks:
                selector.register(stream, selectors.EVENT_READ)
            while selector.get_map():
                if time.monotonic() >= deadline:
                    raise ValueError("Git operation timed out. Inspect the repository before retrying.")
                for key, _ in selector.select(min(0.1, max(0, deadline - time.monotonic()))):
                    data = os.read(key.fileobj.fileno(), 65536)
                    if not data:
                        selector.unregister(key.fileobj)
                        continue
                    remaining = max(0, limit - total)
                    chunks[key.fileobj].extend(data[:remaining])
                    total += len(data)
                    if total > limit:
                        truncated = True
                        process.kill()
                        break
                if truncated:
                    break
        process.wait(timeout=max(0.1, deadline - time.monotonic()))
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()
        for stream in chunks:
            stream.close()
    stdout, stderr = bytes(chunks[process.stdout]), bytes(chunks[process.stderr])
    if process.returncode and not allow_failure and not truncated:
        raise ValueError(stderr.decode("utf-8", "replace").strip() or "Git operation failed.")
    return stdout, truncated, process.returncode


def _complete(workspace, *args):
    data, truncated, _ = _git(workspace, *args)
    if truncated:
        raise ValueError("Git output exceeded the inspection limit. Inspect the repository before retrying.")
    return data


def _text(data):
    return os.fsdecode(data)


def _display_path(path):
    # Git paths remain lossless surrogateescaped strings until all Git/file
    # operations finish. Only presentation uses escaped byte spelling. Escape
    # literal backslashes too, keeping two different paths distinct in Swift.
    return "".join(f"\\x{ord(char) - 0xdc00:02x}" if 0xdc80 <= ord(char) <= 0xdcff
                   else "\\\\" if char == "\\" else char for char in path)


def _unicode_path(path):
    try: path.encode("utf-8"); return True
    except UnicodeError: return False


def decode_branch(value):
    if not isinstance(value, str) or not re.fullmatch(r"(?:[0-9a-fA-F]{2}){1,4096}", value):
        raise ValueError("Choose a branch from this repository.")
    return os.fsdecode(bytes.fromhex(value))


def _root(workspace):
    path = Path(workspace).expanduser().resolve(strict=True)
    if not path.is_dir():
        raise ValueError("Choose an accessible workspace directory.")
    # Git appends exactly one newline; retain newlines belonging to the path.
    return _text(_complete(path, "rev-parse", "--show-toplevel").removesuffix(b"\n"))


def git_changes(workspace):
    root = _root(workspace)
    _, _, code = _git(root, "rev-parse", "--verify", "HEAD", allow_failure=True)
    base = ["HEAD"] if not code else []
    # --cached against an unborn HEAD represents the empty-tree baseline.
    raw_args = ["diff", "--raw", "-z", "--no-abbrev", "--no-ext-diff", "--no-textconv", "--find-renames"]
    if base:
        raw = _complete(root, *raw_args, *base, "--")
    else:
        raw = _complete(root, *raw_args, "--cached", "--")
    tokens = raw.split(b"\0")
    records, i = [], 0
    while i < len(tokens) and tokens[i]:
        status = tokens[i].split()[-1].decode("ascii")
        path = _text(tokens[i + 1]); i += 2
        row = {"path": path, "status": status[0]}
        if status[0] in {"R", "C"}:
            row["oldPath"] = path
            row["path"] = _text(tokens[i]); i += 1
        records.append(row)
    known = {row["path"] for row in records}
    untracked = _complete(root, "ls-files", "--others", "--exclude-standard", "-z").split(b"\0")
    records.extend({"path": _text(path), "status": "?"} for path in untracked if path and _text(path) not in known)
    files, used, truncated = [], 0, len(records) > MAX_FILES
    deadline = time.monotonic() + 30
    for row in records[:MAX_FILES]:
        if used >= MAX_TOTAL or time.monotonic() >= deadline:
            truncated = True
            break
        cap = min(MAX_DIFF, MAX_TOTAL - used)
        if row["status"] == "?" or not base:
            path = Path(root) / row["path"]
            if not path.exists() and not path.is_symlink():
                continue
            if path.is_symlink():
                content = os.fsencode(os.readlink(path)); clipped = len(content) > cap
                content = content[:cap]
            elif path.is_file():
                with path.open("rb") as stream:
                    content = stream.read(cap + 1)
                clipped = len(content) > cap; content = content[:cap]
            else:
                content, clipped = b"", False
            binary = b"\0" in content
            lines = content.decode("utf-8", "replace").splitlines(keepends=True)
            patch = "Binary file" if binary else "".join(difflib.unified_diff([], lines, fromfile="/dev/null", tofile=row["path"], n=3))
            row.update(added=0 if binary else len(lines), deleted=0, binary=binary)
            if clipped and path.is_file() and not path.is_symlink():
                counts, count_clipped, count_code = _git(root, "diff", "--no-index", "--no-ext-diff",
                    "--no-textconv", "--numstat", "-z", "--", "/dev/null", str(path), allow_failure=True)
                if count_clipped or count_code not in {0, 1}:
                    raise ValueError("Could not inspect the complete file counts within the Git limit.")
                fields = counts.split(b"\t", 2)
                if len(fields) == 3:
                    binary = fields[0] == b"-"
                    row.update(added=0 if binary else int(fields[0]), deleted=0, binary=binary)
        else:
            paths = [row["oldPath"], row["path"]] if "oldPath" in row else [row["path"]]
            args = ["diff", "--no-ext-diff", "--no-textconv", "--find-renames", "--no-color"]
            data, clipped, _ = _git(root, *args, "--unified=3", "HEAD", "--", *paths, limit=cap)
            patch = data.decode("utf-8", "replace")
            stats = _complete(root, *args, "--numstat", "-z", "HEAD", "--", *paths).split(b"\0")
            added = deleted = 0; binary = False
            index = 0
            while index < len(stats):
                stat = stats[index]; index += 1
                fields = stat.split(b"\t", 2)
                if len(fields) == 3:
                    if not fields[2]:
                        # A rename has two additional NUL-delimited filenames.
                        index += 2
                    if fields[0] == b"-": binary = True
                    elif fields[0].isdigit() and fields[1].isdigit():
                        added += int(fields[0]); deleted += int(fields[1])
            row.update(added=added, deleted=deleted, binary=binary)
        encoded = patch.encode("utf-8", "replace")
        clipped = clipped or len(encoded) > cap
        suffix = "\n[Diff truncated]" if clipped else ""
        row["diff"] = encoded[:max(0, cap - len(suffix))].decode("utf-8", "ignore") + suffix[:cap]
        row["path"] = _display_path(row["path"])
        if "oldPath" in row: row["oldPath"] = _display_path(row["oldPath"])
        used += len(row["diff"].encode("utf-8")); truncated |= clipped
        files.append(row)
    return {"files": files, "truncated": truncated}


def git_branches(workspace):
    root = _root(workspace)
    data, _, code = _git(root, "symbolic-ref", "--quiet", "--short", "HEAD", allow_failure=True)
    current = None if code else _text(data.removesuffix(b"\n"))
    worktrees, row = [], {}
    for field in _complete(root, "worktree", "list", "--porcelain", "-z").split(b"\0"):
        if not field:
            if row:
                row["current"] = row["path"] == root
                worktrees.append(row); row = {}
            continue
        key, _, value = field.partition(b" ")
        if key == b"worktree": row["path"] = _text(value)
        elif key == b"branch": row["branch"] = _text(value).removeprefix("refs/heads/")
        elif key in {b"locked", b"prunable"}: row[key.decode()] = True
    branches = []
    for ref in _complete(root, "for-each-ref", "--format=%(refname)", "refs/heads/").split(b"\n"):
        if not ref: continue
        name = _text(ref.removeprefix(b"refs/heads/"))
        branch = {"name": name if _unicode_path(name) else _display_path(name),
                  "nameBytes": os.fsencode(name).hex(), "current": name == current}
        tree = next((tree for tree in worktrees if tree.get("branch") == name), None)
        if tree: branch["worktree"] = tree["path"] if _unicode_path(tree["path"]) else _display_path(tree["path"])
        branches.append(branch)
    for tree in worktrees:
        tree["pathBytes"] = os.fsencode(tree["path"]).hex()
        tree["selectable"] = _unicode_path(tree["path"])
        if not tree["selectable"]: tree["path"] = _display_path(tree["path"])
        if tree.get("branch") and not _unicode_path(tree["branch"]): tree["branch"] = _display_path(tree["branch"])
    return {"root": root if _unicode_path(root) else _display_path(root),
            "current": current if current is None or _unicode_path(current) else _display_path(current),
            "branches": branches, "worktrees": worktrees}


def _claim_guard(root):
    """Inspect claim data only; never execute a workspace's guard script.

    Leases mirror the shared lifecycle: manual/contribution leases expire and
    cap at twenty minutes; durable runtime projections always require recovery.
    A clean status cannot establish that another session is about to be idle.
    """
    now = time.time()
    try:
        path = Path(root) / ".work-guard/heartbeat.json"
        with path.open("rb") as stream: sidecar = json.loads(stream.read(1_000_001))
        heartbeats = sidecar.get("markers", {}) if sidecar.get("schemaVersion") == 2 else sidecar
    except (OSError, ValueError, AttributeError): heartbeats = {}
    def timestamp(value):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed.replace(tzinfo=timezone.utc).timestamp() if parsed.tzinfo is None else parsed.timestamp()
        except (ValueError, AttributeError, OverflowError): return None
    for path in Path(root).iterdir():
        if not path.name.startswith((".WORK-IN-PROGRESS", "SHIP-HOLD", "SESSION-IN-PROGRESS")): continue
        if path.is_symlink() or not path.is_file():
            raise ValueError("Cannot verify work claim " + _display_path(path.name) + ". Resolve it before changing branches.")
        with path.open("rb") as stream: content = stream.read(65_537)
        if len(content) > 65_536: raise ValueError("Work claim is too large to inspect: " + _display_path(path.name))
        lines = content.decode("utf-8", "replace").splitlines()
        fields = {}
        if lines and lines[0].strip() == "---":
            for line in lines[1:]:
                if line.strip() == "---": break
                match = re.match(r"^(\w+):[ \t]*(.*)$", line)
                if match:
                    key, value = match.groups()
                    value = value.strip()
                    if value.startswith('"'):
                        try: value = str(json.loads(value))
                        except ValueError: value = value.strip('"')
                    else: value = value.strip("'")
                    fields[key] = value
        target = fields.get("worktree")
        if target and (Path(root) / target).resolve() != Path(root).resolve(): continue
        contribution = path.name.startswith(".WORK-IN-PROGRESS-taskwraith-contribution-") or fields.get("agent") == "taskwraith-contribution"
        runtime = not contribution and (path.name.startswith(".WORK-IN-PROGRESS-taskwraith-runtime-")
                    or fields.get("derived", "").lower() == "true" or fields.get("agent") == "taskwraith-runtime")
        expires, started = timestamp(fields.get("expires")), timestamp(fields.get("started"))
        expiry = min(expires, started + 1200) if expires is not None and started is not None else expires
        if started is None and expiry is not None and expiry > now + 1200: expiry = None
        alive = False
        pid = fields.get("pid", "")
        if pid.isdigit() and int(pid) > 1:
            try: os.kill(int(pid), 0); alive = True
            except ProcessLookupError: pass
            except PermissionError: alive = True
            except (OSError, OverflowError): pass
        heartbeat = heartbeats.get(path.name, {}) if isinstance(heartbeats, dict) else {}
        seen = heartbeat.get("lastSeen") if isinstance(heartbeat, dict) else None
        fresh = isinstance(seen, (int, float)) and 0 <= now - seen / 1000 < 1200
        held = bool(fields.get("lockOwnerId")) if contribution else alive or bool(fields.get("lockOwnerId")) or fresh
        if runtime or (expiry is not None and now <= expiry and held):
            raise ValueError("Active work claim " + _display_path(path.name) + ". Wait for its owner to finish before changing branches or creating a worktree.")


def _clean(workspace):
    root = _root(workspace)
    if _complete(root, "status", "--porcelain=v1", "-z", "--untracked-files=all", "--ignore-submodules=none"):
        raise ValueError("Workspace has uncommitted changes. Commit or resolve them before changing branches.")
    _claim_guard(root)
    return root


def _branch(root, branch, *, new):
    if not isinstance(branch, str) or not branch or branch.startswith("-"):
        raise ValueError("Enter a valid local branch name.")
    _complete(root, "check-ref-format", "refs/heads/" + branch)
    existing = {decode_branch(item["nameBytes"]) for item in git_branches(root)["branches"]}
    if (branch in existing) == new:
        raise ValueError("Choose a new branch name." if new else "Choose an existing local branch.")


def switch_branch(workspace, branch):
    root = _clean(workspace); _branch(root, branch, new=False)
    _clean(root)
    _complete(root, "switch", "--no-guess", "--", branch)
    return git_branches(root)


def create_branch(workspace, branch):
    root = _clean(workspace); _branch(root, branch, new=True)
    _clean(root)
    _complete(root, "switch", "--no-guess", "-c", branch)
    return git_branches(root)


def create_worktree(workspace, branch, path):
    root = _clean(workspace); _branch(root, branch, new=True)
    destination = Path(path).expanduser().absolute()
    if destination.exists() or destination.is_symlink():
        raise ValueError("Choose a destination that does not exist.")
    if not destination.parent.is_dir():
        raise ValueError("The destination's parent folder must already exist.")
    _clean(root)
    _complete(root, "worktree", "add", "-b", branch, "--", str(destination), "HEAD")
    return git_branches(root)


def switch_worktree(workspace, path):
    destination = Path(path).expanduser().resolve(strict=True)
    trees = git_branches(workspace)["worktrees"]
    if not any(tree.get("selectable", True) and Path(os.fsdecode(bytes.fromhex(tree["pathBytes"]))).resolve() == destination for tree in trees):
        raise ValueError("Choose a worktree belonging to this repository.")
    if not _unicode_path(str(destination)):
        raise ValueError("This worktree path cannot be represented by macOS. Relocate it with Git before selecting it.")
    if not destination.is_dir() or not os.access(destination, os.R_OK | os.X_OK):
        raise ValueError("This worktree is unavailable or inaccessible.")
    return str(destination)
