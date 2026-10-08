import copy
import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch

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

    def test_missing_invalid_and_oversized_images_do_not_prevent_switching(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "picture.png"
            entries = [fixtures.entry("user", "Keep this text", attachments=[{
                "kind": "image", "name": "picture.png", "path": str(path)}])]
            for data in (None, b"invalid", b"\x89PNG\r\n\x1a\n" + b"x" * 100):
                if data is not None: path.write_bytes(data)
                with patch("chat_history.MAX_IMAGE_BYTES", 16):
                    messages = portable_history(entries)
                self.assertIn("Keep this text", str(messages))
                self.assertIn("pixels are unavailable or invalid", str(messages))
                self.assertFalse(any(block["type"] == "image" for msg in messages for block in msg["content"]))


if __name__ == "__main__": unittest.main()
