import copy
import unittest
from unittest.mock import patch

from chat_catalogue import chat_choices, chat_connection
from hub_config import defaults, SLOTS


class ChatCatalogueTests(unittest.TestCase):
    def setUp(self):
        self.settings = defaults(SLOTS, "mistral-vibe-cli-latest", 11438)
        self.settings["providers"]["claude"].update(credential_mode="cli", cli_accounts=[{"id": "work", "label": "Work", "config_dir": "/accounts/work"}], cli_account="work")
        spec = {"id": "claude/claude-fable-5-1", "provider_id": "claude", "model_id": "claude-fable-5-1", "tools": True, "context": 200000, "effort_modes": ["low", "high"]}
        self.settings["_model_specs"] = {spec["id"]: spec, "claude/alias": spec}

    def test_catalogue_deduplicates_aliases_and_uses_actual_efforts(self):
        rows = chat_choices(self.settings)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["account"], "work")
        self.assertEqual(rows[0]["efforts"], ["low", "high"])
        self.assertEqual(rows[0]["context"], 200000)

    def test_a_second_credential_source_lists_beside_the_active_one(self):
        # Claude runs on the installed CLI; the Keychain key is listed too.
        self.settings["providers"]["claude"]["chat_both_sources"] = True
        api = {"id": "claude/claude-fable-5-1", "provider_id": "claude", "model_id": "claude-fable-5-1",
               "tools": True, "context": 1000000, "effort_modes": ["low"], "source": "provider_api"}
        legacy = {**api, "id": "claude/claude-opus-4-6", "model_id": "claude-opus-4-6"}
        self.settings["_chat_source_specs"] = {"claude": {"keychain": {api["id"]: api, legacy["id"]: legacy}}}
        rows = chat_choices(self.settings)
        self.assertEqual([(row["account"], row["source"], row["route"]) for row in rows], [
            ("work", "cli", "claude/claude-fable-5-1"), ("", "cli", "claude/claude-fable-5-1"),
            # The second source has no tick list: it lists its whole catalogue.
            ("keychain:", "keychain", "claude/claude-fable-5-1"), ("keychain:", "keychain", "claude/claude-opus-4-6")])
        self.assertEqual([row["accountLabel"] for row in rows],
                         ["Work · Installed CLI", "Default · Installed CLI", "Default · API key", "Default · API key"])
        self.assertEqual(rows[0]["id"], "claude/claude-fable-5-1|work")
        self.assertEqual(rows[2]["id"], "claude/claude-fable-5-1|keychain:")
        # Each source carries its own catalogue's metadata and its own scope;
        # a scope names a connection, so the key's two routes share one.
        self.assertEqual((rows[1]["context"], rows[2]["context"]), (200000, 1000000))
        self.assertEqual(len({rows[0]["scope"], rows[1]["scope"], rows[2]["scope"]}), 3)
        self.assertEqual(rows[2]["scope"], rows[3]["scope"])
        self.assertEqual(rows[2]["connectionPresentation"], rows[1]["connectionPresentation"])
        # Hosted search is a CLI capability; the same model on the key is not offered it.
        self.settings["_model_specs"]["claude/claude-fable-5-1"]["web_search"] = True
        api["web_search"] = True
        rows = chat_choices(self.settings)
        self.assertEqual([row["supportsWebSearch"] for row in rows], [True, True, False, False])
        # Without the switch the second catalogue is ignored and labels stay bare.
        del self.settings["providers"]["claude"]["chat_both_sources"]
        rows = chat_choices(self.settings)
        self.assertEqual([row["account"] for row in rows], ["work", ""])
        self.assertEqual(rows[1]["accountLabel"], "Default")

    def test_a_second_source_account_selects_that_source_without_touching_the_active_one(self):
        before = copy.deepcopy(self.settings)
        with self.assertRaisesRegex(ValueError, "no longer offered"):
            chat_connection(self.settings, "claude", "keychain:")
        self.settings["providers"]["claude"]["chat_both_sources"] = True
        selected, connection, directory, scope = chat_connection(self.settings, "claude", "keychain:")
        self.assertEqual(connection["credential_mode"], "keychain")
        self.assertIsNone(connection["key_account"])
        self.assertIsNone(directory)
        self.assertEqual(selected["providers"]["claude"]["credential_mode"], "keychain")
        self.assertEqual(self.settings["providers"]["claude"]["credential_mode"], "cli")
        _, _, work_directory, work_scope = chat_connection(self.settings, "claude", "work")
        self.assertEqual(work_directory, "/accounts/work")
        self.assertNotEqual(scope, work_scope)
        # Naming the active source explicitly is the same connection as the bare id.
        _, _, _, explicit_scope = chat_connection(self.settings, "claude", "cli:work")
        self.assertEqual(explicit_scope, work_scope)
        with self.assertRaisesRegex(ValueError, "no longer configured"):
            chat_connection(self.settings, "claude", "keychain:work")
        with self.assertRaisesRegex(ValueError, "no longer offered"):
            chat_connection(self.settings, "claude", "environment:")
        # A saved chat on the second source is refused once the switch is off.
        self.settings["providers"]["claude"].pop("chat_both_sources")
        with self.assertRaisesRegex(ValueError, "no longer offered"):
            chat_connection(self.settings, "claude", "keychain:", scope)
        self.settings["providers"]["claude"]["chat_both_sources"] = True
        self.settings["providers"]["claude"]["credential_revision"] += 1
        with self.assertRaisesRegex(ValueError, "connection changed"):
            chat_connection(self.settings, "claude", "keychain:", scope)
        self.settings["providers"]["claude"]["credential_revision"] -= 1
        del self.settings["providers"]["claude"]["chat_both_sources"]
        self.assertEqual(self.settings, before)

    def test_gateway_serves_a_chat_on_the_second_source_from_its_own_catalogue(self):
        import http.client
        import json
        import tempfile
        from pathlib import Path
        import cli_routes
        from bridge_core import atomic_json, connection_signature, default_settings
        from gateway import Runtime, Server
        from hub_config import chat_source_settings
        from test_cli_routes import _fake_adapter
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        saved = dict(cli_routes._cache)
        cli_routes._cache.clear()
        self.addCleanup(lambda: (cli_routes._cache.clear(), cli_routes._cache.update(saved)))
        cli_routes._cache["claude"] = _fake_adapter()
        vibe = {"active_model": "unused", "active_display_name": "Unused", "key_name": "MISTRAL_API_KEY",
                "vibe_home": str(root / "vibe"), "configured_models": []}
        with patch("bridge_core.vibe_settings", return_value=vibe):
            settings = default_settings()
        settings["providers"]["claude"].update(credential_mode="cli", chat_both_sources=True)
        settings["mappings"] = {slot[0]: "claude/claude-sonnet-5" for slot in SLOTS}
        # Curate the launcher lists to one route: the CLI projection seeds every
        # known Claude tier, and an uncurated Codex list would show them all.
        settings["codex_catalogue"] = ["claude/claude-sonnet-5"]
        atomic_json(root / "settings.json", settings)
        def model(identifier, source, context):
            return {"id": identifier, "canonical_id": identifier, "display_name": identifier, "context": context,
                    "aliases": [identifier], "tools": True, "vision": True, "reasoning": True,
                    "effort_modes": ["low", "high"], "fast_mode": False, "inference_status": "advertised",
                    "source": source, "evidence": "test"}
        atomic_json(root / "catalogues" / "claude.json", {
            "provider_id": "claude", "source": "cli", "models": [model("claude-sonnet-5", "cli", 200000)],
            "connection_signature": connection_signature("claude", settings["providers"]["claude"])})
        keyed = chat_source_settings(settings, "claude", "keychain")["providers"]["claude"]
        atomic_json(root / "catalogues" / "claude.keychain.json", {
            "provider_id": "claude", "source": "provider_api",
            "models": [model("claude-sonnet-5", "provider_api", 1000000), model("claude-opus-4-1", "provider_api", 200000)],
            "connection_signature": connection_signature("claude", keyed)})
        with patch("bridge_core.vibe_settings", return_value=vibe):
            runtime = Runtime(root)
        self.assertEqual(sorted(runtime.settings["_chat_source_specs"]["claude"]["keychain"]),
                         ["claude/claude-opus-4-1", "claude/claude-sonnet-5"])
        server = Server(runtime, 0)
        import threading
        threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .05}, daemon=True).start()
        self.addCleanup(server.server_close); self.addCleanup(server.shutdown)
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        connection.request("GET", "/_bridge/chat/models", headers={"Authorization": "Bearer " + runtime.token})
        rows = json.loads(connection.getresponse().read())["models"]
        connection.close()
        self.assertEqual(sorted((row["account"], row["route"], row["context"]) for row in rows), [
            ("", "claude/claude-sonnet-5", 200000),
            ("keychain:", "claude/claude-opus-4-1", 200000), ("keychain:", "claude/claude-sonnet-5", 1000000)])
        base = {"max_tokens": 64, "messages": [{"role": "user", "content": "hello"}], "_provider_hub_surface": "chat"}
        with patch("bridge_core.keychain_read", return_value="sk-ant-test"):
            # The active source plans the CLI turn, exactly as before.
            plan = runtime.plan({**base, "model": "claude/claude-sonnet-5", "_provider_hub_account": ""})
            self.assertTrue(plan["cli"])
            self.assertEqual(plan["model_spec"]["context"], 200000)
            # The key's rows run as API requests, with that source's own metadata.
            plan = runtime.plan({**base, "model": "claude/claude-sonnet-5", "_provider_hub_account": "keychain:"})
            self.assertFalse(plan.get("cli"))
            self.assertIn("api.anthropic.com", plan["url"])
            self.assertEqual(plan["headers"]["x-api-key"], "sk-ant-test")
            self.assertEqual(plan["model_spec"]["context"], 1000000)
            plan = runtime.plan({**base, "model": "claude/claude-opus-4-1", "_provider_hub_account": "keychain:"})
            self.assertEqual(plan["upstream_model"], "claude-opus-4-1")
            # A model only the key offers is not reachable through the CLI rows.
            with self.assertRaisesRegex(Exception, "not in the current provider catalogue"):
                runtime.plan({**base, "model": "claude/claude-opus-4-1", "_provider_hub_account": ""})
            # And the key source is only reachable while the switch is on.
            settings["providers"]["claude"].pop("chat_both_sources")
            atomic_json(root / "settings.json", settings)
            with self.assertRaisesRegex(Exception, "no longer offered"):
                runtime.plan({**base, "model": "claude/claude-sonnet-5", "_provider_hub_account": "keychain:"})

    def test_shared_branding_and_overrides_are_display_only(self):
        self.settings["branding_overrides"] = {"claude": {"accent": "#123456", "displayProvider": "Studio", "shortCode": "ST"}}
        rows = chat_choices(self.settings)
        self.assertEqual(rows[0]["presentation"]["accent"], "#123456")
        self.assertEqual(rows[0]["provider"], "Studio")
        self.assertTrue(rows[0]["route"].startswith("claude/"))

    def test_native_search_requires_advertised_and_supported_route(self):
        spec = self.settings["_model_specs"]["claude/claude-fable-5-1"]
        self.assertTrue(all(row["supportsWebSearch"] is False for row in chat_choices(self.settings)))
        spec["web_search"] = True
        self.assertTrue(all(row["supportsWebSearch"] is True for row in chat_choices(self.settings)))
        self.settings["providers"]["claude"]["credential_mode"] = "keychain"
        self.assertTrue(all(row["supportsWebSearch"] is False for row in chat_choices(self.settings)))
        route = "openrouter/test"
        self.settings["_model_specs"] = {route: {**spec, "id": route, "provider_id": "openrouter", "model_id": "test"}}
        self.settings["mappings"] = {slot[0]: route for slot in SLOTS}
        self.settings["codex_catalogue"] = [route]
        self.assertTrue(all(row["supportsWebSearch"] is True for row in chat_choices(self.settings)))

    def test_per_chat_cli_account_does_not_mutate_active_desktop_account(self):
        before = copy.deepcopy(self.settings)
        selected, connection, directory, default_scope = chat_connection(self.settings, "claude", "")
        self.assertIsNone(directory)
        self.assertIsNone(connection["cli_account"])
        self.assertEqual(self.settings, before)
        _, _, directory, work_scope = chat_connection(self.settings, "claude", "work")
        self.assertEqual(directory, "/accounts/work")
        self.assertNotEqual(default_scope, work_scope)

    def test_live_model_label_override_replaces_previously_resolved_label(self):
        spec = self.settings["_model_specs"]["claude/claude-fable-5-1"]
        spec["presentation"] = {"modelLabel": "Old cached label"}
        self.settings["branding_overrides"] = {"claude": {"modelLabels": {"claude-fable-5-1": "Current label"}}}
        self.assertEqual(chat_choices(self.settings)[0]["label"], "Current label")
        self.settings["branding_overrides"] = {}
        self.assertNotEqual(chat_choices(self.settings)[0]["label"], "Old cached label")

    def test_only_launcher_selected_models_are_listed_and_union_is_deduplicated(self):
        fable = "claude/claude-fable-5-1"
        other = "claude/claude-opus-5-5"
        self.settings["_model_specs"][other] = {**self.settings["_model_specs"][fable], "id": other, "model_id": "claude-opus-5-5"}
        self.settings["mappings"] = {slot[0]: fable for slot in SLOTS}
        self.settings["codex_catalogue"] = [fable]
        self.assertEqual({row["route"] for row in chat_choices(self.settings)}, {fable})
        self.settings["codex_catalogue"] = [other, "claude/alias"]
        rows = chat_choices(self.settings)
        self.assertEqual({row["route"] for row in rows}, {fable, other})
        self.assertEqual(len(rows), 4)  # one row per route/account, not desktop alias
        self.assertEqual(rows[0]["connectionPresentation"]["runtimeProvider"], "claude")

    def test_brand_accent_does_not_change_provider_connection_group(self):
        route = "ollama/gpt-oss:120b"
        self.settings["_model_specs"] = {route: {"id": route, "model_id": "gpt-oss:120b", "provider_id": "ollama", "context": 65536, "tools": True}}
        rows = chat_choices(self.settings)
        self.assertEqual(rows[0]["connectionPresentation"]["runtimeProvider"], "ollama")
        self.assertEqual(rows[0]["route"], route)

    def test_named_key_account_selects_only_its_own_keychain_slot(self):
        self.settings["providers"]["mistral"].update(credential_mode="keychain", key_accounts=[{"id": "work", "label": "Work"}], key_account="work")
        selected, _, _, _ = chat_connection(self.settings, "mistral", "")
        from hub_config import keychain_account
        self.assertEqual(keychain_account(selected["providers"]["mistral"], "mistral"), "MISTRAL_API_KEY")
        selected, _, _, _ = chat_connection(self.settings, "mistral", "work")
        self.assertEqual(keychain_account(selected["providers"]["mistral"], "mistral"), "MISTRAL_API_KEY.work")
        self.assertEqual(self.settings["providers"]["mistral"]["key_account"], "work")

    def test_connection_revision_account_removal_and_mode_changes_require_fresh_chat(self):
        _, _, _, scope = chat_connection(self.settings, "claude", "work")
        self.settings["providers"]["claude"]["credential_revision"] += 1
        with self.assertRaises(ValueError): chat_connection(self.settings, "claude", "work", scope)
        self.settings["providers"]["claude"]["cli_accounts"] = []
        with self.assertRaises(ValueError): chat_connection(self.settings, "claude", "work")
        with self.assertRaises(ValueError): chat_connection(self.settings, "claude", {"id": "work"})

    def test_chat_surface_uses_actual_context_and_ignores_claude_omit_settings(self):
        # Reuse the existing local upstream fixture: no model account is contacted.
        import test_gateway_hub as fixtures
        harness = fixtures.GatewayHubHTTPTests()
        harness.setUp()
        try:
            route = harness.start_gateway("deepseek", "deepseek-test", {"context": 16384, "reasoning": False})
            harness.runtime.settings["mapping_options"] = {SLOTS[0][0]: {"omit_tools": True, "omit_system": True}}
            payload = {"model": route, "messages": [{"role": "user", "content": "hello"}], "max_tokens": 20,
                       "system": "Chat host", "tools": [fixtures.tool_definition()], "_provider_hub_surface": "chat"}
            plan = harness.runtime.plan(payload)
            self.assertEqual(plan["model_spec"]["context"], 16384)
            self.assertIn("Provider Hub Chat", json_text(plan))
            self.assertIn("read_file", json_text(plan))
        finally:
            harness.tearDown()

    def test_chat_search_crosses_real_gateway_and_rechecks_live_capability(self):
        import json
        import cli_routes
        from test_cli_routes import GatewayCliTurnTest
        harness = GatewayCliTurnTest()
        harness.setUp()
        try:
            route = "claude/claude-sonnet-5"
            spec = harness.runtime.settings["_model_specs"][route]
            spec["web_search"] = True
            cli_routes._cache["claude"].WEB_SEARCH = True
            harness.events = [
                {"type": "web_search", "status": "completed", "id": "s1", "action": {"type": "search", "query": "news"},
                 "results": [{"url": "https://example.com/news", "title": "News"}]},
                {"type": "text_delta", "text": "Answer"}, {"type": "message_stop", "stop_reason": "end_turn"}]
            payload = {"model": route, "max_tokens": 512, "messages": [{"role": "user", "content": "news"}],
                       "_provider_hub_surface": "chat", "_web_search": {"context_size": None, "allowed_domains": [], "live": True}}
            status, raw = harness.request(payload)
            self.assertEqual(status, 200, raw)
            self.assertEqual(harness.requests[-1]["web_search"], payload["_web_search"])
            self.assertTrue(any(block["type"] == "web_search_tool_result" for block in json.loads(raw)["content"]))
            harness.events = [{"type": "text_delta", "text": "No search"}, {"type": "message_stop", "stop_reason": "end_turn"}]
            status, raw = harness.request({k: v for k, v in payload.items() if k != "_web_search"})
            self.assertEqual(status, 200, raw)
            self.assertNotIn("web_search", harness.requests[-1])
            spec["web_search"] = False
            status, raw = harness.request(payload)
            self.assertEqual(status, 200, raw)
            self.assertNotIn("web_search", harness.requests[-1])
        finally:
            harness.tearDown()


def json_text(value):
    import json
    return json.dumps(value, default=str)


if __name__ == "__main__": unittest.main()
