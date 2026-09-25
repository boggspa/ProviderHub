"""The hub's end of a live host tool call: the CLI waits inside it for the host's result.

Native host tools (cli_host_mcp) end a CLI turn at every host call and replay
the whole conversation into a fresh process for the next step. That drops the
model's own reasoning, its search results and its native tool history between
steps. A live session keeps the CLI waiting inside the call instead. The
``host`` MCP server (host_tools_mcp.py) forwards each ``tools/call`` over a
private Unix socket to a :class:`HostCallBridge`, the adapter hands the call to
the desktop harness exactly as before, and when the harness's next request
carries the result the bridge answers the waiting call and the same CLI
process carries on.

Only the owner can reach a bridge: its socket lives in the turn's private
(0700) directory, and every connection must present the random token the hub
wrote into the owner-only tools file. Before anything is handed to the host,
the adapter checks that the CLI is waiting on exactly the calls it read off the
CLI's stream: same tool_use id, same tool, same arguments. So the host only
ever answers calls the CLI is really waiting on.
"""
from __future__ import annotations

import hmac
import json
import os
import secrets
import socket
import threading

#: macOS's sockaddr_un.sun_path holds 104 bytes, including the terminator.
SOCKET_PATH_LIMIT = 103
#: A forwarded call is the model's own output, so a patch or file body is far
#: below this. Anything larger is refused rather than buffered.
MAX_REQUEST_BYTES = 16 * 1024 * 1024
_SOCKET_NAME = "host.sock"
#: How long a connecting server may take to send its request line.
_READ_TIMEOUT = 30.0
#: How often the accept loop looks for close(); a blocked accept() is not
#: reliably woken by closing its socket from another thread.
_ACCEPT_POLL = 0.5


class HostBridgeError(RuntimeError):
    """The bridge could not be opened."""


class _Waiting:
    """One ``tools/call`` the CLI is blocked on until the host's result answers it."""

    __slots__ = ("connection", "tool_use_id", "name", "arguments")

    def __init__(self, connection, tool_use_id, name, arguments):
        self.connection = connection
        self.tool_use_id = tool_use_id
        self.name = name
        self.arguments = arguments


def _close(connection) -> None:
    try:
        connection.close()
    except OSError:
        pass


def _read_line(connection) -> bytes:
    chunks, size = [], 0
    while True:
        chunk = connection.recv(65536)
        if not chunk:
            raise ValueError("the connection closed before a complete request")
        newline = chunk.find(b"\n")
        size += len(chunk) if newline < 0 else newline
        if size > MAX_REQUEST_BYTES:
            raise ValueError("the request exceeds the size limit")
        if newline >= 0:
            chunks.append(chunk[:newline])
            return b"".join(chunks)
        chunks.append(chunk)


class HostCallBridge:
    """The listening end of one live CLI session's host tool calls.

    ``host_name`` maps the name the MCP server received (its served name) to
    the host tool it stands for, or None when it is not one.
    """

    def __init__(self, directory, host_name):
        path = os.path.join(os.fspath(directory), _SOCKET_NAME)
        if len(os.fsencode(path)) > SOCKET_PATH_LIMIT:
            raise HostBridgeError("the live session directory is too deep for a Unix socket path")
        self.path = path
        self.token = secrets.token_urlsafe(32)
        self._host_name = host_name
        self._condition = threading.Condition()
        self._waiting: list[_Waiting] = []
        self._bound: dict[str, _Waiting] = {}
        self._closed = False
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            server.bind(path)
            os.chmod(path, 0o600)
            server.listen(32)
            server.settimeout(_ACCEPT_POLL)
        except OSError as exc:
            server.close()
            raise HostBridgeError(f"could not open the live session socket: {exc}") from exc
        self._server = server
        self._thread = threading.Thread(target=self._accept, name="cli-host-bridge", daemon=True)
        self._thread.start()

    @property
    def endpoint(self) -> dict:
        """What the MCP server needs to reach this bridge (written to the owner-only tools file)."""
        return {"socket": self.path, "token": self.token}

    def _accept(self) -> None:
        while not self._closed:
            try:
                connection, _ = self._server.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            threading.Thread(target=self._receive, args=(connection,),
                             name="cli-host-bridge-call", daemon=True).start()

    def _receive(self, connection) -> None:
        """Read one forwarded call and keep its connection open until the result."""
        try:
            connection.settimeout(_READ_TIMEOUT)
            request = json.loads(_read_line(connection))
            token = request.get("token") if isinstance(request, dict) else None
            if not isinstance(token, str) or not hmac.compare_digest(token.encode(), self.token.encode()):
                raise ValueError("the connection did not present this bridge's token")
            identifier, name, arguments = request.get("tool_use_id"), request.get("name"), request.get("arguments")
            if not isinstance(name, str) or not isinstance(arguments, dict) or (
                    identifier is not None and not isinstance(identifier, str)):
                raise ValueError("the forwarded call is malformed")
            connection.settimeout(None)
        except (OSError, ValueError):
            _close(connection)
            return
        with self._condition:
            if self._closed:
                _close(connection)
                return
            self._waiting.append(_Waiting(connection, identifier, name, arguments))
            self._condition.notify_all()

    def _same(self, call, waiting) -> bool:
        return self._host_name(waiting.name) == call.get("name") and waiting.arguments == call.get("input")

    def check(self, calls) -> str:
        """Whether the CLI is waiting on exactly ``calls``, the stream's host calls by id.

        ``"ready"`` binds each call to its waiting connection. ``"conflict"``
        means the CLI is waiting on something the stream did not show, or with
        other arguments, which the host must never answer. ``"waiting"`` means
        not every call has arrived yet. A call forwarded without its tool_use id
        is paired by tool and arguments instead.
        """
        with self._condition:
            bound, unnamed = {}, []
            for waiting in self._waiting:
                if waiting.tool_use_id is None:
                    unnamed.append(waiting)
                    continue
                call = calls.get(waiting.tool_use_id)
                if call is None or waiting.tool_use_id in bound or not self._same(call, waiting):
                    return "conflict"
                bound[waiting.tool_use_id] = waiting
            for waiting in unnamed:
                match = next((identifier for identifier, call in calls.items()
                              if identifier not in bound and self._same(call, waiting)), None)
                if match is None:
                    return "conflict"
                bound[match] = waiting
            if set(bound) != set(calls):
                return "waiting"
            self._bound = bound
            return "ready"

    def deliver(self, identifier, result) -> bool:
        """Answer the bound call ``identifier`` with an MCP tool result; False if it cannot be."""
        with self._condition:
            waiting = self._bound.pop(identifier, None)
            if waiting is not None:
                self._waiting.remove(waiting)
        if waiting is None:
            return False
        try:
            waiting.connection.sendall(json.dumps(result, ensure_ascii=False).encode("utf-8") + b"\n")
            return True
        except (OSError, TypeError, ValueError):
            return False
        finally:
            _close(waiting.connection)

    def close(self) -> None:
        """Stop listening and drop every waiting call; the server reports them unanswered."""
        with self._condition:
            if self._closed:
                return
            self._closed = True
            waiting, self._waiting, self._bound = self._waiting, [], {}
        _close(self._server)
        for entry in waiting:
            _close(entry.connection)
        try:
            os.unlink(self.path)
        except OSError:
            pass
