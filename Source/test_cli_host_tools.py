"""Exercise host tool handoffs through the actual CLI adapters and wire relay.

Vendor processes are scripted; the host really reads, edits, and rereads a
temporary file. No credentials or external services are used by this suite.
"""
from contextlib import ExitStack
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

import cli_routes
from cli_tool_call import CLOSE_SENTINEL, OPEN_SENTINEL


TOOLS = [
    {"name": "read_file", "description": "Read a host workspace file.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}},
                      "required": ["path"]}},
    {"name": "write_file", "description": "Write a host workspace file.",
     "input_schema": {"type": "object", "properties": {
         "path": {"type": "string"}, "text": {"type": "string"}},
         "required": ["path", "text"]}},
]
MODELS = {"codex": "gpt-6-astra", "grok": "grok-4.6", "muse": "muse-spark-1.3",
          "antigravity": "gemini-3.1-pro", "claude": "sonnet"}


def envelope(name="read_file", arguments=None):
    return OPEN_SENTINEL + json.dumps({"name": name, "input": arguments or {}}) + CLOSE_SENTINEL


class Session:
    def __init__(self, events, *, codex=False):
        self.script = events
        self.returncode = None if codex else 0
        self.process = None
        self.stdin = io.StringIO()
        self.requests = []
        self.closed = False

    def request(self, method, params, **kwargs):
        self.requests.append((method, params))
        return {"result": {"thread": {"id": "thread"}, "turn": {"id": "turn"}}}

    def notify(self, *args):
        pass

    def send(self, payload):
        self.stdin.write(payload)

    def events(self, **kwargs):
        yield from self.script

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def vendor_events(provider, name, arguments):
    wire = envelope(name, arguments)
    if provider in {"muse", "grok"}:
        wire = json.dumps({"text": "", "tool_calls": [
            {"name": name, "arguments": json.dumps(arguments)}]})
    if provider == "codex":
        from codex_cli_agent import _tool_alias
        return [{"id": 9, "method": "item/tool/call", "params": {
            "threadId": "thread", "turnId": "turn", "callId": "host_call",
            "namespace": "host", "tool": _tool_alias(name), "arguments": arguments}}]
    if provider == "grok":
        return [{"type": "assistant", "message": {"content": [{"type": "text", "text": wire}]}},
                {"type": "end", "stopReason": "end_turn"}]
    if provider == "muse":
        return [{"payload_type": "run.output.delta", "payload": {"text": wire}},
                {"payload_type": "run.terminal.completed", "payload": {"terminal": "completed"}}]
    if provider == "antigravity":
        return [{"event": "step_update", "step_update": {"step_type": "agent_response", "text_delta": wire}},
                {"event": "result", "result": {"status": "SUCCESS"}}]
    return [{"type": "stream_event", "event": {"type": "content_block_delta",
             "delta": {"type": "text_delta", "text": wire}}},
            {"type": "result", "subtype": "success", "stop_reason": "end_turn"}]


class HostCycleTests(unittest.TestCase):
    def test_read_edit_read_back_through_every_adapter(self):
        for provider, model in MODELS.items():
            with self.subTest(provider=provider), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "sample.txt"
                path.write_text("before")
                messages = [{"role": "user", "content": "Read sample.txt, replace before with after, and verify."}]
                operations = [("read_file", {"path": str(path)}),
                              ("write_file", {"path": str(path), "text": "after"}),
                              ("read_file", {"path": str(path)})]
                results = []
                for name, arguments in operations:
                    adapter = cli_routes.adapter_for(provider)
                    plan = cli_routes.plan_turn(provider, model, {"messages": messages,
                        "system": "Use host tools to perform the requested edit.",
                        "tools": TOOLS}, {}, wanted_output=128)
                    if results:
                        self.assertIn(results[-1], plan["body"]["messages"][-1]["content"])
                    session = Session(vendor_events(provider, name, arguments), codex=provider == "codex")
                    with ExitStack() as stack:
                        stack.enter_context(patch.object(adapter, "StdioSession", return_value=session))
                        resolver = "runtime_binary" if provider == "codex" else "_resolve_binary"
                        stack.enter_context(patch.object(adapter, resolver, return_value="/fake/cli"))
                        wire = []
                        outcome = cli_routes.relay_cli_turn(cli_routes.run_turn(provider, plan["body"],
                            parse_tool_calls=True), wire.append, model=model)
                    self.assertIsNone(outcome["error"], outcome)
                    self.assertEqual(outcome["stop_reason"], "tool_use")
                    self.assertTrue(session.closed)
                    blocks = [event["content_block"] for event in wire if event["type"] == "content_block_start"]
                    call = next(block for block in blocks if block["type"] == "tool_use")
                    args = json.loads(next(event["delta"]["partial_json"] for event in wire
                        if event.get("delta", {}).get("type") == "input_json_delta"))
                    self.assertEqual((call["name"], args), (name, arguments))
                    # Only the host executes the operation, after receiving a
                    # successful tool handoff on the public wire.
                    if name == "write_file":
                        self.assertEqual(path.read_text(), "before")
                        path.write_text(args["text"])
                        result = "write completed"
                    else:
                        result = path.read_text()
                    results.append(result)
                    messages += [{"role": "assistant", "content": [{**call, "input": args}]},
                                 {"role": "user", "content": [{"type": "tool_result",
                                  "tool_use_id": call["id"], "content": result}]}]
                self.assertEqual(results, ["before", "write completed", "after"])
                self.assertEqual(path.read_text(), "after")

    def test_transcript_headers_allow_host_actions(self):
        for provider in MODELS:
            if provider == "codex":
                continue
            adapter = cli_routes.adapter_for(provider)
            text = adapter.render_prompt([{"role": "user", "content": "Edit the file"}], system="host tools")
            self.assertNotIn("You have no tools", text)
            self.assertNotIn("cannot take actions", text)
            self.assertIn("host application", text)


