"""Team Blackboard: bounded store, user commands, member tools and projection."""
import json
import base64
import os
from pathlib import Path
import stat
import shutil
import subprocess
import sys
import threading
import tempfile
import time
import unittest
from unittest.mock import patch

import chat_blackboard as blackboard
import chat_team
from chat_runtime import ChatService, ChatStore, entry
from test_chat_agents import call
from test_chat_runtime import FakeTransport, response

NOW = "2026-10-10T12:00:00+00:00"
MEMBER = {"id": "m1", "name": "Sol", "route": "ollama/test"}


class BlackboardStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.chat = {"id": "a" * 32, "workspace": str(self.root), "entries": [], "messages": []}

    def test_upsert_by_author_and_key_keeps_identity(self):
        first, status = blackboard.post(self.chat, MEMBER, {"key": "api", "body": "Use v2"}, now=NOW)
        self.assertEqual((status, first["category"]), ("posted", "note"))
        again, status = blackboard.post(self.chat, MEMBER, {"key": "api", "body": "Use v3", "category": "decision"}, now="later")
        self.assertEqual(status, "replaced")
        self.assertEqual((again["id"], again["created"], again["updated"]), (first["id"], NOW, "later"))
        peer = {"id": "m2", "name": "Opus", "route": "ollama/other"}
        blackboard.post(self.chat, peer, {"key": "api", "body": "Peer view"}, now=NOW)
        posts = blackboard.board(self.chat)["posts"]
        self.assertEqual([(p["author"], p["body"]) for p in posts], [("m1", "Use v3"), ("m2", "Peer view")])

    def test_invalid_input_never_mutates(self):
        blackboard.post(self.chat, MEMBER, {"key": "keep", "body": "Stays"}, now=NOW)
        saved = json.dumps(self.chat["blackboard"])
        for args in ({"key": "Bad Key", "body": "x"}, {"key": "ok", "body": "  "}, {"key": "ok", "body": "x", "category": "rumour"},
                     {"key": "ok", "body": "é" * blackboard.MAX_BODY_BYTES}, {"key": "ok", "body": "x", "quote_entry_id": "missing"}):
            with self.assertRaises(ValueError): blackboard.post(self.chat, MEMBER, args, now=NOW)
        self.assertEqual(json.dumps(self.chat["blackboard"]), saved)

    def test_full_board_refuses_instead_of_evicting(self):
        with patch.object(blackboard, "MAX_POSTS", 3):
            for index in range(3): blackboard.post(self.chat, MEMBER, {"key": f"k{index}", "body": "x"}, now=NOW)
            with self.assertRaisesRegex(ValueError, "full"): blackboard.post(self.chat, MEMBER, {"key": "new", "body": "x"}, now=NOW)
            self.assertEqual([p["key"] for p in self.chat["blackboard"]["posts"]], ["k0", "k1", "k2"])
            blackboard.post(self.chat, MEMBER, {"key": "k1", "body": "replacing my own key still works"}, now=NOW)
            blackboard.remove(self.chat, key="k0", author="m1")
            blackboard.post(self.chat, MEMBER, {"key": "new", "body": "x"}, now=NOW)
        with patch.object(blackboard, "MAX_BOARD_BYTES", 900):
            with self.assertRaisesRegex(ValueError, "full"):
                blackboard.post(self.chat, MEMBER, {"key": "big", "body": "y" * 1000}, now=NOW)

    def test_quote_pins_a_visible_entry_with_a_bounded_excerpt(self):
        tool = entry("tool", tool="run_shell", summary="Run tests", detail="FAILED " + "x" * 2000, memberName="Sol")
        self.chat["entries"].append(tool)
        row, _ = blackboard.post(self.chat, {"id": "user", "name": "You"}, {"key": "failing", "body": "See this", "quote_entry_id": tool["id"]}, now=NOW)
        self.assertEqual((row["quote"]["entryID"], row["quote"]["tool"], row["quote"]["author"]), (tool["id"], "run_shell", "Sol"))
        self.assertLessEqual(len(row["quote"]["excerpt"].encode()), blackboard.MAX_QUOTE_BYTES)

    def test_members_cannot_remove_user_posts(self):
        row, _ = blackboard.post(self.chat, {"id": "user", "name": "You"}, {"key": "rule", "body": "No force pushes"}, now=NOW)
        with self.assertRaisesRegex(ValueError, "user"):
            blackboard.remove(self.chat, key="rule", author="user", allow_user=False)
        blackboard.remove(self.chat, post_id=row["id"])
        self.assertEqual(self.chat["blackboard"]["posts"], [])

    def test_attachments_are_private_copies_and_message_files_are_listed(self):
        clip = self.root / "clip.mov"; clip.write_bytes(b"\0" * 64)
        song = self.root / "song.m4a"; song.write_bytes(b"\0" * 32)
        added = blackboard.attach(self.chat, self.root, paths=[str(clip), str(song)], now=NOW)
        self.assertEqual([item["kind"] for item in added], ["video", "audio"])
        copy = Path(added[0]["path"])
        self.assertEqual(copy.parent, blackboard.directory(self.root, self.chat["id"]))
        self.assertEqual(stat.S_IMODE(copy.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(copy.parent.stat().st_mode), 0o700)
        link = blackboard.attach(self.chat, self.root, url="https://example.com/spec", now=NOW)[0]
        self.assertEqual((link["kind"], link["name"]), ("url", "example.com/spec"))
        for bad in ("javascript:alert(1)", "https://user:pw@example.com", "ftp://example.com", "https://exa mple.com"):
            with self.assertRaises(ValueError): blackboard.attach(self.chat, self.root, url=bad, now=NOW)
        original = self.root / "chat-attachment.png"; original.write_bytes(b"png")
        self.chat["entries"].append(entry("user", "look", attachments=[
            {"id": "f" * 32, "name": "shot.png", "path": str(original), "kind": "image", "size": 3},
            {"id": "e" * 32, "name": "paper.pdf", "path": str(original), "kind": "file", "size": 3}]))
        snapshot = blackboard.snapshot(self.chat, self.root)
        self.assertEqual([(row["kind"], row["source"]) for row in snapshot["attachments"]],
                         [("url", "board"), ("audio", "board"), ("video", "board"), ("pdf", "message"), ("image", "message")])
        self.assertTrue(snapshot["thumbnails"].endswith("/blackboard/thumbnails"))
        thumbnails = Path(snapshot["thumbnails"]); thumbnails.mkdir()
        (thumbnails / (added[0]["id"] + ".png")).write_bytes(b"thumb")
        removed = blackboard.detach(self.chat, self.root, added[0]["id"])
        blackboard.discard_files(self.root, self.chat["id"], removed)
        self.assertFalse(copy.exists())
        self.assertFalse((thumbnails / (added[0]["id"] + ".png")).exists())
        self.assertTrue(original.exists())
        with self.assertRaises(ValueError): blackboard.detach(self.chat, self.root, "f" * 32)

    def test_attachment_caps(self):
        small = self.root / "a.txt"; small.write_bytes(b"12345")
        with patch.object(blackboard, "MAX_ATTACHMENT_BYTES", 4):
            with self.assertRaisesRegex(ValueError, "larger"): blackboard.attach(self.chat, self.root, paths=[str(small)], now=NOW)
        with patch.object(blackboard, "MAX_ATTACHMENTS_BYTES", 8):
            with self.assertRaisesRegex(ValueError, "total"):
                blackboard.attach(self.chat, self.root, paths=[str(small), str(small)], now=NOW)
        with patch.object(blackboard, "MAX_ATTACHMENTS", 1):
            blackboard.attach(self.chat, self.root, paths=[str(small)], now=NOW)
            with self.assertRaisesRegex(ValueError, "at most"): blackboard.attach(self.chat, self.root, url="https://a.example", now=NOW)
        folder = blackboard.directory(self.root, self.chat["id"])
        self.assertEqual(len(list(folder.iterdir())), 1, "a refused attach left a copy behind")
        with self.assertRaises(ValueError): blackboard.attach(self.chat, self.root, paths=[str(self.root)], now=NOW)

    def member_workspace(self):
        workspace = self.root / "work"; (workspace / "notes").mkdir(parents=True)
        (workspace / "notes" / "a.txt").write_text("finding")
        outside = self.root / "secret.txt"; outside.write_text("secret")
        (workspace / "escape.txt").symlink_to(outside)
        (workspace / "linked").symlink_to(self.root, target_is_directory=True)
        return workspace, self.root / "store"

    def test_member_files_must_be_readable_regular_files_inside_the_workspace(self):
        workspace, store = self.member_workspace()
        locked = workspace / "locked.txt"; locked.write_text("x"); locked.chmod(0)
        self.addCleanup(locked.chmod, 0o600)
        cases = [("../secret.txt", "Parent traversal"), (str(self.root / "secret.txt"), "outside workspace"),
                 ("escape.txt", "Symlink"), ("linked/secret.txt", "Symlink"), ("notes", "not a regular file"),
                 (".", "Expected a file path"), ("missing.txt", "does not exist")]
        if os.geteuid() != 0: cases.append(("locked.txt", "cannot be read"))
        for path, message in cases:
            with self.subTest(path), self.assertRaisesRegex(ValueError, message):
                blackboard.member_attach(self.chat, store, str(workspace), MEMBER, paths=[path], now=NOW)
        # One bad item refuses the whole call: nothing stored, nothing copied.
        with self.assertRaises(ValueError):
            blackboard.member_attach(self.chat, store, str(workspace), MEMBER, paths=["notes/a.txt", "../secret.txt"], now=NOW)
        self.assertNotIn("blackboard", self.chat)
        self.assertFalse(blackboard.directory(store, self.chat["id"]).exists())

    def test_member_file_cap_links_and_attribution(self):
        workspace, store = self.member_workspace()
        with (workspace / "big.bin").open("wb") as stream: stream.truncate(blackboard.MAX_MEMBER_FILE_BYTES + 1)
        with self.assertRaisesRegex(ValueError, "8 MiB"):
            blackboard.member_attach(self.chat, store, str(workspace), MEMBER, paths=["big.bin"], now=NOW)
        for url in ("ftp://example.com/a", "file:///etc/passwd", "javascript:alert(1)", "https://u:p@example.com"):
            with self.subTest(url), self.assertRaisesRegex(ValueError, "http or https"):
                blackboard.member_attach(self.chat, store, str(workspace), MEMBER, links=[{"url": url}], now=NOW)
        with self.assertRaisesRegex(ValueError, "at most"):
            blackboard.member_attach(self.chat, store, str(workspace), MEMBER, links=[{"url": "https://a.example"}] * 5, now=NOW)
        added = blackboard.member_attach(self.chat, store, str(workspace), MEMBER, paths=["notes/a.txt"],
                                         links=[{"url": "https://example.com/spec", "title": "Spec"}], now=NOW)
        self.assertEqual([(i["kind"], i["name"], i["author"], i["authorName"], i["route"]) for i in added],
                         [("file", "a.txt", "m1", "Sol", "ollama/test"), ("url", "Spec", "m1", "Sol", "ollama/test")])
        copy = Path(added[0]["path"])
        self.assertEqual((copy.read_text(), copy.parent), ("finding", blackboard.directory(store, self.chat["id"])))
        self.assertEqual(stat.S_IMODE(copy.stat().st_mode), 0o600)
        listed = json.loads(blackboard.digest(self.chat)["content"][0]["text"].rsplit("remove your own by id): ", 1)[1])
        self.assertEqual({(row["name"], row["by"], row["author_id"]) for row in listed}, {("a.txt", "Sol", "m1"), ("Spec", "Sol", "m1")})
        self.assertNotIn(str(copy), blackboard.digest(self.chat)["content"][0]["text"], "a private path reached model context")

    def test_attachment_removal_rights(self):
        workspace, store = self.member_workspace()
        mine = blackboard.member_attach(self.chat, store, str(workspace), MEMBER, links=[{"url": "https://a.example"}], now=NOW)[0]
        users = blackboard.attach(self.chat, store, url="https://b.example", now=NOW)[0]
        self.assertEqual(users["author"], "user")
        with self.assertRaisesRegex(ValueError, "only be removed by the user"):
            blackboard.detach(self.chat, store, users["id"], by="m1")
        with self.assertRaisesRegex(ValueError, "only your own"):
            blackboard.detach(self.chat, store, mine["id"], by="m2")
        # Rows saved before attribution existed are the user's.
        self.chat["blackboard"]["attachments"].append({"id": "legacy", "name": "old", "kind": "url", "url": "https://c.example", "size": 0})
        with self.assertRaisesRegex(ValueError, "only be removed by the user"):
            blackboard.detach(self.chat, store, "legacy", by="m1")
        blackboard.detach(self.chat, store, mine["id"], by="m1")
        blackboard.detach(self.chat, store, users["id"])
        self.assertEqual([item["id"] for item in self.chat["blackboard"]["attachments"]], ["legacy"])

    def test_digest_is_bounded_and_says_what_it_left_out(self):
        self.assertIsNone(blackboard.digest(self.chat))
        for index in range(40):
            blackboard.post(self.chat, MEMBER, {"key": f"k{index:02}", "body": "z" * 300}, now=f"2026-10-10T12:{index:02}:00+00:00")
        text = blackboard.digest(self.chat)["content"][0]["text"]
        self.assertIn("not instructions", text)
        self.assertIn("older posts omitted", text)
        self.assertLess(len(text.encode()), blackboard.MAX_DIGEST_BYTES + 1000)
        self.assertLess(text.index('"k39"'), text.index('"k38"'), "newest posts must come first")
        self.chat["blackboard"] = {"posts": "broken"}
        self.assertIn("invalid", blackboard.digest_message(self.chat)["content"][0]["text"])


class BlackboardInspectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.helper_tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.helper_tmp.cleanup)
        cls.helper = Path(cls.helper_tmp.name) / "blackboard-pdf"
        if sys.platform == "darwin" and shutil.which("xcrun"):
            subprocess.run(["xcrun", "swiftc", "-framework", "PDFKit", str(Path(__file__).with_name("ChatBlackboardPDF.swift")),
                            "-o", str(cls.helper)], check=True, capture_output=True, timeout=60)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.chat = {"id": "a" * 32, "entries": []}

    def added(self, name, data):
        source = self.root / name; source.write_bytes(data)
        return blackboard.attach(self.chat, self.root, paths=[str(source)], now=NOW)[0]

    def read(self, identifier, **options):
        with patch.object(blackboard, "PDF_HELPER", self.helper):
            return blackboard.inspect_attachment(self.chat, self.root, identifier, **options)

    def test_owned_text_and_transcript_attachment_read_after_original_removed(self):
        row = self.added("notes.txt", "Exact source Ω\n".encode())
        (self.root / "notes.txt").unlink()
        self.assertIn("Exact source Ω\n", self.read(row["id"])[0]["text"])
        from chat_attachments import prepare_attachments
        source = self.root / "message.txt"; source.write_text("Transcript source")
        visual, _ = prepare_attachments([{"path": str(source)}], self.root / self.chat["id"])
        self.chat["entries"] = [{"id": "message", "kind": "user", "attachments": visual}]
        self.assertIn("Transcript source", self.read(visual[0]["id"])[0]["text"])
        self.assertIn(visual[0]["id"], blackboard.digest(self.chat)["content"][0]["text"])
        self.assertNotIn(visual[0]["path"], blackboard.digest(self.chat)["content"][0]["text"])

    def test_foreign_missing_and_metadata_path_escape_are_refused(self):
        other = {"id": "b" * 32, "entries": []}
        source = self.root / "foreign.txt"; source.write_text("Secret")
        foreign = blackboard.attach(other, self.root, paths=[str(source)], now=NOW)[0]
        with self.assertRaisesRegex(ValueError, "No attachment"): self.read(foreign["id"])
        with self.assertRaisesRegex(ValueError, "No attachment"): self.read(str(source))
        row = self.added("owned.txt", b"owned")
        Path(row["path"]).unlink()
        with self.assertRaises(FileNotFoundError): self.read(row["id"])
        self.chat["blackboard"]["attachments"][0]["path"] = str(source)
        with self.assertRaisesRegex(ValueError, "outside"): self.read(row["id"])
        blackboard.detach(self.chat, self.root, row["id"])
        with self.assertRaisesRegex(ValueError, "No attachment"): self.read(row["id"])

    def test_symlink_and_file_and_text_bounds(self):
        row = self.added("large.txt", b"x")
        path = Path(row["path"])
        with path.open("wb") as stream: stream.truncate(blackboard.MAX_READ_BYTES + 1)
        with self.assertRaisesRegex(ValueError, "8 MiB"): self.read(row["id"])
        path.write_text("Ω" * 110_000)
        text = self.read(row["id"])[0]["text"]
        self.assertIn("Text truncated", text)
        self.assertEqual(text.rsplit("\n\n", 1)[1], "Ω" * 100_000)
        path.unlink(); path.symlink_to(self.root / "large.txt")
        with self.assertRaises(OSError): self.read(row["id"])

    def test_real_image_bytes_and_vision_gate(self):
        from test_chat_attachments import PNG
        row = self.added("pixel.png", PNG)
        blocks = self.read(row["id"])
        self.assertIn("width 1, height 1", blocks[0]["text"])
        self.assertEqual(blocks[1]["type"], "image")
        self.assertEqual(base64.b64decode(blocks[1]["source"]["data"]), PNG)
        with self.assertRaisesRegex(ValueError, "does not accept images"): self.read(row["id"], vision=False)

    def test_real_pdf_text_extraction_and_invalid_pdf(self):
        if not self.helper.exists(): self.skipTest("PDFKit helper requires macOS")
        # A complete real PDF with xref and an embedded Helvetica text stream.
        stream = b"BT /F1 12 Tf 20 100 Td (Blackboard PDF reference) Tj ET"
        objects = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
                   b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
                   b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
                   b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream"]
        pdf = b"%PDF-1.4\n"; offsets = [0]
        for index, obj in enumerate(objects, 1):
            offsets.append(len(pdf)); pdf += f"{index} 0 obj\n".encode() + obj + b"\nendobj\n"
        start = len(pdf)
        pdf += b"xref\n0 6\n0000000000 65535 f \n"
        pdf += b"".join(f"{offset:010} 00000 n \n".encode() for offset in offsets[1:])
        pdf += f"trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n{start}\n%%EOF\n".encode()
        row = self.added("reference.pdf", pdf)
        self.assertIn("Blackboard PDF reference", self.read(row["id"])[0]["text"])
        with patch.object(blackboard.subprocess, "run", side_effect=subprocess.TimeoutExpired("pdf", 15)):
            with self.assertRaisesRegex(ValueError, "extraction failed"): self.read(row["id"])
        self.assertEqual(Path(row["path"]).read_bytes(), pdf)
        # Even an unexpectedly oversized helper reply cannot escape the bound.
        with patch.object(blackboard.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, b"x" * 200_010, b"")):
            self.assertEqual(len(self.read(row["id"])[0]["text"].rsplit("\n\n", 1)[1]), 200_000)
        bad = self.added("invalid.pdf", b"%PDF-broken")
        with self.assertRaisesRegex(ValueError, "extraction failed"): self.read(bad["id"])

    def test_audio_video_and_links_report_preview_only(self):
        row = self.added("clip.wav", b"audio fixture")
        self.assertIn("preview-only", self.read(row["id"])[0]["text"])
        link = blackboard.attach(self.chat, self.root, url="https://example.com", now=NOW)[0]
        self.assertIn("not been fetched", self.read(link["id"])[0]["text"])


class BlackboardCommandTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.store = ChatStore(self.root, lock=False); self.events = []
        self.transport = FakeTransport([])
        self.service = ChatService(self.store, self.transport, self.events.append)
        self.service.refresh(); self.service.create(self.transport.rows[0]["id"], str(self.root))
        self.chat = self.service.chat

    def command(self, name, **fields):
        self.service.handle({"command": name, "id": self.chat["id"], "request": "r-" + name, **fields})
        event = self.events[-1]
        self.assertEqual((event["event"], event["chat"], event["request"]), ("blackboard", self.chat["id"], "r-" + name))
        return event

    def test_user_posts_are_saved_and_published(self):
        event = self.command("blackboard_post", key="deploy", body="Deploy after review", category="decision")
        self.assertNotIn("notice", event)
        self.assertEqual(event["blackboard"]["posts"][0]["authorName"], "You")
        saved = self.store.load(self.chat["id"])
        self.assertEqual(saved["blackboard"]["posts"][0]["key"], "deploy")
        post = saved["blackboard"]["posts"][0]["id"]
        self.assertEqual(self.command("blackboard_remove", post=post)["blackboard"]["posts"], [])
        self.assertEqual(self.store.load(self.chat["id"])["blackboard"]["posts"], [])
        snapshot = self.command("blackboard")
        self.assertEqual(snapshot["blackboard"]["limits"]["posts"], blackboard.MAX_POSTS)

    def test_rejected_commands_report_a_notice_without_changes(self):
        event = self.command("blackboard_post", key="x", body="", category="note")
        self.assertIn("nonempty", event["notice"])
        self.assertNotIn("blackboard", self.store.load(self.chat["id"]))
        self.assertIn("notice", self.command("blackboard_remove", post="missing"))

    def test_failed_save_rolls_back_the_board_and_its_copies(self):
        source = self.root / "notes.txt"; source.write_text("hello")
        with patch.object(self.store, "save", side_effect=OSError("disk full")):
            event = self.command("blackboard_attach", paths=[str(source)])
        self.assertIn("disk full", event["notice"])
        self.assertNotIn("blackboard", self.chat)
        folder = blackboard.directory(self.store.root, self.chat["id"])
        self.assertEqual(list(folder.iterdir()) if folder.exists() else [], [])

    def test_solo_chat_model_has_no_blackboard_tools(self):
        (self.root / "notes.txt").write_text("hello")
        self.transport.responses.extend([call("blackboard_post", {"key": "x", "body": "y"}),
                                         call("blackboard_attach", {"paths": ["notes.txt"]}), response("ok")])
        self.service.handle({"command": "send", "id": self.chat["id"], "text": "Pin it"})
        self.service.thread.join(3)
        offered = {tool["name"] for tool in self.transport.requests[0]["tools"]}
        self.assertFalse(offered & blackboard.TOOLS)
        for tool in ("blackboard_post", "blackboard_attach"):
            row = next(item for item in self.chat["entries"] if item.get("tool") == tool)
            self.assertTrue(row["isError"]); self.assertIn("only to Team members", row["detail"])
        self.assertNotIn("blackboard", self.chat)
        self.assertFalse(blackboard.directory(self.store.root, self.chat["id"]).exists())


class BlackboardTeamTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = ChatStore(self.root, lock=False); self.events = []

    def team(self, scripts):
        self.transports = [FakeTransport(script) for script in scripts]
        available = iter(self.transports)
        parent = ChatService(self.store, FakeTransport([]), self.events.append, child_factory=lambda: next(available))
        parent.refresh(); parent.create(parent.models[0]["id"], str(self.root))
        specs = [{"choice": parent.models[i % 2]["id"], "name": f"Member {i + 1}", "effort": "", "responsibility": ""}
                 for i in range(len(scripts))]
        parent.handle({"command": "configure_team", "id": parent.chat["id"], "enabled": True,
                       "execution": {"mode": "contribution"}, "members": specs, "request": "config"})
        return parent

    def run_team(self, parent):
        parent.handle({"command": "send", "id": parent.chat["id"], "text": "Work on it"})
        parent.thread.join(4)
        self.assertFalse(parent.busy)

    def wait_for(self, check):
        end = time.monotonic() + 3
        while not check() and time.monotonic() < end: time.sleep(.005)
        self.assertTrue(check())

    def test_member_posts_reach_peers_and_appear_as_tool_rows(self):
        parent = self.team([[call("blackboard_post", {"key": "schema", "body": "Users table has no email index", "category": "risk"}),
                             response("Posted")], [response("Read it")]])
        self.store.save(parent.chat)
        blackboard.post(parent.chat, {"id": "user", "name": "You"}, {"key": "rule", "body": "Never drop tables"}, now=NOW)
        self.run_team(parent)
        offered = {tool["name"] for tool in self.transports[0].requests[0]["tools"]}
        self.assertTrue(blackboard.TOOLS <= offered)
        first = json.dumps(self.transports[0].requests[0]["messages"][0])
        self.assertIn("Team Blackboard", first); self.assertIn("Never drop tables", first)
        self.assertIn("blackboard_read", self.transports[0].requests[0]["system"])
        post = parent.chat["blackboard"]["posts"][-1]
        member = parent.chat["team"]["members"][0]
        self.assertEqual((post["author"], post["authorName"], post["category"]), (member["id"], "Member 1", "risk"))
        row = next(item for item in parent.chat["entries"] if item.get("tool") == "blackboard_post")
        self.assertEqual((row["summary"], row["memberName"], row["isError"]), ("Pinned schema (risk)", "Member 1", False))
        self.assertTrue(any(e.get("event") == "blackboard" and e["blackboard"]["posts"][-1]["key"] == "schema" for e in self.events))
        self.assertEqual(self.store.load(parent.chat["id"])["blackboard"]["posts"][-1]["key"], "schema")
        saved = [m for m in parent.chat["messages"] if "Team Blackboard" in json.dumps(m)]
        self.assertEqual(saved, [], "the digest is a projection, never saved history")
        for member in parent.chat["team"]["members"]:
            self.assertNotIn("Team Blackboard", json.dumps(member.get("messages", [])))

    def test_member_cannot_remove_user_posts_and_digest_yields_to_small_contexts(self):
        parent = self.team([[call("blackboard_remove", {"key": "rule", "author_id": "user"}), response("Tried")]])
        blackboard.post(parent.chat, {"id": "user", "name": "You"}, {"key": "rule", "body": "Keep me"}, now=NOW)
        for index in range(20):
            blackboard.post(parent.chat, {"id": "user", "name": "You"}, {"key": f"n{index}", "body": "w" * 300}, now=NOW)
        with patch("chat_runtime.chat_execution.context", return_value=6000):
            self.run_team(parent)
        row = next(item for item in parent.chat["entries"] if item.get("tool") == "blackboard_remove")
        self.assertTrue(row["isError"]); self.assertIn("only be removed by the user", row["detail"])
        self.assertIn("rule", [p["key"] for p in parent.chat["blackboard"]["posts"]])
        first = json.dumps(self.transports[0].requests[0]["messages"][0])
        self.assertIn("digest omitted", first); self.assertNotIn("Keep me", first)

    def test_checkpoint_journal_carries_the_board(self):
        parent = self.team([[]])
        member = parent.chat["team"]["members"][0]
        chat_team.start_run(parent, new_input=True)
        parent.save()
        blackboard.post(parent.chat, {"id": member["id"], "name": member["name"]}, {"key": "found", "body": "Bug in parser"}, now=NOW)
        chat_team.checkpoint(parent, member, {})
        # The snapshot predates the post; only the appended checkpoint has it.
        journal = chat_team.journal_path(self.store, parent.chat["id"]).read_text()
        self.assertIn("Bug in parser", journal)
        self.assertEqual(self.store.load(parent.chat["id"])["blackboard"]["posts"][0]["key"], "found")

    def test_member_attachments_are_attributed_listed_and_removable_only_by_owner_or_user(self):
        (self.root / "report.txt").write_text("results")
        def peer(payload, cancel, delta):
            self.wait_for(lambda: bool(parent.chat.get("blackboard", {}).get("attachments")))
            return call("blackboard_remove", {"attachment_id": parent.chat["blackboard"]["attachments"][0]["id"]})
        parent = self.team([[call("blackboard_attach", {"paths": ["report.txt"], "links": [{"url": "https://example.com/spec"}]}),
                             response("Added")], [peer, response("Tried")]])
        self.run_team(parent)
        first, second = parent.chat["team"]["members"]
        row = next(item for item in parent.chat["entries"] if item.get("tool") == "blackboard_attach")
        self.assertEqual((row["summary"], row["memberName"], row["isError"]), ("Added report.txt, example.com/spec", "Member 1", False))
        refused = next(item for item in parent.chat["entries"] if item.get("tool") == "blackboard_remove")
        self.assertTrue(refused["isError"]); self.assertIn("only your own", refused["detail"])
        saved = self.store.load(parent.chat["id"])["blackboard"]["attachments"]
        self.assertEqual([(item["name"], item["author"]) for item in saved], [("report.txt", first["id"]), ("example.com/spec", first["id"])])
        self.assertEqual(Path(saved[0]["path"]).read_text(), "results")
        projected = json.dumps(self.transports[1].requests[1]["messages"][0])
        self.assertIn("report.txt", projected); self.assertIn("Member 1", projected)
        snapshot = next(e for e in reversed(self.events) if e.get("event") == "blackboard" and e.get("blackboard"))
        self.assertEqual(snapshot["blackboard"]["attachments"][0]["authorName"], "Member 1")
        # The user may remove a member's file.
        parent.handle({"command": "blackboard_detach", "id": parent.chat["id"], "attachment": saved[0]["id"], "request": "detach"})
        self.assertNotIn("notice", self.events[-1])
        self.assertFalse(Path(saved[0]["path"]).exists())
        self.assertEqual([item["name"] for item in self.store.load(parent.chat["id"])["blackboard"]["attachments"]], ["example.com/spec"])
        self.assertNotEqual(second["id"], first["id"])

    def test_member_removal_is_durable_before_file_cleanup_and_next_checkpoint(self):
        parent = self.team([[response("Unused")]])
        member = parent.chat["team"]["members"][0]
        (self.root / "report.txt").write_text("keep these results")
        rows = blackboard.member_attach(parent.chat, self.store.root, str(self.root), member,
                                        paths=["report.txt"], now=NOW)
        retained = blackboard.member_attach(parent.chat, self.store.root, str(self.root), member,
                                            paths=["report.txt"], now=NOW)[0]
        parent.save()
        chat_team.start_run(parent, new_input=True)
        parent._working = True
        service = type("MemberService", (), {"role": "team", "team_parent": parent, "team_member": member})()
        cleanup = blackboard.discard_files
        def crash_boundary(root, chat_id, removed):
            # A fresh store replays the journal, as after process death. No
            # subsequent tool-result checkpoint or final Team save has run.
            reopened = ChatStore(self.root, lock=False).load(chat_id)
            self.assertEqual([row["id"] for row in reopened["blackboard"]["attachments"]], [retained["id"]])
            self.assertTrue(Path(removed["path"]).exists())
            cleanup(root, chat_id, removed)
        with patch.object(blackboard, "discard_files", side_effect=crash_boundary):
            blackboard.execute(service, "blackboard_remove", {"attachment_id": rows[0]["id"]})
        self.assertFalse(Path(rows[0]["path"]).exists())
        self.assertEqual(Path(retained["path"]).read_text(), "keep these results")

    def test_attachment_inspection_reaches_member_tool_result_content(self):
        from test_chat_attachments import PNG
        source = self.root / "pixel.png"; source.write_bytes(PNG)
        def inspect(payload, cancel, delta):
            return call("blackboard_read", {"attachment_id": row["id"]})
        parent = self.team([[inspect, response("Inspected")]])
        row = blackboard.attach(parent.chat, self.store.root, paths=[str(source)], now=NOW)[0]
        parent.save()
        self.run_team(parent)
        results = [block for message in self.transports[0].requests[1]["messages"] for block in message.get("content", [])
                   if block.get("type") == "tool_result"]
        image = next(block for result in results for block in result["content"] if block.get("type") == "image")
        self.assertEqual(base64.b64decode(image["source"]["data"]), PNG)
        tool = next(item for item in parent.chat["entries"] if item.get("tool") == "blackboard_read")
        self.assertFalse(tool["isError"])

    def test_slow_pdf_inspection_releases_parent_mutex(self):
        parent = self.team([[response("Unused")]])
        member = parent.chat["team"]["members"][0]
        source = self.root / "slow.pdf"; source.write_bytes(b"%PDF-test")
        row = blackboard.attach(parent.chat, self.store.root, paths=[str(source)], now=NOW)[0]
        service = type("MemberService", (), {"role": "team", "team_parent": parent,
                       "team_member": member, "choice": lambda self: {"vision": True}})()
        entered, release, acquired = threading.Event(), threading.Event(), threading.Event()
        results, errors = [], []
        def slow_pdf(*args, **kwargs):
            entered.set()
            if not release.wait(3): raise AssertionError("PDF release was not signalled")
            return subprocess.CompletedProcess([], 0, b"Extracted reference", b"")
        def read():
            try: results.append(blackboard.execute(service, "blackboard_read", {"attachment_id": row["id"]}))
            except BaseException as exc: errors.append(exc)
        def peer():
            with parent._mutex: acquired.set()
        with patch.object(blackboard.subprocess, "run", side_effect=slow_pdf):
            reader = threading.Thread(target=read); reader.start()
            peer_thread = None
            try:
                self.assertTrue(entered.wait(3), "PDF helper did not start")
                peer_thread = threading.Thread(target=peer); peer_thread.start()
                self.assertTrue(acquired.wait(1), "slow attachment read held the Team mutex")
                self.assertFalse(release.is_set())
                with self.assertRaisesRegex(ValueError, "No attachment"):
                    blackboard.execute(service, "blackboard_read", {"attachment_id": "foreign"})
            finally:
                release.set(); reader.join(3)
                if peer_thread: peer_thread.join(3)
        self.assertFalse(errors, errors)
        self.assertIn("Extracted reference", results[0]["content"][0]["text"])

    def test_member_removal_save_failure_restores_metadata_and_retains_file(self):
        parent = self.team([[response("Unused")]])
        member = parent.chat["team"]["members"][0]
        (self.root / "report.txt").write_text("results")
        row = blackboard.member_attach(parent.chat, self.store.root, str(self.root), member,
                                       paths=["report.txt"], now=NOW)[0]
        parent.save()
        chat_team.start_run(parent, new_input=True)
        parent._working = True
        service = type("MemberService", (), {"role": "team", "team_parent": parent, "team_member": member})()
        with patch.object(chat_team, "checkpoint", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(OSError, "disk full"):
                blackboard.execute(service, "blackboard_remove", {"attachment_id": row["id"]})
        self.assertEqual(parent.chat["blackboard"]["attachments"][0]["id"], row["id"])
        self.assertEqual(self.store.load(parent.chat["id"])["blackboard"]["attachments"][0]["id"], row["id"])
        self.assertEqual(Path(row["path"]).read_text(), "results")

    def test_helpers_and_side_chats_cannot_run_blackboard_tools(self):
        for role in ("parent", "side", "lane"):
            service = ChatService(self.store, FakeTransport([]), lambda event: None, role=role)
            for name, args in (("blackboard_post", {"key": "x", "body": "y"}), ("blackboard_attach", {"links": [{"url": "https://a.example"}]})):
                with self.subTest(role=role, tool=name), self.assertRaisesRegex(ValueError, "Team members"):
                    blackboard.execute(service, name, args)


if __name__ == "__main__": unittest.main()
