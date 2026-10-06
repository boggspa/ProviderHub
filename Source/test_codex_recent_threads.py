"""Observed identity, assistant-only item shapes, bounds and file access."""
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from codex_recent_threads import RecentThreadPreviews, TAIL_BYTES


def tid(n):
    return f"00000000-0000-7000-8000-{n:012d}"


def target(n=1, **updates):
    return {"threadId": tid(n), "hostId": "local", "kind": "local", **updates}


def assistant(text):
    return {"type": "response_item", "payload": {"type": "message", "role": "assistant",
            "content": [{"type": "output_text", "text": text}]}}


class RecentPreviewTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.home = Path(self.folder.name)
        self.sessions = self.home / "sessions"
        self.sessions.mkdir()
        self.database = self.home / "state_5.sqlite"
        with sqlite3.connect(self.database) as connection:
            connection.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT)")
        self.reader = RecentThreadPreviews(self.home)

    def rollout(self, n, records):
        path = self.sessions / f"rollout-{n}.jsonl"
        path.write_text("".join(json.dumps(record) + "\n" for record in records))
        with sqlite3.connect(self.database) as connection:
            connection.execute("INSERT OR REPLACE INTO threads VALUES (?,?)", (tid(n), str(path)))
        return path

    def preview(self, n=1):
        return self.reader([target(n)])[0]["preview"]

    def test_latest_assistant_only_normalized_ordered_and_read_only(self):
        self.rollout(1, [assistant("older"), assistant("latest\nresponse"),
                         {"type": "event_msg", "payload": {"type": "user_message", "message": "not a preview"}},
                         {"type": "response_item", "payload": {"type": "reasoning", "text": "private"}},
                         {"type": "response_item", "payload": {"type": "function_call_output", "output": "tool"}}])
        self.rollout(2, [{"type": "event_msg", "payload": {"type": "agent_message", "message": "second"}}])
        before = self.database.read_bytes()
        self.assertEqual([r["preview"] for r in self.reader([target(2), target(1)])], ["second", "latest response"])
        self.assertEqual(self.database.read_bytes(), before)

    def test_modern_completed_agent_message_and_partial_record(self):
        path = self.rollout(1, [assistant("fallback"), {"type": "event_msg", "payload": {
            "type": "item_completed", "item": {"type": "AgentMessage", "text": "completed"}}}])
        with path.open("a") as stream:
            stream.write(json.dumps(assistant("incomplete")))
        self.assertEqual(self.preview(), "completed")

    def test_cache_refreshes_append_replace_and_remote_identity_cannot_read_local(self):
        path = self.rollout(1, [assistant("old")])
        self.assertEqual(self.preview(), "old")
        with path.open("a") as stream:
            stream.write(json.dumps(assistant("new")) + "\n")
        self.assertEqual(self.preview(), "new")
        replacement = path.with_suffix(".replacement")
        replacement.write_text(json.dumps(assistant("replacement")) + "\n")
        os.replace(replacement, path)
        self.assertEqual(self.preview(), "replacement")
        self.assertEqual([r["preview"] for r in self.reader([target(1, hostId="remote"), target(1, kind="chatgpt")])], [None, None])

    def test_uuid_v7_bounded_targets_and_malformed_inputs(self):
        self.rollout(1, [assistant("value")])
        self.assertEqual(len(self.reader([target()] * 15)), 1)
        self.assertEqual(self.reader([None] * 10 + [target()]), [])
        self.assertEqual(self.reader({}), [])
        self.assertEqual(self.reader([{}, {"threadId": []}, target(1, threadId="bad")]), [])
        self.assertEqual(self.preview(), "value")

    def test_newer_unsupported_or_missing_database_never_uses_stale_schema(self):
        self.rollout(1, [assistant("value")])
        (self.home / "state_10.sqlite").write_bytes(b"unsupported")
        self.assertIsNone(self.preview())
        self.assertIsNone(RecentThreadPreviews(self.home / "missing")([target()])[0]["preview"])
        self.assertFalse((self.home / "missing").exists())

    def test_locked_database_does_not_hold_up_worker(self):
        self.rollout(1, [assistant("value")])
        with sqlite3.connect(self.database) as connection:
            connection.execute("BEGIN EXCLUSIVE")
            self.assertIsNone(self.preview())
            connection.rollback()
        self.assertEqual(self.preview(), "value")

    def test_path_escape_symlinks_and_special_files_are_rejected(self):
        self.rollout(1, [assistant("value")])
        outside = self.home / "outside.jsonl"
        outside.write_text(json.dumps(assistant("outside")) + "\n")
        bad_paths = [str(outside), str(self.sessions / ".." / "outside.jsonl")]
        link = self.sessions / "link.jsonl"
        link.symlink_to(outside)
        bad_paths.append(str(link))
        directory = self.sessions / "linked-directory"
        directory.symlink_to(self.home, target_is_directory=True)
        bad_paths.append(str(directory / "outside.jsonl"))
        fifo = self.sessions / "fifo.jsonl"
        os.mkfifo(fifo)
        bad_paths.append(str(fifo))
        for bad in bad_paths:
            with self.subTest(path=bad), sqlite3.connect(self.database) as connection:
                connection.execute("UPDATE threads SET rollout_path=? WHERE id=?", (bad, tid(1)))
                connection.commit()
                self.assertIsNone(self.preview())

    def test_tail_bounded_and_truncated_message_never_used(self):
        path = self.rollout(1, [assistant("too-old"), {"type": "event_msg", "payload": {
            "type": "token_count", "padding": "x" * (TAIL_BYTES * 2)}}, assistant("tail response")])
        self.assertEqual(self.preview(), "tail response")
        path.write_text(json.dumps(assistant("x" * (TAIL_BYTES * 2))) + "\n")
        self.assertIsNone(self.preview())

    def test_default_constructor_honours_configured_home(self):
        from unittest.mock import patch
        with patch.dict(os.environ, {"CODEX_HOME": str(self.home)}):
            self.assertEqual(RecentThreadPreviews().home, self.home)

    def test_preview_length_and_unknown_shapes(self):
        self.rollout(1, [assistant("a" * 2048)])
        self.assertEqual(len(self.preview()), 512)
        self.rollout(2, [{"content": "user text"}, {"response_item": {"role": "assistant", "text": "wrong shape"}}])
        self.assertIsNone(self.preview(2))
