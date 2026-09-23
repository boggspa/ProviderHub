"""Fast controls reflect provider requests, not just a fast-sounding name."""
import unittest

from claude_cli_agent import ClaudeCliAgentError, build_argv
from cli_routes import CliRouteError, _hub_row, plan_turn
from catalogue import route_specs
from codex_catalogue import project_codex
from fast_models import (CLAUDE_FAST_MODELS, OPENAI_FAST_MODELS,
                         fixed_speed_tier, supports_fast_toggle)
from hub_config import SLOTS, defaults, project_catalogue
from providers import ProviderError, discover, prepare_request
from protocol import model_catalog


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
                rows = project_catalogue(provider, cached_inventory(provider, model,
                                                                     fast_mode=True), config)
                self.assertEqual(rows[0]["speed_tier"], fixed_speed_tier(provider, model))
                self.assertFalse(rows[0]["fast_mode"])
                projected = project_codex(config, {"models": rows})["models"][0]
                self.assertEqual(projected["service_tiers"], [])
                self.assertIn("built into this route", projected["description"])

    def test_cli_picker_rows_and_turns_follow_the_same_fast_allowlists(self):
        for provider, model, expected in (
                ("codex", "gpt-6-sol", True),
                ("codex", "gpt-5.5-pro", False),
                ("claude", "opus", True),
                ("claude", "claude-opus-4-8", True),
                ("claude", "claude-opus-4-7", False),
                ("antigravity", "gemini-3.8-flash", False),
                ("grok", "grok-4.7-build-fast", False)):
            with self.subTest(provider=provider, model=model):
                row = _hub_row(provider, {"id": model})
                self.assertEqual(row["fast_mode"], expected)
                if fixed_speed_tier(provider, model):
                    self.assertEqual(row["speed_tier"], fixed_speed_tier(provider, model))

        message = {"messages": [{"role": "user", "content": "Hello"}]}
        claude_fast = plan_turn("claude", "opus", {**message, "service_tier": "fast"},
                                {"fast_mode": True}, wanted_output=100)["body"]
        self.assertIs(claude_fast["fast_mode"], True)
        claude_standard = plan_turn("claude", "opus", {**message, "speed": "standard"},
                                    {"fast_mode": True}, wanted_output=100)["body"]
        self.assertIs(claude_standard["fast_mode"], False)
        codex_fast = plan_turn("codex", "gpt-6-sol", {**message, "service_tier": "fast"},
                               {"fast_mode": True}, wanted_output=100)["body"]
        self.assertEqual(codex_fast["service_tier"], "fast")
        with self.assertRaises(CliRouteError):
            plan_turn("claude", "claude-opus-4-7", {**message, "speed": "fast"},
                      {"fast_mode": False}, wanted_output=100)

    def test_claude_print_mode_receives_per_turn_fast_setting(self):
        on = build_argv("opus", fast_mode=True)
        off = build_argv("opus", fast_mode=False)
        self.assertEqual(on[on.index("--settings") + 1], '{"fastMode":true}')
        self.assertEqual(off[off.index("--settings") + 1], '{"fastMode":false}')
        self.assertLess(on.index("--settings"), on.index("--tools"))
        self.assertEqual(on[on.index("--model") + 1], "claude-opus-5-5")
        with self.assertRaises(ClaudeCliAgentError):
            build_argv("claude-opus-4-7", fast_mode=True)

    def test_claude_desktop_model_catalogue_carries_exact_speed_metadata(self):
        for provider, model, fast, fixed in (
                ("claude", "claude-opus-5-5", True, None),
                ("claude", "claude-opus-4-7", False, None),
                ("kimi", "kimi-for-coding-highspeed", False, "highspeed")):
            with self.subTest(provider=provider, model=model):
                config = defaults(SLOTS, "mistral-test")
                route = provider + "/" + model
                config["claude_catalogue"] = [{"route": route, "tier": "opus",
                                               "tier_default": True}]
                rows = project_catalogue(provider, cached_inventory(provider, model), config)
                config["_model_specs"] = route_specs({"models": rows})
                item = model_catalog(config)["data"][0]
                self.assertEqual(item["fast_mode"], fast)
                self.assertEqual(item.get("speed_tier"), fixed)
                self.assertTrue(item["description"].endswith("token context"))


if __name__ == "__main__":
    unittest.main()
