"""Socket-free tests for the spawn-depth flag.

Covers the depth decision matrix, the tool rewrite, the gateway
middleware's fail-open body handling (via a stub handler), and the
settings validation for the per-provider flag.
"""
from __future__ import annotations

import io
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from bridge_core import SLOTS
from hub_config import normalize
from responses_tools import tool_name
from spawn_depth import (FLATTENED_SPAWN_TOOL, SPAWN_CHILD_NOTE, SPAWN_DISABLED_NOTE,
                         apply_spawn_depth_limit, apply_subagent_model, filter_spawn_tools,
                         should_strip, strip_spawn_tools, with_note)


NAMESPACED_SPAWN = {"type": "namespace", "name": "collaboration", "tools": [
    {"type": "function", "name": "spawn_agent", "description": "Spawn"},
    {"type": "function", "name": "wait_for_agent", "description": "Wait"},
]}
BARE_SPAWN = {"type": "function", "name": "spawn_agent", "description": "Spawn"}
READ_FILE = {"type": "function", "name": "read_file", "description": "Read"}


def body_for(input_items, tools=True):
    body = {"model": "mistral/mistral-medium-2508", "input": input_items}
    if tools:
        body["tools"] = [dict(NAMESPACED_SPAWN), dict(READ_FILE)]
    return body


def stub_handler(payload, settings):
    headers = {"Content-Length": str(len(payload))}
    return SimpleNamespace(headers=headers, rfile=io.BytesIO(payload),
                           runtime=SimpleNamespace(settings=settings, record_calls=[]))


def record_stub(handler):
    calls = []
    handler.runtime.record = lambda *args, **kwargs: calls.append((args, kwargs))
    return calls


class ShouldStripTests(unittest.TestCase):
    def test_unset_limit_never_strips(self):
        tasked = [{"type": "multi_agent_call", "call_id": "c1", "agent": "a", "arguments": {}}]
        self.assertFalse(should_strip(tasked, None))
        self.assertFalse(should_strip("text input", None))

    def test_zero_strips_unconditionally(self):
        self.assertTrue(should_strip([], 0))
        self.assertTrue(should_strip("text input", 0))
        self.assertTrue(should_strip([{"type": "message"}], 0))

    def test_depth_one_fresh_parent_keeps_tool(self):
        history = [
            {"type": "message", "role": "user", "content": "hi"},
            {"type": "function_call", "call_id": "1", "name": "read_file", "arguments": "{}"},
            {"type": "function_call_output", "call_id": "1", "output": "ok"},
        ]
        self.assertFalse(should_strip(history, 1))

    def test_depth_one_child_loses_tool(self):
        for task in ({"type": "multi_agent_call", "call_id": "c1", "agent": "a", "arguments": {}},
                     {"type": "subagent_call", "call_id": "c1", "agent": "a", "arguments": {}},
                     {"type": "agent_message", "agent": "a", "role": "assistant", "content": "task"}):
            with self.subTest(task=task["type"]):
                self.assertTrue(should_strip([task], 1))

    def test_depth_one_spawning_parent_keeps_tool(self):
        # The critical anti-false-positive: a parent whose history carries
        # delegation records AND its own spawn calls is still a parent.
        history = [
            {"type": "multi_agent_call", "call_id": "c1", "agent": "a", "arguments": {}},
            {"type": "function_call", "call_id": "f1", "namespace": "collaboration",
             "name": "spawn_agent", "arguments": "{}"},
            {"type": "function_call_output", "call_id": "f1", "output": "spawned"},
        ]
        self.assertFalse(should_strip(history, 1))

    def test_depth_one_recognizes_bare_and_flattened_spawn_calls(self):
        task = {"type": "agent_message", "agent": "a", "content": "task"}
        bare = {"type": "function_call", "call_id": "f1", "name": "spawn_agent", "arguments": "{}"}
        flat = {"type": "function_call", "call_id": "f2", "name": FLATTENED_SPAWN_TOOL,
                "arguments": "{}"}
        self.assertFalse(should_strip([task, bare], 1))
        self.assertFalse(should_strip([task, flat], 1))
        self.assertEqual(FLATTENED_SPAWN_TOOL, tool_name("collaboration", "spawn_agent"))

    def test_depth_one_ignores_completion_records(self):
        history = [{"type": "multi_agent_call_output", "call_id": "c1", "output": "done"},
                   {"type": "subagent_call_output", "call_id": "c2", "output": "done"}]
        self.assertFalse(should_strip(history, 1))

    def test_depth_one_string_input_keeps_tool(self):
        self.assertFalse(should_strip("just a prompt", 1))

    def test_non_dict_items_are_skipped(self):
        self.assertFalse(should_strip(["junk", None, 42], 1))
        self.assertTrue(should_strip(["junk", {"type": "agent_message", "content": "t"}], 1))


