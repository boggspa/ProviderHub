"""Stdio MCP server that lists a desktop harness's tools to a CLI-backed route.

A CLI route never executes anything itself: the desktop harness (Codex, Claude
Desktop) owns the tool loop. This server lets the vendor CLI offer the
harness's tools to its model as genuine native tools rather than as a text
protocol in the prompt, so every model calls them the way it was trained to.
The hub reads each call off the CLI's output stream and hands it to the
harness; the CLI's own permission mode refuses to run it here, and even if it
did, ``tools/call`` below executes nothing.

Launched by the hub as ``python -I -B host_tools_mcp.py <tools.json>``: the
flags keep the interpreter inside the signed app from writing bytecode and
keep the CLI's environment out of it. Standard library only.
"""
from __future__ import annotations

import json
import sys

#: Answered when the client asks for a version this server does not know.
PROTOCOL_VERSION = "2025-06-18"
KNOWN_VERSIONS = frozenset({"2024-11-05", "2025-03-26", "2025-06-18"})
SERVER_INFO = {"name": "provider-hub-host-tools", "version": "1"}
NOT_HERE = ("This call belongs to the host application, which runs it. Provider Hub hands it over "
            "from your tool call; this server never runs it.")


def load_tools(path: str) -> list[dict]:
    """The MCP tool list from the hub's tools file: [{name, description, inputSchema}]."""
    with open(path, encoding="utf-8") as handle:
        entries = json.load(handle)
    return [{"name": entry["name"], "description": entry.get("description") or "",
             "inputSchema": entry["input_schema"]} for entry in entries]


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


def serve(stdin, stdout, tools: list[dict]) -> None:
    """Answer newline-delimited JSON-RPC until the client closes stdin."""
    for line in stdin:
        try:
            request = json.loads(line)
        except ValueError:
            continue
        if not isinstance(request, dict):
            continue
        response = respond(request, tools)
        if response is not None:
            stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
            stdout.flush()


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        sys.stderr.write("usage: host_tools_mcp.py <tools.json>\n")
        return 2
    serve(sys.stdin, sys.stdout, load_tools(argv[1]))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
