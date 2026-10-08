import copy
import unittest

from chat_history import portable_history
import test_chat_runtime as fixtures


class ModelSwitchTests(unittest.TestCase):
    def test_switch_preserves_raw_signed_history_but_sends_no_reasoning_or_tool_calls(self):
        fixture = fixtures.ChatRuntimeTests(); fixture.setUp()
        try:
            reply = fixtures.response("Visible answer")
            reply["content"].insert(0, {"type": "thinking", "thinking": "private", "signature": "signed-original"})
            service, transport = fixture.service([reply, fixtures.response("New model answer")])
            fixture.send(service, "Original request"); fixture.finish(service)
            original = copy.deepcopy(service.chat["messages"])
            identifier = service.chat["id"]
            service.chat["entries"].append(fixtures.entry("tool", tool="run_shell", summary="Run tests", detail="Exit code: 0\nTests passed"))
            service.handle({"command": "configure", "choice": transport.rows[1]["id"]})
            self.assertEqual(service.chat["id"], identifier)
            self.assertEqual(service.chat["archives"][0]["messages"], original)
            content = service.chat["messages"]
            self.assertTrue(all(block["type"] == "text" for message in content for block in message["content"]))
            text = str(content)
            self.assertIn("Original request", text); self.assertIn("Visible answer", text); self.assertIn("Tests passed", text)
            self.assertNotIn("signed-original", text); self.assertNotIn("private", text)
            fixture.send(service, "Continue"); fixture.finish(service)
            self.assertEqual(transport.requests[-1]["model"], transport.rows[1]["route"])
            self.assertEqual(transport.requests[-1]["_provider_hub_connection"], "scope-work")
            self.assertNotIn("signed-original", str(transport.requests[-1]))
            saved = fixture.store.load(identifier)
            self.assertEqual(saved["archives"][0]["messages"], original)
            # Switching back also starts a portable context; no stale trace is resurrected.
            service.handle({"command": "configure", "choice": transport.rows[0]["id"]})
            self.assertEqual(len(service.chat["archives"]), 2)
            self.assertNotIn("signed-original", str(service.chat["messages"]))
        finally: fixture.doCleanups()

    def test_tool_records_are_text_and_missing_results_stay_uncertain(self):
        messages = portable_history([fixtures.entry("tool", tool="apply_patch", summary="Update x.py", detail="Interrupted. Inspect before retrying.")])
        self.assertIn("Interrupted", str(messages))
        self.assertIn("do not execute again", str(messages))
        self.assertNotIn("tool_use_id", str(messages))

    def test_switch_does_not_require_pixels_for_text_only_models_and_preserves_file_text(self):
        entries = [fixtures.entry("user", "Discuss these", attachments=[
            {"kind": "image", "name": "plot.png", "path": "/not-read-for-text-only-model"},
            {"kind": "file", "name": "notes.pdf", "contextText": "Original extracted PDF text"}])]
        messages = portable_history(entries, vision=False)
        self.assertIn("Pixels excluded", str(messages))
        self.assertIn("Original extracted PDF text", str(messages))


if __name__ == "__main__": unittest.main()
