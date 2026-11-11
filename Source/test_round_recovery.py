"""Recovery when a provider round writes a tool call the host cannot run."""
import json
import unittest

import grok_cli_agent
from chat_runtime import CORRECTIONS, MAX_CORRECTIONS, ModelRequestError, corrected_payload, empty_response_error, retryable_request
from protocol import StreamTranslator, ToolOutputError, translate_response


def turn(role, text):
    return {"role": role, "content": text}


class GrokPromptBudgetTests(unittest.TestCase):
    def test_cold_transcript_keeps_the_task_and_the_newest_turn_under_the_excerpt_line(self):
        messages = [turn("user", "TASK START " + ("instruction " * 4000) + " TASK END")]
        messages += [turn("assistant" if index % 2 else "user", f"turn {index} " + ("x" * 8000))
                     for index in range(1, 40)]
        messages.append(turn("user", "CURRENT REQUEST please continue from here"))
        bounded = grok_cli_agent.bounded_transcript(messages)
        prompt = grok_cli_agent.render_prompt(bounded)
        self.assertLessEqual(len(prompt.encode("utf-8")), grok_cli_agent.PROMPT_EXCERPT_BUDGET + 2000)
        self.assertIn("TASK START", prompt)
        self.assertIn("CURRENT REQUEST please continue from here", prompt)
        self.assertIn("omitted", prompt)
        self.assertLess(prompt.index("TASK START"), prompt.index("CURRENT REQUEST"))
        # History matching still sees the original messages; this is a projection.
        self.assertEqual(messages[-1]["content"], "CURRENT REQUEST please continue from here")
        self.assertNotIn("omitted", messages[0]["content"])

    def test_preview_is_added_only_once_grok_would_excerpt(self):
        messages = [turn("user", "opening"), turn("assistant", "ack"),
                    turn("user", "HEAD-OF-TURN " + ("m" * 120_000) + " TAIL-OF-TURN")]
        fitted = grok_cli_agent.render_prompt(messages[:2] + [turn("user", "short current")])
        self.assertEqual(grok_cli_agent.with_current_turn(fitted, messages[:2] + [turn("user", "short current")]), fitted)
        prompt = grok_cli_agent.render_prompt(messages)
        previewed = grok_cli_agent.with_current_turn(prompt, messages)
        self.assertGreater(len(prompt.encode("utf-8")), grok_cli_agent.PROMPT_EXCERPT_SEEN)
        self.assertIn("HEAD-OF-TURN", previewed)
        self.assertIn("TAIL-OF-TURN", previewed)
        self.assertLess(previewed.index("HEAD-OF-TURN"), previewed.index("<turn"))

    def test_resume_delta_is_not_a_reason_to_drop_turns(self):
        messages = [turn("user", "a" * 50_000), turn("assistant", "b" * 50_000), turn("user", "keep me")]
        surface = grok_cli_agent._surface(
            {"system": "", "messages": messages, "host_tools": [], "tool_choice": None,
             "images": [], "conversation": "chat-1", "resume_delta": True},
            None)
        self.assertIn("a" * 50_000, surface["prompt"])
        self.assertNotIn("omitted", surface["prompt"])


class ColdReasonTests(unittest.TestCase):
    def test_diagnostics_never_raise(self):
        class Lease:
            scope = "chat"
            pending = {"prefix_count": 0, "prefix": "nope"}
            key = "same"

        class Owner:
            condition = None
            leases = [Lease()]

        self.assertEqual(grok_cli_agent._cold_reason(Owner(), Lease(), "same", []), "unclassified")
        self.assertEqual(grok_cli_agent._cold_reason(Owner(), type("L", (), {"scope": None})(), "same", []),
                         "no_conversation_scope")


class ToolArgumentFailureTests(unittest.TestCase):
    def test_nonstreaming_arguments_must_be_json_objects(self):
        for arguments in ("", "123", "[]", "{"):
            with self.subTest(arguments=arguments), self.assertRaises(ToolOutputError):
                translate_response({"choices": [{"message": {"tool_calls": [{"function": {
                    "name": "apply_patch", "arguments": arguments}}]}, "finish_reason": "tool_calls"}]}, "test", {})

    def test_empty_response_remains_retryable_after_corrections(self):
        self.assertTrue(retryable_request(empty_response_error("max_tokens")))
        self.assertTrue(retryable_request(empty_response_error("end_turn")))

    def test_waiting_tool_receipt_does_not_claim_history_was_rewritten(self):
        self.assertEqual(grok_cli_agent._history_mismatch({"tools": []}, []), "tool_results_mismatch")

    def test_length_cutoff_is_distinguished_from_bad_json(self):
        cut = StreamTranslator("mistral-large", {})
        cut.feed({"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call-1", "function": {
            "name": "apply_patch", "arguments": '{"patch":"' }}]}, "finish_reason": "length"}]})
        with self.assertRaises(ToolOutputError) as caught:
            cut.end()
        self.assertEqual(caught.exception.code, "output_truncated")
        self.assertIn("apply_patch", str(caught.exception))

        bad = StreamTranslator("mistral-large", {})
        bad.feed({"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call-1", "function": {
            "name": "apply_patch", "arguments": "{"}}]}, "finish_reason": "tool_calls"}]})
        with self.assertRaises(ToolOutputError) as caught:
            bad.end()
        self.assertEqual(caught.exception.code, "invalid_tool_arguments")

    def test_correction_is_a_projection_and_stops_after_two(self):
        payload = {"max_tokens": 16384, "messages": [
            {"role": "user", "content": [{"type": "text", "text": "Write the file"}]}]}
        error = ModelRequestError("The provider returned invalid tool arguments (tool apply_patch).",
                                  code="invalid_tool_arguments")
        corrected = corrected_payload(payload, error.code, error)
        self.assertIn("valid JSON", json.dumps(corrected["messages"]))
        self.assertNotIn("Host note", json.dumps(payload["messages"]))
        self.assertEqual(MAX_CORRECTIONS, 2)
        self.assertTrue(set(CORRECTIONS) >= {"output_truncated", "invalid_tool_arguments", "empty_response"})
