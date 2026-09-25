"""Tests for the stdio MCP server that lists host tools (Source/host_tools_mcp.py)."""
from __future__ import annotations

import io
import json
import queue
import signal
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import cli_host_mcp
import host_tools_mcp
from cli_host_bridge import HostCallBridge

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


class _Pipe:
    """A blocking line source standing in for the server's stdin."""

    def __init__(self):
        self.lines = queue.Queue()

    def put(self, message):
        self.lines.put(json.dumps(message) + "\n")

    def close(self):
        self.lines.put(None)

    def __iter__(self):
        while True:
            line = self.lines.get()
            if line is None:
                return
            yield line


class _Stdout:
    def __init__(self):
        self.replies = queue.Queue()

    def write(self, text):
        for line in text.splitlines():
            self.replies.put(json.loads(line))

    def flush(self):
        pass


class BridgeModeTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp(prefix="mcp-bridge-test-")
        self.bridge = HostCallBridge(self.directory, lambda name: name)
        self.addCleanup(self.bridge.close)

    def test_load_reads_the_bridge_file_and_passes_hints_through(self):
        toolset = cli_host_mcp.HostToolset(TOOLS, prefix="mcp__host__")
        tools, bridge = host_tools_mcp.load(str(toolset.write(self.directory, bridge=self.bridge)))
        self.assertEqual(bridge, self.bridge.endpoint)
        self.assertEqual([tool["name"] for tool in tools], ["exec_command", "get_goal"])
        self.assertEqual(tools[0]["annotations"], {"readOnlyHint": True})
        self.assertEqual(tools[0]["_meta"], {"anthropic/maxResultSizeChars": cli_host_mcp.LIVE_RESULT_CHARS})
        self.assertEqual(host_tools_mcp.load_tools(str(Path(self.directory) / "host-tools.json")), tools)

    def test_calls_wait_concurrently_for_the_hub_and_a_cancelled_one_gets_no_reply(self):
        stdin, stdout, lost = _Pipe(), _Stdout(), mock.Mock()
        server = threading.Thread(target=host_tools_mcp.serve, args=(stdin, stdout, LISTED, self.bridge.endpoint),
                                  kwargs={"on_lost": lost}, daemon=True)
        server.start()
        calls = {"toolu_a": {"id": "toolu_a", "name": "exec_command", "input": {"cmd": "ls"}},
                 "toolu_b": {"id": "toolu_b", "name": "get_goal", "input": {}}}
        stdin.put(rpc(1, "tools/call", {"name": "exec_command", "arguments": {"cmd": "ls"},
                                        "_meta": {host_tools_mcp.TOOL_USE_META: "toolu_a"}}))
        stdin.put(rpc(2, "tools/call", {"name": "get_goal", "arguments": {},
                                        "_meta": {host_tools_mcp.TOOL_USE_META: "toolu_b"}}))
        stdin.put(rpc(3, "ping"))
        # Both calls wait in the hub at once, and the server still answers.
        self.assertEqual(stdout.replies.get(timeout=5), {"jsonrpc": "2.0", "id": 3, "result": {}})
        deadline = time.monotonic() + 5
        while self.bridge.check(calls) == "waiting" and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(self.bridge.check(calls), "ready")
        stdin.put(rpc(None, "notifications/cancelled", {"requestId": 2}))
        self.assertTrue(self.bridge.deliver("toolu_a", {"content": [{"type": "text", "text": "a.txt"}]}))
        reply = stdout.replies.get(timeout=5)
        self.assertEqual(reply, {"jsonrpc": "2.0", "id": 1, "result": {
            "content": [{"type": "text", "text": "a.txt"}]}})
        stdin.close()
        server.join(5)
        time.sleep(0.2)
        self.assertTrue(stdout.replies.empty())
        lost.assert_not_called()

    def test_a_call_the_hub_drops_ends_the_session_before_its_error_reply(self):
        stdin, stdout, lost = _Pipe(), _Stdout(), mock.Mock()
        threading.Thread(target=host_tools_mcp.serve, args=(stdin, stdout, LISTED, self.bridge.endpoint),
                         kwargs={"on_lost": lost}, daemon=True).start()
        stdin.put(rpc(1, "tools/call", {"name": "get_goal", "arguments": {},
                                        "_meta": {host_tools_mcp.TOOL_USE_META: "toolu_a"}}))
        deadline = time.monotonic() + 5
        while not self.bridge._waiting and time.monotonic() < deadline:
            time.sleep(0.01)
        self.bridge.close()
        reply = stdout.replies.get(timeout=5)
        self.assertEqual(reply["result"]["content"][0]["text"], host_tools_mcp.NO_RESULT)
        lost.assert_called_once_with()
        stdin.close()

    def test_the_launch_command_forwards_a_call_to_the_bridge(self):
        toolset = cli_host_mcp.HostToolset(TOOLS, prefix="mcp__host__")
        command = cli_host_mcp.server_command(toolset.write(self.directory, bridge=self.bridge))
        child = self.launch(command)
        child.stdin.write(json.dumps(rpc(1, "tools/call", {
            "name": "exec_command", "arguments": {"cmd": "pwd"},
            "_meta": {host_tools_mcp.TOOL_USE_META: "toolu_p"}})) + "\n")
        child.stdin.flush()
        calls = {"toolu_p": {"id": "toolu_p", "name": "exec_command", "input": {"cmd": "pwd"}}}
        deadline = time.monotonic() + 20
        while self.bridge.check(calls) == "waiting" and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue(self.bridge.deliver("toolu_p", {"content": [{"type": "text", "text": "/work"}],
                                                        "isError": False}))
        reply = json.loads(child.stdout.readline())
        self.assertEqual(reply["result"], {"content": [{"type": "text", "text": "/work"}], "isError": False})
        child.stdin.close()
        self.assertEqual(child.wait(20), 0)

    def launch(self, command):
        """The server under a shell standing in for the CLI, which is what a lost hub signals."""
        # A child inherits an ignored SIGTERM, and codex_accent's helper tests
        # leave it ignored in this process; the CLI never runs that way.
        child = subprocess.Popen(["/bin/sh", "-c", '"$@"; exit $?', "sh", *command], stdin=subprocess.PIPE,
                                 stdout=subprocess.PIPE, text=True, cwd=self.directory,
                                 preexec_fn=lambda: signal.signal(signal.SIGTERM, signal.SIG_DFL))
        self.addCleanup(child.stdout.close)
        self.addCleanup(child.stdin.close)
        self.addCleanup(child.kill)
        return child

    def test_a_server_whose_hub_vanishes_ends_the_cli_that_launched_it(self):
        toolset = cli_host_mcp.HostToolset(TOOLS, prefix="mcp__host__")
        cli = self.launch(cli_host_mcp.server_command(toolset.write(self.directory, bridge=self.bridge)))
        cli.stdin.write(json.dumps(rpc(1, "tools/call", {"name": "get_goal", "arguments": {}})) + "\n")
        cli.stdin.flush()
        deadline = time.monotonic() + 20
        while not self.bridge._waiting and time.monotonic() < deadline:
            time.sleep(0.02)
        self.bridge.close()
        self.assertEqual(cli.wait(20), -signal.SIGTERM)


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
