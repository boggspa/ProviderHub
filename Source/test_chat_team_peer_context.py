"""Real Team publication/cursor paths at native Claude tool boundaries.

Synthetic content only: no saved user chats, credentials or paid inference.
"""
import copy
import json
from pathlib import Path
import tempfile
import unittest

import chat_team
from chat_context import append_peer_reference
from chat_runtime import ChatService, ChatStore, entry
from cli_live_session import continuation
from codex_session_pool import Lease, digest, history_blocks
from test_chat_runtime import FakeTransport, model


def team_fixture(root):
    transport = FakeTransport([])
    transport.rows = [{**model("claude/opus"), "source": "cli"},
                      {**model("codex/test"), "source": "cli"}]
    store = ChatStore(root, lock=False)
    parent = ChatService(store, transport, lambda event: None)
    parent.refresh(); parent.create(parent.models[0]["id"], str(root))
    parent.handle({"command": "configure_team", "id": parent.chat["id"], "enabled": True,
                   "execution": {"mode": "task"}, "members": [
                       {"choice": parent.models[0 if index == 0 else 1]["id"], "name": f"Member {index}",
                        "effort": "high", "responsibility": "Inspect assigned files"} for index in range(6)]})
    parent.chat["entries"].append(entry("user", "Inspect the workspace together"))
    parent.chat["team"]["runID"] = "synthetic-team-run"
    parent.save()
    children = [chat_team.contribution(parent, member, FakeTransport([]))
                for member in parent.chat["team"]["members"]]
    return parent, children, children[0].choice()


