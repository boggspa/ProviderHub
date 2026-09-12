#!/usr/bin/env python3
"""Menu app's private worker. Uses the existing Vibe Python runtime only."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import hmac
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import select
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request

from bridge_core import (BridgeError, ClaudeProfile, atomic_json, attach_model_specs, cached_catalogue, catalog, credentials, gateway_token,
                         inspect_state, load_settings, model_labels, private_directory, read_json, ssl_context, state_root, validate_settings)
from catalogue import build_catalogue, read_observations
from protocol import (StreamTranslator, estimated_tokens, model_catalog, resolve_model,
                      translate_request, translate_response)

MAX_BODY = 32 * 1024 * 1024


class Runtime:
    def __init__(self, root: Path, upstream_url="https://api.mistral.ai/v1", key=None):
        self.root = root
        self.settings, self.catalogue = attach_model_specs(load_settings(root), root)
        self.token = gateway_token(root)
        self.key, self.source = (key, "test") if key is not None else credentials(self.settings)
        self.upstream_url = upstream_url
        self.lock = threading.Lock()
        self.semaphore = threading.BoundedSemaphore(8)
        self.active = 0
        self.completed = 0
        self.failed = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.last_error = ""
        self.last_model = ""
        self.started = time.time()
        self.stopping = threading.Event()
        self.connections = set()

    def status(self):
        with self.lock:
            return {"running": True, "active": self.active, "completed": self.completed, "failed": self.failed,
                    "input_tokens": self.input_tokens, "output_tokens": self.output_tokens,
                    "last_error": self.last_error, "last_model": self.last_model,
                    "credential_source": self.source, "uptime_seconds": int(time.time() - self.started), "pid": os.getpid()}

    def record(self, kind, model="", code=None, usage=None, model_unavailable=False):
        # Deliberately excludes prompts, tool arguments, response text, headers,
        # credentials, and raw upstream error bodies.
        event = {"time": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "event": kind}
        if model:
            event["model"] = model
        if code is not None:
            event["status"] = code
        if usage:
            event["usage"] = usage
        if model_unavailable:
            event["model_unavailable"] = True
        with self.lock:
            if kind == "completed":
                self.completed += 1
                self.input_tokens += (usage or {}).get("input_tokens", 0)
                self.output_tokens += (usage or {}).get("output_tokens", 0)
                self.last_error = ""
            elif kind == "error":
                self.failed += 1
                self.last_error = f"Request failed (HTTP {code}). See the message in Claude."
            self.last_model = model or self.last_model
            path = self.root / "activity.jsonl"
            if path.exists() and path.stat().st_size > 512000:
                path.replace(self.root / "activity.previous.jsonl")
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, "a") as stream:
                stream.write(json.dumps(event) + "\n")

    def upstream(self):
        parsed = urllib.parse.urlsplit(self.upstream_url)
        if parsed.scheme == "https":
            connection = http.client.HTTPSConnection(parsed.hostname, parsed.port or 443, timeout=180, context=ssl_context())
        elif parsed.hostname == "127.0.0.1":
            # Only the in-process test harness supplies a local HTTP upstream.
            connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=10)
        else:
            raise BridgeError("Mistral connections require HTTPS.")
        return connection, parsed.path.rstrip("/") + "/chat/completions"


def error_type(status):
    return {400: "invalid_request_error", 401: "authentication_error", 403: "permission_error",
            404: "not_found_error", 413: "request_too_large", 429: "rate_limit_error", 503: "overloaded_error"}.get(status, "api_error")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "MistralBridge/0.2.0"

    @property
    def runtime(self):
        return self.server.runtime

    def setup(self):
        super().setup()
        self.connection.settimeout(30)

    def log_message(self, *_):
        pass

    def json_response(self, status, body, headers=None):
        encoded = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(encoded)
        self.close_connection = True

    def error(self, status, message):
        self.json_response(status, {"type": "error", "error": {"type": error_type(status), "message": message}})

    def allowed(self, *, health=False):
        host = self.headers.get("Host", "").split(":")[0]
        if host not in {"127.0.0.1", "localhost"} or self.headers.get("Origin"):
            self.error(403, "This gateway accepts local desktop requests only.")
            return False
        if health:
            return True
        auth = self.headers.get("Authorization", "")
        token = auth[7:] if auth.startswith("Bearer ") else self.headers.get("x-api-key", "")
        if not hmac.compare_digest(token, self.runtime.token):
            self.error(401, "The local gateway credential is missing or incorrect.")
            return False
        return True

    def do_GET(self):
        path = urllib.parse.urlsplit(self.path).path
        if not self.allowed(health=path == "/_bridge/health"):
            return
        if path == "/_bridge/health":
            self.json_response(200, {"service": "mistral-bridge", "version": "0.2.0"})
        elif path == "/_bridge/status":
            self.json_response(200, self.runtime.status())
        elif path == "/v1/models":
            self.json_response(200, model_catalog(self.runtime.settings))
        else:
            self.error(404, "Unknown gateway endpoint.")

    def do_POST(self):
        if not self.allowed():
            return
        path = urllib.parse.urlsplit(self.path).path
        if path not in {"/v1/messages", "/v1/messages/count_tokens"}:
            self.error(404, "Unknown gateway endpoint.")
            return
        if self.headers.get("Transfer-Encoding"):
            self.error(400, "Use a Content-Length request body.")
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= MAX_BODY:
                self.error(413, "Request body is empty or above 32 MB.")
                return
            body = self.rfile.read(length)
            if len(body) != length:
                raise BridgeError("The request body was interrupted.")
            payload = json.loads(body)
            if not isinstance(payload, dict):
                raise BridgeError("The request must be a JSON object.")
            atomic_json(self.runtime.root / "last-request-shape.json", {
                "fields": sorted(payload),
                "roles": [str(m.get("role"))[:30] for m in payload.get("messages", []) if isinstance(m, dict)],
                "content_types": sorted({str(b.get("type"))[:50] for m in payload.get("messages", []) if isinstance(m, dict)
                                         and isinstance(m.get("content"), list) for b in m["content"] if isinstance(b, dict)}),
                "tool_formats": sorted({str(t.get("type", "function"))[:50] + (":schema" if "input_schema" in t else ":no-schema")
                                         for t in payload.get("tools", []) if isinstance(t, dict)})})
            settings = self.runtime.settings
            if path == "/v1/messages/count_tokens":
                resolve_model(payload.get("model", ""), settings["mappings"])
                self.json_response(200, {"input_tokens": estimated_tokens(payload)}, {"X-Mistral-Bridge-Token-Count": "estimate"})
                return
            upstream, names = translate_request(payload, settings)
        except (ValueError, TypeError, KeyError, AttributeError, BridgeError) as exc:
            self.error(400, str(exc))
            return
        if not self.runtime.semaphore.acquire(blocking=False):
            self.error(429, "Eight requests are already active. Try again shortly.")
            return
        connection = None
        response = None
        upstream_socket = None
        disconnected = threading.Event()
        write_lock = threading.Lock()
        streaming = False
        closed = threading.Event()
        monitor = None
        ping = None
        usage = {}
        try:
            with self.runtime.lock:
                self.runtime.active += 1
            connection, endpoint = self.runtime.upstream()
            with self.runtime.lock:
                self.runtime.connections.add(connection)

            def cancel_monitor():
                while not closed.wait(.25):
                    if self.runtime.stopping.is_set():
                        disconnected.set()
                    else:
                        try:
                            readable, _, _ = select.select([self.connection], [], [], 0)
                            if readable and self.connection.recv(1, socket.MSG_PEEK | socket.MSG_DONTWAIT) == b"":
                                disconnected.set()
                        except (OSError, ValueError):
                            disconnected.set()
                    if disconnected.is_set():
                        try:
                            sock = connection.sock or upstream_socket
                            if sock:
                                sock.shutdown(socket.SHUT_RDWR)
                            connection.close()
                        except OSError:
                            pass
                        return

            monitor = threading.Thread(target=cancel_monitor, daemon=True)
            monitor.start()
            encoded = json.dumps(upstream, ensure_ascii=False).encode()
            connection.request("POST", endpoint, encoded, {"Authorization": "Bearer " + self.runtime.key,
                               "Content-Type": "application/json", "Accept": "text/event-stream" if upstream["stream"] else "application/json",
                               "User-Agent": "MistralBridge/0.2.0"})
            response = connection.getresponse()
            upstream_socket = connection.sock or getattr(getattr(response.fp, "raw", None), "_sock", None)
            if response.status != 200:
                raw = response.read(65536)
                try:
                    data = json.loads(raw)
                    error = data.get("error", data)
                    detail = error.get("message", data.get("detail", "")) if isinstance(error, dict) else str(error)
                except (ValueError, AttributeError):
                    detail = ""
                detail = str(detail).replace(self.runtime.key, "[redacted]")[:700]
                message = f"Mistral returned HTTP {response.status}" + (": " + detail if detail else ".")
                unavailable = response.status in {400, 404, 410} and any(term in detail.lower() for term in ("invalid model", "model not found", "model has been deprecated", "model is no longer"))
                self.runtime.record("error", upstream["model"], response.status, model_unavailable=unavailable)
                self.error(response.status if response.status in {400, 401, 403, 404, 413, 429, 500, 502, 503, 504} else 502, message)
                return
            if not upstream["stream"]:
                raw = response.read(MAX_BODY + 1)
                if len(raw) > MAX_BODY:
                    raise BridgeError("Mistral's response exceeded the response limit.")
                result = translate_response(json.loads(raw), payload["model"], names)
                usage = result["usage"]
                self.json_response(200, result)
            else:
                translator = StreamTranslator(payload["model"], names)
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Transfer-Encoding", "chunked")
                self.send_header("Connection", "close")
                self.end_headers()
                streaming = True

                def write_chunk(data):
                    if disconnected.is_set():
                        raise BrokenPipeError()
                    with write_lock:
                        self.wfile.write(f"{len(data):x}\r\n".encode() + data + b"\r\n")
                        self.wfile.flush()

                def emit(event):
                    write_chunk(("event: " + event["type"] + "\ndata: " + json.dumps(event, ensure_ascii=False) + "\n\n").encode())

                def ping_loop():
                    while not closed.wait(10):
                        try:
                            emit({"type": "ping"})
                        except (OSError, ValueError):
                            disconnected.set()
                            return

                ping = threading.Thread(target=ping_loop, daemon=True)
                ping.start()
                for event in translator.start():
                    emit(event)
                event_lines = []
                event_size = 0
                done = False
                while not disconnected.is_set():
                    line = response.readline(MAX_BODY + 1)
                    if not line:
                        break
                    event_size += len(line)
                    if event_size > MAX_BODY:
                        raise BridgeError("Mistral sent an oversized stream event.")
                    if line in (b"\n", b"\r\n"):
                        data = b"\n".join(event_lines)
                        event_lines.clear()
                        event_size = 0
                        if data == b"[DONE]":
                            done = True
                            break
                        if data:
                            chunk = json.loads(data)
                            if chunk.get("error"):
                                raise BridgeError("Mistral reported an error during generation.")
                            for event in translator.feed(chunk):
                                emit(event)
                    elif line.startswith(b"data:"):
                        event_lines.append(line[5:].strip())
                if disconnected.is_set():
                    raise BrokenPipeError()
                if not done:
                    raise BridgeError("Mistral's stream ended unexpectedly. Please retry.")
                for event in translator.end():
                    emit(event)
                usage = translator.usage
                closed.set()
                with write_lock:
                    self.wfile.write(b"0\r\n\r\n")
                    self.wfile.flush()
                self.close_connection = True
            self.runtime.record("completed", upstream["model"], 200, usage)
        except (BrokenPipeError, ConnectionResetError):
            self.runtime.record("cancelled", upstream["model"])
        except Exception as exc:
            if disconnected.is_set():
                self.runtime.record("cancelled", upstream["model"])
            else:
                message = str(exc) if isinstance(exc, BridgeError) else "The Mistral connection failed. Check your connection and try again."
                self.runtime.record("error", upstream["model"], 502)
                try:
                    if streaming:
                        emit({"type": "error", "error": {"type": "api_error", "message": message}})
                        closed.set()
                        with write_lock:
                            self.wfile.write(b"0\r\n\r\n")
                    else:
                        self.error(502, message)
                except OSError:
                    pass
        finally:
            closed.set()
            if response:
                response.close()
            if connection:
                connection.close()
            with self.runtime.lock:
                self.runtime.connections.discard(connection)
                self.runtime.active -= 1
            self.runtime.semaphore.release()
            self.close_connection = True


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, runtime, port=None):
        self.runtime = runtime
        super().__init__(("127.0.0.1", runtime.settings["port"] if port is None else port), Handler)

    def handle_error(self, *_):
        # Base class prints request tracebacks. Keep request content private.
        pass


def serve(root, parent_pipe=False):
    private_directory(root)
    lock = open(root / "gateway.lock", "a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        raise BridgeError("Mistral Bridge is already running.") from exc
    runtime = Runtime(root)
    try:
        server = Server(runtime)
    except OSError as exc:
        raise BridgeError(f"Port {runtime.settings['port']} is in use. Choose another port in Connection settings.") from exc

    def stop(*_):
        runtime.stopping.set()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    if parent_pipe:
        def parent_watch():
            sys.stdin.buffer.read()
            stop()
        threading.Thread(target=parent_watch, daemon=True).start()
    print(json.dumps({"ready": True, "port": server.server_address[1], "credential_source": runtime.source}), flush=True)
    server.serve_forever(poll_interval=.2)
    runtime.stopping.set()
    with runtime.lock:
        connections = list(runtime.connections)
    for connection in connections:
        try:
            if connection.sock:
                connection.sock.shutdown(socket.SHUT_RDWR)
            connection.close()
        except OSError:
            pass
    server.server_close()
    lock.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["inspect", "save", "discover", "activate", "restore", "serve"])
    parser.add_argument("--parent-pipe", action="store_true")
    args = parser.parse_args()
    root = state_root()
    if args.command == "serve":
        serve(root, args.parent_pipe)
        return
    if args.command == "inspect":
        result = inspect_state(root)
    elif args.command == "save":
        settings = validate_settings(json.load(sys.stdin))
        private_directory(root)
        atomic_json(root / "settings.json", settings)
        inventory = cached_catalogue(settings, root)
        result = {"saved": True, "settings": settings, "friendly_names": model_labels(settings, inventory.get("models", [])), "models": inventory.get("models", [])}
    elif args.command == "discover":
        settings = load_settings(root)
        key, source = credentials(settings)
        from bridge_core import vibe_settings
        inventory = build_catalogue(catalog(key), settings, vibe_settings(), read_observations(root))
        atomic_json(root / "catalog.json", inventory)
        result = {"models": inventory["models"], "credential_source": source, "friendly_names": model_labels(settings, inventory["models"]),
                  "catalog_summary": {k: v for k, v in inventory.items() if k not in {"models", "raw"}}}
    elif args.command == "activate":
        settings = load_settings(root)
        enriched, _ = attach_model_specs(settings, root)
        if any(identifier not in enriched["_model_specs"] for identifier in settings["mappings"].values()):
            raise BridgeError("Refresh models before launching Claude so every mapped model has a provider-reported context limit.")
        token = gateway_token(root)
        # Authenticate the readiness probe so an unrelated process on this port
        # cannot be mistaken for this app's gateway.
        request = urllib.request.Request(f"http://127.0.0.1:{settings['port']}/_bridge/status", headers={"Authorization": "Bearer " + token})
        try:
            with urllib.request.urlopen(request, timeout=3) as response:
                if not json.load(response).get("running"):
                    raise ValueError()
        except Exception as exc:
            raise BridgeError("Start the gateway before launching Claude.") from exc
        result = ClaudeProfile(root).activate(settings, token)
    else:
        result = ClaudeProfile(root).restore()
    print(json.dumps({"ok": True, **result}))


if __name__ == "__main__":
    try:
        main()
    except (BridgeError, OSError, ValueError, subprocess.TimeoutExpired) as exc:
        message = str(exc) if isinstance(exc, BridgeError) else "The operation could not finish. Check the local runtime and configuration."
        print(json.dumps({"ok": False, "error": message}), flush=True)
        sys.exit(1)
