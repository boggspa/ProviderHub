"""The Vibe catalogue projection: what Vibe's own model list is given."""
from __future__ import annotations

import unittest

from vibe_catalogue import (PROVIDER_ID, catalogue_digest, choices, default_thinking,
                             launch_settings, project_vibe, thinking_levels)


def entry(route, **extra):
    """One inventory row, as the merged catalogue publishes it."""
    provider_id, model = route.split("/", 1)
    row = {"id": route, "provider_id": provider_id, "model_id": model,
           "display_name": model, "tools": True, "vision": False, "reasoning": False,
           "effort_modes": [], "context": 200000}
    row.update(extra)
    return row


def inventory(*rows):
    return {"models": list(rows)}


class ProjectionTests(unittest.TestCase):
    def test_a_route_becomes_one_vibe_model_row(self):
        projection = project_vibe({}, inventory(entry("mistral/mistral-large-4")))
        self.assertEqual(len(projection["models"]), 1)
        row = projection["models"][0]
        self.assertEqual(row["provider"], PROVIDER_ID)
        self.assertEqual(row["alias"], "mistral/mistral-large-4")
        self.assertEqual(row["thinking_levels"], ["off"])

    def test_the_model_id_is_the_route_the_gateway_resolves(self):
        projection = project_vibe({}, inventory(entry("codex/gpt-5.2-codex")))
        # The gateway reads the model id straight back as a route, so the
        # row has to carry the qualified id and not the bare model name.
        self.assertEqual(projection["models"][0]["name"], "codex/gpt-5.2-codex")

    def test_xhigh_and_ultra_fold_onto_vibes_own_levels(self):
        spec = {"effort_modes": ["none", "low", "medium", "high", "xhigh", "max", "ultra"]}
        self.assertEqual(thinking_levels(spec), ["off", "low", "medium", "high", "max"])
        # Vibe's schema rejects any other level, so the wider Hub ranks are
        # folded rather than published.
        self.assertLessEqual(set(thinking_levels(spec)), {"off", "low", "medium", "high", "max"})

    def test_a_route_without_reasoning_offers_off_only(self):
        self.assertEqual(thinking_levels({"effort_modes": [], "reasoning": False}), ["off"])

    def test_the_default_level_prefers_the_models_own_default(self):
        self.assertEqual(default_thinking({"default_effort": "medium"}, ["off", "medium", "high"]), "medium")
        self.assertEqual(default_thinking({}, ["off", "medium", "high"]), "high")

    def test_a_curated_catalogue_narrows_the_rows_and_reports_what_is_missing(self):
        settings = {"vibe_catalogue": ["mistral/mistral-large-4", "codex/gpt-5.2-codex"]}
        projection = project_vibe(settings, inventory(entry("mistral/mistral-large-4")))
        self.assertEqual([row["route"] for row in projection["models"]], ["mistral/mistral-large-4"])
        self.assertEqual(projection["excluded"][0]["id"], "codex/gpt-5.2-codex")
        self.assertIn("not advertised", projection["excluded"][0]["reason"])

    def test_a_route_without_tools_is_left_out(self):
        projection = project_vibe({}, inventory(entry("mistral/mistral-large-4", tools=False)))
        self.assertEqual(projection["models"], [])
        self.assertIn("coding tools", projection["excluded"][0]["reason"])

    def test_agent_modes_are_not_chat_models(self):
        projection = project_vibe({}, inventory(entry("devin/senior-dev")))
        self.assertEqual(projection["models"], [])

    def test_the_picker_reads_the_rows(self):
        rows = choices({}, inventory(entry("mistral/mistral-large-4", context=1048576)))
        self.assertEqual(rows[0]["id"], "mistral/mistral-large-4")
        self.assertEqual(rows[0]["context"], 1048576)

    def test_the_digest_moves_when_the_catalogue_moves(self):
        first = catalogue_digest({}, inventory(entry("mistral/mistral-large-4")))
        second = catalogue_digest({}, inventory(entry("mistral/mistral-large-4"),
                                                entry("codex/gpt-5.2-codex")))
        self.assertNotEqual(first, second)

    def test_launch_settings_need_a_model(self):
        with self.assertRaises(ValueError):
            launch_settings({"vibe_model": None})
        with self.assertRaises(ValueError):
            launch_settings({"vibe_model": ""})
        # A bare id is normalised the way every other route is; the gateway
        # refuses it later if no provider advertises it.
        normalised = launch_settings({"vibe_model": "unknown/model"})
        self.assertEqual(normalised["mappings"]["Vibe"], "mistral/unknown/model")
        prepared = launch_settings({"vibe_model": "mistral/mistral-large-4",
                                    "vibe_catalogue": ["codex/gpt-5.2-codex"]})
        self.assertEqual(prepared["mappings"]["Vibe"], "mistral/mistral-large-4")
        self.assertEqual(prepared["mappings"]["vibe-catalog-0"], "codex/gpt-5.2-codex")


if __name__ == "__main__":
    unittest.main()
