"""Regression coverage for named, unpaired outputs sent by the Codex app."""
import copy
import unittest
from unittest import mock

import cli_routes
import codex_cli_agent as codex
from bridge_core import BridgeError
from responses_bridge import to_messages
from responses_tools import flatten_tools, input_names, normalize_custom_calls
from test_codex_cli_agent import FakeCodexSession, _completed_turn


class StandaloneToolOutputTests(unittest.TestCase):
    def notification(self, **extra):
        # Same shape as send_message_to_thread in the failing saved task.
        item = {"type": "function_call_output", "id": "fco-notification",
                "name": "send_message_to_thread", "namespace": "codex_app",
                "output": "<codex_delegation><input>Coordination message</input></codex_delegation>"}
        item.update(extra)
        return item

    def translate(self, items):
        return to_messages({"input": items, "stream": True, "store": False},
                           "codex/gpt-6-astra", {}, None, "test-scope")

    def test_cross_task_notification_survives_codex_history_injection(self):
        notification = self.notification()
        original = copy.deepcopy(notification)
        translated = self.translate([
            {"role": "user", "content": "Fix the renderer"},
            {"type": "function_call", "call_id": "call-1", "name": "exec_command",
             "arguments": '{"cmd":"git status --porcelain"}'},
            {"type": "function_call_output", "call_id": "call-1", "output": "clean"},
            notification,
            {"role": "user", "content": "Lets continue"},
        ])
        request = cli_routes.plan_turn("codex", "gpt-6-astra", translated, {}, wanted_output=1024)["body"]
        fake = FakeCodexSession([])
        fake.script = [_completed_turn()]
        with mock.patch.object(codex, "StdioSession", return_value=fake):
            events = list(codex.run_turn(request))
        self.assertEqual(events, [{"type": "message_stop", "stop_reason": "end_turn"}])
        params = next(params for method, params, _ in fake.requests if method == "thread/inject_items")
        items = params["items"]
        outputs = [item for item in items if item["type"] == "function_call_output"]
        self.assertEqual([item["call_id"] for item in outputs], ["call-1"])
        calls = [item for item in items if item["type"] == "function_call"]
        self.assertEqual([item["call_id"] for item in calls], ["call-1"])
        texts = [part["text"] for item in items if item["type"] == "message" for part in item["content"]]
        self.assertIn("[Standalone tool output from codex_app.send_message_to_thread]", texts)
        self.assertIn(notification["output"], texts)
        self.assertLess(texts.index(notification["output"]), texts.index("Lets continue"))
        self.assertEqual(notification, original)

    def test_absent_null_and_empty_call_ids_are_standalone(self):
        for call_id in (None, ""):
            for namespace in (None, "codex_app"):
                with self.subTest(call_id=call_id, namespace=namespace):
                    message = self.translate([self.notification(call_id=call_id, namespace=namespace)])["messages"][0]
                    self.assertEqual(message["role"], "user")
                    self.assertTrue(all(block["type"] == "text" for block in message["content"]))
                    self.assertIn("send_message_to_thread", message["content"][0]["text"])

    def test_tool_notification_keeps_images_and_empty_content(self):
        image = {"type": "input_image", "image_url": "data:image/png;base64,aW1hZ2U="}
        result = self.translate([self.notification(output=[{"type": "input_text", "text": "notice"}, image])])
        blocks = result["messages"][0]["content"]
        self.assertEqual(blocks[1], {"type": "text", "text": "notice"})
        self.assertEqual(blocks[2], {"type": "image", "source": {
            "type": "base64", "media_type": "image/png", "data": "aW1hZ2U="}})
        for output in ("", [], [{"type": "input_text", "text": ""}]):
            with self.subTest(output=output):
                blocks = self.translate([self.notification(output=output)])["messages"][0]["content"]
                self.assertEqual(len(blocks), 1)
                self.assertTrue(blocks[0]["text"])

    def test_missing_standalone_name_is_rejected_before_provider_call(self):
        for name in (None, "", " ", 7):
            with self.subTest(name=name), self.assertRaisesRegex(BridgeError, "standalone tool output needs a tool name"):
                self.translate([self.notification(name=name)])

    def test_invalid_standalone_namespace_is_rejected(self):
        for namespace in ("", " ", 7):
            with self.subTest(namespace=namespace), self.assertRaisesRegex(BridgeError, "namespace"):
                self.translate([self.notification(namespace=namespace)])

    def test_paired_outputs_keep_the_call_id_and_empty_content(self):
        for kind in ("function_call_output", "custom_tool_call_output"):
            for output, expected in (("", ""), ("done", [{"type": "text", "text": "done"}])):
                with self.subTest(kind=kind, output=output):
                    blocks = self.translate([{"type": kind, "call_id": "call-1", "output": output}])["messages"][0]["content"]
                    self.assertEqual(blocks, [{"type": "tool_result", "tool_use_id": "call-1", "content": expected}])

    def test_custom_output_and_name_collisions_keep_notification_attribution(self):
        _, mapping = flatten_tools([{"type": "namespace", "name": "another_namespace", "tools": [
            {"type": "function", "name": "send_message_to_thread", "parameters": {"type": "object"}}]}])
        items = [self.notification(type="custom_tool_call_output")]
        normalize_custom_calls(items, mapping)
        input_names(items, mapping)
        blocks = self.translate(items)["messages"][0]["content"]
        self.assertEqual(blocks[0]["text"], "[Standalone tool output from codex_app.send_message_to_thread]")
        self.assertEqual(blocks[1]["text"], self.notification()["output"])


if __name__ == "__main__":
    unittest.main()
