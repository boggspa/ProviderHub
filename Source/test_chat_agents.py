"""Delegation and temporary discussions exercise the real host tool loop."""
import copy
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from chat_runtime import ChatService, ChatStore
from chat_agents import close_side, close_all_sides, validate_delegate
from test_chat_runtime import FakeTransport, model, response


def call(name, arguments, identifier="call-helper"):
    return {"role": "assistant", "content": [{"type": "tool_use", "id": identifier,
            "name": name, "input": arguments}], "stop_reason": "tool_use", "usage": {}}


class AgentTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.events = []
        self.store = ChatStore(self.root, lock=False)

    def service(self, replies, children):
        self.transport = FakeTransport(replies)
        self.children = [FakeTransport(items) for items in children]
        available = iter(self.children)
        parent = ChatService(self.store, self.transport, self.events.append, child_factory=lambda: next(available))
        parent.refresh(); parent.create(parent.models[0]["id"], str(self.root))
        self.addCleanup(lambda: close_all_sides(parent))
        return parent

    def send(self, parent):
        parent.handle({"command": "send", "id": parent.chat["id"], "text": "Parent task"})

    def finish(self, parent):
        parent.thread.join(3)
        self.assertFalse(parent.busy)

    def wait_for(self, condition):
        deadline = time.monotonic() + 3
        while not condition() and time.monotonic() < deadline: time.sleep(.005)
        self.assertTrue(condition())

    def test_delegate_is_fresh_inherits_policy_and_keeps_opaque_history_private(self):
        opaque = response("Child result")
        opaque["content"].insert(0, {"type": "thinking", "thinking": "secret-child", "signature": "signed-child"})
        parent = self.service([call("delegate", {"task": "Inspect only"}), response()], [[opaque]])
        parent.chat["effort"] = "high"
        self.send(parent); self.finish(parent)
        child = parent.chat["agents"][0]
        request = self.children[0].requests[0]
        self.assertEqual(request["messages"][0]["content"][0]["text"], "Inspect only")
        self.assertEqual(request["output_config"], {"effort": "high"})
        self.assertNotIn("Parent task", json.dumps(request["messages"]))
        self.assertEqual({t["name"] for t in request["tools"]}, {"read_file", "search_files", "apply_patch", "run_shell"})
        self.assertEqual(child["workspace"], parent.chat["workspace"])
        self.assertEqual(child["approvalMode"], "manual")
        stored = self.store.load(parent.chat["id"])
        self.assertIn("signed-child", json.dumps(stored["agents"][0]["messages"]))
        self.assertNotIn("secret-child", json.dumps(self.events))
        self.assertNotIn("signed-child", json.dumps(self.store.headers()))
        self.assertNotIn("signed-child", json.dumps(self.transport.requests))
        self.assertTrue(any(e.get("entry", {}).get("agentID") == child["id"] for e in self.events))

    def test_recursion_rejected_even_if_model_invents_tool(self):
        parent = self.service([call("delegate", {"task": "Task"}), response()],
                              [[call("delegate", {"task": "Grandchild"}), response("Cannot recurse")]])
        self.send(parent); self.finish(parent)
        result = self.children[0].requests[1]["messages"][-1]["content"][0]
        self.assertTrue(result["is_error"])
        self.assertIn("cannot delegate", result["content"][0]["text"])
        self.assertEqual(len(parent.chat["agents"]), 1)

    def test_override_validation_and_limits(self):
        parent = self.service([], [])
        choice, effort = validate_delegate(parent, {"task": "Task", "choice": "ollama/other|work", "effort": "low"})
        self.assertEqual((choice["account"], effort), ("work", "low"))
        for args in ({"task": "Task", "choice": "unknown"}, {"task": "Task", "effort": "ultra"},
                     {"task": "Task", "approvalMode": "yolo"}, {"task": ""}):
            with self.assertRaises(ValueError): validate_delegate(parent, args)
        parent.models[1]["supportsTools"] = False
        with self.assertRaises(ValueError): validate_delegate(parent, {"task": "Task", "choice": "ollama/other|work"})
        parent._delegations = 4
        with self.assertRaises(ValueError): validate_delegate(parent, {"task": "Task"})

    def test_child_approval_uses_parent_ui_and_stop_settles_both(self):
        patch_text = "*** Begin Patch\n*** Add File: never.txt\n+no\n*** End Patch"
        parent = self.service([call("delegate", {"task": "Task"})], [[response("", patch_text)]])
        self.send(parent)
        self.wait_for(lambda: parent.approval is not None)
        self.assertIn("Helper", parent.approval["summary"])
        approval_id = parent.approval["id"]
        parent.handle({"command": "stop"}); self.finish(parent)
        self.assertFalse((self.root / "never.txt").exists())
        self.assertEqual(parent.chat["agents"][0]["status"], "stopped")
        with self.assertRaises(ValueError): parent.handle({"command": "approve", "id": approval_id, "allow": True})
        self.assertTrue(parent.chat["messages"][-1]["content"][0]["is_error"])

    def test_denied_child_action_and_parent_retry_do_not_repeat_helper(self):
        patch_text = "*** Begin Patch\n*** Add File: never.txt\n+no\n*** End Patch"
        parent = self.service([call("delegate", {"task": "Task"}), ValueError("lost"), response("Recovered")],
                              [[response("", patch_text), response("Denied")]])
        self.send(parent); self.wait_for(lambda: parent.approval is not None)
        parent.handle({"command": "approve", "id": parent.approval["id"], "allow": False})
        self.finish(parent)
        parent.handle({"command": "retry", "id": parent.chat["id"]}); self.finish(parent)
        self.assertEqual(len(parent.chat["agents"]), 1)
        self.assertEqual(len(self.children[0].requests), 2)
        self.assertFalse((self.root / "never.txt").exists())

    def test_steer_waits_for_child_cleanup(self):
        started, cleaned = threading.Event(), threading.Event()
        def streaming(payload, cancel, delta):
            started.set(); cancel.wait(2); cleaned.set(); raise InterruptedError("Stopped")
        def resumed(payload, cancel, delta):
            self.assertTrue(cleaned.is_set()); return response("Updated")
        parent = self.service([call("delegate", {"task": "Task"}), resumed], [[streaming]])
        self.send(parent); self.assertTrue(started.wait(2))
        parent.handle({"command": "steer", "id": parent.chat["id"], "text": "New direction"})
        self.wait_for(lambda: not parent.busy)
        self.assertEqual(parent.chat["agents"][0]["status"], "stopped")
        self.assertEqual(parent.chat["entries"][-1]["text"], "Updated")

    def test_crash_recovery_marks_child_and_settles_unanswered_call(self):
        parent = self.service([], [])
        agent = copy.deepcopy(parent.chat)
        agent.update(id="a" * 32, status="working", messages=[call("apply_patch", {"patch": "uncertain"})])
        parent.chat.update(status="working", agents=[agent])
        parent.save()
        restarted = ChatService(self.store, FakeTransport([]), self.events.append)
        restarted.initialize()
        recovered = self.store.load(parent.chat["id"])["agents"][0]
        self.assertEqual(recovered["status"], "interrupted")
        self.assertTrue(recovered["messages"][-1]["content"][0]["is_error"])

    def open_side(self, parent):
        parent.handle({"command": "open_side", "id": parent.chat["id"], "choice": parent.models[1]["id"]})
        self.assertIsNotNone(parent.side)
        return parent.side

    def test_side_portable_context_read_only_and_memory_only(self):
        opaque = response("Side answer")
        opaque["content"].insert(0, {"type": "thinking", "thinking": "side-secret", "signature": "side-signed"})
        original = response("Parent answer")
        original["content"].insert(0, {"type": "thinking", "thinking": "parent-secret", "signature": "parent-signed"})
        parent = self.service([original], [[call("run_shell", {"command": "touch forbidden"}), opaque]])
        self.send(parent); self.finish(parent)
        before = self.store.path(parent.chat["id"]).read_bytes()
        side = self.open_side(parent)
        parent.handle({"command": "side_send", "id": parent.chat["id"], "side": side.chat["id"], "text": "Discuss"})
        self.finish(side)
        self.assertEqual({t["name"] for t in self.children[0].requests[0]["tools"]}, {"read_file", "search_files"})
        self.assertNotIn("parent-signed", json.dumps(self.children[0].requests))
        self.assertTrue(self.children[0].requests[1]["messages"][-1]["content"][0]["is_error"])
        self.assertFalse((self.root / "forbidden").exists())
        self.assertEqual(before, self.store.path(parent.chat["id"]).read_bytes())
        self.assertFalse(self.store.path(side.chat["id"]).exists())
        self.assertNotIn("side-secret", json.dumps(self.events))
        parent.handle({"command": "side_model", "id": parent.chat["id"], "side": side.chat["id"], "choice": parent.models[0]["id"], "effort": "low"})
        self.assertNotIn("side-signed", json.dumps(side.chat["messages"]))
        self.assertIn("side-signed", json.dumps(side.chat["archives"]))
        close_side(parent)
        self.assertIsNone(parent.side)
        self.assertEqual(self.events[-1]["sideID"], side.chat["id"])

    def test_side_wrong_identity_scoped_and_close_cancels_stream(self):
        started, ended = threading.Event(), threading.Event()
        def streaming(payload, cancel, delta):
            started.set(); cancel.wait(2); ended.set(); raise InterruptedError("Stopped")
        parent = self.service([], [[streaming]])
        side = self.open_side(parent)
        parent.handle({"command": "side_send", "id": parent.chat["id"], "side": "stale", "text": "No"})
        self.assertEqual(self.events[-1]["event"], "side")
        self.assertFalse(side.busy)
        parent.handle({"command": "side_send", "id": parent.chat["id"], "side": side.chat["id"], "text": "Go"})
        self.assertTrue(started.wait(2)); close_side(parent)
        self.assertTrue(ended.wait(2)); self.finish(side)

    def test_side_survives_selection_and_parent_model_changes_until_explicit_close(self):
        parent = self.service([response("Original context")], [[], []])
        self.send(parent); self.finish(parent)
        first_id = parent.chat["id"]
        first = self.open_side(parent)
        original_route = first.chat["route"]
        parent.handle({"command": "configure", "choice": parent.models[1]["id"]})
        self.assertIs(parent.side, first)
        self.assertEqual(first.chat["route"], original_route)
        parent.handle({"command": "create", "choice": parent.models[0]["id"], "workspace": str(self.root)})
        second_id = parent.chat["id"]
        self.assertIsNone(parent.side)
        second = self.open_side(parent)
        parent.handle({"command": "select", "id": first_id})
        self.assertIs(parent.side, first)
        snapshots = [e for e in self.events if e.get("event") == "side" and e.get("side")]
        self.assertEqual(snapshots[-1]["side"]["id"], first.chat["id"])
        parent.handle({"command": "delete", "id": second_id})
        self.assertIs(parent.side, first)
        self.assertNotIn(second_id, parent.sides)
        self.assertTrue(second.cancel.is_set())
        parent.handle({"command": "close_side"})
        self.assertEqual(parent.sides, {})
        self.assertTrue(first.cancel.is_set())

    def test_hidden_side_keeps_own_events_and_all_sides_close_on_shutdown(self):
        started = threading.Event()
        def streaming(payload, cancel, delta):
            started.set(); cancel.wait(2); raise InterruptedError("Stopped")
        parent = self.service([], [[streaming], []])
        owner = parent.chat["id"]
        first = self.open_side(parent)
        parent.handle({"command": "side_send", "id": owner, "side": first.chat["id"], "text": "Keep reading"})
        self.assertTrue(started.wait(2))
        parent.create(parent.models[0]["id"], str(self.root))
        second = self.open_side(parent)
        parent.handle({"command": "branch_action", "id": parent.chat["id"], "action": "create", "branch": "test"})
        self.assertIn("Stop", self.events[-1]["notice"])
        self.assertTrue(first.busy)
        close_all_sides(parent); self.finish(first)
        self.assertIsNone(parent.side)
        self.assertTrue(second.cancel.is_set())

    def test_branch_busy_blocks_parent_and_side_and_scopes_errors(self):
        parent = self.service([], [[]]); side = self.open_side(parent)
        entered, release = threading.Event(), threading.Event()
        def switch(workspace, branch): entered.set(); release.wait(2); return {"current": branch}
        with patch("chat_inspector.switch_branch", switch):
            parent.handle({"command": "branch_action", "id": parent.chat["id"], "action": "switch", "branch": "topic"})
            self.assertTrue(entered.wait(2))
            with self.assertRaises(ValueError): self.send(parent)
            parent.handle({"command": "side_send", "id": parent.chat["id"], "side": side.chat["id"], "text": "Blocked"})
            self.assertFalse(side.busy)
            release.set(); self.wait_for(lambda: not parent._branch_working)
        self.assertTrue(any(e.get("branches", {}).get("current") == "topic" for e in self.events))

    def test_side_direct_steer_keeps_busy_and_parent_can_finish_independently(self):
        started, ended = threading.Event(), threading.Event()
        def streaming(payload, cancel, delta):
            started.set(); cancel.wait(2); ended.set(); raise InterruptedError("Stopped")
        parent = self.service([response("Parent finished")], [[streaming, response("Side updated")]])
        side = self.open_side(parent)
        command = {"command": "side_send", "id": parent.chat["id"], "side": side.chat["id"], "text": "Discuss"}
        parent.handle(command); self.assertTrue(started.wait(2))
        self.send(parent); self.finish(parent)
        self.assertTrue(side.busy)
        start = len(self.events)
        parent.handle({**command, "text": "Change direction"})
        self.wait_for(lambda: not side.busy)
        self.assertTrue(ended.is_set())
        snapshots = [event["side"] for event in self.events[start:] if event.get("event") == "side" and event.get("side")]
        self.assertTrue(any(row["interrupting"] and row["busy"] for row in snapshots))
        self.assertEqual(side.chat["entries"][-1]["text"], "Side updated")
        self.assertEqual(parent.chat["entries"][-1]["text"], "Parent finished")

    def test_child_round_limit_is_twelve(self):
        parent = self.service([call("delegate", {"task": "Task"}), response()],
                              [[call("read_file", {"path": "missing"}, str(i)) for i in range(12)]])
        self.send(parent); self.finish(parent)
        self.assertEqual(len(self.children[0].requests), 12)
        self.assertEqual(parent.chat["agents"][0]["status"], "error")
        self.assertIn("12-step", parent.chat["agents"][0]["entries"][-1]["text"])

    def test_worktree_creation_does_not_move_chat_selection_archives_context(self):
        parent = self.service([response("Keep this transcript")], [])
        self.send(parent); self.finish(parent)
        old_workspace = parent.chat["workspace"]
        identifier = parent.chat["id"]
        destination = self.root / "other"
        destination.mkdir()
        with patch("chat_inspector.create_worktree", return_value={"current": "main"}) as create, patch("chat_inspector.git_branches", return_value={"current": "topic"}), patch("chat_inspector.switch_worktree", return_value=str(destination)):
            parent.handle({"command": "branch_action", "id": identifier, "action": "worktree", "branch": "topic", "path": str(destination)})
            self.wait_for(lambda: not parent._branch_working)
            create.assert_called_once()
            self.assertEqual(parent.chat["workspace"], old_workspace)
            parent.handle({"command": "branch_action", "id": identifier, "action": "select_worktree", "path": str(destination)})
            self.wait_for(lambda: not parent._branch_working)
        self.assertEqual(parent.chat["id"], identifier)
        self.assertEqual(parent.chat["workspace"], str(destination))
        self.assertEqual(parent.chat["archives"][-1]["workspace"], old_workspace)
        self.assertEqual(parent.chat["entries"][-2]["text"], "Keep this transcript")
        self.assertIn(str(destination), parent.chat["entries"][-1]["text"])
        self.assertIn("different worktree", parent.chat["messages"][0]["content"][0]["text"])

    def test_side_request_generation_survives_replacement_and_stale_events(self):
        parent = self.service([], [[response("Old")], [response("New")]])
        base = {"command": "open_side", "id": parent.chat["id"], "choice": parent.models[0]["id"]}
        parent.handle({**base, "request": "request-a"})
        old = parent.side
        parent.handle({**base, "request": "request-b"})
        current = parent.side
        old.emit({"event": "delta", "id": "old-entry", "text": "Late"})
        self.assertFalse(any(e.get("text") == "Late" for e in self.events))
        closed = [e for e in self.events if e.get("event") == "side" and e.get("sideID") == old.chat["id"]]
        self.assertEqual(closed[-1]["request"], "request-a")
        parent.handle({"command": "side_send", "id": parent.chat["id"], "side": current.chat["id"], "text": "New turn"})
        self.finish(current)
        self.assertTrue(all(e["request"] == "request-b" for e in self.events if e.get("event") == "side_delta"))
        parent.handle({"command": "side_stop", "id": parent.chat["id"], "side": old.chat["id"], "request": "request-a"})
        self.assertEqual(self.events[-1]["request"], "request-a")
        self.assertIs(parent.side, current)

    def test_override_executes_on_selected_account_and_approval_is_inherited(self):
        patch_text = "*** Begin Patch\n*** Add File: allowed.txt\n+yes\n*** End Patch"
        parent = self.service([call("delegate", {"task": "Task", "choice": "ollama/other|work", "effort": "low"}), response()],
                              [[response("", patch_text), response("Done")]])
        self.send(parent); self.wait_for(lambda: parent.approval is not None)
        self.assertIn("ollama/other", parent.approval["summary"])
        parent.handle({"command": "approve", "id": parent.approval["id"], "allow": True})
        self.finish(parent)
        request = self.children[0].requests[0]
        self.assertEqual(request["model"], "ollama/other")
        self.assertEqual(request["_provider_hub_account"], "work")
        self.assertEqual(request["output_config"], {"effort": "low"})
        self.assertEqual((self.root / "allowed.txt").read_text(), "yes\n")

    def test_side_survives_effort_and_permission_configuration(self):
        parent = self.service([response("Ready")], [[]])
        self.send(parent); self.finish(parent)
        side = self.open_side(parent)
        parent.handle({"command": "configure", "effort": "low", "approvalMode": "accept_edits"})
        self.assertIs(parent.side, side)

    def test_empty_chat_recovers_by_choosing_folder_when_retained_folder_is_offline(self):
        parent = self.service([], [])
        missing = self.root / "offline"
        missing.mkdir()
        parent.create(parent.models[0]["id"], str(missing))
        for chat in self.store.all(): self.store.delete(chat["id"])
        missing.rmdir()
        parent = ChatService(self.store, FakeTransport([]), self.events.append)
        parent.initialize()
        self.assertIsNone(parent.chat)
        parent.handle({"command": "configure", "workspace": str(self.root)})
        self.assertEqual(parent.chat["workspace"], str(self.root.resolve()))
        self.assertIn(str(missing.resolve()), parent.workspaces.folders)

    def test_side_rejects_disabled_or_changed_accounts_before_new_request(self):
        parent = self.service([], [[]])
        side = self.open_side(parent)
        command = {"command": "side_send", "id": parent.chat["id"], "side": side.chat["id"], "text": "Continue"}
        parent.models[1]["scope"] = "replacement-connection"
        parent.handle(command)
        self.assertIn("connection changed", self.events[-1]["notice"])
        self.assertFalse(side.busy)
        parent.models = parent.models[:1]
        parent.handle(command)
        self.assertIn("no longer", self.events[-1]["notice"])
        self.assertEqual(self.children[0].requests, [])

    def test_side_accepts_large_input_even_when_visible_snapshot_is_bounded(self):
        parent = self.service([], [[response("Received")]])
        side = self.open_side(parent)
        text = "Long question " * 5000
        parent.handle({"command": "side_send", "id": parent.chat["id"], "side": side.chat["id"], "text": text})
        self.finish(side)
        accepted = [e for e in self.events if e["event"] == "side_accepted"]
        self.assertEqual(len(accepted), 1)
        self.assertEqual(accepted[0]["request"], side.side_request)
        self.assertEqual(side.chat["entries"][0]["text"], text)

    def test_switch_branch_records_workspace_change_for_parent_context(self):
        parent = self.service([response("Before switch")], [])
        self.send(parent); self.finish(parent)
        with patch("chat_inspector.switch_branch", return_value={"current": "topic"}):
            parent.handle({"command": "branch_action", "id": parent.chat["id"], "action": "switch", "branch": "topic"})
            self.wait_for(lambda: not parent._branch_working)
        self.assertIn("Switched branch to topic", parent.chat["entries"][-1]["text"])
        self.assertIn("previous branch", parent.chat["messages"][-1]["content"][0]["text"])

    def test_branch_error_has_one_final_state_and_retains_notice(self):
        parent = self.service([], [])
        with patch("chat_inspector.switch_branch", side_effect=ValueError("Dirty checkout")):
            parent.handle({"command": "branch_action", "id": parent.chat["id"], "action": "switch", "branch": "topic"})
            self.wait_for(lambda: not parent._branch_working)
        states = [e for e in self.events if e.get("event") == "branch_state"]
        self.assertEqual(len(states), 2)
        self.assertEqual(states[-1]["notice"], "Dirty checkout")
        self.assertFalse(states[-1]["busy"])

    def test_inspections_and_branch_changes_echo_generation_and_exact_ref(self):
        parent = self.service([], [])
        identifier = parent.chat["id"]
        with patch("chat_inspector.git_changes", return_value={"files":[], "truncated":False}):
            parent.handle({"command":"inspect_git", "id":identifier, "request":"changes-1"})
            self.wait_for(lambda: any(e.get("request") == "changes-1" for e in self.events))
        with patch("chat_inspector.git_branches", return_value={"current":"main"}):
            parent.handle({"command":"branches", "id":identifier, "request":"branches-1"})
            self.wait_for(lambda: any(e.get("request") == "branches-1" for e in self.events))
        raw = b"topic-\xff"
        with patch("chat_inspector.switch_branch", return_value={"current":"topic-\\xff"}) as switch:
            parent.handle({"command":"branch_action", "id":identifier, "request":"switch-1", "action":"switch", "branch":"topic-\\xff", "branchBytes":raw.hex()})
            self.wait_for(lambda: not parent._branch_working)
            self.assertEqual(os.fsencode(switch.call_args.args[1]), raw)
        states = [e for e in self.events if e.get("event") == "branch_state"]
        self.assertTrue(all(e["request"] == "switch-1" for e in states))


if __name__ == "__main__": unittest.main()
