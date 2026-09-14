"""Desktop effort slider mapping for Vibe, DeepSeek, and Ollama Cloud."""
import unittest

from effort_map import (
    CEREBRAS_EFFORT_ALIASES,
    DEEPSEEK_EFFORT_ALIASES,
    EFFORT_ORDER,
    MISTRAL_EFFORT_ALIASES,
    MISTRAL_NARROW_EFFORTS,
    MISTRAL_REASONING_EFFORTS,
    cap_high_end,
    map_effort,
    mistral_effort_modes,
    mistral_ladder_for_model,
    ollama_effort_modes,
)
from providers import discover, prepare_request
from test_providers import text_prompt
from protocol import translate_request
from test_bridge import config, prompt
from codex_catalogue import project_codex
from hub_config import defaults, project_catalogue
from bridge_core import SLOTS


class EffortMapUnitTests(unittest.TestCase):
    def test_vibe_ranks_are_off_low_medium_high_max(self):
        self.assertEqual(MISTRAL_REASONING_EFFORTS, ["none", "low", "medium", "high", "max"])
        self.assertNotIn("xhigh", MISTRAL_REASONING_EFFORTS)
        self.assertNotIn("ultra", MISTRAL_REASONING_EFFORTS)
        for requested, expected in (
            ("none", "none"), ("minimal", "low"), ("low", "low"),
            ("medium", "medium"), ("high", "high"),
            ("xhigh", "max"), ("max", "max"), ("ultra", "max"),
        ):
            self.assertEqual(map_effort(requested, MISTRAL_REASONING_EFFORTS, MISTRAL_EFFORT_ALIASES), expected)

    def test_deepseek_maps_claude_chatgpt_ranks_onto_native_set(self):
        supported = ["none", "low", "high", "max"]
        for requested, expected in (
            ("minimal", "low"), ("low", "low"), ("medium", "high"),
            ("high", "high"), ("xhigh", "high"), ("max", "max"), ("ultra", "max"),
        ):
            self.assertEqual(map_effort(requested, supported, DEEPSEEK_EFFORT_ALIASES), expected)

    def test_ollama_thinking_models_advertise_slider_ranks(self):
        self.assertEqual(
            ollama_effort_modes("deepseek-v4-pro:cloud", True),
            ["none", "low", "high", "max"],
        )
        self.assertEqual(
            ollama_effort_modes("gpt-oss:120b-cloud", True),
            ["low", "medium", "high"],
        )
        self.assertEqual(
            ollama_effort_modes("kimi-k3:cloud", True),
            ["none", "low", "medium", "high", "max"],
        )
        self.assertEqual(ollama_effort_modes("plain:latest", False), [])

    def test_mistral_effort_ladders_are_model_specific(self):
        # Full Off | Low | Medium | High | Max ladder: GLM 5.2 (Mistral hosted),
        # Mistral Medium 3.5, Mistral Small 4. Includes the versioned API IDs
        # from Mistral's model cards and qualified hub routes.
        for identifier in ("glm-5-2", "zai-glm-5-2", "mistral-medium-latest",
                           "mistral-medium-3", "mistral-medium-3-5",
                           "mistral-medium-3-5-26-04", "mistral-medium-2604",
                           "mistral-small-latest", "mistral-small-2603",
                           "mistral-small-4", "mistral/mistral-medium-2604",
                           "mistral/glm-5-2"):
            with self.subTest(identifier=identifier):
                self.assertEqual(
                    mistral_effort_modes(identifier, True),
                    list(MISTRAL_REASONING_EFFORTS),
                )
        # Also match spaced model names (e.g., from Vibe/Codex UI display names).
        for identifier in ("Mistral Medium 3.5", "Mistral Small 4", "GLM 5.2"):
            with self.subTest(identifier=identifier):
                self.assertEqual(
                    mistral_effort_modes(identifier, True),
                    list(MISTRAL_REASONING_EFFORTS),
                )
        # Off | High principle: every other Mistral reasoning model.
        for identifier in ("mistral-large-2512", "mistral-large-3", "codestral-2508",
                           "codestral-latest", "labs-leanstral-1-5-1", "ministral-14b-2512"):
            with self.subTest(identifier=identifier):
                self.assertEqual(
                    mistral_effort_modes(identifier, True),
                    list(MISTRAL_NARROW_EFFORTS),
                )
        # Spaced narrow models also match correctly.
        self.assertEqual(
            mistral_effort_modes("Mistral Large 3", True),
            list(MISTRAL_NARROW_EFFORTS),
        )
        # Non-reasoning models advertise no ladder at all.
        self.assertEqual(mistral_effort_modes("mistral-large-2512", False), [])
        self.assertEqual(mistral_effort_modes("mistral-large-2512", None), [])

    def test_ladder_for_model_checks_every_catalogue_name(self):
        # An opaque raw ID must not narrow a full ladder when the card's other
        # names (canonical, billing, advertised) identify it, and nothing may
        # widen a narrow model.
        full = list(MISTRAL_REASONING_EFFORTS)
        narrow = list(MISTRAL_NARROW_EFFORTS)
        self.assertEqual(
            mistral_ladder_for_model(
                "version-a", True,
                ("mistral-medium", "mistral-medium-3-5", "Mistral Medium 3.5")),
            full)
        self.assertEqual(
            mistral_ladder_for_model("opaque-id", True, ("mistral-small-2603",)),
            full)
        self.assertEqual(
            mistral_ladder_for_model("opaque-id", True, ("glm-5-2",)), full)
        self.assertEqual(
            mistral_ladder_for_model("mistral-medium-2604", True), full)
        self.assertEqual(
            mistral_ladder_for_model(
                "mistral-large-2512", True, ("mistral-large",)), narrow)
        self.assertEqual(mistral_ladder_for_model("opaque-id", True, ()), narrow)
        self.assertEqual(
            mistral_ladder_for_model("opaque-id", True, (None, "")), narrow)
        self.assertEqual(mistral_ladder_for_model("mistral-medium-2604", False), [])
        self.assertEqual(mistral_ladder_for_model("mistral-medium-2604", None), [])


