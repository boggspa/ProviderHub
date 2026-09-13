"""Desktop effort slider mapping for Vibe, DeepSeek, and Ollama Cloud."""
import unittest

from effort_map import (
    DEEPSEEK_EFFORT_ALIASES,
    MISTRAL_EFFORT_ALIASES,
    MISTRAL_REASONING_EFFORTS,
    map_effort,
    ollama_effort_modes,
)
from providers import discover, prepare_request
from test_providers import text_prompt
from protocol import translate_request
from test_bridge import config, prompt
from codex_catalogue import project_codex
from hub_config import defaults
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
            ["none", "low", "high", "max"],
        )

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
