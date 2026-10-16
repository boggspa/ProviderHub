"""Real host coverage for remote creation, cursor paging, uploads and board."""
import base64
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from chat_remote import MacBridge, PhoneClient, exchange, project_event
from chat_remote_controls import command
from chat_remote_files import RemoteFiles, CHUNK, MAX_FILE
from chat_runtime import ChatStore, entry
from chat_sessions import ChatHost
from test_chat_runtime import FakeTransport, response


class RemoteParityTests(unittest.TestCase):
    def test_activity_tracks_current_contribution_and_clears_when_idle(self):
        from chat_remote_actions import activity
        service = self.host.sessions[self.desktop]
        service._working = True
        service.chat["team"] = {"enabled": True, "members": [
            {"id": "m1", "name": "Sol", "route": "codex/sol", "status": "working", "contributionID": "current"},
            {"id": "m2", "name": "Flash", "route": "minimax/flash", "status": "working", "contributionID": "new"},
            {"id": "m3", "name": "Luna", "route": "codex/luna", "status": "waiting", "waitReason": "Process p1"}]}
        service.chat["entries"] = [
            entry("tool", "", "codex/sol", id="tool", memberID="m1", contributionID="current", tool="run_shell", summary="Run in /workspace:\nswift build", detail="Running…"),
            entry("tool", "", "minimax/flash", id="old", memberID="m2", contributionID="prior", tool="run_shell", summary="Outdated command", detail="Running…")]
        rows = activity(service)
        self.assertEqual([row["name"] for row in rows], ["Sol", "Flash", "Luna"])
        self.assertTrue(rows[0]["runningTool"])
        self.assertEqual(rows[0]["entryID"], "tool")
        self.assertEqual(rows[1]["entryID"], "")
        self.assertFalse(rows[1]["runningTool"])
        self.assertEqual(rows[2]["reason"], "Process p1")
        service._working = False
        self.assertEqual(activity(service), [])
        service.chat.pop("team")

    def test_selected_activity_is_published_once_for_identical_token_state(self):
        self.select(self.desktop)
        service = self.host.sessions[self.desktop]
        # Exercise the real host path without a listening socket fixture.
        from types import SimpleNamespace
        self.host.remote = SimpleNamespace(bridge=self.mac)
        service._last_remote_activity = None
        self.host.publish_remote_activity(service)
        first = sum(e.get("event") == "remote_activity" for e in self.events)
        self.host.publish_remote_activity(service)
        self.assertEqual(sum(e.get("event") == "remote_activity" for e in self.events), first)
        self.assertFalse(self.mac.view["controls"]["activity"]["busy"])
        self.host.remote = None

    def test_busy_state_survives_encrypted_reconnect_snapshot(self):
        self.select(self.desktop)
        self.mac.publish({"event": "state", "chat": self.desktop, "busy": True, "interrupting": False, "status": "Thinking…"})
        selected = self.mac._snapshot()["selected"]
        self.assertTrue(selected["busy"])
        self.mac.publish({"event": "state", "chat": "cd" * 16, "busy": False, "status": "Ready"})
        self.assertTrue(self.mac._snapshot()["selected"]["busy"])
        self.mac.publish({"event": "state", "chat": self.desktop, "busy": False, "status": "Ready"})
        self.assertFalse(self.mac._snapshot()["selected"]["busy"])

    def test_turn_changes_uses_complete_turn_instead_of_tail(self):
        from chat_remote_actions import turn_changes
        rows = [entry("user", "Start")]
        for i in range(31):
            path = f"Sources/File{i}.swift"
            rows.append(entry("tool", "", tool="apply_patch", changedFiles=[path], memberName="Astra",
                              detail=f"--- a/{path}\n+++ b/{path}\n@@ -1 +1 @@\n-old\n+new"))
        summary = turn_changes(rows)
        self.assertEqual(summary["totalFiles"], 31)
        self.assertEqual(sum(f["added"] for f in summary["files"]), 31)
        self.assertEqual(sum(f["removed"] for f in summary["files"]), 31)
        self.assertEqual(turn_changes(rows + [entry("user", "New turn")])["files"], [])
    def test_full_responsibility_survives_remote_team_edit(self):
        self.select(self.desktop)
        members = [{"name": "Lead", "choice": self.host.models[0]["id"], "responsibility": "A long responsibility. " * 70}]
        self.assertTrue(self.control("configure_team", {"enabled": True, "members": members})["ok"])
        original = self.host.sessions[self.desktop].chat["team"]["members"][0]
        projected = self.mac.view["controls"]["team"]["members"][0]
        self.assertEqual(projected["responsibility"], original["responsibility"])
        fields = {key: projected[key] for key in ("id", "name", "choice", "effort", "responsibility")}
        self.assertTrue(self.control("configure_team", {"enabled": True, "members": [fields]})["ok"])
        self.assertEqual(self.host.sessions[self.desktop].chat["team"]["members"][0]["id"], original["id"])

    def test_download_only_serves_chat_owned_regular_files(self):
        self.select(self.desktop)
        data = b"private attachment" * 4000
        token = self.upload(data)
        self.assertTrue(self.control("remote_board_attach", {"uploads": [token]})["ok"])
        file = self.mac.view["controls"]["blackboard"]["attachments"][0]
        offset, received, revision = 0, b"", None
        while True:
            args = {"attachment": file["id"], "offset": offset}
            if revision: args["revision"] = revision
            ack = self.control("remote_attachment_read", args)
            self.assertTrue(ack["ok"], ack)
            chunk = ack["result"]; received += base64.b64decode(chunk["data"])
            revision, offset = chunk["revision"], chunk["nextOffset"]
            if chunk["done"]: break
        self.assertEqual(received, data)
        self.assertFalse(self.control("remote_attachment_read", {"attachment": "/etc/passwd", "offset": 0})["ok"])
        from chat_blackboard import attachments
        original = Path(attachments(self.host.sessions[self.desktop].chat)[0]["path"])
        original.unlink(); original.symlink_to(self.root / "remote.json")
        self.assertFalse(self.control("remote_attachment_read", {"attachment": file["id"], "offset": 0})["ok"])

    def test_full_model_pages_preserve_provider_group_and_all_models(self):
        self.host.models *= 40
        offset = 0; models = []
        while True:
            reply = self.control("remote_models", {"offset": offset})
            self.assertTrue(reply["ok"], reply)
            models += reply["result"]["models"]
            offset = reply["result"]["nextOffset"]
            if offset is None: break
        self.assertEqual(len(models), len(self.host.models))
        self.assertIn("context", models[0]); self.assertNotIn("scope", models[0])

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.events = []
        self.host = ChatHost(ChatStore(self.root, lock=False), FakeTransport([]), self.events.append,
                             child_factory=lambda: FakeTransport([response("Attachment accepted")]))
        self.addCleanup(self.host.shutdown)
        self.host.models = FakeTransport([]).catalogue()
        self.host.handle({"command": "create", "workspace": str(self.root), "choice": self.host.models[0]["id"]})
        self.desktop = self.host.view_id
        self.mac = MacBridge(self.host.handle, "remote-parity", auto_accept=True)
        def emit(event):
            self.events.append(event)
            self.mac.publish(event)
        self.host._emit = emit
        self.phone = PhoneClient("remote-parity")
        self.phone.start(); exchange(self.mac, self.phone)

    def control(self, action, args=None, chat=None, request=None):
        request = request or "request-" + str(len(self.host.sessions)) + "-" + str(len(self.phone.acks))
        self.phone._send("chat.control", {"requestId": request, "chat": chat or self.mac.selected_chat,
                                         "action": action, "arguments": args or {}})
        exchange(self.mac, self.phone)
        return self.phone.acks[request]

    def select(self, chat):
        self.phone.select(chat, "select-" + chat); exchange(self.mac, self.phone)

    def test_create_before_selection_preserves_desktop_and_deduplicates(self):
        self.assertTrue(self.control("remote_options")["ok"])
        catalogue = next(e for e in reversed(self.events) if e["event"] == "catalogue")
        self.assertIn("folders", catalogue, "Refreshing phone choices must not clear desktop folder choices")
        folders = self.mac.view["controls"]["options"]["workspaces"]
        args = {"choice": self.host.models[1]["id"], "workspace": folders[0]["path"]}
        reply = self.control("remote_create", args, request="create-once")
        self.assertTrue(reply["ok"], reply)
        new = reply["result"]["chat"]
        self.assertNotEqual(new, self.desktop)
        self.assertEqual(self.host.view_id, self.desktop)
        self.assertEqual(self.host.sessions[new].chat["approvalMode"], "manual")
        self.assertEqual(self.host.subscriptions["remote"], {new})
        again = self.control("remote_create", args, request="create-once")
        self.assertEqual(again, reply)
        self.assertEqual(len(self.host.sessions), 2)
        denied = self.control("remote_create", {**args, "workspace": "/arbitrary/path"})
        self.assertFalse(denied["ok"])

    def test_history_pages_are_contiguous_and_exclude_provider_messages(self):
        service = self.host.sessions[self.desktop]
        service.chat["entries"] = [entry("assistant", f"message {i}", "ollama/test", secret="provider-trace") for i in range(95)]
        self.select(self.desktop)
        selected = self.mac._snapshot()["selected"]
        self.assertTrue(selected["hasMore"])
        before = selected["entries"][0]["id"]
        seen = [row["id"] for row in selected["entries"]]
        for page_number in range(3):
            reply = self.control("remote_history", {"before": before})
            self.assertTrue(reply["ok"], reply)
            page = next(e for e in reversed(self.events) if e["event"] == "remote_history")
            ids = [row["id"] for row in page["entries"]]
            self.assertFalse(set(ids) & set(seen))
            seen = ids + seen
            self.assertNotIn("provider-trace", json.dumps(page))
            if ids: before = ids[0]
        self.assertEqual(seen, [row["id"] for row in service.chat["entries"]])
        self.assertFalse(page["hasMore"])
        self.assertFalse(self.control("remote_history", {"before": "missing"})["ok"])

    def upload(self, data, name="notes.txt", token="ab" * 16):
        args = {"upload": token, "name": name, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
        self.assertTrue(self.control("remote_upload_begin", args)["ok"])
        for offset in range(0, len(data), CHUNK):
            reply = self.control("remote_upload_chunk", {"upload": token, "offset": offset, "data": base64.b64encode(data[offset:offset+CHUNK]).decode()})
            self.assertTrue(reply["ok"], reply)
        self.assertTrue(self.control("remote_upload_finish", {"upload": token})["ok"])
        return token

    def test_chunks_enter_normal_chat_intake_and_original_is_preserved(self):
        self.select(self.desktop)
        data = ("Phone source text 😀\n" * 2000).encode()
        token = self.upload(data)
        reply = self.control("remote_send", {"text": "Inspect this", "uploads": [token]}, request="send-once")
        self.assertTrue(reply["ok"], reply)
        service = self.host.sessions[self.desktop]
        service.thread.join(3)
        user = next(e for e in service.chat["entries"] if e["kind"] == "user")
        self.assertEqual(user["attachments"][0]["name"], "notes.txt")
        self.assertEqual(Path(user["attachments"][0]["path"]).read_bytes(), data)
        projected = project_event({"event": "entry", "chat": self.desktop, "entry": user})
        self.assertEqual(projected["entry"]["attachments"][0]["name"], "notes.txt")
        self.assertNotIn("path", projected["entry"]["attachments"][0])
        self.assertFalse(self.host.remote_files.rows)
        self.assertEqual(self.control("remote_send", {"text": "Inspect this", "uploads": [token]}, request="send-once"), reply)
        self.assertEqual(sum(e["kind"] == "user" for e in service.chat["entries"]), 1)

    def test_blackboard_posts_and_uploaded_files_live_on_same_mac_chat(self):
        self.select(self.desktop)
        self.assertTrue(self.control("blackboard_post", {"key": "phone-note", "body": "Remember the constraint", "category": "fact"})["ok"])
        board = self.mac.view["controls"]["blackboard"]
        self.assertEqual(board["posts"][0]["body"], "Remember the constraint")
        self.assertNotIn("thumbnails", board)
        token = self.upload(b"Board reference", token="cd" * 16)
        self.assertTrue(self.control("remote_board_attach", {"uploads": [token]})["ok"])
        self.assertEqual(self.mac.view["controls"]["blackboard"]["attachments"][0]["name"], "notes.txt")
        post = board["posts"][0]["id"]
        self.assertTrue(self.control("blackboard_remove", {"post": post})["ok"])
        self.assertEqual(self.mac.view["controls"]["blackboard"]["posts"], [])
        self.assertEqual(self.host.view_id, self.desktop)
        self.assertFalse(self.control("blackboard_post", {"key": "bad key", "body": "Keep draft", "category": "note"})["ok"])

    def test_mac_restart_keeps_phone_trust_without_new_confirmation(self):
        from chat_remote import RemoteServer
        state = self.root / "remote-saved.json"
        first = RemoteServer(lambda _: None, state, dispatch=lambda callback: callback())
        first.bridge.registry.pair(self.phone.identity_b64())
        first._save()
        second = RemoteServer(lambda _: None, state, dispatch=lambda callback: callback())
        try:
            phone = PhoneClient("providerhub-phone", identity=self.phone.session.identity)
            phone.start(); exchange(second.bridge, phone)
            self.assertTrue(phone.established)
            self.assertIsNone(second.bridge.pending_pairing)
            self.assertEqual(first.bridge.identity_b64(), second.bridge.identity_b64())
        finally:
            first.stop(); second.stop()


class UploadBoundaryTests(unittest.TestCase):
    def test_chat_binding_replay_checksum_caps_and_expiration(self):
        with tempfile.TemporaryDirectory() as root:
            now = [0]
            files = RemoteFiles(root, clock=lambda: now[0])
            try:
                data = b"hello"
                token = "ab" * 16
                with self.assertRaises(ValueError): files.begin("a", token, "../bad", 5, hashlib.sha256(data).hexdigest())
                with self.assertRaises(ValueError): files.begin("a", token, "big.txt", MAX_FILE + 1, "00" * 32)
                files.begin("a", token, "file.txt", len(data), hashlib.sha256(data).hexdigest())
                with self.assertRaises(ValueError): files.chunk("b", token, 0, base64.b64encode(data).decode())
                chunk = base64.b64encode(data).decode()
                self.assertEqual(files.chunk("a", token, 0, chunk)["offset"], 5)
                self.assertEqual(files.chunk("a", token, 0, chunk)["offset"], 5)
                with self.assertRaises(ValueError): files.chunk("a", token, 0, base64.b64encode(b"other").decode())
                files.finish("a", token)
                self.assertEqual(len(files.inputs("a", [token])), 1)
                now[0] = 901
                with self.assertRaises(ValueError): files.inputs("a", [token])
                self.assertFalse(files.rows)
            finally: files.close()
