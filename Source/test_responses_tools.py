import copy
import unittest

from bridge_core import BridgeError
from responses_tools import flatten_tools, input_names, output_names, tool_name


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


if __name__ == "__main__":
    unittest.main()
