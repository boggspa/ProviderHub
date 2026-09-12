"""Preview namespace and metadata regression checks; no real accounts used."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from bridge_core import atomic_json, cached_catalogue, default_settings, gateway_token, private_token
from hub_config import connection_signature, project_catalogue
from providers import discover


class PreviewTests(unittest.TestCase):
    def test_signing_key_is_private_distinct_and_survives_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            token = gateway_token(root)
            signing = private_token(root, "reasoning-signing-key")
            self.assertNotEqual(token, signing)
            self.assertEqual(signing, private_token(root, "reasoning-signing-key"))
            self.assertEqual((root / "reasoning-signing-key").stat().st_mode & 0o777, 0o600)

    def test_unsigned_account_catalogue_cannot_supply_model_routes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = default_settings()
            inventory = {"provider_id": "deepseek", "models": [{"id": "test-model", "context": 8192}]}
            atomic_json(root / "catalogues/deepseek.json", inventory)
            with patch("bridge_core.vibe_settings", return_value={}):
                self.assertEqual(cached_catalogue(settings, root)["models"], [])
                inventory["connection_signature"] = connection_signature("deepseek", settings["providers"]["deepseek"])
                atomic_json(root / "catalogues/deepseek.json", inventory)
                self.assertEqual(len(cached_catalogue(settings, root)["models"]), 1)
                settings["providers"]["deepseek"]["credential_revision"] += 1
                self.assertEqual(cached_catalogue(settings, root)["models"], [])

    def test_fallback_labels_humanize_raw_ids_without_changing_routes(self):
        settings = default_settings()
        inventory = {"provider_id": "ollama", "models": [{"id": "gpt-oss:120b-cloud", "display_name": "gpt-oss:120b-cloud", "context": 131072}]}
        entry = project_catalogue("ollama", inventory, settings)[0]
        self.assertEqual(entry["id"], "ollama/gpt-oss:120b-cloud")
        self.assertEqual(entry["model_id"], "gpt-oss:120b-cloud")
        self.assertEqual(entry["display_name"], "GPT OSS · 120B Cloud")
        kimi = project_catalogue("kimi", {"models": [{"id": "k3", "display_name": "K3"}]}, settings)[0]
        self.assertEqual(kimi["display_name"], "K3")

    def test_expired_mistral_ids_are_omitted_without_inference(self):
        cards = [{"id": name, "capabilities": {"completion_chat": True}, "max_context_length": 8192, **fields}
                 for name, fields in [("current", {}), ("expired", {"deprecation": "2020-01-01"}), ("future", {"deprecation": "2099-01-01"})]]
        inventory = discover("mistral", {}, "test-only", transport=lambda plan: {"data": cards})
        self.assertEqual({item["id"] for item in inventory["models"]}, {"current", "future"})


if __name__ == "__main__":
    unittest.main()
