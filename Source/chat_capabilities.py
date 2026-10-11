"""Optional Chat tools share the normal host approval and cancellation path.

The native UI executes WebKit requests; external MCP servers remain separate
processes. Neither transport can choose a chat, approve itself, or install code.
"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import uuid

from chat_browser import ChatBrowserTools, TOOL_DEFINITIONS as BROWSER_TOOLS, TOOL_NAMES as BROWSER_NAMES
from chat_plugins import PluginRegistry, peekaboo_config
from chat_plugin_mcp import MCPError


SURFACE_UNAVAILABLE = "The Chat window that runs browser actions is not connected. This action was not executed."
SURFACE_LOST = ("The Chat window disconnected after this browser action was sent. Its outcome is unknown; "
                "inspect the page before running it again.")


class NativeActions:
    def __init__(self, emit, *, timeout=90):
        self.emit, self.timeout = emit, timeout
        self._lock = threading.RLock()
        self._pending = {}
        self._closed = False
        self._enabled = True
        self._generation = 0
        # The persistent Host names the attached window that may execute an
        # action; None (the stdio worker) means its parent UI is that window.
        self.surface = None

    def set_enabled(self, enabled):
        with self._lock:
            self._enabled = enabled
            self._generation += 1
            if not enabled:
                for pending in self._pending.values(): pending["event"].set()

    def generation(self):
        with self._lock: return self._generation

    def call(self, chat, tool, arguments, cancel, request_id=None, *, generation=None):
        identifier = request_id or uuid.uuid4().hex
        pending = {"chat": chat, "event": threading.Event(), "result": None, "surface": None, "lost": False}
        with self._lock:
            if (self._closed or not self._enabled or
                    generation is not None and generation != self._generation or
                    cancel is not None and cancel.is_set()):
                raise InterruptedError("Native action cancelled before dispatch")
            if self.surface is not None:
                pending["surface"] = self.surface()
                if pending["surface"] is None: raise ValueError(SURFACE_UNAVAILABLE)
            self._pending[identifier] = pending
            # Revocation shares this lock: registration and emission are a
            # single admission, so disable cannot slip between them.
            try:
                self.emit({"event": "native_action", "id": identifier, "chat": chat,
                           "tool": tool, "arguments": copy.deepcopy(arguments)})
            except Exception:
                self._pending.pop(identifier, None)
                raise
        completed = False
        try:
            deadline = time.monotonic() + self.timeout
            while not pending["event"].wait(.05):
                if cancel is not None and cancel.is_set():
                    raise InterruptedError("Native action stopped; inspect the recorded surface before retrying")
                if time.monotonic() >= deadline:
                    raise ValueError("Native action timed out; its outcome may be unknown")
            if pending["lost"]:
                # Never replayed against a replacement window or client.
                raise ValueError(SURFACE_LOST)
            if pending["result"] is None:
                raise InterruptedError("Native action cancelled")
            completed = True
            return pending["result"]
        finally:
            with self._lock: self._pending.pop(identifier, None)
            if not completed:
                self.emit({"event": "native_cancel", "id": identifier, "chat": chat})

    def respond(self, command):
        result = command.get("result")
        if (not isinstance(result, dict) or not isinstance(result.get("content"), list)
                or type(result.get("is_error")) is not bool):
            raise ValueError("Invalid native tool result")
        with self._lock:
            pending = self._pending.get(command.get("id"))
            # A late result is harmless; an answer for another chat is not.
            if pending is None: return
            if command.get("chat") != pending["chat"]:
                raise ValueError("Native result belongs to another chat")
            if pending["surface"] is not None and command.get("_attachment") != pending["surface"]:
                raise ValueError("Native result belongs to another Chat window")
            if pending["event"].is_set(): return
            pending["result"] = copy.deepcopy(result)
            pending["event"].set()

    def surface_lost(self, surface):
        """Settle actions sent to a window that went away as unknown outcomes."""
        with self._lock:
            for pending in self._pending.values():
                if pending["surface"] == surface and not pending["event"].is_set():
                    pending["lost"] = True
                    pending["event"].set()

    def cancel_all(self, *, close=False):
        with self._lock:
            self._closed = self._closed or close
            self._generation += 1
            pending = list(self._pending.items())
            for _, request in pending: request["event"].set()


class ChatCapabilities:
    def __init__(self, root, emit):
        self.root, self.emit = Path(root), emit
        self.path = self.root / "capabilities.json"
        self._lock = threading.RLock()
        self._browser_enabled = False
        self.notice = ""
        self._startup_cancel = threading.Event()
        self._startup = None
        self.native = NativeActions(emit)
        self.browser = ChatBrowserTools(self.native.call)
        self._plugin_load_error = ""
        try:
            self.plugins = PluginRegistry(self.root)
        except (ValueError, OSError, KeyError, TypeError) as exc:
            # Optional settings must not prevent opening ordinary saved chats.
            # Preserve the original file until the user chooses an executable.
            self._plugin_load_error = "Computer Use needs setup: " + str(exc)
            self.plugins = PluginRegistry(self.root, load_saved=False)
            self.notice = self._plugin_load_error
        self.dependency = json.loads(Path(__file__).with_name("plugin-dependencies.lock.json").read_text())["plugins"]["peekaboo"]
        if self.path.exists():
            if self.path.is_symlink(): raise ValueError("Capability preferences must not be a symbolic link")
            try:
                saved = json.loads(self.path.read_text())
                self._browser_enabled = saved.get("browserUse") is True
            except (ValueError, OSError):
                self.notice = "Could not read plugin preferences. Browser Use is off."
        self.native.set_enabled(self._browser_enabled)

    def _save(self):
        self.root.mkdir(parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(prefix=".capabilities-", dir=self.root)
        try:
            with os.fdopen(descriptor, "w") as stream:
                json.dump({"version": 1, "browserUse": self._browser_enabled}, stream)
                stream.flush(); os.fsync(stream.fileno())
            os.replace(name, self.path)
        finally:
            if os.path.exists(name): os.unlink(name)

    def status(self):
        with self._lock:
            return {"event": "plugins", "browserUse": self._browser_enabled,
                    "computerUseDependency": copy.deepcopy(self.dependency),
                    "plugins": self.plugins.health(), "starting": bool(self._startup and self._startup.is_alive()),
                    "notice": self.notice or self._plugin_load_error}

    def publish(self):
        self.emit(self.status())

    def handle(self, command):
        action = command.get("command")
        if action == "native_result":
            self.native.respond(command)
            return True
        if action not in {"plugins", "browser_enable", "plugin_configure", "plugin_enable", "plugin_start"}:
            return False
        with self._lock:
            self.notice = ""
            if action == "browser_enable":
                enabled = command.get("enabled")
                if type(enabled) is not bool: raise ValueError("Browser Use must be on or off")
                self._browser_enabled = enabled
                self.native.set_enabled(enabled)
                self._save()
            elif action == "plugin_configure":
                self._startup_cancel.set()
                self.plugins.configure(peekaboo_config(command.get("executable")))
                self._plugin_load_error = ""
            elif action == "plugin_enable":
                if command.get("plugin") != "peekaboo": raise ValueError("Unknown plugin")
                self.plugins.set_enabled("peekaboo", command.get("enabled"))
                if not command["enabled"]: self._startup_cancel.set()
            if action == "plugin_start" or action == "plugin_enable" and command.get("enabled"):
                if command.get("plugin") != "peekaboo": raise ValueError("Unknown plugin")
                if self._startup and self._startup.is_alive():
                    self.notice = "Computer Use setup is already running."
                else:
                    self._startup_cancel = threading.Event()
                    cancel = self._startup_cancel
                    def start():
                        try:
                            self.plugins.start("peekaboo", cancel)
                        except (MCPError, ValueError, OSError, KeyError) as exc:
                            self.notice = str(exc)
                        finally:
                            with self._lock: self._startup = None
                            self.publish()
                    self._startup = threading.Thread(target=start, daemon=True, name="chat-plugin-start")
                    self._startup.start()
        self.publish()
        return True

    @staticmethod
    def allowed(service):
        return service.role not in {"side", "lane"}

    def definitions(self, service):
        if not self.allowed(service): return []
        with self._lock:
            tools = copy.deepcopy(BROWSER_TOOLS) if self._browser_enabled else []
        tools.extend({k: v for k, v in item.items() if k != "requires_approval"}
                     for item in self.plugins.tool_definitions())
        return tools

    def handles(self, name):
        return isinstance(name, str) and (name in BROWSER_NAMES or name.startswith("plugin__"))

    def describe(self, service, name, arguments):
        if not self.allowed(service): raise ValueError("Optional tools are unavailable in read-only chats")
        if name in BROWSER_NAMES:
            with self._lock:
                if not self._browser_enabled: raise ValueError("Browser Use is disabled")
            description = self.browser.describe(name, arguments)
        else:
            try: description = self.plugins.describe(name, arguments)
            except MCPError as exc: raise ValueError(str(exc)) from exc
        description["detail"] = json.dumps(arguments, ensure_ascii=False, indent=2)[:12000]
        return description

    def execute(self, service, name, arguments):
        self.describe(service, name, arguments)  # Recheck after any approval wait.
        if name in BROWSER_NAMES:
            with self._lock:
                if not self._browser_enabled: raise ValueError("Browser Use is disabled")
                generation = self.native.generation()
            owner = service
            while getattr(owner, "team_parent", None) or getattr(owner, "helper_parent", None):
                owner = getattr(owner, "team_parent", None) or owner.helper_parent
            browser = ChatBrowserTools(lambda chat, tool, request, cancel, request_id=None:
                self.native.call(chat, tool, request, cancel, request_id, generation=generation))
            return browser.execute(name, arguments, chat_id=owner.chat["id"], cancel_event=service.cancel)
        try: return self.plugins.dispatch(name, arguments, service.cancel)
        except MCPError as exc: raise ValueError(str(exc)) from exc

    def close(self):
        self._startup_cancel.set()
        self.native.cancel_all(close=True)
        self.plugins.close()
