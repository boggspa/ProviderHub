"""Background shell processes owned by a chat, its Team members and helpers.

``run_shell`` with ``background: true`` starts a command in its own process
group and returns after a short first look at its output. The worker keeps a
bounded output tail and the exit status in memory, publishes a small snapshot
to the inspector's Background Processes tab, and ends every group when its chat
is deleted or Chat closes. Nothing is written to disk: a process must not
outlive the worker that can stop it. If the worker itself is killed, the
operating system does not end these groups on its behalf.

One reader thread per process; at most MAX_RUNNING processes run at once.
"""
from __future__ import annotations

import os
import selectors
import signal
import subprocess
import threading
import time
import weakref

MAX_RUNNING = 8
MAX_RUNNING_PER_CHAT = 4
MAX_FINISHED_PER_CHAT = 10
TAIL_BYTES = 64_000
SNAPSHOT_CHARS = 4_000
REPORT_CHARS = 20_000
FIRST_LOOK = 1.0
STOP_GRACE = 3.0
DRAIN = 1.0
# Exit detection that leaves the group leader unreaped. A zombie keeps its
# pid, so its process-group id cannot be reused while we still signal it.
NONREAPING = hasattr(os, "waitid") and hasattr(os, "WNOWAIT")


class OwnerEvent(threading.Event):
    """A conversation's cancel event, which also names that conversation.

    Every tool runner already receives its conversation's cancel event, so it
    is how a runner learns who owns the processes it starts, without another
    argument through every runner factory.
    """
    def __init__(self, owner):
        super().__init__()
        self._owner = weakref.ref(owner)

    @property
    def owner(self):
        return self._owner()


def scope(service):
    """Return the owning chat id and the display owner for a conversation."""
    root = service
    for _ in range(8):
        parent = getattr(root, "team_parent", None) or getattr(root, "helper_parent", None)
        if parent is None:
            break
        root = parent
    chat, member = service.chat, getattr(service, "team_member", None)
    label = next((model.get("label") for model in service.models or []
                  if model.get("route") == chat.get("route") and model.get("account") == chat.get("account")), None)
    label = label or chat.get("label") or chat.get("route", "")
    if member:
        name = member["name"]
    elif service.role == "delegate":
        name = "Helper · " + label
    else:
        name = label
    return root.chat["id"], {"owner": name, "route": chat.get("route", ""), "account": chat.get("account", ""),
                             "memberID": member["id"] if member else None}


def _complete_utf8(data):
    """Length of data without a trailing, unfinished UTF-8 sequence."""
    for back in range(1, min(4, len(data)) + 1):
        byte = data[-back]
        if byte & 0xC0 != 0x80:
            needed = 2 if byte & 0xE0 == 0xC0 else 3 if byte & 0xF0 == 0xE0 else 4 if byte & 0xF8 == 0xF0 else 1
            return len(data) - back if needed > back else len(data)
    return len(data)


