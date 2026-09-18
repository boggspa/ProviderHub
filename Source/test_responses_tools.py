import copy
import json
import unittest

from bridge_core import BridgeError
from responses_tools import (describe_tool, flatten_tools, input_names, normalize_custom_calls, output_names, register, repair_apply_patch, extract_patch,
                             qualified_name, restore_custom_call, split_hosted_search, tool_name)


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

    def test_a_hosted_search_request_is_lifted_out_instead_of_failing_the_turn(self):
        """web_search is OpenAI's hosted tool and nothing here serves OpenAI,
        so it cannot travel as it stands - but refusing the array outright
        fails a whole thread over a tool the route may be able to answer its
        own way. It comes out first and the route decides. user_location stays
        behind: no provider here takes an equivalent, and it is the one field
        that describes the person rather than the search."""
        tools = [{"type": "function", "name": "read", "parameters": {"type": "object"}},
                 {"type": "web_search", "search_context_size": "high",
                  "filters": {"allowed_domains": ["docs.python.org", 7, ""]},
                  "user_location": {"type": "approximate", "city": "Edinburgh"}}]
        kept, search = split_hosted_search(tools)
        self.assertEqual([tool["name"] for tool in kept], ["read"])
        self.assertEqual(search, {"context_size": "high", "allowed_domains": ["docs.python.org"]})
        self.assertNotIn("Edinburgh", json.dumps(search))
        # What is left flattens exactly as it would have without the request.
        self.assertEqual(flatten_tools(kept)[0][0]["name"], "read")
        # The preview and dated spellings name the same hosted tool.
        for kind in ("web_search_preview", "web_search_2025_08_26"):
            self.assertIsNotNone(split_hosted_search([{"type": kind}])[1])
        # An unrecognised context size is dropped, not forwarded verbatim.
        self.assertEqual(split_hosted_search([{"type": "web_search", "search_context_size": "enormous"}])[1],
                         {"context_size": None, "allowed_domains": []})
        # A turn that asked for no search is handed back untouched.
        self.assertEqual(split_hosted_search(tools[:1]), (tools[:1], None))
        # Nested is not lifted: Codex does not send it there, and guessing
        # would reinterpret the turn rather than translate it.
        with self.assertRaises(BridgeError):
            flatten_tools(split_hosted_search([{"type": "namespace", "name": "browser",
                                                "tools": [{"type": "web_search"}]}])[0])

    def test_a_refused_tool_is_named_with_its_type_and_never_its_schema(self):
        """The refusal has to identify the tool: a desktop that ships one
        unsupported hosted tool otherwise fails every thread on every
        provider with nothing to point at. Only identity travels, so a
        refusal cannot leak what the tool would have been asked to do."""
        with self.assertRaises(BridgeError) as raised:
            flatten_tools([{"type": "local_shell", "name": "shell",
                            "description": "run SECRET commands", "parameters": {"secret": True}}])
        message = str(raised.exception)
        self.assertIn("tool 'shell' of type 'local_shell'", message)
        self.assertNotIn("SECRET", message)
        self.assertNotIn("parameters", message)
        with self.assertRaises(BridgeError) as nested:
            flatten_tools([{"type": "namespace", "name": "browser",
                            "tools": [{"type": "computer_use", "name": "click"}]}])
        self.assertIn("tool 'click' of type 'computer_use' in namespace 'browser'", str(nested.exception))
        with self.assertRaises(BridgeError) as freeform:
            flatten_tools([{"type": "custom", "name": "other_tool", "format": {"type": "grammar"}}])
        self.assertIn("tool 'other_tool' of type 'custom'", str(freeform.exception))
        # Shapes that carry no usable identity still describe themselves.
        self.assertEqual(describe_tool({"type": "mcp"}), "a tool of type 'mcp'")
        self.assertEqual(describe_tool({"name": "x"}), "tool 'x' of type 'an unnamed type'")
        self.assertEqual(describe_tool("nope"), "a tool of type str")

    def test_namespaced_tools_keep_the_name_the_model_was_told_to_call(self):
        # Codex's multi-agent briefing names these tools bare ("use
        # `spawn_agent`"), so renaming them to an opaque hash left the model
        # with no tool matching anything its instructions mentioned, and it
        # simply never delegated. The leaf name has to survive flattening.
        flat, mapping = flatten_tools([{"type": "namespace", "name": "collaboration",
                                        "description": "Tools for spawning and managing sub-agents.",
                                        "tools": [{"type": "function", "name": "spawn_agent"},
                                                  {"type": "function", "name": "send_message"}]}])
        self.assertEqual([tool["name"] for tool in flat], ["spawn_agent", "send_message"])
        # The namespace is not lost: it rides the description, and the reverse
        # mapping still restores it on the way back out.
        self.assertIn("spawning and managing sub-agents", flat[0]["description"])
        restored = output_names({"type": "function_call", "name": "spawn_agent"}, mapping)
        self.assertEqual((restored["namespace"], restored["name"]), ("collaboration", "spawn_agent"))

    def test_a_clashing_leaf_name_falls_back_instead_of_failing_the_turn(self):
        # Two tools that want the same bare name still both travel: the second
        # takes the qualified form. Refusing the request instead would fail
        # every thread on the route over one duplicated leaf.
        flat, mapping = flatten_tools([
            {"type": "function", "name": "run"},
            {"type": "namespace", "name": "ns", "tools": [{"type": "function", "name": "run"}]}])
        names = [tool["name"] for tool in flat]
        self.assertEqual(names[0], "run")
        self.assertEqual(names[1], qualified_name("ns", "run"))
        self.assertEqual(len(set(names)), 2)
        for flat_name, (namespace, name) in zip(names, [(None, "run"), ("ns", "run")]):
            restored = output_names({"type": "function_call", "name": flat_name}, mapping)
            self.assertEqual((restored.get("namespace"), restored["name"]), (namespace, name))

    def test_flat_names_cannot_collide_with_encoded_namespace_names(self):
        # Both candidate names already taken by unrelated tools is a genuine
        # collision and still refuses, rather than silently aliasing two tools.
        with self.assertRaises(BridgeError):
            flatten_tools([{"type": "function", "name": "run"},
                           {"type": "function", "name": qualified_name("ns", "run")},
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

    def test_repair_apply_patch_mends_detached_markers_and_bare_context(self):
        raw = ("*** Begin Patch\n*** Update File: README.md\n@@\n"
               "The old line\n-\n+The new line\n@@\n**0.5.0 qualification**\n-\n+**Qualification**\n\n"
               "All tests pass\n*** End Patch")
        self.assertEqual(repair_apply_patch(raw).split("\n"), [
            "*** Begin Patch", "*** Update File: README.md", "@@", "-The old line", "+The new line",
            "@@", "-**0.5.0 qualification**", "+**Qualification**", "", " All tests pass", "*** End Patch"])

    def test_repair_apply_patch_turns_context_plus_detached_minus_into_a_replace(self):
        raw = ("*** Begin Patch\n*** Update File: README.md\n@@\n"
               " Build from the root to create the app. Old tail.\n-\n+Build from the root to create the app. New tail.\n"
               "\n **Provider connections**\n*** End Patch")
        self.assertIn("@@\n-Build from the root to create the app. Old tail.\n+Build from the root to create the app. New tail.\n"
                      "\n **Provider connections**\n", repair_apply_patch(raw))
        # A genuine blank-line removal next to unrelated context is left alone.
        legit = "*** Begin Patch\n*** Update File: a.txt\n@@\n keep\n-\n+totally different content here\n*** End Patch"
        self.assertEqual(repair_apply_patch(legit), legit)

    def test_repair_apply_patch_drops_diff_headers_and_prefixes_add_lines(self):
        raw = ("*** Begin Patch\n*** Update File: README.md\n--- a/README.md\n+++ b/README.md\n@@\n-old\n+new\n"
               "*** Add File: notes.txt\nfirst line\n+second line\n\n*** End Patch")
        self.assertEqual(repair_apply_patch(raw).split("\n"), [
            "*** Begin Patch", "*** Update File: README.md", "@@", "-old", "+new",
            "*** Add File: notes.txt", "+first line", "+second line", "+", "*** End Patch"])

    def test_repair_apply_patch_leaves_valid_patches_and_non_patches_alone(self):
        valid = "*** Begin Patch\n*** Update File: a.py\n@@ def f():\n     x = 1\n-    return 1\n+    return 2\n*** End Patch"
        self.assertEqual(repair_apply_patch(valid), valid)
        search = "<<<<<<<SEARCH\nfoo\n=======\nbar\n>>>>>>>REPLACE"
        self.assertEqual(repair_apply_patch(search), search)
        self.assertIsNone(repair_apply_patch(None))

    def test_extract_patch_repairs_provider_text(self):
        arguments = json.dumps({"patch": "*** Begin Patch\n*** Update File: a.txt\n@@\nold\n-\n+new\n*** End Patch"})
        self.assertEqual(extract_patch(arguments), "*** Begin Patch\n*** Update File: a.txt\n@@\n-old\n+new\n*** End Patch")

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