class StripSpawnToolsTests(unittest.TestCase):
    def test_namespaced_spawn_removed_siblings_kept(self):
        tools, removed = strip_spawn_tools([NAMESPACED_SPAWN, READ_FILE])
        self.assertEqual(removed, 1)
        self.assertEqual(len(tools), 2)
        self.assertEqual([child["name"] for child in tools[0]["tools"]], ["wait_for_agent"])
        self.assertEqual(tools[1]["name"], "read_file")

    def test_emptied_namespace_dropped(self):
        lone = {"type": "namespace", "name": "collaboration",
                "tools": [{"type": "function", "name": "spawn_agent"}]}
        tools, removed = strip_spawn_tools([lone])
        self.assertEqual((tools, removed), ([], 1))

    def test_bare_and_flattened_spawn_removed(self):
        tools, removed = strip_spawn_tools(
            [BARE_SPAWN, {"type": "function", "name": FLATTENED_SPAWN_TOOL}, READ_FILE])
        self.assertEqual(removed, 2)
        self.assertEqual([tool["name"] for tool in tools], ["read_file"])

    def test_unrelated_tools_untouched(self):
        other_ns = {"type": "namespace", "name": "other", "tools": [
            {"type": "function", "name": "spawn_agent"}]}
        tools, removed = strip_spawn_tools([other_ns, READ_FILE, "junk"])
        self.assertEqual(removed, 0)
        self.assertEqual(tools, [other_ns, READ_FILE, "junk"])

    def test_non_list_passthrough(self):
        self.assertEqual(strip_spawn_tools(None), (None, 0))

    def test_input_never_mutated(self):
        original = [dict(NAMESPACED_SPAWN)]
        snapshot = json.loads(json.dumps(original))
        strip_spawn_tools(original)
        self.assertEqual(original, snapshot)


class ApplyLimitTests(unittest.TestCase):
    def test_no_limit_returns_same_object(self):
        body = body_for([{"type": "agent_message", "content": "t"}])
        self.assertIs(apply_spawn_depth_limit(body, None)[0], body)

    def test_strip_rewrites_copy(self):
        body = body_for([{"type": "agent_message", "content": "t"}])
        edited, stripped = apply_spawn_depth_limit(body, 1)
        self.assertEqual(stripped, 1)
        self.assertIsNot(edited, body)
        self.assertEqual(len(body["tools"]), 2)
        self.assertNotIn("spawn_agent", json.dumps(edited["tools"]))

    def test_no_spawn_tool_returns_same_object(self):
        body = body_for([{"type": "agent_message", "content": "t"}])
        body["tools"] = [dict(READ_FILE)]
        self.assertIs(apply_spawn_depth_limit(body, 1)[0], body)

    def test_non_dict_body_passthrough(self):
        self.assertEqual(apply_spawn_depth_limit([1, 2], 0), ([1, 2], 0))

    def test_a_stripped_request_is_told_what_was_withheld_and_why(self):
        # Codex's briefing tells every agent it "can spawn sub-agents". A
        # tasked child whose spawn tool is silently gone reads that as a
        # broken session ("agent metadata but no direct collaboration
        # controls", 10 Oct 2026) instead of finishing its own task.
        child = body_for([{"type": "agent_message", "content": "t"}])
        child["instructions"] = "You are Codex."
        edited, stripped = apply_spawn_depth_limit(child, 1)
        self.assertEqual(stripped, 1)
        self.assertEqual(edited["instructions"], "You are Codex.\n\n" + SPAWN_CHILD_NOTE)
        self.assertNotIn("spawn_agent", json.dumps(edited["tools"]))
        # The input is never mutated; the note lives on the copy only.
        self.assertEqual(child["instructions"], "You are Codex.")
        # Delegation switched off on the route says so in its own words.
        off = body_for([{"type": "message", "role": "user", "content": "hi"}])
        edited, stripped = apply_spawn_depth_limit(off, 0)
        self.assertEqual(stripped, 1)
        self.assertEqual(edited["instructions"], SPAWN_DISABLED_NOTE)
        self.assertIn("spawn tool", SPAWN_DISABLED_NOTE)
        self.assertIn("spawn tool", SPAWN_CHILD_NOTE)

    def test_note_never_reaches_a_request_that_kept_its_tool(self):
        parent = body_for([{"type": "message", "role": "user", "content": "hi"}])
        parent["instructions"] = "You are Codex."
        edited, stripped = apply_spawn_depth_limit(parent, 1)
        self.assertEqual(stripped, 0)
        self.assertIs(edited, parent)
        self.assertEqual(parent["instructions"], "You are Codex.")

    def test_with_note_keeps_text_and_leaves_other_shapes_alone(self):
        self.assertEqual(with_note(None, "note"), "note")
        self.assertEqual(with_note("", "note"), "note")
        self.assertEqual(with_note("keep", "note"), "keep\n\nnote")
        odd = [{"type": "input_text", "text": "x"}]
        self.assertIs(with_note(odd, "note"), odd)
        child = body_for([{"type": "agent_message", "content": "t"}])
        child["instructions"] = odd
        edited, stripped = apply_spawn_depth_limit(child, 1)
        self.assertEqual(stripped, 1)
        self.assertIs(edited["instructions"], odd)


