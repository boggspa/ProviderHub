"""AntiGravity's renamed native reader and its unreported arguments.

On 20 Oct 2026 agy began offering read_file {Path, StartLine, EndLine,
ByteOffset} instead of view_file {AbsolutePath, StartLine, EndLine}. The hook
then refused every private context read, models fell back to view_file, agy
answered "unknown tool", and its stream carried no arguments for that call:
"native tool parameters must be an object" stopped Team members ten times.
"""
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

import agy_cli_agent as adapter
import cli_routes
from chat_runtime import GatewayClient, ModelRequestError, retryable_request
from chat_tools import TOOL_DEFINITIONS
from cli_tool_call import ToolCallError
from codex_session_pool import SessionPool
import test_agy_protocols as protocols
import test_cli_host_tools as fixtures
from test_cli_host_tools import Session, TOOLS

CONTEXT = "x" * 210_000


def unknown(name, index, **info):
    """The step agy streamed for the call that failed on 20 Oct 2026."""
    return {"event": "step_update", "step_update": {
        "conversation_id": "c1", "step_index": index, "step_type": "tool", "state": "ERROR",
        "tool_info": {"name": name, **info, "error": {"message": f'unknown tool: "{name}" — check spelling'}}}}


def ask_hook(root, name, args, index):
    payload = {"conversationId": "c1", "stepIdx": index, "toolCall": {"name": name, "args": args}}
    result = subprocess.run([sys.executable, str(root / "host_handoff.py")], input=json.dumps(payload),
                            text=True, capture_output=True, check=True)
    return json.loads(result.stdout)


def read_parts(root, first_index=10):
    """Read every private part the way agy now does, through the real hook."""
    for index, path in enumerate(json.loads((root / ".agents/context.json").read_text()), first_index):
        decision = ask_hook(root / ".agents", "read_file", {"Path": path}, index)
        assert decision == {"decision": "allow"}, decision
        yield {"event": "step_update", "step_update": {
            "conversation_id": "c1", "step_index": index, "step_type": "tool", "state": "DONE",
            "tool_info": {"name": "read_file", "parameters": {"Path": path}}}}


