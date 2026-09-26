"""Tests for the prompt-rendered tool-call convention (cli_tool_call)."""
from __future__ import annotations

import json
import unittest

from cli_tool_call import (CLOSE_SENTINEL, MAX_CALLS_PER_TURN, OPEN_SENTINEL,
                           ToolCallError, ToolCallParser, normalize_tools, render_tool_anchor,
                           render_tool_manifest)

WEATHER = {"name": "get_weather", "description": "Fetch weather for a city.",
           "input_schema": {"type": "object",
                            "properties": {"city": {"type": "string"}},
                            "required": ["city"]}}
SHELL = {"name": "shell", "description": "Run a command.",
         "input_schema": {"type": "object", "properties": {"cmd": {"type": "string"}}}}


def envelope(name, arguments):
    return OPEN_SENTINEL + json.dumps({"name": name, "input": arguments}) + CLOSE_SENTINEL


def run_parser(chunks):
    parser = ToolCallParser()
    events = []
    for chunk in chunks:
        events.extend(parser.feed(chunk))
    events.extend(parser.finish())
    return events


class NormalizeToolsTest(unittest.TestCase):
    def test_valid_tools_pass_through_with_defaults(self):
        tools = normalize_tools([WEATHER, {"name": "bare"}])
        self.assertEqual([tool["name"] for tool in tools], ["get_weather", "bare"])
        self.assertEqual(tools[1]["input_schema"], {"type": "object", "properties": {}})
        self.assertEqual(tools[1]["description"], "")

    def test_junk_is_dropped_not_raised(self):
        self.assertEqual(normalize_tools([None, "x", {"no_name": 1},
                                          {"name": "has space"}, {"name": "ok"}]),
                         [{"name": "ok", "description": "",
                           "input_schema": {"type": "object", "properties": {}}}])
        self.assertEqual(normalize_tools(None), [])


class ManifestTest(unittest.TestCase):
    def test_manifest_carries_convention_rules_and_schemas(self):
        text = render_tool_manifest([WEATHER, SHELL])
        self.assertIn(OPEN_SENTINEL, text)
        self.assertIn(CLOSE_SENTINEL, text)
        self.assertIn("host application executes tools", text)
        self.assertIn("never invent tool", text)
        self.assertIn("1. get_weather - Fetch weather for a city.", text)
        self.assertIn('"city"', text)
        self.assertIn("2. shell - Run a command.", text)

    def test_manifest_schemas_are_compact_json_and_lossless(self):
        spaced = {"name": "note", "description": "Keep, verbatim: this.",
                  "input_schema": {"type": "object", "properties": {
                      "text": {"type": "string", "description": "a, b: c"}}, "required": ["text"]}}
        text = render_tool_manifest([spaced])
        line = next(line for line in text.splitlines() if line.startswith("   input schema: "))
        body = line[len("   input schema: "):]
        self.assertEqual(json.loads(body), spaced["input_schema"])
        self.assertEqual(body, json.dumps(spaced["input_schema"], separators=(",", ":")))
        self.assertIn('"a, b: c"', body)
        self.assertIn("1. note - Keep, verbatim: this.", text)

    def test_tool_choice_variants(self):
        self.assertIn("MUST call at least one tool",
                      render_tool_manifest([WEATHER], {"type": "any"}))
        self.assertIn("MUST call the tool 'get_weather'",
                      render_tool_manifest([WEATHER], {"type": "tool", "name": "get_weather"}))
        self.assertNotIn("MUST", render_tool_manifest([WEATHER], {"type": "auto"}))

    def test_anchor_names_tools_and_disowns_cli_inventory(self):
        text = render_tool_anchor([WEATHER, SHELL])
        self.assertIn("get_weather, shell", text)
        self.assertIn("host application", text)
        self.assertIn("not the host's", text)
        self.assertIn(OPEN_SENTINEL, text)


