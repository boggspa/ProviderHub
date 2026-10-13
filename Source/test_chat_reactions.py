"""Emoji reactions: validation, the bounded store, member tool and user command."""
import json
from pathlib import Path
import tempfile
import time
import unittest

import chat_reactions as reactions
from chat_runtime import ChatService, ChatStore, entry
from test_chat_agents import call
from test_chat_runtime import FakeTransport, response

NOW = "2026-10-13T12:00:00+00:00"
SOL = {"id": "m1", "name": "Sol", "route": "ollama/test"}
OPUS = {"id": "m2", "name": "Opus", "route": "ollama/other"}


class EmojiTests(unittest.TestCase):
    def test_single_emoji_graphemes_are_accepted(self):
        for value in ("👍", "❤️", "‼️", "❓", " 😂 ", "👍🏽", "👩‍💻", "🤷🏼‍♂️", "🇬🇧", "1️⃣", "#️⃣", "©️", "🫡", "☕",
                      "👨🏿‍👩🏿‍👧🏿‍👦🏿", "🏳️‍🌈"):
            with self.subTest(value=value):
                self.assertEqual(reactions.normalize_emoji(value), value.strip())

    def test_words_pairs_and_fragments_are_refused(self):
        for value in ("", " ", "ok", "a👍", "👍👍", "👍 ❤️", "🇬", "👩‍", "1", None, 5, "x" * 20, "‍"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                reactions.normalize_emoji(value)


class ReactionStoreTests(unittest.TestCase):
    def setUp(self):
        self.user = entry("user", "Ship it?")
        self.reply = {**entry("assistant", "Shipping.", "ollama/test"), "memberID": "m1", "memberName": "Sol"}
        self.tool = entry("tool", "", "ollama/test", tool="run_shell", summary="Ran", detail="ok")
        self.later = entry("user", "Thanks.")
        self.chat = {"id": "c", "workspace": "/w", "entries": [self.user, self.reply, self.tool, self.later], "messages": []}

    def test_defaults_to_the_latest_user_message_and_one_reaction_per_author(self):
        item, status, previous = reactions.react(self.chat, SOL, emoji="👍", now=NOW)
        self.assertIs(item, self.later); self.assertEqual((status, previous), ("added", None))
        self.assertEqual(self.later["reactions"], [{"emoji": "👍", "author": "m1", "authorName": "Sol", "route": "ollama/test", "time": NOW}])
        _, status, previous = reactions.react(self.chat, SOL, emoji="👍", now="later")
        self.assertEqual((status, [r["emoji"] for r in previous]), ("kept", ["👍"]))
        _, status, _ = reactions.react(self.chat, SOL, emoji="❤️", now="later")
        self.assertEqual((status, [r["emoji"] for r in self.later["reactions"]]), ("replaced", ["❤️"]))
        reactions.react(self.chat, OPUS, emoji="❤️", now=NOW)
        reactions.react(self.chat, reactions.USER, emoji="😂", now=NOW)
        self.assertEqual([(r["author"], r["emoji"]) for r in self.later["reactions"]], [("m1", "❤️"), ("m2", "❤️"), ("user", "😂")])
        _, status, _ = reactions.react(self.chat, OPUS, emoji="x", remove=True, now=NOW)
        self.assertEqual((status, [r["author"] for r in self.later["reactions"]]), ("removed", ["m1", "user"]))
        with self.assertRaisesRegex(ValueError, "no reaction"):
            reactions.react(self.chat, OPUS, emoji="👍", remove=True, now=NOW)

    def test_targets_are_messages_only_and_never_your_own(self):
        item, _, _ = reactions.react(self.chat, OPUS, emoji="👀", entry_id=self.reply["id"], now=NOW)
        self.assertIs(item, self.reply)
        item, _, _ = reactions.react(self.chat, reactions.USER, emoji="👍", entry_id=self.reply["id"], now=NOW)
        self.assertEqual([r["author"] for r in self.reply["reactions"]], ["m2", "user"])
        with self.assertRaisesRegex(ValueError, "not to your own"):
            reactions.react(self.chat, SOL, emoji="👍", entry_id=self.reply["id"], now=NOW)
        for bad in (self.tool["id"], "missing", "", None if False else 7):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                reactions.react(self.chat, SOL, emoji="👍", entry_id=bad, now=NOW)
        self.assertNotIn("reactions", self.tool)
        with self.assertRaisesRegex(ValueError, "no user message"):
            reactions.react({"id": "e", "entries": [self.reply]}, SOL, emoji="👍", now=NOW)

    def test_cap_and_invalid_emoji_leave_the_message_unchanged(self):
        for index in range(reactions.MAX_REACTIONS):
            reactions.react(self.chat, {"id": f"m{index}", "name": f"M{index}", "route": ""}, emoji="👍", now=NOW)
        before = json.dumps(self.later)
        with self.assertRaisesRegex(ValueError, "16 reactions"):
            reactions.react(self.chat, OPUS if OPUS["id"] not in {f"m{i}" for i in range(16)} else {"id": "zz", "name": "Z", "route": ""},
                            emoji="👍", now=NOW)
        with self.assertRaises(ValueError):
            reactions.react(self.chat, {"id": "m0", "name": "M0", "route": ""}, emoji="nope", now=NOW)
        self.assertEqual(json.dumps(self.later), before)
        # The holder of a slot may still replace its own reaction when full.
        _, status, _ = reactions.react(self.chat, {"id": "m0", "name": "M0", "route": ""}, emoji="❤️", now=NOW)
        self.assertEqual(status, "replaced")


class ReactionServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = ChatStore(self.root, lock=False); self.events = []

    def solo(self, script):
        self.transport = FakeTransport(script)
        service = ChatService(self.store, self.transport, self.events.append)
        service.refresh(); service.create(self.transport.rows[0]["id"], str(self.root))
        return service

    def team(self, scripts):
        self.transports = [FakeTransport(script) for script in scripts]
        self.pending = list(self.transports)
        parent = ChatService(self.store, FakeTransport([]), self.events.append, child_factory=lambda: self.pending.pop(0))
        parent.refresh(); parent.create(parent.models[0]["id"], str(self.root))
        specs = [{"choice": parent.models[i % 2]["id"], "name": f"Member {i + 1}", "effort": "", "responsibility": ""}
                 for i in range(len(scripts))]
        parent.handle({"command": "configure_team", "id": parent.chat["id"], "enabled": True,
                       "execution": {"mode": "contribution"}, "members": specs, "request": "config"})
        return parent

    def run_turn(self, service, text="Work on it"):
        service.handle({"command": "send", "id": service.chat["id"], "text": text})
        service.thread.join(4)
        self.assertFalse(service.busy)

    def test_member_reactions_land_on_the_user_message_and_are_published_and_saved(self):
        parent = self.team([[call("react_to_message", {"emoji": "👍"}), response("On it.")],
                            [call("react_to_message", {"emoji": "👍"}), call("react_to_message", {"emoji": "❤️", "remove": True}),
                             response("Reading.")]])
        self.run_turn(parent, "Please review the schema")
        offered = {tool["name"] for tool in self.transports[0].requests[0]["tools"]}
        self.assertIn("react_to_message", offered)
        self.assertIn("react_to_message", self.transports[0].requests[0]["system"])
        message = next(item for item in parent.chat["entries"] if item["kind"] == "user")
        self.assertEqual(sorted((r["authorName"], r["emoji"]) for r in message.get("reactions", [])),
                         [("Member 1", "👍")])
        # Members run concurrently, so their rows interleave in either order.
        rows = [item for item in parent.chat["entries"] if item.get("tool") == "react_to_message"]
        self.assertEqual(sorted((row["summary"], row["isError"]) for row in rows),
                         [("Reacted 👍", False), ("Reacted 👍", False), ("Withdrew reaction", False)])
        first = next(row for row in rows if row["memberName"] == "Member 1")
        self.assertIn({"emoji": "👍", "by": "Member 1"}, json.loads(first["detail"])["reactions"])
        published = [e for e in self.events if e.get("event") == "entry" and e["entry"]["id"] == message["id"] and e["entry"].get("reactions")]
        self.assertTrue(published, "the updated user message never reached the window")
        self.assertEqual(self.store.load(parent.chat["id"])["entries"][0].get("reactions", [])[0]["authorName"], "Member 1")
        # The reaction record (its "author" key) is never projected into any
        # model history; only the tool's own result mentions reactions.
        for member in parent.chat["team"]["members"]:
            self.assertNotIn('"author"', json.dumps(member.get("messages", [])))
        self.assertNotIn('"author"', json.dumps(parent.chat["messages"]))

    def test_member_can_react_to_a_peer_reply_but_not_its_own_and_tool_rows_are_not_targets(self):
        parent = self.team([[response("First reply.")], [response("Second reply.")]])
        self.run_turn(parent)
        replies = {item["memberName"]: item for item in parent.chat["entries"] if item["kind"] == "assistant"}
        tool_row = entry("tool", "", "ollama/test", tool="run_shell", summary="Ran", detail="ok")
        with parent._mutex: parent.chat["entries"].append(tool_row)
        # A second turn: member 1 reacts to its own reply (refused) and to a
        # tool row (refused); member 2 reacts to member 1's reply.
        self.pending.extend([FakeTransport([call("react_to_message", {"emoji": "😂", "entry_id": replies["Member 1"]["id"]}),
                                            call("react_to_message", {"emoji": "😂", "entry_id": tool_row["id"]}), response("Ok.")]),
                             FakeTransport([call("react_to_message", {"emoji": "😂", "entry_id": replies["Member 1"]["id"]}),
                                            response("Nice.")])])
        self.run_turn(parent, "Carry on")
        rows = [item for item in parent.chat["entries"] if item.get("tool") == "react_to_message"]
        by_member = {}
        for row in rows: by_member.setdefault(row["memberName"], []).append((row["isError"], row["detail"]))
        self.assertEqual([flag for flag, _ in by_member["Member 1"]], [True, True])
        self.assertIn("not to your own", by_member["Member 1"][0][1])
        self.assertIn("not on tool rows", by_member["Member 1"][1][1])
        self.assertEqual([flag for flag, _ in by_member["Member 2"]], [False])
        target = next(item for item in parent.chat["entries"] if item["id"] == replies["Member 1"]["id"])
        self.assertEqual([(r["authorName"], r["emoji"]) for r in target["reactions"]], [("Member 2", "😂")])
        self.assertNotIn("reactions", tool_row)

    def test_solo_models_have_no_reaction_tool(self):
        service = self.solo([call("react_to_message", {"emoji": "👍"}), response("ok")])
        self.run_turn(service, "Hi")
        offered = {tool["name"] for tool in self.transport.requests[0]["tools"]}
        self.assertNotIn("react_to_message", offered)
        row = next(item for item in service.chat["entries"] if item.get("tool") == "react_to_message")
        self.assertTrue(row["isError"]); self.assertIn("only to Team members", row["detail"])
        self.assertNotIn("reactions", next(item for item in service.chat["entries"] if item["kind"] == "user"))

    def test_user_reacts_from_the_transcript_and_withdraws(self):
        service = self.solo([response("Hello there.")])
        self.run_turn(service, "Hi")
        reply = next(item for item in service.chat["entries"] if item["kind"] == "assistant")
        service.handle({"command": "react", "id": service.chat["id"], "entry": reply["id"], "emoji": "❤️"})
        self.assertEqual([(r["author"], r["authorName"], r["emoji"]) for r in reply["reactions"]], [("user", "You", "❤️")])
        self.assertEqual(self.store.load(service.chat["id"])["entries"][-1]["reactions"][0]["emoji"], "❤️")
        self.assertTrue(any(e.get("event") == "entry" and e["entry"]["id"] == reply["id"] and e["entry"].get("reactions") for e in self.events))
        with self.assertRaisesRegex(ValueError, "one emoji"):
            service.handle({"command": "react", "id": service.chat["id"], "entry": reply["id"], "emoji": "nice"})
        with self.assertRaisesRegex(ValueError, "Select the chat"):
            service.handle({"command": "react", "id": "other", "entry": reply["id"], "emoji": "👍"})
        service.handle({"command": "react", "id": service.chat["id"], "entry": reply["id"], "emoji": "❤️", "remove": True})
        self.assertNotIn("reactions", reply)
        self.assertNotIn("reactions", self.store.load(service.chat["id"])["entries"][-1])

    def test_failed_save_rolls_the_reaction_back(self):
        service = self.solo([response("Hello there.")])
        self.run_turn(service, "Hi")
        reply = next(item for item in service.chat["entries"] if item["kind"] == "assistant")
        service.handle({"command": "react", "id": service.chat["id"], "entry": reply["id"], "emoji": "👍"})
        before = json.dumps(reply["reactions"])
        from unittest.mock import patch
        with patch.object(service.store, "save", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                service.handle({"command": "react", "id": service.chat["id"], "entry": reply["id"], "emoji": "❤️"})
            with self.assertRaises(OSError):
                service.handle({"command": "react", "id": service.chat["id"], "entry": reply["id"], "emoji": "👍", "remove": True})
        self.assertEqual(json.dumps(reply["reactions"]), before)
        self.assertEqual(self.store.load(service.chat["id"])["entries"][-1]["reactions"][0]["emoji"], "👍")
        user = next(item for item in service.chat["entries"] if item["kind"] == "user")
        with patch.object(service.store, "save", side_effect=OSError("disk full")), self.assertRaises(OSError):
            service.handle({"command": "react", "id": service.chat["id"], "entry": user["id"], "emoji": "❤️"})
        self.assertNotIn("reactions", user)


if __name__ == "__main__":
    unittest.main()
