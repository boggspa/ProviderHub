"""Member request recovery never repeats a host action or stops its peers."""
import time
import unittest
from unittest.mock import patch

import chat_team
from chat_runtime import ModelRequestError
from chat_tools import ChatToolRunner
from test_chat_agents import call
from test_chat_runtime import response
import test_chat_team as fixtures


class MemberLifecycleTests(unittest.TestCase):
    service = fixtures.TeamTests.service
    configure = fixtures.TeamTests.configure
    send = fixtures.TeamTests.send
    finish = fixtures.TeamTests.finish
    wait_for = fixtures.TeamTests.wait_for

    def setUp(self):
        fixtures.TeamTests.setUp(self)
        for target, value in (("chat_runtime.REQUEST_BACKOFF", (.001, .001, .001, .001)),
                              ("chat_runtime.random.uniform", 0)):
            stub = patch(target, return_value=value) if target.endswith("uniform") else patch(target, value)
            stub.start(); self.addCleanup(stub.stop)

    def warnings(self, parent):
        return [e for e in parent.chat["entries"] if e.get("noticeKind") == "team_failure"]

    def test_retry_success_is_silent_and_replaces_partial_output(self):
        def broken(payload, cancel, delta):
            delta("Discard this unfinished reply")
            raise TimeoutError("The read operation timed out")
        parent = self.service([[broken, ConnectionResetError("connection reset"), response("Recovered")], [response("Peer done")]])
        members = self.configure(parent)
        self.send(parent); self.finish(parent)
        self.assertEqual([m["status"] for m in members], ["done", "done"])
        self.assertEqual(len(self.transports[0].requests), 3)
        self.assertTrue(all(request == self.transports[0].requests[0] for request in self.transports[0].requests))
        replies = [e for e in parent.chat["entries"] if e["kind"] == "assistant" and e.get("memberID") == members[0]["id"]]
        self.assertEqual([e["text"] for e in replies], ["Recovered"])
        self.assertFalse(any(e["kind"] in {"notice", "error"} for e in parent.chat["entries"]))
        self.assertTrue(any("Retrying (2/5)" in e.get("status", "") for e in self.events))
        self.assertEqual(len([m for m in members[0]["messages"] if m["role"] == "assistant"]), 1)

    def test_exhaustion_parks_one_member_and_checkpoints_one_warning_while_peer_continues(self):
        parent = self.service([[TimeoutError("connection reset") for _ in range(5)],
            [fixtures.decision("continue", "Finish verification"), response("First result"), response("Verification complete")]])
        members = self.configure(parent)
        self.send(parent); self.finish(parent)
        self.assertEqual(len(self.transports[0].requests), 5)
        self.assertEqual([m["status"] for m in members], ["error", "done"])
        self.assertEqual([m["contributions"] for m in members], [0, 2])
        self.assertEqual(parent.chat["team"]["queue"], [members[0]["id"]])
        self.assertEqual(members[0]["failureReason"], "connection reset")
        warnings = self.warnings(parent)
        self.assertEqual(len(warnings), 1)
        warning = warnings[0]
        self.assertTrue(warning["recorded"])
        self.assertEqual(warning["memberID"], members[0]["id"])
        self.assertIn("after 5 attempts", warning["text"])
        self.assertEqual(warning["detail"].count("Attempt "), 5)
        self.assertEqual(warning["detail"].count("Waited "), 4)
        self.assertIn("TimeoutError", warning["detail"])
        loaded = self.store.load(parent.chat["id"])
        self.assertEqual([e for e in loaded["entries"] if e.get("noticeKind") == "team_failure"], warnings)
        self.assertTrue(any(e.get("text") == "Verification complete" for e in loaded["entries"]))
        self.assertFalse(any(e["kind"] == "error" for e in loaded["entries"]))

    def test_stop_during_backoff_cancels_without_waiting_or_reporting_failure(self):
        parent = self.service([[TimeoutError("read timed out"), response("Must not retry")], [response("Peer done")]])
        self.configure(parent)
        with patch("chat_runtime.REQUEST_BACKOFF", (30, 30, 30, 30)):
            self.send(parent)
            self.wait_for(lambda: any("Retrying (2/5)" in e.get("status", "") for e in self.events))
            started = time.monotonic()
            parent.handle({"command": "stop"}); self.finish(parent)
        self.assertLess(time.monotonic() - started, 1.5)
        self.assertEqual(len(self.transports[0].requests), 1)
        self.assertEqual(self.warnings(parent), [])

    def test_authentication_and_invalid_request_fail_fast_even_inside_502(self):
        for failure in (ModelRequestError("Expired credential", status=401, code="authentication_error"),
                        ModelRequestError("Invalid input", status=400, code="invalid_request_error"),
                        ModelRequestError("Claude CLI: not logged in", status=502, code="api_error")):
            with self.subTest(failure=str(failure)):
                parent = self.service([[failure, response("Must not retry")], [response("Peer done")]])
                self.configure(parent); self.send(parent); self.finish(parent)
                self.assertEqual(len(self.transports[0].requests), 1)
                self.assertIn("after 1 attempt", self.warnings(parent)[0]["text"])

    def test_rate_limits_overload_and_cli_exit_retry(self):
        parent = self.service([[ModelRequestError("Try later", status=429, code="rate_limit_error"),
            ModelRequestError("Busy", status=503), ModelRequestError("the claude CLI exited with code 1", code="api_error"),
            response("Recovered")], [response("Peer done")]])
        self.configure(parent); self.send(parent); self.finish(parent)
        self.assertEqual(len(self.transports[0].requests), 4)
        self.assertEqual(self.warnings(parent), [])

    def test_empty_and_thinking_only_responses_retry(self):
        thinking = response(""); thinking["content"] = [{"type": "thinking", "thinking": "private", "signature": "opaque"}]
        parent = self.service([[response(""), thinking, response("  "), response("Recovered")], [response("Peer done")]])
        self.configure(parent); self.send(parent); self.finish(parent)
        self.assertEqual(len(self.transports[0].requests), 4)
        self.assertEqual(self.warnings(parent), [])

    def test_request_retry_keeps_completed_tool_result_and_executes_shell_once(self):
        executions = []
        class Runner(ChatToolRunner):
            def execute(inner, name, arguments):
                executions.append(name)
                return super().execute(name, arguments)
        parent = self.service([[call("run_shell", {"command": "printf saved >> once.txt"}),
            TimeoutError("read timed out"), ConnectionResetError("connection reset"), response("Done")], [response("Peer done")]])
        parent.runner_type = Runner; parent.chat["approvalMode"] = "yolo"
        self.configure(parent); self.send(parent); self.finish(parent)
        self.assertEqual(executions, ["run_shell"])
        self.assertEqual((self.root / "once.txt").read_text(), "saved")
        requests = self.transports[0].requests
        self.assertEqual(requests[1], requests[2]); self.assertEqual(requests[2], requests[3])
        self.assertTrue(any(b["type"] == "tool_result" for m in requests[-1]["messages"] for b in m["content"]))

    def test_new_user_input_resumes_parked_member_and_clears_reason(self):
        parent = self.service([[TimeoutError("connection reset") for _ in range(5)], [response("Peer done")],
            [response("Resumed")], [response("Peer new result")]])
        self.configure(parent); self.send(parent); self.finish(parent)
        self.send(parent, "Try the next step"); self.finish(parent)
        self.assertEqual([m["status"] for m in parent.chat["team"]["members"]], ["done", "done"])
        self.assertFalse(parent.chat["team"]["members"][0].get("failureReason"))
        self.assertEqual(len(self.warnings(parent)), 1)

    def test_terminal_checkpoint_records_warning_even_when_stream_lane_closed(self):
        parent = self.service([[ModelRequestError("Bad request", status=400)]])
        member = self.configure(parent, 1)[0]
        chat_team.start_run(parent, new_input=True)
        child = chat_team.contribution(parent, member, self.transports[0])
        member["terminalStatus"] = "error"  # Entry forwarding is now guarded out.
        child.run()
        child.store.save(child.chat)
        warnings = self.warnings(parent)
        self.assertEqual(len(warnings), 1)
        loaded = self.store.load(parent.chat["id"])
        self.assertEqual([e for e in loaded["entries"] if e.get("noticeKind") == "team_failure"], warnings)
        self.assertEqual(loaded["team"]["members"][0]["status"], "error")

    def test_tool_argument_errors_remain_recoverable_and_ms_timeout_runs(self):
        invalid = call("run_shell", {"command": "touch must-not-run", "timeout": "bad"})
        parent = self.service([[invalid, call("run_shell", {"command": "printf ok", "timeout": 120000}), response("Done")], [response("Peer done")]])
        parent.chat["approvalMode"] = "yolo"
        self.configure(parent); self.send(parent); self.finish(parent)
        self.assertFalse((self.root / "must-not-run").exists())
        tools = [e for e in parent.chat["entries"] if e["kind"] == "tool"]
        self.assertEqual([e["isError"] for e in tools], [True, False])
        self.assertIn("Invalid timeout", tools[0]["detail"])
        self.assertEqual(self.warnings(parent), [])
        result = next(b for m in self.transports[0].requests[1]["messages"] for b in m["content"] if b["type"] == "tool_result")
        self.assertTrue(result["is_error"])

    def test_non_object_tool_arguments_are_a_tool_result(self):
        invalid = call("team_status", {})
        invalid["content"][0]["input"] = []
        parent = self.service([[invalid, response("Corrected")], [response("Peer done")]])
        self.configure(parent); self.send(parent); self.finish(parent)
        self.assertEqual(self.warnings(parent), [])
        tools = [e for e in parent.chat["entries"] if e["kind"] == "tool"]
        self.assertTrue(tools[0]["isError"])
        self.assertIn("must be an object", tools[0]["detail"])
        history = self.transports[0].requests[1]["messages"]
        self.assertTrue(all(isinstance(b["input"], dict) for m in history for b in m["content"] if b["type"] == "tool_use"))
