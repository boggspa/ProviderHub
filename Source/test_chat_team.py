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
        self.assertEqual([e["text"] for e in answers], ["First result", "Second result", "Third result"])
        self.assertEqual([e["memberID"] for e in answers], [m["id"] for m in members])
        self.assertEqual(len({e["contributionID"] for e in answers}), 3)
        self.assertIn("First result", json.dumps(self.transports[1].requests[0]))
        self.assertIn("Member 1", json.dumps(self.transports[1].requests[0]))
        self.assertIn("Inspect the project", json.dumps(self.transports[2].requests[0]))
        self.assertNotIn("messages", json.dumps([e for e in self.events if e["event"] == "team"]))
        self.assertNotIn("team", self.store.headers()[0])

    def test_explicit_continuation_rotates_fairly_and_must_be_renewed(self):
        parent = self.service([[decision("continue", "Verify the tests"), response("Implementation done"), response("Tests passed")],
                               [response("Review complete")]])
        members = self.configure(parent)
        self.send(parent); self.finish(parent)
        answers = [e for e in parent.chat["entries"] if e["kind"] == "assistant" and e["text"]]
        self.assertEqual([e["text"] for e in answers], ["Implementation done", "Review complete", "Tests passed"])
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

    def test_needs_input_pauses_rest_of_roster_and_resume_needs_real_answer(self):
        parent = self.service([[decision("needs_input", "Which directory?"), response("Which directory should I use?")],
                               [response("Answered now")], [response("Other member answered")]])
        members = self.configure(parent)
        self.send(parent); self.finish(parent)
        self.assertEqual([m["status"] for m in members], ["needs_input", "queued"])
        parent.handle({"command": "team_resume", "id": parent.chat["id"], "request": "resume"})
        self.assertIn("answer", self.events[-1]["notice"])
        self.assertFalse(self.transports[1].requests)
        self.send(parent, "Use the current directory"); self.finish(parent)
        self.assertEqual([m["status"] for m in members], ["done", "done"])
        self.assertIn("Use the current directory", json.dumps(self.transports[1].requests))

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
        for specs in ([], [good] * 4, [good, {"choice": "missing", "name": "Other"}]):
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
        parent = self.service([[call("apply_patch", {"patch": patch_text}), response("Wrote first")],
                               [call("apply_patch", {"patch": second}), response("Wrote second")]])
        self.configure(parent)
        self.send(parent)
        self.wait_for(lambda: parent.approval is not None)
        self.assertIn("Member 1", parent.approval["summary"])
        self.assertEqual(self.transports[1].requests, [])
        parent.handle({"command": "approve", "id": parent.approval["id"], "allow": True})
        self.wait_for(lambda: parent.approval is not None and "Member 2" in parent.approval["summary"])
        self.assertEqual((self.root / "result.txt").read_text(), "first\n")
        parent.handle({"command": "approve", "id": parent.approval["id"], "allow": True})
        self.finish(parent)
        self.assertEqual((self.root / "result.txt").read_text(), "second\n")
        tools = [e for e in parent.chat["entries"] if e["kind"] == "tool"]
        self.assertEqual([e["memberName"] for e in tools], ["Member 1", "Member 2"])

    def test_stop_cancels_active_and_queued_work_resume_does_not_repeat_finished_member(self):
        started = threading.Event()
        def waiting(payload, cancel, delta):
            started.set(); cancel.wait(3); raise InterruptedError("Stopped")
        parent = self.service([[response("Done A")], [waiting], [response("Resumed B")]])
        members = self.configure(parent)
        self.send(parent); self.assertTrue(started.wait(2))
        parent.handle({"command": "stop"}); self.finish(parent)
        self.assertEqual([m["status"] for m in members], ["done", "stopped"])
        parent.handle({"command": "team_resume", "id": parent.chat["id"], "request": "resume"})
        self.finish(parent)
        self.assertEqual([m["contributions"] for m in parent.chat["team"]["members"]], [1, 1])
        self.assertEqual(len(self.transports[0].requests), 1)

    def test_steer_waits_for_cleanup_then_gives_new_input_to_all_members_once(self):
        started, cleaned = threading.Event(), threading.Event()
        def waiting(payload, cancel, delta):
            started.set(); cancel.wait(3); cleaned.set(); raise InterruptedError("Stopped")
        def restarted(payload, cancel, delta):
            self.assertTrue(cleaned.is_set())
            self.assertIn("New direction", json.dumps(payload)); return response("Updated A")
        parent = self.service([[waiting], [restarted], [response("Updated B")]])
        self.configure(parent)
        self.send(parent); self.assertTrue(started.wait(2))
        parent.handle({"command": "steer", "id": parent.chat["id"], "text": "New direction"})
        self.wait_for(lambda: not parent.busy)
        self.assertEqual(parent.chat["team"]["status"], "done")
        self.assertIn("Updated B", json.dumps(parent.chat["entries"]))

    def test_checkpoint_round_limit_yields_only_when_opted_in(self):
        parent = self.service([[decision("continue", "Finish reading"), call("read_file", {"path": "missing"}), response("Finished")]])
        member = self.configure(parent, 1)[0]
        with patch("chat_runtime.MAX_ROUNDS", 2): self.send(parent); self.finish(parent)
        self.assertEqual(member["contributions"], 2)
        self.assertEqual(member["status"], "done")

    def test_steer_at_terminal_save_delivers_new_instruction_to_every_member(self):
        final_save, release = threading.Event(), threading.Event()
        def restarted(payload, cancel, delta):
            self.assertIn("New direction at the final boundary", json.dumps(payload))
            return response("Followed new direction")
        parent = self.service([[response("Old task finished")], [restarted], [restarted]])
        first = self.configure(parent)[0]
        original = chat_team.MemberStore.save
        def held_save(store, chat):
            if store.member["id"] == first["id"] and chat["status"] == "ready" and not final_save.is_set():
                final_save.set(); release.wait(3)
            return original(store, chat)
        with patch.object(chat_team.MemberStore, "save", held_save):
            self.send(parent); self.assertTrue(final_save.wait(2))
            try:
                parent.handle({"command": "steer", "id": parent.chat["id"], "text": "New direction at the final boundary"})
            finally:
                release.set()
            self.wait_for(lambda: not parent.busy)
        self.assertEqual(parent.chat["team"]["status"], "done")
        self.assertEqual([len(t.requests) for t in self.transports], [1, 1, 1])

    def test_shared_notebook_uses_main_transcript_source_ids(self):
        def remember(payload, cancel, delta):
            source = next(e["id"] for e in parent.chat["entries"] if e["kind"] == "user")
            return call("record_decision", {"key": "goal", "text": "Use this workspace", "source_ids": [source]})
        parent = self.service([[remember, response("Recorded")], [response("Read the note")]])
        self.configure(parent)
        self.send(parent); self.finish(parent)
        self.assertEqual(parent.chat["decisions"][0]["key"], "goal")
        self.assertIn("Use this workspace", json.dumps(self.transports[1].requests[0]))
        source = next(e for e in parent.chat["entries"] if e["kind"] == "assistant" and e["text"] == "Recorded")
        read = chat_memory.execute(parent.chat, "read_history", {"entry_id": source["id"]})
        self.assertIn("Member 1", read["content"][0]["text"])

    def test_failed_member_does_not_requeue_earlier_continue_intent(self):
        parent = self.service([[decision("continue", "Next step"), ValueError("Provider failed")], [response("Should not run")]])
        members = self.configure(parent)
        self.send(parent); self.finish(parent)
        self.assertEqual(parent.chat["team"]["status"], "error")
        self.assertEqual(members[0]["status"], "error")
        self.assertEqual(self.transports[1].requests, [])

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
        parent = self.service([[response("Finished A")], [response("Finished B")]])
        first, second = self.configure(parent)
        original = chat_team.checkpoint
        def stop_after_final(parent, member=None, dirty=None):
            original(parent, member, dirty)
            if member and member["id"] == first["id"] and member["status"] == "done":
                parent.cancel.set()
        with patch.object(chat_team, "checkpoint", side_effect=stop_after_final):
            self.send(parent); self.finish(parent)
        self.assertEqual(first["status"], "done")
        self.assertEqual(parent.chat["team"]["queue"], [second["id"]])
        parent.handle({"command": "team_resume", "id": parent.chat["id"]})
        self.finish(parent)
        self.assertEqual([m["contributions"] for m in parent.chat["team"]["members"]], [1, 1])
        self.assertEqual([len(t.requests) for t in self.transports], [1, 1])

    def test_failed_configuration_save_keeps_previous_roster_and_histories(self):
        parent = self.service([[]]); member = self.configure(parent, 1)[0]
        old = copy.deepcopy(parent.chat)
        with patch.object(self.store, "save", side_effect=OSError("Disk full")):
            parent.handle({"command": "configure_team", "id": parent.chat["id"], "enabled": False,
                           "members": [{"id": member["id"], "choice": parent.models[1]["id"], "name": "Replacement"}]})
        self.assertEqual(parent.chat, old)
        self.assertIn("Disk full", self.events[-1]["notice"])

    def test_failed_final_save_pauses_before_next_member_and_emits_error(self):
        parent = self.service([[response("Done")], [response("Must wait")]])
        self.configure(parent)
        original = chat_team.checkpoint
        def fail_final(parent, member=None, dirty=None):
            if member and member.get("terminalStatus") == "ready": raise OSError("Disk full")
            return original(parent, member, dirty)
        with patch.object(chat_team, "checkpoint", side_effect=fail_final):
            self.send(parent); self.finish(parent)
        self.assertEqual(self.transports[1].requests, [])
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


if __name__ == "__main__": unittest.main()
