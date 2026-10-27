"""Exercise real cancellation/ownership and capability revocation boundaries."""
from pathlib import Path
import json
import tempfile
import threading
from types import SimpleNamespace
import unittest

from chat_capabilities import ChatCapabilities, NativeActions


class CapabilityTests(unittest.TestCase):
    def test_invalid_optional_settings_preserve_file_and_leave_browser_available(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "plugins.json"
            path.write_text("not valid JSON")
            capability = ChatCapabilities(Path(directory), lambda event: None)
            self.assertIn("needs setup", capability.status()["notice"])
            self.assertEqual(path.read_text(), "not valid JSON")
            capability.handle({"command": "browser_enable", "enabled": True})
            self.assertTrue(capability.definitions(SimpleNamespace(role="parent")))
            self.assertEqual(path.read_text(), "not valid JSON")
            capability.close()

    def test_browser_tool_roundtrip_uses_exact_native_request_and_arguments(self):
        with tempfile.TemporaryDirectory() as directory:
            result = {"content": [{"type": "text", "text": "loaded"}], "is_error": False,
                      "summary": "Open browser", "changed_files": []}
            seen = []
            def emit(event):
                seen.append(event)
                if event["event"] == "native_action":
                    capability.handle({"command": "native_result", "id": event["id"],
                                       "chat": event["chat"], "result": result})
            capability = ChatCapabilities(Path(directory), emit)
            capability.handle({"command": "browser_enable", "enabled": True})
            service = SimpleNamespace(role="parent", cancel=threading.Event(), chat={"id": "A"})
            returned = capability.execute(service, "browser_open", {"url": "http://localhost:3000"})
            self.assertEqual(returned, result)
            action = next(e for e in seen if e["event"] == "native_action")
            self.assertEqual(action["arguments"], {"url": "http://localhost:3000"})
            self.assertEqual(action["chat"], "A")
            self.assertTrue(action["id"])
            self.assertFalse(capability.native._pending)
            capability.close()

    def test_browser_followups_remain_bound_to_each_service_tab(self):
        with tempfile.TemporaryDirectory() as directory:
            seen = []
            def emit(event):
                if event["event"] != "native_action": return
                seen.append(event)
                arguments = event["arguments"]
                tab = "tab-a" if arguments.get("url", "").endswith("/a") else "tab-b"
                if event["tool"] != "browser_open": tab = arguments.get("tab_id", "")
                result = {"content": [{"type": "text", "text": json.dumps({"tab_id": tab})}],
                          "is_error": False, "summary": "Browser", "changed_files": []}
                capability.handle({"command": "native_result", "id": event["id"],
                                   "chat": event["chat"], "result": result})
            capability = ChatCapabilities(Path(directory), emit)
            capability.handle({"command": "browser_enable", "enabled": True})
            parent = SimpleNamespace(role="parent", cancel=threading.Event(), chat={"id": "root"})
            one = SimpleNamespace(role="team", team_parent=parent, team_member={"id": "one"}, cancel=threading.Event())
            two = SimpleNamespace(role="team", team_parent=parent, team_member={"id": "two"}, cancel=threading.Event())
            with self.assertRaisesRegex(ValueError, "Specify tab_id"):
                capability.describe(one, "browser_navigate", {"url": "https://example.com/next"})
            with self.assertRaisesRegex(ValueError, "Specify tab_id"):
                capability.describe(one, "browser_close", {})
            capability.execute(one, "browser_open", {"url": "https://example.com/a"})
            capability.execute(two, "browser_open", {"url": "https://example.com/b"})
            capability.execute(one, "browser_observe", {})
            self.assertEqual(seen[-1]["arguments"]["tab_id"], "tab-a")
            capability.execute(two, "browser_observe", {})
            self.assertEqual(seen[-1]["arguments"]["tab_id"], "tab-b")
            resumed = SimpleNamespace(role="team", team_parent=parent, team_member={"id": "one"}, cancel=threading.Event())
            capability.execute(resumed, "browser_observe", {})
            self.assertEqual(seen[-1]["arguments"]["tab_id"], "tab-a", "a member retains its tab across contributions")
            # The target presented for approval remains fixed if the service's
            # binding changes before execution.
            prepared = capability.describe(one, "browser_navigate", {"url": "https://example.com/next"})
            self.assertIn('"tab_id": "tab-a"', prepared["detail"])
            capability._browser_binding(one, parent, "tab-b")
            capability.execute(one, "browser_navigate", prepared["_browser_arguments"])
            self.assertEqual(seen[-1]["arguments"]["tab_id"], "tab-a")
            capability.execute(one, "browser_observe", {"tab_id": "explicit"})
            self.assertEqual(seen[-1]["arguments"]["tab_id"], "explicit")
            capability.execute(one, "browser_close", {})
            self.assertEqual(seen[-1]["arguments"]["tab_id"], "explicit")
            capability.execute(one, "browser_observe", {})
            self.assertEqual(seen[-1]["arguments"]["tab_id"], "explicit", "a closed tab must not fall back to another tab")
            capability.close()

    def test_revoked_generation_cannot_dispatch_after_reenable(self):
        events = []
        bridge = NativeActions(events.append)
        generation = bridge.generation()
        bridge.set_enabled(False)
        bridge.set_enabled(True)
        with self.assertRaises(InterruptedError):
            bridge.call("A", "browser_open", {"args": {}}, None, generation=generation)
        self.assertEqual(events, [])
        self.assertFalse(bridge._pending)

    def test_native_reply_requires_matching_chat_and_ignores_late_replies(self):
        events, returned = [], []
        emitted = threading.Event()
        bridge = NativeActions(lambda e: (events.append(e), emitted.set()), timeout=2)
        cancel = threading.Event()
        thread = threading.Thread(target=lambda: returned.append(bridge.call("A", "browser_list", {"args": {}}, cancel)))
        thread.start()
        self.assertTrue(emitted.wait(1))
        request = events[0]
        result = {"content": [{"type": "text", "text": "ok"}], "is_error": False, "summary": "List", "changed_files": []}
        with self.assertRaises(ValueError): bridge.respond({"id": request["id"], "chat": "B", "result": result})
        bridge.respond({"id": request["id"], "chat": "A", "result": result})
        thread.join(1)
        self.assertEqual(returned, [result])
        bridge.respond({"id": request["id"], "chat": "A", "result": result})
        self.assertEqual(len(returned), 1)

    def test_cancel_unblocks_and_notifies_native_surface(self):
        events, errors = [], []
        emitted = threading.Event()
        bridge = NativeActions(lambda e: (events.append(e), emitted.set()), timeout=2)
        cancel = threading.Event()
        def call():
            try: bridge.call("A", "browser_open", {"args": {"url": "https://example.com"}}, cancel)
            except InterruptedError as exc: errors.append(str(exc))
        thread = threading.Thread(target=call); thread.start()
        self.assertTrue(emitted.wait(1)); cancel.set(); thread.join(1)
        self.assertFalse(thread.is_alive())
        self.assertEqual(events[-1]["event"], "native_cancel")
        self.assertEqual(len(errors), 1)
        self.assertFalse(bridge._pending)

    def test_disable_rechecks_catalog_and_pending_approval_and_persists(self):
        with tempfile.TemporaryDirectory() as directory:
            events = []
            capability = ChatCapabilities(Path(directory), events.append)
            service = SimpleNamespace(role="parent", cancel=threading.Event(), chat={"id": "A"})
            self.assertEqual(capability.definitions(service), [])
            capability.handle({"command": "browser_enable", "enabled": True})
            self.assertIn("browser_open", [d["name"] for d in capability.definitions(service)])
            description = capability.describe(service, "browser_open", {"url": "https://example.com"})
            self.assertTrue(description["requires_approval"])
            self.assertIn("https://example.com", description["detail"])
            capability.handle({"command": "browser_enable", "enabled": False})
            with self.assertRaisesRegex(ValueError, "disabled"):
                capability.execute(service, "browser_open", {"url": "https://example.com"})
            self.assertFalse(any(e["event"] == "native_action" for e in events))
            self.assertFalse(ChatCapabilities(Path(directory), events.append).status()["browserUse"])
            capability.handle({"command": "browser_enable", "enabled": True})
            self.assertEqual(capability.definitions(SimpleNamespace(role="side")), [])
            capability.close()


if __name__ == "__main__": unittest.main()