class EffortTransportTests(unittest.TestCase):
    def test_mistral_forwards_vibe_ranks_instead_of_collapsing_to_none_high(self):
        spec = {"reasoning": True, "effort_modes": list(MISTRAL_REASONING_EFFORTS)}
        for requested, expected in (("low", "low"), ("medium", "medium"), ("max", "max"), ("xhigh", "max")):
            plan = prepare_request(
                "mistral", {}, "key", text_prompt(output_config={"effort": requested}),
                "mistral-medium-2508", spec,
            )
            self.assertEqual(plan["body"]["reasoning_effort"], expected)

    def test_deepseek_ultra_maps_to_max(self):
        spec = {"reasoning": True, "effort_modes": ["none", "low", "high", "max"]}
        body = prepare_request(
            "deepseek", {}, "key", text_prompt(output_config={"effort": "ultra"}),
            "deepseek-flash", spec,
        )["body"]
        self.assertEqual(body["output_config"]["effort"], "max")

    def test_ollama_cloud_deepseek_keeps_mapped_effort(self):
        spec = {"reasoning": True, "effort_modes": ["none", "low", "high", "max"]}
        body = prepare_request(
            "ollama", {}, None, text_prompt(output_config={"effort": "medium"}),
            "deepseek-v4-pro:cloud", spec,
        )["body"]
        self.assertEqual(body["thinking"]["type"], "enabled")
        self.assertEqual(body["output_config"]["effort"], "high")

    def test_claude_translate_uses_vibe_ranks(self):
        for requested, expected in (("low", "low"), ("medium", "medium"), ("xhigh", "max"), ("max", "max")):
            result, _ = translate_request(prompt(output_config={"effort": requested}), config())
            self.assertEqual(result["reasoning_effort"], expected)
            self.assertEqual(result["model"], "test-model")

    def test_codex_publishes_ollama_thinking_ranks(self):
        settings = defaults(SLOTS, "mistral-test")
        settings["codex_model"] = "ollama/deepseek-v4-pro:cloud"
        inventory = {"models": [{
            "id": "ollama/deepseek-v4-pro:cloud",
            "model_id": "deepseek-v4-pro:cloud",
            "provider_id": "ollama",
            "display_name": "DeepSeek V4 Pro Cloud",
            "context": 131072,
            "tools": True,
            "effort_modes": ["none", "low", "high", "max"],
        }]}
        row = project_codex(settings, inventory)["models"][0]
        self.assertEqual(
            [entry["effort"] for entry in row["supported_reasoning_levels"]],
            ["none", "low", "high", "max", "ultra"],
        )

    def test_stale_narrow_snapshot_still_projects_full_medium_ladder(self):
        settings = defaults(SLOTS, "mistral-medium-2604")
        settings["codex_model"] = "mistral/mistral-medium-2604"
        stale = {"models": [{
            "id": "mistral-medium-2604", "canonical_id": "mistral-medium",
            "display_name": "Mistral Medium 3.5",
            "billing_model_name": "mistral-medium-3-5",
            "aliases": ["mistral-medium-2604", "mistral-medium-latest"],
            "context": 262144, "tools": True, "vision": True,
            "reasoning": True,
            # Narrow-era snapshot bytes: projection must re-derive the
            # Vibe-verified ladder instead of trusting them.
            "effort_modes": ["none", "high"],
            "fast_mode": False,
        }]}
        inventory = {"models": project_catalogue("mistral", stale, settings)}
        row = project_codex(settings, inventory)["models"][0]
        self.assertEqual(
            [entry["effort"] for entry in row["supported_reasoning_levels"]],
            ["none", "low", "medium", "high", "max", "ultra"],
        )
        self.assertEqual(row["default_reasoning_level"], "high")

    def test_ollama_show_thinking_models_get_effort_modes(self):
        def transport(plan):
            if plan["url"].endswith("/api/tags"):
                return {"models": [
                    {"name": "deepseek-v4-pro:cloud", "model": "deepseek-v4-pro:cloud"},
                    {"name": "plain", "model": "plain"},
                ]}
            if plan["body"]["model"] == "deepseek-v4-pro:cloud":
                return {"capabilities": ["completion", "tools", "thinking"]}
            return {"capabilities": ["completion"]}

        by_id = {model["id"]: model for model in discover(
            "ollama", {}, None, transport=transport,
        )["models"]}
        self.assertEqual(by_id["deepseek-v4-pro:cloud"]["effort_modes"], ["none", "low", "high", "max"])
        self.assertEqual(by_id["plain"]["effort_modes"], [])


