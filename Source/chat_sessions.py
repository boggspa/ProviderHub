"""One JSONL worker, independently owned chats, and bounded live requests.

Selection is a view cursor. It never changes the chat owned by a running
ChatService. There is no idle timer, scheduler, or process per saved chat.
"""
from __future__ import annotations

import copy
from pathlib import Path
import subprocess
import threading
import time

from chat_runtime import ChatService, GatewayClient
from chat_tools import ChatToolRunner
from chat_workspaces import ChatWorkspaces

MAX_ACTIVE_CHATS = 4
MAX_ACTIVE_REQUESTS = 8
HEADER_KEYS = ("id", "title", "updated", "route", "account", "workspace", "effort", "approvalMode", "scope", "status")


class ResourceGate:
    """Bounded active work; waiters sleep until release, Stop, or shutdown."""
    def __init__(self, capacity):
        self.capacity, self.active = capacity, 0
        self.condition = threading.Condition()

    def take(self, cancel, closing):
        with self.condition:
            while self.active >= self.capacity and not cancel.is_set() and not closing():
                self.condition.wait()
            if cancel.is_set() or closing(): raise InterruptedError("Stopped while waiting for a resource")
            self.active += 1

    def release(self):
        with self.condition:
            self.active -= 1
            self.condition.notify_all()

    def wake(self):
        with self.condition: self.condition.notify_all()


class SharedWorkspaces(ChatWorkspaces):
    def __init__(self, *args):
        self._lock = threading.RLock()
        super().__init__(*args)

    def remember(self, *args, **kwargs):
        with self._lock: return super().remember(*args, **kwargs)


class SessionStore:
    """The process owns storage; sessions update only their own snapshot."""
    def __init__(self, host):
        self.host = host
        self.root = host.store.root

    def save(self, chat):
        self.host.store.save(chat)
        self.host.record(chat)

    def load(self, identifier):
        return self.host.store.load(identifier)

    def path(self, identifier):
        return self.host.store.path(identifier)

    def delete(self, identifier):
        self.host.store.delete(identifier)
        with self.host._metadata:
            self.host.headers.pop(identifier, None)

    def headers(self):
        with self.host._metadata:
            return sorted((dict(row) for row in self.host.headers.values()), key=lambda row: row["updated"], reverse=True)


class SessionTransport:
    """Cancellation reaches exactly one socket, including delegated requests."""
    def __init__(self, host, client):
        self.host, self.client, self.root = host, client, getattr(client, "root", host.store.root.parent)

    def catalogue(self):
        return self.client.catalogue()

    def cancel(self):
        self.client.cancel()
        self.host.wake_waiters()

    def stream(self, payload, cancel, delta):
        if self.host.closing or cancel.is_set(): raise InterruptedError("Stopped")
        self.host._requests.take(cancel, lambda: self.host.closing)
        try:
            if self.host.closing or cancel.is_set(): raise InterruptedError("Stopped")
            return self.client.stream(payload, cancel, delta)
        finally:
            self.host._requests.release()


class WorkspaceRunner:
    def __init__(self, host, workspace, cancel_event):
        self.host, self.cancel = host, cancel_event
        self.runner = host.runner_type(workspace, cancel_event=cancel_event)
        self.workspace, self.lock = workspace, None

    def describe(self, name, args):
        return self.runner.describe(name, args)

    def execute(self, name, args):
        if name not in {"apply_patch", "run_shell"}: return self.runner.execute(name, args)
        if self.lock is None:
            key = self.host.workspace_key(self.workspace)
            with self.host._metadata: self.lock = self.host._writers.setdefault(key, ResourceGate(1))
        # Never hold the checkout while waiting for a user's approval. The
        # service calls execute only after approval; Stop can release a waiter.
        self.lock.take(self.cancel, lambda: self.host.closing)
        try:
            if self.cancel.is_set() or self.host.closing: raise InterruptedError("Stopped")
            return self.runner.execute(name, args)
        finally:
            self.lock.release()


