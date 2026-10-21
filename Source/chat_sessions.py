"""One JSONL worker, independently owned chats, and bounded live requests.

Selection is a view cursor. It never changes the chat owned by a running
ChatService. There is no idle timer, scheduler, or process per saved chat.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path
import subprocess
import threading
import time

from chat_runtime import ChatService, GatewayClient
from chat_processes import ProcessRegistry
import chat_execution
from chat_team import live_editable as live_team_edit
from chat_tools import ChatToolRunner
from chat_workspaces import ChatWorkspaces

MAX_ACTIVE_CHATS = 16
MAX_ACTIVE_REQUESTS = 16
# Model requests share the gateway's 16-slot admission budget. Waiting for
# user input or local work does not occupy a provider-request slot.
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

    def secondaries(self, *args):
        with self._lock: return super().secondaries(*args)

    def attach(self, *args):
        with self._lock: return super().attach(*args)

    def detach(self, *args):
        with self._lock: return super().detach(*args)


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
            chat_execution.guard(getattr(cancel, "owner", None))
            return self.client.stream(payload, cancel, delta)
        finally:
            self.host._requests.release()


class WorkspaceRunner:
    def __init__(self, host, workspace, cancel_event):
        self.host, self.cancel = host, cancel_event
        roots = host.workspaces.secondaries(workspace)
        self.runner = host.runner_type(workspace, cancel_event=cancel_event, **({"roots": roots} if roots else {}))
        self.runner.processes = host.processes
        self.workspace = workspace

    def describe(self, name, args):
        self.refresh_roots()
        return self.runner.describe(name, args)

    def refresh_roots(self):
        # Refresh at tool boundaries, including after waiting for approval.
        # A detached folder must not remain authorized in a turn's cached runner.
        if isinstance(self.runner, ChatToolRunner):
            roots = self.host.workspaces.secondaries(self.workspace)
            self.runner.extra_roots = tuple(Path(root).resolve() for root in roots if Path(root).is_dir())
            self.runner.roots = (self.runner.workspace, *self.runner.extra_roots)

    def execute(self, name, args):
        self.refresh_roots()
        if name not in {"apply_patch", "run_shell"}: return self.runner.execute(name, args)
        keys = sorted(self.host.workspace_keys(self.workspace))
        with self.host._metadata:
            locks = [self.host._writers.setdefault(key, ResourceGate(1)) for key in keys]
        # Never hold the checkout while waiting for a user's approval. The
        # service calls execute only after approval; Stop can release a waiter.
        acquired = []
        try:
            for lock in locks:
                lock.take(self.cancel, lambda: self.host.closing)
                acquired.append(lock)
            if self.cancel.is_set() or self.host.closing: raise InterruptedError("Stopped")
            if self.host.workspace_busy(self.workspace, branch_only=True):
                raise ValueError("Wait for this checkout's branch operation to finish.")
            chat_execution.guard(getattr(self.cancel, "owner", None))
            self.refresh_roots()
            if isinstance(self.runner, ChatToolRunner):
                # Attach may occur while a writer waits: newly granted roots
                # become usable on the next call after taking their gates.
                self.runner.roots = tuple(root for root in self.runner.roots if self.host.workspace_key(root) in keys)
                self.runner.extra_roots = self.runner.roots[1:]
            return self.runner.execute(name, args)
        finally:
            for lock in reversed(acquired): lock.release()


class ChatHost:
    def __init__(self, store, transport, emit, *, child_factory=None, runner=ChatToolRunner,
                 max_active=MAX_ACTIVE_CHATS, dispatch=None, dispatch_event=None):
        self.store, self.catalogue_transport, self._emit = store, transport, emit
        self.dispatch = dispatch
        self.dispatch_event = dispatch_event
        self.remote = None
        self.remote_host = None
        self.remote_overflow = False
        self.client_factory = child_factory or (lambda: GatewayClient(transport.root))
        self.runner_type, self.max_active = runner, max_active
        self._metadata = threading.RLock()
        self._output = threading.RLock()
        self._requests = ResourceGate(MAX_ACTIVE_REQUESTS)
        self._writers = {}
        self.processes = ProcessRegistry(self.publish_processes)
        self.headers = {row["id"]: {key: row.get(key) for key in HEADER_KEYS} for row in store.headers()}
        self.workspaces = SharedWorkspaces(store.root, (row["workspace"] for row in self.headers.values()))
        self.models = []
        self.preferences = {"webSearch": True}
        self.sessions = {}
        self.sides = {}
        self.view_id = None
        # Selection is presentation only. Each client may watch several chats.
        self.subscriptions = {}
        self.closing = False
        self._initializing = False
        self._creating = None
        self._replacing = None
        self._last_summaries = None
        self.session_store = SessionStore(self)
        from chat_capabilities import ChatCapabilities
        self.capabilities = ChatCapabilities(store.root, self.emit)

    def emit(self, event):
        self._emit(event)
        if self.remote is not None and event.get("event") != "remote":
            # Never take a network lock while a chat/output lock is held. All
            # remote state and commands are reconciled on the main queue.
            saved = copy.deepcopy(event)
            def publish():
                if self.remote is None: return
                if self.remote_overflow:
                    self.remote_overflow = False
                    self.remote.stop()
                    self.publish_remote("Remote updates fell behind. Reconnect to refresh the device.")
                    return
                try: self.remote.publish(saved)
                except Exception as exc:
                    self.remote.stop()
                    self.publish_remote("Remote access stopped: " + str(exc)[:300])
            if self.dispatch_event is not None and not self.dispatch_event(publish):
                self.remote_overflow = True

    def publish_remote(self, notice=""):
        status = self.remote.status() if self.remote is not None else {"listening": False, "connected": False}
        if self.remote is not None:
            if status.get("listening") and self.remote_host:
                status["bootstrap"] = self.remote.bootstrap(self.remote_host)
        self._emit({"event": "remote", "status": status, "notice": notice})

    def remote_command(self, command):
        action = command.get("command")
        if action not in {"remote_status", "remote_start", "remote_stop", "remote_confirm", "remote_revoke"}: return False
        if action == "remote_start":
            if self.dispatch is None or self.dispatch_event is None: raise ValueError("Remote access needs the serial Chat worker")
            if self.remote is None:
                from chat_remote import RemoteServer
                self.remote = RemoteServer(self.handle, self.store.root / "remote.json", dispatch=self.dispatch, detach=self.detach_client)
            self.remote.start(host=command.get("host"), port=command.get("port", 8765))
            self.remote_host = command.get("host")
            self._last_summaries = None
            self.publish_summaries()
        elif action == "remote_stop" and self.remote is not None:
            self.remote.stop()
            self.subscriptions.pop("remote", None)
        elif action == "remote_confirm":
            if self.remote is None: raise ValueError("No device is pairing")
            allow = command.get("allow")
            if type(allow) is not bool: raise ValueError("Pairing decision must be yes or no")
            self.remote.confirm_pairing(command.get("identity"), command.get("code"), allow)
        elif action == "remote_revoke":
            if self.remote is None: raise ValueError("No paired devices")
            self.remote.revoke(command.get("identity"))
        self.publish_remote()
        return True

    def transport(self):
        return SessionTransport(self, self.client_factory())

    def wake_waiters(self):
        self._requests.wake()
        with self._metadata: writers = list(self._writers.values())
        for writer in writers: writer.wake()

    def runner(self, workspace, *, cancel_event, roots=()):
        return WorkspaceRunner(self, workspace, cancel_event)

    def publish_workspaces(self):
        with self.workspaces._lock:
            event = {"event": "catalogue", "models": self.models, "folders": self.workspaces.folders[:],
                     "projects": copy.deepcopy(self.workspaces.projects)}
        with self._output: self.emit(event)

    def workspace_keys(self, workspace):
        with self.workspaces._lock:
            roots = [Path(root).resolve() for root in [workspace, *self.workspaces.secondaries(workspace)]]
            known = [*self.workspaces.folders, *(root for values in self.workspaces.projects.values() for root in values)]
        # Nested folders overlap even outside Git. Include their registered
        # identities so a primary and an attached subfolder share writer gates.
        overlapping = [root for root in known if any(Path(root).is_relative_to(candidate) or candidate.is_relative_to(Path(root)) for candidate in roots)]
        return {self.workspace_key(root) for root in [*roots, *overlapping]}

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
            choice = next((item for item in self.models if item.get("route") == row.get("route")
                           and item.get("account") == row.get("account")), None)
            if choice:
                presentation = choice.get("presentation") or {}
                row["modelLabel"] = choice.get("label") or ""
                row["providerLabel"] = presentation.get("displayProvider") or choice.get("provider") or ""
                row["accent"] = presentation.get("accent") or ""
        with self._output:
            if summaries != self._last_summaries:
                self._last_summaries = summaries
                self.emit({"event": "chats", "chats": summaries})

    def publish_processes(self, chat, notice=None):
        service = self.sessions.get(chat)
        if service is not None: service.team_wake.set()
        # Output tails go only to the chat on screen; others get the rows.
        with self._output:
            remote_selected = self.remote is not None and chat in self.remote.selections.values()
            event = {"event": "processes", "chat": chat, "processes": self.processes.snapshot(chat, output=chat == self.view_id or remote_selected)}
            if notice: event["notice"] = notice
            self.emit(event)

    def state(self, service):
        status = "Needs approval" if service.approval else getattr(service, "display_status", "Thinking…") if service.busy else {
            "error": "Request failed", "stopped": "Stopped", "interrupted": "Interrupted"}.get(service.chat.get("status"), "Ready")
        return {"busy": service.busy, "interrupting": service._steering or service.busy and service.cancel.is_set(), "status": status,
                "approval": copy.deepcopy(service.approval), "usage": service.chat.get("usage")}

    def publish_remote_configuration(self, service):
        chat = service.chat
        if chat:
            self.emit({"event": "remote_configuration", "chat": chat["id"],
                       "choice": chat["route"] + "|" + chat.get("account", ""),
                       "effort": chat.get("effort", ""), "approvalMode": chat.get("approvalMode", "manual"),
                       "workspace": chat["workspace"], "busy": self.active(service),
                       "runBusy": service.busy, "interrupting": self.state(service)["interrupting"],
                       "status": self.state(service)["status"]})

    def publish_remote_activity(self, service):
        if not self.remote or (service.chat or {}).get("id") not in self.remote.selections.values(): return
        from chat_remote_actions import activity
        state = self.state(service)
        event = {"event": "remote_activity", "chat": service.chat["id"], "busy": state["busy"],
                 "interrupting": state["interrupting"], "status": state["status"], "rows": activity(service)}
        # Streaming token deltas seldom change the activity label. Do not copy
        # the same status frame for each token; still publish every real change.
        if getattr(service, "_last_remote_activity", None) != event:
            service._last_remote_activity = event
            self.emit(event)

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
                if identifier != self.view_id and identifier not in self.watched_chats(): return
                event = {**event, "chat": identifier, **(self.state(service) if service.chat else {})}
                if identifier != self.view_id: event = {**event, "event": "snapshot"}
            elif identifier is not None:
                event = {**event, "chat": identifier}
                if identifier != self.view_id and identifier not in self.watched_chats():
                    if kind in {"delta", "agent_delta", "side_delta"}: return
                    if kind == "entry" and event.get("entry", {}).get("kind") != "user": return
                    if kind == "agents":
                        event = {**event, "agents": [{**agent, "entries": []} for agent in event["agents"]]}
                    if kind == "side" and event.get("side"):
                        event = {**event, "side": {**event["side"], "entries": []}}
            self.emit(event)
            if kind in {"selected", "snapshot", "state"} and service.chat:
                self.publish_remote_configuration(service)
            if kind in {"selected", "snapshot", "state", "team", "entry", "delta"} and service.chat:
                previous = getattr(service, "_last_remote_activity", None) or {}
                # Once a reply has started streaming, later tokens cannot
                # change its activity wording. Avoid rescanning saved history
                # on every token, especially while a peer has no new row yet.
                same_reply = kind == "delta" and any(
                    row.get("entryID") == event.get("id") and row.get("streaming")
                    for row in previous.get("rows", []))
                if not same_reply: self.publish_remote_activity(service)
        if kind in {"state", "approval", "branch_state", "side", "agents"}: self.publish_summaries()

    def new_session(self, chat=None):
        holder = {}
        service = ChatService(self.session_store, self.transport(), lambda event: self.session_event(holder["service"], event),
                              runner=self.runner, workspaces=self.workspaces, child_factory=self.transport)
        holder["service"] = service
        service.chat, service.models, service.preferences, service.sides = chat, self.models, self.preferences, self.sides
        service.process_registry = self.processes
        service.capabilities = self.capabilities
        return service

    def initialize(self):
        self._initializing = True
        try:
            service = self.new_session()
            # Publishes the selected transcript; repeating it here cost the
            # window another full decode before ready.
            service.initialize()
            self.models = service.models
            if service.chat:
                self.sessions[service.chat["id"]] = service
                self.view_id = service.chat["id"]
            self.publish_summaries()
        finally:
            self._initializing = False
        with self._output: self.emit({"event": "ready"})
        self.capabilities.publish()

    def welcome(self):
        """Republish current state for a newly attached client; nothing changes.

