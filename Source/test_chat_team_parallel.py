"""Parallel Team boundaries use barriers rather than scheduler timing guesses."""
import copy
import json
import threading
import unittest
from unittest.mock import patch

import chat_team
from chat_runtime import ChatService
from chat_tools import ChatToolRunner
from test_chat_agents import call
from test_chat_runtime import response
import test_chat_team as fixtures

decision = fixtures.decision


class ParallelTeamTests(unittest.TestCase):
    setUp = fixtures.TeamTests.setUp
    service = fixtures.TeamTests.service
    configure = fixtures.TeamTests.configure
    send = fixtures.TeamTests.send
    finish = fixtures.TeamTests.finish
    wait_for = fixtures.TeamTests.wait_for

    def test_two_same_account_streams_overlap_and_usage_is_published_per_member(self):
        barrier = threading.Barrier(3)
        release = threading.Event()
        self.addCleanup(release.set)
        def streaming(payload, cancel, delta):
            delta("Live reply")
            barrier.wait(2)
            release.wait(3)
            return response("Final reply")
        parent = self.service([[streaming], [streaming]])
        specs = [{"choice": parent.models[0]["id"], "name": f"Member {i}", "effort": "", "responsibility": ""}
                 for i in (1, 2)]
        parent.handle({"command": "configure_team", "id": parent.chat["id"], "enabled": True, "members": specs})
        members = parent.chat["team"]["members"]
        self.send(parent)
        try:
            barrier.wait(2)
            snapshot = chat_team.public(parent.chat["team"])
            self.assertEqual(set(snapshot["activeMemberIDs"]), {m["id"] for m in members})
            self.assertIn(snapshot["activeMemberID"], snapshot["activeMemberIDs"])
            self.assertEqual([m["status"] for m in snapshot["members"]], ["working", "working"])
            self.assertEqual([m["context"] for m in snapshot["members"]], [100000, 100000])
            self.assertEqual(len({m["contributionID"] for m in snapshot["members"]}), 2)
        finally:
            release.set()
        self.finish(parent)
        self.assertEqual(parent.chat["team"]["activeMemberIDs"], [])
        self.assertEqual([m["usage"] for m in members], [12, 12])
        live = [e["team"] for e in self.events if e.get("event") == "team"]
        self.assertTrue(any(any(m["usage"] == 12 for m in s["members"]) and
                            any(m["status"] == "working" for m in s["members"]) for s in live))

    def test_four_members_contribute_in_one_wave(self):
        together = threading.Barrier(chat_team.MAX_MEMBERS)
        def meet(payload, cancel, delta):
            together.wait(2)
            return response("Contributed")
        parent = self.service([[meet] for _ in range(chat_team.MAX_MEMBERS)])
        members = self.configure(parent, count=chat_team.MAX_MEMBERS)
        self.assertEqual(len(members), 4)
        self.send(parent); self.finish(parent)
        self.assertEqual(parent.chat["team"]["status"], "done")
        self.assertEqual([m["contributions"] for m in members], [1, 1, 1, 1])

    def test_write_fifo_never_overlaps_but_read_tools_do(self):
        models = threading.Barrier(3)
        reads = threading.Barrier(2)
        write_started, release_write = threading.Event(), threading.Event()
        self.addCleanup(release_write.set)
        first_write = []
        guard = threading.Lock()
        busy = 0
        order = []
        class Runner(ChatToolRunner):
            def execute(inner, name, args):
                nonlocal busy
                if name == "read_file":
                    reads.wait(2)
                    return {"content": [{"type": "text", "text": "read"}], "is_error": False,
                            "summary": "Read", "changed_files": []}
                with guard:
                    busy += 1
                    self.assertEqual(busy, 1, "Two writers entered the workspace")
                    order.append(args["command"])
                    if not first_write: first_write.append(args["command"])
                write_started.set()
                release_write.wait(3)
                with guard: busy -= 1
                return {"content": [{"type": "text", "text": "written"}], "is_error": False,
                        "summary": "Shell", "changed_files": []}
        def inspect(payload, cancel, delta):
            models.wait(2)
            return call("read_file", {"path": "file"})
        parent = self.service([[inspect, call("run_shell", {"command": "first"}), response("A done")],
                               [inspect, call("run_shell", {"command": "second"}), response("B done")]])
        parent.runner_type = Runner
        parent.chat["approvalMode"] = "yolo"
        self.configure(parent)
        self.send(parent)
        try:
            models.wait(2)
            self.assertTrue(write_started.wait(2))
            self.assertEqual(len(order), 1)
        finally:
            release_write.set()
        self.finish(parent)
        self.assertEqual(set(order), {"first", "second"})
        self.assertEqual(busy, 0)

    def test_approvals_are_queued_and_allow_deny_resolve_only_their_member(self):
        together = threading.Barrier(2)
        def command(payload, cancel, delta):
            together.wait(2)
            name = payload["model"].split("/")[-1]
            return call("apply_patch", {"patch": f"*** Begin Patch\n*** Add File: {name}.txt\n+saved\n*** End Patch"})
        parent = self.service([[command, response("A settled")], [command, response("B settled")]])
        members = self.configure(parent)
        self.send(parent)
        self.wait_for(lambda: parent.approval is not None)
        first = copy.deepcopy(parent.approval)
        self.assertIn(first["memberID"], {m["id"] for m in members})
        parent.handle({"command": "approve", "id": first["id"], "allow": False})
        self.wait_for(lambda: parent.approval is not None and parent.approval["id"] != first["id"])
        second = copy.deepcopy(parent.approval)
        self.assertNotEqual(first["memberID"], second["memberID"])
        with self.assertRaisesRegex(ValueError, "no longer pending"):
            parent.handle({"command": "approve", "id": first["id"], "allow": True})
        parent.handle({"command": "approve", "id": second["id"], "allow": True})
        self.finish(parent)
        tools = [e for e in parent.chat["entries"] if e["kind"] == "tool"]
        by_member = {e["memberID"]: e for e in tools}
        self.assertTrue(by_member[first["memberID"]]["isError"])
        self.assertFalse(by_member[second["memberID"]]["isError"])
        self.assertEqual(len(list(self.root.glob("*.txt"))), 1)

    def test_stop_reaches_every_live_provider_and_retains_every_result(self):
        streams = threading.Barrier(3)
        cancelled = []
        def waiting(payload, cancel, delta):
            delta("Partial")
            streams.wait(2)
            self.assertTrue(cancel.wait(3))
            cancelled.append(payload["model"])
            raise InterruptedError("Stopped")
        parent = self.service([[waiting], [waiting]])
        members = self.configure(parent)
        self.send(parent)
        streams.wait(2)
        parent.handle({"command": "stop"})
        self.finish(parent)
        self.assertEqual(len(cancelled), 2)
        self.assertEqual([m["status"] for m in members], ["stopped", "stopped"])
        self.assertEqual(parent.chat["team"]["activeMemberIDs"], [])
        self.assertEqual(len([e for e in parent.chat["entries"] if e.get("text") == "Partial"]), 2)
        self.assertTrue(all(e["recorded"] for e in parent.chat["entries"] if e["kind"] == "assistant"))

    def test_rate_limited_member_does_not_cancel_peer_or_its_continuation(self):
        together = threading.Barrier(2)
        def throttled(payload, cancel, delta):
            together.wait(2)
            raise ValueError("Provider returned HTTP 429; retry later")
        def running(payload, cancel, delta):
            together.wait(2)
            self.assertFalse(cancel.is_set())
            return decision("continue", "Finish independent verification")
        parent = self.service([[throttled, *[ValueError("Provider returned HTTP 429; retry later") for _ in range(4)]],
                               [running, response("Progress saved"), response("Verification complete")]])
        members = self.configure(parent)
        with patch("chat_runtime.REQUEST_BACKOFF", (0, 0, 0, 0)), patch("chat_runtime.random.uniform", return_value=0):
            self.send(parent); self.finish(parent)
        self.assertEqual(len(self.transports[0].requests), 5)
        self.assertEqual([m["status"] for m in members], ["error", "done"])
        self.assertEqual([m["contributions"] for m in members], [0, 2])
        self.assertEqual(parent.chat["team"]["status"], "error")
        loaded = self.store.load(parent.chat["id"])
        self.assertTrue(any(e.get("text") == "Verification complete" for e in loaded["entries"]))

    def test_steer_reaches_all_members_at_tool_boundary_without_cancelling_streams(self):
        together = threading.Barrier(3)
        release = threading.Event()
        self.addCleanup(release.set)
        def working(payload, cancel, delta):
            together.wait(2)
            release.wait(3)
            self.assertFalse(cancel.is_set())
            return call("read_file", {"path": "missing"})
        def updated(payload, cancel, delta):
            self.assertEqual(json.dumps(payload).count("New direction for both"), 1)
            return response("Updated")
        parent = self.service([[working, updated], [working, updated]])
        self.configure(parent)
        self.send(parent)
        try:
            together.wait(2)
            parent.handle({"command": "steer", "id": parent.chat["id"], "text": "New direction for both"})
        finally:
            release.set()
        self.finish(parent)
        self.assertEqual(parent.chat["team"]["status"], "done")
        self.assertFalse(any(e["kind"] in {"notice", "error"} and "Stopped" in e.get("text", "") for e in parent.chat["entries"]))

    def test_every_round_shares_completed_peer_output_once_after_streaming_gap(self):
        started, release_peer, peer_saved = threading.Event(), threading.Event(), threading.Event()
        self.addCleanup(release_peer.set)
        def producer(payload, cancel, delta):
            delta("Unfinished peer fragment")
            started.set()
            self.assertTrue(release_peer.wait(3))
            return response("Recorded peer answer")
        def before_record(payload, cancel, delta):
            self.assertTrue(started.wait(2))
            self.assertNotIn("Unfinished peer fragment", json.dumps(payload))
            return call("read_file", {"path": "first"})
        def wait_for_record(payload, cancel, delta):
            self.assertNotIn("Unfinished peer fragment", json.dumps(payload))
            release_peer.set()
            self.assertTrue(peer_saved.wait(2))
            return call("read_file", {"path": "second"})
        def inspect_shared(payload, cancel, delta):
            wire = json.dumps(payload)
            self.assertEqual(wire.count("Recorded peer answer"), 1)
            self.assertNotIn("Unfinished peer fragment", wire)
            self.assertIn("Recorded Team output", wire)
            return call("read_file", {"path": "third"})
        def no_repeat(payload, cancel, delta):
            self.assertEqual(json.dumps(payload).count("Recorded peer answer"), 1)
            return response("Used peer evidence")
        parent = self.service([[producer], [before_record, wait_for_record, inspect_shared, no_repeat]])
        first, second = self.configure(parent)
        original = chat_team.MemberStore.save
        def saved(store, chat):
            result = original(store, chat)
            if store.member["id"] == first["id"] and store.member["status"] == "done": peer_saved.set()
            return result
        with patch.object(chat_team.MemberStore, "save", saved):
            self.send(parent); self.finish(parent)
        self.assertEqual([first["status"], second["status"]], ["done", "done"])
        self.assertEqual(second["sharedDeferred"], [])

    def test_interleaved_cursor_holes_survive_journal_recovery_without_repeat(self):
        from chat_runtime import entry
        parent = self.service([[], [], []])
        source, other, reader = self.configure(parent, 3)
        first = entry("assistant", "Still streaming", memberID=source["id"], memberName=source["name"], recorded=False)
        later = entry("tool", "", memberID=other["id"], memberName=other["name"], tool="read_file", detail="Recorded later result", recorded=True)
        parent.chat["entries"] += [first, later]
        projection = chat_team.shared_context(parent.chat, reader, parent.choice(reader["choice"]))
        reader["messages"] = projection
        chat_team.checkpoint(parent, reader, {first["id"]: first, later["id"]: later})
        restored = self.store.load(parent.chat["id"])
        restored_reader = restored["team"]["members"][2]
        self.assertEqual(restored_reader["sharedDeferred"], [first["id"]])
        self.assertEqual(chat_team.shared_context(restored, restored_reader, parent.choice(reader["choice"])), [])
        gap = next(e for e in restored["entries"] if e["id"] == first["id"])
        gap.update(text="Completed earlier row", recorded=True)
        delta = chat_team.shared_context(restored, restored_reader, parent.choice(reader["choice"]))
        self.assertIn("Completed earlier row", json.dumps(delta))
        self.assertNotIn("Recorded later result", json.dumps(delta))
        self.assertEqual(chat_team.shared_context(restored, restored_reader, parent.choice(reader["choice"])), [])

    def test_bounded_shared_delta_defers_overflow_then_delivers_it_once(self):
        from chat_runtime import entry
        parent = self.service([[], []])
        writer, reader = self.configure(parent)
        older = entry("assistant", "Earlier finding " + "a" * 300, memberID=writer["id"], recorded=True)
        newer = entry("assistant", "Newer finding " + "b" * 300, memberID=writer["id"], recorded=True)
        parent.chat["entries"] += [older, newer]
        with patch.object(chat_team, "MAX_SHARED_BYTES", 500):
            first = chat_team.shared_context(parent.chat, reader, parent.choice(reader["choice"]))
            self.assertIn("Newer finding", json.dumps(first))
            self.assertNotIn("Earlier finding", json.dumps(first))
            second = chat_team.shared_context(parent.chat, reader, parent.choice(reader["choice"]))
            self.assertIn("Earlier finding", json.dumps(second))
            self.assertNotIn("Newer finding", json.dumps(second))
            self.assertEqual(chat_team.shared_context(parent.chat, reader, parent.choice(reader["choice"])), [])

    def test_shared_delta_shortens_peer_tool_output_and_bounds_the_backlog(self):
        from chat_runtime import entry
        parent = self.service([[], []])
        writer, reader = self.configure(parent)
        tool = entry("tool", tool="run_shell", summary="ls", detail="T" * 3000 + "TAIL", memberID=writer["id"], recorded=True)
        reply = entry("assistant", "R" * 3000 + "KEPT", memberID=writer["id"], recorded=True)
        parent.chat["entries"] += [tool, reply]
        delta = json.dumps(chat_team.shared_context(parent.chat, reader, parent.choice(reader["choice"])))
        self.assertNotIn("TAIL", delta)
        self.assertIn("T" * chat_team.MAX_SHARED_TOOL, delta)
        self.assertIn("KEPT", delta)
        self.assertIn("shortened", delta)
        parent.chat["entries"] += [entry("assistant", f"Finding {i} " + "x" * 300, memberID=writer["id"], recorded=True)
                                   for i in range(60)]
        with patch.object(chat_team, "MAX_SHARED_BYTES", 500), patch.object(chat_team, "MAX_SHARED_BACKLOG", 5):
            first = json.dumps(chat_team.shared_context(parent.chat, reader, parent.choice(reader["choice"])))
            self.assertIn("Finding 59 ", first)
            self.assertIn("54 older peer records were omitted", first)
            self.assertEqual(len(reader["sharedDeferred"]), 5)
            delivered = [json.dumps(chat_team.shared_context(parent.chat, reader, parent.choice(reader["choice"]))) for _ in range(5)]
            self.assertIn("Finding 58 ", delivered[0])
            self.assertEqual(chat_team.shared_context(parent.chat, reader, parent.choice(reader["choice"])), [])

    def test_parallel_notebook_writes_keep_both_source_linked_notes(self):
        together = threading.Barrier(2)
        def remember(payload, cancel, delta):
            together.wait(2)
            key = "alpha" if payload["model"] == parent.models[0]["route"] else "beta"
            source = next(e["id"] for e in parent.chat["entries"] if e["kind"] == "user")
            return call("record_decision", {"key": key, "text": key + " finding", "source_ids": [source]})
        parent = self.service([[remember, response("Alpha saved")], [remember, response("Beta saved")]])
        self.configure(parent)
        self.send(parent); self.finish(parent)
        loaded = self.store.load(parent.chat["id"])
        self.assertEqual({n["key"] for n in loaded["decisions"]}, {"alpha", "beta"})
        self.assertTrue(all(n["source_ids"] for n in loaded["decisions"]))
        self.assertEqual(parent.chat["team"]["status"], "done")

    def test_fifo_gate_removes_cancelled_waiter_and_preserves_next_owner(self):
        gate = chat_team.FifoGate()
        owner_started, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        cancels = [threading.Event() for _ in range(3)]
        order, stopped = [], []
        def enter(index):
            try:
                with gate.enter(cancels[index]):
                    order.append(index)
                    if index == 0:
                        owner_started.set(); release.wait(3)
            except InterruptedError:
                stopped.append(index)
        threads = [threading.Thread(target=enter, args=(i,)) for i in range(3)]
        threads[0].start()
        self.assertTrue(owner_started.wait(2))
        threads[1].start()
        self.wait_for(lambda: len(gate.queue) == 2)
        threads[2].start()
        self.wait_for(lambda: len(gate.queue) == 3)
        cancels[1].set()
        threads[1].join(2)
        release.set()
        for thread in threads: thread.join(2)
        self.assertEqual(order, [0, 2])
        self.assertEqual(stopped, [1])
        self.assertEqual(list(gate.queue), [])

    def test_stop_while_approving_cancels_all_queued_writes(self):
        together = threading.Barrier(2)
        def request(payload, cancel, delta):
            together.wait(2)
            return call("apply_patch", {"patch": "*** Begin Patch\n*** Add File: stopped.txt\n+no\n*** End Patch"})
        parent = self.service([[request], [request]])
        members = self.configure(parent)
        self.send(parent)
        self.wait_for(lambda: parent.approval is not None)
        parent.handle({"command": "stop"})
        self.finish(parent)
        self.assertIsNone(parent.approval)
        self.assertFalse((self.root / "stopped.txt").exists())
        self.assertEqual([m["status"] for m in members], ["stopped", "stopped"])

    def test_crash_mid_parallel_tools_recovers_both_histories_and_marks_uncertainty(self):
        tools = threading.Barrier(3)
        class Runner(ChatToolRunner):
            def execute(inner, name, args):
                tools.wait(2)
                inner.cancel_event.wait(3)
                raise InterruptedError("Stopped")
        parent = self.service([[call("read_file", {"path": "one"})], [call("read_file", {"path": "two"})]])
        parent.runner_type = Runner
        self.configure(parent)
        self.send(parent)
        try:
            tools.wait(2)
            with parent._mutex: crashed = self.store.load(parent.chat["id"])
            self.assertEqual(len(crashed["team"]["activeMemberIDs"]), 2)
            chat_team.recover(crashed, ChatService.settle)
            chat_team.recover(crashed, ChatService.settle)
            self.assertEqual([m["status"] for m in crashed["team"]["members"]], ["interrupted", "interrupted"])
            self.assertEqual(crashed["team"]["activeMemberIDs"], [])
            for member in crashed["team"]["members"]:
                result = member["messages"][-1]["content"][0]
                self.assertEqual(result["type"], "tool_result")
                self.assertTrue(result["is_error"])
            rows = [e for e in crashed["entries"] if e["kind"] == "tool"]
            self.assertEqual(len(rows), 2)
            self.assertTrue(all(e["isError"] and e["recorded"] for e in rows))
        finally:
            parent.handle({"command": "stop"})
            self.finish(parent)

    def test_failed_journal_wakes_peer_approval_and_stops_queued_mutation(self):
        def final_reply(payload, cancel, delta):
            self.wait_for(lambda: parent.approval is not None)
            return response("Recorded first reply")
        patch_text = "*** Begin Patch\n*** Add File: unsafe-after-save.txt\n+no\n*** End Patch"
        parent = self.service([[final_reply], [call("apply_patch", {"patch": patch_text})]])
        first, second = self.configure(parent)
        original = chat_team.checkpoint
        def fail_final(parent, member=None, dirty=None):
            if member and member["id"] == first["id"] and member.get("terminalStatus") == "ready":
                raise OSError("Journal unavailable")
            return original(parent, member, dirty)
        with patch.object(chat_team, "checkpoint", fail_final):
            self.send(parent); self.finish(parent)
        self.assertEqual(parent.chat["team"]["status"], "error")
        self.assertIsNone(parent.approval)
        self.assertFalse((self.root / "unsafe-after-save.txt").exists())
        self.assertEqual(second["status"], "stopped")

    def test_real_tool_output_matching_running_placeholder_is_still_recorded(self):
        waiting = threading.Event()
        class Runner(ChatToolRunner):
            def execute(inner, name, args):
                return {"content": [{"type": "text", "text": "Running…"}], "is_error": False,
                        "summary": "Read", "changed_files": []}
        def wait_for_stop(payload, cancel, delta):
            waiting.set(); cancel.wait(3); raise InterruptedError("Stopped")
        parent = self.service([[call("read_file", {"path": "file"}), wait_for_stop], [response("Peer finished")]])
        parent.runner_type = Runner
        first, reader = self.configure(parent)
        self.send(parent); self.assertTrue(waiting.wait(2))
        parent.handle({"command": "stop"}); self.finish(parent)
        result = next(e for e in parent.chat["entries"] if e.get("tool") == "read_file")
        self.assertTrue(result["recorded"])
        self.assertFalse(result["isError"])
        self.assertEqual(result["detail"], "Running…")
        reader["cursor"] = 0
        shared = chat_team.shared_context(parent.chat, reader, parent.choice(reader["choice"]))
        self.assertIn(result["id"], json.dumps(shared))

    def test_peer_checkpoint_preserves_interleaved_row_order_before_origin_saves(self):
        origin_started, peer_checkpointed = threading.Event(), threading.Event()
        release = threading.Event()
        self.addCleanup(release.set)
        def origin(payload, cancel, delta):
            delta("In-flight origin")
            origin_started.set(); release.wait(3)
            if cancel.is_set(): raise InterruptedError("Stopped")
            return response("Recorded origin")
        def peer_read(payload, cancel, delta):
            self.assertTrue(origin_started.wait(2))
            return call("read_file", {"path": "missing"})
        def peer_wait(payload, cancel, delta):
            peer_checkpointed.set(); release.wait(3)
            if cancel.is_set(): raise InterruptedError("Stopped")
            return response("Peer finished")
        parent = self.service([[origin], [peer_read, peer_wait]])
        first, reader = self.configure(parent)
        self.send(parent)
        try:
            self.assertTrue(peer_checkpointed.wait(2))
            with parent._mutex:
                expected = [e["id"] for e in parent.chat["entries"][:reader["cursor"]]]
                restored = self.store.load(parent.chat["id"])
            self.assertEqual([e["id"] for e in restored["entries"][:reader["cursor"]]], expected)
            restored_reader = restored["team"]["members"][1]
            gap = next(e for e in restored["entries"] if e.get("memberID") == first["id"])
            self.assertFalse(gap["recorded"])
            self.assertIn(gap["id"], restored_reader["sharedDeferred"])
            gap.update(text="Newly recorded origin", recorded=True)
            shared = chat_team.shared_context(restored, restored_reader, parent.choice(reader["choice"]))
            self.assertIn("Newly recorded origin", json.dumps(shared))
            self.assertEqual(chat_team.shared_context(restored, restored_reader, parent.choice(reader["choice"])), [])
        finally:
            parent.handle({"command": "stop"}); release.set(); self.finish(parent)

if __name__ == "__main__": unittest.main()