def _leader_exited(popen):
    """Return (exited, reaped) for a group leader without reaping it.

    Only waitid with WNOWAIT can see an exit and leave the zombie in place, so
    start() refuses background launches without it: a poll() fallback reaps
    the leader, releases its group id, and leaves the rest of the group
    unreachable. ChildProcessError means something else reaped it.
    """
    try:
        return os.waitid(os.P_PID, popen.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None, False
    except ChildProcessError:
        return True, True


class BackgroundProcess:
    def __init__(self, chat, number, owner, workspace, command, popen):
        self.chat, self.id, self.owner = chat, f"p{number}", owner
        self.workspace, self.command, self.popen, self.pid = workspace, command, popen, popen.pid
        self.started, self.ended = time.time(), None
        self.status, self.code, self.stopped_by = "running", None, None
        self.tail, self.total, self.seen = bytearray(), 0, 0
        self.kill_at, self.forgotten, self.thread, self.reaped = None, False, None, False

    @property
    def alive(self):
        return self.status in {"running", "stopping"}

    def signal(self, number):
        # start_new_session made the shell a group leader: pgid == pid. Callers
        # hold the registry lock, and a reaped leader's id may belong to
        # another process group by now, so it is never signalled.
        if self.reaped:
            return
        try:
            os.killpg(self.pid, number)
        except (ProcessLookupError, PermissionError):
            pass

    def public(self, output):
        text, truncated = "", False
        if output:
            decoded = bytes(self.tail).decode("utf-8", errors="replace")
            text = decoded[-SNAPSHOT_CHARS:]
            truncated = self.total > len(self.tail) or len(text) < len(decoded)
        return {"id": self.id, "pid": self.pid, "command": self.command, "workspace": self.workspace,
                **self.owner, "status": self.status, "code": self.code, "started": self.started,
                "ended": self.ended, "output": text, "truncated": truncated, "stoppedBy": self.stopped_by}


class ProcessRegistry:
    """Live and recently finished background processes, grouped by chat."""
    def __init__(self, on_change=lambda chat: None):
        self._condition = threading.Condition(threading.RLock())
        self._chats = {}
        self.on_change = on_change
        self.closing = False

    def _record(self, chat):
        return self._chats.setdefault(chat, {"next": 0, "processes": []})

    def _find(self, chat, identifier):
        processes = self._chats.get(chat, {}).get("processes", [])
        found = next((process for process in processes if process.id == identifier), None)
        if found is None:
            known = ", ".join(f"{process.id} ({process.status})" for process in processes) or "none"
            raise ValueError(f"No background process {identifier!r} in this chat. Known: {known}.")
        return found

    def _changed(self, process):
        if not process.forgotten:
            self.on_change(process.chat)

    def start(self, service, workspace, command, env, cancel):
        if not NONREAPING:
            raise ValueError("Background processes need a Python runtime with os.waitid (3.13 or later on macOS); "
                             "run the command in the foreground.")
        chat, owner = scope(service)
        with self._condition:
            if self.closing:
                raise ValueError("Chat is closing.")
            running = [process for record in self._chats.values() for process in record["processes"] if process.alive]
            mine = [process for process in running if process.chat == chat]
            if len(mine) >= MAX_RUNNING_PER_CHAT:
                raise ValueError(f"This chat already has {len(mine)} background processes running "
                                 f"({', '.join(process.id for process in mine)}). Stop one with stop_process first.")
            if len(running) >= MAX_RUNNING:
                raise ValueError(f"{MAX_RUNNING} background processes are already running in Chat. Stop one first.")
            popen = subprocess.Popen(["/bin/sh", "-c", command], cwd=workspace, env=env, stdin=subprocess.DEVNULL,
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
            record = self._record(chat)
            record["next"] += 1
            process = BackgroundProcess(chat, record["next"], owner, workspace, command, popen)
            record["processes"].append(process)
            process.thread = threading.Thread(target=self._pump, args=(process,), daemon=True, name="chat-process-" + process.id)
            process.thread.start()
        self._changed(process)
        self._wait(process, FIRST_LOOK, cancel)
        return self._report(process, "Started background process")

    def read(self, service, identifier, wait, cancel):
        with self._condition:
            process = self._find(scope(service)[0], identifier)
        self._wait(process, wait, cancel)
        return self._report(process, "Background process")

    def stop(self, service, identifier, cancel):
        with self._condition:
            process = self._find(scope(service)[0], identifier)
            stopping = process.alive
        if stopping:
            self._request_stop(process, "agent")
            self._wait(process, STOP_GRACE + DRAIN, cancel)
        return self._report(process, "Background process")

    def stop_by_user(self, chat, identifier):
        with self._condition:
            if not isinstance(identifier, str):
                raise ValueError("Choose a background process to stop.")
            process = self._find(chat, identifier)
        if not process.alive:
            return "That process has already finished."
        self._request_stop(process, "user")
        return None

    def clear(self, chat):
        with self._condition:
            record = self._chats.get(chat)
            if record:
                record["processes"] = [process for process in record["processes"] if process.alive]

    def forget(self, chat):
        """End a deleted chat's processes at once and drop their records."""
        with self._condition:
            record = self._chats.pop(chat, None)
            for process in record["processes"] if record else []:
                process.forgotten = True
                if process.alive:
                    process.status, process.stopped_by = "stopping", "chat"
                    process.signal(signal.SIGKILL)

    def shutdown(self, timeout=2.0):
        """SIGTERM every group, allow half the timeout, then SIGKILL the rest."""
        with self._condition:
            self.closing = True
            processes = [process for record in self._chats.values() for process in record["processes"]]
            for process in processes:
                if process.alive:
                    process.status, process.stopped_by = "stopping", process.stopped_by or "chat"
                    process.signal(signal.SIGTERM)
        start = time.monotonic()
        deadline = start + timeout
        with self._condition:
            while any(process.alive for process in processes) and time.monotonic() < start + timeout / 2:
                self._condition.wait(0.05)
            for process in processes:
                if process.alive:
                    process.signal(signal.SIGKILL)
        for process in processes:
            if process.thread and process.thread is not threading.current_thread():
                process.thread.join(max(0, deadline - time.monotonic()))

    def snapshot(self, chat, *, output=True):
        with self._condition:
            processes = list(self._chats.get(chat, {}).get("processes", []))
            running = sorted((process for process in processes if process.alive), key=lambda process: process.started)
            finished = sorted((process for process in processes if not process.alive), key=lambda process: -process.ended)
            return [process.public(output) for process in running + finished]

    def _request_stop(self, process, by):
        with self._condition:
            if not process.alive or process.status == "stopping":
                return
            process.status, process.stopped_by = "stopping", by
            process.kill_at = time.monotonic() + STOP_GRACE
            process.signal(signal.SIGTERM)
        self._changed(process)

    def _wait(self, process, seconds, cancel):
        deadline = time.monotonic() + max(0, seconds)
        with self._condition:
            while process.alive and not (cancel and cancel.is_set()):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._condition.wait(min(0.1, remaining))

    def _report(self, process, prefix):
        with self._condition:
            # The model sees each byte once; complete UTF-8 only while running.
            end = process.total if not process.alive else process.total - len(process.tail) + _complete_utf8(process.tail)
            first = process.total - len(process.tail)
            lost = max(0, first - process.seen)
            data = bytes(process.tail[max(process.seen, first) - first:end - first])
            process.seen = max(process.seen, end)
            state = {"running": "is running", "stopping": "is stopping"}.get(process.status)
            if state is None:
                verb = "was stopped" if process.status == "stopped" else "exited"
                signalled = process.code is not None and process.code < 0
                state = f"{verb} (signal {-process.code})" if signalled else f"{verb} (exit code {process.code})"
            alive, failed = process.alive, process.status == "exited" and process.code != 0
            lines = [f"{prefix} {process.id} (pid {process.pid}) {state}.", f"Command: {process.command}"]
        text = data.decode("utf-8", errors="replace")
        if lost:
            lines.append(f"[{lost} earlier bytes of output were not kept]")
        if len(text) > REPORT_CHARS:
            lines.append("[earlier new output truncated]")
            text = text[-REPORT_CHARS:]
        lines.append("New output:\n" + text if text else "No new output.")
        if alive:
            lines.append(f"It keeps running between turns. Use read_process with id {process.id!r} to check it "
                         "and stop_process when it is no longer needed.")
        return "\n".join(lines), failed and prefix.startswith("Started")

    def _pump(self, process):
        stream = process.popen.stdout
        fd = stream.fileno()
        os.set_blocking(fd, False)
        selector = selectors.DefaultSelector()
        selector.register(fd, selectors.EVENT_READ)
        eof, drain_until = False, None
        try:
            while True:
                if eof:
                    time.sleep(0.1)
                else:
                    for _key, _ in selector.select(0.1):
                        try:
                            data = os.read(fd, 65536)
                        except BlockingIOError:
                            continue
                        if not data:
                            eof = True
                            selector.unregister(fd)
                            break
                        with self._condition:
                            process.total += len(data)
                            process.tail.extend(data)
                            del process.tail[:-TAIL_BYTES]
                with self._condition:
                    if process.kill_at is not None and time.monotonic() >= process.kill_at:
                        process.kill_at = None
                        process.signal(signal.SIGKILL)
                    # Detect the exit under the same lock every signal holds,
                    # so Stop, delete and shutdown see one consistent state.
                    if drain_until is None:
                        exited, reaped = _leader_exited(process.popen)
                        if exited:
                            process.reaped = reaped
                            # Anything the command left in its group ends with
                            # it, as for foreground shells. Escaped readers get
                            # a moment.
                            process.signal(signal.SIGKILL)
                            drain_until = time.monotonic() + DRAIN
                if drain_until is not None and (eof or time.monotonic() >= drain_until):
                    break
        finally:
            selector.close()
            stream.close()
            with self._condition:
                if drain_until is None:
                    # The reader failed before the leader exited.
                    process.signal(signal.SIGKILL)
                # Reaping releases the group id: no signal may follow it.
                process.reaped = True
            code = process.popen.wait()
            with self._condition:
                process.code, process.ended, process.kill_at = code, time.time(), None
                process.status = "stopped" if process.stopped_by else "exited"
                record = None if process.forgotten else self._chats.get(process.chat)
                if record:
                    finished = sorted((item for item in record["processes"] if not item.alive), key=lambda item: item.ended)
                    for old in finished[:max(0, len(finished) - MAX_FINISHED_PER_CHAT)]:
                        record["processes"].remove(old)
                self._condition.notify_all()
            self._changed(process)
