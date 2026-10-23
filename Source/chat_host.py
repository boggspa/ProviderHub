"""Persistent Chat Host: the existing ChatHost behind a private local socket.

The Mac UI attaches to this process instead of owning it. Closing, reloading or
crashing the UI only detaches it: runs, approvals, questions and background
processes carry on. Only an explicit `shutdown` command or SIGTERM runs
ChatHost's teardown. There is no idle timeout, and the last client leaving is
not a stop. docs/chat-host.md is the protocol contract.

One Host serves one user and effective state root. Ownership is an flock on
<state>/host/host.lock, held for the Host's whole lifetime; a PID is never
evidence of ownership. The socket and a per-start credential live in the same
private directory. Execution, storage and scheduling stay in ChatHost and its
serial command owner; this module adds only lifetime and connection.

The gateway is not the Host's child. With --ensure-gateway the Host starts one
if none is serving (gateway_service), and it leaves it running when it stops or
crashes: restarting Chat never interrupts the hub's desktop sessions.
"""
from __future__ import annotations

import argparse
import collections
from datetime import datetime, timezone
import fcntl
import hmac
import importlib
import json
import os
from pathlib import Path
import secrets
import select
import signal
import socket
import stat
import struct
import subprocess
import sys
import threading
import time
import uuid

from bridge_core import BridgeError, private_directory, state_root
from chat_runtime import MAX_NATIVE_RESULT, ChatStore, CommandOwner, GatewayClient, entry
from chat_sessions import ChatHost
import gateway_service

PROTOCOL = 1
HELLO_LIMIT = 64_000
HANDSHAKE_SECONDS = 5
# Per-client delivery bound. A client that falls this far behind is detached
# and must reconnect for a fresh snapshot; emitters never wait for a reader.
CLIENT_QUEUE_BYTES = 64 * 1024 * 1024
CLIENT_QUEUE_FRAMES = 20_000
CLOSE_GRACE = 2.0
SOCKET_PATH_LIMIT = 103  # macOS sun_path holds 104 bytes, including the terminator.
EXIT_RUNNING = 3
ROLES = {"desktop", "control", "peer"}
PEER_ADMIN_COMMANDS = {"peer_enroll", "peer_enroll_chat", "peer_revoke"}
HOST_COMMANDS = {"host_status", "detach", "shutdown", "gateway_status", "gateway_start"} | PEER_ADMIN_COMMANDS
CONTROL_COMMANDS = {"host_status", "detach", "shutdown"} | PEER_ADMIN_COMMANDS
# Modules the worker imports lazily. Loading them at start keeps a running Host
# on the code it started with if its app bundle is replaced underneath it.
RUNTIME_MODULES = ("chat_agents", "chat_inspector", "chat_remote", "cli_images", "claude_context")


class HostError(ValueError):
    def __init__(self, message, reply=None):
        super().__init__(message)
        self.reply = reply or {}


def now():
    return datetime.now(timezone.utc).isoformat()


def frame(event):
    return (json.dumps(event, ensure_ascii=False) + "\n").encode("utf-8")


def build_identity():
    """The bundle's version and build, if this runtime came from a release."""
    try:
        manifest = json.loads((Path(__file__).resolve().parent.parent / "build-manifest.json").read_text(encoding="utf-8"))
        application = manifest["application"]
        if isinstance(application["version"], str) and isinstance(application["build"], str):
            return {"version": application["version"], "build": application["build"]}
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return {"version": "development", "build": None}


class HostPaths:
    def __init__(self, root):
        self.root = Path(root)
        self.directory = self.root / "host"
        self.lock = self.directory / "host.lock"
        self.socket = self.directory / "host.sock"
        self.credential = self.directory / "credential"

    def prepare(self):
        private_directory(self.root)
        private_directory(self.directory)
        info = os.lstat(self.directory)
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
            raise HostError("The Chat Host directory must be private to this user.")
        if len(os.fsencode(str(self.socket))) > SOCKET_PATH_LIMIT:
            raise HostError("The Chat state directory path is too long for a local socket.")


def acquire_lock(paths):
    """The Host's ownership: an exclusive flock, released only when the process ends."""
    descriptor = os.open(paths.lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(descriptor)
        return None
    return descriptor


def write_credential(paths):
    value = secrets.token_hex(32)
    temporary = paths.directory / (".credential-" + uuid.uuid4().hex)
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="ascii") as stream:
            stream.write(value); stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, paths.credential)
    finally:
        temporary.unlink(missing_ok=True)
    return value


