"""Small local Messages tools. The caller must approve patch/shell before execute.

File tools reject symlinks and anchor opens to directory descriptors. Shell is
ordinary local execution, NOT a sandbox; its exact command requires approval.
Background shells (chat_processes.py) keep running after the call returns.
"""
from __future__ import annotations

import fnmatch
import difflib
import math
import os
from pathlib import Path
import selectors
import signal
import stat
import subprocess
import threading
import time
import uuid

MAX_OUTPUT = 40_000
MAX_FILE = 2_000_000
MAX_SCAN = 5_000


def _tool(name, description, properties, required):
    return {"name": name, "description": description, "input_schema": {
        "type": "object", "properties": properties, "required": required,
        "additionalProperties": False}}


TOOL_DEFINITIONS = [
    _tool("read_file", "Read UTF-8 text inside the workspace. Offset is a 1-based line number; output is bounded.", {
        "path": {"type": "string"}, "offset": {"type": "integer", "minimum": 1},
        "limit": {"type": "integer", "minimum": 1, "maximum": 2000}}, ["path"]),
    _tool("search_files", "Search literal text in UTF-8 workspace files, excluding symlinks and .git. Bounded scan.", {
        "pattern": {"type": "string"}, "path": {"type": "string"},
        "glob": {"type": "string"}, "max_results": {"type": "integer", "minimum": 1, "maximum": 500}}, ["pattern"]),
    _tool("apply_patch", "Apply a Codex *** Begin Patch patch (Add/Update/Delete File, optional Move to). All hunks are validated first. The host applies the selected approval mode.", {
        "patch": {"type": "string"}}, ["patch"]),
    _tool("run_shell", "Run a command with /bin/sh in the workspace under the host's selected approval mode. This is not sandboxed. Output is bounded; timeout is in seconds. "
          "Set background to true for servers, watchers or long jobs: the command keeps running between turns, the call returns its first output and an id for read_process and stop_process, and timeout is not used. Stop background processes you no longer need.", {
        "command": {"type": "string"}, "timeout": {"type": "number", "minimum": 0.1, "maximum": 300},
        "background": {"type": "boolean"}}, ["command"]),
    _tool("read_process", "Read new output and the status of a background process this chat started. wait is seconds (at most 30) to wait for it to finish first; it returns early when the process exits.", {
        "id": {"type": "string"}, "wait": {"type": "number", "minimum": 0, "maximum": 30}}, ["id"]),
    _tool("stop_process", "Stop a background process this chat started: SIGTERM to its process group, then SIGKILL after 3 seconds.", {
        "id": {"type": "string"}}, ["id"]),
]
PROCESS_TOOLS = ("read_process", "stop_process")


def _bounded(text):
    if len(text) <= MAX_OUTPUT:
        return text
    suffix = "\n[output truncated]"
    return text[:MAX_OUTPUT - len(suffix)] + suffix


def _environment():
    # Retain operational PATH/HOME/locale, remove recognizable credentials.
    sensitive = ("KEY", "TOKEN", "SECRET", "PASSWORD", "PASSWD", "CREDENTIAL", "AUTH", "COOKIE")
    return {key: value for key, value in os.environ.items()
            if not any(word in key.upper() for word in sensitive)}


