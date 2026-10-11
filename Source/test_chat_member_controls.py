"""Member card commands use the real scheduler, cancellation and persistence."""
import copy
import threading
import unittest
from unittest.mock import patch

import chat_team
import test_chat_concurrency as concurrency
from test_chat_agents import call
from test_chat_runtime import response
import test_chat_team as fixtures
from test_chat_team import decision


class MemberControlTests(unittest.TestCase):
    setUp = fixtures.TeamTests.setUp
    service = fixtures.TeamTests.service
    configure = fixtures.TeamTests.configure
    send = fixtures.TeamTests.send
    finish = fixtures.TeamTests.finish
    wait_for = fixtures.TeamTests.wait_for

    def action(self, parent, member, start=False):
        parent.handle({"command": "team_member_start" if start else "team_member_stop",
                       "id": parent.chat["id"], "member": member["id"], "request": "card"})

    def blocked(self, entered, released):
        def work(payload, cancel, delta):
            entered.set()
            while not released.wait(.005):
                if cancel.is_set(): raise InterruptedError("Stopped")
            if cancel.is_set(): raise InterruptedError("Stopped")
            return response("Finished")
        return work

    def test_stop_cancels_only_target_and_peer_can_continue(self):
        entered, release = threading.Event(), threading.Event()
        parent = self.service([[self.blocked(entered, release)],
                               [decision("continue", "Inspect another file"), response("First contribution"), response("Peer done")]])
        members = self.configure(parent)
        self.send(parent); self.wait_for(entered.is_set)
        self.action(parent, members[0]); self.finish(parent)
        self.assertFalse(parent.cancel.is_set())
        self.assertEqual([m["status"] for m in members], ["stopped", "done"])
        self.assertEqual(len(self.transports[1].requests), 3)
        self.assertTrue(members[0]["memberStoppedByUser"])
        saved = self.store.load(parent.chat["id"])
        self.assertEqual(saved["team"]["members"][0]["status"], "stopped")

    def test_play_done_member_during_peer_work_preserves_run_allowance(self):
        entered, release = threading.Event(), threading.Event()
        parent = self.service([[response("First"), response("Second")], [self.blocked(entered, release)]])
        members = self.configure(parent)
        self.send(parent)
        self.wait_for(lambda: entered.is_set() and members[0]["status"] == "done")
        usage, run_id = parent.chat["team"]["runUsage"], parent.chat["team"]["runID"]
        started = usage["started"]
        self.action(parent, members[0], start=True)
        self.wait_for(lambda: len(self.transports[0].requests) == 2 and members[0]["status"] == "done")
        self.assertIs(parent.chat["team"]["runUsage"], usage)
        self.assertEqual(usage["started"], started)
        self.assertEqual(parent.chat["team"]["runID"], run_id)
        self.assertEqual(len(self.transports[1].requests), 1)
        self.assertFalse(parent.team_children[members[1]["id"]].cancel.is_set())
        self.assertIn("user started another contribution", str(self.transports[0].requests[-1]))
        release.set(); self.finish(parent)

    def test_idle_play_starts_only_selected_member_and_keeps_history(self):
        parent = self.service([[response("First")], [response("Peer")], [response("Again")]])
        members = self.configure(parent)
        self.send(parent); self.finish(parent)
        old_messages = copy.deepcopy(members[0]["messages"])
        self.action(parent, members[0], start=True); self.finish(parent)
        self.assertEqual(members[0]["contributions"], 2)
        self.assertEqual(members[1]["contributions"], 1)
        self.assertEqual(len(self.transports[1].requests), 1)
        self.assertEqual(members[0]["messages"][:len(old_messages)], old_messages)

    def test_play_ready_member_has_explicit_user_context(self):
        parent = self.service([[response("Started")]])
        members = self.configure(parent)
        self.action(parent, members[1], start=True); self.finish(parent)
        self.assertEqual([m["contributions"] for m in members], [0, 1])
        self.assertIn("current Team context", str(self.transports[0].requests[0]))

    def test_active_play_rejects_exhausted_allowance(self):
        entered, release = threading.Event(), threading.Event()
        parent = self.service([[response("First")], [self.blocked(entered, release)]])
        members = self.configure(parent)
        self.send(parent)
        self.wait_for(lambda: entered.is_set() and members[0]["status"] == "done")
        usage = copy.deepcopy(parent.chat["team"]["runUsage"])
        with patch("chat_execution.limit", return_value="Token limit reached"):
            self.action(parent, members[0], start=True)
        self.assertEqual(members[0]["status"], "done")
        self.assertEqual(parent.chat["team"]["runUsage"], usage)
        self.assertIn("allowance", self.events[-1]["notice"])
        release.set(); self.finish(parent)

    def test_play_cannot_bypass_whole_team_stop_or_safety_pause(self):
        entered, release = threading.Event(), threading.Event()
        parent = self.service([[response("First")], [self.blocked(entered, release)]])
        members = self.configure(parent)
        self.send(parent)
        self.wait_for(lambda: entered.is_set() and members[0]["status"] == "done")
        members[0]["status"] = "stopped"
        self.action(parent, members[0], start=True)
        self.assertIn("paused for review", self.events[-1]["notice"])
        self.assertEqual(members[0]["status"], "stopped")
        parent.cancel.set()
        self.action(parent, members[0], start=True)
        self.assertIn("active work to stop", self.events[-1]["notice"])
        self.assertTrue(parent.cancel.is_set())
        release.set(); self.finish(parent)

    def test_stop_pending_approval_denies_target_and_leaves_peer_working(self):
        entered, release = threading.Event(), threading.Event()
        patch_text = "*** Begin Patch\n*** Add File: forbidden.txt\n+no\n*** End Patch"
        parent = self.service([[response("", patch_text)], [self.blocked(entered, release)]])
        members = self.configure(parent)
        self.send(parent); self.wait_for(lambda: entered.is_set() and parent.approval is not None)
        self.assertEqual(parent.approval["memberID"], members[0]["id"])
        self.action(parent, members[0])
        self.wait_for(lambda: members[0]["status"] == "stopped")
        self.assertFalse((self.root / "forbidden.txt").exists())
        self.assertFalse(parent.team_children[members[1]["id"]].cancel.is_set())
        self.assertFalse(any(e["kind"] == "tool" and e.get("memberID") == members[0]["id"]
                             and e.get("detail") == "Running…" for e in parent.chat["entries"]))
        release.set(); self.finish(parent)

    def test_needs_input_and_stale_member_are_rejected(self):
        parent = self.service([])
        members = self.configure(parent)
        members[1]["status"] = "needs_input"
        self.action(parent, members[0], start=True)
        self.assertIn("answer", self.events[-1]["notice"])
        self.action(parent, {"id": "missing"}, start=True)
        self.assertIn("no longer", self.events[-1]["notice"])
        self.assertFalse(parent.busy)

    def test_card_action_waits_for_scheduler_mutex(self):
        parent = self.service([])
        members = self.configure(parent)
        attempted, completed = threading.Event(), threading.Event()
        errors = []
        def action():
            attempted.set()
            try:
                chat_team.member_control(parent, {"command": "team_member_start", "id": parent.chat["id"],
                                                  "member": members[0]["id"]})
            except ValueError as error: errors.append(str(error))
            finally: completed.set()
        with parent._mutex:
            thread = threading.Thread(target=action)
            thread.start()
            self.assertTrue(attempted.wait(1))
            self.assertFalse(completed.wait(.05))
            # Simulate a scheduler checkpoint while it holds the lock. The
            # card must validate the resulting state, never overwrite it.
            members[1]["status"] = "needs_input"
            self.assertEqual(members[0]["status"], "ready")
        thread.join(2)
        self.assertTrue(completed.is_set())
        self.assertIn("answer", errors[0])
        self.assertFalse(parent.busy)

    def test_duplicate_stop_is_idempotent_and_snapshot_exposes_stopping(self):
        parent = self.service([])
        members = self.configure(parent)
        member = members[0]
        member.update(status="working", memberStoppedByUser=True)
        parent.team_children[member["id"]] = object()
        chat_team.member_control(parent, {"command": "team_member_stop", "id": parent.chat["id"], "member": member["id"]})
        self.assertTrue(chat_team.public(parent.chat["team"])["members"][0]["memberStoppedByUser"])
        self.assertEqual(member["status"], "working")
        parent.team_children.clear()

    def test_stop_during_tool_records_result_and_releases_workspace_gate(self):
        entered = threading.Event()
        parent = self.service([[call("run_shell", {"command": "first"})],
                               [call("run_shell", {"command": "second"}), response("Peer finished")]])
        class HeldRunner(parent.runner_type):
            def execute(runner, name, args):
                if args["command"] == "first":
                    entered.set()
                    self.assertTrue(runner.cancel_event.wait(3))
                return {"content": [{"type": "text", "text": "Recorded " + args["command"]}],
                        "is_error": args["command"] == "first", "summary": args["command"], "changed_files": []}
        parent.runner_type = HeldRunner
        members = self.configure(parent)
        parent.chat["approvalMode"] = "yolo"
        self.send(parent); self.wait_for(entered.is_set)
        self.action(parent, members[0]); self.finish(parent)
        rows = [e for e in parent.chat["entries"] if e["kind"] == "tool"]
        self.assertEqual({e["detail"] for e in rows}, {"Recorded first", "Recorded second"})
        self.assertTrue(all(e["recorded"] for e in rows))
        self.assertEqual(members[1]["status"], "done")


