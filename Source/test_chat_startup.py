"""Startup must not decode the archive or replay interrupted actions."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from chat_runtime import ChatService, ChatStore, entry
from chat_sessions import ChatHost
from test_chat_runtime import FakeTransport, model


class ChatStartupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = ChatStore(self.root)
        self.addCleanup(self.store._lock.close)
        self.events = []

    def save(self, identifier, updated=1, status="ready"):
        choice = model()
        chat = {"id": identifier * 32, "title": identifier, "updated": updated,
                "route": choice["route"], "account": "", "scope": choice["scope"],
                "workspace": str(self.root), "effort": "", "approvalMode": "manual",
                "status": status, "entries": [], "messages": []}
        self.store.save(chat)
        return chat

    def host(self, transport=None):
        host = ChatHost(self.store, transport or FakeTransport([]), self.events.append,
                        child_factory=lambda: FakeTransport([]))
        self.addCleanup(host.shutdown)
        return host

    def test_startup_loads_only_latest_once_and_defers_other_recovery(self):
        interrupted = self.save("a", status="working")
        interrupted["entries"] = [entry("tool", tool="run_shell", detail="Running…")]
        interrupted["messages"] = [{"role": "assistant", "content": [
            {"type": "tool_use", "id": "never-run", "name": "run_shell",
             "input": {"command": "touch never"}}]}]
        self.store.save(interrupted)
        before = self.store.path(interrupted["id"]).read_bytes()
        latest = self.save("b", updated=2)
        host = self.host()
        with mock.patch.object(self.store, "load", wraps=self.store.load) as load:
            host.initialize()
            self.assertEqual(load.call_args_list, [mock.call(latest["id"])])
        self.assertEqual(self.store.path(interrupted["id"]).read_bytes(), before)
        recovered = host.session(interrupted["id"]).chat
        self.assertEqual(recovered["status"], "interrupted")
        self.assertTrue(recovered["entries"][0]["isError"])
        self.assertTrue(recovered["messages"][-1]["content"][0]["is_error"])
        self.assertFalse((self.root / "never").exists())
        after = self.store.path(interrupted["id"]).read_bytes()
        host.sessions.pop(interrupted["id"])
        host.session(interrupted["id"])
        self.assertEqual(self.store.path(interrupted["id"]).read_bytes(), after)

    def test_large_latest_chat_lists_without_loading_any_transcript(self):
        self.save("a")
        latest = self.save("b", updated=2)
        path = self.store.path(latest["id"])
        with path.open("ab") as stream:
            stream.truncate(17 * 1024 * 1024)
        before = path.stat().st_mtime_ns
        host = self.host()
        with mock.patch.object(self.store, "load", side_effect=AssertionError("decoded archive")):
            host.initialize()
        self.assertIsNone(host.view_id)
        self.assertEqual(len(host.headers), 2)
        self.assertTrue(any(e["event"] == "ready" for e in self.events))
        self.assertTrue(any("last chat is large" in e.get("message", "") for e in self.events))
        self.assertEqual(path.stat().st_mtime_ns, before)

    def test_corrupt_latest_transcript_cannot_hide_other_chats(self):
        self.save("a")
        latest = self.save("b", updated=2)
        path = self.store.path(latest["id"])
        with path.open("a") as stream:
            stream.write("{broken transcript\n")
        before = path.read_bytes()
        host = self.host()
        host.initialize()
        self.assertTrue(any(e["event"] == "ready" for e in self.events))
        self.assertTrue(any(len(e.get("chats", [])) == 2 for e in self.events))
        self.assertTrue(any("Could not open the last chat" in e.get("message", "") for e in self.events))
        self.assertEqual(path.read_bytes(), before)

    def test_standalone_select_recovers_a_deferred_interrupted_chat(self):
        older = self.save("a", status="working")
        self.save("b", updated=2)
        service = ChatService(self.store, FakeTransport([]), self.events.append)
        service.initialize()
        service.handle({"command": "select", "id": older["id"]})
        self.assertEqual(service.chat["status"], "interrupted")
        self.assertEqual(service.chat["id"], older["id"])


if __name__ == "__main__":
    unittest.main()
