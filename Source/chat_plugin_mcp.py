"""Bounded, sequential local stdio MCP client. No SDK or shell launcher."""
from __future__ import annotations

import json
import os
import queue
import selectors
import subprocess
import threading
import time

MAX_MESSAGE = 2_000_000
MAX_TOOL_RESPONSE = 12_000_000  # One bounded 8 MiB screenshot plus JSON/base64.
MAX_TOOLS = 128


class MCPError(RuntimeError):
    pass


class StdioMCPClient:
    """Explicit start; one outstanding request; close interrupts pending calls.

    Servers cannot initiate host requests (sampling/elicitation/roots). Such
    requests receive method-not-found. stderr is drained without retaining it.
    """

    def __init__(self, executable, args=(), *, timeout=30, environment=None):
        if not os.path.isabs(executable):
            raise ValueError("MCP executable must be an absolute configured path")
        if not .1 <= timeout <= 300:
            raise ValueError("MCP timeout must be between .1 and 300 seconds")
        self.executable, self.args, self.timeout = executable, tuple(args), timeout
        self.environment = environment
        self._process = None
        self._serial = threading.Lock()
        self._write_lock = threading.Lock()
        self._lifecycle_lock = threading.Lock()
        self._reaped = False
        self._messages = queue.Queue(maxsize=4)
        self._response_limit = MAX_MESSAGE
        self._next_id = 0
        self._closed = threading.Event()
        self._threads = []

    @property
    def running(self):
        return self._process is not None and self._process.poll() is None and not self._closed.is_set()

    def start(self, cancel_event=None):
        if cancel_event is not None and cancel_event.is_set():
            raise MCPError("MCP startup cancelled")
        env = self.environment
        if env is None:
            env = {k: v for k, v in os.environ.items() if k in
                   {"HOME", "PATH", "LANG", "LC_ALL", "TMPDIR", "USER"}}
        with self._lifecycle_lock:
            if self._process is not None or self._closed.is_set():
                raise MCPError("MCP client cannot be restarted; create a new client")
            self._process = subprocess.Popen([self.executable, *self.args], stdin=subprocess.PIPE,
                                             stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                             env=env, start_new_session=True, bufsize=0)
            os.set_blocking(self._process.stdin.fileno(), False)
        for target in (self._read, self._drain_stderr):
            thread = threading.Thread(target=target, daemon=True)
            self._threads.append(thread)
            thread.start()
        try:
            result = self.request("initialize", {"protocolVersion": "2024-11-05",
                                  "capabilities": {}, "clientInfo": {
                                      "name": "Provider Hub", "version": "1"}}, cancel_event)
            if result.get("protocolVersion") not in ("2024-11-05", "2025-03-26", "2025-06-18"):
                raise MCPError("Unsupported MCP protocol version")
            if not isinstance(result.get("capabilities", {}).get("tools"), dict):
                raise MCPError("MCP server does not advertise tools")
            self._send({"jsonrpc": "2.0", "method": "notifications/initialized"}, cancel_event)
            return result
        except Exception:
            self.close()
            raise

    def _send(self, message, cancel_event=None):
        raw = json.dumps(message, allow_nan=False, separators=(",", ":")).encode() + b"\n"
        if len(raw) > MAX_MESSAGE:
            raise MCPError("MCP message exceeds size limit")
        deadline = time.monotonic() + self.timeout
        while not self._write_lock.acquire(timeout=.05):
            self._check_wait(deadline, cancel_event)
        try:
            self._check_wait(deadline, cancel_event)
            if not self.running:
                raise MCPError("MCP process is unavailable")
            try:
                fd = self._process.stdin.fileno()
                with selectors.DefaultSelector() as selector:
                    selector.register(fd, selectors.EVENT_WRITE)
                    remaining = memoryview(raw)
                    while remaining:
                        self._check_wait(deadline, cancel_event)
                        if not selector.select(.05):
                            continue
                        try:
                            remaining = remaining[os.write(fd, remaining):]
                        except BlockingIOError:
                            continue
            except (OSError, ValueError) as error:
                raise MCPError("MCP write failed") from error
        finally:
            self._write_lock.release()

    def _read(self):
        try:
            for raw in self._lines():
                message = json.loads(raw, parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Non-finite JSON")))
                if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
                    raise MCPError("Invalid MCP response")
                if "method" in message:
                    if "id" in message:
                        self._send({"jsonrpc": "2.0", "id": message["id"], "error": {
                            "code": -32601, "message": "Host requests are not supported"}})
                    continue
                self._messages.put_nowait(message)
        except Exception as error:
            try:
                self._messages.put_nowait(MCPError(str(error)))
            except queue.Full:
                self._closed.set()
            self.close()

    def _lines(self):
        """Nonblocking chunk reads bound memory and stop even with inherited pipes."""
        fd = self._process.stdout.fileno()
        os.set_blocking(fd, False)
        pending = bytearray()
        with selectors.DefaultSelector() as selector:
            selector.register(fd, selectors.EVENT_READ)
            while not self._closed.is_set():
                if not selector.select(.05):
                    continue
                raw = os.read(fd, 65536)
                if not raw:
                    raise MCPError("MCP process closed stdout")
                pending.extend(raw)
                while b"\n" in pending:
                    end = pending.index(b"\n") + 1
                    if end > self._response_limit:
                        raise MCPError("MCP response exceeds size limit")
                    yield bytes(pending[:end])
                    del pending[:end]
                if len(pending) > self._response_limit:
                    raise MCPError("MCP response exceeds size limit")

    def _drain_stderr(self):
        try:
            fd = self._process.stderr.fileno()
            os.set_blocking(fd, False)
            with selectors.DefaultSelector() as selector:
                selector.register(fd, selectors.EVENT_READ)
                while not self._closed.is_set():
                    if selector.select(.05) and not os.read(fd, 4096):
                        return
        except (OSError, ValueError):
            pass

    def request(self, method, params, cancel_event=None):
        deadline = time.monotonic() + self.timeout
        while not self._serial.acquire(timeout=.05):
            self._check_wait(deadline, cancel_event)
        try:
            self._check_wait(deadline, cancel_event)
            self._next_id += 1
            request_id = self._next_id
            self._response_limit = MAX_TOOL_RESPONSE if method == "tools/call" else MAX_MESSAGE
            try:
                self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}, cancel_event)
            except Exception:
                self.close()
                raise
            while True:
                try:
                    self._check_wait(deadline, cancel_event)
                except MCPError:
                    # Reap immediately: cancellation notification could itself block
                    # behind a server request or an upstream that stopped reading.
                    self.close()
                    raise
                try:
                    message = self._messages.get(timeout=.05)
                except queue.Empty:
                    continue
                if isinstance(message, Exception):
                    self.close()
                    raise message
                if type(message.get("id")) is not int or message["id"] != request_id:
                    self.close()
                    raise MCPError("Unexpected MCP response id")
                if "error" in message:
                    raise MCPError("MCP request failed: " + str(message["error"])[:2000])
                result = message.get("result")
                if not isinstance(result, dict):
                    raise MCPError("Invalid MCP result")
                return result
        finally:
            self._response_limit = MAX_MESSAGE
            self._serial.release()

    def _check_wait(self, deadline, cancel_event):
        if self._closed.is_set():
            raise MCPError("MCP client closed")
        if cancel_event is not None and cancel_event.is_set():
            raise MCPError("MCP request cancelled")
        if time.monotonic() >= deadline:
            raise MCPError("MCP request timed out")

    def list_tools(self, cancel_event=None):
        tools, cursor, seen = [], None, set()
        while True:
            result = self.request("tools/list", {"cursor": cursor} if cursor else {}, cancel_event)
            batch = result.get("tools")
            if not isinstance(batch, list) or len(tools) + len(batch) > MAX_TOOLS:
                raise MCPError("Invalid or excessive MCP tool catalog")
            tools.extend(batch)
            cursor = result.get("nextCursor")
            if cursor is None:
                return tools
            if not isinstance(cursor, str) or not cursor or cursor in seen or len(seen) >= MAX_TOOLS:
                raise MCPError("Invalid MCP pagination")
            seen.add(cursor)

    def call_tool(self, name, arguments, cancel_event=None):
        result = self.request("tools/call", {"name": name, "arguments": arguments}, cancel_event)
        if not isinstance(result.get("content"), list):
            raise MCPError("Invalid MCP tool content")
        return result

    def close(self):
        self._closed.set()
        with self._lifecycle_lock:
            process = self._process
            if process is None or self._reaped:
                return
            self._reaped = True
            import signal
            try:
                os.killpg(process.pid, signal.SIGTERM)
                process.wait(timeout=.5)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
        for thread in self._threads:
            if thread is not threading.current_thread():
                thread.join(timeout=.5)
        for stream in (process.stdin, process.stdout, process.stderr):
            stream.close()
