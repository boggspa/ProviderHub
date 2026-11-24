"""The Antigravity catalogue projection: Hub routes in Antigravity's own shape."""
from __future__ import annotations

import unittest

from antigravity_catalogue import (catalogue_digest, choices, launch_settings,
                                   effort_choices, project_antigravity, route_for_slug, slug_for)


def entry(route, **extra):
    provider_id, model = route.split("/", 1)
    row = {"id": route, "provider_id": provider_id, "model_id": model,
           "display_name": model, "tools": True, "vision": False, "reasoning": False,
           "effort_modes": [], "context": 200000}
    row.update(extra)
    return row


class SlugTests(unittest.TestCase):
    def test_a_route_becomes_one_flat_segment(self):
        self.assertTrue(slug_for("mistral/large-3").startswith("ph-"))

    def test_the_slug_has_no_slash(self):
        # The id travels as one URL path segment; a route's slash would split it.
        self.assertNotIn("/", slug_for("mistral/large-3"))

    def test_the_slug_turns_back_into_the_route(self):
        self.assertEqual(route_for_slug(slug_for("mistral/large-3")), "mistral/large-3")

    def test_a_model_id_with_a_slash_survives_the_round_trip(self):
        route = "mistral/team/large-3"
        self.assertEqual(route_for_slug(slug_for(route)), route)

    def test_an_unknown_provider_is_not_a_route(self):
        self.assertIsNone(route_for_slug("nonesuch~model"))

    def test_an_id_without_the_separator_is_not_a_route(self):
        self.assertIsNone(route_for_slug("large-3"))

    def test_a_malformed_model_is_not_a_route(self):
        self.assertIsNone(route_for_slug("mistral~"))


class ProjectionTests(unittest.TestCase):
    def setUp(self):
        self.settings = {"port": 11436}
        self.inventory = {"models": [
            entry("mistral/large-3", display_name="Mistral Large 3", context=200000,
                  reasoning=True, effort_modes=["low", "medium", "high"]),
            entry("codex/gpt-5.2-codex", display_name="GPT-5.2 Codex", context=400000,
                  vision=True),
        ]}

    def test_every_compatible_route_is_projected(self):
        projected = project_antigravity(self.settings, self.inventory)
        # The rows are ordered by their label, the way the picker shows them.
        self.assertEqual([row["route"] for row in projected["models"]],
                         ["codex/gpt-5.2-codex", "mistral/large-3"])

    def test_only_the_models_advertised_levels_are_selectable(self):
        plain, reasoning = project_antigravity(self.settings, self.inventory)["models"]
        self.assertEqual(list(effort_choices(plain)), [])
        variants = list(effort_choices(reasoning))
        self.assertEqual([row["effort"] for row in variants], ["low", "medium", "high"])
        self.assertEqual(reasoning["default_effort"], "high")

    def test_picker_shows_native_rungs_switches_and_fixed_models(self):
        canonical = ["none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"]
        cases = (
            ("antigravity/gemini-4-hydrogen", canonical, ["low", "medium", "high", "xhigh"],
             ["Low", "Medium", "High", "Extra High"]),
            ("antigravity/gpt-oss-120b", canonical, ["medium"], []),
            ("kimi/kimi-for-coding-highspeed", ["high"], [], []),
            ("minimax/MiniMax-M2.7-highspeed", ["high"], [], []),
            ("minimax/MiniMax-M3", ["none", "high"], [], ["Off", "On"]),
            ("mimo/mimo-v2.6-pro", ["none", "high"], [], ["Off", "On"]),
            ("mistral/mistral-medium-latest", ["none", "low", "medium", "high", "max"], [],
             ["Off", "On"]),
            ("minimax/MiniMax-M3.1-Flash-Preview", ["low", "medium", "high", "xhigh", "max"], [],
             ["Low", "Medium", "High", "Extra High", "Max"]),
        )
        for route, modes, native_modes, expected in cases:
            with self.subTest(route=route):
                inventory = {"models": [entry(route, reasoning=True, effort_modes=modes,
                                              provider_effort_modes=native_modes)]}
                row, = project_antigravity(self.settings, inventory)["models"]
                self.assertEqual([variant["label"]
                                  for variant in effort_choices(row)], expected)

    def test_a_saved_selection_narrows_the_list(self):
        settings = {**self.settings, "antigravity_catalogue": ["mistral/large-3"]}
        projected = project_antigravity(settings, self.inventory)
        self.assertEqual([row["route"] for row in projected["models"]], ["mistral/large-3"])

    def test_a_route_nobody_advertises_is_reported(self):
        settings = {**self.settings, "antigravity_catalogue": ["mistral/gone-3"]}
        projected = project_antigravity(settings, self.inventory)
        self.assertEqual(projected["models"], [])
        self.assertEqual(len(projected["excluded"]), 1)
        self.assertEqual(projected["excluded"][0]["id"], "mistral/gone-3")
        self.assertIn("not advertised", projected["excluded"][0]["reason"])

    def test_a_model_without_tools_is_left_out(self):
        inventory = {"models": [entry("mistral/large-3", tools=False)]}
        projected = project_antigravity(self.settings, inventory)
        self.assertEqual(projected["models"], [])
        self.assertIn("tools", projected["excluded"][0]["reason"])

    def test_the_output_allowance_stays_below_the_window(self):
        # Antigravity subtracts the allowance from the window before it plans
        # a turn and refuses the route when the remainder is negative.
        inventory = {"models": [entry("mistral/small-3", context=32768)]}
        row = project_antigravity(self.settings, inventory)["models"][0]
        self.assertLess(row["max_output_tokens"], row["context"])

    def test_a_route_with_no_advertised_window_gets_a_safe_one(self):
        inventory = {"models": [entry("mistral/large-3", context=None)]}
        row = project_antigravity(self.settings, inventory)["models"][0]
        self.assertEqual(row["context"], 200000)

    def test_the_digest_moves_when_the_catalogue_moves(self):
        first = catalogue_digest(self.settings, self.inventory)
        self.assertEqual(first, catalogue_digest(self.settings, self.inventory))
        moved = {"models": self.inventory["models"][:1]}
        self.assertNotEqual(first, catalogue_digest(self.settings, moved))

    def test_choices_carry_the_picker_fields(self):
        rows = choices(self.settings, self.inventory)
        self.assertEqual([row["id"] for row in rows], ["codex/gpt-5.2-codex", "mistral/large-3"])
        self.assertEqual(rows[1]["name"], "Mistral Large 3")
        self.assertIn("token context", rows[1]["description"])


class LaunchSettingsTests(unittest.TestCase):
    def test_a_selected_model_is_required(self):
        with self.assertRaises(ValueError):
            launch_settings({"providers": {}})

    def test_every_catalogue_route_is_freshness_checked(self):
        settings = {"antigravity_model": "mistral/large-3",
                    "antigravity_catalogue": ["mistral/large-3", "codex/gpt-5.2-codex"]}
        prepared = launch_settings(settings)
        self.assertEqual(prepared["mappings"]["Antigravity"], "mistral/large-3")
        self.assertEqual(prepared["mappings"]["antigravity-catalog-1"], "codex/gpt-5.2-codex")

    def test_an_unconfigured_provider_is_refused(self):
        with self.assertRaises(ValueError):
            launch_settings({"antigravity_model": "nonesuch/model"})


if __name__ == "__main__":
    unittest.main()
