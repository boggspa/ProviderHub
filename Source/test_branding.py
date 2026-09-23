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
        """An OpenRouter `stealth/` preview belongs to an anonymous provider, so
        it has no brand to borrow. It gets an arbitrary accent, the gold
        TaskWraith minted for the same namespace: the most saturated gold
        this palette's luminance can hold, clear of Claude's amber and
        Cursor's yellow. The route and the bill remain OpenRouter's.
        """
        presentation = resolve_presentation(
            "openrouter", "stealth/synthetic-preview", supplied_label="Synthetic Preview",
            catalogue=self.catalogue)
        self.assertEqual(presentation, {
            "runtimeProvider": "openrouter",
            "displayProvider": "Stealth",
            "hueKey": "stealth",
            "accent": "#9E6C00",
            "shortCode": "STL",
            "model": "stealth/synthetic-preview",
            "modelLabel": "Synthetic Preview",
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

    def test_ollama_brand_table_covers_the_taskwraith_roster_local_and_cloud(self):
        """The Ollama display-brand table is mirrored from TaskWraith's
        `OLLAMA_DISPLAY_BRANDS`, needles included, so a local or Cloud tag
        wears its maker's hue while the runtime stays `ollama`. Cloud tags
        are the same names with a `:…-cloud` suffix, so one needle set
        serves both.
        """
        expected = {
            "qwen3:8b": ("Alibaba", "alibaba", "#8C52EF"),
            "qwen3-coder:480b-cloud": ("Alibaba", "alibaba", "#8C52EF"),
            "north-mini-code-1.0:30b-a3b-q4": ("Cohere", "cohere", "#5E7C6F"),
            "deepseek-r1:8b": ("DeepSeek", "deepseek", "#4E6AEE"),
            "deepseek-v3.1:671b-cloud": ("DeepSeek", "deepseek", "#4E6AEE"),
            "ornith:9b": ("Deep Reinforce", "deep-reinforce", "#BE5809"),
            "rnj-1:8b": ("Essential AI", "essential", "#8462CA"),
            # The Gemma spoof class wears Google's Antigravity green.
            "gemma4:12b": ("Google", "google", "#308713"),
            "granite4.1:3b": ("IBM", "ibm", "#3079BC"),
            "kimi-k2:1t-cloud": ("Kimi", "kimi", "#0073E6"),
            "lfm2.5:8b": ("Liquid", "liquid", "#D72D82"),
            "llama3.2:3b": ("Meta", "meta", "#1671EA"),
            "minimax-m2:cloud": ("MiniMax", "minimax", "#C044A4"),
            "devstral:24b": ("Mistral", "mistral", "#D44404"),
            # 'mistral' is not a substring of 'ministral'; it needs its own needle.
            "ministral:8b": ("Mistral", "mistral", "#D44404"),
            "nemotron3:33b": ("NVIDIA", "nvidia", "#538200"),
            # OpenAI's spoof hue is the Codex token, as it is upstream.
            "gpt-oss:120b-cloud": ("OpenAI", "openai", "#705AFF"),
            "minicpm-v4.5:8b": ("OpenBMB", "openbmb", "#E22B17"),
            "laguna-xs-2.1:33b": ("Poolside", "poolside", "#0C8194"),
            "glm-4.6:cloud": ("Z.ai", "zai", "#177DAA"),
        }
        for tag, (label, hue, accent) in expected.items():
            with self.subTest(model=tag):
                presentation = resolve_presentation("ollama", tag, catalogue=self.catalogue)
                self.assertEqual(presentation["runtimeProvider"], "ollama")
                self.assertEqual(presentation["displayProvider"], label)
                self.assertEqual(presentation["hueKey"], hue)
                self.assertEqual(presentation["accent"], accent)
        self.assertEqual(
            {rule["id"] for rule in self.catalogue["modelBrandOverrides"]
             if rule["runtimeProvider"] == "ollama"},
            {hue for _, hue, _ in expected.values()},
        )
        # A version-pinned needle still reaches its brand through the
        # humanised label the hub always supplies, as it does upstream.
        spaced = resolve_presentation("ollama", "north-mini-code:30b",
                                      supplied_label="North Mini Code \u00b7 30B",
                                      catalogue=self.catalogue)
        self.assertEqual(spaced["hueKey"], "cohere")
        # An unrecognised tag keeps Ollama's own walnut rather than guessing.
        unknown = resolve_presentation("ollama", "mystery-model:7b", catalogue=self.catalogue)
        self.assertEqual((unknown["displayProvider"], unknown["accent"]), ("Ollama", "#976C52"))

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

    def test_logo_display_options_round_trip_without_changing_routing(self):
        logo = {
            "light": "provider-logos/wordmark.png",
            "leadingMarkAspectRatio": 1.5,
            "template": True,
        }
        overrides = {"muse": {"logo": logo}}
        before = copy.deepcopy(overrides)
        normalized = validate_overrides(overrides, self.catalogue)
        presentation = resolve_presentation(
            "muse", "muse-spark-1.3", normalized, catalogue=self.catalogue)
        self.assertEqual(presentation["runtimeProvider"], "muse")
        self.assertEqual(presentation["model"], "muse-spark-1.3")
        self.assertEqual(presentation["logo"], {**logo, "dark": logo["light"]})
        self.assertEqual(overrides, before)

        # Replacing a default cropped/template logo with plain artwork must
        # not retain the old asset's display settings.
        for provider in ("muse", "qwen-token-plan", "devin"):
            with self.subTest(provider=provider):
                plain = resolve_presentation(
                    provider, overrides={provider: {"logo": {"light": "custom.png"}}},
                    catalogue=self.catalogue)
                self.assertEqual(plain["logo"], {"light": "custom.png", "dark": "custom.png"})

    def test_invalid_logo_display_options_fail_closed(self):
        invalid = [
            {"leadingMarkAspectRatio": value}
            for value in (True, "1.5", 0, -1, 0.49, 2.01, float("nan"), float("inf"))
        ] + [{"template": value} for value in (0, 1, "true", None)]
        for options in invalid:
            with self.subTest(options=options), self.assertRaises(BrandingError):
                validate_overrides(
                    {"muse": {"logo": {"light": "logo.png", **options}}}, self.catalogue)

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
