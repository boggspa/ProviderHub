"""Bounded automatic context, explicit recall and cross-CLI continuations."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import chat_execution
import chat_memory
import chat_team
import chat_team_mentions
from chat_context import native_context
from chat_runtime import entry
from codex_session_pool import Lease, digest, history_blocks
from cli_live_session import continuation
import codex_cli_agent
from protocol import estimated_tokens, reported_input_tokens
from test_chat_team_peer_context import team_fixture, complete_tool, publish_peer


class TeamContextPolicyTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.parent, self.children, self.choice = team_fixture(Path(temp.name))
        self.reader = self.children[0]

    def test_verbose_peers_produce_small_previews_and_full_output_is_explicitly_recoverable(self):
        full = "Finding: " + "unicode 🧪 " * 800 + " EXACT_TAIL"
        sources = []
        for peer in self.children[1:]:
            for index in range(10):
                publish_peer(peer, f"Old result {index}: " + "x" * 12000)
            sources.append(publish_peer(peer, full))
            publish_peer(peer, "SECRET_RAW_TOOL_OUTPUT" * 2000, kind="tool")
        projected = chat_team.shared_context(self.parent.chat, self.reader.team_member, self.choice)
        wire = json.dumps(projected, ensure_ascii=False)
        self.assertLess(len(wire.encode()), 7500)
        self.assertNotIn("SECRET_RAW_TOOL_OUTPUT", wire)
        self.assertNotIn("EXACT_TAIL", wire)
        self.assertNotIn("Old result", wire)
        for source in sources:
            self.assertIn(source, wire)
            recovered, offset = "", 0
            while offset is not None:
                result = chat_memory.execute(self.parent.chat, "read_history", {"entry_id": source, "limit": 8000, "offset": offset})
                page = json.loads(result["content"][0]["text"])
                recovered += page["text"]; offset = page["next_offset"]
            self.assertEqual(recovered, full)
        self.assertEqual(chat_team.shared_context(self.parent.chat, self.reader.team_member, self.choice), [])
        self.assertEqual(self.reader.team_member["sharedDeferred"], [])

    def test_full_user_correction_and_explicit_peer_mention_bypass_routine_preview_limit(self):
        user = "Correction: " + "preserve all of these requirements " * 400 + " USER_TAIL"
        self.parent.chat["entries"].append(entry("user", user))
        peer = self.children[1]
        text = "@Member 0 " + "specific relevant finding " * 70 + " MENTION_TAIL"
        source_id = publish_peer(peer, text)
        source = next(row for row in self.parent.chat["entries"] if row["id"] == source_id)
        chat_team_mentions.record(self.parent.chat, source)
        with patch.object(chat_team, "MAX_SHARED_BYTES", 1):
            projected, reference_only = chat_team._shared_context_update(self.parent.chat, self.reader.team_member, self.choice)
        wire = json.dumps(projected)
        self.assertIn(user, wire)
        self.assertIn("MENTION_TAIL", wire)
        self.assertIn("Reference only: no user instruction", wire)
        self.assertFalse(reference_only)
        self.assertEqual(source["peerDeliveries"][0]["status"], "delivered")

    def test_changed_references_and_five_peer_publishers_preserve_each_stateful_cli(self):
        for provider in ("claude", "codex", "grok"):
            with self.subTest(provider=provider):
                choice = {**self.choice, "source": "cli", "route": provider + "/test"}
                self.reader.chat.update(route=choice["route"], messages=[{"role": "user", "content": "Original task"}])
                memory = {"role": "user", "content": [{"type": "text", "text": "Original notebook"}]}
                with patch.object(chat_memory, "memory_message", return_value=memory):
                    first = self.reader.payload(choice)["messages"]
                call = {"id": "call-1", "name": "read_file", "input": {"path": "file.py"}}
                prefix = history_blocks(first)
                lease = Lease(key="fixture", pending={"prefix": digest(prefix), "prefix_count": len(prefix),
                    "call": call, "calls": {call["id"]: {"name": call["name"], "arguments": call["input"]}}})
                complete_tool(self.reader)
                for peer in self.children[1:]: publish_peer(peer, "Updated peer evidence")
                chat_team.consume_update(self.reader, choice)
                memory["content"][0]["text"] = "Updated notebook"
                with patch.object(chat_memory, "memory_message", return_value=memory):
                    after = self.reader.payload(choice)["messages"]
                matched = (codex_cli_agent._continuation(lease, {"history": after}) if provider == "codex"
                           else continuation(lease, after, images=provider != "grok"))
                self.assertIsNotNone(matched)
                self.assertIn("Updated notebook", str(matched))
                self.assertIn("Updated peer evidence", str(matched))
                self.assertNotIn("Updated notebook", str(self.reader.chat["messages"]))
        self.assertFalse(native_context({"source": "api", "route": "codex/test"}))
        self.assertFalse(native_context({"source": "cli", "route": "muse/test"}))

    def test_compaction_leaves_room_for_subsequent_tool_rounds(self):
        reader = self.children[1]
        choice = {**reader.choice(), "context": 64000, "max_output": 4096}
        self.parent.chat["team"]["execution"]["contextTokens"] = 64000
        reader.chat["messages"] = [{"role": "user", "content": "Pinned user requirement"}]
        for i in range(80): complete_tool(reader, str(i), content="tool result " * 500)
        before = reader.payload(choice)
        self.assertLess(estimated_tokens(before), 44000)
        self.assertIn("Pinned user requirement", str(before))
        prefix = history_blocks(before["messages"])
        for i in range(6):
            complete_tool(reader, "new-" + str(i), content="brief result " * 20)
            next_payload = reader.payload(choice)
            blocks = history_blocks(next_payload["messages"])
            self.assertEqual(digest(blocks[:len(prefix)]), digest(prefix), "Trimmed the prefix again without filling headroom")
            self.assertLess(estimated_tokens(next_payload), 50000)
        uses = {b["id"] for m in next_payload["messages"] for b in m["content"] if b.get("type") == "tool_use"}
        results = {b["tool_use_id"] for m in next_payload["messages"] for b in m["content"] if b.get("type") == "tool_result"}
        self.assertEqual(uses, results)

    def test_estimated_cli_usage_cannot_be_treated_as_measured_team_consumption(self):
        counts = {"input_tokens": 425000, "output_tokens": 0, "provider_hub_usage_source": "estimate"}
        chat_execution.begin(self.parent.chat["team"])
        chat_execution.record(self.reader, counts)
        run = self.parent.chat["team"]["runUsage"]
        self.assertEqual(run["tokens"], 0)
        self.assertEqual(run["estimatedInputTokens"], 425000)
        self.assertFalse(run["usageComplete"])
        self.assertIsNone(reported_input_tokens(counts))
        self.parent.chat["team"]["execution"]["tokens"] = 1000000
        self.assertIn("unavailable", chat_execution.limit(self.parent.chat["team"]))

    def test_streamed_native_usage_replaces_the_initial_estimate_label(self):
        from test_chat_runtime import MessagesStreamTests
        fixture = MessagesStreamTests()
        for measured in (True, False):
            with self.subTest(measured=measured):
                events = fixture.events()
                events[0]["message"]["usage"] = {"input_tokens": 425000, "output_tokens": 0,
                                                    "provider_hub_usage_source": "estimate"}
                if measured:
                    events[-2]["usage"] = {"input_tokens": 12, "output_tokens": 5, "cache_read_input_tokens": 100}
                else:
                    events[-2]["usage"] = dict(events[0]["message"]["usage"])
                received = fixture.stream(events)["usage"]
                self.assertEqual(received.get("provider_hub_usage_source"), None if measured else "estimate")
                self.assertEqual(reported_input_tokens(received), 112 if measured else None)
