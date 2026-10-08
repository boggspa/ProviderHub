"""Small local Chat host: Messages streaming, four tools, private JSONL chats.

The gateway continues to own all provider adaptation and credentials. This
worker only connects to its authenticated loopback endpoint. No nested engine,
orchestration service, or additional third-party runtime is involved.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone
import fcntl
import http.client
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import threading
import uuid

from bridge_core import gateway_token, load_settings, private_directory, state_root
from chat_tools import ChatToolRunner, TOOL_DEFINITIONS
from chat_attachments import prepare_attachments, bound_image_history
from chat_history import portable_history
from chat_git import git_status
from chat_workspaces import ChatWorkspaces, available_workspace
from protocol import compact_conversation, estimated_tokens

MAX_ROUNDS = 24
MAX_TEXT = 100_000
CHAT_ID = re.compile(r"[0-9a-f]{32}\Z")
APPROVAL_MODES = {"manual", "accept_edits", "yolo"}
SYSTEM = """You are an assistant in Provider Hub Chat, a small local coding harness.
Use the offered tools to inspect and change the chosen workspace. Read relevant
files and AGENTS.md instructions before editing. File and search tools are
restricted to the workspace. Shell commands run with the user's normal OS
permissions; the working directory is not a sandbox. The host enforces the
selected approval mode described below. A denial is a real result:
do not bypass it through a different tool. Wait for actual tool results before
claiming that something ran or changed. Keep replies clear and concise.
The visible transcript is saved locally. Older model context may be trimmed
with an explicit notice; do not pretend to remember text you cannot see.
"""


def now():
    return datetime.now(timezone.utc).isoformat()


def entry(kind, text="", route="", **extra):
    return {"id": uuid.uuid4().hex, "kind": kind, "text": text, "route": route,
            "isError": False, "changedFiles": [], **extra}


def needs_approval(mode, name, arguments, workspace):
    """One small permission decision, independent of anything the model says.

Accept Edits only preauthorizes validated patches within the selected Git
worktree. Shell always asks in that mode, including shell commands that appear
to edit files: trying to infer a command's effects would be a policy engine.
"""
    if mode == "yolo":
        return False
    if mode != "accept_edits" or name != "apply_patch":
        return True
    folder = Path(workspace).resolve()
    try:
        result = subprocess.run(["git", "-C", str(folder), "rev-parse", "--show-toplevel"],
                                capture_output=True, text=True, timeout=3)
        if result.returncode:
            return True
        root = Path(result.stdout.strip()).resolve(strict=True)
        folder.relative_to(root)
        paths = []
        for line in arguments["patch"].splitlines():
            for prefix in ("*** Add File: ", "*** Update File: ", "*** Delete File: ", "*** Move to: "):
                if line.startswith(prefix):
                    target = (folder / line[len(prefix):]).resolve()
                    relative = target.relative_to(root)
                    target.relative_to(folder)
                    if ".git" in relative.parts:
                        return True
                    paths.append(target)
        return not paths
    except (OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired):
        return True


class ChatStore:
    """Atomic JSONL snapshots. Persist before an action, and after its result.

