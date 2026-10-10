"""Team Blackboard: bounded store, user commands, member tools and projection."""
import json
from pathlib import Path
import stat
import tempfile
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
        self.transport.responses.extend([call("blackboard_post", {"key": "x", "body": "y"}), response("ok")])
        self.service.handle({"command": "send", "id": self.chat["id"], "text": "Pin it"})
        self.service.thread.join(3)
        offered = {tool["name"] for tool in self.transport.requests[0]["tools"]}
        self.assertFalse(offered & blackboard.TOOLS)
        row = next(item for item in self.chat["entries"] if item.get("tool") == "blackboard_post")
        self.assertTrue(row["isError"]); self.assertIn("only to Team members", row["detail"])
        self.assertNotIn("blackboard", self.chat)


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

    def test_helpers_and_side_chats_cannot_run_blackboard_tools(self):
        for role in ("parent", "side", "lane"):
            service = ChatService(self.store, FakeTransport([]), lambda event: None, role=role)
            with self.assertRaisesRegex(ValueError, "Team members"):
                blackboard.execute(service, "blackboard_post", {"key": "x", "body": "y"})


if __name__ == "__main__": unittest.main()
