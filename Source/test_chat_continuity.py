"""Long Team work: checkpoints, independent scheduling and event-driven waits."""
import copy
import json
import threading
import time
import unittest
from unittest.mock import patch

import chat_execution
import chat_team
from chat_processes import ProcessRegistry, NONREAPING
from chat_runtime import ChatService
from chat_tools import ChatToolRunner
from test_chat_agents import call
from test_chat_runtime import response
import test_chat_team as fixtures

decision = fixtures.decision


class ContinuityTests(unittest.TestCase):
    setUp = fixtures.TeamTests.setUp
    service = fixtures.TeamTests.service
    configure = fixtures.TeamTests.configure
    send = fixtures.TeamTests.send
    finish = fixtures.TeamTests.finish
    wait_for = fixtures.TeamTests.wait_for

    def task_team(self, scripts, count=1, **settings):
        parent = self.service(scripts)
        self.configure(parent, count)
        parent.chat["team"]["execution"] = chat_execution.settings(settings)
        self.addCleanup(self.stop, parent)
        return parent

    @staticmethod
    def stop(parent):
        if parent.busy:
            parent.handle({"command": "stop"})
            parent.thread.join(4)

    def registry(self, parent):
        registry = ProcessRegistry(lambda _chat: parent.team_wake.set())
        self.addCleanup(registry.shutdown)
        parent.process_registry = registry
        parent.runner_type = lambda workspace, cancel_event: ChatToolRunner(workspace, cancel_event, registry)
        return registry

    def test_new_team_defaults_to_task_and_legacy_settings_are_preserved(self):
        parent = self.service([])
        specs = [{"choice": parent.models[0]["id"], "name": "Sol"}]
        chat_team.configure(parent, {"enabled": True, "members": specs})
        self.assertEqual(parent.chat["team"]["execution"]["mode"], "task")
        parent.chat["team"].pop("execution")
        chat_team.configure(parent, {"enabled": True, "members": specs})
        self.assertEqual(parent.chat["team"]["execution"]["mode"], "contribution")

    def test_task_checkpoint_keeps_tools_and_same_history_without_signoff(self):
        (self.root / "a").write_text("finding a")
        (self.root / "b").write_text("finding b")
        parent = self.task_team([[call("read_file", {"path": "a"}), call("read_file", {"path": "b"}), response("Complete")]])
        with patch("chat_runtime.MAX_ROUNDS", 1):
            self.send(parent); self.finish(parent)
        member = parent.chat["team"]["members"][0]
        self.assertEqual((member["status"], member["checkpoints"], member["contributions"]), ("done", 2, 1))
        self.assertEqual(len(self.transports[0].requests), 3)
        self.assertTrue(all(any(t["name"] == "read_file" for t in req["tools"]) for req in self.transports[0].requests))
        self.assertNotIn("tool budget is used up", json.dumps(parent.chat["entries"]))
        self.assertIn("finding a", json.dumps(self.transports[0].requests[-1]))

    def test_fast_member_resumes_before_slow_member_finishes(self):
        slow_started, fast_resumed, release = threading.Event(), threading.Event(), threading.Event()
        def slow(payload, cancel, delta):
            slow_started.set()
            self.assertTrue(release.wait(3))
            return response("Slow complete")
        def fast(payload, cancel, delta):
            self.assertTrue(slow_started.wait(2))
            fast_resumed.set()
            return response("Fast complete")
        parent = self.task_team([[decision("continue", "Verify"), response("First part"), fast], [slow]], count=2)
        try:
            self.send(parent)
            self.assertTrue(fast_resumed.wait(2), "Fast member was held behind the slow peer")
            self.assertTrue(parent.busy)
        finally:
            release.set(); self.finish(parent)

    @unittest.skipUnless(NONREAPING, "Needs CPython 3.13 waitid")
    def test_process_completion_resumes_only_waiter_without_polling_rounds(self):
        def resumed(payload, cancel, delta):
            self.assertIn("verified-output", json.dumps(payload))
            self.assertIn('"code": 0', json.dumps(payload, ensure_ascii=False).replace('\\"', '"'))
            return response("Verified")
        parent = self.task_team([[call("run_shell", {"command": "sleep 0.3; echo verified-output", "background": True}),
                                 call("team_status", {"state": "waiting", "process_id": "p1", "next_step": "Check result"}), resumed],
                                [response("Peer done")]], count=2)
        registry = self.registry(parent)
        parent.chat["approvalMode"] = "yolo"
        with patch("chat_processes.FIRST_LOOK", .01):
            self.send(parent); self.finish(parent)
        self.assertEqual([len(t.requests) for t in self.transports], [3, 1])
        self.assertEqual(registry.inspect(parent.chat["id"], "p1")["status"], "exited")
        self.assertEqual([m["status"] for m in parent.chat["team"]["members"]], ["done", "done"])

    def test_wait_for_member_finishes_once_with_recorded_outcome(self):
        release = threading.Event()
        def peer(payload, cancel, delta):
            self.assertTrue(release.wait(3))
            return response("Dependency implemented")
        parent = self.task_team([[], [peer]], count=2)
        first, second = parent.chat["team"]["members"]
        self.transports[0].responses = [call("team_status", {"state": "waiting", "member_id": second["id"], "next_step": "Verify peer's result"}), response("Verified")]
        try:
            self.send(parent)
            self.wait_for(lambda: first["status"] == "waiting")
            self.assertEqual(len(self.transports[0].requests), 1)
        finally:
            release.set(); self.finish(parent)
        self.assertEqual(len(self.transports[0].requests), 2)
        self.assertIn("Dependency implemented", json.dumps(self.transports[0].requests[-1]))

    @unittest.skipUnless(NONREAPING, "Needs CPython 3.13 waitid")
    def test_stop_interrupts_wait_without_stopping_background_process(self):
        parent = self.task_team([[call("run_shell", {"command": "sleep 30", "background": True}),
                                 call("team_status", {"state": "waiting", "process_id": "p1", "next_step": "Verify"})]])
        registry = self.registry(parent)
        parent.chat["approvalMode"] = "yolo"
        with patch("chat_processes.FIRST_LOOK", .01):
            self.send(parent)
            self.wait_for(lambda: parent.chat["team"]["members"][0]["status"] == "waiting")
            parent.handle({"command": "stop"}); self.finish(parent)
        self.assertEqual(parent.chat["team"]["status"], "stopped")
        self.assertEqual(registry.inspect(parent.chat["id"], "p1")["status"], "running")
        self.assertEqual(len(self.transports[0].requests), 2)

    def test_cyclic_member_wait_is_rejected(self):
        parent = self.task_team([[], []], count=2)
        team = parent.chat["team"]
        chat_team.start_run(parent, new_input=True)
        first, second = team["members"]
        child = chat_team.contribution(parent, first, self.transports[0])
        second.update(status="waiting", waitFor={"kind": "member", "id": first["id"]})
        with self.assertRaisesRegex(ValueError, "cycle"):
            chat_team.decide(child, {"state": "waiting", "member_id": second["id"], "next_step": "Verify"})
        self.assertNotIn("waitFor", first)

    def test_recovery_does_not_resume_process_from_an_old_worker(self):
        parent = self.task_team([[]])
        member = parent.chat["team"]["members"][0]
        parent.chat["team"]["status"] = "waiting"
        member.update(status="waiting", waitFor={"kind": "process", "id": "p1"})
        self.assertTrue(chat_team.recover(parent.chat, ChatService.settle))
        self.assertEqual(member["status"], "interrupted")
        self.assertNotIn("waitFor", member)

    def test_token_limit_records_rejected_tools_without_executing_them(self):
        action = response(call="*** Begin Patch\n*** Add File: forbidden.txt\n+no\n*** End Patch")
        action["usage"] = {"input_tokens": 15, "output_tokens": 5}
        parent = self.task_team([[action]], tokens=20)
        parent.chat["approvalMode"] = "yolo"
        self.send(parent); self.finish(parent)
        self.assertFalse((self.root / "forbidden.txt").exists())
        self.assertEqual(parent.chat["team"]["status"], "limit_reached")
        self.assertEqual(parent.chat["team"]["runUsage"]["tokens"], 20)
        self.assertIn("This action was not executed", json.dumps(parent.chat["entries"]))
        self.assertEqual(len(self.transports[0].requests), 1)

    def test_missing_usage_pauses_when_token_limit_was_requested(self):
        action = call("read_file", {"path": "a"}); action["usage"] = {}
        parent = self.task_team([[action]], tokens=1000)
        self.send(parent); self.finish(parent)
        self.assertEqual(parent.chat["team"]["status"], "limit_reached")
        self.assertIn("unavailable", parent.chat["team"]["limitReason"])

    def test_incomplete_usage_is_not_presented_as_a_complete_total(self):
        action = call("read_file", {"path": "a"})
        action["usage"] = {"input_tokens": 12}
        parent = self.task_team([[action]], tokens=1000)
        self.send(parent); self.finish(parent)
        self.assertEqual(parent.chat["team"]["runUsage"]["tokens"], 12)
        self.assertFalse(parent.chat["team"]["runUsage"]["usageComplete"])
        self.assertEqual(parent.chat["team"]["status"], "limit_reached")

    @unittest.skipUnless(NONREAPING, "Needs CPython 3.13 waitid")
    def test_failed_process_wakes_with_its_exit_code(self):
        def resumed(payload, cancel, delta):
            contents = json.dumps(payload, ensure_ascii=False).replace('\\"', '"')
            self.assertIn('"code": 3', contents)
            self.assertIn("test-failed", contents)
            return response("Investigated failure")
        parent = self.task_team([[call("run_shell", {"command": "sleep 0.1; echo test-failed; exit 3", "background": True}),
                                 call("team_status", {"state": "waiting", "process_id": "p1", "next_step": "Inspect result"}), resumed]])
        self.registry(parent)
        parent.chat["approvalMode"] = "yolo"
        with patch("chat_processes.FIRST_LOOK", .01):
            self.send(parent); self.finish(parent)
        self.assertEqual(parent.chat["team"]["status"], "done")

    def test_limit_after_provider_admission_is_reported_as_limit_not_error(self):
        def queued(payload, cancel, delta):
            parent.chat["team"]["runUsage"]["started"] -= 61
            chat_execution.guard(cancel.owner)
            self.fail("The expired request was sent")
        parent = self.task_team([[queued]], minutes=1)
        self.send(parent); self.finish(parent)
        self.assertEqual(parent.chat["team"]["status"], "limit_reached")
        self.assertFalse(any(e.get("noticeKind") == "team_failure" for e in parent.chat["entries"]))

    def test_time_limit_is_checked_before_next_request(self):
        def elapsed(payload, cancel, delta):
            parent.chat["team"]["runUsage"]["started"] -= 61
            return call("read_file", {"path": "a"})
        parent = self.task_team([[elapsed]], minutes=1)
        self.send(parent); self.finish(parent)
        self.assertEqual(parent.chat["team"]["status"], "limit_reached")
        self.assertEqual(parent.chat["team"]["limitReason"], "Time limit reached")

    def test_limit_is_rechecked_after_an_approval_wait(self):
        action = call("apply_patch", {"patch": "*** Begin Patch\n*** Add File: after-limit.txt\n+no\n*** End Patch"})
        parent = self.task_team([[action]], minutes=1)
        self.send(parent)
        self.wait_for(lambda: parent.approval is not None)
        parent.chat["team"]["runUsage"]["started"] -= 61
        parent.handle({"command": "approve", "id": parent.approval["id"], "allow": True})
        self.finish(parent)
        self.assertFalse((self.root / "after-limit.txt").exists())
        self.assertEqual(parent.chat["team"]["status"], "limit_reached")

    def test_completed_work_is_not_labelled_as_a_budget_pause(self):
        reply = response("Complete")
        reply["usage"] = {"input_tokens": 15, "output_tokens": 5}
        parent = self.task_team([[reply]], tokens=20)
        self.send(parent); self.finish(parent)
        self.assertEqual(parent.chat["team"]["status"], "done")
        self.assertNotIn("limitReason", parent.chat["team"])

    def test_repeated_unchanged_reads_pause_but_new_findings_count_as_progress(self):
        (self.root / "a").write_text("Same finding")
        parent = self.task_team([[call("read_file", {"path": "a"})] * 5])
        with patch("chat_runtime.MAX_ROUNDS", 1):
            self.send(parent); self.finish(parent)
        self.assertEqual(parent.chat["team"]["status"], "stopped")
        self.assertEqual(len(self.transports[0].requests), 4)
        self.assertIn("No new tool results", json.dumps(parent.chat["entries"]))

    def test_effective_context_and_progress_survive_contribution_and_disk(self):
        parent = self.task_team([[call("team_status", {"state": "continue", "next_step": "Implement", "findings": "Importer found", "owned_paths": ["renderer.py"]}),
                                 response("Ready to implement"), response("Implemented")]], contextTokens=32000)
        self.send(parent); self.finish(parent)
        member = parent.chat["team"]["members"][0]
        self.assertEqual(member["context"], 32000)
        self.assertEqual(member["modelContext"], 100000)
        self.assertIn("Importer found", json.dumps(self.transports[0].requests[-1]))
        restored = parent.store.load(parent.chat["id"])
        self.assertEqual(restored["team"]["members"][0]["progress"]["owned_paths"], ["renderer.py"])
        parent.chat["entries"].append({"kind": "user", "text": "Continue verification", "id": "followup"})
        chat_team.start_run(parent, new_input=True)
        self.assertEqual(member["progress"]["findings"], "Importer found")
        self.assertEqual(member["progress"]["latest_request"], "Continue verification")

    def test_invalid_limits_are_rejected_before_configuration_changes(self):
        for item in ({"mode": "forever"}, {"tokens": True}, {"minutes": 0}, {"processes": 9}, {"contextTokens": -1}, {"dollars": 10}):
            with self.subTest(item=item), self.assertRaises(ValueError):
                chat_execution.settings(item)


if __name__ == "__main__": unittest.main()