def read_credential(paths):
    if paths.directory.is_symlink():
        raise HostError("The Chat Host directory must not be a symbolic link.")
    descriptor = os.open(paths.credential, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, encoding="ascii") as stream:
        info = os.fstat(stream.fileno())
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
            raise HostError("The Chat Host credential must be private to this user.")
        return stream.read(256).strip()


def peer_uid(connection):
    """The connecting process's user, from the kernel; None where unavailable."""
    if hasattr(socket, "SO_PEERCRED"):
        data = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        return struct.unpack("3i", data)[1]
    if sys.platform == "darwin":
        # struct xucred { u_int cr_version; uid_t cr_uid; short cr_ngroups; gid_t cr_groups[16]; }
        data = connection.getsockopt(getattr(socket, "SOL_LOCAL", 0), getattr(socket, "LOCAL_PEERCRED", 1), 76)
        version, uid = struct.unpack_from("=II", data)
        if version != 0:
            raise OSError("Unknown peer credential layout")
        return uid
    return None


class Attachment:
    """One connected client: a bounded outbound queue and its own writer thread.

`send` never performs I/O, so ChatHost.emit cannot wait for a slow reader. When
the queue is over its bound the client is detached; it reconnects for a fresh
snapshot rather than receiving a stream with silently missing events.
"""
    def __init__(self, connection, role, client, *, limit_bytes, limit_frames):
        self.id = uuid.uuid4().hex
        self.connection, self.role, self.client = connection, role, client
        self.since = now()
        self.limit_bytes, self.limit_frames = limit_bytes, limit_frames
        self.reason = None
        self._frames = collections.deque()
        self._bytes = 0
        self._closed = False
        self._timer = None
        self._condition = threading.Condition()
        self._writer = threading.Thread(target=self._write, daemon=True, name="chat-host-writer")

    @property
    def closed(self):
        return self._closed

    def start(self):
        self._writer.start()

    def send(self, data):
        with self._condition:
            if self._closed: return False
            if self._frames and (self._bytes + len(data) > self.limit_bytes or len(self._frames) >= self.limit_frames):
                self._finish("overflow", None)
                return False
            self._frames.append(data)
            self._bytes += len(data)
            self._condition.notify()
            return True

    def close(self, reason, final=None):
        with self._condition: self._finish(reason, final)

    def _finish(self, reason, final):
        if self._closed: return
        self._closed, self.reason = True, reason
        self._frames.clear(); self._bytes = 0
        if final is None:
            self._shutdown()
        else:
            # One explanatory frame, then the socket closes. A reader that is
            # not reading still loses the connection after the grace period.
            self._frames.append(final)
            self._timer = threading.Timer(CLOSE_GRACE, self._shutdown)
            self._timer.daemon = True
            self._timer.start()
        self._condition.notify_all()

    def _shutdown(self):
        try: self.connection.shutdown(socket.SHUT_RDWR)
        except OSError: pass

    def _write(self):
        try:
            while True:
                with self._condition:
                    while not self._frames and not self._closed: self._condition.wait()
                    if not self._frames: break
                    data = self._frames.popleft()
                    self._bytes -= len(data)
                self.connection.sendall(data)
        except OSError:
            pass
        finally:
            with self._condition:
                if not self._closed:
                    self._closed, self.reason = True, "disconnected"
                    self._frames.clear(); self._bytes = 0
                timer = self._timer
            if timer is not None: timer.cancel()
            self._shutdown()

    def join(self, timeout):
        if self._writer.is_alive(): self._writer.join(timeout)

    def public(self):
        return {"id": self.id, "role": self.role, "client": self.client, "since": self.since}


