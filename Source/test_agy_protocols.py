"""Exercise AGY context and finish repair through both desktop protocols."""
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import agy_cli_agent as adapter
import cli_routes
from agy_context import MAX_PROMPT_BYTES
from responses_bridge import MessagesResponsesAdapter, to_messages
from test_cli_host_tools import Session, TOOLS


def finish_result(reply):
    return {"event": "result", "result": {"status": "SUCCESS", "response": "display metadata",
                                            "structured_output": reply}}


def rejected_finish(text):
    return {"event": "step_update", "step_update": {"step_type": "tool", "state": "ERROR",
            "tool_info": {"name": "finish", "parameters": {"text": text,
                          "toolAction": "Reply", "toolSummary": "Reply"},
                          "error": {"message": "missing required property tool_calls"}}}}


class AgyDesktopProtocolTests(unittest.TestCase):
    def payload(self, surface, system):
        if surface == "responses":
            body = {"instructions": system, "stream": True, "store": False, "input": [
                {"role": "user", "content": "Read first.txt then second.txt"},
                {"type": "function_call", "call_id": "call1", "name": "read_file", "arguments": '{"path":"first.txt"}'},
                {"type": "function_call_output", "call_id": "call1", "output": "Result one: ALPHA"},
                {"role": "user", "content": "Steer: keep both actual results"},
                {"type": "function_call", "call_id": "call2", "name": "read_file", "arguments": '{"path":"second.txt"}'},
                {"type": "function_call_output", "call_id": "call2", "output": "Result two: BETA"},
                {"role": "user", "content": "Latest steer: answer in two words"}],
                "tools": [{"type": "function", "name": t["name"], "parameters": t["input_schema"]} for t in TOOLS]}
            payload = to_messages(body, "antigravity/gemini-3.1-pro", {}, SimpleNamespace(), "test")
        else:
            payload = {"system": system, "stream": True, "tools": TOOLS, "messages": [
                {"role": "user", "content": "Read first.txt then second.txt"},
                {"role": "assistant", "content": [{"type": "tool_use", "id": "call1", "name": "read_file", "input": {"path": "first.txt"}}]},
                {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "call1", "content": "Result one: ALPHA"},
                                             {"type": "text", "text": "Steer: keep both actual results"}]},
                {"role": "assistant", "content": [{"type": "tool_use", "id": "call2", "name": "read_file", "input": {"path": "second.txt"}}]},
                {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "call2", "content": "Result two: BETA"},
                                             {"type": "text", "text": "Latest steer: answer in two words"}]}]}
        payload["_provider_hub_surface"] = surface
        return payload

    def run_plan(self, plan, factory):
        with patch.object(adapter, "StdioSession", side_effect=factory), \
                patch.object(adapter, "_resolve_binary", return_value="/fake/agy"):
            return [event for event in cli_routes._tool_turn(adapter, plan["body"], timeout=10)
                    if event["type"] != "ping"]

    def check_wire(self, events, surface, expected_text, expected_calls):
        wire = []
        result = cli_routes.relay_cli_turn(iter(events), wire.append, model="antigravity/gemini-3.1-pro")
        self.assertIsNone(result["error"])
        self.assertEqual(sum(e["type"] == "message_stop" for e in wire), 1)
        if surface == "responses":
            translator = MessagesResponsesAdapter("antigravity/gemini-3.1-pro", SimpleNamespace(), "test")
            translated = [item for event in wire for item in translator.feed(event)]
            self.assertEqual(sum(e["type"] == "response.completed" for e in translated), 1)
            completed = next(e["response"] for e in translated if e["type"] == "response.completed")
            text = "".join(block["text"] for item in completed["output"] if item["type"] == "message"
                           for block in item["content"])
            calls = [item for item in completed["output"] if item["type"] == "function_call"]
        else:
            text = "".join(e["delta"]["text"] for e in wire if e["type"] == "content_block_delta"
                           and e["delta"]["type"] == "text_delta")
            calls = [e for e in wire if e["type"] == "content_block_start"
                     and e["content_block"]["type"] == "tool_use"]
        self.assertEqual(text, expected_text)
        self.assertEqual(len(calls), expected_calls)

    def test_oversized_preamble_keeps_consecutive_results_and_steers_on_both_surfaces(self):
        system = "Preamble start\n" + "Bounded context line.\n" * 12_000 + "Preamble end"
        for surface in ("messages", "responses"):
            with self.subTest(surface=surface):
                plan = cli_routes.plan_turn("antigravity", "gemini-3.1-pro", self.payload(surface, system), {}, wanted_output=128)
                roots = []
                def factory(argv, **kwargs):
                    root = Path(kwargs["cwd"])
                    roots.append(root)
                    session = Session([])
                    session.close_stdin = lambda: None
                    def script():
                        prompt = session.stdin.getvalue()
                        self.assertLessEqual(len(prompt.encode()), MAX_PROMPT_BYTES)
                        self.assertIn("fields populated directly", prompt)
                        needles = ("Read first.txt then second.txt", '"tool_use_id": "call1"', "Result one: ALPHA",
                                   "Steer: keep both actual results", '"tool_use_id": "call2"', "Result two: BETA",
                                   "Latest steer: answer in two words")
                        positions = [prompt.index(text) for text in needles]
                        self.assertEqual(positions, sorted(positions))
                        paths = json.loads((root / ".agents/context.json").read_text())
                        recovered = "".join(Path(path).read_text() for path in paths)
                        self.assertIn(system, recovered)
                        for path in paths:
                            yield {"event": "step_update", "step_update": {"step_type": "tool", "state": "DONE",
                                   "tool_info": {"name": "view_file", "parameters": {"AbsolutePath": path}}}}
                        yield finish_result({"text": "ALPHA BETA", "tool_calls": []})
                    session.script = script()
                    return session
                events = self.run_plan(plan, factory)
                self.assertEqual(events, [{"type": "text_delta", "text": "ALPHA BETA"},
                                          {"type": "message_stop", "stop_reason": "end_turn"}])
                self.check_wire(events, surface, "ALPHA BETA", 0)
                self.assertTrue(roots and all(not root.exists() for root in roots))

    def test_unread_or_failed_context_never_releases_a_reply(self):
        for read_status in (None, "ERROR", "ACTIVE"):
            with self.subTest(status=read_status):
                plan = cli_routes.plan_turn("antigravity", "gemini-3.1-pro", self.payload("messages", "x" * 210_000), {}, wanted_output=128)
                def factory(argv, **kwargs):
                    paths = json.loads((Path(kwargs["cwd"]) / ".agents/context.json").read_text())
                    events = []
                    if read_status:
                        events.append({"event": "step_update", "step_update": {"step_type": "tool", "state": read_status,
                                      "tool_info": {"name": "view_file", "parameters": {"AbsolutePath": paths[0]}}}})
                    return Session(events + [finish_result({"text": "Incomplete answer", "tool_calls": []})])
                events = self.run_plan(plan, factory)
                self.assertEqual([event["type"] for event in events], ["error"])
                self.assertEqual(events[0]["code"], "incomplete_cli_context")

    def test_private_context_lifetime_uses_the_process_exit_cleanup(self):
        plan = cli_routes.plan_turn("antigravity", "gemini-3.1-pro", self.payload("messages", "x" * 210_000), {}, wanted_output=128)
        pending = []
        roots = []
        def factory(argv, **kwargs):
            root = Path(kwargs["cwd"])
            roots.append(root)
            # An early provider failure must still retain the files until the
            # existing process-exit cleanup mechanism invokes its callback.
            return Session([{"event": "result", "result": {"status": "ERROR", "error": "cancelled"}}])
        with patch.object(adapter, "cleanup_after_exit", side_effect=lambda session, callback: pending.append(callback)):
            events = self.run_plan(plan, factory)
        try:
            self.assertEqual(events[0]["type"], "error")
            self.assertEqual(len(pending), 1)
            self.assertTrue(all(root.exists() and list(root.glob("context-*.txt")) for root in roots))
        finally:
            for release in pending:
                release()
        self.assertTrue(all(not root.exists() for root in roots))

    def test_nested_finish_repair_is_scoped_and_tool_calls_stay_validated(self):
        for surface in ("messages", "responses"):
            for inner in ({"text": "Final answer", "tool_calls": []},
                          {"text": "Reading", "tool_calls": [{"name": "read_file", "arguments": '{"path":"sample.txt"}'}]}):
                for witnessed in (False, True):
                    with self.subTest(surface=surface, inner=inner, witnessed=witnessed):
                        text = json.dumps(inner)
                        payload = self.payload(surface, "Keep JSON examples literal.")
                        plan = cli_routes.plan_turn("antigravity", "gemini-3.1-pro", payload, {}, wanted_output=128)
                        sequence = ([rejected_finish(text)] if witnessed else []) + [finish_result({"text": text, "tool_calls": []})]
                        events = self.run_plan(plan, lambda *a, **k: Session(sequence))
                        self.assertEqual(events[0]["text"], inner["text"] if witnessed else text)
                        calls = [event for event in events if event["type"] == "tool_call"]
                        self.assertEqual(len(calls), int(witnessed and bool(inner["tool_calls"])))
                        if calls:
                            self.assertEqual(calls[0]["input"], {"path": "sample.txt"})
                        self.check_wire(events, surface, inner["text"] if witnessed else text, len(calls))

    def test_invalid_nested_calls_and_choices_are_never_released(self):
        for calls, choice in [
            ([{"name": "unoffered", "arguments": "{}"}], None),
            ([{"name": "read_file", "arguments": '["not an input object"]'}], None),
            ([{"name": "read_file", "arguments": '{"path":"x"}'}] * 2, {"disable_parallel_tool_use": True}),
            ([], {"type": "any"}),
            ([{"name": "read_file", "arguments": '{"path":"x"}'}], {"type": "tool", "name": "write_file"}),
        ]:
            with self.subTest(calls=calls, choice=choice):
                text = json.dumps({"text": "Looks fine", "tool_calls": calls})
                state = adapter._TurnState()
                state.structured, state.tools, state.tool_choice = True, TOOLS, choice
                adapter._translate(rejected_finish(text), state)
                self.assertEqual(adapter._translate(finish_result({"text": text, "tool_calls": []}), state), [])
                self.assertEqual(state.failure_code, "invalid_cli_tool_call")
                self.assertFalse(state.terminal)

    def test_json_answers_without_matching_repair_evidence_remain_literal(self):
        state = adapter._TurnState()
        state.structured = True
        state.tools = TOOLS
        adapter._translate(rejected_finish('a different failed answer'), state)
        answer = '{"text":"Quoted example", "tool_calls":[]}'
        adapter._translate(finish_result({"text": answer, "tool_calls": []}), state)
        self.assertEqual(json.loads(state.result_text)["text"], answer)