The file contains metadata, display entries, and provider history separately;
opaque thinking/signatures are kept intact and are never rendered as text.
"""
    def __init__(self, root, *, lock=True):
        self.root = Path(root) / "chats"
        if self.root.is_symlink():
            raise ValueError("Chat storage must not be a symbolic link.")
        private_directory(self.root)
        self._lock = None
        if lock:
            path = self.root / ".lock"
            fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            self._lock = os.fdopen(fd, "a+")
            try:
                fcntl.flock(self._lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                self._lock.close()
                raise ValueError("Chat is already open in another Provider Hub instance.")

    def path(self, identifier):
        if not isinstance(identifier, str) or not CHAT_ID.fullmatch(identifier):
            raise ValueError("Choose a saved chat.")
        path = self.root / (identifier + ".jsonl")
        if path.is_symlink():
            raise ValueError("A saved chat must not be a symbolic link.")
        return path

    def save(self, chat):
        target = self.path(chat["id"])
        metadata = {key: value for key, value in chat.items() if key not in {"entries", "messages", "archives", "pending_update", "agents"}}
        rows = [{"type": "metadata", "value": metadata}]
        rows += [{"type": "entry", "value": value} for value in chat["entries"]]
        rows += [{"type": "message", "value": value} for value in chat["messages"]]
        rows += [{"type": "archive", "value": value} for value in chat.get("archives", [])]
        rows += [{"type": "agent", "value": value} for value in chat.get("agents", [])]
        if chat.get("pending_update"):
            rows.append({"type": "pending_update", "value": chat["pending_update"]})
        temp = self.root / ("." + uuid.uuid4().hex + ".tmp")
        try:
            fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                for row in rows:
                    stream.write(json.dumps(row, ensure_ascii=False) + "\n")
                stream.flush(); os.fsync(stream.fileno())
            os.replace(temp, target)
        finally:
            temp.unlink(missing_ok=True)

    def load(self, identifier):
        with self.path(identifier).open(encoding="utf-8") as stream:
            rows = [json.loads(line) for line in stream if line.strip()]
        if not rows or rows[0].get("type") != "metadata":
            raise ValueError("This saved chat is incomplete.")
        chat = rows[0]["value"]
        if chat.get("approvalMode") not in APPROVAL_MODES:
            chat["approvalMode"] = "manual"
        if chat.get("id") != identifier:
            raise ValueError("The saved chat's identity does not match its filename.")
        chat["entries"] = [row["value"] for row in rows[1:] if row.get("type") == "entry"]
        chat["messages"] = [row["value"] for row in rows[1:] if row.get("type") == "message"]
        chat["archives"] = [row["value"] for row in rows[1:] if row.get("type") == "archive"]
        chat["agents"] = [row["value"] for row in rows[1:] if row.get("type") == "agent"] or chat.get("agents", [])
        pending = [row["value"] for row in rows[1:] if row.get("type") == "pending_update"]
        if pending: chat["pending_update"] = pending[-1]
        return chat

    def all(self):
        chats = []
        for path in self.root.glob("*.jsonl"):
            try:
                chats.append(self.load(path.stem))
            except (ValueError, OSError, KeyError):
                # Keep damaged source files for recovery; never overwrite them.
                continue
        return sorted(chats, key=lambda value: value["updated"], reverse=True)

    def headers(self):
        """The rail needs one small metadata line, not every saved image/trace."""
        rows = []
        for path in self.root.glob("*.jsonl"):
            try:
                with self.path(path.stem).open(encoding="utf-8") as stream:
                    first = json.loads(stream.readline())
                row = first["value"]
                if first["type"] != "metadata" or row["id"] != path.stem:
                    continue
                row.setdefault("approvalMode", "manual")
                rows.append(row)
            except (ValueError, OSError, KeyError, TypeError):
                continue
        return sorted(rows, key=lambda row: row["updated"], reverse=True)

    def delete(self, identifier):
        self.path(identifier).unlink()
        attachment_root = self.root / identifier
        if attachment_root.is_dir() and not attachment_root.is_symlink():
            shutil.rmtree(attachment_root)


class GatewayClient:
    def __init__(self, root):
        self.root = Path(root)
        self._socket = None
        self._mutex = threading.Lock()

    def connect(self):
        connection = http.client.HTTPConnection("127.0.0.1", load_settings(self.root)["port"], timeout=120)
        connection.connect()
        with self._mutex:
            self._socket = connection.sock
        return connection

    def cancel(self):
        with self._mutex:
            active = self._socket
        if active is not None:
            try:
                active.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def headers(self):
        return {"Authorization": "Bearer " + gateway_token(self.root), "Content-Type": "application/json"}

    def catalogue(self):
        connection = self.connect()
        try:
            connection.request("GET", "/_bridge/chat/models", headers=self.headers())
            response = connection.getresponse()
            if response.status != 200:
                raise ValueError("The gateway could not load Chat's models. Reopen Chat after checking Providers.")
            return json.loads(response.read())["models"]
        finally:
            connection.close()

    def stream(self, payload, cancel, delta):
        connection = self.connect()
        blocks, fragments, usage = {}, {}, {}
        open_blocks = set()
        stopped = False
        reason = None
        try:
            if cancel.is_set():
                raise InterruptedError("Stopped")
            connection.request("POST", "/v1/messages", json.dumps({**payload, "stream": True}).encode(), self.headers())
            response = connection.getresponse()
            if response.status != 200:
                body = response.read(64_000)
                try:
                    error = json.loads(body).get("error", {})
                    message = error.get("message") if isinstance(error, dict) else error
                except (ValueError, AttributeError):
                    message = None
                raise ValueError(message or f"Gateway request failed ({response.status}).")
            if "text/event-stream" not in response.getheader("Content-Type", ""):
                raise ValueError("The gateway did not return a Messages stream.")
            for event in self.events(response):
                if cancel.is_set():
                    raise InterruptedError("Stopped")
                kind = event.get("type")
                if kind == "error":
                    raise ValueError((event.get("error") or {}).get("message", "Model request failed."))
                if kind == "message_start":
                    usage.update((event.get("message") or {}).get("usage") or {})
                elif kind == "content_block_start":
                    index = event["index"]
                    if index in blocks:
                        raise ValueError("The model stream repeated a content block.")
                    blocks[index] = copy.deepcopy(event["content_block"])
                    fragments[index] = ""
                    open_blocks.add(index)
                    if blocks[index].get("type") == "text" and blocks[index].get("text"):
                        delta(blocks[index]["text"])
                elif kind == "content_block_delta":
                    block = blocks.get(event.get("index"))
                    if block is None or event.get("index") not in open_blocks:
                        raise ValueError("The model stream supplied an unknown content block.")
                    change = event.get("delta") or {}
                    dtype = change.get("type")
                    if dtype == "text_delta":
                        value = change.get("text", "")
                        block["text"] = block.get("text", "") + value; delta(value)
                    elif dtype == "thinking_delta":
                        block["thinking"] = block.get("thinking", "") + change.get("thinking", "")
                    elif dtype == "signature_delta":
                        block["signature"] = block.get("signature", "") + change.get("signature", "")
                    elif dtype == "input_json_delta":
                        fragments[event["index"]] += change.get("partial_json", "")
                    else:
                        raise ValueError("The model stream used an unsupported content delta.")
                elif kind == "content_block_stop":
                    index = event["index"]
                    if index not in open_blocks:
                        raise ValueError("The model stream ended an unknown content block.")
                    if fragments.get(index):
                        value = json.loads(fragments[index])
                        if not isinstance(value, dict):
                            raise ValueError("The model returned invalid tool arguments.")
                        blocks[index]["input"] = value
                    open_blocks.remove(index)
                elif kind == "message_delta":
                    usage.update(event.get("usage") or {}); reason = (event.get("delta") or {}).get("stop_reason")
                elif kind == "message_stop":
                    if open_blocks:
                        raise ValueError("The model stream ended with incomplete content. No pending tool was executed.")
                    stopped = True; break
                if sum(len(json.dumps(b)) for b in blocks.values()) + sum(len(s) for s in fragments.values()) > 4_000_000:
                    raise ValueError("The model response exceeded Chat's 4 MB limit.")
            if cancel.is_set():
                raise InterruptedError("Stopped")
            if not stopped:
                raise ValueError("The model stream ended before its response completed. No pending tool was executed.")
            content = [blocks[index] for index in sorted(blocks)]
            if not content:
                raise ValueError("The model returned an empty response.")
            return {"role": "assistant", "content": content, "usage": usage, "stop_reason": reason}
        finally:
            connection.close()
            with self._mutex:
                self._socket = None

    @staticmethod
    def events(response):
        parts = []
        total = 0
        while line := response.readline(1_000_001):
            if len(line) > 1_000_000:
                raise ValueError("The gateway sent an oversized stream event.")
            text = line.decode("utf-8").rstrip("\r\n")
            if not text:
                if parts:
                    yield json.loads("\n".join(parts))
                    parts = []; total = 0
            elif text.startswith("data:"):
                parts.append(text[5:].lstrip(" ")); total += len(text)
                if total > 1_000_000:
                    raise ValueError("The gateway sent an oversized stream event.")


class ChatService:
    def __init__(self, store, transport, emit, *, runner=ChatToolRunner, workspaces=None, child_factory=None, role="parent"):
        self.store, self.transport, self.emit, self.runner_type = store, transport, emit, runner
        self.workspaces = workspaces or ChatWorkspaces(store.root, (chat["workspace"] for chat in store.headers()))
        self.role = role
        self.child_factory = child_factory or (lambda: GatewayClient(transport.root))
        self.child = None
        self.sides = {}
        self._branch_working = False
        self.max_rounds = MAX_ROUNDS
        self._delegations = 0
        self.models = []
        self.chat = None
        self.cancel = threading.Event()
        self.thread = None
        self._working = False
        self._git_working = False
        self._steering = False
        self._resume_after_interrupt = False
        self.approval = None
        self.approval_event = threading.Event()
        self.approval_allowed = False
        self._mutex = threading.RLock()

    @property
    def busy(self):
        return self._working

    @property
    def side(self):
        return self.sides.get(self.chat["id"]) if self.chat else None

    @side.setter
    def side(self, value):
        if value is not None:
            self.sides[value.parent_id] = value
        elif self.chat:
            self.sides.pop(self.chat["id"], None)

    def summaries(self):
        keys = ("id", "title", "updated", "route", "account", "workspace", "effort", "approvalMode", "scope")
        return [{key: chat[key] for key in keys} for chat in self.store.headers()]

    def publish(self):
        self.emit({"event": "chats", "chats": self.summaries()})
        if self.chat:
            self.emit({"event": "selected", "id": self.chat["id"], "entries": self.chat["entries"], "usage": self.chat.get("usage")})
        else:
            self.emit({"event": "selected", "id": None, "entries": [], "usage": None})
        if self.role == "parent" and self.chat and self.chat.get("agents"):
            from chat_agents import publish_agents
            publish_agents(self)
        if self.role == "parent" and self.side:
            from chat_agents import publish_side
            publish_side(self)

    def refresh(self):
        self.models = self.transport.catalogue()
        self.publish_workspaces()

    def publish_workspaces(self):
        self.emit({"event": "catalogue", "models": self.models, "folders": self.workspaces.folders[:]})

    def current_workspace(self):
        if self.chat:
            return self.chat["workspace"]
        return self.workspaces.folders[0] if self.workspaces.folders else str(Path.home())

    def initialize(self):
        self.publish_workspaces()
        chats = self.store.all()
        for chat in chats:
            from chat_agents import recover_agents
            if recover_agents(chat, self.settle): self.store.save(chat)
            if chat.get("status") == "working" or chat.get("pending_update"):
                self.settle(chat, "Chat was interrupted before a recorded result. Inspect the workspace before retrying.")
                self.apply_pending_update(chat)
                chat["entries"].append(entry("notice", "Interrupted when Chat closed. Recorded changes and output are kept.", chat["route"]))
                for item in chat["entries"]:
                    if item.get("kind") == "tool" and item.get("detail") == "Running…":
                        item["detail"] = "Interrupted. Inspect the workspace before running this action again."; item["isError"] = True
                chat["status"] = "interrupted"; self.store.save(chat)
        if chats:
            self.chat = self.store.load(chats[0]["id"])
        self.publish()
        try:
            self.refresh()
        except Exception as exc:
            self.emit({"event": "notice", "message": "Saved chats are available. The gateway is offline: " + str(exc)})
        if not chats and self.models:
            try:
                self.create(self.models[0]["id"], self.current_workspace())
            except (ValueError, OSError) as exc:
                self.emit({"event": "notice", "message": str(exc)})
        self.publish(); self.emit({"event": "ready"})

    def choice(self, identifier=None):
        identifier = identifier or (self.chat["route"] + "|" + self.chat["account"] if self.chat else "")
        row = next((row for row in self.models if row["id"] == identifier), None)
        if row is None:
            raise ValueError("This model/account is no longer in the gateway catalogue. Refresh models and choose a new chat.")
        return row

    def create(self, identifier, workspace, effort="", approval_mode="manual"):
        choice = self.choice(identifier)
        folder = available_workspace(workspace)
        if effort and effort not in choice["efforts"]:
            raise ValueError("Choose one of this model's supported reasoning levels.")
        if approval_mode not in APPROVAL_MODES:
            raise ValueError("Choose Manual, Accept Edits, or YOLO.")
        self.workspaces.remember(folder)
        self.publish_workspaces()
        self.chat = {"id": uuid.uuid4().hex, "title": "New chat", "updated": now(),
                     "route": choice["route"], "account": choice["account"], "scope": choice["scope"],
                     "workspace": str(folder), "effort": effort, "approvalMode": approval_mode,
                     "entries": [], "messages": [], "status": "ready"}
        self.store.save(self.chat); self.publish()

    def add(self, item):
        with self._mutex:
            if item.get("kind") == "tool": item.setdefault("workspace", self.chat["workspace"])
            self.chat["entries"].append(item)
            self.emit({"event": "entry", "chat": self.chat["id"], "entry": item})
        return item

    def save(self):
        with self._mutex:
            self.chat["updated"] = now(); self.store.save(self.chat)

    def prepare_update(self, command):
        text = command.get("text", "")
        inputs = command.get("attachments", [])
        if not isinstance(text, str) or (not text.strip() and not inputs) or len(text) > MAX_TEXT:
            raise ValueError("Enter a message of at most 100,000 characters.")
        attached, blocks = prepare_attachments(inputs, self.store.root / self.chat["id"], vision=self.choice().get("vision") is not False)
        visible = entry("user", text, self.chat["route"], attachments=attached)
        content = [{"type": "text", "text": text if text.strip() else "Please review the attached files."}, *blocks]
        return {"entry": visible, "content": content}

    @staticmethod
    def apply_pending_update(chat):
        pending = chat.pop("pending_update", None)
        if pending:
            chat["messages"].append({"role": "user", "content": pending["content"]})
        return pending

    def interrupt_with_update(self, command):
        if not self.chat or command.get("id") != self.chat["id"]:
            raise ValueError("Select the active chat before sending an update.")
        if not self.busy:
            return self.handle({**command, "command": "send"})
        with self._mutex:
            if not self.busy:
                return self.handle({**command, "command": "send"})
            if self._steering:
                raise ValueError("The previous update is interrupting the turn. Keep the next draft in the composer.")
            pending = self.prepare_update(command)
            self.chat["pending_update"] = pending
            self.chat["entries"].append(pending["entry"])
            try: self.save()
            except Exception:
                self.chat.pop("pending_update", None)
                self.chat["entries"] = [row for row in self.chat["entries"] if row["id"] != pending["entry"]["id"]]
                raise
            self._steering = True; self._working = True; self._resume_after_interrupt = True
            previous = self.thread
            self.cancel.set(); self.transport.cancel(); self.approval_event.set()
            if self.child:
                self.child.cancel.set(); self.child.transport.cancel(); self.child.approval_event.set()
            self.emit({"event": "entry", "chat": self.chat["id"], "entry": pending["entry"]})
            self.emit({"event": "state", "busy": True, "interrupting": True, "status": "Interrupting for your update…"})
        # The old turn must finish cleanup and record real tool outcomes before
        # the next request starts. This one interruption is never a task queue.
        def restart():
            if previous is not None: previous.join()
            with self._mutex:
                self.apply_pending_update(self.chat)
                resume = self._resume_after_interrupt
                self._steering = False; self._resume_after_interrupt = False
                self.chat["status"] = "working" if resume else "stopped"
                try: self.save(); self.publish()
                except Exception:
                    resume = False
                    self.emit({"event": "error", "message": "The update was saved but Chat could not restart. Reopen Chat to recover it."})
                if resume:
                    self.cancel.clear(); self.approval_event.clear()
                    self.thread = threading.Thread(target=self.run, name="provider-hub-chat", daemon=True)
                    self.thread.start()
                else:
                    self._working = False
                    self.emit({"event": "state", "busy": False, "interrupting": False, "status": "Stopped"})
        threading.Thread(target=restart, name="chat-interrupt", daemon=True).start()

    def handle(self, command):
        action = command.get("command")
        if self.role == "parent":
            from chat_agents import handle_auxiliary
            if handle_auxiliary(self, command):
                return
        if self._branch_working and action != "stop":
            raise ValueError("Wait for the branch operation to finish.")
        if action == "git_status":
            if not self.chat or command.get("id") != self.chat["id"] or self._git_working:
                return
            self._git_working = True
            identifier, workspace = self.chat["id"], self.chat["workspace"]
            def report_git():
                try: self.emit({"event": "git_status", "chat": identifier, "workspace": workspace, "status": git_status(workspace)})
                finally: self._git_working = False
            threading.Thread(target=report_git, name="chat-git-status", daemon=True).start()
            return
        if action == "stop":
            with self._mutex:
                self._resume_after_interrupt = False
                self.cancel.set(); self.transport.cancel(); self.approval_event.set()
                if self.child:
                    self.child.cancel.set(); self.child.transport.cancel(); self.child.approval_event.set()
            return
        if action == "steer":
            return self.interrupt_with_update(command)
        if action == "approve":
            with self._mutex:
                if self.approval is None or command.get("id") != self.approval["id"]:
                    raise ValueError("This approval is no longer pending.")
                self.approval_allowed = command.get("allow") is True
                self.approval_event.set()
            return
        if self.busy:
            self.emit({"event": "notice", "message": "Stop the current turn before changing chats or settings."}); return
        if action == "refresh":
            self.refresh(); self.publish()
        elif action == "create":
            self.create(command["choice"], command.get("workspace", self.current_workspace()))
        elif action == "select":
            selected = self.store.load(command["id"])
            # Reading a saved transcript must also work with a disconnected
            # drive. New chats and tool execution still require a real folder.
            self.workspaces.remember(selected["workspace"], require_available=False)
            self.chat = selected
            self.publish_workspaces(); self.publish()
        elif action == "configure":
            choice = self.choice(command.get("choice") or (self.models[0]["id"] if not self.chat and self.models else None))
            folder = command.get("workspace", self.current_workspace())
            if "workspace" in command:
                folder = available_workspace(folder)
            effort = command.get("effort", self.chat["effort"] if self.chat and choice["route"] == self.chat["route"] else "")
            mode = command.get("approvalMode", self.chat.get("approvalMode", "manual") if self.chat else "manual")
            if mode not in APPROVAL_MODES:
                raise ValueError("Choose Manual, Accept Edits, or YOLO.")
            if self.chat and not self.chat["messages"] and not self.side and not (("effort" in command or "approvalMode" in command) and "choice" not in command and "workspace" not in command):
                old_id = self.chat["id"]
                self.create(choice["id"], folder, effort, mode); self.store.delete(old_id); self.publish()
            elif self.chat and ("effort" in command or "approvalMode" in command) and "choice" not in command and "workspace" not in command:
                if effort and effort not in choice["efforts"]:
                    raise ValueError("Choose a supported reasoning level.")
                # CLI adapters already restart native reasoning when effort changes.
                self.chat["effort"] = effort; self.chat["approvalMode"] = mode; self.save(); self.publish()
            elif self.chat and "choice" in command and folder == self.chat["workspace"]:
                if effort and effort not in choice["efforts"]:
                    raise ValueError("Choose a supported reasoning level.")
                # Archive the exact provider context, then begin a fresh native
                # session using only portable transcript data. Never splice an
                # earlier provider's encrypted/signed trace into a new route.
                messages = portable_history(self.chat["entries"], vision=choice.get("vision") is not False)
                archive = {key: copy.deepcopy(self.chat[key]) for key in ("route", "account", "scope", "effort", "messages")}
                archive["ended"] = now()
                self.chat.setdefault("archives", []).append(archive)
                self.chat.update(route=choice["route"], account=choice["account"], scope=choice["scope"],
                                 effort=effort, messages=messages, status="ready")
                self.add(entry("notice", "Switched to " + choice["label"] + ". Conversation and tool results carried over; earlier provider reasoning kept in the saved log.", choice["route"]))
                self.save(); self.publish()
            else:
                self.create(choice["id"], folder, effort)
        elif action in {"send", "retry"}:
            if not self.chat or command.get("id") != self.chat["id"]:
                raise ValueError("Select a chat before sending.")
            choice = self.choice()
            if action == "send":
                update = self.prepare_update(command)
                accepted_entry = update["entry"]
                self.chat["messages"].append({"role": "user", "content": update["content"]})
                self.chat["entries"].append(accepted_entry)
                if self.chat["title"] == "New chat":
                    text = accepted_entry["text"]
                    self.chat["title"] = " ".join(text.split())[:64] if text.strip() else accepted_entry["attachments"][0]["name"]
            elif self.chat.get("status") not in {"error", "interrupted", "stopped"} or not self.chat["messages"]:
                raise ValueError("There is no interrupted turn to retry.")
            self.chat["status"] = "working"; self.save()
            if action == "send":
                self.emit({"event": "entry", "chat": self.chat["id"], "entry": accepted_entry})
            self.publish()
            self.cancel.clear(); self.approval_event.clear()
            self._working = True
            self.thread = threading.Thread(target=self.run, name="provider-hub-chat", daemon=True)
            self.thread.start()
        elif action == "rename":
            title = command.get("title")
            if not isinstance(title, str) or not title.strip() or len(title) > 120:
                raise ValueError("Use a chat title of at most 120 characters.")
            chat = self.store.load(command["id"]); chat["title"] = title.strip(); self.store.save(chat)
            if self.chat and self.chat["id"] == chat["id"]: self.chat = chat
            self.publish()
        elif action == "delete":
            self.store.delete(command["id"])
            from chat_agents import close_side
            close_side(self, owner=command["id"])
            if self.chat and self.chat["id"] == command["id"]:
                chats = self.store.all(); self.chat = chats[0] if chats else None
            self.publish()
        else:
            raise ValueError("Unknown Chat command.")

    @staticmethod
    def settle(chat, message):
        """Complete every unanswered tool cycle without replaying an action."""
        chat["messages"] = [item for item in chat["messages"] if item.get("content")]
        pending = {}
        for item in chat["messages"]:
            for block in item.get("content", []):
                if block.get("type") == "tool_use": pending[block["id"]] = block
                elif block.get("type") == "tool_result": pending.pop(block.get("tool_use_id"), None)
        if pending:
            chat["messages"].append({"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": identifier, "is_error": True,
                 "content": [{"type": "text", "text": message}]} for identifier in pending]})

    def payload(self, choice):
        chat = self.chat
        bounded, omitted = bound_image_history(chat["messages"])
        if omitted:
            chat["messages"] = bounded
            self.add(entry("notice", "Older images removed from model context. Their thumbnails and original files are kept here.", chat["route"]))
            self.save()
        tools = list(TOOL_DEFINITIONS) if choice["supportsTools"] else []
        if self.role == "side":
            tools = [tool for tool in tools if tool["name"] in {"read_file", "search_files"}]
        elif self.role == "parent" and tools:
            from chat_agents import delegate_definition
            tools.append(delegate_definition(self.models))
        context = choice.get("context")
        output = min(choice.get("max_output") or 4096, 8192)
        if isinstance(context, int) and context > 0:
            output = min(output, max(1, context // 4))
        permission = {"manual": "Manual: patches and shell commands need the user's Allow once.",
                      "accept_edits": "Accept Edits: patches inside the selected Git repository are preauthorized; shell commands and other mutations need Allow once.",
                      "yolo": "YOLO: the user preauthorized the available tools without approval prompts."}[chat.get("approvalMode", "manual")]
        payload = {"model": chat["route"], "messages": chat["messages"], "system": SYSTEM + "\nWorkspace: " + chat["workspace"] + "\nApproval mode: " + permission,
                   "tools": tools, "max_tokens": output,
                   "_provider_hub_surface": "chat", "_provider_hub_account": chat["account"],
                   "_provider_hub_connection": chat["scope"]}
        if self.role == "side":
            payload["system"] += "\nThis is a temporary Side Chat. Only read_file and search_files are available; do not edit files or run commands. This conversation is held in memory until the app closes."
        if chat["effort"]:
            payload["output_config"] = {"effort": chat["effort"]}
        if isinstance(context, int) and context > 0:
            budget = max(1, int(context * .85) - payload["max_tokens"])
            if estimated_tokens(payload) >= budget:
                compacted = compact_conversation(payload, budget)
                if compacted["messages"] != chat["messages"]:
                    chat["messages"] = compacted["messages"]; payload = compacted
                    self.add(entry("notice", "Older model context trimmed. The full visible transcript is kept.", chat["route"]))
                    self.save()
        return payload

    def approved(self, summary, detail=None):
        with self._mutex:
            self.approval_allowed = False; self.approval_event.clear()
            self.approval = {"id": uuid.uuid4().hex, "summary": summary, "workspace": self.chat["workspace"], "detail": detail}
            self.emit({"event": "approval", "approval": self.approval})
        while not self.cancel.is_set() and not self.approval_event.wait(.1):
            pass
        with self._mutex:
            allowed = self.approval_allowed and not self.cancel.is_set(); self.approval = None
        return allowed

    def run(self):
        chat = self.chat
        current = None
        self._delegations = 0
        try:
            choice = self.choice()
            runner = self.runner_type(chat["workspace"], cancel_event=self.cancel)
            for _ in range(self.max_rounds):
                if self.cancel.is_set(): raise InterruptedError("Stopped")
                self.emit({"event": "state", "busy": True, "interrupting": False, "status": "Thinking…"})
                current = self.add(entry("assistant", route=chat["route"]))
                def delta(text):
                    current["text"] += text
                    self.emit({"event": "delta", "chat": chat["id"], "id": current["id"], "text": text})
                message = self.transport.stream(self.payload(choice), self.cancel, delta)
                content = message["content"]
                current["text"] = "".join(block.get("text", "") for block in content if block.get("type") == "text")
                self.emit({"event": "entry", "chat": chat["id"], "entry": current})
                calls = [block for block in content if block.get("type") == "tool_use"]
                if calls and message.get("stop_reason") != "tool_use":
                    raise ValueError("The model did not finish its tool response. No action was executed.")
                ids = [call.get("id") for call in calls]
                if len(calls) > 32 or len(set(ids)) != len(ids) or any(not isinstance(i, str) or not i for i in ids):
                    raise ValueError("The model returned invalid or too many tool calls. No action was executed.")
                chat["messages"].append({"role": "assistant", "content": content})
                counts = message.get("usage") or {}
                chat["usage"] = (counts.get("input_tokens") or 0) + (counts.get("cache_read_input_tokens") or 0) + (counts.get("cache_creation_input_tokens") or 0)
                self.save()
                if not calls:
                    if message.get("stop_reason") == "max_tokens":
                        self.add(entry("notice", "The response reached its output limit. Send Continue to carry on.", chat["route"]))
                    chat["status"] = "ready"; break
                results = {"role": "user", "content": []}
                chat["messages"].append(results)
                for call in calls:
                    if self.cancel.is_set(): raise InterruptedError("Stopped")
                    name, arguments = call.get("name"), call.get("input")
                    # Provider call IDs belong to that provider context and may
                    # repeat after switching routes. Visible row identity is local.
                    display_id = uuid.uuid4().hex
                    try:
                        if name == "delegate":
                            from chat_agents import validate_delegate
                            validate_delegate(self, arguments)
                            description = {"summary": "Delegate: " + arguments["task"][:160], "requires_approval": False}
                        elif self.role == "side" and name not in {"read_file", "search_files"}:
                            raise ValueError("Side Chat only has read_file and search_files.")
                        else:
                            description = runner.describe(name, arguments)
                        must_ask = description["requires_approval"] and needs_approval(chat.get("approvalMode", "manual"), name, arguments, chat["workspace"])
                        allowed = not must_ask or self.approved(description["summary"], arguments.get("patch"))
                        if self.cancel.is_set(): raise InterruptedError("Stopped")
                        if allowed:
                            self.emit({"event": "state", "busy": True, "status": description["summary"]})
                            self.add(entry("tool", route=chat["route"], tool=name, summary=description["summary"], detail="Running…", id=display_id))
                            # Pending tool + working status are durable before execution.
                            self.save()
                            if name == "delegate":
                                from chat_agents import delegate
                                result = delegate(self, arguments, call["id"])
                            else:
                                result = runner.execute(name, arguments)
                        else:
                            result = {"content": [{"type": "text", "text": "The user denied this action. It was not executed."}],
                                      "is_error": True, "summary": description["summary"], "changed_files": []}
                    except InterruptedError:
                        raise
                    except (ValueError, OSError, TypeError, KeyError) as exc:
                        result = {"content": [{"type": "text", "text": str(exc)}], "is_error": True,
                                  "summary": str(name or "Invalid tool"), "changed_files": []}
                    results["content"].append({"type": "tool_result", "tool_use_id": call["id"],
                                              "is_error": result["is_error"], "content": result["content"]})
                    detail = "\n".join(part.get("text", "") for part in result["content"] if part.get("type") == "text")
                    visible = entry("tool", route=chat["route"], tool=name, id=display_id, summary=result["summary"], detail=detail,
                                    isError=result["is_error"], changedFiles=result.get("changed_files") or [], workspace=chat["workspace"])
                    if result.get("agent_id"): visible["agentID"] = result["agent_id"]
                    index = next((i for i, item in enumerate(chat["entries"]) if item["id"] == display_id), None)
                    if index is not None: chat["entries"][index] = visible; self.emit({"event": "entry", "chat": chat["id"], "entry": visible})
                    else: self.add(visible)
                    self.save()
                current = None
            else:
                raise ValueError(f"This turn reached its {self.max_rounds}-step limit. Send Continue to carry on from recorded results.")
        except Exception as exc:
            stopped = self.cancel.is_set() or isinstance(exc, InterruptedError)
            chat["status"] = "stopped" if stopped else "error"
            text = "Stopped. Partial output and recorded actions are kept." if stopped else str(exc)
            chat["messages"] = [item for item in chat["messages"] if item.get("content")]
            self.settle(chat, "Stopped or interrupted before a recorded result. An action may have run; inspect the workspace before retrying.")
            for item in chat["entries"]:
                if item.get("kind") == "tool" and item.get("detail") == "Running…":
                    item["detail"] = "Interrupted. Inspect the workspace before running this action again."; item["isError"] = True
                    self.emit({"event": "entry", "chat": chat["id"], "entry": item})
            self.add(entry("notice" if stopped else "error", text, chat["route"], isError=not stopped))
        finally:
            self.approval = None
            try:
                self.save(); self.publish()
            except Exception:
                self.emit({"event": "error", "message": "Chat could not save its latest result. Inspect the workspace before retrying."})
            status = {"stopped": "Stopped", "error": "Request failed"}.get(chat["status"], "Ready")
            with self._mutex:
                if not self._steering:
                    self._working = False
                    self.emit({"event": "state", "busy": False, "interrupting": False, "status": status, "usage": chat.get("usage")})


def main():
    mutex = threading.Lock()
    def emit(event):
        with mutex:
            print(json.dumps(event, ensure_ascii=False), flush=True)
    service = None
    try:
        root = state_root()
        service = ChatService(ChatStore(root), GatewayClient(root), emit)
        service.initialize()
        for line in sys.stdin:
            command = None
            try:
                if len(line) > 2_000_000: raise ValueError("Chat command is too large.")
                command = json.loads(line)
                if not isinstance(command, dict): raise ValueError("Chat command must be an object.")
                service.handle(command)
            except Exception as exc:
                kind = "rejected" if isinstance(command, dict) and command.get("command") in {"send", "steer"} else "notice" if service.busy else "error"
                emit({"event": kind, "message": str(exc), "busy": service.busy, "interrupting": service._steering})
    except Exception as exc:
        emit({"event": "error", "message": str(exc)})
    finally:
        if service:
            from chat_agents import close_all_sides
            service.handle({"command": "stop"})
            close_all_sides(service)
            if service.thread:
                service.thread.join(timeout=10)


if __name__ == "__main__":
    main()
