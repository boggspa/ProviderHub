"""Claude Desktop context propagation, with disposable homes and no inference."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from bridge_core import BridgeError, ClaudeProfile, SLOTS, atomic_json, read_json
from catalogue import route_specs
from claude_context import claude_context_spec, codex_context_window
from codex_catalogue import project_codex
from gateway import Runtime
from hub_config import claude_catalogue_rows, defaults, project_catalogue
from protocol import estimated_tokens, model_catalog


CODEX_MODELS = ("gpt-5.6-luna", "gpt-5.6-terra", "gpt-5.6-sol", "gpt-6-astra",
                "gpt-6-luna", "gpt-6-sol")
GEMINI_MODELS = ("gemini-3.1-pro", "gemini-3.6-flash", "gemini-3.7-flash", "gemini-3.8-flash")


class ClaudeContextTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name)
        self.codex = self.home / ".codex"
        self.codex.mkdir()
        home_patch = patch("claude_context.Path.home", return_value=self.home)
        home_patch.start()
        self.addCleanup(home_patch.stop)
        self.settings = defaults(SLOTS, "mistral-test")
        self.settings["providers"]["codex"]["credential_mode"] = "cli"

    def configure(self, text):
        (self.codex / "config.toml").write_text(text)

    def catalogue(self, provider="codex", models=CODEX_MODELS, context=None):
        raw = {"source": "cli", "models": [
            {"id": model, "display_name": model, "context": context,
             "aliases": [model], "tools": True, "reasoning": True, "effort_modes": ["high"]}
            for model in models]}
        inventory = {"models": project_catalogue(provider, raw, self.settings)}
        self.settings["claude_catalogue"] = [
            {"route": row["id"], "tier": "opus", "tier_default": index == 0}
            for index, row in enumerate(inventory["models"])]
        self.settings["_model_specs"] = route_specs(inventory)
        self.settings["_display_names"] = {row["id"]: row["display_name"] for row in inventory["models"]}
        return inventory

    def runtime(self, inventory):
        with patch("gateway.load_settings", return_value=self.settings), \
                patch("gateway.attach_model_specs", return_value=(self.settings, inventory)):
            return Runtime(self.home / "hub", key="")

    def test_configured_window_reaches_every_desktop_row_without_duplicates(self):
        self.configure("model_context_window = 1_000_000\nmodel_auto_compact_token_limit = 900_000\n")
        self.catalogue()
        rows = model_catalog(self.settings)["data"]
        self.assertEqual(len(rows), len(CODEX_MODELS))
        self.assertEqual(len({row["id"] for row in rows}), len(CODEX_MODELS))
        for row in rows:
            self.assertTrue(row["id"].endswith("[1m]"))
            self.assertEqual(row["max_tokens"], 1_000_000)
            self.assertEqual(row["max_input_tokens"], 1_000_000)
            self.assertIn("1,000,000 token context", row["description"])
            self.assertIs(row["supports_1m"], False)

    def test_absent_setting_uses_256k_without_advertising_1m(self):
        self.catalogue()
        for row in model_catalog(self.settings)["data"]:
            self.assertEqual(row["max_tokens"], 256_000)
            self.assertFalse(row["id"].endswith("[1m]"))

    def test_absent_setting_keeps_the_runtime_catalogue_window(self):
        self.catalogue(context=872_000)
        for row in model_catalog(self.settings)["data"]:
            self.assertEqual(row["max_tokens"], 872_000)

    def test_saved_catalogues_gain_documented_context_without_refresh(self):
        self.settings["providers"]["claude"]["credential_mode"] = "cli"
        cases = (
            ("claude", "fable", 1_000_000),
            ("claude", "opus", 1_000_000),
            ("claude", "claude-opus-5-5", 1_000_000),
            ("claude", "claude-opus-5", 1_000_000),
            ("claude", "sonnet", 1_000_000),
            ("claude", "claude-sonnet-5-5", 1_000_000),
            ("claude", "claude-sonnet-5", 1_000_000),
            ("claude", "claude-fable-5", 1_000_000),
            ("claude", "haiku", 1_000_000),
            ("claude", "claude-haiku-5-5", 1_000_000),
            ("claude", "claude-haiku-4-5", 200_000),
            ("deepseek", "deepseek-v4-pro", 1_048_576),
            ("deepseek", "deepseek-flash", 1_048_576),
            ("grok", "grok-4.7", 500_000),
            ("grok", "grok-4.7-build-fast", 500_000),
            ("grok", "grok-4.6", 500_000),
            ("grok", "grok-4.5", 500_000),
            ("qwen-token-plan", "qwen3.8-flash", 1_000_000),
        )
        for provider, model, expected in cases:
            with self.subTest(provider=provider, model=model):
                route = f"{provider}/{model}"
                # Model an old saved snapshot directly: project_catalogue
                # would already replace legacy Claude aliases with versions.
                self.settings["claude_catalogue"] = [
                    {"route": route, "tier": "opus", "tier_default": True}]
                self.settings["_model_specs"] = {route: {
                    "id": route, "provider_id": provider, "model_id": model,
                    "display_name": model, "context": None,
                    "inference_status": "advertised"}}
                original = copy.deepcopy(self.settings)
                rows = model_catalog(self.settings)["data"]
                picker_id = next(item["id"] for item in claude_catalogue_rows(self.settings)
                                 if item["route"] == f"{provider}/{model}")
                row = next(row for row in rows if row["id"].removesuffix("[1m]") == picker_id)
                self.assertEqual(row["max_tokens"], expected)
                self.assertEqual(row["description"], f"{expected:,} token context")
                self.assertEqual(row["id"].endswith("[1m]"), expected >= 1_000_000)
                self.assertEqual(self.settings, original)

    def test_published_context_fallback_does_not_guess_or_replace_reported_limits(self):
        for provider, model in (("claude", "future-model"), ("grok", "grok-latest"),
                                ("deepseek", "deepseek-future"), ("ollama", "grok-4.6")):
            spec = {"provider_id": provider, "model_id": model, "context": None}
            with self.subTest(provider=provider, model=model):
                self.assertIs(claude_context_spec(spec, self.settings), spec)
        for extra in ({"context": 123_456}, {"context_options": [200_000, 1_000_000]}):
            spec = {"provider_id": "grok", "model_id": "grok-4.6", **extra}
            self.assertIs(claude_context_spec(spec, self.settings), spec)

    def test_picker_subtitles_contain_only_context_even_for_small_or_unknown_models(self):
        for context, expected in ((32_768, "32,768 token context"),
                                  (None, "Provider-managed context")):
            self.catalogue("mistral", ("custom-model",), context)
            spec = self.settings["_model_specs"]["mistral/custom-model"]
            spec["inference_status"] = "responded"
            row = model_catalog(self.settings)["data"][0]
            self.assertEqual(row["description"], expected)
            self.assertIn("anthropic_family_tier", row)
            self.assertIn("fits_desktop_baseline", row)

    def test_invalid_missing_or_unreadable_toml_does_not_override_context(self):
        for text in ("", "broken = [", 'model_context_window = "1000000"',
                     "model_context_window = true", "model_context_window = 0",
                     "model_context_window = -1", "model_context_window = 1000000.0"):
            with self.subTest(text=text):
                self.configure(text)
                self.assertIsNone(codex_context_window())
        with patch("claude_context.Path.read_text", side_effect=PermissionError):
            self.assertIsNone(codex_context_window())
        (self.codex / "config.toml").write_bytes(b"\xff")
        self.assertIsNone(codex_context_window())

    def test_nested_settings_and_compaction_are_not_the_context_window(self):
        self.configure('''model_auto_compact_token_limit = 900_000
[projects."/tmp/example"]
model_context_window = 123_456
[profiles.unselected]
model_context_window = 1_000_000
''')
        self.assertIsNone(codex_context_window())

    def test_selected_profile_overrides_root_in_both_codex_formats(self):
        self.configure('''profile = "long"
model_context_window = 256_000
[profiles.long]
model_context_window = 800_000
''')
        self.assertEqual(codex_context_window(), 800_000)
        (self.codex / "long.config.toml").write_text("model_context_window = 1_000_000\n")
        self.assertEqual(codex_context_window(), 1_000_000)

    def test_profile_cannot_read_outside_codex_home(self):
        self.configure('profile = "../elsewhere"\nmodel_context_window = 256_000\n')
        (self.home / "elsewhere.config.toml").write_text("model_context_window = 1_000_000\n")
        self.assertEqual(codex_context_window(), 256_000)

    def test_home_matches_cli_transport_even_with_parent_codex_home_override(self):
        self.configure("model_context_window = 1_000_000\n")
        other = self.home / "other"
        other.mkdir()
        (other / "config.toml").write_text("model_context_window = 123_456\n")
        with patch.dict("os.environ", {"CODEX_HOME": str(other)}):
            self.assertEqual(codex_context_window(), 1_000_000)

    def test_cached_discovery_does_not_mask_subsequent_config_changes(self):
        inventory = self.catalogue(context=200_000)
        before = copy.deepcopy(inventory)
        self.configure("model_context_window = 1_000_000\n")
        self.assertEqual(model_catalog(self.settings)["data"][0]["max_tokens"], 1_000_000)
        self.configure("model_context_window = 512_000\n")
        row = model_catalog(self.settings)["data"][0]
        self.assertEqual(row["max_tokens"], 512_000)
        self.assertFalse(row["id"].endswith("[1m]"))
        self.assertEqual(inventory, before)

    def test_antigravity_gemini_families_and_native_effort_ids_get_1m(self):
        for suffix in ("", "-low", "-medium", "-high"):
            with self.subTest(suffix=suffix):
                self.catalogue("antigravity", tuple(model + suffix for model in GEMINI_MODELS), 200_000)
                rows = model_catalog(self.settings)["data"]
                self.assertEqual(len(rows), 4)
                for row in rows:
                    self.assertEqual(row["max_tokens"], 1_000_000)
                    self.assertTrue(row["id"].endswith("[1m]"))

    def test_other_models_and_codex_api_routes_keep_their_metadata(self):
        self.configure("model_context_window = 1_000_000\n")
        for provider, model in (("antigravity", "claude-opus-5.5"),
                                ("antigravity", "gpt-oss-120b"),
                                ("antigravity", "gemini-future"),
                                ("gemini", "gemini-3.8-flash")):
            with self.subTest(provider=provider, model=model):
                self.catalogue(provider, (model,), 123_456)
                self.assertEqual(model_catalog(self.settings)["data"][0]["max_tokens"], 123_456)
        self.catalogue(context=272_000)
        self.settings["providers"]["codex"]["credential_mode"] = "environment"
        self.assertEqual({row["max_tokens"] for row in model_catalog(self.settings)["data"]}, {272_000})

    def test_explicit_codex_override_applies_to_other_cli_models_without_guessing_defaults(self):
        self.catalogue("codex", ("gpt-5.5", "custom-model"), 272_000)
        self.assertEqual({row["max_tokens"] for row in model_catalog(self.settings)["data"]}, {272_000})
        self.configure("model_context_window = 1_000_000\n")
        self.assertEqual({row["max_tokens"] for row in model_catalog(self.settings)["data"]}, {1_000_000})

    def test_claude_projection_does_not_change_codex_desktop_or_shared_specs(self):
        self.configure("model_context_window = 1_000_000\n")
        for provider, models in (("codex", CODEX_MODELS), ("antigravity", GEMINI_MODELS)):
            inventory = self.catalogue(provider, models)
            original = copy.deepcopy(self.settings)
            codex_before = project_codex(self.settings, inventory)
            model_catalog(self.settings)
            self.assertEqual(self.settings, original)
            self.assertEqual(project_codex(self.settings, inventory), codex_before)
            self.assertEqual({row["context_window"] for row in codex_before["models"]}, {None})

    def test_resolved_window_clears_stale_ambiguous_identity(self):
        self.configure("model_context_window = 1_000_000\n")
        spec = {"id": "codex/gpt-6-astra", "provider_id": "codex",
                "context_options": [200_000, 256_000]}
        resolved = claude_context_spec(spec, self.settings)
        self.assertNotIn("context_options", resolved)
        self.assertIn("context_options", spec)

    def test_managed_code_picker_matches_discovery_and_restores_user_settings(self):
        self.configure("model_context_window = 1_000_000\n")
        self.catalogue()
        profile = ClaudeProfile(self.home / "hub", self.home / "support", self.home / "claude")
        original = {"theme": "dark", "modelPicker": {"options": [{"model": "my-model", "label": "Mine"}]}}
        atomic_json(profile.code_settings, original)
        profile.activate(self.settings, "local-test-token", require_closed=False)
        rows = read_json(profile.code_settings)["modelPicker"]["options"][1:]
        self.assertEqual({row["model"] for row in rows}, {row["id"] for row in model_catalog(self.settings)["data"]})
        self.assertEqual({row["behavesAs"] for row in rows}, {"claude-opus-5[1m]"})
        self.assertTrue(profile.restore(require_closed=False)["restored"])
        self.assertEqual(read_json(profile.code_settings), original)

    def test_messages_planning_uses_1m_for_admission_identity_and_compaction(self):
        self.configure("model_context_window = 1_000_000\n")
        for provider, model in (("codex", "gpt-6-astra"), ("antigravity", "gemini-3.8-flash")):
            with self.subTest(provider=provider):
                inventory = self.catalogue(provider, (model,), 200_000)
                runtime = self.runtime(inventory)
                selected = model_catalog(self.settings)["data"][0]["id"]
                payload = {"model": selected, "max_tokens": 4096,
                           "messages": [{"role": "user", "content": "a" * 1_000_000}]}
                self.assertGreater(estimated_tokens(payload), 200_000)
                self.assertLess(estimated_tokens(payload), 850_000)
                plan = runtime.plan(payload)
                self.assertEqual(plan["model_spec"]["context"], 1_000_000)
                self.assertNotIn("auto_compact", plan.get("compatibility", {}))
                self.assertIn("context window is 1,000,000 tokens", json.dumps(plan["body"]))
                self.assertEqual(plan["upstream_model"], model)

    def test_responses_bridge_keeps_original_context_metadata(self):
        self.configure("model_context_window = 1_000_000\n")
        inventory = self.catalogue(context=200_000)
        runtime = self.runtime(inventory)
        plan = runtime.plan({"model": "codex/gpt-6-astra", "max_tokens": 4096,
                             "_provider_hub_surface": "responses",
                             "messages": [{"role": "user", "content": "Hello"}]})
        self.assertEqual(plan["model_spec"]["context"], 200_000)

    def test_reduced_configuration_rejects_stale_1m_selection(self):
        self.configure("model_context_window = 256_000\n")
        runtime = self.runtime(self.catalogue())
        selected = claude_catalogue_rows(self.settings)[0]["id"] + "[1m]"
        with self.assertRaisesRegex(BridgeError, "1M context window has not been established"):
            runtime.plan({"model": selected, "max_tokens": 4096,
                          "messages": [{"role": "user", "content": "Hello"}]})


if __name__ == "__main__":
    unittest.main()
