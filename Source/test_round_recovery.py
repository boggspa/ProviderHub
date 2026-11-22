"""Recovery when a provider round writes a tool call the host cannot run."""
import json
import unittest

import cli_routes
import grok_cli_agent
from chat_runtime import (CORRECTIONS, MAX_CORRECTIONS, ModelRequestError, corrected_payload, empty_response_error,
                          entry, grok_progress_hint, grok_recall, retryable_request)
from cli_tool_call import TOOL_RESULTS_KEY
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

    def test_cold_prompt_reserves_standing_context_and_keeps_durable_findings(self):
        old = entry("tool", tool="read_file", summary="Read placement.json", detail="Bay 3 is free",
                    workspace="/project", memberID="grok", recorded=True)
        entries = [old, {**old, "id": "duplicate"}, {**old, "id": "peer", "memberID": "claude"},
                   {**old, "id": "unrecorded", "recorded": False}, {**old, "id": "failed", "isError": True}]
        recall = grok_recall(entries, "grok", "/project")
        self.assertEqual([item["source"] for item in recall], ["duplicate"])
        messages = [turn("user", "Opening request"), {**turn("user", "Latest actual steer"),
                     "_provider_hub_user_instruction": True}]
        messages += [turn("assistant", str(i) + "x" * 8000) for i in range(10)]
        messages += [turn("assistant", "current tool call"),
                     {**turn("user", "current tool result"), TOOL_RESULTS_KEY: True}]
        plan = {"system": "Standing instructions" * 1100, "messages": messages, "host_tools": [],
                "tool_choice": None, "images": [], "conversation": "chat-1", "recall": recall}
        surface = grok_cli_agent._surface(plan, None)
        self.assertLessEqual(len((surface["system"] + surface["prompt"]).encode()), grok_cli_agent.PROMPT_EXCERPT_BUDGET)
        for expected in ("duplicate", "placement.json", "Bay 3 is free", "Latest actual steer",
                         "current tool call", "current tool result"):
            self.assertIn(expected, surface["prompt"])
        self.assertNotIn("workspace observations", grok_cli_agent._surface({**plan, "resume_delta": True}, None)["prompt"])

    def test_cold_recall_keeps_shell_findings_and_latest_failure_with_source_ids(self):
        def shell(identifier, command, detail, **extra):
            return entry("tool", id=identifier, tool="run_shell", summary=f"Run in /project:\n{command}",
                         detail=detail, workspace="/project", memberID="grok", recorded=True, **extra)

        finding = "Exit code: 0\nBay 3 is free\n" + "x" * 1200 + "\nFound Jango's slot"
        records = [shell("older", "rg Jango placement.json", finding),
                   shell("current", "grep Jango placement.json", finding),
                   shell("old-timeout", "find . -name era.json", "Exit code: -15 (timed out)", isError=True),
                   shell("new-timeout", "find . -name placement.json", "Exit code: -15 (timed out)", isError=True),
                   shell("empty", "true", "Exit code: 0"),
                   shell("background", "make", "Started background process")]
        recall = grok_recall(records, "grok", "/project")
        self.assertEqual([item["source"] for item in recall], ["current", "new-timeout"])
        self.assertEqual(recall[0]["summary"], "grep Jango placement.json")
        self.assertIn("Bay 3 is free", recall[0]["excerpt"])
        self.assertIn("Found Jango's slot", recall[0]["excerpt"])
        self.assertEqual(recall[1]["outcome"], "failed")
        self.assertIn("timed out", recall[1]["excerpt"])
        self.assertEqual(grok_recall(records, "peer", "/project"), [])

    def test_repeat_check_respects_new_user_input_and_changed_files(self):
        shell = entry("tool", route="grok/grok-4.7", tool="run_shell", summary="Run rg",
                      detail="Exit code: 0\nBay 3 is free", workspace="/project", memberID="grok", recorded=True)
        repeat = {**shell, "summary": "Run grep"}
        self.assertIn("repeated earlier results", grok_progress_hint([shell, repeat], shell, "grok"))
        self.assertIsNone(grok_progress_hint([shell, repeat], {**shell, "detail": "Exit code: 0\nBay 4 is free"}, "grok"))
        for boundary in ({"kind": "user"}, {**shell, "tool": "apply_patch", "changedFiles": ["placement.json"]},
                         {**repeat, "grokLoopHint": True}):
            self.assertIsNone(grok_progress_hint([shell, repeat, boundary], shell, "grok"))
        self.assertIsNone(grok_progress_hint([shell, {**repeat, "memberID": "peer"}], shell, "grok"))

    def test_repeated_shell_failures_and_a_recall_burst_get_guidance(self):
        shell = entry("tool", route="grok/grok-4.7", tool="run_shell", summary="Run find",
                      detail="Exit code: -15 (timed out)", workspace="/project", recorded=True, isError=True)
        self.assertIn("repeated earlier results or failures", grok_progress_hint([shell, shell], shell, None))
        recalls = [{**shell, "tool": "read_history", "isError": False, "detail": str(i)} for i in range(6)]
        self.assertIn("consecutive transcript lookups", grok_progress_hint(recalls[:-1], recalls[-1], None))

    def test_four_different_empty_workspace_searches_get_a_specific_nudge(self):
        searches = [entry("tool", route="grok/grok-4.7", tool="search_files", workspace="/project",
                          memberID="grok", summary=f"Search Sources for 'query-{i}'",
                          detail=f"[0 matches; {i} entries skipped; scan truncated]", recorded=True)
                    for i in range(4)]
        self.assertIsNone(grok_progress_hint(searches[:2], searches[2], "grok"))
        hint = grok_progress_hint(searches[:3], searches[3], "grok")
        self.assertIn("Four consecutive workspace searches returned no matches", hint)
        self.assertIn("read a known entry point", hint)
        self.assertIsNone(grok_progress_hint([searches[0]] * 2, searches[0], "grok"))
        self.assertIn("Four consecutive workspace searches", grok_progress_hint([searches[0]] * 3, searches[0], "grok"))
        for detail in ("Sources/placement.json:1: query-2\n[1 matches; 0 entries skipped]", "FileNotFoundError"):
            middle = {**searches[2], "detail": detail, "isError": detail == "FileNotFoundError"}
            self.assertIsNone(grok_progress_hint([*searches[:2], middle], searches[3], "grok"))


