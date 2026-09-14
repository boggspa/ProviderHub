"""Preview namespace and metadata regression checks; no real accounts used."""
import ast
import json
from pathlib import Path
import re
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

    def test_worker_bundle_lists_every_local_import(self):
        # The Preview app runs worker/gateway.py from its bundle, not from
        # this checkout. A worker module forgotten in build.sh bricks the
        # shipped gateway with ModuleNotFoundError, so the bundle list must
        # cover the import closure of everything it ships.
        source = Path(__file__).with_name("build.sh").read_text()
        match = re.search(r"^for module in ([^;]+); do", source, re.MULTILINE)
        self.assertIsNotNone(match, "build.sh worker module list not found")
        listed = set(match.group(1).split())
        self.assertIn("gateway", listed)
        for module in sorted(listed):
            self.assertTrue(
                Path(__file__).with_name(module + ".py").is_file(),
                f"build.sh lists {module} but Source/{module}.py is missing",
            )
        for module in sorted(listed):
            tree = ast.parse(Path(__file__).with_name(module + ".py").read_text())
            for node in ast.walk(tree):
                names = []
                if isinstance(node, ast.Import):
                    names = [alias.name.split(".")[0] for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    names = [node.module.split(".")[0]]
                for name in names:
                    if Path(__file__).with_name(name + ".py").is_file():
                        self.assertIn(
                            name, listed,
                            f"{module}.py imports sibling {name}, "
                            f"which build.sh does not bundle",
                        )

    def test_expired_mistral_ids_are_omitted_without_inference(self):
        cards = [{"id": name, "capabilities": {"completion_chat": True}, "max_context_length": 8192, **fields}
                 for name, fields in [("current", {}), ("expired", {"deprecation": "2020-01-01"}), ("future", {"deprecation": "2099-01-01"})]]
        inventory = discover("mistral", {}, "test-only", transport=lambda plan: {"data": cards})
        self.assertEqual({item["id"] for item in inventory["models"]}, {"current", "future"})


if __name__ == "__main__":
    unittest.main()