class RenamedReaderTests(unittest.TestCase):
    payload = protocols.AgyDesktopProtocolTests.payload
    run_plan = protocols.AgyDesktopProtocolTests.run_plan
    check_wire = protocols.AgyDesktopProtocolTests.check_wire
    handoff = fixtures.AgyNativeHandoffTests.handoff

    def test_hook_lets_either_reader_open_only_private_parts(self):
        with tempfile.TemporaryDirectory() as directory:
            part = str((Path(directory) / "context-0001.txt").resolve())
            Path(part).write_text("required context")
            other = str((Path(directory) / "elsewhere.txt").resolve())
            hook = adapter._install_host_hook(directory, [], [part])
            for index, (name, args, decision) in enumerate([
                    ("read_file", {"Path": part}, "allow"),
                    ("read_file", {"Path": part, "StartLine": 1, "EndLine": 5, "ByteOffset": 0}, "allow"),
                    ("view_file", {"AbsolutePath": part}, "allow"),
                    ("view_file", {"Path": part}, "allow"),
                    ("read_file", {"Path": other}, "deny"),
                    # A second spelling cannot smuggle another file past the check.
                    ("read_file", {"Path": other, "AbsolutePath": part}, "deny"),
                    ("view_file", {"AbsolutePath": part, "Path": other}, "deny"),
                    ("read_file", {"Path": ["not", "a", "path"]}, "deny"),
                    ("run_command", {"Path": part}, "deny")]):
                with self.subTest(name=name, args=args):
                    answer = ask_hook(hook, name, args, index)
                    self.assertEqual(answer["decision"], decision)
                    if decision == "deny":
                        self.assertIn("read_file", answer["reason"])
                        self.assertNotIn("with native view_file", answer["reason"])

    def test_prompts_name_the_reader_agy_offers_today(self):
        from agy_context import CONTEXT_NOTE
        self.assertIn("read_file", CONTEXT_NOTE)
        self.assertNotIn("with native view_file", CONTEXT_NOTE)

    def test_context_read_with_read_file_completes_in_one_cli_turn(self):
        for surface in ("messages", "responses"):
            with self.subTest(surface=surface):
                plan = cli_routes.plan_turn("antigravity", "gemini-3.8-flash", self.payload(surface, CONTEXT),
                                            {}, wanted_output=128)
                launches = []

                def factory(argv, **kwargs):
                    root = Path(kwargs["cwd"])
                    launches.append(root)
                    session = Session([])

                    def script():
                        yield from read_parts(root)
                        self.assertEqual(json.loads((root / ".agents/context-pending.json").read_text()), {})
                        yield protocols.finish_result({"text": "ALPHA BETA", "tool_calls": []})
                    session.script = script()
                    return session

                events = self.run_plan(plan, factory)
                self.assertEqual(len(launches), 1)
                self.assertEqual(events, [{"type": "text_delta", "text": "ALPHA BETA"},
                                          {"type": "message_stop", "stop_reason": "end_turn"}])
                self.check_wire(events, surface, "ALPHA BETA", 0, model="antigravity/gemini-3.8-flash")

    def test_unknown_view_file_without_reported_arguments_is_left_to_agy(self):
        # Every value that could have produced "must be an object" there.
        for shape in ({}, {"parameters": None}, {"parameters": ""}, {"parameters": []}, {"parameters": {}}):
            with self.subTest(shape=shape):
                plan = cli_routes.plan_turn("antigravity", "gemini-3.8-flash", self.payload("messages", CONTEXT),
                                            {}, wanted_output=128)
                launches = []

                def factory(argv, **kwargs):
                    root = Path(kwargs["cwd"])
                    launches.append(root)
                    session = Session([])

                    def script():
                        active = unknown("view_file", 6, **shape)
                        active["step_update"] = {**active["step_update"], "state": "ACTIVE"}
                        active["step_update"]["tool_info"] = {"name": "view_file"}
                        yield active
                        yield unknown("view_file", 6, **shape)
                        yield from read_parts(root)
                        yield protocols.finish_result({"text": "ALPHA BETA", "tool_calls": []})
                    session.script = script()
                    return session

                events = self.run_plan(plan, factory)
                self.assertEqual(len(launches), 1)
                self.assertEqual([event["type"] for event in events], ["text_delta", "message_stop"])

    def test_repeated_unknown_tools_take_the_route_correction_with_their_code(self):
        plan = cli_routes.plan_turn("antigravity", "gemini-3.8-flash", {
            "tools": TOOLS, "messages": [{"role": "user", "content": "Inspect the repository."}]},
            {}, wanted_output=128)
        sessions = []

        def factory(argv, **kwargs):
            session = protocols.PromptSession([unknown("view_file", index) for index in (2, 3, 4)]
                                              + [protocols.finish_result({"text": "unreachable", "tool_calls": []})])
            sessions.append(session)
            return session

        events = self.run_plan(plan, factory)
        self.assertEqual(len(sessions), 2)
        self.assertIn("rejected for its formatting alone", sessions[1].prompt)
        self.assertEqual([event["type"] for event in events], ["error"])
        self.assertEqual(events[0]["code"], "invalid_cli_tool_call")
        self.assertEqual(events[0]["http_status"], 400)
        self.assertIn("'view_file', agy did not report the native tool's arguments", events[0]["message"])
        result =cli_routes.relay_cli_turn(iter(events), lambda _: None, model="antigravity/gemini-3.8-flash")
        self.assertEqual((result["http_status"], result["error_code"]), (400, "invalid_cli_tool_call"))

    def test_unknown_step_cannot_clear_another_pending_capture(self):
        state = adapter._TurnState()
        state.structured, state.tools = True, TOOLS
        with tempfile.TemporaryDirectory() as directory:
            state.hook_root = Path(directory)
            pending = {"conversation_id": "c1", "step_index": 2, "step_type": "tool", "state": "ACTIVE",
                       "tool_info": {"name": "run_command", "parameters": {"CommandLine": "pwd"}}}
            adapter._translate({"event": "step_update", "step_update": pending}, state)
            adapter._translate(unknown("view_file", 3), state)
            adapter._translate(protocols.finish_result({"text": "Done", "tool_calls": []}), state)
        self.assertIn("unresolved native tool request", state.failure)
        self.assertFalse(state.terminal)

    def test_native_reader_maps_to_each_host_read(self):
        call = {"Path": "/workspace/README.md", "StartLine": 3, "EndLine": 8, "ByteOffset": 0,
                "toolAction": "Reading", "toolSummary": "README"}
        for tools, name, arguments in (
                (TOOL_DEFINITIONS, "read_file", {"path": "/workspace/README.md", "offset": 3, "limit": 6}),
                ([{"name": "Read", "input_schema": {"type": "object"}}], "Read",
                 {"file_path": "/workspace/README.md", "offset": 3, "limit": 6}),
                ([{"name": "exec_command", "input_schema": {"type": "object"}}], "exec_command",
                 {"cmd": "sed -n 3,8p /workspace/README.md"})):
            with self.subTest(host=name):
                reply = json.loads(adapter._host_reply_for_native("read_file", call, tools))
                self.assertEqual(reply["tool_calls"], [{"name": name, "arguments": json.dumps(arguments)}])
        # Lean Chat's own read_file call keeps its exact host shape.
        reply = json.loads(adapter._host_reply_for_native("read_file", {"path": "README.md"}, TOOL_DEFINITIONS))
        self.assertEqual(json.loads(reply["tool_calls"][0]["arguments"]), {"path": "README.md"})

    def test_captured_read_file_reaches_lean_chat_in_its_schema(self):
        events, sessions = self.handoff("read_file", {"Path": "/workspace/README.md"}, TOOL_DEFINITIONS)
        self.assertEqual([event["type"] for event in events], ["tool_call", "message_stop"])
        self.assertEqual((events[0]["name"], events[0]["input"]), ("read_file", {"path": "/workspace/README.md"}))
        self.assertEqual(len(sessions), 1)

    def test_argument_shapes_decode_only_when_unambiguous(self):
        bash = [{"name": "Bash", "input_schema": {"type": "object"}}]
        reply = json.loads(adapter._host_reply_for_native("run_command", '{"CommandLine": "pwd"}', bash))
        self.assertEqual(json.loads(reply["tool_calls"][0]["arguments"]), {"command": "pwd"})
        minimal = [{"name": "read_file", "input_schema": {"type": "object", "properties": {
            "path": {"type": "string"}}, "additionalProperties": False}}]
        for name, parameters, tools, message in (
                ("run_command", None, bash, "did not report"),
                ("run_command", "", bash, "not valid JSON"),
                ("run_command", "[1]", bash, "must be an object"),
                ("run_command", ["pwd"], bash, "must be an object"),
                ("read_file", {"Path": "/a", "AbsolutePath": "/b"}, bash, "no supported host tool mapping"),
                ("read_file", {"Path": "/a", "ByteOffset": -1}, bash, "range is invalid"),
                # Never falls back to the same-named host tool with agy's keys.
                ("read_file", {"Path": "/a", "StartLine": 2}, minimal, "no supported host tool mapping")):
            with self.subTest(name=name, parameters=parameters):
                with self.assertRaisesRegex(ToolCallError, message):
                    adapter._host_reply_for_native(name, parameters, tools)

    def test_unusable_stream_parameters_never_crash_the_translator(self):
        with tempfile.TemporaryDirectory() as directory:
            part = Path(directory) / "context-0001.txt"
            part.write_text("one\ntwo\n")
            for parameters in (json.dumps({"Path": str(part.resolve())}), ["not", "an", "object"], "{broken"):
                with self.subTest(parameters=parameters):
                    state = adapter._TurnState()
                    state.context_reads = adapter.ContextReads([str(part.resolve())])
                    events = adapter._translate({"event": "step_update", "step_update": {
                        "step_type": "tool", "state": "DONE",
                        "tool_info": {"name": "read_file", "parameters": parameters}}}, state)
                    self.assertEqual(events, [])
                    self.assertEqual(state.context_reads.complete(), isinstance(parameters, str)
                                     and not parameters.startswith("{b"))

    def test_scoped_image_reads_accept_the_renamed_reader(self):
        image = str(Path("/tmp/screenshot.png").resolve())
        state = adapter._TurnState()
        state.image_paths = [image]
        events = adapter._translate({"event": "step_update", "step_update": {
            "step_type": "tool", "state": "DONE", "tool_info": {"name": "read_file", "parameters": {"Path": image}}}},
            state)
        self.assertEqual(events, [{"type": "thinking_delta", "text": "Inspected attached image 1.\n"}])
        self.assertEqual(adapter._translate(unknown("view_file", 4), state), [])
        self.assertIsNone(state.failure)


