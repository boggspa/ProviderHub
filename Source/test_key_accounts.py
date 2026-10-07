"""Extra API-key accounts: one Keychain item per key, settings name the active one."""
import unittest
from unittest import mock

import bridge_core
from bridge_core import SLOTS, credentials, key_account_states
from hub_config import connection_signature, keychain_account, normalize


def settings(provider="mistral", accounts=None, active=None, mode="keychain"):
    connection = {"credential_mode": mode}
    if accounts is not None:
        connection["key_accounts"] = accounts
    if active is not None:
        connection["key_account"] = active
    return normalize({"providers": {provider: connection}}, SLOTS, "mistral-test")


WORK = [{"id": "work", "label": " Work "}, {"id": "personal", "label": "Personal"}]


class SettingsTests(unittest.TestCase):
    def test_accounts_round_trip_and_name_their_keychain_items(self):
        value = settings(accounts=WORK, active="work")
        connection = value["providers"]["mistral"]
        self.assertEqual(connection["key_accounts"], [{"id": "work", "label": "Work"}, {"id": "personal", "label": "Personal"}])
        self.assertEqual(connection["key_account"], "work")
        self.assertEqual(keychain_account(connection, "mistral"), "MISTRAL_API_KEY.work")
        self.assertEqual(keychain_account(connection, "mistral", "personal"), "MISTRAL_API_KEY.personal")

    def test_no_active_account_keeps_the_pre_accounts_keychain_item(self):
        value = settings(accounts=WORK)
        connection = value["providers"]["mistral"]
        self.assertNotIn("key_account", connection)
        self.assertEqual(keychain_account(connection, "mistral"), "MISTRAL_API_KEY")
        # Providers without accounts do not grow the fields at all.
        self.assertNotIn("key_accounts", value["providers"]["kimi"])
        self.assertEqual(keychain_account(value["providers"]["kimi"], "kimi"), "KIMI_CODE_API_KEY")
        self.assertIsNone(keychain_account({}, "ollama"))

    def test_every_listed_provider_accepts_accounts(self):
        for provider in ("mistral", "kimi", "mimo", "deepseek", "cerebras", "gemini", "grok",
                         "qwen-token-plan", "minimax", "openrouter"):
            with self.subTest(provider=provider):
                value = settings(provider, [{"id": "work", "label": "Work"}], "work")
                self.assertEqual(value["providers"][provider]["key_account"], "work")

    def test_rejections(self):
        cases = [
            ("mistral", [{"id": "Work", "label": "W"}], None),
            ("mistral", [{"id": "w", "label": ""}], None),
            ("mistral", [{"id": "w", "label": "W", "api_key": "x"}], None),
            ("mistral", [{"id": "w", "label": "A"}, {"id": "w", "label": "B"}], None),
            ("mistral", [{"id": "w", "label": "W"}], "other"),
            ("mistral", None, "w"),
            ("mistral", [{"id": str(i), "label": "A"} for i in range(9)], None),
            ("muse", [{"id": "w", "label": "W"}], None),
            ("claude", [{"id": "w", "label": "W"}], None),
            ("devin", [{"id": "w", "label": "W"}], None),
            ("antigravity", [{"id": "w", "label": "W"}], None),
        ]
        for provider, accounts, active in cases:
            with self.subTest(provider=provider, accounts=accounts, active=active):
                with self.assertRaises(ValueError):
                    settings(provider, accounts, active)

    def test_switching_the_active_key_changes_the_catalogue_signature(self):
        base = settings(accounts=WORK)["providers"]["mistral"]
        work = settings(accounts=WORK, active="work")["providers"]["mistral"]
        personal = settings(accounts=WORK, active="personal")["providers"]["mistral"]
        signatures = {connection_signature("mistral", c) for c in (base, work, personal)}
        self.assertEqual(len(signatures), 3)


class CredentialTests(unittest.TestCase):
    def test_credentials_read_the_active_accounts_item(self):
        value = settings(accounts=WORK, active="personal")
        with mock.patch.object(bridge_core, "keychain_read", return_value="sk-personal") as read:
            self.assertEqual(credentials(value, "mistral"), ("sk-personal", "macOS Keychain · Personal"))
        read.assert_called_once_with(bridge_core.keychain_service(), "MISTRAL_API_KEY.personal")

    def test_a_missing_key_names_the_account(self):
        value = settings(accounts=WORK, active="work")
        with mock.patch.object(bridge_core, "keychain_read", return_value=None):
            with self.assertRaises(bridge_core.BridgeError) as caught:
                credentials(value, "mistral")
        self.assertIn("Work", str(caught.exception))

    def test_states_probe_the_default_slot_and_every_account(self):
        value = settings(accounts=WORK, active="work")
        held = {"MISTRAL_API_KEY": "sk-default", "MISTRAL_API_KEY.work": "sk-work"}
        with mock.patch.object(bridge_core, "keychain_read", side_effect=lambda service, account: held.get(account)):
            rows = key_account_states(value, "mistral")
        self.assertEqual(rows, [
            {"id": None, "label": "Default", "active": False, "found": True},
            {"id": "work", "label": "Work", "active": True, "found": True},
            {"id": "personal", "label": "Personal", "active": False, "found": False},
        ])
        for row in rows:
            self.assertNotIn("key", row)


if __name__ == "__main__":
    unittest.main()