class ParserTest(unittest.TestCase):
    def test_plain_text_passes_through(self):
        self.assertEqual(run_parser(["hello world"]), [("text", "hello world")])

    def test_single_call_one_chunk(self):
        events = run_parser(["The sky says " + envelope("get_weather", {"city": "Paris"})])
        kinds = [kind for kind, _ in events]
        self.assertEqual(kinds, ["text", "call"])
        self.assertEqual(events[0], ("text", "The sky says "))
        call = events[1][1]
        self.assertEqual(call["name"], "get_weather")
        self.assertEqual(call["input"], {"city": "Paris"})
        self.assertTrue(call["id"].startswith("toolu_"))

    def test_call_split_across_every_chunk_boundary(self):
        wire = "pre " + envelope("shell", {"cmd": "ls -la"}) + " post"
        for size in (1, 2, 3, 5, 7, 11):
            with self.subTest(chunk=size):
                events = run_parser([wire[i:i + size] for i in range(0, len(wire), size)])
                texts = "".join(payload for kind, payload in events if kind == "text")
                calls = [payload for kind, payload in events if kind == "call"]
                self.assertEqual(texts, "pre  post")
                self.assertEqual(len(calls), 1)
                self.assertEqual(calls[0]["input"], {"cmd": "ls -la"})

    def test_multiple_calls_and_text_between(self):
        wire = (envelope("get_weather", {"city": "Paris"}) + " and then " +
                envelope("shell", {"cmd": "pwd"}) + " done")
        events = run_parser([wire])
        calls = [payload for kind, payload in events if kind == "call"]
        self.assertEqual([call["name"] for call in calls], ["get_weather", "shell"])
        self.assertEqual("".join(p for k, p in events if k == "text"), " and then  done")

    def test_arguments_alias_is_accepted(self):
        wire = OPEN_SENTINEL + '{"name": "shell", "arguments": {"cmd": "ls"}}' + CLOSE_SENTINEL
        events = run_parser([wire])
        self.assertEqual(events[0][0], "call")
        self.assertEqual(events[0][1]["input"], {"cmd": "ls"})

    def test_parameters_alias_is_accepted(self):
        # Responses-native models (codex) habitually write "parameters".
        wire = OPEN_SENTINEL + '{"name": "get_weather", "parameters": {"city": "Paris"}}' + CLOSE_SENTINEL
        events = run_parser([wire])
        self.assertEqual(events[0][0], "call")
        self.assertEqual(events[0][1]["input"], {"city": "Paris"})

    def test_invalid_json_is_an_explicit_error(self):
        wire = "look: " + OPEN_SENTINEL + "{not json}" + CLOSE_SENTINEL
        with self.assertRaisesRegex(ToolCallError, "not valid JSON"):
            run_parser([wire])

    def test_non_object_bad_name_and_nonfinite_numbers_are_errors(self):
        for body in ('[1, 2]', '{"name": "has space"}', '{"name": 42}',
                     '{"name": "ok", "input": [1]}',
                     '{"name": "ok", "input": {"x": NaN}}'):
            with self.subTest(body=body), self.assertRaises(ToolCallError):
                run_parser([OPEN_SENTINEL + body + CLOSE_SENTINEL])

    def test_unclosed_envelope_is_an_error(self):
        with self.assertRaisesRegex(ToolCallError, "not closed"):
            run_parser(["start " + OPEN_SENTINEL + '{"name": "shell"'])

    def test_bounded_closing_marker_repair_keeps_json_validation(self):
        for close in ("</tool_call>", "<</tool_call>>", "<<</tool_call>>"):
            with self.subTest(close=close):
                wire = OPEN_SENTINEL + '{"name":"shell","input":{"cmd":"pwd"}}' + close
                events = run_parser(list(wire))
                self.assertEqual([kind for kind, _ in events], ["call"])
                self.assertEqual(events[0][1]["input"], {"cmd": "pwd"})
                with self.assertRaises(ToolCallError):
                    run_parser([OPEN_SENTINEL + '{broken}' + close])

    def test_closing_delimiters_inside_arguments_are_not_framing(self):
        argument = 'print("</tool_call> and <<</tool_call>>>")'
        wire = envelope("shell", {"cmd": argument})
        events = run_parser(list(wire))
        self.assertEqual([kind for kind, _ in events], ["call"])
        self.assertEqual(events[0][1]["input"], {"cmd": argument})

    def test_multiple_shortened_closing_markers_parse_without_argument_leakage(self):
        wire = (envelope("shell", {"cmd": "pwd"}) + envelope("shell", {"cmd": "ls"}))
        wire = wire.replace(CLOSE_SENTINEL, "<</tool_call>>")
        events = run_parser(list(wire))
        self.assertEqual([kind for kind, _ in events], ["call", "call"])

    def test_partial_sentinel_at_finish_is_text(self):
        events = run_parser(["almost <<<tool_ca"])
        self.assertEqual("".join(payload for kind, payload in events), "almost <<<tool_ca")

    def test_size_limit_applies_to_closed_unclosed_and_unicode_envelopes(self):
        for value in ("x" * 70000, "😀" * 17000):
            wire = envelope("shell", {"cmd": value})
            for chunks in ([wire], [wire[:-len(CLOSE_SENTINEL)]],
                           [wire[i:i + 100] for i in range(0, len(wire), 100)]):
                with self.subTest(length=len(value)), self.assertRaises(ToolCallError):
                    run_parser(chunks)

    def test_call_cap_is_an_error(self):
        wire = "".join(envelope("shell", {"cmd": f"cmd{i}"}) for i in range(MAX_CALLS_PER_TURN + 1))
        with self.assertRaisesRegex(ToolCallError, "too many"):
            run_parser([wire])

    def test_empty_feed_and_double_finish(self):
        parser = ToolCallParser()
        self.assertEqual(parser.feed(""), [])
        self.assertEqual(parser.finish(), [])
        self.assertEqual(parser.finish(), [])

    def test_text_right_up_to_sentinel_edge(self):
        # "<<" is a plausible ordinary suffix; it must not be eaten early.
        events = run_parser(["1 << 2"])
        self.assertEqual(events, [("text", "1 << 2")])


if __name__ == "__main__":
    unittest.main()
