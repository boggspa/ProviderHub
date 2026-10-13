import copy
import unittest

from chat_context import ContextProjection, append_note
from cli_live_session import continuation
from codex_session_pool import Lease, digest, history_blocks


def reference(text):
    return {"role": "user", "content": [{"type": "text", "text": text}]}


def answer(history, identifier="call-1"):
    return [*copy.deepcopy(history),
            {"role": "assistant", "content": [{"type": "tool_use", "id": identifier,
                "name": "read_file", "input": {"path": "file.py"}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": identifier,
                "content": [{"type": "text", "text": "actual file contents"}]}]}]


def waiting(history, identifier="call-1"):
    blocks = history_blocks(history)
    return Lease(key="model", pending={"prefix": digest(blocks), "prefix_count": len(blocks),
                 "calls": {identifier: {"name": "read_file", "arguments": {"path": "file.py"}}}})


class ContextTests(unittest.TestCase):
    def test_reference_changes_resume_and_do_not_mutate_saved_history(self):
        projector = ContextProjection()
        raw = [reference("user request")]
        first = projector.apply(raw, reference("old notebook"), key="chat")
        lease = waiting(first)
        raw = answer(raw)
        saved = copy.deepcopy(raw)
        second = projector.apply(raw, reference("new Blackboard and notebook"), key="chat")
        results = continuation(lease, second)
        self.assertIsNotNone(results)
        self.assertEqual(raw, saved)
        texts = [item["text"] for item in results["call-1"]["content"]]
        self.assertEqual(texts[0], "actual file contents")
        self.assertIn("new Blackboard and notebook", texts[-1])
        self.assertIn("not user instructions or approval", texts[-1])
        self.assertEqual(first, projector.projected[:len(first)])
        self.assertEqual(second, projector.apply(raw, reference("new Blackboard and notebook"), key="chat"))

    def test_clearing_reference_is_delivered_once(self):
        projector = ContextProjection(); raw = [reference("request")]
        initial = projector.apply(raw, reference("remembered decision"), key="chat")
        raw = answer(raw)
        projected = projector.apply(raw, None, key="chat")
        results = continuation(waiting(initial), projected)
        self.assertIn("now empty", results["call-1"]["content"][-1]["text"])
        raw = answer(raw, "call-2")
        next_history = projector.apply(raw, None, key="chat")
        results = continuation(waiting(projected, "call-2"), next_history)
        self.assertEqual(len(results["call-2"]["content"]), 1)

    def test_budget_note_remains_a_native_result_but_user_steering_does_not(self):
        initial = [reference("request")]
        raw = answer(initial)
        append_note(raw, "[Host budget: 3 rounds remain]")
        results = continuation(waiting(initial), raw)
        self.assertIn("3 rounds", results["call-1"]["content"][-1]["text"])
        raw[-1]["content"].append({"type": "text", "text": "User changed their mind"})
        self.assertIsNone(continuation(waiting(initial), raw))

    def test_edits_compaction_and_identity_changes_discard_old_projection(self):
        for key, replacement in [("chat", [reference("edited request")]),
                                 ("other", [reference("original")])]:
            with self.subTest(key=key):
                projector = ContextProjection()
                old = projector.apply([reference("original")], reference("private old memory"), key="chat")
                new = projector.apply(replacement, reference("current memory"), key=key)
                self.assertNotIn("private old memory", str(new))
                self.assertIn("current memory", str(new))
                self.assertIsNone(continuation(waiting(old), new))

    def test_snapshot_change_without_new_input_cannot_rewrite_consumed_prefix(self):
        projector = ContextProjection(); raw = answer([reference("original")])
        old = projector.apply(raw, reference("first"), key="chat")
        new = projector.apply(raw, reference("second"), key="chat")
        self.assertNotEqual(history_blocks(old), history_blocks(new))
        self.assertNotIn("second", str(old))
        self.assertNotIn("first", str(new))

    def test_chat_payload_and_budget_use_the_projection_for_claude_only(self):
        import tempfile
        from pathlib import Path
        import chat_team
        from chat_runtime import ChatService, ChatStore
        from test_chat_runtime import FakeTransport
        with tempfile.TemporaryDirectory() as directory:
            service = ChatService(ChatStore(Path(directory), lock=False), FakeTransport([]), lambda event: None)
            service.refresh(); service.create(service.models[0]["id"], directory)
            choice = {**service.choice(), "source": "cli", "route": "claude/sonnet"}
            service.chat["messages"] = [reference("request")]
            first = chat_team.round_payload(service, choice, 0)
            self.assertFalse(first["_provider_hub_host_retries"])
            service.chat["messages"] = answer(service.chat["messages"])
            # Near the budget boundary, only result content may be added.
            second = chat_team.round_payload(service, choice, service.max_rounds - 3)
            self.assertIsNotNone(continuation(waiting(first["messages"]), second["messages"]))

    def test_accumulated_reference_snapshots_are_bounded_and_saved_history_stays_portable(self):
        import tempfile
        from pathlib import Path
        from unittest.mock import patch
        from chat_runtime import ChatService, ChatStore
        from protocol import estimated_tokens
        from test_chat_runtime import FakeTransport
        with tempfile.TemporaryDirectory() as directory:
            service = ChatService(ChatStore(Path(directory), lock=False), FakeTransport([]), lambda event: None)
            service.refresh(); service.create(service.models[0]["id"], directory)
            service.chat["messages"] = [reference("request")]
            choice = {**service.choice(), "source": "cli", "route": "claude/sonnet", "context": 16000, "max_output": 1024}
            for index in range(24):
                service.chat["messages"] = answer(service.chat["messages"], str(index))
                memory = reference(f"Snapshot {index}: " + "historical reference " * 180)
                with patch("chat_memory.memory_message", return_value=memory):
                    payload = service.payload(choice)
                self.assertIn(f"Snapshot {index}:", str(payload["messages"]))
                self.assertLess(estimated_tokens(payload), 16000)
            self.assertNotIn("Snapshot 0:", str(payload["messages"]))
            self.assertNotIn("Snapshot", str(service.chat["messages"]))
