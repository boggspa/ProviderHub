"""Concurrent real chat loops with controlled transports and workspace tools."""
import json
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from chat_runtime import ChatStore
from chat_sessions import MAX_ACTIVE_REQUESTS, ChatHost
from test_chat_runtime import FakeTransport, response


class ConcurrentChatTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.store = ChatStore(self.root, lock=False)
        self.events, self.clients, self.pending_children = [], [], []

        def client():
            transport = FakeTransport(self.pending_children.pop(0) if self.pending_children else [])
            transport.root = self.root
            self.clients.append(transport)
            return transport
        self.host = ChatHost(self.store, FakeTransport([]), self.events.append, child_factory=client)
        self.host.models = FakeTransport([]).catalogue()
        self.addCleanup(self.host.shutdown)

    def create(self, folder="project", choice=0):
        workspace = self.root / folder
        workspace.mkdir(parents=True, exist_ok=True)
        self.host.handle({"command": "create", "choice": self.host.models[choice]["id"], "workspace": str(workspace)})
        return self.host.view_id

    def send(self, identifier, replies, text="Work"):
        service = self.host.session(identifier)
        service.transport.client.responses = list(replies)
        self.host.handle({"command": "send", "chat": identifier, "id": identifier, "request": "request-" + identifier, "text": text})
        return service

    def wait(self, predicate):
        deadline = time.monotonic() + 4
        while not predicate() and time.monotonic() < deadline: time.sleep(.005)
        self.assertTrue(predicate())

    def blocked(self, text="Partial"):
        started, release = threading.Event(), threading.Event()
        def run(payload, cancel, delta):
            delta(text)
            started.set()
            deadline = time.monotonic() + 5
            while not release.wait(.005):
                if cancel.is_set(): raise InterruptedError("Stopped")
                if time.monotonic() > deadline: raise RuntimeError("fixture timed out")
            if cancel.is_set(): raise InterruptedError("Stopped")
            return response(text)
        return run, started, release

    def test_models_projects_stream_and_stop_independently(self):
        a, b = self.create("a"), self.create("b", choice=1)
        run_a, started_a, release_a = self.blocked("A 🧑‍💻")
        run_b, started_b, release_b = self.blocked("B")
        first = self.send(a, [run_a]); second = self.send(b, [run_b])
        self.assertTrue(started_a.wait(2) and started_b.wait(2))
        self.assertIsNot(first.transport.client, second.transport.client)
        self.assertEqual(self.host.view_id, b)
        self.assertFalse(any(e.get("event") == "delta" and e.get("chat") == a for e in self.events))
        self.host.handle({"command": "select", "id": a})
        snapshot = [e for e in self.events if e["event"] == "selected"][-1]
        self.assertTrue(snapshot["busy"])
        self.assertEqual(snapshot["entries"][-1]["text"], "A 🧑‍💻")
        self.assertIs(self.host.sessions[a], first)
        self.host.handle({"command": "stop", "chat": a})
        self.wait(lambda: not first.busy)
        self.assertFalse(second.cancel.is_set()); self.assertTrue(second.busy)
        release_b.set(); self.wait(lambda: not second.busy)
        self.assertEqual(self.store.load(a)["status"], "stopped")
        self.assertEqual(self.store.load(b)["status"], "ready")
        self.assertEqual(second.transport.client.requests[0]["model"], "ollama/other")
        self.assertEqual(self.host.view_id, a)
        scoped = {"state", "approval", "entry", "delta", "notice", "error"}
        self.assertTrue(all(e.get("chat") for e in self.events if e["event"] in scoped))

    def test_navigation_creation_and_background_completion_do_not_steal_focus(self):
        a = self.create("a")
        run, started, release = self.blocked()
        service = self.send(a, [run]); self.assertTrue(started.wait(2))
        b = self.create("b")
        self.assertTrue(service.busy); self.assertEqual(self.host.view_id, b)
        release.set(); self.wait(lambda: not service.busy)
        self.assertEqual(self.host.view_id, b)
        self.assertEqual([e for e in self.events if e["event"] == "selected"][-1]["id"], b)
        self.host.handle({"command": "select", "id": a})
        snapshot = [e for e in self.events if e["event"] == "selected"][-1]
        self.assertFalse(snapshot["busy"]); self.assertIsNone(snapshot["approval"])
        self.assertEqual(snapshot["entries"][-1]["text"], "Partial")

    def test_approvals_are_owned_by_chat_and_waiting_does_not_lock_workspace(self):
        a, b = self.create("a"), self.create("b")
        patch_text = "*** Begin Patch\n*** Add File: result.txt\n+written\n*** End Patch"
        first = self.send(a, [response("", patch_text), response("A done")])
        second = self.send(b, [response("", patch_text), response("B denied")])
        self.wait(lambda: first.approval and second.approval)
        wrong = {"command": "approve", "chat": b, "id": first.approval["id"], "allow": True}
        with self.assertRaisesRegex(ValueError, "no longer pending"): self.host.handle(wrong)
        self.assertIsNotNone(first.approval); self.assertIsNotNone(second.approval)
        self.host.handle({"command": "approve", "chat": a, "id": first.approval["id"], "allow": True})
        self.wait(lambda: not first.busy)
        self.assertTrue(second.busy)
        self.assertEqual((self.root / "a/result.txt").read_text(), "written\n")
        self.host.handle({"command": "approve", "chat": b, "id": second.approval["id"], "allow": False})
        self.wait(lambda: not second.busy)
        self.assertFalse((self.root / "b/result.txt").exists())

    def test_fifth_turn_and_duplicate_send_are_rejected_before_recording(self):
        running = []
        for index in range(4):
            identifier = self.create(str(index))
            run, started, release = self.blocked()
            service = self.send(identifier, [run])
            self.assertTrue(started.wait(2)); running.append(service)
        fifth = self.create("fifth")
        command = {"command": "send", "chat": fifth, "text": "Keep in draft"}
        with self.assertRaisesRegex(ValueError, "4 chats") as rejected: self.host.handle(command)
        self.host.reject(command, rejected.exception)
        self.assertEqual(self.events[-1]["event"], "rejected")
        self.assertEqual(self.events[-1]["chat"], fifth)
        self.assertEqual(self.store.load(fifth)["entries"], [])
        count = len(running[0].chat["entries"])
        with self.assertRaisesRegex(ValueError, "already running"):
            self.host.handle({"command": "send", "chat": running[0].chat["id"], "text": "Second input"})
        self.assertEqual(len(running[0].chat["entries"]), count)

    def test_submission_identity_survives_reload_and_never_enters_provider_context(self):
        identifier = self.create()
        service = self.send(identifier, [response()], text="Continue")
        self.wait(lambda: not service.busy)
        saved = self.store.load(identifier)
        self.assertEqual(saved["entries"][0]["clientRequest"], "request-" + identifier)
        self.assertNotIn("clientRequest", json.dumps(service.transport.client.requests))
        self.host.handle({"command": "select", "id": identifier})
        self.assertEqual([e for e in self.events if e["event"] == "selected"][-1]["entries"][0]["clientRequest"], "request-" + identifier)

    def test_rename_delete_and_rekey_use_the_live_owner(self):
        a = self.create("a")
        self.host.handle({"command": "rename", "chat": a, "title": "Saved title"})
        original = self.host.sessions[a]
        original.save(); self.assertEqual(self.store.load(a)["title"], "Saved title")
        b = self.create("b")
        self.host.handle({"command": "configure", "chat": a, "choice": self.host.models[1]["id"]})
        replacement = next(row["id"] for row in self.host.session_store.headers() if row["workspace"] == str(self.root / "a"))
        self.assertNotEqual(replacement, a); self.assertNotIn(a, self.host.sessions)
        self.assertEqual(self.host.view_id, b)
        run, started, release = self.blocked()
        service = self.send(replacement, [run]); self.assertTrue(started.wait(2))
        for action in ("delete", "rename", "configure"):
            with self.assertRaisesRegex(ValueError, "Stop this chat"):
                self.host.handle({"command": action, "chat": replacement, "title": "No"})
        self.host.handle({"command": "delete", "chat": b})
        self.assertTrue(service.busy); self.assertFalse(self.store.path(b).exists())

    def test_captured_approval_cannot_follow_a_workspace_change(self):
        identifier = self.create("a")
        with self.assertRaisesRegex(ValueError, "workspace changed"):
            self.host.handle({"command": "configure", "chat": identifier, "approvalMode": "yolo", "expectedWorkspace": str(self.root / "b")})
        self.assertEqual(self.store.load(identifier)["approvalMode"], "manual")

    def test_header_cache_and_idle_eviction_do_not_read_all_transcripts(self):
        a, b = self.create("a"), self.create("b")
        self.assertNotIn(a, self.host.sessions)
        with patch.object(self.store, "headers", side_effect=AssertionError("Repeated disk header scan")):
            self.host.publish_summaries()
            self.host.handle({"command": "select", "id": a})
        self.assertNotIn(b, self.host.sessions)
        self.assertEqual(self.host.sessions[a].chat["id"], a)

    def test_read_only_turn_does_not_spawn_git_for_concurrency_bookkeeping(self):
        identifier = self.create()
        with patch("chat_sessions.subprocess.run", side_effect=AssertionError("Unexpected Git lookup")):
            service = self.send(identifier, [response("Read-only reply")])
            self.wait(lambda: not service.busy)
        self.assertEqual(service.chat["status"], "ready")

    def test_stop_wakes_checkout_waiter_without_executing_its_tool(self):
        workspace = self.root / "repo"; workspace.mkdir()
        entered, release, stopped = threading.Event(), threading.Event(), threading.Event()
        called = []
        self.addCleanup(release.set)
        class Runner:
            def __init__(self, workspace, cancel_event): pass
            def execute(self, name, args):
                called.append(args["id"])
                entered.set(); release.wait(3)
                return {"is_error": False}
        self.host.runner_type = Runner
        first = self.host.runner(str(workspace), cancel_event=threading.Event())
        cancel = threading.Event()
        second = self.host.runner(str(workspace), cancel_event=cancel)
        holder = threading.Thread(target=first.execute, args=("run_shell", {"id": "first"}))
        holder.start(); self.assertTrue(entered.wait(2))
        def wait_for_workspace():
            try: second.execute("apply_patch", {"id": "second"})
            except InterruptedError: stopped.set()
        waiting = threading.Thread(target=wait_for_workspace)
        waiting.start(); self.assertFalse(stopped.wait(.05))
        cancel.set(); self.host.wake_waiters()
        self.assertTrue(stopped.wait(2)); waiting.join(2)
        self.assertEqual(called, ["first"])
        release.set(); holder.join(2)

    def test_workspace_writers_serialize_checkout_aliases_but_other_projects_progress(self):
        repository = self.root / "repo"
        repository.mkdir(); (repository / "nested").mkdir()
        subprocess.run(["git", "init", "-q", str(repository)], check=True)
        other = self.root / "other"; other.mkdir()
        entered = [threading.Event() for _ in range(3)]
        release = threading.Event()
        self.addCleanup(release.set)
        class Runner:
            def __init__(self, workspace, cancel_event): self.workspace = Path(workspace)
            def execute(self, name, args):
                entered[args["index"]].set()
                if args["index"] == 0: release.wait(3)
                return {"is_error": False}
        self.host.runner_type = Runner
        runners = [self.host.runner(str(path), cancel_event=threading.Event()) for path in (repository, repository / "nested", other)]
        threads = [threading.Thread(target=runner.execute, args=("run_shell", {"index": index})) for index, runner in enumerate(runners)]
        threads[0].start(); self.assertTrue(entered[0].wait(2))
        threads[1].start(); threads[2].start()
        self.assertTrue(entered[2].wait(2)); self.assertFalse(entered[1].wait(.05))
        release.set()
        for thread in threads: thread.join(2); self.assertFalse(thread.is_alive())
        self.assertTrue(entered[1].is_set())

    def test_branch_guard_sees_active_peer_in_same_checkout(self):
        repository = self.root / "repo"
        repository.mkdir(); (repository / "nested").mkdir()
        subprocess.run(["git", "init", "-q", str(repository)], check=True)
        a, b = self.create("repo"), self.create("repo/nested")
        run, started, release = self.blocked()
        service = self.send(b, [run]); self.assertTrue(started.wait(2))
        with patch("chat_inspector.switch_branch") as switch:
            self.host.handle({"command": "branch_action", "chat": a, "request": "branch", "action": "switch", "branch": "next"})
            switch.assert_not_called()
        notice = next(e for e in reversed(self.events) if e["event"] == "branch_state")
        self.assertFalse(notice["busy"]); self.assertIn("other chats", notice["notice"])
        self.assertTrue(service.busy)
        self.host.session(a)._branch_working = True
        idle = self.create("repo/idle")
        with self.assertRaisesRegex(ValueError, "branch operation"):
            self.host.handle({"command": "send", "chat": idle, "text": "Wait"})
        self.host.session(a)._branch_working = False
        self.assertEqual(self.store.load(idle)["entries"], [])

    def test_team_and_solo_chat_progress_on_separate_transports(self):
        a, b = self.create("team"), self.create("solo", choice=1)
        self.host.handle({"command": "configure_team", "chat": a, "request": "roster", "enabled": True,
                          "members": [{"name": "Member", "choice": self.host.models[0]["id"], "effort": "", "responsibility": "Inspect"}]})
        team_run, team_started, team_release = self.blocked("Team result")
        solo_run, solo_started, solo_release = self.blocked("Solo result")
        self.pending_children.append([team_run])
        team = self.send(a, [])
        solo = self.send(b, [solo_run])
        self.assertTrue(team_started.wait(2) and solo_started.wait(2))
        self.assertTrue(team.busy and solo.busy)
        self.assertEqual(self.host.view_id, b)
        team_release.set(); self.wait(lambda: not team.busy)
        self.assertTrue(solo.busy)
        solo_release.set(); self.wait(lambda: not solo.busy)
        self.assertTrue(any(item.get("memberID") for item in self.store.load(a)["entries"]))
        self.assertEqual(self.store.load(b)["entries"][-1]["text"], "Solo result")

    def test_side_chat_cap_is_shared_across_all_parents(self):
        for index in range(9):
            identifier = self.create(str(index))
            self.host.handle({"command": "open_side", "chat": identifier, "request": str(index), "choice": self.host.models[0]["id"]})
        self.assertEqual(len(self.host.sides), 8)
        self.assertIn("Eight temporary", self.events[-1]["notice"])

    def configure_parallel_team(self, identifier):
        self.host.handle({"command": "configure_team", "chat": identifier, "request": "team-" + identifier,
                          "enabled": True, "members": [
                              {"name": "Member " + str(index), "choice": self.host.models[0]["id"],
                               "effort": "", "responsibility": "Inspect"} for index in (1, 2)]})
        return self.host.session(identifier)

    def test_parallel_teams_and_solo_chat_keep_updates_and_stop_scoped(self):
        from test_chat_agents import call
        a, b, solo_id = self.create("team-a"), self.create("team-b"), self.create("solo")
        together = threading.Barrier(5)
        release_a = threading.Event()
        self.addCleanup(release_a.set)
        def active_a(payload, cancel, delta):
            delta("A partial")
            together.wait(3); self.assertTrue(release_a.wait(3))
            self.assertFalse(cancel.is_set())
            return call("read_file", {"path": "missing"})
        def updated_a(payload, cancel, delta):
            self.assertEqual(json.dumps(payload).count("Update only Team A"), 1)
            return response("A updated")
        def active_b(payload, cancel, delta):
            delta("B partial")
            together.wait(3)
            self.assertTrue(cancel.wait(3))
            raise InterruptedError("Stopped")
        first = self.configure_parallel_team(a)
        self.pending_children.extend([[active_a, updated_a], [active_a, updated_a]])
        self.send(a, [])
        self.wait(lambda: len(first.team_children) == 2)
        second = self.configure_parallel_team(b)
        self.pending_children.extend([[active_b], [active_b]])
        self.send(b, [])
        self.wait(lambda: len(second.team_children) == 2)
        solo_run, solo_started, solo_release = self.blocked("Independent solo result")
        self.addCleanup(solo_release.set)
        solo = self.send(solo_id, [solo_run])
        try:
            together.wait(3); self.assertTrue(solo_started.wait(2))
            self.assertEqual(self.host._requests.active, 5)
            self.assertEqual(len(first.chat["team"]["activeMemberIDs"]), 2)
            self.assertEqual(len(second.chat["team"]["activeMemberIDs"]), 2)
            self.host.handle({"command": "steer", "chat": a, "text": "Update only Team A"})
            self.host.handle({"command": "stop", "chat": b})
            self.wait(lambda: not second.busy)
            self.assertTrue(first.busy and solo.busy)
            self.assertFalse(first.cancel.is_set() or solo.cancel.is_set())
            release_a.set(); solo_release.set()
            self.wait(lambda: not first.busy and not solo.busy)
            self.assertEqual(first.chat["team"]["status"], "done")
            self.assertEqual([m["status"] for m in second.chat["team"]["members"]], ["stopped", "stopped"])
            self.assertNotIn("Update only Team A", json.dumps(second.chat))
            self.assertNotIn("Update only Team A", json.dumps(solo.chat))
            self.assertEqual(self.host.view_id, solo_id)
        finally:
            release_a.set(); solo_release.set(); self.host.stop_all()

    def test_parallel_team_approvals_stay_with_their_chat_after_selection_changes(self):
        from test_chat_agents import call
        a, b = self.create("approve-a"), self.create("approve-b")
        patches = ["*** Begin Patch\n*** Add File: allowed-" + str(index) + ".txt\n+saved\n*** End Patch" for index in (1, 2)]
        first = self.configure_parallel_team(a)
        self.pending_children.extend([[call("apply_patch", {"patch": patches[0]}), response("A first settled")],
                                      [call("apply_patch", {"patch": patches[1]}), response("A second settled")]])
        self.send(a, [])
        self.wait(lambda: first.approval is not None)
        second = self.configure_parallel_team(b)
        self.pending_children.extend([[call("apply_patch", {"patch": patches[0]}), response("B first settled")],
                                      [call("apply_patch", {"patch": patches[1]}), response("B second settled")]])
        self.send(b, [])
        self.wait(lambda: second.approval is not None)
        a_id, b_id = first.approval["id"], second.approval["id"]
        self.assertNotEqual(a_id, b_id)
        self.host.handle({"command": "select", "id": b})
        with self.assertRaisesRegex(ValueError, "no longer pending"):
            self.host.handle({"command": "approve", "chat": a, "id": b_id, "allow": True})
        self.host.handle({"command": "approve", "chat": a, "id": a_id, "allow": False})
        self.wait(lambda: first.approval is not None and first.approval["id"] != a_id)
        self.assertEqual(second.approval["id"], b_id)
        self.assertIn(first.approval["memberID"], first.chat["team"]["activeMemberIDs"])
        self.host.handle({"command": "approve", "chat": a, "id": first.approval["id"], "allow": False})
        self.host.handle({"command": "approve", "chat": b, "id": b_id, "allow": True})
        self.wait(lambda: second.approval is not None and second.approval["id"] != b_id)
        self.host.handle({"command": "approve", "chat": b, "id": second.approval["id"], "allow": False})
        self.wait(lambda: not first.busy and not second.busy)
        self.assertEqual(list((self.root / "approve-a").glob("allowed-*.txt")), [])
        saved = list((self.root / "approve-b").glob("allowed-*.txt"))
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0].read_text(), "saved\n")
        self.assertEqual(first.chat["team"]["status"], "done")
        self.assertEqual(second.chat["team"]["status"], "done")

    def test_host_shutdown_tracks_all_parallel_members_and_releases_request_slots(self):
        identifier = self.create("shutdown-team")
        together = threading.Barrier(3)
        def active(payload, cancel, delta):
            together.wait(3)
            self.assertTrue(cancel.wait(3))
            raise InterruptedError("Stopped")
        parent = self.configure_parallel_team(identifier)
        self.pending_children.extend([[active], [active]])
        self.send(identifier, [])
        together.wait(3)
        children = list(parent.team_children.values())
        self.assertEqual(len(children), 2)
        self.assertTrue(all(child in self.host.all_services() for child in children))
        self.host.shutdown(timeout=3)
        self.assertFalse(parent.busy or parent.thread.is_alive())
        self.assertTrue(all(child.closing and child.cancel.is_set() and not child.busy for child in children))
        self.assertEqual(self.host._requests.active, 0)
        self.assertEqual(parent.chat["team"]["activeMemberIDs"], [])

    def test_parallel_team_and_solo_writers_share_checkout_gate_across_subfolders(self):
        from chat_tools import ChatToolRunner
        from test_chat_agents import call
        a, b = self.create("shared-repo"), self.create("shared-repo/nested")
        subprocess.run(["git", "init", "-q", str(self.root / "shared-repo")], check=True)
        requested = threading.Barrier(4)
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        guard = threading.Lock()
        active = 0
        executed = []
        class Runner(ChatToolRunner):
            def execute(inner, name, args):
                nonlocal active
                with guard:
                    active += 1
                    self.assertEqual(active, 1, "Team and solo wrote the same checkout together")
                    executed.append(args["command"])
                entered.set()
                try:
                    self.assertTrue(release.wait(3))
                    return {"content": [{"type": "text", "text": "Saved"}], "is_error": False,
                            "summary": "Controlled write", "changed_files": []}
                finally:
                    with guard: active -= 1
        self.host.runner_type = Runner
        def write(command):
            def request(payload, cancel, delta):
                requested.wait(3)
                return call("run_shell", {"command": command})
            return request
        team = self.configure_parallel_team(a)
        self.host.handle({"command": "configure", "chat": a, "approvalMode": "yolo"})
        self.pending_children.extend([[write("Team first"), response("First done")],
                                      [write("Team second"), response("Second done")]])
        self.send(a, [])
        self.wait(lambda: len(team.team_children) == 2)
        self.host.handle({"command": "configure", "chat": b, "approvalMode": "yolo"})
        solo = self.send(b, [write("Solo"), response("Solo done")])
        try:
            requested.wait(3); self.assertTrue(entered.wait(2))
            with guard: self.assertEqual(len(executed), 1)
            release.set()
            self.wait(lambda: not team.busy and not solo.busy)
            self.assertEqual(set(executed), {"Team first", "Team second", "Solo"})
            self.assertEqual(active, 0)
            self.assertEqual(team.chat["team"]["status"], "done")
            self.assertEqual(solo.chat["status"], "ready")
        finally:
            release.set(); self.host.stop_all()

    def test_stream_budget_waits_without_opening_one_over_the_cap_socket_and_stop_releases_waiter(self):
        release = threading.Event(); entered = [threading.Event() for _ in range(MAX_ACTIVE_REQUESTS)]
        self.addCleanup(release.set)
        threads = []
        for index in range(MAX_ACTIVE_REQUESTS):
            transport = self.host.transport()
            def run(payload, cancel, delta, index=index):
                entered[index].set(); release.wait(3); return response()
            transport.client.responses = [run]
            thread = threading.Thread(target=transport.stream, args=({}, threading.Event(), lambda text: None))
            thread.start(); threads.append(thread)
        self.assertTrue(all(event.wait(2) for event in entered))
        overCap = self.host.transport()
        cancel = threading.Event(); stopped = threading.Event()
        def waiter():
            try: overCap.stream({}, cancel, lambda text: None)
            except InterruptedError: stopped.set()
        waiting = threading.Thread(target=waiter)
        waiting.start(); self.assertFalse(stopped.wait(.05))
        self.assertEqual(overCap.client.requests, [])
        cancel.set(); overCap.cancel(); self.assertTrue(stopped.wait(2)); waiting.join(2)
        release.set()
        for thread in threads: thread.join(2); self.assertFalse(thread.is_alive())

    def test_delete_damaged_unloaded_chat_does_not_decode_its_transcript(self):
        a, b = self.create("a"), self.create("b")
        with self.store.path(a).open("a") as stream: stream.write("invalid JSON\n")
        with patch.object(self.store, "load", side_effect=AssertionError("Delete loaded history")):
            self.host.handle({"command": "delete", "chat": a})
        self.assertFalse(self.store.path(a).exists())
        self.assertEqual(self.host.view_id, b)

    def test_auxiliary_rejections_settle_the_matching_request(self):
        identifier = self.create()
        self.host.session(identifier)
        self.host.reject({"command": "configure_team", "chat": identifier, "request": "team-request"}, ValueError("Busy"))
        self.assertEqual(self.events[-1]["event"], "team")
        self.assertEqual(self.events[-1]["request"], "team-request")
        self.host.reject({"command": "open_side", "chat": identifier, "request": "side-request"}, ValueError("Branch operation"))
        self.assertEqual(self.events[-1]["event"], "side")
        self.assertEqual(self.events[-1]["request"], "side-request")
        self.assertIsNone(self.events[-1]["side"])

    def test_startup_recovers_all_interrupted_chats_without_provider_replay(self):
        a, b = self.create("a"), self.create("b")
        for identifier in (a, b):
            chat = self.store.load(identifier); chat["status"] = "working"; self.store.save(chat)
        client = FakeTransport([]); client.root = self.root
        recovered = ChatHost(self.store, client, self.events.append, child_factory=lambda: client)
        recovered.initialize(); self.addCleanup(recovered.shutdown)
        self.assertEqual(self.store.load(a)["status"], "interrupted")
        self.assertEqual(self.store.load(b)["status"], "interrupted")
        self.assertEqual(client.requests, [])

    def test_shutdown_prevents_a_steered_request_from_restarting(self):
        identifier = self.create()
        started, canceled, cleanup = threading.Event(), threading.Event(), threading.Event()
        def run(payload, cancel, delta):
            started.set(); cancel.wait(3); canceled.set(); cleanup.wait(3)
            raise InterruptedError("Stopped")
        service = self.send(identifier, [run, response("Must not run")]); self.assertTrue(started.wait(2))
        self.host.handle({"command": "steer", "chat": identifier, "text": "New direction", "request": "update"})
        self.assertTrue(canceled.wait(2))
        shutdown = threading.Thread(target=self.host.shutdown)
        shutdown.start(); self.wait(lambda: self.host.closing)
        cleanup.set(); shutdown.join(3)
        self.assertFalse(shutdown.is_alive()); self.assertFalse(service.busy)
        self.assertEqual(len(service.transport.client.requests), 1)
        saved = self.store.load(identifier)
        self.assertNotIn("pending_update", saved)
        self.assertEqual(saved["entries"][-2]["clientRequest"], "update")


if __name__ == "__main__": unittest.main()
