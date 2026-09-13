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
from providers import discover


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


class EffortAndFastProjectionTests(unittest.TestCase):
    def projected(self, *models):
        selected = settings(codex_model=models[0]["id"])
        return {row["slug"]: row for row in project_codex(selected, inventory(*models))["models"]}

    def test_each_provider_publishes_its_own_effort_ranks_not_duplicate_rows(self):
        rows = self.projected(
            model("mistral/codestral", effort_modes=["none", "high"]),
            model("kimi/k3", name="K3", effort_modes=["low", "high", "max"]),
            model("qwen-token-plan/qwen3.8-max", name="Qwen 3.8 Max",
                  effort_modes=["none", "low", "medium", "xhigh"], default_effort="xhigh"),
            model("gemini/gemini-3.8-flash", name="Gemini 3.8 Flash",
                  effort_modes=["low", "medium", "high"], default_effort="medium"),
            model("cerebras/gpt-oss-120b", name="GPT OSS 120B",
                  effort_modes=["low", "medium", "high"]),
        )
        self.assertEqual(
            [entry["effort"] for entry in rows["mistral/codestral"]["supported_reasoning_levels"]],
            ["none", "high"],
        )
        self.assertEqual(rows["mistral/codestral"]["default_reasoning_level"], "high")
        self.assertEqual(
            [entry["effort"] for entry in rows["kimi/k3"]["supported_reasoning_levels"]],
            ["low", "high", "max"],
        )
        self.assertEqual(rows["qwen-token-plan/qwen3.8-max"]["default_reasoning_level"], "xhigh")
        self.assertEqual(rows["gemini/gemini-3.8-flash"]["default_reasoning_level"], "medium")
        self.assertEqual(rows["cerebras/gpt-oss-120b"]["service_tiers"], [])
        self.assertEqual(len(rows), 5)

    def test_chatgpt_invalid_effort_names_are_dropped(self):
        rows = self.projected(model(
            "mistral/a-model",
            effort_modes=["none", "persistent", "high", "foo", "high"],
        ))
        self.assertEqual(
            [entry["effort"] for entry in rows["mistral/a-model"]["supported_reasoning_levels"]],
            ["none", "high"],
        )

    def test_fast_is_advertised_only_for_same_model_fast_controls(self):
        rows = self.projected(
            model("grok/grok-4.6", name="Grok 4.6",
                  effort_modes=["low", "medium", "high", "xhigh"], fast_mode=True,
                  presentation={"displayProvider": "Grok"}),
            model("grok/unknown", name="Unknown Grok", effort_modes=["low", "high"]),
            model("muse/muse-spark-1.3", name="Muse Spark",
                  effort_modes=["minimal", "low", "medium", "high", "xhigh", "max"], fast_mode=True),
            model("ollama/thinker:latest", name="Thinker", reasoning=True,
                  effort_modes=["none", "high"], fast_mode=True),
        )
        grok = rows["grok/grok-4.6"]
        self.assertEqual(grok["service_tiers"][0]["id"], "priority")
        self.assertIn("Priority", grok["description"])
        self.assertEqual(
            [entry["effort"] for entry in grok["supported_reasoning_levels"]],
            ["low", "medium", "high", "xhigh"],
        )
        self.assertEqual(rows["grok/unknown"]["service_tiers"], [])
        self.assertNotIn("Fast", rows["grok/unknown"]["description"])
        self.assertEqual(
            [entry["effort"] for entry in rows["muse/muse-spark-1.3"]["supported_reasoning_levels"]],
            ["minimal", "low", "medium", "high", "xhigh", "max"],
        )
        self.assertEqual(rows["muse/muse-spark-1.3"]["service_tiers"][0]["id"], "fast")
        self.assertEqual(
            [entry["effort"] for entry in rows["ollama/thinker:latest"]["supported_reasoning_levels"]],
            ["none", "high"],
        )
        self.assertEqual(rows["ollama/thinker:latest"]["service_tiers"], [])

    def test_models_without_high_default_to_their_top_advertised_rank(self):
        rows = self.projected(model(
            "openrouter/z-ai/glm-5.2", name="GLM 5.2",
            effort_modes=["none", "low", "medium", "xhigh"],
        ))
        self.assertEqual(rows["openrouter/z-ai/glm-5.2"]["default_reasoning_level"], "xhigh")

    def test_spark_13_catalogue_publishes_first_party_ranks_without_inventing_fast(self):
        discovered = discover("muse", {}, "key", transport=lambda _plan: {
            "data": [{"id": "muse-spark-1.3"}],
        })["models"][0]
        self.assertEqual(
            discovered["effort_modes"],
            ["minimal", "low", "medium", "high", "xhigh", "max"],
        )
        self.assertFalse(discovered["fast_mode"])
        self.assertNotIn("none", discovered["effort_modes"])
        rows = self.projected(model(
            "muse/muse-spark-1.3",
            name=discovered["display_name"],
            context=discovered["context"],
            effort_modes=discovered["effort_modes"],
            fast_mode=discovered["fast_mode"],
            reasoning=discovered["reasoning"],
            tools=discovered["tools"],
        ))
        row = rows["muse/muse-spark-1.3"]
        self.assertEqual(
            [entry["effort"] for entry in row["supported_reasoning_levels"]],
            ["minimal", "low", "medium", "high", "xhigh", "max"],
        )
        self.assertEqual(row["default_reasoning_level"], "high")
        self.assertEqual(row["service_tiers"], [])


if __name__ == "__main__":
    unittest.main()