class ChatHost:
    def __init__(self, store, transport, emit, *, child_factory=None, runner=ChatToolRunner,
                 max_active=MAX_ACTIVE_CHATS):
        self.store, self.catalogue_transport, self.emit = store, transport, emit
        self.client_factory = child_factory or (lambda: GatewayClient(transport.root))
        self.runner_type, self.max_active = runner, max_active
        self._metadata = threading.RLock()
        self._output = threading.RLock()
        self._requests = ResourceGate(MAX_ACTIVE_REQUESTS)
        self._writers = {}
        self.headers = {row["id"]: {key: row.get(key) for key in HEADER_KEYS} for row in store.headers()}
        self.workspaces = SharedWorkspaces(store.root, (row["workspace"] for row in self.headers.values()))
        self.models = []
        self.preferences = {"webSearch": True}
        self.sessions = {}
        self.sides = {}
        self.view_id = None
        self.closing = False
        self._initializing = False
        self._creating = None
        self._replacing = None
        self._last_summaries = None
        self.session_store = SessionStore(self)

    def transport(self):
        return SessionTransport(self, self.client_factory())

    def wake_waiters(self):
        self._requests.wake()
        with self._metadata: writers = list(self._writers.values())
        for writer in writers: writer.wake()

    def runner(self, workspace, *, cancel_event):
        return WorkspaceRunner(self, workspace, cancel_event)

    @staticmethod
    def workspace_key(workspace):
        path = Path(workspace).resolve()
        try:
            result = subprocess.run(["git", "-C", str(path), "rev-parse", "--show-toplevel"],
                                    capture_output=True, text=True, timeout=3)
            if result.returncode == 0: path = Path(result.stdout.strip()).resolve()
            info = path.stat()
            return (info.st_dev, info.st_ino)
        except (OSError, subprocess.TimeoutExpired):
            return str(path)

    def active(self, service):
        identifier = service.chat["id"] if service.chat else None
        side = self.sides.get(identifier)
        return service.busy or service._steering or service._branch_working or bool(side and side.busy)

    def record(self, chat):
        # Headers contain no transcript, image payload, or provider reasoning.
        with self._metadata:
            self.headers[chat["id"]] = {key: chat.get(key) for key in HEADER_KEYS}

    def publish_summaries(self):
        summaries = self.session_store.headers()
        for row in summaries:
            service = self.sessions.get(row["id"])
            row["busy"] = bool(service and service.busy)
            row["active"] = bool(service and self.active(service))
            row["pendingApproval"] = bool(service and service.approval)
        with self._output:
            if summaries != self._last_summaries:
                self._last_summaries = summaries
                self.emit({"event": "chats", "chats": summaries})

    def state(self, service):
        status = "Needs approval" if service.approval else getattr(service, "display_status", "Thinking…") if service.busy else {
            "error": "Request failed", "stopped": "Stopped", "interrupted": "Interrupted"}.get(service.chat.get("status"), "Ready")
        return {"busy": service.busy, "interrupting": service._steering or service.busy and service.cancel.is_set(), "status": status,
                "approval": copy.deepcopy(service.approval), "usage": service.chat.get("usage")}

    def session_event(self, service, event):
        kind = event.get("event")
        if kind == "ready" and self._initializing: return
        if kind == "state" and event.get("status") is not None: service.display_status = event["status"]
        if kind == "catalogue":
            self.models = event["models"]
            for current in list(self.sessions.values()): current.models = self.models
            with self._output: self.emit(event)
            return
        if kind == "chats":
            if self._replacing is not None or self._creating is service: return
            self.publish_summaries()
            return
        identifier = event.get("chat") or (service.chat["id"] if service.chat else None)
        with self._output:
            if kind == "selected":
                identifier = event.get("id")
                if ((self._initializing and self.view_id is None) or self._creating is service
                        or self._replacing is not None and self._replacing == (service, self.view_id)):
                    self.view_id = identifier
                if identifier != self.view_id: return
                event = {**event, "chat": identifier, **(self.state(service) if service.chat else {})}
            elif identifier is not None:
                event = {**event, "chat": identifier}
                if identifier != self.view_id:
                    if kind in {"delta", "agent_delta", "side_delta"}: return
                    if kind == "entry" and event.get("entry", {}).get("kind") != "user": return
                    if kind == "agents":
                        event = {**event, "agents": [{**agent, "entries": []} for agent in event["agents"]]}
                    if kind == "side" and event.get("side"):
                        event = {**event, "side": {**event["side"], "entries": []}}
            self.emit(event)
        if kind in {"state", "approval", "branch_state", "side", "agents"}: self.publish_summaries()

    def new_session(self, chat=None):
        holder = {}
        service = ChatService(self.session_store, self.transport(), lambda event: self.session_event(holder["service"], event),
                              runner=self.runner, workspaces=self.workspaces, child_factory=self.transport)
        holder["service"] = service
        service.chat, service.models, service.preferences, service.sides = chat, self.models, self.preferences, self.sides
        return service

    def initialize(self):
        self._initializing = True
        try:
            service = self.new_session()
            service.initialize()
            self.models = service.models
            if service.chat:
                self.sessions[service.chat["id"]] = service
                self.view_id = service.chat["id"]
            self.publish_summaries()
            service.publish()
        finally:
            self._initializing = False
        with self._output: self.emit({"event": "ready"})

    def session(self, identifier):
        if identifier not in self.sessions:
            self.sessions[identifier] = self.new_session(self.store.load(identifier))
        return self.sessions[identifier]

    def prune(self, keep=None):
        for identifier, service in list(self.sessions.items()):
            if (identifier not in {self.view_id, keep} and not self.active(service) and identifier not in self.sides
                    and not service._git_working and not (service.thread and service.thread.is_alive())
                    and not (service.restart_thread and service.restart_thread.is_alive())):
                self.sessions.pop(identifier, None)

    def workspace_busy(self, workspace, *, excluding=None, branch_only=False):
        candidates = [service for identifier, service in list(self.sessions.items())
                      if identifier != excluding and service.chat and (service._branch_working if branch_only else self.active(service))]
        sides = [side for side in list(self.sides.values()) if side.busy] if not branch_only else []
        if not candidates and not sides: return False
        key = self.workspace_key(workspace)
        for service in candidates:
            if self.workspace_key(service.chat["workspace"]) == key: return True
        for side in sides:
            if self.workspace_key(side.chat["workspace"]) == key: return True
        return False

    def reject(self, command, error):
        command = command if isinstance(command, dict) else {}
        action = command.get("command")
        identifier = command.get("chat") or (command.get("id") if action != "approve" else self.view_id)
        service = self.sessions.get(identifier)
        if action in {"configure_team", "team_resume"}:
            if service:
                import chat_team
                chat_team.publish(service, request=command.get("request"), notice=str(error))
            else:
                self.emit({"event": "team", "chat": identifier, "request": command.get("request"), "team": None, "notice": str(error)})
            return
        if action in {"open_side", "side_send", "side_model", "side_stop", "close_side"}:
            from chat_agents import visible
            side = self.sides.get(identifier)
            snapshot = visible(side.chat) | {"busy": side.busy, "interrupting": side._steering, "notice": str(error)} if side else None
            request = command.get("request") if action == "open_side" else side.side_request if side else command.get("request")
            self.emit({"event": "side", "chat": identifier, "request": request,
                       "sideID": command.get("side"), "side": snapshot, "notice": str(error)})
            return
        kind = "rejected" if action in {"send", "steer"} else "notice" if service and self.active(service) else "error"
        event = {"event": kind, "message": str(error), "busy": bool(service and service.busy),
                 "interrupting": bool(service and service._steering)}
        if identifier is not None and action not in {"select", "create", "refresh"}: event["chat"] = identifier
        with self._output: self.emit(event)

    def handle(self, command):
        action = command.get("command")
        if action in {"shutdown", "stop_all"}:
            if action == "shutdown": self.shutdown()
            else: self.stop_all()
            return
        if self.closing: raise ValueError("Chat is closing.")
        self.prune(keep=command.get("chat") or command.get("id"))
        if action == "preferences":
            if type(command.get("webSearch")) is not bool: raise ValueError("Web search must be enabled or disabled.")
            self.preferences["webSearch"] = command["webSearch"]
            return
        if action == "refresh":
            self.models = self.catalogue_transport.catalogue()
            for service in list(self.sessions.values()): service.models = self.models
            self.emit({"event": "catalogue", "models": self.models, "folders": self.workspaces.folders[:]})
            self.publish_summaries()
            return
        if action == "select":
            identifier = command["id"]
            service = self.session(identifier)
            self.workspaces.remember(service.chat["workspace"], require_available=False)
            with service._mutex:
                with self._output: self.view_id = identifier
                service.publish()
            self.prune()
            return
        if action == "create" or action == "configure" and not (command.get("chat") or command.get("id") or self.view_id):
            service = self.new_session()
            self._creating = service
            try:
                workspace = command.get("workspace") or self.headers.get(self.view_id, {}).get("workspace") or self.workspaces.folders[0]
                service.create(command.get("choice") or self.models[0]["id"], workspace)
                self.sessions[service.chat["id"]] = service
                self.publish_summaries()
            finally: self._creating = None
            self.prune()
            return
        identifier = command.get("chat") or (command.get("id") if action != "approve" else None) or self.view_id
        if identifier is None: raise ValueError("Choose a chat.")
        if command.get("chat") and action != "approve" and command.get("id", identifier) != identifier:
            raise ValueError("This command names two different chats.")
        if action == "delete":
            service = self.sessions.get(identifier)
            if service and self.active(service): raise ValueError("Stop this chat and its Side Chat before changing it.")
            if service:
                from chat_agents import close_side
                close_side(service, owner=identifier)
            self.session_store.delete(identifier)
            self.sessions.pop(identifier, None)
            self.publish_summaries()
            if self.view_id == identifier:
                rows = self.session_store.headers()
                self.view_id = None
                if rows:
                    try:
                        next_service = self.session(rows[0]["id"])
                        with next_service._mutex:
                            with self._output: self.view_id = rows[0]["id"]
                            next_service.publish()
                        return
                    except (OSError, ValueError, KeyError, TypeError):
                        self.emit({"event": "notice", "message": "Chat deleted. Choose another readable saved chat."})
                self.emit({"event": "selected", "id": None, "entries": [], "busy": False, "approval": None})
            return
        service = self.session(identifier)
        if action in {"send", "retry"} and service.busy:
            raise ValueError("This chat is already running. Use an update to interrupt it.")
        if action in {"send", "retry", "steer", "team_resume"} and not service.busy:
            if sum(current.busy for current in list(self.sessions.values())) >= self.max_active:
                raise ValueError(f"{self.max_active} chats are already running. Send this draft after a chat finishes.")
        if action in {"send", "retry", "steer", "team_resume", "side_send", "open_side"}:
            if self.workspace_busy(service.chat["workspace"], branch_only=True):
                raise ValueError("Wait for this checkout's branch operation to finish.")
        if action == "branch_action" and self.workspace_busy(service.chat["workspace"], excluding=identifier):
            self.emit({"event": "branch_state", "chat": identifier, "request": command.get("request"), "busy": False,
                       "notice": "Stop the other chats using this checkout before changing branches or worktrees."})
            return
        if action in {"delete", "rename", "configure", "configure_team"} and self.active(service):
            raise ValueError("Stop this chat and its Side Chat before changing it.")
        if action == "rename":
            title = command.get("title")
            if not isinstance(title, str) or not title.strip() or len(title) > 120: raise ValueError("Use a chat title of at most 120 characters.")
            with service._mutex:
                service.chat["title"] = title.strip()
                service.save()
            self.publish_summaries()
            return
        old_id = service.chat["id"]
        if action in {"send", "retry", "team_resume"}: service.display_status = "Connecting…"
        if action == "stop": service.display_status = "Stopping…"
        self._replacing = (service, old_id) if action == "configure" else None
        try:
            service.handle({**command, **({"id": identifier} if action != "approve" else {})})
            if service.chat["id"] != old_id:
                self.sessions.pop(old_id, None)
                self.sessions[service.chat["id"]] = service
            self.publish_summaries()
        finally: self._replacing = None

    def all_services(self):
        services = list(self.sessions.values()) + list(self.sides.values())
        for parent in list(services):
            with parent._mutex:
                if parent.child: services.append(parent.child)
                services.extend(parent.lanes)
                services.extend(parent.team_children.values())
        return list({id(service): service for service in services}.values())

    def stop_all(self):
        for service in self.all_services(): service.handle({"command": "stop"})

    def shutdown(self, timeout=10):
        if self.closing: return
        self.closing = True
        self.wake_waiters()
        services = self.all_services()
        for service in services: service.closing = True
        for service in services: service.handle({"command": "stop"})
        from chat_agents import close_all_sides
        for service in list(self.sessions.values()): close_all_sides(service)
        deadline = time.monotonic() + timeout
        for service in services:
            for thread in (service.thread, service.restart_thread, service.branch_thread):
                if thread and thread is not threading.current_thread():
                    thread.join(max(0, deadline - time.monotonic()))
