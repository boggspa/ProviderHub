import copy
import json
import unittest

from bridge_core import BridgeError
from responses_tools import (flatten_tools, input_names, normalize_custom_calls, output_names, register,
                             restore_custom_call, tool_name)


class ResponsesToolTests(unittest.TestCase):
    def test_namespaces_are_flattened_and_names_restore_without_call_id_changes(self):
        source = [{"type": "function", "name": "read", "parameters": {"type": "object"}},
                  {"type": "namespace", "name": "workspace", "description": "Workspace operations",
                   "tools": [{"type": "function", "name": "read", "parameters": {"type": "object"}}]}]
        before = copy.deepcopy(source)
        flat, mapping = flatten_tools(source)
        self.assertEqual(source, before)
        self.assertEqual(flat[0]["name"], "read")
        self.assertNotEqual(flat[1]["name"], "read")
        self.assertLessEqual(len(flat[1]["name"]), 64)
        item = {"type": "function_call", "name": flat[1]["name"], "id": "fc", "call_id": "call", "arguments": "{}"}
        output_names(item, mapping)
        self.assertEqual(item["name"], "read")
        self.assertEqual(item["namespace"], "workspace")
        self.assertEqual(item["call_id"], "call")
        # Returning that exact history to the provider restores the same name.
        input_names([item], mapping)
        self.assertNotIn("namespace", item)
        self.assertEqual(item["name"], flat[1]["name"])

    def test_invalid_namespaces_and_non_function_children_fail_explicitly(self):
        for tool in ({"type": "namespace", "tools": [{"type": "function", "name": "read"}]},
                     {"type": "namespace", "name": "ns", "tools": [{"type": "custom", "name": "patch"}]},
                     {"type": "namespace", "name": "ns", "tools": None},
                     {"type": "web_search"}):
            with self.assertRaises(BridgeError):
                flatten_tools([tool])

    def test_flat_names_cannot_collide_with_encoded_namespace_names(self):
        encoded = tool_name("ns", "run")
        with self.assertRaises(BridgeError):
            flatten_tools([{"type": "function", "name": encoded},
                           {"type": "namespace", "name": "ns", "tools": [{"type": "function", "name": "run"}]}])

    def test_long_or_nonstandard_names_are_stable_and_reversible(self):
        name = "mcp::" + "long-operation-" * 10
        flat, mapping = flatten_tools([{"type": "function", "name": name}])
        encoded = flat[0]["name"]
        self.assertLessEqual(len(encoded), 64)
        self.assertEqual(encoded, tool_name(None, name))
        item = {"type": "function_call", "name": encoded}
        self.assertEqual(output_names(item, mapping)["name"], name)


class CustomApplyPatchAdapterTests(unittest.TestCase):
    PATCH = "*** Begin Patch\n*** Update File: a.txt\n@@\n-old\n+new\n*** End Patch"

    def test_custom_apply_patch_projects_to_a_patch_string_function(self):
        source = [{"type": "custom", "name": "apply_patch",
                   "description": "Do not wrap the patch in JSON.",
                   "format": {"type": "grammar", "syntax": "lark", "definition": "start: patch"}}]
        flat, mapping = flatten_tools(copy.deepcopy(source))
        self.assertEqual(flat[0]["type"], "function")
        self.assertEqual(flat[0]["name"], "apply_patch")
        # The freeform "do not wrap in JSON" instruction must not reach the
        # provider side, which needs the opposite discipline.
        self.assertNotIn("Do not wrap", flat[0]["description"])
        self.assertIn("'patch'", flat[0]["description"])
        # The grammar Codex sends cannot be forwarded as a JSON function, so
        # the description restates the format the model must produce.
        self.assertIn("*** Begin Patch", flat[0]["description"])
        self.assertIn("*** Update File:", flat[0]["description"])
        self.assertIn("SEARCH/REPLACE", flat[0]["description"])
        self.assertEqual(flat[0]["parameters"]["required"], ["patch"])
        self.assertEqual(flat[0]["parameters"]["properties"]["patch"]["type"], "string")
        self.assertEqual(mapping["apply_patch"]["custom"], "apply_patch")

    def test_other_custom_tools_still_need_a_separate_adapter(self):
        for tool in ({"type": "custom", "name": "other_tool"},
                     {"type": "custom"}):
            with self.assertRaises(BridgeError):
                flatten_tools([tool])

    def test_custom_history_normalizes_to_function_items(self):
        items = [{"type": "custom_tool_call", "call_id": "call-1", "name": "apply_patch", "input": self.PATCH},
                 {"type": "custom_tool_call_output", "call_id": "call-1", "output": "applied"}]
        mapping = {}
        normalize_custom_calls(items, mapping)
        call, output = items
        self.assertEqual(call["type"], "function_call")
        self.assertEqual(json.loads(call["arguments"]), {"patch": self.PATCH})
        self.assertEqual(output["type"], "function_call_output")
        self.assertEqual(output["output"], "applied")
        # Returning history re-registers without clobbering the egress marker.
        input_names(items, mapping)
        self.assertEqual(mapping["apply_patch"]["custom"], "apply_patch")
        with self.assertRaises(BridgeError):
            normalize_custom_calls([{"type": "custom_tool_call", "name": "other_tool"}], {})

    def test_provider_calls_restore_to_custom_tool_call(self):
        _, mapping = flatten_tools([{"type": "custom", "name": "apply_patch"}])
        item = {"type": "function_call", "id": "fc-1", "call_id": "call-1", "name": "apply_patch",
                "arguments": json.dumps({"patch": self.PATCH}), "status": "completed"}
        self.assertTrue(restore_custom_call(item, mapping))
        self.assertEqual(item["type"], "custom_tool_call")
        self.assertEqual(item["input"], self.PATCH)
        self.assertNotIn("arguments", item)
        self.assertEqual(item["call_id"], "call-1")
        # Raw patch text instead of JSON still yields a usable patch.
        raw = {"type": "function_call", "call_id": "call-2", "name": "apply_patch", "arguments": self.PATCH}
        self.assertTrue(restore_custom_call(raw, mapping))
        self.assertEqual(raw["input"], self.PATCH)
        # Unmapped tools keep the plain function path.
        plain = {"type": "function_call", "call_id": "call-3", "name": "read", "arguments": "{}"}
        self.assertFalse(restore_custom_call(plain, mapping))
        self.assertEqual(plain["type"], "function_call")

    def test_plain_registration_keeps_a_custom_marker(self):
        _, mapping = flatten_tools([{"type": "custom", "name": "apply_patch"}])
        self.assertEqual(register(mapping, None, "apply_patch"), "apply_patch")
        self.assertEqual(mapping["apply_patch"]["custom"], "apply_patch")


if __name__ == "__main__":
    unittest.main()
