"""Structured handoffs must be atomic, bounded, and isolated from Codex."""
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import cli_routes
import cli_structured_reply as structured
from cli_tool_call import OPEN_SENTINEL, ToolCallError
from test_cli_host_tools import Session, TOOLS, envelope, vendor_events


def reply(name="read_file", arguments=None, text="Inspecting the file."):
    return json.dumps({"text": text, "tool_calls": [
        {"name": name, "arguments": json.dumps(arguments or {"path": "sample.txt"})}]})


class StructuredManifestTests(unittest.TestCase):
    def test_manifest_lists_each_tool_as_compact_lossless_json(self):
        tool = {"name": "note", "description": "Keep, verbatim: this.",
                "input_schema": {"type": "object", "properties": {"text": {"type": "string"}}}}
        lines = structured.render_manifest([tool]).splitlines()
        listed = lines[lines.index("Host tools available:") + 1:]
        self.assertEqual(listed, [json.dumps(tool, separators=(",", ":"))])
        self.assertEqual(json.loads(listed[0]), tool)


class StructuredReplyTests(unittest.TestCase):
    def parse(self, chunks, terminal="completed", **kwargs):
        events = [{"type": "text_delta", "text": chunk} for chunk in chunks]
        if terminal is not None:
            events.append({"type": "message_stop", "stop_reason": terminal})
        return list(structured.parse_stream(iter(events), tools=TOOLS, **kwargs))

    def test_chunked_reply_is_one_validated_host_handoff(self):
        wire = reply(arguments={"path": "unicode-🐈.txt"})
        events = self.parse(list(wire))
        visible = [event for event in events if event["type"] != "ping"]
        self.assertEqual([event["type"] for event in visible], ["text_delta", "tool_call", "message_stop"])
        self.assertEqual(visible[0]["text"], "Inspecting the file.")
        self.assertEqual(visible[1]["input"], {"path": "unicode-🐈.txt"})
        self.assertEqual(visible[-1]["stop_reason"], "tool_use")

    def test_final_answer_and_literal_sentinels_are_not_calls(self):
        text = "The literal marker is " + OPEN_SENTINEL
        events = self.parse([json.dumps({"text": text, "tool_calls": []})])
        self.assertEqual(events[-2], {"type": "text_delta", "text": text})
        self.assertEqual(events[-1]["stop_reason"], "end_turn")

    def test_only_the_grounded_first_answer_is_executed(self):
        """Muse appends further schema-shaped answers about unexecuted work.

        The observed shape: one value asking for the edit, then a second that
        "verifies" it although the host has run nothing. Executing the tail,
        or rejecting the whole buffer over it, is what stalled these turns.
        """
        first = reply("write_file", {"path": "sample.txt", "text": "after"}, "Applying the edit.")
        speculative = reply("read_file", {"path": "sample.txt"}, "Edit applied; verifying it now.")
        for tail in (speculative, speculative + speculative, "  \n" + speculative, "{not json at all"):
            with self.subTest(tail=tail):
                events = [e for e in self.parse(list(first + tail)) if e["type"] != "ping"]
                self.assertEqual([e["type"] for e in events], ["text_delta", "tool_call", "message_stop"])
                self.assertEqual(events[0]["text"], "Applying the edit.")
                self.assertEqual(events[1]["name"], "write_file")
                self.assertEqual(events[1]["input"], {"path": "sample.txt", "text": "after"})

    def test_a_trailing_answer_cannot_revive_a_rejected_first_answer(self):
        """The tail is dropped, never promoted: value one decides the turn."""
        for head in ('{"text": "", "tool_calls": []}', '{"text": "x", "tool_calls": [{}]}'):
            with self.subTest(head=head):
                events = self.parse([head + reply()])
                self.assertEqual([e["type"] for e in events if e["type"] != "ping"], ["error"])

    def test_invalid_batch_releases_neither_prose_nor_partial_calls(self):
        valid = json.loads(reply())
        cases = ["I'll inspect that now.", reply()[:-1],
                 json.dumps({**valid, "tool_calls": valid["tool_calls"] + [{"name": "unoffered", "arguments": "{}"}]}),
                 json.dumps({**valid, "tool_calls": [{"name": "read_file", "arguments": "[1]"}]}),
                 json.dumps({**valid, "tool_calls": [{"name": "read_file", "arguments": '{"x": NaN}'}]}),
                 json.dumps({**valid, "tool_calls": valid["tool_calls"] * 9}),
                 json.dumps({"text": "", "tool_calls": []})]
        for wire in cases:
            with self.subTest(wire=wire):
                events = self.parse([wire])
                self.assertEqual([e["type"] for e in events if e["type"] != "ping"], ["error"])
                self.assertEqual(events[-1]["code"], "invalid_cli_tool_call")

    def test_tool_choice_and_parallel_limit_are_enforced(self):
        for choice, wire in [({"type": "any"}, '{"text":"I will do it","tool_calls":[]}'),
                             ({"type": "tool", "name": "write_file"}, reply()),
                             ({"disable_parallel_tool_use": True}, json.dumps({
                                 "text": "", "tool_calls": json.loads(reply())["tool_calls"] * 2}))]:
            with self.subTest(choice=choice), self.assertRaises(ToolCallError):
                structured.parse_reply(wire, TOOLS, choice)

    def test_limits_are_checked_before_dispatch(self):
        with patch.object(structured, "MAX_REPLY_BYTES", 10):
            self.assertEqual(self.parse([reply()])[-1]["type"], "error")
        with patch.object(structured, "MAX_ENVELOPE_BYTES", 2):
            self.assertEqual(self.parse([reply()])[-1]["type"], "error")

    def test_cancellation_or_cli_error_does_not_retry_or_release_a_call(self):
        for terminal in ("cancelled", "max_tokens", "failed"):
            requests = []
            def run(request, **kwargs):
                requests.append(request)
                yield {"type": "text_delta", "text": reply()}
                yield {"type": "message_stop", "stop_reason": terminal}
            events = list(cli_routes._tool_turn(SimpleNamespace(run_turn=run), {
                "tools": TOOLS, "host_tool_schema": structured.reply_schema(TOOLS)}, timeout=10))
            self.assertEqual(len(requests), 1)
            self.assertEqual([e["type"] for e in events if e["type"] != "ping"], ["error"])

    def test_bad_json_gets_one_private_correction(self):
        requests, closed = [], []
        def run(request, **kwargs):
            requests.append(request)
            try:
                yield {"type": "text_delta", "text": "I'll look first" if len(requests) == 1 else reply()}
                yield {"type": "message_stop", "stop_reason": "end_turn"}
            finally:
                closed.append(True)
        events = list(cli_routes._tool_turn(SimpleNamespace(run_turn=run), {
            "tools": TOOLS, "host_tool_schema": structured.reply_schema(TOOLS)}, timeout=10))
        self.assertEqual(len(requests), 2)
        self.assertEqual(len(closed), 2)
        self.assertEqual([e["text"] for e in events if e["type"] == "text_delta"], ["Inspecting the file."])
        correction = requests[-1]["messages"][-1]["content"]
        self.assertIn("JSON-encoded objects", correction)
        self.assertIn("do not perform that work again", correction)
        self.assertNotIn("reissue the intended call", correction)


