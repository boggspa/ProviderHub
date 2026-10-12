"""Named API keys remain separate from CLI logins and per-chat choices."""
import copy
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from bridge_core import SLOTS, atomic_json, credentials, default_settings, load_settings
from chat_catalogue import chat_choices, chat_connection
from gateway import Runtime, save_key_metadata
from hub_config import KEY_ACCOUNT_PROVIDERS, normalize
from provider_registry import PROVIDERS


class ApiKeyPrimaryTests(unittest.TestCase):
    def settings(self, provider="claude", mode="keychain"):
        return normalize({"providers": {provider: {"credential_mode": mode,
            "key_accounts": [{"id": "work", "label": "Work"}, {"id": "personal", "label": "Personal"}],
            "key_account": "work"}}}, SLOTS, "mistral-test")

    def test_every_api_key_provider_accepts_named_accounts(self):
        expected = {provider for provider, row in PROVIDERS.items() if row.get("credential_account")}
        self.assertEqual(set(KEY_ACCOUNT_PROVIDERS), expected)
        for provider in expected:
            with self.subTest(provider=provider):
                value = self.settings(provider)
                with patch("bridge_core.keychain_read", return_value="mock-secret") as read:
                    credentials(value, provider)
                self.assertEqual(read.call_args.args[1], PROVIDERS[provider]["credential_account"] + ".work")

    def test_chat_lists_default_and_named_keys_with_primary_first(self):
        for provider in ("claude", "codex", "muse", "grok"):
            with self.subTest(provider=provider):
                value = self.settings(provider)
                route = provider + "/mock-model"
                spec = {"id": route, "provider_id": provider, "model_id": "mock-model", "tools": True}
                value["_model_specs"] = {route: spec};value["codex_catalogue"] = [route]
                value["mappings"] = {slot[0]: route for slot in SLOTS}
                before = copy.deepcopy(value)
                choices = chat_choices(value)
                self.assertEqual([row["account"] for row in choices], ["work", "", "personal"])
                self.assertEqual([row["accountLabel"] for row in choices], ["Work", "Default", "Personal"])
                personal, connection, _, scope = chat_connection(value, provider, "personal")
                self.assertEqual(connection["key_account"], "personal")
                self.assertEqual(value, before)
                with patch("bridge_core.keychain_read", return_value="mock-personal") as read:
                    credentials(personal, provider)
                self.assertTrue(read.call_args.args[1].endswith(".personal"))
                value["providers"][provider]["key_account"] = None
                self.assertEqual(chat_connection(value, provider, "personal")[3], scope)

    def test_primary_switch_does_not_write_a_key_or_change_revision_or_routes(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);value=self.settings()
            atomic_json(root/"settings.json", value)
            before=load_settings(root)
            with patch("gateway.key_account_states", return_value=[{"active": True, "found": True}]):
                saved=save_key_metadata(root, "claude", {"key_account": "personal"}, credential_saved=False)
            self.assertEqual(saved["providers"]["claude"]["key_account"], "personal")
            self.assertEqual(saved["providers"]["claude"]["credential_revision"], before["providers"]["claude"]["credential_revision"])
            self.assertEqual(saved["mappings"], before["mappings"])
            self.assertEqual(saved["port"], before["port"])
            self.assertEqual(saved["providers"]["codex"], before["providers"]["codex"])

    def test_empty_primary_slot_is_rejected_without_persisting(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);atomic_json(root/"settings.json", self.settings())
            before=(root/"settings.json").read_bytes()
            with patch("gateway.key_account_states", return_value=[{"active": True, "found": False}]), \
                    self.assertRaisesRegex(Exception, "Save a key"):
                save_key_metadata(root, "claude", {"key_account": "personal"}, credential_saved=False)
            self.assertEqual((root/"settings.json").read_bytes(),before)

    def test_cli_login_and_second_source_api_accounts_are_independent(self):
        value=self.settings(mode="cli");value["providers"]["claude"]["chat_both_sources"]=True
        _, primary, directory, _=chat_connection(value,"claude", "")
        api, selected, _, _=chat_connection(value,"claude", "keychain:personal")
        self.assertEqual(primary["credential_mode"], "cli")
        self.assertIsNone(directory)
        self.assertEqual(selected["key_account"],"personal")
        self.assertEqual(selected["credential_mode"],"keychain")
        self.assertEqual(value["providers"]["claude"]["credential_mode"],"cli")
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);atomic_json(root/"settings.json",value)
            saved=save_key_metadata(root,"claude",{"key_account":"personal"},credential_saved=False)
            self.assertEqual(saved["providers"]["claude"]["credential_mode"],"cli")

    def test_gateway_next_call_resolves_primary_without_stopping_or_caching_wrong_key(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);value=self.settings();atomic_json(root/"settings.json",value)
            runtime=Runtime.__new__(Runtime)
            runtime.root=root;runtime.settings=value;runtime.lock=threading.Lock()
            runtime.key=None;runtime.credential_cache={}
            with patch("gateway.credentials", side_effect=lambda settings, provider: (
                    "key-"+settings["providers"][provider]["key_account"],"mock")), \
                    patch("gateway.key_account_states",return_value=[{"active":True,"found":True}]):
                captured=runtime.provider_key("claude")
                save_key_metadata(root,"claude",{"key_account":"personal"},credential_saved=False)
                self.assertEqual(runtime.provider_key("claude"),"key-personal")
                self.assertEqual(captured,"key-work")

    def test_account_controls_exist_in_both_provider_surfaces(self):
        source=Path(__file__).parent
        compact=(source/"CompactShell.swift").read_text()
        sub=compact[compact.index('if model.settings.providers[provider.id]?.credential_mode == "keychain"'):]
        self.assertIn("KeyAccountsSection(model: model, provider: provider.id)",sub)
        view=(source/"ProviderViews.swift").read_text()
        self.assertIn('Text("Primary key")',view)
        self.assertIn("model.saveKeyAccounts(provider)",view)


if __name__=="__main__":unittest.main()
