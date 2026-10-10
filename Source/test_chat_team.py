"""Team scheduling and persistence use real ChatService with scripted providers."""
import copy
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import chat_memory
import chat_team
from chat_runtime import ChatService, ChatStore, entry
from test_chat_agents import call
from test_chat_runtime import FakeTransport, response


def decision(state, next_step=""):
    return call("team_status", {"state": state, "next_step": next_step})


class TeamTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = ChatStore(self.root, lock=False)
        self.events = []

    def service(self, scripts):
        self.transports = [FakeTransport(script) for script in scripts]
        self.primary = FakeTransport([])
        available = iter(self.transports)
        parent = ChatService(self.store, self.primary, self.events.append, child_factory=lambda: next(available))
        parent.refresh(); parent.create(parent.models[0]["id"], str(self.root))
        return parent

    def configure(self, parent, count=2):
        specs = [{"choice": parent.models[i % 2]["id"], "name": f"Member {i + 1}",
                  "effort": "", "responsibility": f"Responsibility {i + 1}"} for i in range(count)]
        parent.handle({"command": "configure_team", "id": parent.chat["id"], "enabled": True,
                       "execution": {"mode": "contribution"},
                       "members": specs, "request": "config-request"})
        self.assertTrue(parent.chat.get("team"), self.events[-1])
        return parent.chat["team"]["members"]

    def send(self, parent, text="Inspect the project"):
        parent.handle({"command": "send", "id": parent.chat["id"], "text": text})

    def finish(self, parent):
        parent.thread.join(4)
        self.assertFalse(parent.busy, "Team did not settle")

    def wait_for(self, check):
        end = time.monotonic() + 3
        while not check() and time.monotonic() < end: time.sleep(.005)
        self.assertTrue(check())

    def test_default_one_contribution_each_shared_transcript_and_no_coordinator_request(self):
        parent = self.service([[response("First result")], [response("Second result")], [response("Third result")]])
        members = self.configure(parent, 3)
        self.assertFalse(any(t.requests for t in self.transports))
        self.send(parent); self.finish(parent)
        self.assertEqual(self.primary.requests, [])
        self.assertEqual([len(t.requests) for t in self.transports], [1, 1, 1])
        self.assertEqual([m["status"] for m in members], ["done"] * 3)
        self.assertEqual(parent.chat["team"]["queue"], [])
        answers = [e for e in parent.chat["entries"] if e["kind"] == "assistant"]
        self.assertEqual({e["memberID"]: e["text"] for e in answers},
                         dict(zip([m["id"] for m in members], ["First result", "Second result", "Third result"])))
        self.assertEqual(len({e["contributionID"] for e in answers}), 3)
        self.assertIn("Inspect the project", json.dumps(self.transports[2].requests[0]))
        self.assertNotIn("messages", json.dumps([e for e in self.events if e["event"] == "team"]))
        self.assertNotIn("team", self.store.headers()[0])

    def test_team_search_uses_shared_preference_and_each_members_capability(self):
        for enabled in (False, True):
            with self.subTest(enabled=enabled):
                parent = self.service([[response("Search capable")], [response("No native search")]])
                parent.models[0]["supportsWebSearch"] = True
                parent.models[1]["supportsWebSearch"] = False
                self.configure(parent)
                parent.handle({"command": "preferences", "webSearch": enabled})
                self.send(parent); self.finish(parent)
                self.assertEqual("_web_search" in self.transports[0].requests[0], enabled)
                self.assertNotIn("_web_search", self.transports[1].requests[0])
                for transport in self.transports:
                    system = transport.requests[0]["system"]
                    self.assertIn(chat_memory.GUIDANCE, system)
                    self.assertIn(chat_team.GUIDANCE, system)
                    if not enabled:
                        self.assertIn("Web search is disabled", system)

    def test_disabling_search_mid_contribution_reaches_all_active_members(self):
        requested = threading.Barrier(2)
        disabled = threading.Event()
        def disable_search(payload, cancel, delta):
            requested.wait(2)
            parent.handle({"command": "preferences", "webSearch": False})
            disabled.set()
            return call("read_file", {"path": "missing.txt"})
        def peer_read(payload, cancel, delta):
            requested.wait(2); self.assertTrue(disabled.wait(2))
            return call("read_file", {"path": "peer.txt"})
        parent = self.service([[disable_search, response("First finished")], [peer_read, response("Second finished")]])
        for choice in parent.models:
            choice["supportsWebSearch"] = True
        self.configure(parent)
        self.send(parent); self.finish(parent)
        self.assertEqual(len(self.transports[0].requests), 2)
        self.assertIn("_web_search", self.transports[0].requests[0])
        self.assertNotIn("_web_search", self.transports[0].requests[1])
        self.assertNotIn("_web_search", self.transports[1].requests[1])

    def test_explicit_continuation_rotates_fairly_and_must_be_renewed(self):
        def after_review(payload, cancel, delta):
            self.wait_for(lambda: members[1]["status"] == "done")
            return response("Implementation done")
        parent = self.service([[decision("continue", "Verify the tests"), after_review, response("Tests passed")],
                               [response("Review complete")]])
        members = self.configure(parent)
        self.send(parent); self.finish(parent)
        answers = [e for e in parent.chat["entries"] if e["kind"] == "assistant" and e["text"]]
        self.assertEqual({e["text"] for e in answers[:-1]}, {"Implementation done", "Review complete"})
        self.assertEqual(answers[-1]["text"], "Tests passed")
        self.assertEqual([m["contributions"] for m in members], [2, 1])
        self.assertEqual([m["status"] for m in members], ["done", "done"])
        self.assertEqual(json.dumps(self.transports[0].requests[-1]).count("Review complete"), 1)

    def test_more_than_twenty_four_contributions_has_no_total_round_quota(self):
        script = []
        for i in range(28): script += [decision("continue", f"Step {i + 1}"), response(f"Step {i} complete")]
        script.append(response("Finished all work"))
        parent = self.service([script]); member = self.configure(parent, 1)[0]
        self.send(parent); self.finish(parent)
        self.assertEqual(member["contributions"], 29)
        self.assertEqual(member["status"], "done")
        self.assertEqual(len(self.transports[0].requests), 57)

    def test_same_contribution_repeating_three_times_pauses_without_global_turn_quota(self):
        script = [decision("continue", "Do it again"), response("Same outcome")] * 3
        parent = self.service([script]); member = self.configure(parent, 1)[0]
        self.send(parent); self.finish(parent)
        self.assertEqual(member["contributions"], 3)
        self.assertEqual(parent.chat["team"]["status"], "stopped")
        self.assertIn("repeated", parent.chat["entries"][-1]["text"])

    def test_needs_input_pauses_future_contributions_and_resume_needs_real_answer(self):
        parent = self.service([[decision("needs_input", "Which directory?"), response("Which directory should I use?")],
                               [response("Independent answer")], [response("Answered now")], [response("Other member answered")]])
        members = self.configure(parent)
        self.send(parent); self.finish(parent)
        self.assertEqual([m["status"] for m in members], ["needs_input", "done"])
        parent.handle({"command": "team_resume", "id": parent.chat["id"], "request": "resume"})
        self.assertIn("answer", self.events[-1]["notice"])
        self.assertEqual(len(self.transports[1].requests), 1)
        self.send(parent, "Use the current directory"); self.finish(parent)
        self.assertEqual([m["status"] for m in members], ["done", "done"])
        self.assertIn("Use the current directory", json.dumps(self.transports[3].requests))

    def test_route_accounts_and_opaque_histories_stay_private_across_resume(self):
        first = response("Public A")
        first["content"].insert(0, {"type": "thinking", "thinking": "PRIVATE-A", "signature": "sig-A"})
        second = response("Public B")
        second["content"].insert(0, {"type": "thinking", "thinking": "PRIVATE-B", "signature": "sig-B"})
        parent = self.service([[decision("continue", "Check public B"), first, response("Done A")], [second]])
        members = self.configure(parent)
        self.send(parent); self.finish(parent)
        self.assertEqual(self.transports[1].requests[0]["_provider_hub_account"], "work")
        self.assertIn("sig-A", json.dumps(members[0]["messages"]))
        self.assertNotIn("sig-B", json.dumps(members[0]["messages"]))
        self.assertNotIn("PRIVATE-A", json.dumps(self.transports[1].requests))
        self.assertNotIn("PRIVATE-", json.dumps(self.events))
        loaded = self.store.load(parent.chat["id"])
        self.assertIn("sig-A", json.dumps(loaded["team"]["members"][0]["messages"]))

    def test_invalid_or_changed_roster_starts_nothing(self):
        parent = self.service([[], []])
        good = {"choice": parent.models[0]["id"], "name": "One"}
        for specs in ([], [good] * (chat_team.MAX_MEMBERS + 1), [good, {"choice": "missing", "name": "Other"}]):
            parent.handle({"command": "configure_team", "id": parent.chat["id"], "enabled": True, "members": specs})
            self.assertIn("notice", self.events[-1]); self.assertNotIn("team", parent.chat)
        members = self.configure(parent)
        parent.models[1]["scope"] = "changed"
        with self.assertRaisesRegex(ValueError, "connection changed"): self.send(parent)
        self.assertFalse(any(t.requests for t in self.transports))
        self.assertEqual([m["status"] for m in members], ["ready", "ready"])

    def test_mutations_and_approvals_are_serial_and_attributed(self):
        patch_text = "*** Begin Patch\n*** Add File: result.txt\n+first\n*** End Patch"
        second = "*** Begin Patch\n*** Update File: result.txt\n@@\n-first\n+second\n*** End Patch"
        second_ready = threading.Event()
        def second_request(payload, cancel, delta):
            self.wait_for(lambda: parent.approval is not None)
            second_ready.set()
            return call("apply_patch", {"patch": second})
        parent = self.service([[call("apply_patch", {"patch": patch_text}), response("Wrote first")],
                               [second_request, response("Wrote second")]])
        self.configure(parent)
        self.send(parent)
        self.wait_for(lambda: parent.approval is not None)
        self.assertIn("Member 1", parent.approval["summary"])
        self.wait_for(lambda: len(self.transports[1].requests) == 1)
        self.assertTrue(second_ready.wait(2))
        parent.handle({"command": "approve", "id": parent.approval["id"], "allow": True})
        self.wait_for(lambda: parent.approval is not None and "Member 2" in parent.approval["summary"])
        self.assertEqual((self.root / "result.txt").read_text(), "first\n")
        parent.handle({"command": "approve", "id": parent.approval["id"], "allow": True})
        self.finish(parent)
        self.assertEqual((self.root / "result.txt").read_text(), "second\n")
        tools = [e for e in parent.chat["entries"] if e["kind"] == "tool"]
        self.assertEqual({e["memberName"] for e in tools}, {"Member 1", "Member 2"})

    def test_stop_cancels_active_and_queued_work_resume_does_not_repeat_finished_member(self):
        started = threading.Event()
        def waiting(payload, cancel, delta):
            started.set(); cancel.wait(3); raise InterruptedError("Stopped")
        parent = self.service([[response("Done A")], [waiting], [response("Resumed B")]])
        members = self.configure(parent)
        self.send(parent); self.assertTrue(started.wait(2))
        self.wait_for(lambda: members[0]["status"] == "done")
        parent.handle({"command": "stop"}); self.finish(parent)
        self.assertEqual([m["status"] for m in members], ["done", "stopped"])
        parent.handle({"command": "team_resume", "id": parent.chat["id"], "request": "resume"})
        self.finish(parent)
        self.assertEqual([m["contributions"] for m in parent.chat["team"]["members"]], [1, 1])
        self.assertEqual(len(self.transports[0].requests), 1)

    def test_steer_preserves_run_queue_and_continuation_at_tool_boundary(self):
        started, release = threading.Event(), threading.Event()
        def waiting(payload, cancel, delta):
            started.set(); self.assertTrue(release.wait(3))
            self.assertFalse(cancel.is_set())
            return call("read_file", {"path": "missing.txt"})
        def restarted(payload, cancel, delta):
            self.assertIn("New direction", json.dumps(payload))
            self.assertTrue(any(b.get("type") == "tool_result" for m in payload["messages"] for b in m["content"] if isinstance(b, dict)))
            return response("Updated B")
        def continued(payload, cancel, delta):
            self.assertTrue(release.wait(3))
            return call("read_file", {"path": "missing.txt"})
        parent = self.service([[decision("continue", "Verify tests"), response("First A"), continued, response("Continued A")],
                               [waiting, restarted]])
        members = self.configure(parent)
        self.send(parent); self.assertTrue(started.wait(2))
        self.wait_for(lambda: members[0]["contributions"] == 1)
        team = parent.chat["team"]
        run_id, contribution_id, queue = team["runID"], members[1]["contributionID"], list(team["queue"])
        try:
            parent.handle({"command": "steer", "id": parent.chat["id"], "text": "New direction"})
            self.assertTrue(parent.busy)
            self.assertFalse(parent.cancel.is_set())
            self.assertEqual(team["queue"], queue)
            self.assertEqual(members[0]["nextStep"], "Verify tests")
        finally:
            release.set()
        self.finish(parent)
        self.assertEqual(team["runID"], run_id)
        self.assertEqual(members[1]["contributionID"], contribution_id)
        self.assertIn("New direction", json.dumps(self.transports[0].requests[-1]))
        self.assertEqual([m["contributions"] for m in members], [2, 1])
        self.assertFalse(any("stopped" in e.get("text", "").lower() for e in parent.chat["entries"]))
        self.assertEqual(parent.chat["team"]["status"], "done")
        self.assertIn("Updated B", json.dumps(parent.chat["entries"]))

    def test_checkpoint_round_limit_yields_only_when_opted_in(self):
        parent = self.service([[decision("continue", "Finish reading"), call("read_file", {"path": "missing"}),
                                response("Pausing here; I will finish reading next."), response("Finished")]])
        member = self.configure(parent, 1)[0]
        with patch("chat_runtime.MAX_ROUNDS", 2): self.send(parent); self.finish(parent)
        self.assertEqual(member["contributions"], 2)
        self.assertEqual(member["status"], "done")

    def test_mid_tool_checkpoint_gets_signoff_without_implicit_continuation(self):
        signoff = "Both reads finished. No edits yet; next I need to update the configuration."
        parent = self.service([[call("read_file", {"path": "a"}), call("read_file", {"path": "b"}), response(signoff)],
                               [response("Peer finished its independent work")]])
        members = self.configure(parent)
        with patch("chat_runtime.MAX_ROUNDS", 2): self.send(parent); self.finish(parent)
        self.assertEqual(parent.chat["team"]["status"], "needs_input")
        self.assertEqual([m["status"] for m in members], ["needs_input", "done"])
        self.assertEqual(len(self.transports[0].requests), 3)
        self.assertEqual(len(self.transports[1].requests), 1)
        closing = self.transports[0].requests[-1]
        self.assertEqual([t["name"] for t in closing["tools"]], ["team_status"])
        self.assertIn("1 model/tool round", json.dumps(self.transports[0].requests[1]))
        stored = self.store.load(parent.chat["id"])
        self.assertTrue(any(e.get("text") == signoff for e in stored["entries"]))
        notice = next(e for e in stored["entries"] if e["kind"] == "notice" and e["text"].startswith("Team contribution checkpoint reached."))
        self.assertTrue(notice["text"].startswith("Team contribution checkpoint reached."))
        self.assertIn("2-round tool budget", notice["text"])
        self.assertFalse(any(e["kind"] == "error" for e in stored["entries"]))

    def test_closing_can_request_continuation_then_sign_off_before_next_wave(self):
        parent = self.service([[call("read_file", {"path": "a"}), decision("continue", "Apply the change"),
                                response("Read complete; I will apply the change after the review."), response("Change complete")],
                               [response("Review complete")]])
        members = self.configure(parent)
        with patch("chat_runtime.MAX_ROUNDS", 1): self.send(parent); self.finish(parent)
        self.assertEqual([m["contributions"] for m in members], [2, 1])
        self.assertEqual([m["status"] for m in members], ["done", "done"])
        replies = [e["text"] for e in parent.chat["entries"] if e["kind"] == "assistant" and e["text"]]
        self.assertEqual(set(replies[:-1]), {"Read complete; I will apply the change after the review.", "Review complete"})
        self.assertEqual(replies[-1], "Change complete")
        self.assertEqual(self.transports[0].requests[2]["tools"], [])
        self.assertEqual(members[0]["decision"]["state"], "done")

    def test_explicit_done_during_closing_finishes_instead_of_asking_for_input(self):
        parent = self.service([[call("read_file", {"path": "a"}), decision("done"), response("Inspection complete")]])
        member = self.configure(parent, 1)[0]
        with patch("chat_runtime.MAX_ROUNDS", 1): self.send(parent); self.finish(parent)
        self.assertEqual(member["status"], "done")
        self.assertEqual(parent.chat["team"]["status"], "done")

    def test_real_stop_after_steering_still_cancels_active_and_queued_members(self):
        started = threading.Barrier(3)
        def waiting(payload, cancel, delta):
            started.wait(2); cancel.wait(3); raise InterruptedError("Stopped")
        parent = self.service([[waiting], [waiting]])
        members = self.configure(parent)
        self.send(parent); started.wait(2)
        parent.handle({"command": "steer", "id": parent.chat["id"], "text": "Update before stop"})
        parent.handle({"command": "stop"}); self.finish(parent)
        self.assertEqual(parent.chat["team"]["status"], "stopped")
        self.assertEqual([m["status"] for m in members], ["stopped", "stopped"])
        self.assertEqual(len(self.transports[1].requests), 1)
        self.assertIn("Team stopped. Recorded results are kept.", [e.get("text") for e in parent.chat["entries"]])

    def test_steer_queues_completed_member_after_active_member(self):
        started, release = threading.Event(), threading.Event()
        def waiting(payload, cancel, delta):
            started.set(); release.wait(3)
            return response("Old B result")
        parent = self.service([[response("Old A result"), response("Updated A")],
                               [waiting, response("Updated B")]])
        first, second = self.configure(parent)
        self.send(parent); self.assertTrue(started.wait(2))
        self.wait_for(lambda: first["status"] == "done")
        try:
            parent.handle({"command": "steer", "id": parent.chat["id"], "text": "New direction for both"})
            self.assertEqual(parent.chat["team"]["queue"], [second["id"], first["id"]])
        finally:
            release.set()
        self.finish(parent)
        answers = [e["text"] for e in parent.chat["entries"] if e["kind"] == "assistant"]
        self.assertEqual(set(answers[:2]), {"Old A result", "Old B result"})
        self.assertEqual(set(answers[2:]), {"Updated B", "Updated A"})
        for transport in self.transports:
            self.assertIn("New direction for both", json.dumps(transport.requests[-1]))

    def test_steer_during_running_tool_records_its_real_result_once(self):
        started, release = threading.Event(), threading.Event()
        executed = []
        (self.root / "input.txt").write_text("Recorded file content")
        def updated(payload, cancel, delta):
            self.assertIn("New instruction", json.dumps(payload))
            self.assertIn("Recorded file content", json.dumps(payload))
            saved = self.store.load(parent.chat["id"])
            rows = [e for e in saved["entries"] if e.get("tool") == "read_file"]
            self.assertEqual(len(rows), 1)
            self.assertIn("Recorded file content", rows[0]["detail"])
            self.assertFalse(rows[0]["isError"])
            return response("Updated result")
        parent = self.service([[call("read_file", {"path": "input.txt"}), updated], [response("Second result"), response("Updated peer result")]])
        class HeldRunner(parent.runner_type):
            def execute(runner, name, args):
                executed.append(name)
                started.set(); release.wait(3)
                return super().execute(name, args)
        parent.runner_type = HeldRunner
        members = self.configure(parent)
        self.send(parent); self.assertTrue(started.wait(2))
        try:
            parent.handle({"command": "steer", "id": parent.chat["id"], "text": "New instruction"})
            self.assertFalse(parent.team_children[members[0]["id"]].cancel.is_set())
            self.assertFalse(self.events[-1]["interrupting"])
        finally:
            release.set()
        self.finish(parent)
        self.assertEqual(executed, ["read_file"])
        self.assertEqual(parent.chat["team"]["status"], "done")
        self.assertFalse(any("stopped" in e.get("text", "").lower() for e in parent.chat["entries"]))

    def test_steering_at_terminal_checkpoint_recovers_queue_without_replaying_result(self):
        parent = self.service([[], []]); first, second = self.configure(parent)
        chat_team.start_run(parent, new_input=True)
        first.update(status="working", contributionID="steered-contribution", cursor=len(parent.chat["entries"]))
        parent.chat["team"]["activeMemberID"] = first["id"]
        parent.chat["entries"].append(entry("user", "New instruction at the final boundary"))
        parent.save()
        child = {"messages": [{"role": "assistant", "content": [{"type": "text", "text": "Recorded result"}]}],
                 "status": "ready", "route": first["route"],
                 "teamDecision": {"state": "continue", "next_step": "Verify the result"}}
        chat_team.MemberStore(parent, first, {}).save(child)
        loaded = self.store.load(parent.chat["id"])
        chat_team.recover(loaded, ChatService.settle)
        chat_team.recover(loaded, ChatService.settle)
        member = loaded["team"]["members"][0]
        self.assertEqual(member["contributions"], 1)
        self.assertEqual(member["appliedContributionID"], "steered-contribution")
        self.assertEqual(member["status"], "queued")
        self.assertEqual(member["nextStep"], "Verify the result")
        self.assertEqual(loaded["team"]["queue"], [first["id"], second["id"]])
        self.assertEqual(loaded["team"]["runID"], parent.chat["team"]["runID"])
        self.assertTrue(chat_team.has_update(loaded, member))

    def test_steer_at_terminal_save_delivers_new_instruction_to_every_member(self):
        final_save, release = threading.Event(), threading.Event()
        started = threading.Barrier(2)
        def first_reply(payload, cancel, delta):
            started.wait(2)
            return response("Old task finished")
        def peer_reply(payload, cancel, delta):
            started.wait(2)
            return response("Old peer finished")
        def restarted(payload, cancel, delta):
            self.assertIn("New direction at the final boundary", json.dumps(payload))
            return response("Followed new direction")
        parent = self.service([[first_reply, restarted], [peer_reply, restarted]])
        first, second = self.configure(parent)
        original = chat_team.MemberStore.save
        def held_save(store, chat):
            if store.member["id"] == first["id"] and chat["status"] == "ready" and not final_save.is_set():
                final_save.set(); release.wait(3)
            return original(store, chat)
        with patch.object(chat_team.MemberStore, "save", held_save):
            self.send(parent); self.assertTrue(final_save.wait(2))
            self.wait_for(lambda: second["status"] == "done")
            try:
                parent.handle({"command": "steer", "id": parent.chat["id"], "text": "New direction at the final boundary"})
            finally:
                release.set()
            self.wait_for(lambda: not parent.busy)
        self.assertEqual(parent.chat["team"]["status"], "done")
        self.assertEqual([len(t.requests) for t in self.transports], [2, 2])

    def test_shared_notebook_uses_main_transcript_source_ids(self):
        def remember(payload, cancel, delta):
            source = next(e["id"] for e in parent.chat["entries"] if e["kind"] == "user")
            return call("record_decision", {"key": "goal", "text": "Use this workspace", "source_ids": [source]})
        def wait_for_note(payload, cancel, delta):
            self.wait_for(lambda: bool(parent.chat.get("decisions")))
            return call("read_file", {"path": "missing"})
        parent = self.service([[remember, response("Recorded")], [wait_for_note, response("Read the note")]])
        self.configure(parent)
        self.send(parent); self.finish(parent)
        self.assertEqual(parent.chat["decisions"][0]["key"], "goal")
        self.assertIn("Use this workspace", json.dumps(self.transports[1].requests[1]))
        source = next(e for e in parent.chat["entries"] if e["kind"] == "assistant" and e["text"] == "Recorded")
        read = chat_memory.execute(parent.chat, "read_history", {"entry_id": source["id"]})
        self.assertIn("Member 1", read["content"][0]["text"])

    def test_failed_member_does_not_requeue_earlier_continue_intent(self):
        parent = self.service([[decision("continue", "Next step"), ValueError("Provider failed")], [response("Should not run")]])
        members = self.configure(parent)
        self.send(parent); self.finish(parent)
        self.assertEqual(parent.chat["team"]["status"], "error")
        self.assertEqual(members[0]["status"], "error")
        self.assertEqual(members[1]["status"], "done")
        self.assertEqual(len(self.transports[1].requests), 1)

    def test_all_members_have_host_tools_but_cannot_delegate(self):
        parent = self.service([[call("delegate", {"task": "Spawn fourth member"}), response("No more members")]])
        self.configure(parent, 1)
        self.send(parent); self.finish(parent)
        offered = {t["name"] for t in self.transports[0].requests[0]["tools"]}
        self.assertTrue({"read_file", "search_files", "apply_patch", "run_shell", "team_status", "search_history"} <= offered)
        self.assertNotIn("delegate", offered)
        self.assertTrue(self.transports[0].requests[1]["messages"][-1]["content"][0]["is_error"])

    def test_change_member_model_archives_old_private_state_and_disabling_keeps_transcript(self):
        parent = self.service([[response("A public answer")]])
        member = self.configure(parent, 1)[0]
        self.send(parent); self.finish(parent)
        spec = {"id": member["id"], "choice": parent.models[1]["id"], "name": "A", "effort": "", "responsibility": ""}
        parent.handle({"command": "configure_team", "id": parent.chat["id"], "enabled": False, "members": [spec]})
        self.assertFalse(parent.chat["team"]["enabled"])
        self.assertEqual(parent.chat["team"]["members"][0]["messages"], [])
        self.assertIn("A public answer", json.dumps(parent.chat["messages"]))
        self.assertTrue(any(a.get("kind") == "team_member" for a in parent.chat["archives"]))

    def test_incremental_checkpoint_replays_once_and_crash_settles_pending_action(self):
        parent = self.service([[]]); member = self.configure(parent, 1)[0]
        chat_team.start_run(parent, new_input=True)
        member["status"] = "working"
        parent.chat["team"]["activeMemberID"] = member["id"]
        parent.save()
        member["messages"] = [{"role": "assistant", "content": [{"type": "tool_use", "id": "pending", "name": "run_shell", "input": {"command": "echo hi"}}]}]
        item = entry("tool", tool="run_shell", detail="Running…", memberID=member["id"], memberName=member["name"])
        parent.chat["entries"].append(item)
        dirty = {item["id"]: item}
        chat_team.checkpoint(parent, member, dirty)
        self.assertFalse(dirty)
        loaded = self.store.load(parent.chat["id"])
        self.assertEqual(loaded["entries"][-1]["id"], item["id"])
        chat_team.load_journal(self.store, loaded)
        self.assertEqual(sum(e["id"] == item["id"] for e in loaded["entries"]), 1)
        path = chat_team.journal_path(self.store, parent.chat["id"])
        with path.open("ab") as stream: stream.write(b'{"partial":')
        restarted = ChatService(self.store, FakeTransport([]), self.events.append)
        restarted.initialize()
        saved = self.store.load(parent.chat["id"])
        self.assertEqual(saved["team"]["members"][0]["status"], "interrupted")
        self.assertTrue(saved["team"]["members"][0]["messages"][-1]["content"][0]["is_error"])
        self.assertEqual(restarted.transport.requests, [])
        self.assertFalse(path.exists())

    def test_checkpoint_contains_only_changed_rows_and_one_private_history(self):
        parent = self.service([[], []]); members = self.configure(parent)
        parent.chat["entries"].append(entry("assistant", "OLD-PUBLIC-TEXT"))
        members[1]["messages"] = [{"role": "assistant", "content": [{"type": "thinking", "thinking": "OTHER-MEMBER-PRIVATE"}]}]
        parent.save()
        current = entry("assistant", "new", memberID=members[0]["id"])
        parent.chat["entries"].append(current)
        chat_team.checkpoint(parent, members[0], {current["id"]: current})
        raw = chat_team.journal_path(self.store, parent.chat["id"]).read_text()
        self.assertNotIn("OLD-PUBLIC-TEXT", raw)
        self.assertNotIn("OTHER-MEMBER-PRIVATE", raw)
        self.assertEqual(self.store.load(parent.chat["id"])["team"]["members"][1]["messages"], members[1]["messages"])

    def test_terminal_checkpoint_recovers_outcome_once_before_any_later_scheduling(self):
        for outcome in ("done", "continue", "needs_input"):
            with self.subTest(outcome=outcome):
                parent = self.service([[], []]); first, second = self.configure(parent)
                chat_team.start_run(parent, new_input=True)
                first.update(status="working", contributionID="contribution-1")
                parent.chat["team"]["activeMemberID"] = first["id"]
                parent.save()
                row = entry("assistant", "Completed the requested work", memberID=first["id"], contributionID="contribution-1")
                parent.chat["entries"].append(row)
                child = {"messages": [{"role": "assistant", "content": [{"type": "text", "text": row["text"]}]}],
                         "status": "ready", "route": first["route"],
                         "teamDecision": {"state": outcome, "next_step": "Next step" if outcome != "done" else ""}}
                # This is the final child save; no parent scheduler code runs.
                chat_team.MemberStore(parent, first, {row["id"]: row}).save(child)
                loaded = self.store.load(parent.chat["id"])
                chat_team.recover(loaded, ChatService.settle)
                chat_team.recover(loaded, ChatService.settle)
                member = loaded["team"]["members"][0]
                self.assertEqual(member["contributions"], 1)
                self.assertEqual(member["status"], {"done": "done", "continue": "continuing", "needs_input": "needs_input"}[outcome])
                self.assertEqual(loaded["team"]["queue"], {"done": [second["id"]],
                    "continue": [second["id"], first["id"]], "needs_input": [first["id"], second["id"]]}[outcome])
                self.assertFalse(any(t.requests for t in self.transports))

    def test_stop_just_after_final_checkpoint_preserves_completed_member(self):
        started = threading.Event()
        def finished(payload, cancel, delta):
            self.assertTrue(started.wait(2))
            return response("Finished A")
        def waiting(payload, cancel, delta):
            started.set(); cancel.wait(3); raise InterruptedError("Stopped")
        parent = self.service([[finished], [waiting], [response("Finished B")]])
        first, second = self.configure(parent)
        original = chat_team.checkpoint
        def stop_after_final(parent, member=None, dirty=None):
            original(parent, member, dirty)
            if member and member["id"] == first["id"] and member["status"] == "done":
                parent.handle({"command": "stop"})
        with patch.object(chat_team, "checkpoint", side_effect=stop_after_final):
            self.send(parent); self.finish(parent)
        self.assertEqual(first["status"], "done")
        self.assertEqual(parent.chat["team"]["queue"], [second["id"]])
        parent.handle({"command": "team_resume", "id": parent.chat["id"]})
        self.finish(parent)
        self.assertEqual([m["contributions"] for m in parent.chat["team"]["members"]], [1, 1])
        self.assertEqual([len(t.requests) for t in self.transports], [1, 1, 1])

    def test_failed_configuration_save_keeps_previous_roster_and_histories(self):
        parent = self.service([[]]); member = self.configure(parent, 1)[0]
        old = copy.deepcopy(parent.chat)
        with patch.object(self.store, "save", side_effect=OSError("Disk full")):
            parent.handle({"command": "configure_team", "id": parent.chat["id"], "enabled": False,
                           "members": [{"id": member["id"], "choice": parent.models[1]["id"], "name": "Replacement"}]})
        self.assertEqual(parent.chat, old)
        self.assertIn("Disk full", self.events[-1]["notice"])

    def test_failed_final_save_cancels_peers_and_emits_error(self):
        started = threading.Barrier(2)
        def done(payload, cancel, delta):
            started.wait(2)
            return response("Done")
        def waiting(payload, cancel, delta):
            started.wait(2); cancel.wait(3); raise InterruptedError("Stopped")
        parent = self.service([[done], [waiting]])
        self.configure(parent)
        original = chat_team.checkpoint
        def fail_final(parent, member=None, dirty=None):
            if member and member.get("terminalStatus") == "ready" and member["name"] == "Member 1": raise OSError("Disk full")
            return original(parent, member, dirty)
        with patch.object(chat_team, "checkpoint", side_effect=fail_final):
            self.send(parent); self.finish(parent)
        self.assertEqual(len(self.transports[1].requests), 1)
        self.assertEqual(parent.chat["team"]["status"], "error")
        self.assertEqual(parent.chat["team"]["members"][0]["status"], "done")
        self.assertTrue(any(e["event"] == "error" and "save" in e["message"] for e in self.events))

    def test_failed_resume_save_keeps_paused_queue_and_starts_no_request(self):
        parent = self.service([[]]); member = self.configure(parent, 1)[0]
        member["status"] = "stopped"
        parent.chat["team"].update(status="stopped", queue=[member["id"]])
        before = copy.deepcopy(parent.chat["team"])
        with patch.object(self.store, "save", side_effect=OSError("Disk full")):
            parent.handle({"command": "team_resume", "id": parent.chat["id"]})
        self.assertEqual(parent.chat["team"], before)
        self.assertFalse(parent.busy)
        self.assertEqual(self.transports[0].requests, [])
        self.assertIn("Disk full", self.events[-1]["notice"])

    def test_malformed_complete_journal_record_is_preserved_and_rejected(self):
        parent = self.service([[]]); member = self.configure(parent, 1)[0]
        chat_team.checkpoint(parent, member)
        path = chat_team.journal_path(self.store, parent.chat["id"])
        record = json.loads(path.read_text())
        record["team"]["members"] = [None]
        path.write_text(json.dumps(record) + "\n")
        with self.assertRaisesRegex(ValueError, "checkpoint contents"):
            self.store.load(parent.chat["id"])
        self.assertTrue(path.exists())
        self.assertTrue(self.store.path(parent.chat["id"]).exists())

    def test_late_provider_delta_cannot_mutate_a_completed_shared_row(self):
        callbacks = []
        def streamed(payload, cancel, delta):
            callbacks.append(delta)
            delta("Streaming")
            return response("Recorded result")
        parent = self.service([[streamed], [response("Peer result")]])
        self.configure(parent)
        self.send(parent); self.finish(parent)
        before = copy.deepcopy(parent.chat["entries"])
        event_count = len(self.events)
        callbacks[0]("Late response from old contribution")
        self.assertEqual(parent.chat["entries"], before)
        self.assertEqual(len(self.events), event_count)

    @staticmethod
    def served(transport):
        """The member a scripted transport ran for, from its system prompt."""
        return transport.requests[0]["system"].split("Your member name: ", 1)[1].split("\n", 1)[0]

    def chips(self, parent, text):
        """The chips a composer drawing from the current roster sends with text."""
        return chat_team.mentions(parent.chat["team"], text)

    def steer(self, parent, text):
        """An update as the composer sends it, with the chips it drew."""
        parent.handle({"command": "steer", "id": parent.chat["id"], "text": text, "mentions": self.chips(parent, text)})

    def test_tagged_message_runs_only_its_members_in_tag_order(self):
        # Child transports are taken in scheduling order, one set per run.
        parent = self.service([[response("Third")], [response("Second")],
                               [response("First")], [response("Second again")], [response("Third again")]])
        members = self.configure(parent, 3)
        self.send(parent, "@Member 3 then @member 2: review the diff"); self.finish(parent)
        user = next(e for e in parent.chat["entries"] if e["kind"] == "user")
        self.assertEqual([(m["id"], m["start"], m["length"]) for m in user["mentions"]],
                         [(members[2]["id"], 0, 9), (members[1]["id"], 15, 9)])
        self.assertEqual([self.served(t) for t in self.transports[:2]], ["Member 3", "Member 2"])
        self.assertEqual([len(t.requests) for t in self.transports], [1, 1, 0, 0, 0])
        self.assertEqual([m["status"] for m in members], ["ready", "done", "done"])
        self.assertEqual(parent.chat["team"]["status"], "done")
        self.assertIn("[Addressed to you and Member 2.]", json.dumps(self.transports[0].requests[0]))
        self.assertIn("[Addressed to you and Member 3.]", json.dumps(self.transports[1].requests[0]))
        # An untagged message reaches everyone; the earlier one is context for Member 1.
        self.send(parent, "Everyone: summarise"); self.finish(parent)
        self.assertEqual([len(t.requests) for t in self.transports], [1] * 5)
        self.assertEqual(self.served(self.transports[2]), "Member 1")
        first = json.dumps(self.transports[2].requests[0])
        self.assertIn("[Addressed to Member 3 and Member 2, not you. Treat it as context.]", first)
        self.assertIn("Everyone: summarise", first)
        self.assertNotIn("mentions", [e for e in parent.chat["entries"] if e["kind"] == "user"][-1])
        self.assertEqual([m["status"] for m in members], ["done", "done", "done"])

    def test_tagged_update_queues_only_its_members(self):
        started, release = threading.Event(), threading.Event()
        def waiting(payload, cancel, delta):
            started.set(); release.wait(3)
            return response("Old B result")
        parent = self.service([[response("Old A result"), response("Updated A")], [waiting]])
        first, second = self.configure(parent)
        self.send(parent); self.assertTrue(started.wait(2))
        self.wait_for(lambda: first["status"] == "done")
        try:
            self.steer(parent, "@Member 1 also check the tests")
            self.assertEqual(parent.chat["team"]["queue"], [second["id"], first["id"]])
        finally:
            release.set()
        self.finish(parent)
        # Member 2 finished its own work; the update was not for it.
        self.assertEqual([len(t.requests) for t in self.transports], [2, 1])
        self.assertIn("[Addressed to you.]", json.dumps(self.transports[0].requests[-1]))
        self.assertEqual([first["status"], second["status"]], ["done", "done"])

    def test_chips_from_an_outdated_roster_are_refused_untouched(self):
        parent = self.service([[response("Only the tagged member")], []])
        first, second = self.configure(parent)
        attachment = self.root / "notes.txt"; attachment.write_text("notes")
        before = copy.deepcopy(parent.chat["entries"])
        text = "@Member 2 go"
        [chip] = self.chips(parent, text)
        # Drawn for a member since renamed, removed or moved to another model,
        # or over text that does not read as its member's name.
        for chips in ([{**chip, "name": "Old name"}], [{**chip, "id": "gone"}], [{**chip, "route": "other/model"}],
                      [{**chip, "id": first["id"], "name": first["name"], "route": first["route"]}],
                      [{**chip, "start": 1}], [{**chip, "length": 5}], [{**chip, "length": 40}],
                      [{**chip, "start": "0"}], [chip, chip], [7], "chips"):
            with self.subTest(chips=str(chips)[:80]):
                with self.assertRaisesRegex(ValueError, "no longer match"):
                    parent.handle({"command": "send", "id": parent.chat["id"], "text": text,
                                   "attachments": [{"path": str(attachment)}], "mentions": chips})
                # The composer is sent the current roster so its tints can be corrected.
                self.assertEqual(self.events[-1]["event"], "team")
                self.assertEqual(len(self.events[-1]["team"]["members"]), 2)
        self.assertEqual(parent.chat["entries"], before)
        self.assertFalse((self.root / parent.chat["id"] / "attachments").exists())
        self.assertFalse(any(t.requests for t in self.transports))
        parent.handle({"command": "send", "id": parent.chat["id"], "text": text, "mentions": [chip]})
        self.finish(parent)
        self.assertEqual([len(t.requests) for t in self.transports], [1, 0])
        self.assertEqual(self.served(self.transports[0]), "Member 2")

    def test_an_untinted_tag_is_plain_text(self):
        # The composer drew no chip, so the message is for the whole Team,
        # whatever the worker's own resolver or Unicode tables would make of it.
        parent = self.service([[response("First")], [response("Second")]])
        members = self.configure(parent)
        parent.handle({"command": "send", "id": parent.chat["id"], "text": "@Member 2 go", "mentions": []})
        self.finish(parent)
        self.assertEqual([len(t.requests) for t in self.transports], [1, 1])
        self.assertEqual([m["status"] for m in members], ["done", "done"])
        self.assertNotIn("mentions", [e for e in parent.chat["entries"] if e["kind"] == "user"][-1])

    def test_member_names_must_differ_so_each_tag_reaches_one_member(self):
        parent = self.service([[], []])
        # Case aside, and however their accents are composed.
        for names in (("Sol", "sol"), ("Zoë", "ZOË")):
            specs = [{"choice": parent.models[0]["id"], "name": names[0]}, {"choice": parent.models[1]["id"], "name": names[1]}]
            parent.handle({"command": "configure_team", "id": parent.chat["id"], "enabled": True, "members": specs})
            self.assertIn("different name", self.events[-1]["notice"]); self.assertNotIn("team", parent.chat)

    def test_tagged_message_stands_by_a_member_awaiting_input(self):
        parent = self.service([[decision("needs_input", "Which directory?"), response("Which directory should I use?")],
                               [response("Independent answer")], [response("Second look")]])
        first, second = self.configure(parent)
        self.send(parent); self.finish(parent)
        self.assertEqual([first["status"], second["status"]], ["needs_input", "done"])
        # The question stays in the transcript; it does not hold the tagged member back.
        self.send(parent, "@Member 2 take another look"); self.finish(parent)
        self.assertEqual([first["status"], second["status"]], ["ready", "done"])
        self.assertEqual(parent.chat["team"]["status"], "done")
        self.assertEqual(self.served(self.transports[2]), "Member 2")

    def test_waiting_on_a_member_standing_by_is_refused(self):
        roster = {}
        def wait_on_second(payload, cancel, delta):
            return call("team_status", {"state": "waiting", "next_step": "Use Member 2's audit", "member_id": roster["second"]})
        parent = self.service([[wait_on_second, response("Finished without Member 2")]])
        first, second = self.configure(parent)
        roster["second"] = second["id"]
        self.send(parent, "@Member 1 go"); self.finish(parent)
        # Member 2 will not run until a message addresses it, so it is no dependency.
        self.assertEqual([first["status"], second["status"]], ["done", "ready"])
        self.assertIn("standing by", json.dumps(self.transports[0].requests[-1]))

    def test_late_question_after_a_tagged_update_stands_by_and_releases_its_waiter(self):
        roster, release = {}, threading.Event()
        def wait_on_second(payload, cancel, delta):
            return call("team_status", {"state": "waiting", "next_step": "Use Member 2's audit", "member_id": roster["second"]})
        def held_question(payload, cancel, delta):
            release.wait(3)
            return decision("needs_input", "Which directory?")
        parent = self.service([[wait_on_second, response("Waiting for Member 2"), response("Continued without Member 2")],
                               [held_question, response("Which directory should I use?")],
                               [response("Initial third"), response("Handled the update")]])
        first, second, third = self.configure(parent, 3)
        roster["second"] = second["id"]
        self.send(parent)
        self.wait_for(lambda: first["status"] == "waiting" and third["status"] == "done")
        try:
            self.steer(parent, "@Member 3 handle the update")
        finally:
            release.set()
        self.finish(parent)
        # Member 2's question does not hold back the member the user addressed;
        # it stays Member 2's next step, and Member 1 stops waiting on it.
        self.assertEqual([m["status"] for m in (first, second, third)], ["done", "ready", "done"])
        self.assertEqual(second["nextStep"], "Which directory?")
        self.assertEqual(parent.chat["team"]["status"], "done")
        self.assertIn("standing by", json.dumps(self.transports[0].requests[-1]))
        self.assertIn("handle the update", json.dumps(self.transports[2].requests[-1]))

    def test_tagged_update_queues_its_members_in_tag_order(self):
        started, release = threading.Event(), threading.Event()
        def held(text, signal=None):
            def reply(payload, cancel, delta):
                if signal: signal.set()
                release.wait(3)
                return response(text)
            return reply
        parent = self.service([[response("First"), held("First again")], [response("Second"), held("Second again")],
                               [held("Third", started)]])
        first, second, third = self.configure(parent, 3)
        self.send(parent); self.assertTrue(started.wait(2))
        self.wait_for(lambda: first["status"] == "done" and second["status"] == "done")
        try:
            self.steer(parent, "@Member 2 then @Member 1: compare notes")
            self.assertEqual(parent.chat["team"]["queue"], [third["id"], second["id"], first["id"]])
        finally:
            release.set()
        self.finish(parent)
        self.assertEqual([len(t.requests) for t in self.transports], [2, 2, 1])


if __name__ == "__main__": unittest.main()