class ChatToolRunner:
    def __init__(self, workspace: str, cancel_event: threading.Event | None = None, processes=None):
        self.workspace = Path(workspace).resolve(strict=True)
        if not self.workspace.is_dir():
            raise ValueError("Workspace must be an existing directory")
        self.cancel_event = cancel_event if cancel_event is not None else threading.Event()
        # A chat_processes.ProcessRegistry; the Chat worker supplies one.
        self.processes = processes

    def _cancel(self):
        if self.cancel_event.is_set():
            raise ValueError("Tool execution cancelled")

    def _relative(self, value):
        if not isinstance(value, str) or not value or "\x00" in value:
            raise ValueError("Path must be a nonempty string")
        path = Path(value)
        if path.is_absolute():
            try:
                path = path.relative_to(self.workspace)
            except ValueError:
                raise ValueError("Path is outside workspace") from None
        if ".." in path.parts:
            raise ValueError("Parent traversal is not allowed")
        # Reject even internal symlinks, for predictable read/write semantics.
        current = self.workspace
        for part in path.parts:
            current /= part
            if current.is_symlink():
                raise ValueError("Symlink paths are not allowed")
        return path

    def _parent(self, path, create=False):
        """Return owned parent fd; O_NOFOLLOW protects against path swaps."""
        if not path.name:
            raise ValueError("Expected a file path")
        fd = os.open(self.workspace, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for part in path.parts[:-1]:
                if create:
                    try:
                        os.mkdir(part, dir_fd=fd)
                    except FileExistsError:
                        pass
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = child
            return fd
        except BaseException:
            os.close(fd)
            raise

    def _read(self, path):
        parent = self._parent(path)
        try:
            fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            with os.fdopen(fd, "rb") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode):
                    raise ValueError("Only regular files are supported")
                data = stream.read(MAX_FILE + 1)
                if len(data) > MAX_FILE:
                    raise ValueError(f"File exceeds {MAX_FILE} bytes")
                return data.decode("utf-8"), stat.S_IMODE(info.st_mode)
        finally:
            os.close(parent)

    def _validate(self, name, args):
        schemas = {tool["name"]: tool["input_schema"] for tool in TOOL_DEFINITIONS}
        if not isinstance(name, str) or name not in schemas:
            raise ValueError("Unknown tool")
        schema = schemas[name]
        if not isinstance(args, dict) or any(key not in schema["properties"] for key in args):
            raise ValueError("Invalid or unexpected tool arguments")
        if any(key not in args for key in schema["required"]):
            raise ValueError("Missing required argument")
        for key, value in args.items():
            spec = schema["properties"][key]
            kind = spec["type"]
            if kind == "string":
                if not isinstance(value, str) or not value or "\x00" in value:
                    raise ValueError(f"{key} must be a nonempty string without NUL")
                if len(value) > (MAX_FILE if key == "patch" else MAX_OUTPUT):
                    raise ValueError(f"{key} is too long")
            elif kind == "boolean":
                if type(value) is not bool:
                    raise ValueError(f"{key} must be true or false")
            else:
                if type(value) not in ((int,) if kind == "integer" else (int, float)) or (type(value) is float and not math.isfinite(value)):
                    raise ValueError(f"Invalid {key}")
                if value < spec["minimum"] or value > spec.get("maximum", 10**9):
                    raise ValueError(f"{key} is out of range")
        if name in ("read_file", "search_files"):
            self._relative(args.get("path", "."))
        if name == "apply_patch":
            return self._plan_patch(args["patch"])
        return None

    def describe(self, name, args):
        plan = self._validate(name, args)
        if name == "run_shell" and args.get("background"):
            summary = f"Run in background in {self.workspace}:\n{args['command']}"
        elif name == "run_shell":
            summary = f"Run in {self.workspace}:\n{args['command']}"
        elif name == "read_process":
            summary = f"Read background process {args['id']}" + (f" (wait {args['wait']:g}s)" if args.get("wait") else "")
        elif name == "stop_process":
            summary = f"Stop background process {args['id']}"
        elif name == "apply_patch":
            operations = []
            for line in args["patch"].splitlines():
                for kind in ("Add", "Update", "Delete"):
                    if line.startswith(f"*** {kind} File: "):
                        operations.append(f"{kind}: {line.split(': ', 1)[1]}")
                if line.startswith("*** Move to: "):
                    operations[-1] += f" → Move to: {line[13:]}"
            summary = "Apply patch:\n" + "\n".join(operations)
        elif name == "read_file":
            summary = f"Read {args['path']} (line {args.get('offset', 1)}, limit {args.get('limit', 500)})"
        else:
            summary = f"Search {args.get('path', '.')} for {args['pattern']!r} (glob {args.get('glob', '*')})"
        return {"name": name, "summary": summary, "requires_approval": name in ("apply_patch", "run_shell")}

    def execute(self, name, args):
        start = time.monotonic()
        changed = []
        summary = f"{name} failed" if isinstance(name, str) else "Tool failed"
        error = False
        try:
            self._cancel()
            summary = self.describe(name, args)["summary"]
            if name == "read_file":
                content, _ = self._read(self._relative(args["path"]))
                lines = content.splitlines(keepends=True)
                offset = args.get("offset", 1) - 1
                selected = lines[offset:offset + args.get("limit", 500)]
                output = "".join(f"{i}: {line}" + ("" if line.endswith("\n") else "\n")
                                 for i, line in enumerate(selected, offset + 1))
                output += f"\n[lines {offset + 1 if selected else 0}-{offset + len(selected) if selected else 0} of {len(lines)}]"
            elif name == "search_files":
                output = self._search(args)
            elif name == "apply_patch":
                plan = self._plan_patch(args["patch"])
                self._cancel()
                self._apply(plan, changed)
                output = "Applied patch:\n" + "\n".join(changed) + "\n\n" + self._patch_diff(plan)
            elif name in PROCESS_TOOLS or args.get("background"):
                output, error = self._background(name, args)
            else:
                output, error = self._shell(args)
        except (ValueError, OSError, UnicodeError) as exc:
            error = True
            output = f"{type(exc).__name__}: {exc}"
            if changed:
                output += "\nPartial filesystem failure; changed files:\n" + "\n".join(changed)
        return {"content": [{"type": "text", "text": _bounded(output)}], "is_error": error,
                "summary": summary, "changed_files": changed,
                "duration_ms": int((time.monotonic() - start) * 1000)}

    def _background(self, name, args):
        owner = getattr(self.cancel_event, "owner", None)
        if self.processes is None or owner is None:
            raise ValueError("Background processes are not available in this conversation; run the command in the foreground.")
        if name == "run_shell":
            return self.processes.start(owner, str(self.workspace), args["command"], _environment(), self.cancel_event)
        if name == "read_process":
            return self.processes.read(owner, args["id"], args.get("wait", 0), self.cancel_event)
        return self.processes.stop(owner, args["id"], self.cancel_event)

    def _search(self, args):
        root = self._relative(args.get("path", "."))
        (self.workspace / root).stat()  # Missing search roots are errors.
        results, seen, skipped = [], 0, 0
        max_results = args.get("max_results", 100)
        # Iterative traversal, capped by visited entries as well as output size.
        pending = [root]
        while pending and seen < MAX_SCAN:
            self._cancel()
            path = pending.pop()
            seen += 1
            absolute = self.workspace / path
            if absolute.is_symlink():
                continue
            if absolute.is_dir():
                # Do not recurse via a concurrently swapped symlink.
                parent = self._parent(path / "placeholder")
                try:
                    with os.scandir(parent) as entries:
                        for entry in entries:
                            if entry.name == ".git" or entry.is_symlink():
                                continue
                            if len(pending) + seen >= MAX_SCAN:
                                skipped += 1
                                break
                            pending.append(path / entry.name)
                finally:
                    os.close(parent)
                continue
            if not fnmatch.fnmatch(str(path), args.get("glob", "*")):
                continue
            try:
                content, _ = self._read(path)
            except (OSError, ValueError, UnicodeError):
                skipped += 1
                continue
            for number, line in enumerate(content.splitlines(), 1):
                if args["pattern"] in line:
                    results.append(f"{path}:{number}: {line}")
                    if len(results) >= max_results or sum(map(len, results)) >= MAX_OUTPUT:
                        return "\n".join(results) + "\n[search truncated]"
        return "\n".join(results) + f"\n[{len(results)} matches; {skipped} entries skipped" + ("; scan truncated" if pending else "") + "]"

    def _plan_patch(self, patch):
        lines = patch.splitlines()
        if len(lines) < 3 or lines[0] != "*** Begin Patch" or lines[-1] != "*** End Patch":
            raise ValueError("Expected *** Begin Patch / *** End Patch")
        plans = {}
        i = 1
        while i < len(lines) - 1:
            header = lines[i]
            kinds = [kind for kind in ("Add", "Update", "Delete") if header.startswith(f"*** {kind} File: ")]
            if not kinds:
                raise ValueError(f"Invalid patch header: {header}")
            kind = kinds[0]
            path = self._relative(header.split(": ", 1)[1])
            if path == Path(".") or path in plans:
                raise ValueError("Duplicate or invalid patch path")
            i += 1
            target = path
            if kind == "Update" and i < len(lines) - 1 and lines[i].startswith("*** Move to: "):
                target = self._relative(lines[i][13:])
                i += 1
                if target == path or target == Path(".") or target in plans:
                    raise ValueError("Invalid move destination")
            if kind == "Add":
                if os.path.lexists(self.workspace / path):
                    raise ValueError(f"Add destination already exists: {path}")
                added = []
                while i < len(lines) - 1 and lines[i].startswith("+"):
                    added.append(lines[i][1:])
                    i += 1
                plans[path] = (None, "\n".join(added) + ("\n" if added else ""), 0o644)
                continue
            original, mode = self._read(path)
            if kind == "Delete":
                plans[path] = (original, None, mode)
                continue
            source = original.splitlines()
            cursor, pieces, hunks = 0, [], 0
            while i < len(lines) - 1 and lines[i].startswith("@@"):
                header = lines[i]
                if header != "@@" and not header.startswith("@@ "):
                    raise ValueError("Invalid hunk header")
                anchor = header[3:] if header.startswith("@@ ") else ""
                i += 1
                old, new, eof = [], [], False
                while i < len(lines) - 1 and not lines[i].startswith(("@@", "*** ")):
                    line = lines[i]
                    if not line or line[0] not in " +-":
                        raise ValueError("Every hunk line needs a context/add/delete prefix")
                    if line[0] in " -":
                        old.append(line[1:])
                    if line[0] in " +":
                        new.append(line[1:])
                    i += 1
                if i < len(lines) - 1 and lines[i] == "*** End of File":
                    eof = True
                    i += 1
                if not old and not new:
                    raise ValueError("Empty hunk")
                search_start = cursor
                if anchor:
                    anchors = [n for n in range(cursor, len(source)) if source[n] == anchor]
                    if len(anchors) != 1:
                        raise ValueError("Missing or ambiguous hunk anchor")
                    search_start = anchors[0] + 1
                if old:
                    matches = [n for n in range(search_start, len(source) - len(old) + 1)
                               if source[n:n + len(old)] == old and (not eof or n + len(old) == len(source))]
                else:
                    # Context-free insertion is only unambiguous at EOF.
                    matches = [len(source)] if eof or not source else []
                if len(matches) != 1:
                    raise ValueError(f"Missing or ambiguous hunk in {path}")
                position = matches[0]
                pieces.extend(source[cursor:position])
                pieces.extend(new)
                cursor = position + len(old)
                hunks += 1
            if not hunks:
                raise ValueError("Update requires at least one hunk")
            pieces.extend(source[cursor:])
            newline = "\r\n" if "\r\n" in original else "\n"
            updated = newline.join(pieces) + (newline if pieces and original.endswith("\n") else "")
            if target != path:
                if os.path.lexists(self.workspace / target):
                    raise ValueError("Move destination already exists")
                plans[target] = (None, updated, mode)
                plans[path] = (original, None, mode)
            else:
                plans[path] = (original, updated, mode)
        if not plans:
            raise ValueError("Patch has no file operations")
        # File/ancestor conflicts would otherwise fail after earlier mutations.
        for path in plans:
            if any(parent in plans for parent in path.parents):
                raise ValueError("Conflicting file and directory patch paths")
            for parent in path.parents:
                full = self.workspace / parent
                if full.exists() and not full.is_dir():
                    raise ValueError("Patch parent is not a directory")
        return plans

    @staticmethod
    def _patch_diff(plans):
        chunks, length = [], 0
        for path, (before, after, _) in plans.items():
            for line in difflib.unified_diff(
                    (before or "").splitlines(keepends=True),
                    (after or "").splitlines(keepends=True),
                    fromfile=f"a/{path}" if before is not None else "/dev/null",
                    tofile=f"b/{path}" if after is not None else "/dev/null"):
                # Keep unterminated source lines distinct in the displayed diff.
                if not line.endswith("\n"):
                    line += "\n\\ No newline at end of file\n"
                remaining = MAX_OUTPUT - length
                chunks.append(line[:remaining])
                length += len(line)
                if length >= MAX_OUTPUT:
                    return _bounded("".join(chunks) + "\n[diff truncated]")
        return "".join(chunks) or "[No textual content changes]"

    def _apply(self, plans, changed):
        # Validate every file again immediately before the first mutation.
        for path, (before, _, _) in plans.items():
            self._relative(str(path))
            if before is None:
                if os.path.lexists(self.workspace / path):
                    raise ValueError("Patch destination changed during validation")
            elif self._read(path)[0] != before:
                raise ValueError("File changed during patch validation")
        for path, (_, after, mode) in plans.items():
            parent = self._parent(path, create=after is not None)
            temporary = ".chat-patch-" + uuid.uuid4().hex
            try:
                if after is None:
                    os.unlink(path.name, dir_fd=parent)
                else:
                    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode, dir_fd=parent)
                    with os.fdopen(fd, "wb") as stream:
                        stream.write(after.encode("utf-8"))
                        os.fchmod(stream.fileno(), mode)
                    os.replace(temporary, path.name, src_dir_fd=parent, dst_dir_fd=parent)
                changed.append(str(path))
            finally:
                try:
                    os.unlink(temporary, dir_fd=parent)
                except FileNotFoundError:
                    pass
                os.close(parent)

    def _shell(self, args):
        env = _environment()
        timeout = args.get("timeout", 60)
        self._cancel()
        process = subprocess.Popen(["/bin/sh", "-c", args["command"]], cwd=self.workspace,
                                   env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, start_new_session=True)
        tail = bytearray()
        total = 0
        reason = None
        group_killed = False
        deadline = time.monotonic() + timeout
        selector = selectors.DefaultSelector()
        try:
            os.set_blocking(process.stdout.fileno(), False)
            selector.register(process.stdout, selectors.EVENT_READ)
            while selector.get_map() or process.poll() is None:
                if reason is None and (self.cancel_event.is_set() or time.monotonic() >= deadline):
                    reason = "cancelled" if self.cancel_event.is_set() else "timed out"
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    group_killed = True
                for key, _ in selector.select(0.05):
                    data = os.read(key.fd, 65536)
                    if not data:
                        selector.unregister(key.fileobj)
                    else:
                        total += len(data)
                        tail.extend(data)
                        del tail[:-MAX_OUTPUT * 4]
                # Escaped descendants may retain the pipe; never wait forever.
                if reason and time.monotonic() > deadline + 1:
                    break
                if reason == "cancelled":
                    deadline = min(deadline, time.monotonic())
            code = process.wait()
        finally:
            try:
                try:
                    if not group_killed:
                        os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                except PermissionError:
                    # Still reap the direct child if group signalling is denied.
                    if process.poll() is None:
                        process.kill()
                    raise
            finally:
                process.wait()
                selector.close()
                process.stdout.close()
        prefix = f"Exit code: {code}" + (f" ({reason})" if reason else "") + "\n"
        decoded = tail.decode("utf-8", errors="replace")
        if total > len(tail) or len(decoded) + len(prefix) > MAX_OUTPUT:
            prefix += "[earlier output truncated]\n"
        return prefix + decoded[-(MAX_OUTPUT - len(prefix)):], reason is not None or code != 0
