"""Tests for the hub's end of live host tool calls (Source/cli_host_bridge.py)."""
from __future__ import annotations

import json
import os
import socket
import stat
import tempfile
import threading
import time
import unittest

import host_tools_mcp
from cli_host_bridge import HostBridgeError, HostCallBridge, SOCKET_PATH_LIMIT

CALLS = {"toolu_1": {"id": "toolu_1", "name": "exec_command", "input": {"cmd": "ls"}},
         "toolu_2": {"id": "toolu_2", "name": "get_goal", "input": {}}}


def served(name):
    """Served names are the host names here, except one alias."""
    return {"exec_command": "exec_command", "get_goal": "get_goal", "long_1234": "a_long_host_tool"}.get(name)


def call(identifier, name, arguments):
    return {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {
        "name": name, "arguments": arguments,
        **({"_meta": {host_tools_mcp.TOOL_USE_META: identifier, "progressToken": 7}} if identifier else {})}}


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp(prefix="bridge-test-")
        self.bridge = HostCallBridge(self.directory, served)
        self.addCleanup(self.bridge.close)
        self.results = {}
        self.threads = []

    def forward(self, key, request, endpoint=None):
        """What the CLI's MCP server does for one tools/call: blocks until answered."""
        def run():
            self.results[key] = host_tools_mcp.forward(request, endpoint or self.bridge.endpoint)
        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        self.threads.append(thread)
        return thread

    def settle(self, calls=CALLS, expect="ready", seconds=5):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            verdict = self.bridge.check(calls)
            if verdict != "waiting":
                break
            time.sleep(0.01)
        self.assertEqual(verdict, expect)

    def test_socket_is_owner_only_and_the_endpoint_names_it(self):
        mode = stat.S_IMODE(os.stat(self.bridge.path).st_mode)
        self.assertEqual(mode & 0o077, 0)
        self.assertEqual(self.bridge.endpoint, {"socket": self.bridge.path, "token": self.bridge.token})
        self.assertGreaterEqual(len(self.bridge.token), 32)

    def test_waiting_calls_are_confirmed_by_id_and_answered_by_delivery(self):
        self.forward("a", call("toolu_1", "exec_command", {"cmd": "ls"}))
        self.assertEqual(self.bridge.check(CALLS), "waiting")
        self.forward("b", call("toolu_2", "get_goal", {}))
        self.settle()
        result = {"content": [{"type": "text", "text": "a.txt"}], "isError": False}
        self.assertTrue(self.bridge.deliver("toolu_1", result))
        self.assertTrue(self.bridge.deliver("toolu_2", {"content": [], "isError": True}))
        for thread in self.threads:
            thread.join(5)
        self.assertEqual(self.results["a"], result)
        self.assertEqual(self.results["b"], {"content": [], "isError": True})
        # Each call is answered exactly once.
        self.assertFalse(self.bridge.deliver("toolu_1", result))

    def test_calls_without_their_id_pair_by_tool_and_arguments(self):
        self.forward("a", call(None, "get_goal", {}))
        self.forward("b", call(None, "exec_command", {"cmd": "ls"}))
        self.settle()
        self.assertTrue(self.bridge.deliver("toolu_2", {"content": [{"type": "text", "text": "goal"}]}))
        self.threads[0].join(5)
        self.assertEqual(self.results["a"]["content"][0]["text"], "goal")

    def test_a_call_the_stream_did_not_show_is_a_conflict(self):
        for request in (call("toolu_1", "exec_command", {"cmd": "rm -rf /"}),   # other arguments
                        call("toolu_1", "get_goal", {}),                         # other tool
                        call("toolu_9", "exec_command", {"cmd": "ls"}),          # unknown id
                        call(None, "exec_command", {"cmd": "pwd"})):             # unmatched, unnamed
            with self.subTest(request=request["params"]):
                bridge = HostCallBridge(tempfile.mkdtemp(prefix="bridge-test-"), served)
                try:
                    self.forward(request["params"]["name"], request, bridge.endpoint)
                    deadline = time.monotonic() + 5
                    verdict = "waiting"
                    while verdict == "waiting" and time.monotonic() < deadline:
                        verdict = bridge.check({"toolu_1": CALLS["toolu_1"]})
                        time.sleep(0.01)
                    self.assertEqual(verdict, "conflict")
                finally:
                    bridge.close()

    def test_numbers_compare_by_value_as_json_does(self):
        calls = {"toolu_1": {"id": "toolu_1", "name": "exec_command", "input": {"cmd": "ls", "timeout": 10}}}
        self.forward("a", call("toolu_1", "exec_command", {"cmd": "ls", "timeout": 10.0}))
        self.settle(calls)

    def test_a_wrong_token_or_malformed_call_is_never_accepted(self):
        bad = {"socket": self.bridge.path, "token": "not-the-token"}
        self.assertTrue(host_tools_mcp.forward(call("toolu_1", "exec_command", {"cmd": "ls"}), bad)["isError"])
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.connect(self.bridge.path)
            connection.sendall(json.dumps({"token": self.bridge.token, "name": 5, "arguments": {}}).encode() + b"\n")
            self.assertEqual(connection.recv(10), b"")
        self.assertEqual(self.bridge.check(CALLS), "waiting")

    def test_close_drops_waiting_calls_and_the_socket(self):
        self.forward("a", call("toolu_1", "exec_command", {"cmd": "ls"}))
        deadline = time.monotonic() + 5
        while not self.bridge._waiting and time.monotonic() < deadline:
            time.sleep(0.01)
        self.bridge.close()
        self.threads[0].join(5)
        self.assertTrue(self.results["a"]["isError"])
        self.assertEqual(self.results["a"]["content"][0]["text"], host_tools_mcp.NO_RESULT)
        self.assertFalse(os.path.exists(self.bridge.path))
        self.assertTrue(host_tools_mcp.forward(call("toolu_2", "get_goal", {}), self.bridge.endpoint)["isError"])

    def test_a_socket_path_past_the_platform_limit_is_refused(self):
        deep = os.path.join(self.directory, "d" * (SOCKET_PATH_LIMIT - len(self.directory)))
        os.mkdir(deep)
        with self.assertRaises(HostBridgeError):
            HostCallBridge(deep, served)


if __name__ == "__main__":
    unittest.main()