class PersistentHost:
    def __init__(self, root, *, ensure_gateway=False, queue_bytes=CLIENT_QUEUE_BYTES, queue_frames=CLIENT_QUEUE_FRAMES):
        self.paths = HostPaths(root)
        self.queue_bytes, self.queue_frames = queue_bytes, queue_frames
        self.generation = uuid.uuid4().hex
        self.started = now()
        self.build = build_identity()
        self.credential = None
        self.owner = CommandOwner()
        self.service = None
        self.ensure_gateway = ensure_gateway
        self.gateway = {"state": "unknown"}
        self.attachments = {}
        self.desktop = None
        self.listener = None
        self.closing = False
        self.overflows = 0
        self.shutdown_reason = None
        self._lock = threading.Lock()
        self._socket_identity = None

    # Delivery -----------------------------------------------------------------

    def emit(self, event):
        """ChatHost's output: queued for the attached window, never waiting on it.

Serialising under the delivery lock keeps the stdio worker's ordering. With
no window attached there is nobody to deliver to: saved state and the next
attachment's welcome snapshot are the record, not a replay of these events.
"""
        if self.desktop is None: return
        with self._lock:
            if self.desktop is not None: self.desktop.send(frame(event))

    def surface(self):
        desktop = self.desktop
        return desktop.id if desktop is not None and not desktop.closed else None

    def _ensure_gateway(self):
        """Start or find this state root's gateway; never ties it to this process."""
        try:
            current = gateway_service.ensure(self.paths.root)
            summary = {"state": "ready", "pid": current.get("pid"), "started": current.get("started"),
                       "active": current.get("active"), "message": ""}
        except (gateway_service.GatewayError, BridgeError, OSError) as exc:
            summary = {"state": "error", "pid": None, "started": False, "message": str(exc)}
        self.gateway = summary
        return summary

    def _gateway_report(self, origin, ensure):
        """HTTP stays off the serial owner; the answer goes to whoever asked."""
        def run():
            if ensure:
                summary = self._ensure_gateway()
                if summary["state"] == "ready" and self.service is not None:
                    self._on_owner(self._refresh_catalogue)
            else:
                current = gateway_service.status(self.paths.root)
                summary = ({"state": "ready", "pid": current.get("pid"), "active": current.get("active"), "message": ""}
                           if current else {"state": "stopped", "pid": None, "message": ""})
            origin.send(frame({"event": "gateway", **summary}))
        threading.Thread(target=run, daemon=True, name="chat-host-gateway").start()

    def _refresh_catalogue(self):
        if self.service is None or self.service.closing: return
        try: self.service.handle({"command": "refresh"})
        except Exception as exc:
            self.emit({"event": "notice", "message": "The gateway started, but Chat could not load its models: " + str(exc)})

    def _on_owner(self, callback):
        """Run on the serial owner without blocking the caller."""
        if self.owner.dispatch_event(callback): return
        def later():
            try: self.owner.dispatch(callback)
            except ValueError: pass
        threading.Thread(target=later, daemon=True, name="chat-host-deferred").start()

    # Connections --------------------------------------------------------------

    def _bind(self):
        path = self.paths.socket
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            info = None
        if info is not None:
            # Holding the lock proves no other Host is serving this socket.
            if not stat.S_ISSOCK(info.st_mode):
                raise HostError("An unexpected file occupies the Chat Host socket path.")
            path.unlink()
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            # The directory is already private; the socket is narrowed too.
            listener.bind(str(path))
            os.chmod(path, 0o600)
            info = os.lstat(path)
            self._socket_identity = (info.st_dev, info.st_ino)
            listener.listen(16)
            listener.settimeout(.25)
        except BaseException:
            listener.close()
            raise
        self.listener = listener

    def _accept(self):
        while not self.closing:
            try:
                connection, _ = self.listener.accept()
            except socket.timeout:
                continue
            except OSError:
                if self.closing: break
                time.sleep(.1)
                continue
            threading.Thread(target=self._attach, args=(connection,), daemon=True, name="chat-host-attach").start()

    def _refuse(self, connection, reason, message):
        try:
            connection.settimeout(1)
            connection.sendall(frame({"event": "host_rejected", "reason": reason, "message": message,
                                      "protocol": PROTOCOL, "build": self.build}))
            connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        connection.close()

    def _hello(self, connection, stream):
        try: uid = peer_uid(connection)
        except OSError: raise HostError("The connecting process could not be identified.", {"reason": "peer"}) from None
        if uid is not None and uid != os.getuid():
            raise HostError("This Chat Host belongs to another user.", {"reason": "peer"})
        connection.settimeout(HANDSHAKE_SECONDS)
        line = stream.readline(HELLO_LIMIT + 1)
        if not line.endswith(b"\n"):
            raise HostError("Send a complete hello first.", {"reason": "invalid_hello"})
        try: hello = json.loads(line).get("hello")
        except (ValueError, AttributeError): hello = None
        if not isinstance(hello, dict):
            raise HostError("Send a complete hello first.", {"reason": "invalid_hello"})
        role, client = hello.get("role", "desktop"), hello.get("client", "")
        if role not in ROLES or not isinstance(client, str) or len(client) > 128 or type(hello.get("protocol")) is not int:
            raise HostError("The hello is not valid.", {"reason": "invalid_hello"})
        principal = None
        if role == "peer":
            if hello["protocol"] != PROTOCOL or self.service is None:
                raise HostError("Peer protocol unavailable.", {"reason": "protocol"})
            try:
                principal = self.service.peers.broker.attach(hello.get("thread_id"), hello.get("token"))
            except (ValueError, TypeError, PermissionError):
                raise HostError("The peer endpoint credential did not match.", {"reason": "credential"}) from None
        else:
            credential = hello.get("credential")
            if not isinstance(credential, str) or not hmac.compare_digest(credential.encode("utf-8", "replace"), self.credential.encode()):
                raise HostError("The Chat Host credential did not match.", {"reason": "credential"})
        # A control connection may read status or ask for shutdown across
        # versions: that is how an updated app retires an older Host.
        if role == "desktop" and hello["protocol"] != PROTOCOL:
            raise HostError(f"This Chat Host speaks protocol {PROTOCOL}; the client asked for {hello['protocol']}.",
                            {"reason": "protocol"})
        connection.settimeout(None)
        return role, client, principal

    def _attach(self, connection):
        attachment = None
        stream = connection.makefile("rb")
        try:
            try:
                role, client, principal = self._hello(connection, stream)
            except HostError as exc:
                return self._refuse(connection, exc.reply.get("reason", "invalid_hello"), str(exc))
            except (OSError, ValueError):
                return self._refuse(connection, "invalid_hello", "Send a complete hello first.")
            candidate = Attachment(connection, role, client, limit_bytes=self.queue_bytes, limit_frames=self.queue_frames)
            candidate.peer_principal = principal
            previous = None
            with self._lock:
                if not self.closing:
                    attachment = candidate
                    self.attachments[attachment.id] = attachment
                    if role == "desktop":
                        previous, self.desktop = self.desktop, attachment
                    attachment.start()
                    # First frame, enqueued before any broadcast can reach this client.
                    attachment.send(frame({"event": "host", "protocol": PROTOCOL, "generation": self.generation,
                                           "attachment": attachment.id, "role": role, "pid": os.getpid(),
                                           "started": self.started, "build": self.build,
                                           "superseded": previous.id if previous is not None else None}))
            if attachment is None:
                return self._refuse(connection, "closing", "The Chat Host is shutting down.")
            if previous is not None:
                previous.close("superseded", frame({"event": "host_detached", "reason": "superseded", "by": attachment.id}))
            if role == "desktop":
                self._on_owner(self._welcome)
            self._read(attachment, stream)
        finally:
            if attachment is not None:
                attachment.close("disconnected")
                self._detached(attachment)
                attachment.join(CLOSE_GRACE + 1)
            try: stream.close()
            except OSError: pass
            connection.close()

    def _welcome(self):
        if self.service is None or self.service.closing: return
        self.service.welcome()

    def _read(self, attachment, stream):
        try:
            while True:
                line = stream.readline(MAX_NATIVE_RESULT + 1)
                if not line or attachment.closed: return
                if not line.endswith(b"\n"):
                    if len(line) > MAX_NATIVE_RESULT:
                        attachment.close("frame_too_large", frame({"event": "host_detached", "reason": "frame_too_large"}))
                    return
                if not self.owner.submit(line, attachment): return
        except (OSError, ValueError):
            return

    def _detached(self, attachment):
        with self._lock:
            if self.attachments.pop(attachment.id, None) is None: return
            if self.desktop is attachment: self.desktop = None
            if attachment.reason == "overflow": self.overflows += 1
        if attachment.role == "peer" and self.service is not None:
            try: self.service.peers.broker.disconnect(attachment.peer_principal)
            except (ValueError, RuntimeError, OSError): pass  # The Host may already be shut down.
        if attachment.role != "desktop" or self.service is None: return
        self.service.capabilities.native.surface_lost(attachment.id)
        if not self.closing:
            self._on_owner(lambda: self.service.detach_client(attachment.id))

    # Serial owner hooks -----------------------------------------------------

    def intercept(self, command, origin):
        command.pop("_attachment", None)
        if origin is None: return False
        action = command.get("command")
        if origin.role == "peer":
            # Complete allowlist: peer traffic cannot enter UI commands, read
            # other transcripts, start a turn, or present itself as the user.
            if origin.closed:
                return True
            if action == "detach":
                origin.close("detached", frame({"event": "host_detached", "reason": "detached"}))
                return True
            if action != "peer_call" or set(command) - {"command", "request", "name", "arguments"}:
                raise HostError("Peer connections accept only peer_call and detach.")
            result = self.service.peers.broker.dispatch(origin.peer_principal, command.get("name"), command.get("arguments", {}))
            origin.send(frame({"event": "peer_result", "request": command.get("request"), "result": result}))
            return True
        if origin.closed and action in {"select", "subscribe", "detach", "host_status", "gateway_status"}:
            # Accepted input still runs; a departed client's view requests do
            # not, so it cannot steer the window that replaced it.
            return True
        if origin.role == "control" and action not in CONTROL_COMMANDS:
            raise HostError("A control connection may only read status, detach or shut the Host down.")
        if action in PEER_ADMIN_COMMANDS:
            broker = self.service.peers.broker
            if action == "peer_enroll":
                allowed = {"command", "request", "app", "native_thread_id", "workspace", "label", "model", "provider"}
                if set(command) - allowed:
                    raise HostError("Unsupported peer enrollment fields.")
                result = broker.enroll(**{key: value for key, value in command.items() if key not in {"command", "request"}})
            elif action == "peer_enroll_chat":
                if set(command) - {"command", "request", "chat"}:
                    raise HostError("Unsupported chat enrollment fields.")
                identifier = command.get("chat")
                service = self.service.session(identifier)
                with service._mutex:
                    endpoint = self.service.peers.enroll(service.chat, restore=True)
                    try:
                        self._peer_chat_notice(endpoint, True)
                    except Exception:
                        broker.revoke(endpoint["thread_id"])
                        raise
                    result = {"thread": endpoint}
            else:
                if set(command) - {"command", "request", "thread_id"}:
                    raise HostError("Unsupported revocation fields.")
                endpoint = broker.thread(command.get("thread_id"))
                broker.revoke(command.get("thread_id"))
                if endpoint:
                    self._peer_chat_notice(endpoint, False)
                result = {"revoked": True}
            origin.send(frame({"event": "peer_result", "request": command.get("request"), "result": result}))
            return True
        if action == "detach":
            origin.close("detached", frame({"event": "host_detached", "reason": "detached"}))
            return True
        if action == "host_status":
            origin.send(frame(self.status()))
            return True
        if action == "shutdown":
            if command.get("ifIdle") is True and not self.idle():
                raise HostError("The Chat Host still has active work. Stop it first, or keep the Host running.")
            self.shutdown_reason = origin.role + " request"
            self.owner.stop()
            return True
        if action in {"gateway_status", "gateway_start"}:
            # Stopping the gateway is the hub's decision (gateway_service.stop),
            # never a side effect of Chat's own lifetime.
            self._gateway_report(origin, ensure=action == "gateway_start")
            return True
        if action == "subscribe":
            command["client"] = origin.id
        elif action == "native_result":
            command["_attachment"] = origin.id
        return False

    def _peer_chat_notice(self, endpoint, enabled):
        if endpoint["app"] != "provider_hub" or endpoint["native_thread_id"] not in self.service.headers:
            return
        service = self.service.session(endpoint["native_thread_id"])
        with service._mutex:
            record = {"thread_id": endpoint["thread_id"], "workspace": endpoint["workspace"], "enabled": enabled}
            if service.chat.get("peerEnrollment") == record:
                return
            service.chat["peerEnrollment"] = record
            text = ("Peer messaging enabled for this chat. Enrolled peers in " + endpoint["workspace"] +
                    " may exchange attributed reference messages. Delivery requires polling; no agent is woken.") if enabled else "Peer messaging revoked for this chat."
            service.add(entry("notice", text, service.chat.get("route", "")))
            service.save()

    def refuse(self, command, error, origin):
        action = command.get("command") if isinstance(command, dict) else None
        if origin is None or origin.role not in {"control", "peer"} and action not in HOST_COMMANDS: return False
        origin.send(frame({"event": "host_error", "command": action,
                           "request": command.get("request") if isinstance(command, dict) else None, "message": str(error)}))
        return True

    def activity(self):
        host = self.service
        sessions = list(host.sessions.values())
        active = [service.chat["id"] for service in sessions if service.chat and host.active(service)]
        approvals = sum(1 for service in host.all_services() if service.approval)
        questions = sum(1 for service in sessions if service.chat for item in service.chat["entries"]
                        if isinstance(item.get("question"), dict) and item["question"].get("status") == "pending")
        return {"activeChats": active, "pendingApprovals": approvals, "pendingQuestions": questions,
                "processes": host.processes.running()}

    def idle(self):
        activity = self.activity()
        return not (activity["activeChats"] or activity["pendingApprovals"] or activity["pendingQuestions"] or activity["processes"])

    def status(self):
        with self._lock:
            attachments = [attachment.public() for attachment in self.attachments.values() if not attachment.closed]
            overflows = self.overflows
        return {"event": "host_status", "protocol": PROTOCOL, "generation": self.generation, "pid": os.getpid(),
                "started": self.started, "build": self.build, "attachments": attachments, "overflows": overflows,
                **self.activity(), "gateway": dict(self.gateway)}

    # Lifetime -----------------------------------------------------------------

    def serve(self, announce, *, install_signals=True):
        try:
            self.paths.prepare()
            lock = acquire_lock(self.paths)
        except (OSError, ValueError, BridgeError) as exc:
            announce({"ready": False, "reason": "error", "message": str(exc)})
            return 1
        if lock is None:
            announce({"ready": False, "reason": "running", "message": "A Chat Host already owns this state directory."})
            return EXIT_RUNNING
        try:
            return self._serve(announce, install_signals)
        finally:
            os.close(lock)

    def _serve(self, announce, install_signals):
        accept = None
        try:
            for name in RUNTIME_MODULES:
                try: importlib.import_module(name)
                except ImportError: pass
            self.credential = write_credential(self.paths)
            if self.ensure_gateway: self._ensure_gateway()
            root = self.paths.root
            self.service = ChatHost(ChatStore(root), GatewayClient(root), self.emit,
                                    dispatch=self.owner.dispatch, dispatch_event=self.owner.dispatch_event)
            self.service.capabilities.native.surface = self.surface
            self.service.initialize()
            self._bind()
            accept = threading.Thread(target=self._accept, daemon=True, name="chat-host-accept")
            accept.start()
            if install_signals:
                def stop(*_):
                    self.shutdown_reason = self.shutdown_reason or "signal"
                    # Never touch the queue's lock from a handler on its own thread.
                    threading.Thread(target=self.owner.stop, daemon=True).start()
                signal.signal(signal.SIGTERM, stop)
                signal.signal(signal.SIGINT, stop)
                signal.signal(signal.SIGHUP, signal.SIG_IGN)
            announce({"ready": True, "protocol": PROTOCOL, "generation": self.generation, "pid": os.getpid(),
                      "socket": str(self.paths.socket), "build": self.build})
            self.owner.run(self.service, intercept=self.intercept, refuse=self.refuse)
            return 0
        except Exception as exc:
            announce({"ready": False, "reason": "error", "message": str(exc)})
            return 1
        finally:
            self._teardown(accept)

    def _teardown(self, accept):
        with self._lock:
            self.closing = True
            attachments = list(self.attachments.values())
        self.owner.closing.set()
        if accept is not None: accept.join(1)
        if self.listener is not None: self.listener.close()
        try:
            if self.service is not None: self.service.shutdown()
        finally:
            goodbye = frame({"event": "host_detached", "reason": "shutdown", "by": self.shutdown_reason or "error"})
            for attachment in attachments: attachment.close("shutdown", goodbye)
            for attachment in attachments: attachment.join(CLOSE_GRACE + 1)
            self._remove_runtime_files()

    def _remove_runtime_files(self):
        try:
            info = os.lstat(self.paths.socket)
            if (info.st_dev, info.st_ino) == self._socket_identity: self.paths.socket.unlink()
        except OSError:
            pass
        if self.credential is not None:
            try:
                if read_credential(self.paths) == self.credential: self.paths.credential.unlink()
            except (OSError, ValueError):
                pass