class FailedHandoffTests(unittest.TestCase):
    def parse(self, events, **kwargs):
        return list(cli_routes._parse_tool_stream(iter(events), tools=TOOLS, **kwargs))

    def test_malformed_truncated_and_unoffered_calls_never_reach_host(self):
        bad = [OPEN_SENTINEL + '{"name": "read_file", "input": {broken}}' + CLOSE_SENTINEL,
               OPEN_SENTINEL + '{"name": "read_file"', envelope("unoffered")]
        for wire in bad:
            with self.subTest(wire=wire):
                events = self.parse([{"type": "text_delta", "text": envelope() + wire},
                                     {"type": "message_stop", "stop_reason": "end_turn"}])
                self.assertEqual([e["type"] for e in events], ["error"])
                self.assertNotIn("broken", events[0]["message"])
                self.assertNotIn(OPEN_SENTINEL, events[0]["message"])

    def test_cancelled_or_truncated_stream_discards_pending_calls(self):
        for terminal in [[], [{"type": "error", "message": "cancelled"}],
                         [{"type": "message_stop", "stop_reason": "cancelled"}],
                         [{"type": "message_stop", "stop_reason": "unknown"}]]:
            events = self.parse([{"type": "text_delta", "text": envelope()}] + terminal)
            self.assertEqual([e["type"] for e in events], ["error"])

    def test_required_tool_cannot_finish_with_a_promise(self):
        events = self.parse([{"type": "text_delta", "text": "I will edit that now."},
                             {"type": "message_stop", "stop_reason": "end_turn"}],
                            tool_choice={"type": "any"})
        self.assertEqual(events[-1]["type"], "error")

    def test_relay_closes_producer_on_terminal_and_error(self):
        for terminal in ({"type": "message_stop", "stop_reason": "end_turn"},
                         {"type": "error", "message": "failed"}):
            closed = []
            def source():
                try:
                    yield terminal
                    self.fail("Relay consumed events after terminal")
                finally:
                    closed.append(True)
            cli_routes.relay_cli_turn(source(), lambda _: None, model="test")
            self.assertEqual(closed, [True])

    def test_native_history_preserves_call_result_identity_and_long_arguments(self):
        import codex_cli_agent
        text = "data " * 100
        items = codex_cli_agent._history_items([
            {"role": "assistant", "content": [{"type": "tool_use", "id": "c1",
             "name": "write_file", "input": {"text": text}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "c1",
             "is_error": True, "content": "Permission denied by host"}]}])
        self.assertEqual(items[0]["namespace"], "host")
        self.assertEqual(json.loads(items[0]["arguments"])["text"], text)
        self.assertEqual(items[0]["call_id"], items[1]["call_id"])
        self.assertEqual(items[1]["type"], "function_call_output")
        self.assertIn("Host tool error", items[1]["output"])

    def test_invalid_call_gets_one_correction_without_executing_failed_batch(self):
        requests = []
        closed = []
        def run(request, **kwargs):
            requests.append(request)
            try:
                yield {"type": "text_delta", "text": "I'll list the files first.\n"}
                wire = envelope() + OPEN_SENTINEL + "{broken}" + CLOSE_SENTINEL if len(requests) == 1 else envelope()
                yield {"type": "text_delta", "text": wire}
                yield {"type": "message_stop", "stop_reason": "end_turn"}
            finally:
                closed.append(True)
        request = {"tools": TOOLS, "messages": [{"role": "user", "content": "Read file"}]}
        events = list(cli_routes._tool_turn(SimpleNamespace(run_turn=run), request, timeout=10))
        self.assertEqual([event["type"] for event in events if event["type"] != "ping"],
                         ["text_delta", "tool_call", "message_stop"])
        self.assertEqual([event["text"] for event in events if event["type"] == "text_delta"],
                         ["I'll list the files first.\n"])
        self.assertEqual(len(requests), 2)
        self.assertEqual(len(closed), 2)
        self.assertEqual(len(request["messages"]), 1)
        correction = requests[1]["messages"][-1]["content"]
        self.assertIn("nothing in it was executed", correction)
        self.assertIn("do not perform that work again", correction)
        self.assertIn("tool-call delimiters", correction)

    def test_repeated_invalid_calls_stop_after_one_retry_and_cancellation_never_retries(self):
        for cancelled in (False, True):
            requests = []
            def run(request, **kwargs):
                requests.append(request)
                yield {"type": "text_delta", "text": OPEN_SENTINEL + "{broken}" + CLOSE_SENTINEL}
                yield {"type": "message_stop", "stop_reason": "end_turn"}
            def cancel(request, **kwargs):
                requests.append(request)
                yield {"type": "message_stop", "stop_reason": "cancelled"}
            events = list(cli_routes._tool_turn(SimpleNamespace(run_turn=cancel if cancelled else run),
                                                {"tools": TOOLS}, timeout=10))
            self.assertEqual(len(requests), 1 if cancelled else 2)
            self.assertEqual([event["type"] for event in events], ["error"])


if __name__ == "__main__":
    unittest.main()