class AgyResumedSession:
    """A retained agy process: the first turn answers, the second is scripted."""

    def __init__(self, second, argv, **kwargs):
        self.argv, self.cwd, self.stdin = argv, kwargs.get("cwd"), io.StringIO()
        self.returncode, self.closed, self.turns, self.second = None, False, 0, second

    def events(self, timeout=None):
        self.turns += 1
        if self.turns == 1:
            yield {"event": "result", "result": {"conversation_id": "synthetic-agy", "status": "SUCCESS",
                   "response": json.dumps({"text": "First reply", "tool_calls": []})}}
        else:
            yield from self.second

    def close(self):
        self.closed, self.returncode = True, 0


class ResumedTurnFailureTests(unittest.TestCase):
    def turn(self, second):
        from test_provider_continuity import TASK, next_history
        pool = SessionPool(lambda entry: entry.session.close(), capacity=4, idle_ttl=0, pending_ttl=60, label="AGY")
        self.addCleanup(pool.close)
        spawned = []
        with patch.object(adapter, "StdioSession", side_effect=lambda argv, **kwargs: spawned.append(
                AgyResumedSession(second, argv, **kwargs)) or spawned[-1]), \
                patch.object(adapter, "_streaming_supported", return_value=True), \
                patch.object(adapter, "_resolve_binary", return_value=__file__):
            requests = [{"model": "gemini-3.8-flash", "conversation": "chat:member", "history": history,
                         "messages": [{"role": "user", "content": "First exact request"}],
                         "tools": TOOLS, "host_tool_schema": {"type": "object"}} for history in (TASK, next_history())]
            first = list(adapter.run_turn(requests[0], pool=pool))
            second_events = list(adapter.run_turn(requests[1], pool=pool))
        self.assertEqual(first[-1]["type"], "message_stop")
        self.assertEqual(len(spawned), 1)
        return second_events

    def test_resumed_turn_reports_the_same_status_and_code_as_a_cold_turn(self):
        error = self.turn([unknown("view_file", index) for index in (5, 6, 7)])[-1]
        self.assertEqual((error["type"], error.get("http_status"), error.get("code")),
                         ("error", 400, "invalid_cli_tool_call"))

    def test_resumed_unconfirmed_native_action_is_not_marked_retryable(self):
        executed = {"event": "step_update", "step_update": {
            "conversation_id": "c1", "step_index": 5, "step_type": "tool", "state": "DONE",
            "tool_info": {"name": "run_command", "parameters": {"CommandLine": "touch completed"}}}}
        error = self.turn([executed])[-1]
        self.assertEqual((error["type"], error.get("http_status")), ("error", 400))
        self.assertNotIn("code", error)
        result = cli_routes.relay_cli_turn(iter([error]), lambda _: None, model="antigravity/gemini-3.8-flash")
        failure = ModelRequestError("AntiGravity CLI route failed: " + result["error"],
                                    status=result["http_status"], code=result["error_code"])
        self.assertFalse(retryable_request(failure))