class StdioPeerSocket(PersistentHost):
    """Peer/admin access for the shipping stdio UI, without replacing its owner.

    ChatStore's flock already excludes another stdio or persistent Chat Host.
    The pipe still owns lifecycle and UI. This socket cannot attach a desktop
    or shut down the worker. All commands use that pipe's serial owner.
    """
    def __init__(self, root, service, owner):
        super().__init__(root)
        self.service, self.owner = service, owner
        self.accept_thread = None
        self.host_lock = None

    def start(self):
        self.paths.prepare()
        self.host_lock = acquire_lock(self.paths)
        if self.host_lock is None:
            raise HostError("A Chat Host already owns this state directory.")
        self.credential = write_credential(self.paths)
        self._bind()
        self.accept_thread = threading.Thread(target=self._accept, daemon=True, name="chat-peer-accept")
        self.accept_thread.start()

    def _hello(self, connection, stream):
        role, client, principal = super()._hello(connection, stream)
        if role == "desktop":
            raise HostError("This Chat worker's UI is owned by its stdio pipe; only peers and local enrollment may attach.", {"reason": "peer_only"})
        return role, client, principal

    def intercept(self, command, origin):
        if origin is not None and origin.role == "control" and command.get("command") not in PEER_ADMIN_COMMANDS | {"host_status", "detach"}:
            raise HostError("The stdio worker's lifetime belongs to its UI; this connection permits only peer enrollment and status.")
        return super().intercept(command, origin)

    def close(self):
        with self._lock:
            self.closing = True
            attachments = list(self.attachments.values())
        if self.listener is not None:
            self.listener.close()
        if self.accept_thread is not None:
            self.accept_thread.join(1)
        for attachment in attachments:
            attachment.close("shutdown", frame({"event": "host_detached", "reason": "shutdown"}))
        for attachment in attachments:
            attachment.join(CLOSE_GRACE + 1)
        self._remove_runtime_files()
        if self.host_lock is not None:
            os.close(self.host_lock)
            self.host_lock = None


