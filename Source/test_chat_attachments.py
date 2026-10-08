import base64
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from chat_attachments import prepare_attachments, bound_image_history
from cli_routes import _flatten_blocks
import test_chat_runtime as fixtures

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jGJ8AAAAASUVORK5CYII=")


class AttachmentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = self.root / "chats"; self.store.mkdir()

    def test_image_pixels_preserved_with_private_copy_and_dimensions(self):
        source = self.root / "picture.png"; source.write_bytes(PNG)
        visual, blocks = prepare_attachments([{"path": str(source)}], self.store)
        self.assertEqual(visual[0]["kind"], "image")
        self.assertEqual(Path(visual[0]["path"]).read_bytes(), PNG)
        self.assertEqual(os.stat(visual[0]["path"]).st_mode & 0o777, 0o600)
        self.assertIn("width 1, height 1", blocks[0]["text"])
        self.assertEqual(base64.b64decode(blocks[1]["source"]["data"]), PNG)
        source.unlink()
        self.assertTrue(Path(visual[0]["path"]).exists())

    def test_text_and_pdf_are_actual_text_for_existing_cli_routes(self):
        text = self.root / "code.py"; text.write_text("print('source')\n")
        pdf = self.root / "paper.pdf"; pdf.write_bytes(b"%PDF-1.7\nfixture")
        visual, blocks = prepare_attachments([{"path": str(text)}, {"path": str(pdf), "extractedText": "Extracted PDF text"}], self.store)
        flattened = _flatten_blocks(blocks)
        self.assertIn("print('source')", flattened)
        self.assertIn("Extracted PDF text", flattened)
        self.assertNotIn("document omitted", flattened)
        self.assertEqual([item["kind"] for item in visual], ["file", "file"])

    def test_invalid_files_rejected_before_any_copy_or_send(self):
        good = self.root / "good.txt"; good.write_text("text")
        bad = self.root / "bad.bin"; bad.write_bytes(b"\x00\xffbinary")
        with self.assertRaises(ValueError): prepare_attachments([{"path": str(good)}, {"path": str(bad)}], self.store)
        self.assertFalse((self.store / "attachments").exists())
        with self.assertRaises(ValueError): prepare_attachments([{"path": str(good)}] * 9, self.store)
        with patch("chat_attachments.MAX_FILE_BYTES", 2):
            with self.assertRaises(ValueError): prepare_attachments([{"path": str(good)}], self.store)
        os.mkfifo(self.root / "pipe")
        with self.assertRaises(ValueError): prepare_attachments([{"path": str(self.root / "pipe")}], self.store)
        pdf = self.root / "scanned.pdf"; pdf.write_bytes(b"%PDF-1.7")
        with self.assertRaisesRegex(ValueError, "extractable"): prepare_attachments([{"path": str(pdf)}], self.store)

    def test_known_text_only_model_and_symlink_storage_are_rejected(self):
        source = self.root / "picture.png"; source.write_bytes(PNG)
        with self.assertRaisesRegex(ValueError, "does not accept images"):
            prepare_attachments([{"path": str(source)}], self.store, vision=False)
        (self.store / "attachments").symlink_to(self.root, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symbolic link"):
            prepare_attachments([{"path": str(source)}], self.store)

    def test_history_bounds_images_without_losing_text_or_original(self):
        history = [{"role": "user", "content": [{"type": "text", "text": "keep my instruction"},
                    *[{"type": "image", "source": {"data": base64.b64encode(PNG).decode()}} for _ in range(22)]]}]
        bounded, count = bound_image_history(history)
        self.assertEqual(count, 2)
        self.assertEqual(bounded[0]["content"][0]["text"], "keep my instruction")
        self.assertEqual(sum(block["type"] == "image" for block in history[0]["content"]), 22)
        self.assertEqual(sum(block["type"] == "image" for block in bounded[0]["content"]), 20)

    def test_image_only_turn_is_saved_and_sent_with_pixels(self):
        fixture = fixtures.ChatRuntimeTests(); fixture.setUp()
        try:
            source = fixture.root / "picture.png"; source.write_bytes(PNG)
            service, transport = fixture.service([fixtures.response("Image received")])
            service.handle({"command": "send", "id": service.chat["id"], "text": "", "attachments": [{"path": str(source)}]})
            fixture.finish(service)
            saved = fixture.store.load(service.chat["id"])
            self.assertEqual(saved["entries"][0]["attachments"][0]["kind"], "image")
            self.assertEqual(transport.requests[0]["messages"][0]["content"][-1]["source"]["media_type"], "image/png")
            self.assertEqual(saved["title"], "picture.png")
            copied = Path(saved["entries"][0]["attachments"][0]["path"])
            fixture.store.delete(service.chat["id"])
            self.assertFalse(copied.exists())
            self.assertTrue(source.exists())
        finally: fixture.doCleanups()


if __name__ == "__main__": unittest.main()
