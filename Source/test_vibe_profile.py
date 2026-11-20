"""The Vibe profile transaction: the Hub's rows go into Vibe's own config and
come back out again without touching the user's own settings."""
from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from vibe_catalogue import PROVIDER_ID
from vibe_profile import TOKEN_ENV, VibeProfile

USER_CONFIG = '''theme = "ansi-dark"
active_model = "mistral-large-4"

[[providers]]
name = "mistral"
api_base = "https://api.mistral.ai/v1"
api_key_env_var = "MISTRAL_API_KEY"
backend = "mistral"

[[models]]
name = "mistral-large-4"
provider = "mistral"
alias = "mistral-large-4"
display_name = "Mistral Large 4"
thinking = "high"

[tools.bash]
allowlist = [
    "cat",
    "ls",
]
'''


def entry(route, **extra):
    provider_id, model = route.split("/", 1)
    row = {"id": route, "provider_id": provider_id, "model_id": model,
           "display_name": model, "tools": True, "vision": False, "reasoning": False,
           "effort_modes": [], "context": 200000}
    row.update(extra)
    return row


class VibeProfileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.home = self.base / "vibe-home"
        self.home.mkdir()
        self.config = self.home / "config.toml"
        self.config.write_text(USER_CONFIG)
        self.profile = VibeProfile(self.base / "bridge", vibe_home_dir=self.home, running=lambda: False)
        self.settings = {"port": 11436, "vibe_model": "codex/gpt-5.2-codex"}
        self.inventory = {"models": [entry("codex/gpt-5.2-codex"), entry("mistral/mistral-large-4")]}

    def tearDown(self):
        self.tmp.cleanup()

    def text(self):
        return self.config.read_text()

    def test_activation_adds_the_provider_and_the_model_rows(self):
        result = self.profile.activate(self.settings, self.inventory)
        self.assertTrue(self.profile.active())
        text = self.text()
        self.assertIn('name = "provider_hub"', text)
        self.assertIn('api_base = "http://127.0.0.1:11436"', text)
        self.assertIn('api_style = "anthropic"', text)
        self.assertIn('alias = "codex/gpt-5.2-codex"', text)
        self.assertIn("codex/gpt-5.2-codex", result["vibe_models"])

    def test_the_users_own_rows_and_settings_survive(self):
        self.profile.activate(self.settings, self.inventory)
        text = self.text()
        self.assertIn('theme = "ansi-dark"', text)
        self.assertIn('name = "mistral"', text)
        self.assertIn('alias = "mistral-large-4"', text)
        self.assertIn("[tools.bash]", text)
        self.assertIn("allowlist", text)

    def test_the_token_never_reaches_the_file(self):
        self.profile.activate(self.settings, self.inventory)
        text = self.text()
        self.assertIn(f'api_key_env_var = "{TOKEN_ENV}"', text)
        self.assertNotIn("Bearer", text)

    def test_restore_removes_our_rows_and_restores_the_users_model(self):
        self.profile.activate(self.settings, self.inventory)
        result = self.profile.restore()
        self.assertTrue(result["restored"])
        text = self.text()
        self.assertNotIn("provider_hub", text)
        self.assertNotIn("codex/gpt-5.2-codex", text)
        self.assertIn('active_model = "mistral-large-4"', text)
        self.assertFalse(self.profile.journal.exists())

    def test_restore_keeps_a_model_the_user_changed_while_the_hub_was_active(self):
        self.profile.activate(self.settings, self.inventory)
        # Vibe rewrites its own file; the user picks another model in the app.
        text = self.text().replace('active_model = "codex/gpt-5.2-codex"',
                                    'active_model = "mistral-large-4"')
        self.config.write_text(text)
        result = self.profile.restore()
        self.assertEqual(result["preserved_external_changes"], 1)
        self.assertIn('active_model = "mistral-large-4"', self.text())

    def test_activation_refuses_while_vibe_is_running(self):
        profile = VibeProfile(self.base / "bridge", vibe_home_dir=self.home, running=lambda: True)
        with self.assertRaises(Exception) as raised:
            profile.activate(self.settings, self.inventory)
        self.assertIn("Quit Vibe", str(raised.exception))

    def test_activation_refuses_a_second_time_before_a_restore(self):
        self.profile.activate(self.settings, self.inventory)
        with self.assertRaises(Exception) as raised:
            self.profile.activate(self.settings, self.inventory)
        self.assertIn("Restore the previous Vibe configuration", str(raised.exception))

    def test_a_file_that_did_not_exist_is_removed_again(self):
        self.config.unlink()
        self.profile.activate(self.settings, self.inventory)
        self.assertTrue(self.config.exists())
        self.profile.restore()
        self.assertFalse(self.config.exists())

    def test_the_journal_records_what_it_replaced(self):
        self.profile.activate(self.settings, self.inventory)
        from bridge_core import read_json
        journal = read_json(self.profile.journal)
        self.assertEqual(journal["active_model"], {"present": True, "value": "mistral-large-4"})
        self.assertTrue(journal["config"]["existed"])


if __name__ == "__main__":
    unittest.main()