# Clients ------------------------------------------------------------------------

class HostRefused(ConnectionError):
    def __init__(self, reply):
        super().__init__((reply or {}).get("message") or "The Chat Host refused the connection.")
        self.reply = reply or {}


class HostConnection:
    """A small blocking client: the reference implementation of the handshake."""
    def __init__(self, connection, *, limit=MAX_NATIVE_RESULT):
        self.connection, self.limit = connection, limit
        self.buffer = bytearray()
        self.host = None

    def send(self, command):
        self.connection.sendall(frame(command))

    def read(self, timeout=None):
        """The next event, or None at end of stream. Raises TimeoutError."""
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            newline = self.buffer.find(b"\n")
            if newline >= 0:
                line = bytes(self.buffer[:newline]); del self.buffer[:newline + 1]
                return json.loads(line)
            if len(self.buffer) > self.limit: raise ValueError("The Chat Host sent an oversized frame.")
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0: raise TimeoutError("No Chat Host event arrived in time.")
            readable, _, _ = select.select([self.connection], [], [], remaining)
            if not readable: continue
            chunk = self.connection.recv(1 << 20)
            if not chunk: return None
            self.buffer.extend(chunk)

    def until(self, predicate, timeout=10):
        deadline = time.monotonic() + timeout
        while True:
            event = self.read(max(0, deadline - time.monotonic()))
            if event is None: raise ConnectionError("The Chat Host closed the connection.")
            if predicate(event): return event

    def close(self):
        try: self.connection.shutdown(socket.SHUT_RDWR)
        except OSError: pass
        self.connection.close()