class FilterMiddlewareTests(unittest.TestCase):
    SETTINGS = {"providers": {"mistral": {"spawn_depth_limit": 1}}}

    def test_strip_rewrites_body_and_records(self):
        body = body_for([{"type": "agent_message", "agent": "a", "content": "task"}])
        body["instructions"] = "You are Codex."
        handler = stub_handler(json.dumps(body).encode(), self.SETTINGS)
        calls = record_stub(handler)
        self.assertEqual(filter_spawn_tools(handler), 1)
        downstream = json.loads(handler.rfile.read())
        self.assertNotIn("spawn_agent", json.dumps(downstream["tools"]))
        # The downstream request explains the strip to the model.
        self.assertEqual(downstream["instructions"], "You are Codex.\n\n" + SPAWN_CHILD_NOTE)
        self.assertEqual(handler.headers["Content-Length"], str(len(json.dumps(
            downstream, ensure_ascii=False).encode())))
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0][0], "spawn_stripped")

    def test_parent_passes_byte_identical(self):
        body = body_for([{"type": "message", "role": "user", "content": "hi"}])
        raw = json.dumps(body).encode()
        handler = stub_handler(raw, self.SETTINGS)
        calls = record_stub(handler)
        self.assertEqual(filter_spawn_tools(handler), 0)
        self.assertEqual(handler.rfile.read(), raw)
        self.assertEqual(calls, [])

    def test_flag_absent_passes_through(self):
        body = body_for([{"type": "agent_message", "agent": "a", "content": "task"}])
        raw = json.dumps(body).encode()
        handler = stub_handler(raw, {"providers": {"mistral": {}}})
        self.assertEqual(filter_spawn_tools(handler), 0)
        self.assertEqual(handler.rfile.read(), raw)

    def test_other_provider_flag_not_applied(self):
        body = body_for([{"type": "agent_message", "agent": "a", "content": "task"}])
        raw = json.dumps(body).encode()
        settings = {"providers": {"mistral": {}, "deepseek": {"spawn_depth_limit": 0}}}
        handler = stub_handler(raw, settings)
        self.assertEqual(filter_spawn_tools(handler), 0)
        self.assertEqual(handler.rfile.read(), raw)

    def test_limit_zero_strips_fresh_parent(self):
        body = body_for([{"type": "message", "role": "user", "content": "hi"}])
        handler = stub_handler(json.dumps(body).encode(),
                               {"providers": {"mistral": {"spawn_depth_limit": 0}}})
        record_stub(handler)
        self.assertEqual(filter_spawn_tools(handler), 1)
        self.assertNotIn("spawn_agent", json.dumps(json.loads(handler.rfile.read())["tools"]))

    def test_malformed_shapes_pass_through(self):
        settings = {"providers": {"mistral": {"spawn_depth_limit": 0}}}
        for raw in (b"{not json", b"[1,2]", b"null"):
            with self.subTest(raw=raw):
                handler = stub_handler(raw, settings)
                self.assertEqual(filter_spawn_tools(handler), 0)
                self.assertEqual(handler.rfile.read(), raw)

    def test_missing_or_bad_model_passes_through(self):
        settings = {"providers": {"mistral": {"spawn_depth_limit": 0}}}
        for body in ({"input": []}, {"model": 42}, {"model": "unknown/x"},
                     {"model": ""}, {"model": "no/such provider!"}):
            with self.subTest(body=body):
                raw = json.dumps(body).encode()
                handler = stub_handler(raw, settings)
                self.assertEqual(filter_spawn_tools(handler), 0)
                self.assertEqual(handler.rfile.read(), raw)

    def test_transfer_encoding_and_bad_length_untouched(self):
        settings = {"providers": {"mistral": {"spawn_depth_limit": 0}}}
        handler = stub_handler(b'{"model": "mistral/m"}', settings)
        handler.headers["Transfer-Encoding"] = "chunked"
        self.assertEqual(filter_spawn_tools(handler), 0)
        for bad in ("0", "-5", "not-a-number", str(33 * 1024 * 1024)):
            with self.subTest(bad=bad):
                plain = SimpleNamespace(headers={"Content-Length": bad},
                                        rfile=io.BytesIO(b"junk"),
                                        runtime=handler.runtime)
                self.assertEqual(filter_spawn_tools(plain), 0)


