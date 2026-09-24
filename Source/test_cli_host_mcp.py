"""Tests for serving host tools to CLI routes as MCP tools (Source/cli_host_mcp.py)."""
from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

from cli_host_mcp import NAME_LIMIT, SERVER_SCRIPT, HostToolset, server_command, tools_note
from cli_tool_call import HOST_EXECUTION_NOTE

PREFIX = "mcp__host__"


def tool(name, description="does a thing"):
    return {"name": name, "description": description, "input_schema": {"type": "object", "properties": {}}}


class HostToolsetTests(unittest.TestCase):
    def test_names_map_both_ways(self):
        toolset = HostToolset([tool("exec_command"), tool("view_image")], prefix=PREFIX)
        self.assertEqual(toolset.model_name("exec_command"), "mcp__host__exec_command")
        self.assertEqual(toolset.host_name("mcp__host__view_image"), "view_image")
        self.assertEqual(toolset.model_names, {"mcp__host__exec_command", "mcp__host__view_image"})
        for foreign in ("exec_command", "mcp__other__exec_command", "WebSearch", None, 7):
            self.assertIsNone(toolset.host_name(foreign))
        self.assertTrue(toolset)
        self.assertFalse(HostToolset([], prefix=PREFIX))

    def test_long_names_get_stable_aliases_within_the_ceiling(self):
        long_name = "ph_mcp__codex_apps__openai_platform__create_key_" + "a1b2c3d4e5f60718"
        near = long_name[:NAME_LIMIT - len(PREFIX)]
        toolset = HostToolset([tool(long_name, "Create a key."), tool(near)], prefix=PREFIX)
        alias = toolset.model_name(long_name)
        self.assertLessEqual(len(alias), NAME_LIMIT)
        self.assertNotEqual(alias, toolset.model_name(near))
        self.assertEqual(toolset.host_name(alias), long_name)
        self.assertEqual(alias, HostToolset([tool(long_name)], prefix=PREFIX).model_name(long_name))
        served = {entry["name"]: entry for entry in toolset.served_tools()}
        self.assertTrue(served[alias[len(PREFIX):]]["description"].startswith(f"(Host tool `{long_name}`.)"))
        self.assertEqual(served[near]["description"], "does a thing")

    def test_prefix_must_leave_room_for_names(self):
        with self.assertRaises(ValueError):
            HostToolset([tool("x")], prefix="p" * 50)

    def test_write_makes_an_owner_only_tools_file(self):
        with tempfile.TemporaryDirectory() as directory:
            toolset = HostToolset([tool("exec_command", "Run it.")], prefix=PREFIX)
            path = toolset.write(directory)
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
            self.assertEqual(json.loads(path.read_text()), [
                {"name": "exec_command", "description": "Run it.",
                 "input_schema": {"type": "object", "properties": {}}}])
            with self.assertRaises(FileExistsError):
                toolset.write(directory)

    def test_server_command_keeps_the_signed_interpreter_clean(self):
        command = server_command("/tmp/tools.json")
        self.assertEqual(command, [sys.executable, "-I", "-B", str(SERVER_SCRIPT), "/tmp/tools.json"])
        self.assertTrue(Path(SERVER_SCRIPT).is_file())


class ToolsNoteTests(unittest.TestCase):
    def test_note_explains_names_and_keeps_the_execution_rules(self):
        toolset = HostToolset([tool("exec_command"), tool("apply_patch")], prefix=PREFIX)
        note = tools_note(toolset)
        self.assertTrue(note.startswith(HOST_EXECUTION_NOTE))
        self.assertIn("`mcp__host__<tool>`", note)
        self.assertIn("such as `exec_command`, call it as `mcp__host__exec_command`", note)
        self.assertNotIn("must call", note)

    def test_note_carries_the_required_tool_choice(self):
        toolset = HostToolset([tool("exec_command"), tool("apply_patch")], prefix=PREFIX)
        self.assertIn("You must call at least one host tool", tools_note(toolset, {"type": "any"}))
        self.assertIn("You must call `mcp__host__apply_patch`",
                      tools_note(toolset, {"type": "tool", "name": "apply_patch"}))
        self.assertNotIn("must call", tools_note(toolset, {"type": "tool", "name": "missing"}))


if __name__ == "__main__":
    unittest.main()