class ClaudePromptBudgetTests(unittest.TestCase):
    def test_chat_cold_replay_keeps_user_steer_and_current_cycle_but_native_history_is_complete(self):
        messages = [turn("user", "Opening prompt"), turn("assistant", "Earlier reasoning " + "x" * 32_000),
                    {**turn("user", "Latest user steer"), "_provider_hub_user_instruction": True},
                    turn("user", "Synthetic contribution prompt"),
                    {"role": "assistant", "content": [{"type": "tool_use", "name": "read_history", "id": "c",
                                                       "input": {"entry_id": "source"}}]},
                    {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "c",
                                                  "content": "Requested history result"}]}]
        payload = {"messages": messages, "_provider_hub_surface": "chat", "_provider_hub_conversation": "chat",
                   "tools": [{"name": "read_history", "description": "Read a saved record", "input_schema": {"type": "object"}}]}
        body = cli_routes.plan_turn("claude", "sonnet", payload, {}, wanted_output=4096)["body"]
        wire = json.dumps(body["messages"])
        self.assertNotIn("Earlier reasoning", wire)
        self.assertIn("Latest user steer", wire)
        self.assertIn("[tool call: read_history (c)", wire)
        self.assertIn("Requested history result", wire)
        self.assertEqual(body["history"], messages)
        external = cli_routes.plan_turn("claude", "sonnet", {**payload, "_provider_hub_surface": "responses"},
                                        {}, wanted_output=4096)["body"]
        self.assertIn("Earlier reasoning", json.dumps(external["messages"]))


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
