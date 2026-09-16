"""Offline tests for TaskWraith-compatible provider presentation data."""
import copy
import unittest

from branding import BrandingError, load_branding, resolve_presentation, validate_overrides


#: The hub's own dark chrome (MistralBridge.swift's window background).
HUB_DARK = "#18191A"


def relative_luminance(colour: str) -> float:
    channels = []
    for offset in (1, 3, 5):
        value = int(colour[offset:offset + 2], 16) / 255
        channels.append(value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4)
    red, green, blue = channels
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def contrast(first: str, second: str) -> float:
    high, low = sorted((relative_luminance(first), relative_luminance(second)), reverse=True)
    return (high + 0.05) / (low + 0.05)


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

    def test_accent_palette_holds_one_readable_luminance_band(self):
        """Accents are drawn as foreground on both the light and the dark
        surface, so the palette normalizes every hue to a single WCAG
        relative luminance rather than to a brand's own lightness: AA
        against white, and a clear step off the hub's dark chrome. An
        arbitrary new accent picks its hue and takes all the chroma sRGB
        allows at that luminance; it does not pick its own lightness.
        """
        for key, colour in self.catalogue["accents"].items():
            with self.subTest(accent=key):
                self.assertGreaterEqual(round(contrast(colour, "#FFFFFF"), 2), 4.5)
                self.assertGreaterEqual(round(contrast(colour, HUB_DARK), 2), 3.0)
                self.assertTrue(
                    0.170 <= relative_luminance(colour) <= 0.183,
                    f"{key} {colour} leaves the palette's shared luminance band",
                )

    def test_openrouter_stealth_preview_wears_its_own_gold(self):
        """`stealth/union-alpha` is an anonymous provider's free preview, so
        it has no brand to borrow. It gets an arbitrary accent: the most
        saturated gold sRGB holds at the palette's luminance (OKLCH hue 75,
        chroma at the gamut edge), clear of Mistral's orange and Ollama's
        brown. The route and the bill remain OpenRouter's.
        """
        presentation = resolve_presentation(
            "openrouter", "stealth/union-alpha", supplied_label="Union Alpha",
            catalogue=self.catalogue)
        self.assertEqual(presentation, {
            "runtimeProvider": "openrouter",
            "displayProvider": "Stealth",
            "hueKey": "stealth",
            "accent": "#A06B00",
            "shortCode": "STL",
            "model": "stealth/union-alpha",
            "modelLabel": "Union Alpha",
        })
        # No sourced mark for an anonymous provider, and no borrowed one.
        self.assertNotIn("logo", presentation)

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