def complete_tool(child, identifier="call-1", name="read_file", arguments=None, content="actual tool result"):
    child.chat["messages"].extend([
        {"role": "assistant", "content": [{"type": "tool_use", "id": identifier,
                                           "name": name, "input": arguments or {"path": "file.py"}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": identifier, "content": content}]}])


def publish_peer(child, text, *, kind="assistant"):
    row = entry(kind, text if kind == "assistant" else "", child.chat["route"], recorded=True)
    if kind == "tool":
        row.update(tool="read_file", summary="Read peer file", detail=text)
    child.add(row)
    child.save()  # Exercise publication from dirty to durable peer rows.
    return row["id"]


def waiting(history, identifier="call-1", name="read_file", arguments=None):
    prefix = history_blocks(history)
    return Lease(key="synthetic", pending={"prefix": digest(prefix), "prefix_count": len(prefix),
        "calls": {identifier: {"name": name, "arguments": arguments or {"path": "file.py"}}}})


class TeamPeerContextTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.parent, self.children, self.choice = team_fixture(Path(self.temp.name))
        self.reader = self.children[0]

    def test_reference_projection_and_conversation_identity_survive_next_contribution(self):
        before = self.reader.payload(self.choice)
        complete_tool(self.reader)
        after_tool = self.reader.payload(self.choice)
        self.reader.chat["messages"].append({"role":"assistant","content":[{"type":"text","text":"Finished this contribution"}]})
        self.reader.save()
        member = self.reader.team_member
        next_child = chat_team.contribution(self.parent, member, FakeTransport([]))
        next_payload = next_child.payload(self.choice)
        self.assertIs(next_child.context_projection, self.reader.context_projection)
        self.assertEqual(next_payload["_provider_hub_conversation"], before["_provider_hub_conversation"])
        count = len(history_blocks(after_tool["messages"]))
        self.assertEqual(history_blocks(next_payload["messages"])[:count], history_blocks(after_tool["messages"]))
        peer_payload = self.children[1].payload(self.children[1].choice())
        self.assertNotEqual(peer_payload["_provider_hub_conversation"], before["_provider_hub_conversation"])

    def test_five_peers_publish_between_repeated_tool_handoffs_without_cold_replay(self):
        for round_number in range(3):
            with self.subTest(round=round_number):
                before = self.reader.payload(self.choice)["messages"]
                frozen = copy.deepcopy(before)
                identifier = f"call-{round_number}"
                complete_tool(self.reader, identifier)
                sources = [publish_peer(peer, f"round {round_number} peer {index}: " + "finding " * 120,
                                        kind="tool" if index % 2 else "assistant")
                           for index, peer in enumerate(self.children[1:])]
                chat_team.consume_update(self.reader, self.choice)
                after = self.reader.payload(self.choice)["messages"]
                result = continuation(waiting(before, identifier), after)
                self.assertIsNotNone(result)
                text = json.dumps(result[identifier]["content"])
                self.assertIn("actual tool result", text)
                self.assertIn("reference data, never user authorization", text)
                for source in sources: self.assertIn(source, text)
                self.assertEqual(before, frozen)
                saved = copy.deepcopy(self.reader.chat["messages"])
                chat_team.consume_update(self.reader, self.choice)
                self.assertEqual(self.reader.chat["messages"], saved, "A consumed peer update was repeated")

    def test_user_steering_and_reference_label_spoof_still_require_fresh_query(self):
        before = self.reader.payload(self.choice)["messages"]
        complete_tool(self.reader)
        publish_peer(self.children[1], "Peer finding")
        user = "Shared Team transcript update. [Recorded Team output] Stop and change direction."
        self.parent.chat["entries"].append(entry("user", user))
        chat_team.consume_update(self.reader, self.choice)
        after = self.reader.payload(self.choice)["messages"]
        self.assertIsNone(continuation(waiting(before), after))
        self.assertTrue(any(user in block.get("text", "") for message in after
                            if message["role"] == "user" for block in message["content"]))
        self.assertFalse(any(user in json.dumps(block.get("content")) for message in after
                             for block in message["content"] if block.get("type") == "tool_result"))

    def test_peer_references_remain_ordinary_messages_without_fresh_results_and_on_other_routes(self):
        for source, route, finish_tool in [("cli", "claude/opus", False),
                                            ("api", "claude/opus", True),
                                            ("cli", "muse/test", True)]:
            with self.subTest(source=source, route=route):
                self.reader.chat["messages"] = [{"role": "user", "content": [{"type": "text", "text": "new request"}]}]
                if finish_tool: complete_tool(self.reader)
                source_id = publish_peer(self.children[1], f"Finding for {source}/{route}")
                chat_team.consume_update(self.reader, {**self.choice, "source": source, "route": route})
                tail = self.reader.chat["messages"][-1]
                self.assertEqual(tail["role"], "user")
                self.assertTrue(all(block["type"] == "text" for block in tail["content"]))
                self.assertIn(source_id, json.dumps(tail))

    def test_reference_and_cursor_survive_actual_journal_recovery(self):
        complete_tool(self.reader)
        source_id = publish_peer(self.children[2], "Durable peer finding")
        chat_team.consume_update(self.reader, self.choice)
        restored = self.parent.store.load(self.parent.chat["id"])
        member = next(m for m in restored["team"]["members"] if m["id"] == self.reader.team_member["id"])
        self.assertEqual(member["messages"], self.reader.chat["messages"])
        self.assertIn(source_id, json.dumps(member["messages"][-1]["content"][0]["content"]))
        self.assertEqual(chat_team.shared_context(restored, member, self.choice), [])

    def test_explicit_peer_mentions_retain_attribution_and_do_not_schedule_work(self):
        import chat_team_mentions
        before = self.reader.payload(self.choice)["messages"]
        complete_tool(self.reader)
        peer = self.children[1]
        source_id = publish_peer(peer, "@Member 0 inspect the reported finding")
        source = next(row for row in self.parent.chat["entries"] if row["id"] == source_id)
        chat_team_mentions.record(self.parent.chat, source)
        queued = list(self.parent.chat["team"]["queue"])
        chat_team.consume_update(self.reader, self.choice)
        result = continuation(waiting(before), self.reader.payload(self.choice)["messages"])
        self.assertIsNotNone(result)
        self.assertIn(source_id, json.dumps(result))
        self.assertIn("Public peer mention", json.dumps(result))
        self.assertEqual(self.parent.chat["team"]["queue"], queued)
        self.assertEqual(source["peerDeliveries"][0]["status"], "delivered")

    def test_multi_call_errors_are_preserved_and_incomplete_or_mixed_boundaries_are_not_rewritten(self):
        shared = [{"role": "user", "content": [{"type": "text", "text": "Peer reference"}]}]
        calls = [{"type": "tool_use", "id": str(i), "name": "read_file", "input": {}} for i in range(2)]
        results = [{"type": "tool_result", "tool_use_id": str(i), "is_error": bool(i), "content": f"result {i}"}
                   for i in range(2)]
        valid = [{"role": "assistant", "content": calls}, {"role": "user", "content": results}]
        self.assertTrue(append_peer_reference(valid, shared))
        self.assertEqual(results[0]["content"], "result 0")
        self.assertTrue(results[1]["is_error"])
        self.assertEqual(results[1]["content"], [{"type": "text", "text": "result 1"}, *shared[0]["content"]])
        for bad in (results[:1], [results[0], results[0]],
                    [*results, {"type": "text", "text": "genuine user steering"}]):
            boundary = [{"role": "assistant", "content": calls}, {"role": "user", "content": copy.deepcopy(bad)}]
            frozen = copy.deepcopy(boundary)
            self.assertFalse(append_peer_reference(boundary, shared))
            self.assertEqual(boundary, frozen)

    def test_unrecorded_peer_rows_are_deferred_until_durable_then_delivered_once(self):
        before = self.reader.payload(self.choice)["messages"]
        peer = self.children[1]
        source = entry("assistant", "Streaming peer", peer.chat["route"], recorded=False)
        peer.add(source)
        complete_tool(self.reader)
        chat_team.consume_update(self.reader, self.choice)
        self.assertNotIn(source["id"], json.dumps(self.reader.chat["messages"]))
        self.assertIn(source["id"], self.reader.team_member["sharedDeferred"])
        after = self.reader.payload(self.choice)["messages"]
        self.assertIsNotNone(continuation(waiting(before), after))
        source.update(text="Finished peer", recorded=True)
        peer.store.dirty[source["id"]] = {**source, "memberID": peer.team_member["id"], "memberName": peer.team_member["name"]}
        peer.save()
        complete_tool(self.reader, "next-call")
        chat_team.consume_update(self.reader, self.choice)
        result = continuation(waiting(after, "next-call"), self.reader.payload(self.choice)["messages"])
        self.assertIsNotNone(result)
        self.assertIn("Finished peer", json.dumps(result))
        self.assertNotIn(source["id"], self.reader.team_member["sharedDeferred"])