class CapHighEndTests(unittest.TestCase):
    def test_caps_above_top_to_highest_advertised(self):
        self.assertEqual(cap_high_end("ultra", ["low", "medium", "high"]), "high")
        self.assertEqual(cap_high_end("xhigh", ["low", "medium", "high"]), "high")
        self.assertEqual(cap_high_end("max", ["low", "medium", "high"]), "high")

    def test_keeps_exact_match(self):
        self.assertEqual(cap_high_end("high", ["low", "medium", "high"]), "high")
        self.assertEqual(cap_high_end("xhigh", ["low", "high", "xhigh"]), "xhigh")
        self.assertEqual(cap_high_end("max", ["none", "low", "high", "max"]), "max")

    def test_caps_to_top_when_above_top(self):
        self.assertEqual(cap_high_end("max", ["low", "high", "xhigh"]), "xhigh")
        self.assertEqual(cap_high_end("ultra", ["low", "high", "xhigh"]), "xhigh")

    def test_returns_none_for_below_top_mismatch(self):
        self.assertIsNone(cap_high_end("low", ["medium", "high"]))
        self.assertIsNone(cap_high_end("medium", ["high", "xhigh"]))

    def test_returns_none_for_empty_or_unknown(self):
        self.assertIsNone(cap_high_end("high", []))
        self.assertIsNone(cap_high_end(None, ["low", "high"]))
        self.assertIsNone(cap_high_end("bogus", ["low", "high"]))

    def test_cerebras_narrow_model_caps_ultra_to_high(self):
        for requested in ("xhigh", "max", "ultra"):
            normalized = CEREBRAS_EFFORT_ALIASES.get(requested, requested)
            self.assertEqual(cap_high_end(normalized, ["low", "medium", "high"]), "high")


if __name__ == "__main__":
    unittest.main(verbosity=2)