class MemberHostBoundaryTests(unittest.TestCase):
    setUp = concurrency.ConcurrentChatTests.setUp
    create = concurrency.ConcurrentChatTests.create

    def configured(self):
        identifier = self.create()
        service = self.host.session(identifier)
        self.host.handle({"command": "configure_team", "id": identifier, "enabled": True,
                          "members": [{"choice": self.host.models[0]["id"], "name": "Member", "effort": "", "responsibility": ""}]})
        return identifier, service

    def test_idle_start_obeys_active_chat_cap_and_routes_team_notice(self):
        identifier, service = self.configured()
        self.host.max_active = 0
        with self.assertRaises(ValueError) as raised:
            self.host.handle({"command": "team_member_start", "id": identifier,
                              "member": service.chat["team"]["members"][0]["id"]})
        self.assertIn("already running", str(raised.exception))
        self.assertFalse(service.busy)

    def test_member_start_obeys_workspace_branch_boundary(self):
        identifier, service = self.configured()
        with patch.object(self.host, "workspace_busy", return_value=True):
            with self.assertRaises(ValueError) as raised:
                self.host.handle({"command": "team_member_start", "id": identifier,
                                  "member": service.chat["team"]["members"][0]["id"]})
        self.assertIn("branch operation", str(raised.exception))
        self.assertFalse(service.busy)


if __name__ == "__main__": unittest.main()
