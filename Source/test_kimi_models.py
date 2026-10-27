"""K3.1 catalogue rows and exact-model requests on the desktop protocols."""
import copy
from types import SimpleNamespace
import unittest

from catalogue import route_specs
from codex_catalogue import project_codex
from hub_config import SLOTS, defaults, project_catalogue
from protocol import model_catalog, stated_context
from providers import ProviderError, discover, prepare_request
from responses_bridge import to_messages


class Kimi31Tests(unittest.TestCase):
    def setUp(self):
        self.inventory = discover("kimi", {}, None)
        self.models = {m["id"]: m for m in self.inventory["models"]}
        self.new_models = ("k3.1-pro", "k3.1-pro-highspeed")

    def test_both_desktop_catalogues_publish_exact_routes_context_and_real_efforts(self):
        settings = defaults(SLOTS, "mistral-test")
        routes = ["kimi/" + model for model in self.new_models]
        settings["claude_catalogue"] = [{"route": route, "tier": tier, "tier_default": True}
                                        for route, tier in zip(routes, ("fable", "opus"))]
        settings["codex_catalogue"] = routes
        settings["codex_model"] = routes[0]
        rows = project_catalogue("kimi", self.inventory, settings)
        settings["_model_specs"] = route_specs({"models": rows})
        claude = model_catalog(settings)["data"]
        for model in self.new_models:
            row = next(r for r in claude if r["display_name"] == self.models[model]["display_name"] + " · Kimi")
            self.assertTrue(row["id"].endswith("[1m]"))
            self.assertFalse(row["fast_mode"])
        codex = {r["slug"]: r for r in project_codex(settings, {"models": rows})["models"]}
        for route in routes:
            self.assertEqual(codex[route]["context_window"], 1048576)
            self.assertEqual(codex[route]["default_reasoning_level"], "high")
            self.assertEqual([r["effort"] for r in codex[route]["supported_reasoning_levels"]],
                             ["low", "high", "max", "ultra"])
            self.assertEqual(codex[route]["service_tiers"], [])
        self.assertEqual(stated_context(self.models["k3.1-pro"]), ("range", 262144, 1048576))
        self.assertEqual(stated_context(self.models["k3.1-pro-highspeed"]), ("exact", 1048576, 1048576))

    def test_effort_controls_preserve_exact_model_on_messages_and_responses(self):
        for model in self.new_models:
            spec = self.models[model]
            for effort, expected in (("low", "low"), ("medium", "high"), ("high", "high"),
                                     ("xhigh", "max"), ("max", "max"), ("ultra", "max")):
                for surface in ("messages", "responses"):
                    with self.subTest(model=model, effort=effort, surface=surface):
                        payload = {"messages": [{"role": "user", "content": "Hello"}], "max_tokens": 128,
                                   "output_config": {"effort": effort}}
                        if surface == "responses":
                            payload = to_messages({"input": "Hello", "stream": True, "reasoning": {"effort": effort},
                                                   "max_output_tokens": 128}, "kimi/" + model,
                                                  spec, SimpleNamespace(), "test")
                        before = copy.deepcopy(payload)
                        plan = prepare_request("kimi", {}, "test-key", payload, model, spec)
                        self.assertEqual(plan["body"]["model"], model)
                        self.assertEqual(plan["body"]["thinking"]["type"], "enabled")
                        self.assertEqual(plan["body"]["output_config"]["effort"], expected)
                        self.assertEqual(payload, before)

    def test_default_thinking_is_on_and_explicit_off_never_falls_back_to_k28(self):
        for model in self.new_models:
            payload = {"messages": [{"role": "user", "content": "Hello"}], "max_tokens": 128}
            body = prepare_request("kimi", {}, "test-key", payload, model, self.models[model])["body"]
            self.assertEqual(body["model"], model)
            self.assertEqual(body["thinking"]["type"], "enabled")
            self.assertEqual(body["output_config"]["effort"], "high")
            for controls in ({"thinking": {"type": "disabled"}}, {"output_config": {"effort": "none"}},
                             {"thinking": {"type": "disabled"}, "output_config": {"effort": "high"}}):
                with self.subTest(model=model, controls=controls), self.assertRaisesRegex(ProviderError, "requires thinking"):
                    prepare_request("kimi", {}, "test-key", {**payload, **controls}, model, self.models[model])
            with self.assertRaises(ProviderError):
                prepare_request("kimi", {}, "test-key", {**payload, "speed": "fast"}, model, self.models[model])


if __name__ == "__main__":
    unittest.main()