def connect(root, *, role="desktop", client="", credential=None, protocol=PROTOCOL, timeout=5):
    paths = HostPaths(root)
    credential = read_credential(paths) if credential is None else credential
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        connection.settimeout(timeout)
        connection.connect(str(paths.socket))
        connection.settimeout(None)
        session = HostConnection(connection)
        session.send({"hello": {"protocol": protocol, "role": role, "client": client, "credential": credential}})
        reply = session.read(timeout)
        if not isinstance(reply, dict) or reply.get("event") != "host":
            raise HostRefused(reply)
        session.host = reply
        return session
    except BaseException:
        connection.close()
        raise


def launch(root, *, ensure_gateway=False, python=None, timeout=60, stderr=subprocess.DEVNULL, extra=(), env=None):
    """Start a detached Host for `root`; returns (announcement, process).

A `running` announcement means another Host already owns the state root: attach
to it rather than retrying the launch.
"""
    environment = {**os.environ, **(env or {}), "MISTRAL_BRIDGE_STATE_DIR": str(root)}
    arguments = [python or sys.executable, str(Path(__file__).resolve()), "serve", "--detach",
                 *(["--ensure-gateway"] if ensure_gateway else []), *extra]
    process = subprocess.Popen(arguments, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=stderr,
                               env=environment, start_new_session=True, close_fds=True)
    deadline = time.monotonic() + timeout
    data = b""
    try:
        while b"\n" not in data:
            remaining = deadline - time.monotonic()
            if remaining <= 0: raise TimeoutError("The Chat Host did not report readiness in time.")
            readable, _, _ = select.select([process.stdout], [], [], remaining)
            if not readable: continue
            chunk = os.read(process.stdout.fileno(), 65_536)
            if not chunk: break
            data += chunk
    finally:
        process.stdout.close()
    try: announcement = json.loads(data.split(b"\n", 1)[0])
    except ValueError: announcement = {"ready": False, "reason": "error", "message": "The Chat Host exited before it was ready."}
    return announcement, process