class StructuredRouteTests(unittest.TestCase):
    def plan(self, provider, surface=None):
        payload = {"messages": [{"role": "user", "content": "Read the file"}], "tools": TOOLS}
        if surface:
            payload["_provider_hub_surface"] = surface
        return cli_routes.plan_turn(provider, "grok-4.6" if provider == "grok" else "muse-spark-1.3",
                                    payload, {}, wanted_output=512)

    def test_messages_uses_schema_but_responses_keeps_existing_protocol(self):
        messages = self.plan("muse")
        responses = self.plan("muse", "responses")
        self.assertIn("host_tool_schema", messages["body"])
        self.assertNotIn(OPEN_SENTINEL, messages["body"]["system"])
        self.assertNotIn("host_tool_schema", responses["body"])
        self.assertIn(OPEN_SENTINEL, responses["body"]["system"])
        # Grok offers host tools through its own use_tool dispatcher on both
        # surfaces instead, so neither schema nor envelope is planned for it.
        for surface in (None, "responses"):
            plan = self.plan("grok", surface)
            self.assertNotIn("host_tool_schema", plan["body"])
            self.assertNotIn(OPEN_SENTINEL, plan["body"]["system"] or "")
            self.assertEqual(plan["compatibility"]["cli_host_tools"], "use_tool")

    def test_adapter_schema_flags_and_temporary_file_cleanup(self):
        for provider in ("muse", "grok"):
            adapter = cli_routes.adapter_for(provider)
            plan = self.plan(provider)
            session = Session(vendor_events(provider, "read_file", {"path": "sample.txt"}))
            observed = {}
            def spawn(argv, **kwargs):
                observed["argv"] = argv
                if provider == "muse":
                    path = Path(argv[argv.index("--output-schema") + 1])
                    observed["path"] = path
                    observed["schema"] = json.loads(path.read_text())
                return session
            with self.subTest(provider=provider), patch.object(adapter, "_resolve_binary", return_value="/fake/cli"), \
                    patch.object(adapter, "StdioSession", side_effect=spawn):
                events = list(cli_routes.run_turn(provider, plan["body"], parse_tool_calls=True))
            self.assertEqual(events[-1]["stop_reason"], "tool_use")
            self.assertTrue(session.closed)
            argv = observed["argv"]
            if provider == "muse":
                self.assertEqual(observed["schema"], plan["body"]["host_tool_schema"])
                self.assertFalse(observed["path"].exists())
                self.assertEqual(argv[argv.index("--max-model-steps") + 1], "4")
            else:
                # Grok's host call rides use_tool: no schema, no turn cap.
                self.assertNotIn("--json-schema", argv)
                self.assertNotIn("--max-turns", argv)
                self.assertNotIn("use_tool", argv[argv.index("--disallowed-tools") + 1].split(","))
                self.assertEqual(argv[-2:], ["--tools", ""])


if __name__ == "__main__":
    unittest.main()