class TeamRetryClassificationTests(unittest.TestCase):
    def sse_failure(self, error):
        wire = io.BytesIO(("event: error\ndata: " + json.dumps({"type": "error", "error": error}) + "\n\n").encode())
        wire.status = 200
        wire.getheader = lambda key, default="": "text/event-stream"

        class Connection:
            def request(self, *args): pass
            def getresponse(self): return wire
            def close(self): pass
        client = GatewayClient(Path("/unused"))
        client.connect, client.headers = Connection, lambda: {}
        with self.assertRaises(ModelRequestError) as caught:
            client.stream({}, threading.Event(), lambda text: None)
        return caught.exception

    def test_pre_execution_rejection_is_retryable_on_both_gateway_paths(self):
        message = "AntiGravity CLI route failed: agy kept requesting native tools it does not have."
        streamed = self.sse_failure({"type": "invalid_request_error", "message": message,
                                     "code": "invalid_cli_tool_call"})
        self.assertEqual(streamed.code, "invalid_cli_tool_call")
        self.assertTrue(retryable_request(streamed))
        self.assertTrue(retryable_request(ModelRequestError(message, status=400, code="invalid_cli_tool_call")))

    def test_failures_that_may_have_executed_stay_final(self):
        for message in ("agy did not confirm the captured native action was blocked; no host action was dispatched.",
                        "agy attempted a native tool without a captured host handoff; no host action was dispatched."):
            with self.subTest(message=message):
                streamed = self.sse_failure({"type": "invalid_request_error", "message": message})
                self.assertFalse(retryable_request(streamed))
                self.assertFalse(retryable_request(ModelRequestError(message, status=400, code="invalid_request_error")))


if __name__ == "__main__":
    unittest.main()
