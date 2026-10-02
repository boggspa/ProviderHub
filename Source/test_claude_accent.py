"""The mod gets only truthful display data, never settings or credentials."""
import json
from pathlib import Path
import tempfile
import unittest

from branding import resolve_presentation
from claude_accent import CATALOGUE_FILE, accent_catalogue, refresh_accents
from hub_config import claude_catalogue_rows


class ClaudeAccentTests(unittest.TestCase):
    def settings(self):
        return {
            "port": 11436,
            "claude_catalogue": [
                {"route": "ollama/qwen3.5:4b", "tier": "haiku", "tier_default": True},
                {"route": "mistral/mistral-small-2603", "tier": "sonnet", "tier_default": True},
            ],
            "branding_overrides": {},
            "_display_names": {"ollama/qwen3.5:4b": "Qwen 3.5 · 4B"},
            "api_key": "must-never-escape",
            "providers": {"mistral": {"api_key": "must-never-escape"}},
        }

    def test_exact_picker_ids_and_existing_brand_rules(self):
        settings = self.settings()
        projection = accent_catalogue(settings, active=True)
        qwen_id = claude_catalogue_rows(settings)[0]["id"]
        row = projection["models"][qwen_id]
        expected = resolve_presentation("ollama", "qwen3.5:4b")
        self.assertEqual(row, {"accent": expected["accent"], "modelLabel": "Qwen 3.5 · 4B"})
        self.assertEqual(projection["models"]["claude-haiku-4-5"], row)
        self.assertNotIn("must-never-escape", json.dumps(projection))
        self.assertEqual(set(projection), {"schema", "active", "gatewayUrl", "models"})

    def test_user_branding_and_mapping_mode(self):
        settings = self.settings()
        settings["claude_catalogue"] = None
        settings["mappings"] = {"claude-sonnet-5": "mistral/mistral-small-2603"}
        settings["branding_overrides"] = {"mistral": {"accent": "#123ABC"}}
        projection = accent_catalogue(settings, active=False)
        self.assertFalse(projection["active"])
        self.assertEqual(list(projection["models"]), ["claude-sonnet-5"])
        self.assertEqual(projection["models"]["claude-sonnet-5"]["accent"], "#123ABC")

    def test_refresh_requires_opt_in_and_never_follows_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / CATALOGUE_FILE
            self.assertFalse(refresh_accents(root, self.settings(), active=True))
            self.assertFalse(target.exists())
            target.write_text('{}')
            self.assertTrue(refresh_accents(root, self.settings(), active=True))
            self.assertTrue(json.loads(target.read_text())["active"])
            other = root / 'other.json'
            target.rename(other)
            target.symlink_to(other)
            self.assertFalse(refresh_accents(root, self.settings(), active=False))
            self.assertTrue(json.loads(other.read_text())["active"])


if __name__ == '__main__':
    unittest.main()