class SubagentModelTests(unittest.TestCase):
    def call(self, arguments, namespace="collaboration", name="spawn_agent"):
        return {"id": "fc_1", "type": "function_call", "namespace": namespace,
                "name": name, "arguments": json.dumps(arguments)}

    def test_absent_model_is_filled_in(self):
        item = self.call({"task_name": "t", "message": "do it"})
        self.assertTrue(apply_subagent_model(item, "mistral/small"))
        self.assertEqual(json.loads(item["arguments"])["model"], "mistral/small")
        # Everything the parent did say survives the rewrite.
        self.assertEqual(json.loads(item["arguments"])["message"], "do it")

    def test_a_model_the_parent_named_is_left_alone(self):
        # The setting picks who answers when nobody asked for anyone in
        # particular; it does not overrule a choice the agent made.
        item = self.call({"task_name": "t", "message": "x", "model": "kimi/k3"})
        self.assertFalse(apply_subagent_model(item, "mistral/small"))
        self.assertEqual(json.loads(item["arguments"])["model"], "kimi/k3")

    def test_the_flattened_name_is_recognised_too(self):
        # By the time egress sees the call the namespace is restored, but a
        # route that fell back to the qualified name has no namespace at all.
        item = self.call({"task_name": "t", "message": "x"}, namespace=None,
                         name=FLATTENED_SPAWN_TOOL)
        self.assertTrue(apply_subagent_model(item, "mistral/small"))

    def test_unrelated_and_malformed_calls_pass_through(self):
        for item, why in (
            (self.call({"a": 1}, namespace=None, name="shell"), "another tool"),
            ({"type": "message", "role": "assistant"}, "not a call"),
            (self.call([1, 2]), "arguments are not an object"),
            ({**self.call({"a": 1}), "arguments": "{not json"}, "unparseable"),
            ({**self.call({"a": 1}), "arguments": None}, "no arguments"),
        ):
            with self.subTest(why=why):
                before = json.dumps(item, sort_keys=True)
                self.assertFalse(apply_subagent_model(item, "mistral/small"))
                self.assertEqual(json.dumps(item, sort_keys=True), before)

    def test_no_configured_route_changes_nothing(self):
        item = self.call({"task_name": "t", "message": "x"})
        before = json.dumps(item, sort_keys=True)
        for route in (None, ""):
            self.assertFalse(apply_subagent_model(item, route))
        self.assertEqual(json.dumps(item, sort_keys=True), before)


class SpawnDepthSettingsTests(unittest.TestCase):
    def test_default_is_one_level_of_delegation(self):
        # Unbounded was never chosen, only unset, and it is the one setting
        # nobody wants: every grandchild is another full-context streaming
        # request against the same provider quota.
        settings = normalize({}, SLOTS, "mistral-medium-2508")
        self.assertEqual(settings["providers"]["mistral"]["spawn_depth_limit"], 1)

    def test_zero_and_one_preserved(self):
        for value in (0, 1):
            with self.subTest(value=value):
                settings = normalize({"providers": {"mistral": {"spawn_depth_limit": value}}},
                                     SLOTS, "mistral-medium-2508")
                self.assertEqual(settings["providers"]["mistral"]["spawn_depth_limit"], value)

    def test_invalid_rejected(self):
        for value in (2, -1, "1", 1.0, True, [1], {"v": 1}):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    normalize({"providers": {"mistral": {"spawn_depth_limit": value}}},
                              SLOTS, "mistral-medium-2508")

    def test_round_trip_stable(self):
        once = normalize({"providers": {"mistral": {"spawn_depth_limit": 1}}},
                         SLOTS, "mistral-medium-2508")
        twice = normalize(once, SLOTS, "mistral-medium-2508")
        self.assertEqual(twice["providers"]["mistral"]["spawn_depth_limit"], 1)


if __name__ == "__main__":
    unittest.main()
