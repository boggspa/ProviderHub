import threading
import time
import unittest
from unittest.mock import patch

from chat_runtime import ChatService
import test_chat_runtime as fixtures


class ChatSteeringTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ChatRuntimeTests(); self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.started = threading.Event(); self.cleanup = threading.Event()
        self.addCleanup(self.cleanup.set)

    def waiting(self, payload, cancel, delta):
        delta("Partial answer")
        self.started.set()
        if not cancel.wait(3): raise RuntimeError("cancellation never arrived")
        self.cleanup.wait(3)
        raise InterruptedError("interrupted")

    def wait_done(self, service):
        deadline = time.monotonic() + 4
        while service.busy and time.monotonic() < deadline: time.sleep(.01)
        self.assertFalse(service.busy)

    def steer(self, service, text="New direction"):
        service.handle({"command": "steer", "id": service.chat["id"], "text": text})

    def test_interrupt_cancels_now_but_waits_for_cleanup_before_restarting(self):
        service, transport = self.fixture.service([self.waiting, fixtures.response("Updated answer")])
        self.fixture.send(service); self.assertTrue(self.started.wait(2))
        self.steer(service)
        self.assertTrue(service.cancel.is_set())
        self.assertEqual(len(transport.requests), 1)
        saved = self.fixture.store.load(service.chat["id"])
        self.assertEqual(saved["pending_update"]["content"][0]["text"], "New direction")
        self.assertTrue(service.busy)
        self.cleanup.set(); self.wait_done(service)
        self.assertEqual(len(transport.requests), 2)
        last = transport.requests[-1]["messages"][-1]
        self.assertEqual(last["role"], "user")
        self.assertEqual(last["content"][0], {"type": "text", "text": "New direction"})
        self.assertTrue(any(item["text"] == "Partial answer" for item in service.chat["entries"]))
        self.assertNotIn("pending_update", self.fixture.store.load(service.chat["id"]))

    def test_stop_during_interruption_cancels_restart_without_losing_the_update(self):
        service, transport = self.fixture.service([self.waiting])
        self.fixture.send(service); self.assertTrue(self.started.wait(2))
        self.steer(service); service.handle({"command": "stop"})
        self.cleanup.set(); self.wait_done(service)
        self.assertEqual(len(transport.requests), 1)
        self.assertEqual(service.chat["status"], "stopped")
        self.assertEqual(service.chat["messages"][-1]["content"][0]["text"], "New direction")

    def test_second_update_is_rejected_not_queued(self):
        service, transport = self.fixture.service([self.waiting, fixtures.response()])
        self.fixture.send(service); self.assertTrue(self.started.wait(2))
        self.steer(service, "First update")
        with self.assertRaises(ValueError): self.steer(service, "Never queued")
        self.cleanup.set(); self.wait_done(service)
        self.assertNotIn("Never queued", str(service.chat))
        self.assertEqual(len(transport.requests), 2)

    def test_interrupt_pending_approval_never_executes_the_old_action(self):
        change = "*** Begin Patch\n*** Add File: never.txt\n+never\n*** End Patch"
        service, transport = self.fixture.service([fixtures.response("", change), fixtures.response("Updated")])
        self.fixture.send(service); self.fixture.approval(service)
        self.steer(service); self.wait_done(service)
        self.assertFalse((self.fixture.root / "never.txt").exists())
        results = [block for msg in transport.requests[-1]["messages"] for block in msg["content"] if block["type"] == "tool_result"]
        self.assertEqual(len(results), 1); self.assertTrue(results[0]["is_error"])
        self.assertEqual(transport.requests[-1]["messages"][-1]["content"][0]["text"], "New direction")

    def test_invalid_update_does_not_interrupt_active_work(self):
        service, _ = self.fixture.service([self.waiting])
        self.fixture.send(service); self.assertTrue(self.started.wait(2))
        with self.assertRaises(ValueError): self.steer(service, "")
        self.assertFalse(service.cancel.is_set())
        service.handle({"command": "stop"}); self.cleanup.set(); self.wait_done(service)

    def test_crash_recovers_the_update_as_user_input_without_running_it(self):
        service, transport = self.fixture.service([])
        pending = service.prepare_update({"text": "Saved direction"})
        service.chat["entries"].append(pending["entry"])
        service.chat["pending_update"] = pending; service.chat["status"] = "stopped"; service.save()
        recovered = ChatService(self.fixture.store, transport, self.fixture.events.append)
        recovered.initialize()
        self.assertEqual(recovered.chat["messages"][-1]["content"][0]["text"], "Saved direction")
        self.assertEqual(recovered.chat["status"], "interrupted")
        self.assertFalse(recovered.busy); self.assertEqual(transport.requests, [])

    def test_git_inspection_does_not_block_stop_or_touch_model_history(self):
        service, _ = self.fixture.service([self.waiting])
        self.fixture.send(service); self.assertTrue(self.started.wait(2))
        git_entered = threading.Event(); git_release = threading.Event()
        self.addCleanup(git_release.set)
        def blocked_git(workspace): git_entered.set(); git_release.wait(3); return None
        with patch("chat_runtime.git_status", side_effect=blocked_git):
            service.handle({"command": "git_status", "id": service.chat["id"]})
            self.assertTrue(git_entered.wait(2))
            service.handle({"command": "stop"})
            self.assertTrue(service.cancel.is_set())
            self.cleanup.set(); self.wait_done(service); git_release.set()

    def test_idle_update_starts_immediately_as_an_ordinary_turn(self):
        service, transport = self.fixture.service([fixtures.response()])
        self.steer(service, "Already idle")
        self.wait_done(service)
        self.assertEqual(transport.requests[0]["messages"][0]["content"][0]["text"], "Already idle")

    def test_running_shell_is_reaped_and_its_actual_result_precedes_the_update(self):
        call = {"content": [{"type": "tool_use", "id": "shell", "name": "run_shell", "input": {
            "command": "printf started > started.txt; sleep 10; printf wrong > never.txt", "timeout": 15}}], "stop_reason": "tool_use"}
        service, transport = self.fixture.service([call, fixtures.response("Updated")])
        service.handle({"command": "configure", "approvalMode": "yolo"})
        self.fixture.send(service)
        deadline = time.monotonic() + 3
        while not (self.fixture.root / "started.txt").exists() and time.monotonic() < deadline: time.sleep(.01)
        self.assertTrue((self.fixture.root / "started.txt").exists())
        self.steer(service); self.wait_done(service)
        self.assertFalse((self.fixture.root / "never.txt").exists())
        messages = transport.requests[-1]["messages"]
        results = [block for message in messages for block in message["content"] if block["type"] == "tool_result"]
        self.assertEqual(len(results), 1)
        self.assertIn("cancelled", results[0]["content"][0]["text"])
        self.assertNotIn("New direction", str(results))
        self.assertEqual(messages[-1]["content"][0]["text"], "New direction")


if __name__ == "__main__": unittest.main()