The persistent Host calls this on the serial owner after a UI attaches. It is
the same publication `initialize` and `select` perform, ending with `ready`.
"""
        self.publish_workspaces()
        with self._output: self._last_summaries = None
        self.publish_summaries()
        service = self.sessions.get(self.view_id)
        if service is None and self.view_id in self.headers:
            try: service = self.session(self.view_id)
            except (OSError, ValueError, KeyError, TypeError): service = None
        if service is not None:
            with service._mutex: service.publish()
        else:
            with self._output: self.emit({"event": "selected", "id": None, "entries": [], "usage": None})
        self.capabilities.publish()
        self.publish_remote()
        with self._output: self.emit({"event": "ready"})

    def detach_client(self, client):
        """A disconnected client stops watching. Its chats keep running."""
        if client in self.subscriptions:
            self.subscriptions = {key: value for key, value in self.subscriptions.items() if key != client}
        self.prune()

    def session(self, identifier):
        if identifier not in self.sessions:
            self.sessions[identifier] = self.new_session(self.store.load(identifier))
        return self.sessions[identifier]

    def watched_chats(self):
        return set().union(*self.subscriptions.values()) if self.subscriptions else set()

    def prune(self, keep=None):
        for identifier, service in list(self.sessions.items()):
            if (identifier not in {self.view_id, keep} | self.watched_chats() and not self.active(service) and identifier not in self.sides
                    and not service._git_working and not (service.thread and service.thread.is_alive())
                    and not (service.restart_thread and service.restart_thread.is_alive())
                    and not any(item.get("question", {}).get("status") == "pending" for item in service.chat["entries"])):
                self.sessions.pop(identifier, None)

    def workspace_busy(self, workspace, *, excluding=None, branch_only=False):
        candidates = [service for identifier, service in list(self.sessions.items())
                      if identifier != excluding and service.chat and (service._branch_working if branch_only else self.active(service))]
        sides = [side for side in list(self.sides.values()) if side.busy] if not branch_only else []
        if not candidates and not sides:
            with self._metadata:
                if branch_only or not any(gate.active for gate in self._writers.values()): return False
        keys = self.workspace_keys(workspace)
        # An in-flight mutation may finish after detach. Keep its checkout
        # protected until execution releases the already-acquired writer gate.
        if not branch_only:
            with self._metadata:
                if any(self._writers.get(key) and self._writers[key].active for key in keys): return True
        for service in candidates:
            if self.workspace_keys(service.chat["workspace"]) & keys: return True
        for side in sides:
            if self.workspace_keys(side.chat["workspace"]) & keys: return True
        return False

    def reject(self, command, error):
        if isinstance(command, dict) and str(command.get("command", "")).startswith("remote_"):
            self.publish_remote(str(error))
            return
        if isinstance(command, dict) and command.get("command") == "select":
            with self._output:
                self.emit({"event": "selection_rejected", "id": command.get("id"),
                           "request": command.get("request"), "message": str(error)})
            return
        if isinstance(command, dict) and command.get("command") in {"plugins", "browser_enable", "plugin_configure", "plugin_enable", "plugin_start", "native_result"}:
            self.capabilities.notice = str(error)
            self.capabilities.publish()
            return
        command = command if isinstance(command, dict) else {}
        action = command.get("command")
        identifier = command.get("chat") or (command.get("id") if action != "approve" else self.view_id)
        service = self.sessions.get(identifier)
        if action in {"processes", "stop_process", "clear_processes"}:
            # The tab's own notice: a chat error would reset the composer.
            if identifier is not None: self.publish_processes(identifier, str(error))
            return
        if action in {"configure_team", "team_resume", "team_member_stop", "team_member_start"}:
            if service:
                import chat_team
                chat_team.publish(service, request=command.get("request"), notice=str(error))
            else:
                self.emit({"event": "team", "chat": identifier, "request": command.get("request"), "team": None, "notice": str(error)})
            return
        if action in {"blackboard", "blackboard_post", "blackboard_remove", "blackboard_attach", "blackboard_detach"}:
            # The pane's own notice; the board itself is unchanged.
            if identifier is not None:
                self.emit({"event": "blackboard", "chat": identifier, "request": command.get("request"), "notice": str(error)})
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
        if self.remote_command(command): return
        if self.capabilities.handle(command): return
        from chat_remote_actions import handle as remote_action
        handled, result = remote_action(self, command)
        if handled: return result
        if action == "subscribe":
            client, identifiers = command.get("client", "desktop"), command.get("chats")
            if (not isinstance(client, str) or not client or len(client) > 128 or
                    not isinstance(identifiers, list) or len(identifiers) > 256 or
                    any(not isinstance(item, str) or item not in self.headers for item in identifiers)):
                raise ValueError("Invalid chat subscription.")
            watched = set(identifiers)
            self.subscriptions[client] = watched
            for identifier in sorted(watched):
                service = self.session(identifier)
                with service._mutex:
                    with self._output:
                        self.emit({"event": "snapshot", "chat": identifier, "id": identifier,
                                   "entries": copy.deepcopy(service.chat["entries"]), "usage": service.chat.get("usage"),
                                   **self.state(service)})
                    # The snapshot is this chat's only transcript copy; its
                    # helper, Side Chat and Team state follow for the inspector.
                    service.publish_inspector()
                    self.publish_remote_configuration(service)
            self.prune()
            return
        if action in {"remote_inspect", "remote_browser"}:
            identifier = command.get("chat")
            if identifier not in self.headers: raise ValueError("Choose an existing chat.")
            service = self.session(identifier)
            if action == "remote_inspect":
                self.publish_workspaces()
                self.capabilities.publish()
                service.publish_inspector()
                service.handle({"command": "blackboard", "id": identifier})
                # Empty panels must clear a previous chat's cached state too.
                if not service.chat.get("team"): self.emit({"event": "team", "chat": identifier, "team": None})
                if not service.side: self.emit({"event": "side", "chat": identifier, "side": None})
                if not service.chat.get("todos"): self.emit({"event": "todos", "chat": identifier, "todos": None})
                self.publish_remote_configuration(service)
                self.publish_processes(identifier)
                service._last_remote_activity = None
                self.publish_remote_activity(service)
                from chat_remote_actions import publish_turn_changes
                publish_turn_changes(self, service)
                return
            tool, arguments = command.get("tool"), command.get("arguments", {})
            preview = tool == "browser_remote_preview"
            if preview:
                if arguments: raise ValueError("Preview takes no arguments.")
                self.capabilities.describe(service, "browser_list", {})
            else: self.capabilities.describe(service, tool, arguments)
            browser_generation = self.capabilities.native.generation()
            pending = getattr(self, "_remote_browser_pending", set())
            if identifier in pending: raise ValueError("Wait for this browser action to finish.")
            self._remote_browser_pending = pending
            pending.add(identifier)
            def browse():
                try:
                    # These are direct human controls, independent of a model's
                    # Stop token. Still use the same validated native surface
                    # and revocation generation as agent browser actions.
                    result = self.capabilities.native.call(identifier, tool, arguments, None, generation=browser_generation)
                    details, text = {}, []
                    for block in result.get("content", []):
                        if preview and block.get("type") == "image":
                            self.emit({"event": "remote_browser_preview", "chat": identifier,
                                       "image": (block.get("source") or {}).get("data")})
                        if block.get("type") != "text": continue
                        value = block.get("text", "")
                        try:
                            parsed = json.loads(value)
                            if isinstance(parsed, dict): details.update(parsed)
                            else: text.append(value)
                        except (ValueError, TypeError): text.append(value)
                    self.emit({"event": "remote_browser", "chat": identifier, "summary": result.get("summary", ""),
                               "text": "\n".join(text), "details": details, "isError": bool(result.get("is_error"))})
                except Exception as exc:
                    self.emit({"event": "remote_browser", "chat": identifier, "summary": "Browser unavailable", "text": str(exc), "isError": True})
                finally: pending.discard(identifier)
            threading.Thread(target=browse, name="remote-browser", daemon=True).start()
            return
        self.prune(keep=command.get("chat") or command.get("id"))
        if action == "preferences":
            if type(command.get("webSearch")) is not bool: raise ValueError("Web search must be enabled or disabled.")
            self.preferences["webSearch"] = command["webSearch"]
            return
        if action == "refresh":
            self.models = self.catalogue_transport.catalogue()
            for service in list(self.sessions.values()): service.models = self.models
            self.publish_workspaces()
            self.publish_summaries()
            return
        if action in {"attach_workspace", "detach_workspace"}:
            identifier = command.get("chat") or command.get("id") or self.view_id
            workspace = command.get("workspace")
            if not workspace:
                if identifier is None: raise ValueError("Choose a workspace.")
                workspace = self.session(identifier).chat["workspace"]
            if action == "attach_workspace": self.workspaces.attach(workspace, command["folder"])
            else: self.workspaces.detach(workspace, command["folder"])
            self.publish_workspaces()
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
        if action in {"processes", "stop_process", "clear_processes"}:
            # Polled by the inspector: never loads or wakes a saved chat.
            notice = None
            if action == "stop_process": notice = self.processes.stop_by_user(identifier, command.get("process"))
            elif action == "clear_processes": self.processes.clear(identifier)
            self.publish_processes(identifier, notice)
            return
        if action == "delete":
            service = self.sessions.get(identifier)
            if service and self.active(service): raise ValueError("Stop this chat and its Side Chat before changing it.")
            if service:
                from chat_agents import close_side
                close_side(service, owner=identifier)
            self.session_store.delete(identifier)
            self.sessions.pop(identifier, None)
            for watched in self.subscriptions.values(): watched.discard(identifier)
            self.processes.forget(identifier)
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
        if action in {"send", "retry", "steer", "team_resume", "team_member_start", "answer_question"} and not service.busy:
            if sum(current.busy for current in list(self.sessions.values())) >= self.max_active:
                raise ValueError(f"{self.max_active} chats are already running. Send this draft after a chat finishes.")
        if action in {"send", "retry", "steer", "team_resume", "team_member_start", "side_send", "open_side"}:
            if self.workspace_busy(service.chat["workspace"], branch_only=True):
                raise ValueError("Wait for this checkout's branch operation to finish.")
        if action == "branch_action" and self.workspace_busy(service.chat["workspace"], excluding=identifier):
            self.emit({"event": "branch_state", "chat": identifier, "request": command.get("request"), "busy": False,
                       "notice": "Stop the other chats using this checkout before changing branches or worktrees."})
            return
        if action in {"delete", "rename", "configure", "configure_team"} and self.active(service) and not (
                action == "configure_team" and live_team_edit(service)):
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
        if action == "configure_team" and command.get("_remoteControl") is True and isinstance(command.get("members"), list):
            # Compatibility with companion build 2/3's 240-character roster
            # preview: an unchanged preview is not an edit to the saved role.
            existing = {m["id"]: m for m in (service.chat.get("team") or {}).get("members", [])}
            restored = []
            for item in command["members"]:
                item = dict(item)
                prior = existing.get(item.get("id"))
                if prior and len(prior.get("responsibility", "")) > 240 and item.get("responsibility") == prior["responsibility"][:240]:
                    item["responsibility"] = prior["responsibility"]
                restored.append(item)
            command = {**command, "members": restored}
        if action in {"send", "retry", "team_resume"}: service.display_status = "Connecting…"
        if action == "stop": service.display_status = "Stopping…"
        self._replacing = (service, old_id) if action == "configure" else None
        try:
            service.handle({**command, **({"id": identifier} if action != "approve" else {})})
            if service.chat["id"] != old_id:
                self.sessions.pop(old_id, None)
                self.sessions[service.chat["id"]] = service
                if self.remote is not None and old_id in self.remote.selections.values():
                    replacement = service.chat["id"]
                    for client, selected in self.remote.replace_chat(old_id, replacement).items():
                        self.subscriptions[client] = {selected}
                    self.publish_remote_configuration(service)
                    service.publish()
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
        try:
            self.wake_waiters()
            services = self.all_services()
            for service in services: service.closing = True
            for service in services: service.handle({"command": "stop"})
            from chat_agents import close_all_sides
            for service in list(self.sessions.values()): close_all_sides(service)
        finally:
            # A closed UI pipe can fail the events above; processes still end,
            # because a later shutdown call returns early.
            self.processes.shutdown()
            self.capabilities.close()
            if self.remote is not None: self.remote.stop()
            if hasattr(self, "remote_files"): self.remote_files.close()
        deadline = time.monotonic() + timeout
        for service in services:
            for thread in (service.thread, service.restart_thread, service.branch_thread):
                if thread and thread is not threading.current_thread():
                    thread.join(max(0, deadline - time.monotonic()))
