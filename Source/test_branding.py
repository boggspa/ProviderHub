"""Offline tests for TaskWraith-compatible provider presentation data."""
import copy
import unittest

from branding import BrandingError, load_branding, resolve_presentation, validate_overrides


class BrandingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.catalogue = load_branding()

    def test_required_provider_defaults_use_reviewed_taskwraith_accents(self):
        expected = {
            "mistral": ("Mistral", "#D44404"),
            "kimi": ("Kimi", "#0073E6"),
            "mimo": ("MiMo", "#008844"),
            "ollama": ("Ollama", "#976C52"),
            "deepseek": ("DeepSeek", "#4E6AEE"),
            "cerebras": ("Cerebras", "#BB584A"),
            "grok": ("Grok", "#757575"),
            "muse": ("Muse", "#1671EA"),
            # Gemini wears Google's Antigravity green (TaskWraith's
            # --provider-antigravity-color), not the retired Gemini blue.
            "gemini": ("Gemini", "#308713"),
        }
        for provider, (label, accent) in expected.items():
            with self.subTest(provider=provider):
                presentation = resolve_presentation(provider, catalogue=self.catalogue)
                self.assertEqual(presentation["runtimeProvider"], provider)
                self.assertEqual(presentation["displayProvider"], label)
                self.assertEqual(presentation["accent"], accent)
        self.assertEqual(resolve_presentation("gemini", catalogue=self.catalogue)["hueKey"], "antigravity")
        self.assertEqual(self.catalogue["accents"]["gemini"], "#346EEC")  # the contract keeps the retired hue

    def test_model_brand_override_never_changes_runtime_identity(self):
        presentation = resolve_presentation(
            "ollama", "qwen3:8b", catalogue=self.catalogue)
        self.assertEqual(presentation["runtimeProvider"], "ollama")
        self.assertEqual(presentation["displayProvider"], "Alibaba")
        self.assertEqual(presentation["hueKey"], "alibaba")
        self.assertEqual(presentation["accent"], "#8C52EF")
        self.assertEqual(presentation["shortCode"], "QWN")
        self.assertEqual(presentation["model"], "qwen3:8b")
        self.assertEqual(presentation["modelLabel"], "Qwen")
        self.assertNotIn("logo", presentation)

        branded_logo = resolve_presentation(
            "ollama", "deepseek-r1:8b", catalogue=self.catalogue)
        self.assertEqual(branded_logo["runtimeProvider"], "ollama")
        self.assertEqual(branded_logo["displayProvider"], "DeepSeek")
        self.assertEqual(branded_logo["logo"], {
            "light": "provider-logos/provider-logo-deepseek.png",
            "dark": "provider-logos/provider-logo-deepseek.png",
        })

    def test_model_id_brand_match_wins_over_a_stale_label(self):
        presentation = resolve_presentation(
            "ollama", "deepseek-r1:8b", supplied_label="Old Qwen label",
            catalogue=self.catalogue)
        self.assertEqual(presentation["runtimeProvider"], "ollama")
        self.assertEqual(presentation["displayProvider"], "DeepSeek")
        self.assertEqual(presentation["hueKey"], "deepseek")

    def test_model_label_precedence_matches_taskwraith_contract(self):
        overrides = {"ollama": {"modelLabels": {"qwen3:8b": "Personal Qwen"}}}
        self.assertEqual(
            resolve_presentation("ollama", "qwen3:8b", overrides,
                                 catalogue=self.catalogue)["modelLabel"],
            "Personal Qwen",
        )
        self.assertEqual(
            resolve_presentation("ollama", "qwen3:8b", overrides, "Provider Qwen",
                                 catalogue=self.catalogue)["modelLabel"],
            "Provider Qwen",
        )
        self.assertEqual(
            resolve_presentation("muse", "muse-spark-1.3", catalogue=self.catalogue)["modelLabel"],
            "Muse Spark 1.3",
        )
        self.assertEqual(
            resolve_presentation("grok", "grok-4.6", catalogue=self.catalogue)["modelLabel"],
            "Grok 4.6",
        )
        self.assertEqual(
            resolve_presentation(
                "muse", "muse/muse-spark-1.3", catalogue=self.catalogue)["modelLabel"],
            "Muse Spark 1.3",
        )

    def test_sparse_user_override_controls_only_presentation(self):
        overrides = {
            "mistral": {
                "displayProvider": "Studio Mistral",
                "hueKey": "deepseek",
                "accent": "#123abc",
                "shortCode": "LAB",
                "logo": {"light": "provider-logos/lab.png", "scale": 1.08},
            }
        }
        presentation = resolve_presentation(
            "mistral", "mistral/small", overrides, catalogue=self.catalogue)
        self.assertEqual(presentation["runtimeProvider"], "mistral")
        self.assertEqual(presentation["displayProvider"], "Studio Mistral")
        self.assertEqual(presentation["hueKey"], "deepseek")
        self.assertEqual(presentation["accent"], "#123ABC")
        self.assertEqual(presentation["shortCode"], "LAB")
        self.assertEqual(presentation["logo"], {
            "light": "provider-logos/lab.png",
            "dark": "provider-logos/lab.png",
            "scale": 1.08,
        })

    def test_unknown_provider_falls_back_without_guessing_a_brand(self):
        presentation = resolve_presentation(
            "private-cloud", "owner/model-v1", catalogue=self.catalogue)
        self.assertEqual(presentation, {
            "runtimeProvider": "private-cloud",
            "displayProvider": "Private Cloud",
            "hueKey": "ensemble",
            "accent": "#986781",
            "shortCode": "PRI",
            "model": "owner/model-v1",
            "modelLabel": "owner/model-v1",
        })

    def test_overrides_are_normalized_without_mutating_settings(self):
        value = {"grok": {"accent": "#abcdef"}}
        before = copy.deepcopy(value)
        self.assertEqual(validate_overrides(value, self.catalogue), {
            "grok": {"accent": "#ABCDEF"},
        })
        self.assertEqual(value, before)

    def test_routing_and_unknown_fields_are_rejected(self):
        invalid = [
            {"mistral": {"runtimeProvider": "deepseek"}},
            {"mistral": {"apiBase": "https://example.invalid"}},
            {"mistral": {"hueKey": "unregistered"}},
        ]
        for override in invalid:
            with self.subTest(override=override), self.assertRaises(BrandingError):
                validate_overrides(override, self.catalogue)

    def test_bad_colours_text_assets_and_scale_fail_closed(self):
        invalid = [
            {"mistral": {"accent": "red"}},
            {"mistral": {"displayProvider": "Mistral\nInjected"}},
            {"mistral": {"shortCode": "lower"}},
            {"mistral": {"logo": {"dark": "dark.png"}}},
            {"mistral": {"logo": {"light": "../outside.png"}}},
            {"mistral": {"logo": {"light": "/tmp/logo.png"}}},
            {"mistral": {"logo": {"light": "logo.png", "scale": float("nan")}}},
        ]
        for override in invalid:
            with self.subTest(override=override), self.assertRaises(BrandingError):
                validate_overrides(override, self.catalogue)


if __name__ == "__main__":
    unittest.main()
