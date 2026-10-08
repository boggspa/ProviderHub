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

    def test_shared_branding_and_overrides_are_display_only(self):
        self.settings["branding_overrides"] = {"claude": {"accent": "#123456", "displayProvider": "Studio", "shortCode": "ST"}}
        rows = chat_choices(self.settings)
        self.assertEqual(rows[0]["presentation"]["accent"], "#123456")
        self.assertEqual(rows[0]["provider"], "Studio")
        self.assertTrue(rows[0]["route"].startswith("claude/"))

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


def json_text(value):
    import json
    return json.dumps(value, default=str)


if __name__ == "__main__": unittest.main()
