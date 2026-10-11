"""Exercise AntiGravity's denied native handoff against actual lean Chat schemas."""
import json
import shlex
import unittest

import agy_cli_agent as adapter
from chat_tools import TOOL_DEFINITIONS
from cli_tool_call import ToolCallError
import test_cli_host_tools as fixtures


class LeanMappingTests(unittest.TestCase):
    handoff = fixtures.AgyNativeHandoffTests.handoff

    def test_shell_and_directory_calls_handoff_once_without_native_execution(self):
        cases = [
            ("run_command", {"CommandLine":"git status --short", "Cwd":"/workspace/it's a repo", "SafeToAutoRun":True},
             "cd " + shlex.quote("/workspace/it's a repo") + " && git status --short"),
            ("list_dir", {"DirectoryPath":"/workspace/a; $(literal)"}, "ls -la -- " + shlex.quote("/workspace/a; $(literal)")),
        ]
        for native, parameters, expected in cases:
            with self.subTest(native=native):
                events, sessions = self.handoff(native, parameters, TOOL_DEFINITIONS)
                self.assertEqual([e["type"] for e in events], ["tool_call", "message_stop"])
                self.assertEqual(events[0]["name"], "run_shell")
                self.assertEqual(events[0]["input"], {"command":expected})
                self.assertEqual(len(sessions), 1)
                self.assertTrue(sessions[0].confirmation_sent)

    def test_ranged_file_read_uses_read_tool_even_in_read_only_lane(self):
        offered = [tool for tool in TOOL_DEFINITIONS if tool["name"] in {"read_file", "search_files"}]
        events, sessions = self.handoff("view_file", {"AbsolutePath":"/workspace/README.md", "StartLine":3, "EndLine":8}, offered)
        self.assertEqual(events[0]["name"], "read_file")
        self.assertEqual(events[0]["input"], {"path":"/workspace/README.md", "offset":3, "limit":6})
        self.assertEqual(len(sessions), 1)

    def test_unoffered_shell_stays_rejected_and_names_native_tool(self):
        offered = [tool for tool in TOOL_DEFINITIONS if tool["name"] == "read_file"]
        with self.assertRaisesRegex(ToolCallError, "run_command.*no supported host tool mapping"):
            adapter._host_reply_for_native("run_command", {"CommandLine":"touch forbidden"}, offered)
        events, sessions = self.handoff("run_command", {"CommandLine":"touch forbidden"}, offered)
        self.assertEqual([e["type"] for e in events], ["error"])
        self.assertEqual(len(sessions), 2)  # Existing single format-correction budget.

    def test_required_tool_and_receipt_guards_remain_in_force(self):
        for options in ({"tool_choice":{"type":"tool", "name":"read_file"}}, {"receipt":False, "terminal":"DONE"}, {"stale_receipt":True}):
            with self.subTest(options=options):
                events, _ = self.handoff("run_command", {"CommandLine":"touch never-replay"}, TOOL_DEFINITIONS, **options)
                self.assertEqual([e["type"] for e in events], ["error"])

    def test_existing_minimal_read_schema_does_not_receive_range_fields(self):
        tools = [{"name":"read_file", "input_schema":{"type":"object", "properties":{"path":{"type":"string"}}, "additionalProperties":False}},
                 next(tool for tool in TOOL_DEFINITIONS if tool["name"] == "run_shell")]
        reply = json.loads(adapter._host_reply_for_native("view_file", {"AbsolutePath":"/workspace/a b", "StartLine":2}, tools))
        call = reply["tool_calls"][0]
        self.assertEqual(call["name"], "run_shell")
        self.assertEqual(shlex.split(json.loads(call["arguments"])["command"]), ["sed", "-n", "2,$p", "/workspace/a b"])


if __name__ == "__main__": unittest.main()
