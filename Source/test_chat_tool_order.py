"""Claude Desktop tool-result order must not emit tool after user."""
import unittest

from chat_tool_order import repair_openai_tool_order
from providers import prepare_request
from protocol import translate_request, tool_id
from test_bridge import config, prompt
from test_providers import text_prompt


class RepairOrderTests(unittest.TestCase):
    def test_moves_tool_results_ahead_of_intervening_user_text(self):
        messages = [
            {"role": "user", "content": "read"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "abc123456", "type": "function", "function": {"name": "Read", "arguments": "{}"}},
            ]},
            {"role": "user", "content": "reminder text"},
            {"role": "tool", "tool_call_id": "abc123456", "content": "file contents"},
            {"role": "user", "content": "now edit"},
        ]
        repaired = repair_openai_tool_order(messages)
        self.assertEqual([m["role"] for m in repaired], ["user", "assistant", "tool", "user", "user"])
        self.assertEqual(repaired[2]["content"], "file contents")
        self.assertEqual(repaired[3]["content"], "reminder text")

    def test_orphan_tools_become_user_text(self):
        messages = [
            {"role": "user", "content": "hello"},
            {"role": "tool", "tool_call_id": "missing", "content": "orphan result"},
        ]
        repaired = repair_openai_tool_order(messages)
        self.assertEqual([m["role"] for m in repaired], ["user", "user"])
        self.assertEqual(repaired[1]["content"], "orphan result")


class TranslationOrderTests(unittest.TestCase):
    def test_mistral_chat_plan_reorders_text_before_tool_result(self):
        payload = text_prompt(messages=[
            {"role": "user", "content": "Read a.txt"},
            {"role": "assistant", "content": [
                {"type": "tool_use", "id": "toolu_one", "name": "Read", "input": {"path": "a.txt"}},
            ]},
            {"role": "user", "content": [
                {"type": "text", "text": "<total_tokens>99 tokens left</total_tokens>"},
                {"type": "tool_result", "tool_use_id": "toolu_one", "content": "alpha"},
            ]},
        ])
        roles = [message["role"] for message in prepare_request(
            "mistral", {}, "key", payload, "mistral-medium-2508", {"reasoning": True, "vision": True},
        )["body"]["messages"]]
        # After repair: tool follows assistant, user text is preserved as separate message
        self.assertEqual(roles, ["user", "assistant", "tool", "user"])
        self.assertNotIn(("user", "tool"), list(zip(roles, roles[1:])))

    def test_legacy_translate_reorders_text_before_tool_result(self):
        body = prompt(messages=[
            {"role": "user", "content": "Read a.txt"},
            {"role": "assistant", "content": [
                {"type": "tool_use", "id": "toolu_one", "name": "Read", "input": {"path": "a.txt"}},
            ]},
            {"role": "user", "content": [
                {"type": "text", "text": "hello reminder"},
                {"type": "tool_result", "tool_use_id": "toolu_one", "content": "alpha"},
            ]},
        ])
        result, _ = translate_request(body, config())
        roles = [message["role"] for message in result["messages"]]
        self.assertEqual(roles[0], "user")
        self.assertIn("assistant", roles)
        self.assertIn("tool", roles)
        self.assertEqual(roles[roles.index("assistant") + 1], "tool")
        self.assertNotIn(("user", "tool"), list(zip(roles, roles[1:])))
        mapped = tool_id("toolu_one")
        tool_message = next(message for message in result["messages"] if message["role"] == "tool")
        self.assertEqual(tool_message["tool_call_id"], mapped)


if __name__ == "__main__":
    unittest.main(verbosity=2)