def ensure(root, *, client="", ensure_gateway=False, timeout=60, **options):
    """Attach to this state root's Host, starting one only if none is serving."""
    try:
        return connect(root, client=client, **options), None
    except (FileNotFoundError, ConnectionRefusedError):
        pass
    announcement, process = launch(root, ensure_gateway=ensure_gateway, timeout=timeout)
    if not announcement.get("ready") and announcement.get("reason") != "running":
        raise HostRefused(announcement)
    deadline = time.monotonic() + timeout
    while True:
        try:
            return connect(root, client=client, **options), process
        except (FileNotFoundError, ConnectionRefusedError):
            if time.monotonic() > deadline: raise
            time.sleep(.1)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Provider Hub persistent Chat Host")
    commands = parser.add_subparsers(dest="action", required=True)
    serve = commands.add_parser("serve")
    serve.add_argument("--ensure-gateway", action="store_true", help="Start this state root's gateway if none is serving.")
    serve.add_argument("--detach", action="store_true", help="Leave the launcher's session and process group.")
    serve.add_argument("--client-queue-bytes", type=int, default=CLIENT_QUEUE_BYTES)
    serve.add_argument("--client-queue-frames", type=int, default=CLIENT_QUEUE_FRAMES)
    commands.add_parser("status")
    stop = commands.add_parser("stop")
    stop.add_argument("--if-idle", action="store_true")
    arguments = parser.parse_args(argv)
    root = state_root()
    if arguments.action == "serve":
        if arguments.detach:
            try: os.setsid()
            except OSError: pass
        def announce(value):
            sys.stdout.write(json.dumps(value) + "\n"); sys.stdout.flush()
            if value.get("ready"):
                # Readiness is the only stdout output; a closed launcher pipe
                # must never fail a later write.
                devnull = os.open(os.devnull, os.O_WRONLY)
                os.dup2(devnull, 1); os.close(devnull)
        host = PersistentHost(root, ensure_gateway=arguments.ensure_gateway,
                              queue_bytes=max(1, arguments.client_queue_bytes),
                              queue_frames=max(1, arguments.client_queue_frames))
        return host.serve(announce)
    try:
        session = connect(root, role="control", client="chat_host " + arguments.action)
    except (OSError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        return 1
    try:
        if arguments.action == "status":
            session.send({"command": "host_status"})
            reply = session.until(lambda event: event.get("event") in {"host_status", "host_error"})
        else:
            session.send({"command": "shutdown", "ifIdle": arguments.if_idle})
            reply = session.read(30)
            reply = reply or {"event": "host_detached", "reason": "shutdown"}
        print(json.dumps(reply))
        return 0 if reply.get("event") != "host_error" else 1
    finally:
        session.close()


if __name__ == "__main__":
    sys.exit(main())
