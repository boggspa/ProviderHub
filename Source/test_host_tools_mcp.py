"""Tests for the stdio MCP server that lists host tools (Source/host_tools_mcp.py)."""
from __future__ import annotations

import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

import cli_host_mcp
import host_tools_mcp

TOOLS = [
    {"name": "exec_command", "description": "Run a command.",
     "input_schema": {"type": "object", "properties": {"cmd": {"type": "string"}}, "required": ["cmd"]}},
    {"name": "get_goal", "description": "", "input_schema": {"type": "object", "properties": {}}},
]
LISTED = [{"name": tool["name"], "description": tool["description"], "inputSchema": tool["input_schema"]}
          for tool in TOOLS]


def rpc(identifier, method, params=None):
    message = {"jsonrpc": "2.0", "method": method, "params": params or {}}
    if identifier is not None:
        message["id"] = identifier
    return message


class RespondTests(unittest.TestCase):
    def test_initialize_echoes_a_known_protocol_version_and_offers_tools(self):
        result = host_tools_mcp.respond(rpc(1, "initialize", {"protocolVersion": "2025-03-26"}), LISTED)["result"]
        self.assertEqual(result["protocolVersion"], "2025-03-26")
        self.assertEqual(result["capabilities"], {"tools": {"listChanged": False}})
        unknown = host_tools_mcp.respond(rpc(1, "initialize", {"protocolVersion": "2099-01-01"}), LISTED)
        self.assertEqual(unknown["result"]["protocolVersion"], host_tools_mcp.PROTOCOL_VERSION)

    def test_tools_are_listed_and_never_executed(self):
        self.assertEqual(host_tools_mcp.respond(rpc(2, "tools/list"), LISTED)["result"], {"tools": LISTED})
        called = host_tools_mcp.respond(rpc(3, "tools/call", {"name": "exec_command",
                                                              "arguments": {"cmd": "rm -rf /tmp/x"}}), LISTED)
        self.assertIs(called["result"]["isError"], True)
        self.assertIn("host application", called["result"]["content"][0]["text"])

    def test_ping_notifications_and_unknown_methods(self):
        self.assertEqual(host_tools_mcp.respond(rpc(4, "ping"), LISTED)["result"], {})
        self.assertIsNone(host_tools_mcp.respond(rpc(None, "notifications/initialized"), LISTED))
        self.assertEqual(host_tools_mcp.respond(rpc(5, "resources/list"), LISTED)["error"]["code"], -32601)

    def test_serve_skips_garbage_and_answers_in_order(self):
        stdin = io.StringIO("not json\n[1, 2]\n" + json.dumps(rpc(1, "ping")) + "\n"
                            + json.dumps(rpc(None, "notifications/initialized")) + "\n"
                            + json.dumps(rpc(2, "tools/list")) + "\n")
        stdout = io.StringIO()
        host_tools_mcp.serve(stdin, stdout, LISTED)
        replies = [json.loads(line) for line in stdout.getvalue().splitlines()]
        self.assertEqual([reply["id"] for reply in replies], [1, 2])


class ProcessTests(unittest.TestCase):
    def test_the_hub_launch_command_serves_a_tools_file_and_exits_on_eof(self):
        with tempfile.TemporaryDirectory() as directory:
            toolset = cli_host_mcp.HostToolset(TOOLS, prefix="mcp__host__")
            command = cli_host_mcp.server_command(toolset.write(directory))
            requests = [rpc(1, "initialize", {"protocolVersion": "2025-06-18"}),
                        rpc(None, "notifications/initialized"), rpc(2, "tools/list"),
                        rpc(3, "tools/call", {"name": "get_goal", "arguments": {}})]
            done = subprocess.run(command, input="".join(json.dumps(r) + "\n" for r in requests),
                                  capture_output=True, text=True, timeout=30, cwd=directory)
            self.assertEqual(done.returncode, 0, done.stderr)
            replies = [json.loads(line) for line in done.stdout.splitlines()]
            self.assertEqual([reply["id"] for reply in replies], [1, 2, 3])
            self.assertEqual(replies[1]["result"]["tools"], LISTED)
            self.assertIs(replies[2]["result"]["isError"], True)
            self.assertEqual(sorted(path.name for path in Path(directory).iterdir()), ["host-tools.json"])

    def test_usage_error_without_a_tools_file(self):
        done = subprocess.run(cli_host_mcp.server_command("")[:-1], capture_output=True, text=True, timeout=30)
        self.assertEqual(done.returncode, 2)
        self.assertIn("usage", done.stderr)


if __name__ == "__main__":
    unittest.main()
