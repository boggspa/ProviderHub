"""Stdio MCP server that serves a desktop harness's tools to a CLI-backed route.

A CLI route never executes anything itself: the desktop harness (Codex, Claude
Desktop) owns the tool loop. This server lets the vendor CLI offer the
harness's tools to its model as genuine native tools rather than as a text
protocol in the prompt, so every model calls them the way it was trained to.

It runs in one of two modes, chosen by the tools file the hub writes:

* List-only (a bare tool list). The hub reads each call off the CLI's output
  stream and hands it to the harness; the CLI's own permission mode refuses to
  run it here, and even if it did, ``tools/call`` below executes nothing.
* Bridge (``{"tools": [...], "bridge": {"socket", "token"}}``). The CLI is
  allowed to call the tools, and each ``tools/call`` is forwarded over the
  hub's private socket (cli_host_bridge) and answered only once the harness
  has run the call and the hub delivers its result. The CLI waits inside the
  call, so the same process carries on with its reasoning intact. Nothing runs
  here in this mode either. If the hub goes away without answering (it quit or
  crashed mid-call), the server ends the CLI rather than let its model carry on
  unsupervised, retrying calls nobody will run.

Launched by the hub as ``python -I -B host_tools_mcp.py <tools.json>``: the
flags keep the interpreter inside the signed app from writing bytecode and
keep the CLI's environment out of it. Standard library only.
"""
from __future__ import annotations

import json
import os
import signal
import socket
import sys
import threading

#: Answered when the client asks for a version this server does not know.
PROTOCOL_VERSION = "2025-06-18"
KNOWN_VERSIONS = frozenset({"2024-11-05", "2025-03-26", "2025-06-18"})
SERVER_INFO = {"name": "provider-hub-host-tools", "version": "1"}
NOT_HERE = ("This call belongs to the host application, which runs it. Provider Hub hands it over "
            "from your tool call; this server never runs it.")
NO_RESULT = ("The host application did not return a result for this call; Provider Hub could not "
             "deliver one. Do not assume the call ran.")
#: Where Claude Code (2.1.280) puts the tool_use id of the call it forwards.
TOOL_USE_META = "claudecode/toolUseId"


def load(path: str) -> tuple[list[dict], dict | None]:
    """The MCP tool list and, in bridge mode, the hub's bridge endpoint."""
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    entries, bridge = (data, None) if isinstance(data, list) else (data["tools"], data.get("bridge"))
    tools = []
    for entry in entries:
        tool = {"name": entry["name"], "description": entry.get("description") or "",
                "inputSchema": entry["input_schema"]}
        for key in ("annotations", "_meta"):
            if isinstance(entry.get(key), dict):
                tool[key] = entry[key]
        tools.append(tool)
    return tools, bridge if isinstance(bridge, dict) else None


def load_tools(path: str) -> list[dict]:
    """The MCP tool list from the hub's tools file: [{name, description, inputSchema, ...}]."""
    return load(path)[0]


def respond(request: dict, tools: list[dict]):
    """The JSON-RPC response for one request, or None for a notification."""
    identifier = request.get("id")
    method = request.get("method")
    if identifier is None:
        return None
    if method == "initialize":
        params = request.get("params") if isinstance(request.get("params"), dict) else {}
        asked = params.get("protocolVersion")
        result = {"protocolVersion": asked if asked in KNOWN_VERSIONS else PROTOCOL_VERSION,
                  "capabilities": {"tools": {"listChanged": False}}, "serverInfo": SERVER_INFO}
    elif method == "tools/list":
        result = {"tools": tools}
    elif method == "tools/call":
        result = {"isError": True, "content": [{"type": "text", "text": NOT_HERE}]}
    elif method == "ping":
        result = {}
    else:
        return {"jsonrpc": "2.0", "id": identifier,
                "error": {"code": -32601, "message": f"Method not found: {method}"}}
    return {"jsonrpc": "2.0", "id": identifier, "result": result}


def forward(request: dict, bridge: dict, connections: dict | None = None) -> dict:
    """The host's MCP result for one ``tools/call``: blocks until the hub answers.

    Any failure - an unreachable hub, a closed connection, an unreadable reply -
    is an error result saying the call may not have run, never a guess.
    """
    result = exchange(request, bridge, connections)
    return result if result is not None else {"isError": True, "content": [{"type": "text", "text": NO_RESULT}]}


def exchange(request: dict, bridge: dict, connections: dict | None = None) -> dict | None:
    """One ``tools/call`` sent to the hub and its MCP result, or None when none came back."""
    params = request.get("params") if isinstance(request.get("params"), dict) else {}
    meta = params.get("_meta") if isinstance(params.get("_meta"), dict) else {}
    identifier = meta.get(TOOL_USE_META)
    arguments = params.get("arguments")
    message = {"token": bridge.get("token"), "tool_use_id": identifier if isinstance(identifier, str) else None,
               "name": params.get("name"), "arguments": arguments if isinstance(arguments, dict) else {}}
    result = None
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    if connections is not None:
        connections[request.get("id")] = connection
    try:
        connection.connect(bridge["socket"])
        connection.sendall(json.dumps(message, ensure_ascii=False).encode("utf-8") + b"\n")
        with connection.makefile("rb") as reader:
            line = reader.readline()
        result = json.loads(line) if line else None
    except (OSError, ValueError, KeyError, TypeError):
        result = None
    finally:
        if connections is not None:
            connections.pop(request.get("id"), None)
        connection.close()
    if not isinstance(result, dict) or not isinstance(result.get("content"), list):
        return None
    return result


def end_session() -> None:
    """SIGTERM the CLI that launched this server: its hub is gone, so its session is over."""
    parent = os.getppid()
    if parent > 1:
        try:
            os.kill(parent, signal.SIGTERM)
        except OSError:
            pass


def serve(stdin, stdout, tools: list[dict], bridge: dict | None = None, *, on_lost=end_session) -> None:
    """Answer newline-delimited JSON-RPC until the client closes stdin.

    In bridge mode each ``tools/call`` waits on its own thread, so the client
    can have several host calls outstanding at once; a ``notifications/cancelled``
    drops the matching call's connection and its reply. A call the hub drops
    without a result calls ``on_lost`` before its error reply is written.
    """
    lock = threading.Lock()
    connections: dict = {}
    cancelled: set = set()

    def write(message):
        with lock:
            stdout.write(json.dumps(message, ensure_ascii=False) + "\n")
            stdout.flush()

    def call(request):
        result = exchange(request, bridge, connections)
        if request["id"] in cancelled:
            return
        if result is None:
            on_lost()
            result = {"isError": True, "content": [{"type": "text", "text": NO_RESULT}]}
        write({"jsonrpc": "2.0", "id": request["id"], "result": result})

    for line in stdin:
        try:
            request = json.loads(line)
        except ValueError:
            continue
        if not isinstance(request, dict):
            continue
        method = request.get("method")
        if bridge is not None and method == "tools/call" and request.get("id") is not None:
            threading.Thread(target=call, args=(request,), daemon=True).start()
            continue
        if bridge is not None and method == "notifications/cancelled":
            params = request.get("params") if isinstance(request.get("params"), dict) else {}
            target = params.get("requestId")
            cancelled.add(target)
            connection = connections.get(target)
            if connection is not None:
                try:
                    connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            continue
        response = respond(request, tools)
        if response is not None:
            write(response)


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        sys.stderr.write("usage: host_tools_mcp.py <tools.json>\n")
        return 2
    tools, bridge = load(argv[1])
    serve(sys.stdin, sys.stdout, tools, bridge)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
