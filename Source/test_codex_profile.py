"""Catalogue and configuration transactions in disposable homes only."""
import copy
import json
from pathlib import Path
import tempfile
import unittest

from branding import resolve_presentation
from bridge_core import BridgeError, SLOTS
from codex_catalogue import catalogue_digest, project_codex
from codex_profile import BEARER_PLACEHOLDER, ROOT_KEYS, CodexProfile, digest, parse, redact_provider, tomlkit, value_at
from hub_config import defaults


def fixture():
    settings = defaults(SLOTS, "mistral-medium-latest", 11438)
    settings["codex_model"] = "grok/grok-4.6"
    inventory = {"models": [{
        "id": "grok/grok-4.6", "display_name": "Grok 4.6", "provider_id": "grok",
        "context": 500000, "tools": True, "vision": True, "reasoning": True,
        "effort_modes": ["low", "medium", "high", "xhigh"],
        "fast_mode": True,
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
        self.assertEqual(models["grok/grok-4.6"]["input_modalities"], ["text", "image"])
        # Ollama is exempt from a catalogue vision:false, here exactly as it is
        # in the request path: its own endpoints accept or reject images, and
        # test_responses_native asserts the gateway forwards a screenshot to
        # this very shape of route. Advertising text-only contradicted that and
        # left the composer refusing an attachment the gateway would carry.
        self.assertEqual(models["ollama/small-model:latest"]["input_modalities"], ["text", "image"])
        self.assertTrue(all(model["apply_patch_tool_type"] is None for model in models.values()))

    def test_unknown_vision_advertises_image_input_to_the_desktop(self):
        """A CLI route whose adapter never reports modalities must not read as a denial.

        Every CLI-backed provider ships an image transport, so the gateway
        delivers these images. Demanding an explicit True here made the
        composer refuse to attach them, and the model then reported that its
        environment prevented it from seeing images.
        """
        settings, inventory = fixture()
        inventory["models"] += [
            {"id": "claude/opus-5", "display_name": "Opus 5", "context": 200000,
             "tools": True, "vision": None, "provider_id": "claude"},
            {"id": "antigravity/gemini-3.8-flash", "display_name": "Gemini 3.8 Flash",
             "context": 1000000, "tools": True, "provider_id": "antigravity"},
        ]
        models = {model["slug"]: model for model in project_codex(settings, inventory)["models"]}
        self.assertEqual(models["claude/opus-5"]["input_modalities"], ["text", "image"])
        self.assertEqual(models["antigravity/gemini-3.8-flash"]["input_modalities"], ["text", "image"])

    def test_confirmed_text_only_route_still_advertises_text_only(self):
        settings, inventory = fixture()
        inventory["models"] += [{"id": "mistral/glm-5-2", "display_name": "GLM 5.2",
                                 "context": 131072, "tools": True, "vision": False,
                                 "provider_id": "mistral"}]
        models = {model["slug"]: model for model in project_codex(settings, inventory)["models"]}
        self.assertEqual(models["mistral/glm-5-2"]["input_modalities"], ["text"])

    def test_advertised_modality_matches_what_the_request_path_accepts(self):
        """The two sides must answer one question, or the composer and the gateway disagree."""
        from catalogue import image_input_blocked
        from provider_requests import _image_input_rejected
        settings, inventory = fixture()
        inventory["models"] += [
            {"id": "claude/opus-5", "context": 200000, "tools": True, "vision": None, "provider_id": "claude"},
            {"id": "mistral/glm-5-2", "context": 131072, "tools": True, "vision": False, "provider_id": "mistral"},
        ]
        by_slug = {entry["id"]: entry for entry in inventory["models"]}
        for model in project_codex(settings, inventory)["models"]:
            entry = by_slug[model["slug"]]
            provider_id = entry.get("provider_id") or model["slug"].split("/")[0]
            advertises = "image" in model["input_modalities"]
            self.assertEqual(advertises, not _image_input_rejected(provider_id, entry), model["slug"])
            self.assertEqual(advertises, not image_input_blocked(provider_id, entry), model["slug"])

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

    def test_projection_flags_desktop_baseline_fit(self):
        settings, inventory = fixture()
        inventory["models"] += [
            {"id": "ollama/tiny:latest", "display_name": "Tiny", "context": 32768,
             "tools": True, "provider_id": "ollama"},
            {"id": "ollama/mystery:latest", "display_name": "Mystery",
             "tools": True, "provider_id": "ollama"},
        ]
        models = {model["slug"]: model for model in project_codex(settings, inventory)["models"]}
        self.assertTrue(models["grok/grok-4.6"]["fits_desktop_baseline"])
        self.assertNotIn("below the desktop baseline", models["grok/grok-4.6"]["description"])
        self.assertIs(models["ollama/tiny:latest"]["fits_desktop_baseline"], False)
        self.assertIn("below the desktop baseline", models["ollama/tiny:latest"]["description"])
        self.assertIsNone(models["ollama/mystery:latest"]["fits_desktop_baseline"])
        self.assertNotIn("below the desktop baseline", models["ollama/mystery:latest"]["description"])

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

    def test_codex_search_is_on_whenever_any_route_can_serve_it(self):
        """Once search is on, Codex offers the hosted tool on every row, and an
        in-app model switch never re-runs activation, so the setting follows
        the whole catalogue: live if any route can search (the gateway drops
        the tool on the rest), disabled when none can. A searching route that
        is not the launch model must still be able to search. Either value is
        journalled, so the user's own setting still returns."""
        self.activate()
        self.assertEqual(parse(self.manager.config.read_text())["web_search"], "disabled")
        self.manager.restore()
        searching = copy.deepcopy(self.inventory)
        for entry in searching["models"]:
            if entry["id"] != self.settings["codex_model"]:
                entry["web_search"] = True
        self.manager.activate(self.settings, searching)
        self.assertEqual(parse(self.manager.config.read_text())["web_search"], "live")
        self.manager.restore()
        self.assertNotIn("web_search", parse(self.manager.config.read_text()))

    def test_no_root_compaction_limit_overrides_the_catalogue_rows(self):
        """A root model_auto_compact_token_limit applies to every model in a
        thread, so the starting model's value would compact a 1M route at a
        smaller model's threshold after an in-app switch. Activation leaves
        none (each catalogue row carries its own), and the user's own value
        returns on restore."""
        self.edit(lambda doc: doc.__setitem__("model_auto_compact_token_limit", 123456))
        self.activate()
        rows = json.loads(self.manager.catalogue.read_text())["models"]
        starting = next(row for row in rows if row["slug"] == self.settings["codex_model"])
        self.assertIsInstance(starting["auto_compact_token_limit"], int)  # the value that used to leak
        self.assertNotIn("model_auto_compact_token_limit", parse(self.manager.config.read_text()))
        self.manager.restore()
        self.assertEqual(parse(self.manager.config.read_text())["model_auto_compact_token_limit"], 123456)

    def test_switch_and_restore_preserve_original_bytes_and_unrelated_state(self):
        self.activate()
        updated = self.manager.config.read_text()
        doc = parse(updated)
        self.assertEqual(doc["model"], "grok/grok-4.6")
        self.assertEqual(doc["model_provider"], "provider_hub")
        self.assertNotIn("model_context_window", doc)
        self.assertNotIn("model_reasoning_effort", doc)
        # Multi-agent keys are owned: the selected model advertises v2, so
        # features.multi_agent_v2 is written on activation and restored to its
        # prior state on restore. The subagent defaults are owned in the other
        # direction - cleared, never written - because they are resolved once
        # here while the model is switched in the app, so a pinned target goes
        # stale the moment the route changes. Cleared, Codex's own rule applies
        # and a sub-agent inherits the thread's model.
        self.assertNotIn("default_subagent_model", doc)
        self.assertNotIn("default_subagent_reasoning_effort", doc)
        self.assertIn("multi_agent_v2", doc["features"])
        self.assertTrue(doc["features"]["multi_agent_v2"])
        self.assertIn('# Preserve my settings and formatting.', updated)
        self.assertIn('model = "this-is-not-a-setting"', updated)
        auth = doc["model_providers"]["provider_hub"]["auth"]
        self.assertEqual(auth["args"][:2], ["-I", "-B"])
        self.assertTrue(auth["args"][2].endswith("codex_token.py"))
        self.assertNotIn("PRIVATE-AUTH-SENTINEL", updated)
        self.assertEqual(self.manager.restore()["preserved_external_changes"], 0)
        self.assertEqual(self.manager.config.read_text(), self.original)
        self.assertFalse(self.manager.journal.exists())

    def test_default_mode_keeps_command_backed_auth_without_a_login_requirement(self):
        self.activate()
        entry = parse(self.manager.config.read_text())["model_providers"]["provider_hub"]
        self.assertIn("auth", entry)
        self.assertNotIn("requires_openai_auth", entry)
        self.assertNotIn("experimental_bearer_token", entry)
        self.assertFalse((self.root / "gateway-token").exists())

    def test_chatgpt_account_mode_presents_the_login_without_journaling_the_bearer(self):
        self.settings["codex_chatgpt_account"] = True
        self.activate()
        updated = self.manager.config.read_text()
        entry = parse(updated)["model_providers"]["provider_hub"]
        token = (self.root / "gateway-token").read_text().strip()
        self.assertGreaterEqual(len(token), 32)
        self.assertIs(entry["requires_openai_auth"], True)
        self.assertEqual(entry["experimental_bearer_token"], token)
        self.assertNotIn("auth", entry)
        self.assertEqual(entry["base_url"], "http://127.0.0.1:11438/v1")
        self.assertEqual(entry["wire_api"], "responses")
        journal = self.manager.journal.read_text()
        self.assertNotIn(token, journal)
        self.assertEqual(json.loads(journal)["provider"]["experimental_bearer_token"], BEARER_PLACEHOLDER)
        self.assertEqual(redact_provider(entry.unwrap()), json.loads(journal)["provider"])
        self.assertNotIn("PRIVATE-AUTH-SENTINEL", updated)
        self.assertEqual(self.manager.restore()["preserved_external_changes"], 0)
        self.assertEqual(self.manager.config.read_text(), self.original)
        self.assertFalse(self.manager.journal.exists())

    def test_chatgpt_account_mode_restores_after_the_bearer_rotates(self):
        self.settings["codex_chatgpt_account"] = True
        self.activate()

        def rotate(doc):
            doc["model_providers"]["provider_hub"]["experimental_bearer_token"] = "rotated-elsewhere"
        self.edit(rotate)
        self.assertEqual(self.manager.restore()["preserved_external_changes"], 0)
        restored = self.manager.config.read_text()
        doc = parse(restored)
        self.assertNotIn("provider_hub", doc["model_providers"])
        self.assertEqual(doc["model"], "my-usual-model")
        self.assertEqual(doc["model_provider"], "my-provider")
        self.assertNotIn("rotated-elsewhere", restored)

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

    def test_non_reasoning_model_writes_no_subagent_keys(self):
        self.settings["codex_model"] = "ollama/small-model:latest"
        self.activate()
        doc = parse(self.manager.config.read_text())
        self.assertNotIn("default_subagent_model", doc)
        self.assertNotIn("default_subagent_reasoning_effort", doc)
        # features.multi_agent_v2 is not written for a non-reasoning route.
        if "features" in doc:
            self.assertNotIn("multi_agent_v2", doc["features"])
        self.manager.restore()
        self.assertEqual(self.manager.config.read_text(), self.original)

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

    def test_activate_leaves_desktop_chrome_theme_untouched(self):
        # The desktop chrome theme is global to the app and has no live-update
        # path: activation requires a quit app, and in-app model switches never
        # re-run activation. The hub therefore owns no theme keys: a prior
        # custom accent survives byte-wise and none is written when absent.
        self.manager.config.write_text(
            self.original + '\n[desktop.appearanceDarkChromeTheme]\naccent = "#123456"\naccentSource = "custom"\n')
        original = self.manager.config.read_text()
        result = self.activate()
        self.assertNotIn("codex_accent", result)
        doc = parse(self.manager.config.read_text())
        self.assertEqual(doc["desktop"]["appearanceDarkChromeTheme"]["accent"], "#123456")
        self.assertEqual(doc["desktop"]["appearanceDarkChromeTheme"]["accentSource"], "custom")
        self.assertNotIn("appearanceLightChromeTheme", doc["desktop"])
        self.manager.restore()
        self.assertEqual(self.manager.config.read_text(), original)

    def test_legacy_hub_accent_is_removed_on_restore(self):
        # Journals written while the hub owned the desktop chrome theme still
        # restore: the hub accent is removed and the prior theme returns.
        hub_accent = resolve_presentation("grok", "grok-4.6")["accent"]
        original = self.original + '\n[desktop.appearanceDarkChromeTheme]\naccent = "#123456"\n'
        current = ('model = "grok/grok-4.6"\nmodel_provider = "provider_hub"\n'
                   'model_catalog_json = "/tmp/codex-models.json"\nweb_search = "disabled"\n'
                   'default_subagent_model = "grok/grok-4.6"\ndefault_subagent_reasoning_effort = "xhigh"\n'
                   '\n[features]\nmulti_agent_v2 = true\n'
                   '\n[desktop.appearanceLightChromeTheme]\naccent = "%s"\naccentSource = "custom"\n'
                   '\n[desktop.appearanceDarkChromeTheme]\naccent = "%s"\naccentSource = "custom"\n'
                   '\n[model_providers.provider_hub]\nname = "Provider Hub"\n') % (hub_accent, hub_accent)
        before_doc, applied_doc = parse(original), parse(current)
        journal = {
            "version": 2, "config_path": str(self.manager.config), "existed": True,
            "mode": 0o600,
            "before": {key: value_at(before_doc, key) for key in ROOT_KEYS},
            "applied": {key: value_at(applied_doc, key) for key in ROOT_KEYS},
            "nested_before": {
                "features.multi_agent_v2": {"present": False, "value": None},
                "desktop.appearanceLightChromeTheme.accent": {"present": False, "value": None},
                "desktop.appearanceLightChromeTheme.accentSource": {"present": False, "value": None},
                "desktop.appearanceDarkChromeTheme.accent": {"present": True, "value": "#123456"},
                "desktop.appearanceDarkChromeTheme.accentSource": {"present": False, "value": None},
            },
            "nested_applied": {
                "features.multi_agent_v2": True,
                "desktop.appearanceLightChromeTheme.accent": hub_accent,
                "desktop.appearanceLightChromeTheme.accentSource": "custom",
                "desktop.appearanceDarkChromeTheme.accent": hub_accent,
                "desktop.appearanceDarkChromeTheme.accentSource": "custom",
            },
            "providers_existed": False,
            "provider": {"name": "Provider Hub"},
            "before_digest": digest(original),
            "applied_digest": "legacy-hub-applied-state",
        }
        self.manager.root.mkdir(parents=True, exist_ok=True)
        self.manager.backup.write_text(original)
        self.manager.config.write_text(current)
        self.manager.catalogue.write_text(json.dumps({"models": [{"slug": "grok/grok-4.6"}]}))
        self.manager.journal.write_text(json.dumps(journal))
        result = self.manager.restore()
        self.assertTrue(result["restored"])
        doc = parse(self.manager.config.read_text())
        self.assertEqual(doc["desktop"]["appearanceDarkChromeTheme"]["accent"], "#123456")
        self.assertNotIn("accentSource", doc["desktop"]["appearanceDarkChromeTheme"])
        self.assertNotIn("accent", doc["desktop"]["appearanceLightChromeTheme"])


if __name__ == "__main__":
    unittest.main()
