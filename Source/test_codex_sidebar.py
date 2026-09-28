"""Sidebar ownership and read-only metadata transport regressions."""
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from codex_accent import AccentBridge, SidebarAccents
from test_codex_accent import FakeTransport


def tid(number):
    return f"00000000-0000-4000-8000-{number:012d}"


class SidebarMetadataTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.path = self.home / "state_5.sqlite"
        self.db = sqlite3.connect(self.path)
        self.addCleanup(self.db.close)
        self.db.executescript("CREATE TABLE threads (id TEXT PRIMARY KEY, model TEXT); "
                             "CREATE TABLE thread_spawn_edges (child_thread_id TEXT PRIMARY KEY, parent_thread_id TEXT);")
        self.palette = SidebarAccents({"qwen/model": "#123abc", "kimi/model": "#0073E6"},
                                      ["GPT-6 Astra"], self.home)

    def thread(self, number, model, parent=None):
        self.db.execute("INSERT INTO threads VALUES (?, ?)", (tid(number), model))
        if parent is not None:
            self.db.execute("INSERT INTO thread_spawn_edges VALUES (?, ?)", (tid(number), tid(parent)))
        self.db.commit()

    def test_descendants_follow_root_and_refresh_after_root_model_switch(self):
        self.thread(1, "qwen/model")
        self.thread(2, "kimi/model", 1)
        self.thread(3, "gpt-6-astra", 2)
        before = self.path.read_bytes()
        self.assertEqual(self.palette([tid(1), tid(2), tid(3)]), dict.fromkeys(map(tid, [1, 2, 3]), "#123ABC"))
        self.assertEqual(self.path.read_bytes(), before)
        self.db.execute("UPDATE threads SET model='kimi/model' WHERE id=?", (tid(1),))
        self.db.commit()
        self.assertEqual(self.palette([tid(3)]), {tid(3): "#0073E6"})

    def test_native_exact_names_and_brand_override_take_precedence(self):
        self.thread(1, "gpt-6-astra")
        self.thread(2, "codex/gpt-6-astra")
        self.thread(3, "gpt-6-astra-made-up")
        self.assertEqual(self.palette(list(map(tid, [1, 2, 3]))), {tid(1): "#705AFF", tid(2): "#705AFF"})
        palette = SidebarAccents({"codex/gpt-6-astra": "#AABBCC"}, ["GPT-6 Astra"], self.home)
        self.assertEqual(palette([tid(2)]), {tid(2): "#AABBCC"})

    def test_unknown_missing_and_cyclic_parents_do_not_borrow_child_colour(self):
        self.thread(1, "unknown")
        self.thread(2, "kimi/model", 1)
        self.thread(3, "kimi/model", 99)
        self.thread(4, "kimi/model", 5)
        self.thread(5, "qwen/model", 4)
        self.thread(6, None)
        self.assertEqual(self.palette(list(map(tid, range(1, 7)))), {})

    def test_depth_and_input_are_bounded(self):
        for i in range(1, 35):
            self.thread(i, "kimi/model", i + 1 if i < 34 else None)
        self.assertEqual(self.palette([tid(1)]), {})
        self.assertEqual(self.palette([tid(34)]), {tid(34): "#0073E6"})
        self.assertEqual(self.palette([None, {}, "'; DROP TABLE threads; --", tid(34)]), {tid(34): "#0073E6"})
        self.assertEqual(self.palette([None] * 256 + [tid(34)]), {})
        self.assertEqual(self.palette({tid(34): "untrusted"}), {})

    def test_missing_locked_or_newer_unsupported_database_clears_palette(self):
        self.thread(1, "kimi/model")
        self.assertEqual(SidebarAccents({}, config_home=self.home / "absent")([tid(1)]), {})
        self.assertFalse((self.home / "absent").exists())
        self.db.execute("BEGIN EXCLUSIVE")
        try:
            self.assertEqual(self.palette([tid(1)]), {})
        finally:
            self.db.rollback()
        self.assertEqual(self.palette([tid(1)]), {tid(1): "#0073E6"})
        (self.home / "state_10.sqlite").write_bytes(b"unsupported database")
        self.assertEqual(self.palette([tid(1)]), {})


