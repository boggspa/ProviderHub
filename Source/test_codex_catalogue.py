"""Curated Codex catalogue projection and launch-coverage tests."""
from __future__ import annotations

import unittest

from codex_catalogue import (
    catalogue_digest,
    choices,
    launch_settings,
    project_codex,
)
from hub_config import defaults


SLOTS = [("claude-fable-5", "Fable 5", "fable", True)]


def settings(**updates):
    value = defaults(SLOTS, "mistral-test")
    value.update(updates)
    return value


def model(route, *, name=None, context=131072, tools=True, **extra):
    provider_id, model_id = route.split("/", 1)
    value = {
        "id": route,
        "model_id": model_id,
        "provider_id": provider_id,
        "display_name": name or model_id,
        "context": context,
        "tools": tools,
        "vision": False,
        "effort_modes": ["none", "high"],
    }
    value.update(extra)
    return value


def inventory(*models):
    return {"models": list(models)}


class CuratedCatalogueProjectionTests(unittest.TestCase):
    def test_curated_selection_filters_and_sorts_by_name(self):
        selected = settings(
            codex_model="mistral/b-model",
            codex_catalogue=["mistral/b-model", "ollama/z-model", "mistral/a-model"],
        )
        stock = inventory(
            model("mistral/b-model"), model("ollama/z-model"),
            model("mistral/a-model"), model("mistral/unselected-model"),
        )
        projected = project_codex(selected, stock)
        self.assertEqual(
            [entry["slug"] for entry in projected["models"]],
            ["mistral/a-model", "mistral/b-model", "ollama/z-model"],
        )
        self.assertEqual(projected["excluded"], [])

    def test_missing_curated_route_is_excluded_not_raised(self):
        selected = settings(
            codex_model="mistral/a-model",
            codex_catalogue=["mistral/a-model", "mistral/gone-model"],
        )
        projected = project_codex(
            selected, inventory(model("mistral/a-model")))
        self.assertEqual(
            [entry["slug"] for entry in projected["models"]], ["mistral/a-model"])
        self.assertEqual(len(projected["excluded"]), 1)
        self.assertEqual(projected["excluded"][0]["id"], "mistral/gone-model")
        self.assertIn("not advertised", projected["excluded"][0]["reason"])

    def test_curated_route_without_tools_is_excluded(self):
        selected = settings(
            codex_model="mistral/a-model",
            codex_catalogue=["mistral/a-model", "mistral/no-tools-model"],
        )
        projected = project_codex(selected, inventory(
            model("mistral/a-model"), model("mistral/no-tools-model", tools=False)))
        self.assertEqual(
            [entry["slug"] for entry in projected["models"]], ["mistral/a-model"])
        self.assertEqual(projected["excluded"][0]["id"], "mistral/no-tools-model")

    def test_all_models_mode_matches_previous_projection(self):
        stock = inventory(
            model("mistral/b-model"), model("mistral/no-tools-model", tools=False),
            model("ollama/z-model"),
        )
        automatic = project_codex(settings(codex_model="mistral/b-model"), stock)
        explicit_all = project_codex(
            settings(codex_model="mistral/b-model", codex_catalogue=None), stock)
        self.assertEqual(automatic, explicit_all)
        self.assertEqual(
            [entry["slug"] for entry in automatic["models"]],
            ["mistral/b-model", "ollama/z-model"],
        )
        self.assertEqual(automatic["excluded"][0]["id"], "mistral/no-tools-model")

    def test_choices_reflect_the_curated_selection(self):
        selected = settings(
            codex_model="mistral/a-model",
            codex_catalogue=["mistral/a-model", "ollama/z-model"],
        )
        rows = choices(selected, inventory(
            model("mistral/a-model"), model("ollama/z-model"),
            model("mistral/unselected-model")))
        self.assertEqual(
            [row["id"] for row in rows], ["mistral/a-model", "ollama/z-model"])

    def test_launch_settings_covers_every_curated_route(self):
        selected = settings(
            codex_model="mistral/a-model",
            codex_catalogue=[
                "mistral/a-model", "ollama/z-model", "mistral/b-model"],
        )
        launch = launch_settings(selected)
        self.assertEqual(launch["mappings"]["Codex"], "mistral/a-model")
        self.assertEqual(
            set(launch["mappings"].values()),
            {"mistral/a-model", "ollama/z-model", "mistral/b-model"},
        )
        # One pseudo-slot per curated entry plus the Codex default slot.
        self.assertEqual(len(launch["mappings"]), 4)

    def test_digest_tracks_selection_but_not_the_default(self):
        stock = inventory(
            model("mistral/a-model"), model("mistral/b-model"),
            model("ollama/z-model"),
        )
        base = settings(
            codex_model="mistral/a-model",
            codex_catalogue=["mistral/a-model", "ollama/z-model"],
        )
        moved_default = catalogue_digest(
            {**base, "codex_model": "ollama/z-model"}, stock)
        self.assertEqual(moved_default, catalogue_digest(base, stock))
        narrowed = catalogue_digest({**base, "codex_catalogue": ["mistral/a-model"]}, stock)
        self.assertNotEqual(narrowed, catalogue_digest(base, stock))


if __name__ == "__main__":
    unittest.main()
