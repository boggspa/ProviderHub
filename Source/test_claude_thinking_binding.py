"""Anthropic prefix-bound replay regressions with a local signature-checking mock."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import test_gateway_hub as fixtures
from provider_requests import CLAUDE_THINKING_BINDING_BETA, prepare_request
from provider_registry import ProviderError
from responses_bridge import ReasoningEnvelope, to_messages


def prompt(**changes):
    return {"model": "claude-opus-5-5", "max_tokens": 1024,
            "system": "Original system", "messages": [
                {"role": "user", "content": "Original question"},
                {"role": "assistant", "content": [
                    {"type": "thinking", "thinking": "Original summary", "signature": "opaque-signature"},
                    {"type": "redacted_thinking", "data": "opaque-redacted-data"},
                    {"type": "tool_use", "id": "call1", "name": "read_file", "input": {"path": "a"}}]},
                {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "call1", "content": "File contents"}]}],
            **changes}


def plan(payload, model="claude-opus-5-5", provider="claude", **spec):
    return prepare_request(provider, {}, "mock-key", payload, model,
                           {"reasoning": True, "tools": True, **spec})


class ThinkingBindingTests(unittest.TestCase):
    def test_adaptive_and_enabled_replay_keep_every_block_unchanged(self):
        for thinking in ({"type": "adaptive", "display": "summarized"},
                         {"type": "enabled", "budget_tokens": 1024}):
            with self.subTest(thinking=thinking):
                payload = prompt(thinking=thinking)
                original = copy.deepcopy(payload)
                result = plan(payload, "claude-opus-4-6")
                self.assertEqual(payload, original)
                self.assertEqual(result["body"]["messages"], original["messages"])
                self.assertEqual(result["body"]["thinking"], {
                    **thinking, "block_binding": {"prefix_mismatch_behavior": "drop_block"}})
                self.assertIn(CLAUDE_THINKING_BINDING_BETA, result["headers"]["anthropic-beta"].split(","))

    def test_omitted_thinking_on_default_adaptive_models_negotiates_binding(self):
        for model in ("claude-fable-5-1", "claude-mythos-5-1", "claude-opus-5-5",
                      "claude-sonnet-5-5", "claude-haiku-5-5"):
            with self.subTest(model=model):
                result = plan(prompt(), model)
                self.assertEqual(result["body"]["thinking"], {
                    "type": "adaptive", "block_binding": {"prefix_mismatch_behavior": "drop_block"}})
                self.assertEqual(result["body"]["messages"], prompt()["messages"])

    def test_explicit_error_policy_and_other_binding_fields_are_preserved(self):
        thinking = {"type": "adaptive", "block_binding": {
            "prefix_mismatch_behavior": "error", "future_control": True}}
        result = plan(prompt(thinking=thinking))
        self.assertEqual(result["body"]["thinking"], thinking)
        self.assertEqual(result["compatibility"]["thinking_prefix_mismatch_behavior"], "error")
        self.assertIn(CLAUDE_THINKING_BINDING_BETA, result["headers"]["anthropic-beta"])

    def test_fast_beta_and_binding_beta_are_both_kept(self):
        result = plan(prompt(thinking={"type": "adaptive"}, speed="fast"), fast_mode=True)
        tokens = result["headers"]["anthropic-beta"].split(",")
        self.assertIn("fast-mode-2026-02-01", tokens)
        self.assertIn(CLAUDE_THINKING_BINDING_BETA, tokens)
        self.assertEqual(len(tokens), len(set(tokens)))

    def test_modes_that_forbid_binding_remove_only_replayed_thinking(self):
        for thinking in ({"type": "disabled"}, {"type": "between_tools"}):
            with self.subTest(thinking=thinking):
                payload = prompt(thinking=thinking)
                result = plan(payload)
                self.assertEqual(result["body"]["thinking"], thinking)
                self.assertNotIn("anthropic-beta", result["headers"])
                self.assertEqual(result["body"]["messages"][1]["content"], payload["messages"][1]["content"][2:])
                self.assertEqual(result["body"]["messages"][2], payload["messages"][2])
                self.assertEqual(result["compatibility"]["thinking_replay_blocks_removed"], 2)

    def test_no_config_does_not_enable_legacy_or_unknown_model(self):
        for model in ("claude-opus-4-5", "claude-opus-4-8", "claude-future"):
            with self.subTest(model=model):
                result = plan(prompt(), model)
                self.assertNotIn("thinking", result["body"])
                self.assertNotIn("anthropic-beta", result["headers"])
                self.assertEqual(result["compatibility"]["thinking_replay_blocks_removed"], 2)

    def test_thinking_only_assistant_is_removed_without_empty_placeholder(self):
        payload = prompt(thinking={"type": "disabled"})
        payload["messages"][1]["content"] = payload["messages"][1]["content"][:2]
        payload["messages"][2]["content"] = "New question"
        result = plan(payload)
        self.assertEqual(result["body"]["messages"], [payload["messages"][0], payload["messages"][2]])

    def test_other_native_providers_keep_replay_and_thinking_configuration(self):
        payload = prompt(thinking={"type": "adaptive"})
        result = plan(payload, "muse-spark-1.3", "muse")
        self.assertEqual(result["body"]["messages"], payload["messages"])
        self.assertEqual(result["body"]["thinking"], payload["thinking"])
        self.assertNotIn("anthropic-beta", result["headers"])

    def test_malformed_binding_fails_with_a_clear_adapter_error(self):
        with self.assertRaisesRegex(ProviderError, "block_binding must be an object"):
            plan(prompt(thinking={"type": "adaptive", "block_binding": "invalid"}))

    def test_responses_encrypted_replay_reaches_the_same_binding_policy(self):
        with tempfile.TemporaryDirectory() as directory:
            envelope = ReasoningEnvelope(Path(directory))
            blocks = prompt()["messages"][1]["content"][:2]
            token = envelope.seal(blocks, "scope")
            converted = to_messages({"model": "claude/claude-opus-5-5", "stream": False,
                "instructions": "Changed instructions", "reasoning": {"effort": "high"},
                "input": [{"role": "user", "content": "Earlier question"},
                          {"type": "reasoning", "encrypted_content": token},
                          {"role": "assistant", "content": "Earlier answer"},
                          {"role": "user", "content": "Follow-up"}]},
                "claude/claude-opus-5-5", {"max_output": 4096}, envelope, "scope")
            result = plan(converted)
            self.assertEqual(result["body"]["thinking"]["block_binding"]["prefix_mismatch_behavior"], "drop_block")
            self.assertEqual(result["body"]["messages"][1]["content"][:2], blocks)


def prefix(body, messages):
    return hashlib.sha256(json.dumps({"system": body.get("system"), "tools": body.get("tools"),
                                     "messages": messages}, sort_keys=True).encode()).hexdigest()


class BindingProvider(fixtures.MockProvider):
    """Simulate the documented permanent 400 on a changed signed prefix."""
    def do_POST(self):
        body = self.read_body()
        replay = [block for message in body["messages"] for block in message.get("content", [])
                  if isinstance(block, dict) and block.get("type") == "thinking"]
        mismatched = bool(replay and replay[0]["signature"] != prefix(body, body["messages"][:1]))
        policy = ((body.get("thinking") or {}).get("block_binding") or {}).get("prefix_mismatch_behavior")
        beta = self.headers.get("anthropic-beta", "").split(",")
        if mismatched and (policy != "drop_block" or CLAUDE_THINKING_BINDING_BETA not in beta):
            self.send_json(400, {"type": "error", "error": {"type": "invalid_request_error", "message":
                "messages.1.content.0: Invalid `signature` in `thinking` block. The block is bound to a different conversation."}})
            return
        if replay:
            if body.get("stream"):
                self.send_sse(fixtures.native_stream_final())
            else:
                self.send_json(200, fixtures.native_message([{"type": "text", "text": "Tool loop completed"}], "end_turn"))
        else:
            self.send_json(200, fixtures.native_message([
                {"type": "thinking", "thinking": "Unchanged summary", "signature": prefix(body, body["messages"])},
                {"type": "tool_use", "id": "bound_call", "name": "read_file", "input": {"path": "a"}}], "tool_use"))


class ThinkingBindingHTTPTests(unittest.TestCase):
    tearDown = fixtures.GatewayHubHTTPTests.tearDown
    start_gateway = fixtures.GatewayHubHTTPTests.start_gateway
    request = fixtures.GatewayHubHTTPTests.request

    def setUp(self):
        fixtures.GatewayHubHTTPTests.setUp(self)
        self.upstream.RequestHandlerClass = BindingProvider
        BindingProvider.reset()

    def test_changed_system_and_history_continue_in_json_and_streaming_tool_loops(self):
        self.start_gateway("claude", "claude-opus-5-5", {"reasoning": True})
        for streaming in (False, True):
            with self.subTest(streaming=streaming):
                original = {"model": "claude-fable-5", "max_tokens": 1024,
                            "system": "Original system", "messages": [{"role": "user", "content": "Question"}],
                            "tools": [fixtures.tool_definition()]}
                status, raw, _ = self.request(original)
                self.assertEqual(status, 200, raw)
                content = json.loads(raw)["content"]
                followup = copy.deepcopy(original)
                followup["system"] = "Changed system"
                followup["messages"][0]["content"] = "Compacted earlier question"
                followup["messages"] += [{"role": "assistant", "content": content},
                    {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "bound_call", "content": "File contents"}]}]
                followup["stream"] = streaming
                status, raw, _ = self.request(followup, extra_headers={"anthropic-beta": "interleaved-thinking-2025-05-14"})
                self.assertEqual(status, 200, raw)
                self.assertEqual(BindingProvider.requests[-1]["messages"][1]["content"], content)
                self.assertEqual(BindingProvider.requests[-1]["messages"][2], followup["messages"][2])
                tokens = BindingProvider.request_headers[-1]["anthropic-beta"].split(",")
                self.assertIn(CLAUDE_THINKING_BINDING_BETA, tokens)
                self.assertIn("interleaved-thinking-2025-05-14", tokens)
                self.assertIn(b"Native stream complete" if streaming else b"Tool loop completed", raw)

    def test_mock_reproduces_permanent_400_without_the_binding_fix(self):
        self.start_gateway("claude", "claude-opus-5-5", {"reasoning": True})
        # Demonstrate that the local provider actually rejects the old wire
        # shape; the success regressions must depend on policy AND beta.
        with patch("provider_requests._claude_thinking_binding", return_value={}):
            status, raw, _ = self.request(prompt(model="claude-fable-5"))
        self.assertEqual(status, 400, raw)
        self.assertIn(b"bound to a different conversation", raw)

    def test_append_only_tool_loop_keeps_original_token_reminder_prefix(self):
        self.start_gateway("claude", "claude-opus-5-5", {"reasoning": True})
        reminder = "<total_tokens>15000000 tokens left</total_tokens>"
        original = {"model": "claude-fable-5", "max_tokens": 1024, "system": reminder,
                    "messages": [{"role": "user", "content": "Question\n" + reminder}],
                    "tools": [fixtures.tool_definition()]}
        status, raw, _ = self.request(original)
        self.assertEqual(status, 200, raw)
        content = json.loads(raw)["content"]
        followup = copy.deepcopy(original)
        followup["messages"] += [{"role": "assistant", "content": content},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "bound_call", "content": "Result"}]}]
        status, raw, _ = self.request(followup)
        self.assertEqual(status, 200, raw)
        first, second = BindingProvider.requests[-2:]
        self.assertEqual(first["system"], second["system"])
        self.assertEqual(first["messages"], second["messages"][:1])
        self.assertEqual(second["messages"][0], original["messages"][0])
        self.assertEqual(content[0]["signature"], prefix(second, second["messages"][:1]))
        self.assertNotIn("context_reminders", self.runtime.plan(followup)["compatibility"])
