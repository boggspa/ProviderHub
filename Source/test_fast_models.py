"""Fast controls reflect provider requests, not just a fast-sounding name."""
import unittest

from codex_catalogue import project_codex
from fast_models import (CLAUDE_FAST_MODELS, OPENAI_FAST_MODELS,
                         fixed_speed_tier, supports_fast_toggle)
from hub_config import SLOTS, defaults, project_catalogue
from providers import ProviderError, discover, prepare_request


def prompt(**changes):
    return {"model": "claude-opus-5-5", "max_tokens": 100,
            "messages": [{"role": "user", "content": "Hello"}], **changes}


def cached_inventory(provider, model, *, fast_mode=False):
    return {"provider_id": provider, "source": "provider_api", "models": [{
        "id": model, "canonical_id": model, "aliases": [model],
        "display_name": model, "context": 256000, "tools": True,
        "reasoning": True, "effort_modes": ["low", "high"],
        "fast_mode": fast_mode,
    }]}


class FastModelTests(unittest.TestCase):
    def test_exact_toggle_allowlists_exclude_retired_opus_fast_modes(self):
        self.assertEqual(CLAUDE_FAST_MODELS,
                         {"claude-opus-5-5", "claude-opus-5", "claude-opus-4-8"})
        self.assertEqual(len(OPENAI_FAST_MODELS), 7)
        for model in ("claude-opus-4-7", "claude-opus-4-6", "claude-sonnet-5"):
            self.assertFalse(supports_fast_toggle("claude", model))
        self.assertFalse(supports_fast_toggle("codex", "gpt-5.5-pro"))
        self.assertFalse(supports_fast_toggle("antigravity", "gemini-3.8-flash"))

    def test_discovery_and_old_cache_enable_exact_openai_fast_rows(self):
        listed = ["gpt-5.5", "gpt-5.6-luna", "gpt-5.6-terra", "gpt-5.6-sol",
                  "gpt-6-luna", "gpt-6-sol", "gpt-6-astra", "gpt-5.5-pro"]
        inventory = discover("codex", None, "key", transport=lambda _: {
            "data": [{"id": model} for model in listed],
        })
        by_id = {model["id"]: model for model in inventory["models"]}
        for model in listed:
            self.assertEqual(by_id[model]["fast_mode"], model in OPENAI_FAST_MODELS)
        config = defaults(SLOTS, "mistral-test")
        for model in listed:
            rows = project_catalogue("codex", cached_inventory("codex", model), config)
            self.assertEqual(rows[0]["fast_mode"], model in OPENAI_FAST_MODELS)
            projected = project_codex(config, {"models": rows})["models"][0]
            self.assertEqual(bool(projected["service_tiers"]), model in OPENAI_FAST_MODELS)

    def test_discovery_and_old_cache_gate_claude_opus_fast_rows(self):
        listed = ["claude-opus-5-5", "claude-opus-5", "claude-opus-4-8",
                  "claude-opus-4-7", "claude-opus-4-6"]
        inventory = discover("claude", None, "key", transport=lambda _: {
            "data": [{"id": model, "type": "model"} for model in listed],
        })
        config = defaults(SLOTS, "mistral-test")
        for raw in inventory["models"]:
            model = raw["id"]
            expected = model in CLAUDE_FAST_MODELS
            self.assertEqual(raw["fast_mode"], expected)
            rows = project_catalogue("claude", cached_inventory("claude", model, fast_mode=True), config)
            self.assertEqual(rows[0]["fast_mode"], expected)
            projected = project_codex(config, {"models": rows})["models"][0]
            self.assertEqual(bool(projected["service_tiers"]), expected)

    def test_anthropic_fast_is_translated_and_beta_header_is_added(self):
        spec = {"fast_mode": True, "context": 1_000_000}
        request = prepare_request("claude", None, "key", prompt(speed="fast"),
                                  "claude-opus-5-5", spec)
        self.assertEqual(request["body"]["speed"], "fast")
        self.assertEqual(request["headers"]["anthropic-beta"], "fast-mode-2026-02-01")
        via_codex = prepare_request("claude", None, "key", prompt(service_tier="fast"),
                                    "claude-opus-5-5", spec)
        self.assertEqual(via_codex["body"]["speed"], "fast")
        self.assertNotIn("service_tier", via_codex["body"])
        standard = prepare_request("claude", None, "key", prompt(speed="standard"),
                                   "claude-opus-5-5", spec)
        self.assertNotIn("speed", standard["body"])
        self.assertNotIn("anthropic-beta", standard["headers"])
        for model in ("claude-opus-4-7", "claude-opus-4-6"):
            with self.subTest(model=model), self.assertRaises(ProviderError):
                prepare_request("claude", None, "key", prompt(speed="fast"),
                                model, {"fast_mode": False, "context": 1_000_000})

    def test_openai_fast_reaches_service_tier_only_on_supported_model(self):
        fast = prepare_request("codex", None, "key", prompt(service_tier="fast"),
                               "gpt-6-sol", {"fast_mode": True})
        self.assertEqual(fast["body"]["service_tier"], "fast")
        standard = prepare_request("codex", None, "key", prompt(service_tier="standard"),
                                   "gpt-6-sol", {"fast_mode": True})
        self.assertEqual(standard["body"]["service_tier"], "default")
        with self.assertRaises(ProviderError):
            prepare_request("codex", None, "key", prompt(service_tier="fast"),
                            "gpt-5.5-pro", {"fast_mode": False})

    def test_fixed_fast_routes_are_metadata_not_switchable_tiers(self):
        config = defaults(SLOTS, "mistral-test")
        for provider, model in (("kimi", "kimi-for-coding-highspeed"),
                                ("antigravity", "gemini-3.6-flash"),
                                ("antigravity", "gemini-3.7-flash"),
                                ("antigravity", "gemini-3.8-flash"),
                                ("grok", "grok-4.7-build-fast"),
                                ("cerebras", "gpt-oss-120b"),
                                ("cerebras", "qwen-3.8-27b")):
            with self.subTest(provider=provider, model=model):
                self.assertIsNotNone(fixed_speed_tier(provider, model))
                rows = project_catalogue(provider, cached_inventory(provider, model), config)
                self.assertEqual(rows[0]["speed_tier"], fixed_speed_tier(provider, model))
                self.assertFalse(rows[0]["fast_mode"])
                projected = project_codex(config, {"models": rows})["models"][0]
                self.assertEqual(projected["service_tiers"], [])


if __name__ == "__main__":
    unittest.main()
