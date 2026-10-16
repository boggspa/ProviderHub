"""Control commands cross the authenticated channel without widening host authority."""
import json
import unittest
import tempfile
import time
from pathlib import Path

from chat_remote import exchange, project_event
from chat_remote_controls import command, bounded
from test_chat_remote import pair, CHAT


class ControlTests(unittest.TestCase):
    def test_companion_brand_resources_match_desktop_verbatim(self):
        root = Path(__file__).resolve().parent.parent
        resources = root / "Packages/ProviderHubRemote/Sources/ProviderHubRemote/Resources"
        self.assertEqual((root / "Source/provider_branding.json").read_bytes(), (resources / "provider_branding.json").read_bytes(), "Sync the companion branding resource after changing desktop branding")
        expected = list((root / "Source/provider-logos").glob("*.png"))
        for asset in expected:
            self.assertEqual(asset.read_bytes(), (resources / "provider-logos" / asset.name).read_bytes(), asset.name)
        self.assertEqual({p.name for p in expected}, {p.name for p in (resources / "provider-logos").glob("*.png")})

    def test_private_state_is_omitted_from_models_team_and_processes(self):
        catalogue = project_event({"event": "catalogue", "models": [{"id": "route|opaque", "label": "Sol", "account": "secret", "scope": "secret", "apiKey": "secret"}]})
        self.assertEqual(catalogue["value"], [{"id": "route|opaque", "label": "Sol"}])
        team = project_event({"event": "team", "chat": CHAT, "team": {"enabled": True, "messages": ["secret"], "members": [
            {"id": "member", "name": "Sol", "choice": "route|opaque", "account": "secret", "messages": ["secret"], "pendingChange": {"name": "New", "apiKey": "secret"}}]}})
        self.assertNotIn("secret", json.dumps(team))
        processes = project_event({"event": "processes", "chat": CHAT, "processes": [{"id": "p", "command": "pytest", "output": "pass", "environment": {"TOKEN": "secret"}}]})
        self.assertNotIn("secret", json.dumps(processes))

    def test_all_eight_members_survive_verbose_roster_projection(self):
        event = {"event": "team", "team": {"enabled": True, "members": [
            {"id": str(i), "name": "Member " + str(i), "choice": "codex/sol|", "responsibility": "😀" * 2000,
             "nextStep": "x" * 1000, "status": "working"} for i in range(8)]}}
        value = project_event(event)["value"]
        self.assertEqual([m["id"] for m in value["members"]], list(map(str, range(8))))

    def test_authenticated_controls_are_deduplicated_and_bound_to_selection(self):
        host, mac, phone = pair()
        phone.select(CHAT, "select"); exchange(mac, phone)
        params = {"chat": CHAT, "action": "configure", "arguments": {"approvalMode": "accept_edits", "expectedWorkspace": "/work"}, "requestId": "config"}
        phone._send("chat.control", params); exchange(mac, phone)
        self.assertTrue(phone.acks["config"]["ok"])
        self.assertEqual(host.commands[-1], {"command": "configure", "chat": CHAT, "id": CHAT, "request": "config", "approvalMode": "accept_edits", "expectedWorkspace": "/work"})
        phone._send("chat.control", params); exchange(mac, phone)
        self.assertEqual(len(host.commands), 2)
        phone._send("chat.control", {**params, "chat": "cd" * 16, "requestId": "wrong"}); exchange(mac, phone)
        self.assertFalse(phone.acks["wrong"]["ok"])

    def test_control_fields_cannot_inject_global_or_native_commands(self):
        for action, args in [("shutdown", {}), ("native_result", {}), ("remote_revoke", {}),
                             ("configure", {"approvalMode": "yolo"}), ("configure", {"expectedWorkspace": "/work", "workspace": "/other"}),
                             ("side_send", {"text": "hello"}), ("remote_browser", {"tool": "run_shell", "arguments": {}})]:
            with self.subTest(action=action, args=args), self.assertRaises(ValueError):
                command({"action": action, "arguments": args, "chat": CHAT}, "request")

    def test_snapshots_keep_controls_and_selection_clears_owned_panels(self):
        host, mac, phone = pair()
        phone.select(CHAT, "select"); exchange(mac, phone)
        mac.publish({"event": "team", "chat": CHAT, "team": {"enabled": True, "members": [{"id": "m", "name": "Sol"}]}})
        self.assertEqual(mac._snapshot()["controls"]["team"]["members"][0]["name"], "Sol")
        self.assertIsNone(mac.publish({"event": "team", "chat": "cd" * 16, "team": {"members": []}}))
        phone.select("cd" * 16, "second"); exchange(mac, phone)
        self.assertNotIn("team", mac._snapshot()["controls"])
        self.assertEqual(mac._snapshot()["controls"]["version"], 1)

    def test_adversarial_unicode_panels_fit_encrypted_snapshot(self):
        host, mac, phone = pair()
        huge = '\x00\\"😀' * 20000
        events = [
            {"event": "catalogue", "models": [{"id": str(i), "label": huge} for i in range(256)]},
            {"event": "team", "team": {"members": [{"id": str(i), "name": huge, "responsibility": huge} for i in range(8)]}},
            {"event": "side", "side": {"id": CHAT, "entries": [{"kind": "assistant", "text": huge} for _ in range(30)]}},
            {"event": "agents", "agents": [{"id": CHAT, "entries": [{"text": huge}]} for _ in range(8)]},
            {"event": "processes", "processes": [{"id": str(i), "output": huge} for i in range(32)]},
            {"event": "git_changes", "changes": {"files": [{"path": huge, "diff": huge} for _ in range(64)]}},
            {"event": "remote_browser", "text": huge, "details": {"nodes": [{"text": huge} for _ in range(256)]}},
        ]
        for event in events:
            projected = mac.publish(event)
            self.assertLess(len(json.dumps(projected, ensure_ascii=False).encode()), 80000 if event["event"] == "team" else 18000)
        snapshot = mac._snapshot()
        self.assertLess(len(json.dumps(snapshot, ensure_ascii=False).encode()), 120000)
        mac._emit("session.snapshot", snapshot)
        exchange(mac, phone)

    def test_bounded_values_remain_valid_json(self):
        value = bounded({"files": [{"diff": "\\" * 50000} for _ in range(64)]}, 10000)
        self.assertLess(len(json.dumps(value, ensure_ascii=False).encode()), 10000)

    def test_production_host_permissions_team_side_and_browser_share_exact_chat(self):
        from chat_sessions import ChatHost
        from chat_runtime import ChatStore
        from test_chat_runtime import FakeTransport, response
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            events = []
            host = ChatHost(ChatStore(root, lock=False), FakeTransport([]), events.append,
                            child_factory=lambda: FakeTransport([response("Side reply")]))
            host.models = FakeTransport([]).catalogue()
            host.handle({"command": "create", "choice": host.models[0]["id"], "workspace": str(root)})
            chat = host.view_id
            try:
                host.handle(command({"chat": chat, "action": "remote_inspect", "arguments": {}}, "inspect"))
                configuration = next(e for e in reversed(events) if e["event"] == "remote_configuration")
                self.assertEqual(configuration["choice"], host.models[0]["id"])
                host.handle(command({"chat": chat, "action": "configure", "arguments": {"approvalMode": "accept_edits", "expectedWorkspace": configuration["workspace"]}}, "permissions"))
                self.assertEqual(host.sessions[chat].chat["approvalMode"], "accept_edits")
                members = [{"name": "Lead", "choice": host.models[0]["id"]}, {"name": "Peer", "choice": host.models[1]["id"]}]
                host.handle(command({"chat": chat, "action": "configure_team", "arguments": {"enabled": True, "members": members}}, "team"))
                team = next(e for e in reversed(events) if e["event"] == "team")["team"]
                self.assertEqual([m["name"] for m in team["members"]], ["Lead", "Peer"])
                host.handle(command({"chat": chat, "action": "open_side", "arguments": {"choice": host.models[1]["id"]}}, "open-side"))
                side = host.sessions[chat].side
                self.assertIsNotNone(side)
                host.handle(command({"chat": chat, "action": "side_send", "arguments": {"side": side.chat["id"], "text": "Hello"}}, "side-send"))
                side.thread.join(3)
                self.assertTrue(any(e.get("event") == "side" and any(r.get("text") == "Side reply" for r in (e.get("side") or {}).get("entries", [])) for e in events))
                host.capabilities.native.set_enabled(True)
                host.capabilities._browser_enabled = True
                calls = []
                def native(chat_id, tool, args, cancel, request_id=None, **kwargs):
                    calls.append((chat_id, tool))
                    return {"is_error": False, "summary": "Observed", "content": [{"type": "text", "text": json.dumps({"title": "Shared Mac page", "url": "https://example.com", "snapshot_id": "snapshot", "nodes": []})}]}
                host.capabilities.native.call = native
                host.handle(command({"chat": chat, "action": "remote_browser", "arguments": {"tool": "browser_observe", "arguments": {"depth": 3, "max_bytes": 10000}}}, "browser"))
                deadline = time.monotonic() + 2
                while not any(e.get("event") == "remote_browser" for e in events) and time.monotonic() < deadline: time.sleep(.01)
                browser = next(e for e in reversed(events) if e["event"] == "remote_browser")
                self.assertEqual(browser["details"]["title"], "Shared Mac page")
                self.assertEqual(calls, [(chat, "browser_observe")])
                self.assertEqual(host.view_id, chat)
            finally: host.shutdown()
