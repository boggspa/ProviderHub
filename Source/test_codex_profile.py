"""Catalogue and configuration transactions in disposable homes only."""
import copy
from pathlib import Path
import tempfile
import unittest

from bridge_core import BridgeError, SLOTS
from codex_catalogue import catalogue_digest, project_codex
from codex_profile import CodexProfile, parse, tomlkit
from hub_config import defaults


def fixture():
    settings = defaults(SLOTS, "mistral-medium-latest", 11438)
    settings["codex_model"] = "grok/grok-4.6"
    inventory = {"models": [{
        "id": "grok/grok-4.6", "display_name": "Grok 4.6", "provider_id": "grok",
        "context": 500000, "tools": True, "vision": True,
        "effort_modes": ["low", "medium", "high", "xhigh"],
        "presentation": {"displayProvider": "Grok"},
    }, {"id": "ollama/small-model:latest", "display_name": "Small Model",
        "context": 131072, "tools": True, "vision": False, "provider_id": "ollama"}]}
    return settings, inventory


class CodexCatalogueTests(unittest.TestCase):
    def test_uses_real_provider_routes_and_exact_independent_contexts(self):
        settings, inventory = fixture()
        catalogue = project_codex(settings, inventory)
        models = {model["slug"]: model for model in catalogue["models"]}
        self.assertEqual(set(models), {"grok/grok-4.6", "ollama/small-model:latest"})
        self.assertEqual(models["grok/grok-4.6"]["display_name"], "Grok 4.6")
        self.assertEqual(models["grok/grok-4.6"]["context_window"], 500000)
        self.assertEqual(models["ollama/small-model:latest"]["context_window"], 131072)
        self.assertEqual(models["grok/grok-4.6"]["service_tiers"][0]["id"], "priority")
        self.assertEqual(models["grok/grok-4.6"]["effective_context_window_percent"], 100)
        self.assertEqual(models["ollama/small-model:latest"]["supported_reasoning_levels"], [])
        self.assertTrue(all(model["apply_patch_tool_type"] is None for model in models.values()))

    def test_all_provider_catalogues_preserve_unknown_context_without_fabrication(self):
        settings, inventory = fixture()
        inventory["models"] += [{"id": "grok/unknown", "context": None},
                                {"id": "grok/no-tools", "context": 4096, "tools": False},
                                {"id": "mistral/a-model", "context": 1000000},
                                copy.deepcopy(inventory["models"][0])]
        result = project_codex(settings, inventory)
        self.assertEqual(len(result["models"]), 4)
        self.assertEqual(len(result["excluded"]), 1)
        unknown = next(model for model in result["models"] if model["slug"] == "grok/unknown")
        self.assertIsNone(unknown["context_window"])
        self.assertIsNone(unknown["auto_compact_token_limit"])

    def test_fingerprint_covers_metadata_and_credentials_without_secrets(self):
        settings, inventory = fixture()
        first = catalogue_digest(settings, inventory)
        changed = copy.deepcopy(inventory)
        changed["models"][0]["context"] += 1
        self.assertNotEqual(first, catalogue_digest(settings, changed))
        settings["providers"]["grok"]["credential_revision"] += 1
        self.assertNotEqual(first, catalogue_digest(settings, inventory))


class CodexProfileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.home = self.base / "codex"
        self.home.mkdir()
        self.root = self.base / "hub"
        self.manager = CodexProfile(self.root, config_home=self.home, running=lambda: False)
        self.settings, self.inventory = fixture()
        self.original = '''# Preserve my settings and formatting.
"model" = "my-usual-model" # selection
model_provider = "my-provider"
model_context_window = 200000
model_reasoning_effort = "medium"
notes = """A multiline string:
model = "this-is-not-a-setting"
"""

[model_providers.my-provider]
name = "Existing provider"
base_url = "https://example.invalid/v1"

[mcp_servers.local]
command = "existing-command"
'''
        self.manager.config.write_text(self.original)
        (self.home / "auth.json").write_text("PRIVATE-AUTH-SENTINEL")
        (self.home / "history.jsonl").write_text("PRIVATE-HISTORY-SENTINEL")

    def tearDown(self):
        self.assertEqual((self.home / "auth.json").read_text(), "PRIVATE-AUTH-SENTINEL")
        self.assertEqual((self.home / "history.jsonl").read_text(), "PRIVATE-HISTORY-SENTINEL")
        self.temp.cleanup()

    def activate(self):
        return self.manager.activate(self.settings, self.inventory)

    def edit(self, edit):
        doc = parse(self.manager.config.read_text())
        edit(doc)
        self.manager.config.write_text(tomlkit.dumps(doc))

    def test_switch_and_restore_preserve_original_bytes_and_unrelated_state(self):
        self.activate()
        updated = self.manager.config.read_text()
        doc = parse(updated)
        self.assertEqual(doc["model"], "grok/grok-4.6")
        self.assertEqual(doc["model_provider"], "provider_hub")
        self.assertNotIn("model_context_window", doc)
        self.assertNotIn("model_reasoning_effort", doc)
        self.assertIn('# Preserve my settings and formatting.', updated)
        self.assertIn('model = "this-is-not-a-setting"', updated)
        auth = doc["model_providers"]["provider_hub"]["auth"]
        self.assertEqual(auth["args"][:2], ["-I", "-B"])
        self.assertTrue(auth["args"][2].endswith("codex_token.py"))
        self.assertNotIn("PRIVATE-AUTH-SENTINEL", updated)
        self.assertEqual(self.manager.restore()["preserved_external_changes"], 0)
        self.assertEqual(self.manager.config.read_text(), self.original)
        self.assertFalse(self.manager.journal.exists())

    def test_unrelated_edits_after_launch_survive_restore(self):
        self.activate()
        self.edit(lambda doc: doc.update({"new_preference": "user-edit"}))
        self.manager.restore()
        doc = parse(self.manager.config.read_text())
        self.assertEqual(doc["model"], "my-usual-model")
        self.assertEqual(doc["new_preference"], "user-edit")
        self.assertNotIn("provider_hub", doc["model_providers"])

    def test_external_provider_switch_is_preserved_as_a_group(self):
        self.activate()
        self.edit(lambda doc: doc.update({"model_provider": "another-provider", "model": "another-model",
                                          "model_catalog_json": "/user/catalog.json"}))
        result = self.manager.restore()
        doc = parse(self.manager.config.read_text())
        self.assertGreater(result["preserved_external_changes"], 0)
        self.assertEqual(doc["model_provider"], "another-provider")
        self.assertEqual(doc["model"], "another-model")
        self.assertEqual(doc["model_catalog_json"], "/user/catalog.json")

    def test_hub_picker_change_restores_the_pre_hub_selection(self):
        self.activate()
        self.edit(lambda doc: doc.update({"model": "ollama/small-model:latest"}))
        self.manager.restore()
        self.assertEqual(parse(self.manager.config.read_text())["model"], "my-usual-model")

    def test_unknown_selection_under_hub_is_not_silently_retargeted(self):
        self.activate()
        self.edit(lambda doc: doc.update({"model": "external-model"}))
        current = self.manager.config.read_text()
        with self.assertRaises(BridgeError):
            self.manager.restore()
        self.assertEqual(self.manager.config.read_text(), current)
        self.assertTrue(self.manager.journal.exists())

    def test_external_provider_definition_edits_survive(self):
        self.activate()
        self.edit(lambda doc: doc["model_providers"]["provider_hub"].update({"name": "User renamed it"}))
        self.manager.restore()
        self.assertEqual(parse(self.manager.config.read_text())["model_providers"]["provider_hub"]["name"], "User renamed it")

    def test_existing_foreign_stanza_and_malformed_toml_are_not_overwritten(self):
        self.manager.config.write_text(self.original + '\n[model_providers.provider_hub]\nname="Already mine"\n')
        original = self.manager.config.read_text()
        with self.assertRaises(BridgeError):
            self.activate()
        self.assertEqual(self.manager.config.read_text(), original)
        self.manager.config.write_text('broken = "')
        with self.assertRaises(BridgeError):
            self.activate()
        self.assertEqual(self.manager.config.read_text(), 'broken = "')

    def test_running_app_blocks_switch_and_restore(self):
        blocked = CodexProfile(self.root, config_home=self.home, running=lambda: True)
        with self.assertRaises(BridgeError):
            blocked.activate(self.settings, self.inventory)
        self.activate()
        with self.assertRaises(BridgeError):
            blocked.restore()
        self.assertTrue(self.manager.journal.exists())

    def test_missing_original_config_is_removed_on_restore(self):
        self.manager.config.unlink()
        self.activate()
        self.manager.restore()
        self.assertFalse(self.manager.config.exists())

    def test_interrupted_before_write_recovers_without_changing_original(self):
        self.activate()
        self.manager.config.write_text(self.original)
        self.manager.restore()
        self.assertEqual(self.manager.config.read_text(), self.original)


if __name__ == "__main__":
    unittest.main()