class SidebarPipeTests(unittest.TestCase):
    def setUp(self):
        self.transport = FakeTransport()
        self.requests = []
        def resolve(ids):
            self.requests.append(ids)
            return {tid(1): "#123ABC"}
        self.bridge = AccentBridge(self.transport, "SCRIPT", sidebar_accents=resolve)
        self.bridge.handle({"method": "Target.attachedToTarget", "params": {"sessionId": "app", "targetInfo": {
            "type": "page", "url": "app://-/index.html"}}})

    def reply(self, call, value):
        self.bridge.handle({"id": call["id"], "result": {"result": {"value": value}}})

    def test_two_second_poll_is_bounded_and_only_uses_attached_app_watcher(self):
        self.bridge.handle({"method": "Target.attachedToTarget", "params": {"sessionId": "web", "targetInfo": {
            "type": "page", "url": "https://example.com"}}})
        with patch("codex_accent.time.monotonic", return_value=1):
            self.bridge.refresh_sidebar()
            count = len(self.transport.sent)
            self.bridge.refresh_sidebar()
            self.assertEqual(len(self.transport.sent), count)
        read = self.transport.sent[-1]
        self.assertEqual(read["sessionId"], "app")
        self.assertIn("window !== window.top", read["params"]["expression"])
        self.assertIn("String(location.href)", read["params"]["expression"])
        self.assertIn("sidebarThreadIds", read["params"]["expression"])
        with patch("codex_accent.time.monotonic", return_value=4):
            self.bridge.refresh_sidebar()
            self.assertEqual(len(self.transport.sent), count)  # no accumulating requests
        self.reply(read, [tid(1)])
        self.assertEqual(self.requests, [[tid(1)]])
        write = self.transport.sent[-1]
        self.assertIn(json.dumps({tid(1): "#123ABC"}), write["params"]["expression"])
        self.reply(write, 1)
        with patch("codex_accent.time.monotonic", return_value=7):
            self.bridge.refresh_sidebar()
        self.assertIn("sidebarThreadIds", self.transport.sent[-1]["params"]["expression"])
        self.bridge.handle({"method": "Target.detachedFromTarget", "params": {"sessionId": "app"}})
        count = len(self.transport.sent)
        self.reply(self.transport.sent[-1], [tid(1)])
        self.assertEqual(len(self.transport.sent), count)

    def test_lost_reply_expires_and_polling_resumes(self):
        events = []
        self.bridge.emit = events.append
        with patch("codex_accent.time.monotonic", return_value=1):
            self.bridge.refresh_sidebar()
        count = len(self.transport.sent)
        with patch("codex_accent.time.monotonic", return_value=5):
            self.bridge.refresh_sidebar()
        self.assertEqual(len(self.transport.sent), count)  # still waiting
        with patch("codex_accent.time.monotonic", return_value=12):
            self.bridge.refresh_sidebar()
        self.assertEqual(len(self.transport.sent), count + 1)
        self.assertIn("sidebarThreadIds", self.transport.sent[-1]["params"]["expression"])
        self.assertEqual(events, [{"event": "error", "stage": "sidebar-read", "session": "app",
                                   "message": "no reply in 10 s; polling again"}])

    def test_write_status_is_logged_only_when_it_changes(self):
        events = []
        self.bridge.emit = events.append
        status = {"colours": 1, "rows": 2, "local": 2, "matched": 1, "spinners": 1, "rowSpinners": 1, "painted": 1,
                  "sample": [["local:" + tid(1), "local", "local"]]}
        for now in (1, 4, 7):
            with patch("codex_accent.time.monotonic", return_value=now):
                self.bridge.refresh_sidebar()
            self.reply(self.transport.sent[-1], [tid(1), tid(2)])
            self.reply(self.transport.sent[-1], dict(status, painted=0) if now == 7 else status)
        self.assertEqual(events, [{"event": "sidebar", "session": "app", "requested": 2, **status},
                                  {"event": "sidebar", "session": "app", "requested": 2, **dict(status, painted=0)}])

    def test_failed_metadata_read_clears_previous_colours_and_keeps_bridge_alive(self):
        def unavailable(ids):
            raise RuntimeError("database unavailable")
        self.bridge.sidebar_accents = unavailable
        self.bridge.refresh_sidebar()
        self.reply(self.transport.sent[-1], [tid(1)])
        self.assertIn("setSidebarAccents?.({})", self.transport.sent[-1]["params"]["expression"])
        self.assertEqual(self.bridge.injected, {"app"})


if __name__ == "__main__":
    unittest.main()
