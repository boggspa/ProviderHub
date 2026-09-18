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


def model(route, *, name=None, context=131072, tools=True, reasoning=True, **extra):
    provider_id, model_id = route.split("/", 1)
    value = {
        "id": route,
        "model_id": model_id,
        "provider_id": provider_id,
        "display_name": name or model_id,
        "context": context,
        "tools": tools,
        "vision": False,
        "reasoning": reasoning,
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

    def test_search_is_advertised_only_where_the_route_can_serve_it(self):
        """web_search is a hosted tool: the model's own server runs it, and
        nothing behind this gateway is OpenAI. Offered on a route whose
        provider has no search of its own, Codex sends a tool the request
        cannot carry and the whole turn fails rather than the search quietly
        going missing, so the projection advertises it only where the route
        layer has a translation to make."""
        rows = {row["slug"]: row for row in project_codex(
            settings(), inventory(model("openrouter/searching", web_search=True),
                                  model("mistral/plain")))["models"]}
        self.assertEqual(rows["openrouter/searching"]["web_search_tool_type"], "text")
        # Absent, not null. Codex's catalogue parser reads this field as one of
        # "text" or "text_and_image" and rejects every other value including
        # null, and a single unreadable field makes it discard the entire file
        # and fall back to its own models - so a null here does not decline a
        # tool, it takes the whole hub catalogue down with it.
        self.assertNotIn("web_search_tool_type", rows["mistral/plain"])
        self.assertIsNone(rows["mistral/plain"]["apply_patch_tool_type"])

    def test_apply_patch_freeform_is_a_per_route_opt_in(self):
        stock = inventory(model("mistral/a-model"), model("mistral/b-model"))
        base = settings(codex_model="mistral/a-model",
                        codex_catalogue=["mistral/a-model", "mistral/b-model"])
        default = {row["slug"]: row for row in project_codex(base, stock)["models"]}
        self.assertIsNone(default["mistral/a-model"]["apply_patch_tool_type"])
        self.assertIsNone(default["mistral/b-model"]["apply_patch_tool_type"])
        qualified = {row["slug"]: row for row in project_codex(
            {**base, "codex_apply_patch": ["mistral/b-model"]}, stock)["models"]}
        self.assertIsNone(qualified["mistral/a-model"]["apply_patch_tool_type"])
        self.assertEqual(qualified["mistral/b-model"]["apply_patch_tool_type"], "freeform")
        # Qualification changes the projected catalogue, so snapshots refresh.
        self.assertNotEqual(catalogue_digest({**base, "codex_apply_patch": ["mistral/b-model"]}, stock),
                            catalogue_digest(base, stock))

    def test_apply_patch_switch_covers_the_catalogue_minus_exclusions(self):
        stock = inventory(model("mistral/a-model"), model("mistral/b-model"), model("ollama/z-model"))
        base = settings(codex_model="mistral/a-model",
                        codex_catalogue=["mistral/a-model", "mistral/b-model", "ollama/z-model"])
        everything = {row["slug"]: row["apply_patch_tool_type"] for row in project_codex(
            {**base, "codex_apply_patch_all": True}, stock)["models"]}
        self.assertEqual(everything, {"mistral/a-model": "freeform", "mistral/b-model": "freeform",
                                      "ollama/z-model": "freeform"})
        held_back = {row["slug"]: row["apply_patch_tool_type"] for row in project_codex(
            {**base, "codex_apply_patch_all": True, "codex_apply_patch_exclude": ["ollama/z-model"]},
            stock)["models"]}
        self.assertEqual(held_back, {"mistral/a-model": "freeform", "mistral/b-model": "freeform",
                                     "ollama/z-model": None})
        # An exclusion also overrides the per-route list while the switch is off.
        listed = {row["slug"]: row["apply_patch_tool_type"] for row in project_codex(
            {**base, "codex_apply_patch": ["mistral/b-model"], "codex_apply_patch_exclude": ["mistral/b-model"]},
            stock)["models"]}
        self.assertIsNone(listed["mistral/b-model"])
        # The switch changes the projected catalogue, so snapshots refresh.
        self.assertNotEqual(catalogue_digest({**base, "codex_apply_patch_all": True}, stock),
                            catalogue_digest(base, stock))

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


class DesktopBriefingProjectionTests(unittest.TestCase):
    """Flags the installed catalogue resolves to false when a route omits them."""

    def projected(self, *models):
        selected = settings(codex_model=models[0]["id"])
        return {row["slug"]: row for row in project_codex(selected, inventory(*models))["models"]}

    def rows(self):
        return self.projected(
            model("mistral/a-model"),
            model("mistral/plain-model", reasoning=False, effort_modes=[]),
        )

    def test_every_route_asks_for_the_skill_plugin_and_app_briefings(self):
        for slug, row in self.rows().items():
            with self.subTest(slug=slug):
                self.assertTrue(row["include_skills_usage_instructions"])
                self.assertTrue(row["include_plugin_usage_instructions"])
                self.assertTrue(row["include_apps_usage_instructions"])

    def test_node_repl_javascript_keeps_the_strict_auto_review(self):
        # node_repl is wired per install, so even a route with no reasoning
        # ladder (and therefore no multi-agent runtime) is handed the js tools.
        for slug, row in self.rows().items():
            with self.subTest(slug=slug):
                self.assertTrue(row["node_repl_auto_review_required"])


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
            ["none", "high", "ultra"],
        )
        self.assertEqual(rows["mistral/codestral"]["default_reasoning_level"], "high")
        self.assertEqual(
            [entry["effort"] for entry in rows["kimi/k3"]["supported_reasoning_levels"]],
            ["low", "high", "max", "ultra"],
        )
        self.assertEqual(rows["qwen-token-plan/qwen3.8-max"]["default_reasoning_level"], "xhigh")
        self.assertEqual(rows["gemini/gemini-3.8-flash"]["default_reasoning_level"], "medium")
        self.assertEqual(rows["cerebras/gpt-oss-120b"]["service_tiers"], [])
        # Multi-agent v2 is advertised for every reasoning-capable model with a
        # ladder, and the sub-agent rank is that model's own top rank rather
        # than one hardcoded rank most of these never advertise.
        top_rank = {"mistral/codestral": "high", "kimi/k3": "max",
                    "qwen-token-plan/qwen3.8-max": "xhigh",
                    "gemini/gemini-3.8-flash": "high", "cerebras/gpt-oss-120b": "high"}
        for slug in rows:
            with self.subTest(slug=slug):
                self.assertEqual(rows[slug]["multi_agent_version"], "v2")
                self.assertEqual(rows[slug]["multi_agent_reasoning_effort"], top_rank[slug])
                self.assertIn(rows[slug]["multi_agent_reasoning_effort"],
                              [entry["effort"] for entry in rows[slug]["supported_reasoning_levels"]])
        self.assertEqual(len(rows), 5)

    def test_non_reasoning_model_publishes_no_ultra_or_multi_agent(self):
        rows = self.projected(model(
            "mistral/plain-model", reasoning=False, effort_modes=[],
        ))
        row = rows["mistral/plain-model"]
        self.assertEqual(row["supported_reasoning_levels"], [])
        self.assertIsNone(row["multi_agent_version"])
        self.assertIsNone(row["multi_agent_reasoning_effort"])

    def test_kimi_highspeed_placeholder_rank_yields_a_two_step_ladder(self):
        # HighSpeed documents thinking always on and no effort ladder. Its one
        # placeholder rank keeps the slider from being empty and lets the
        # synthesized Ultra alias carry the multi-agent affordance; the
        # gateway drops both ranks on the wire.
        rows = self.projected(model(
            "kimi/kimi-for-coding-highspeed", name="Kimi for Coding HighSpeed",
            context=262144, effort_modes=["high"], speed_tier="highspeed",
        ))
        row = rows["kimi/kimi-for-coding-highspeed"]
        self.assertEqual(
            [entry["effort"] for entry in row["supported_reasoning_levels"]],
            ["high", "ultra"],
        )
        self.assertEqual(row["default_reasoning_level"], "high")
        self.assertEqual(row["multi_agent_version"], "v2")
        self.assertEqual(row["multi_agent_reasoning_effort"], "high")

    def test_fixed_reasoning_route_still_reaches_ultra(self):
        # A provider that runs thinking always-on publishes no rank at all.
        # Without a placeholder the ladder is empty, Codex stands on "none",
        # and the Ultra alias - and with it multi-agent orchestration - is
        # unreachable for the whole route.
        rows = self.projected(model("mistral/always-thinking", effort_modes=[]))
        row = rows["mistral/always-thinking"]
        self.assertEqual([entry["effort"] for entry in row["supported_reasoning_levels"]],
                         ["high", "ultra"])
        self.assertEqual(row["default_reasoning_level"], "high")
        self.assertEqual(row["multi_agent_version"], "v2")
        self.assertEqual(row["multi_agent_reasoning_effort"], "high")

    def test_ultra_is_not_duplicated_when_model_advertises_it(self):
        rows = self.projected(model(
            "mistral/ultra-native", effort_modes=["none", "low", "ultra"],
        ))
        efforts = [e["effort"] for e in rows["mistral/ultra-native"]["supported_reasoning_levels"]]
        self.assertEqual(efforts.count("ultra"), 1)

    def test_chatgpt_invalid_effort_names_are_dropped(self):
        rows = self.projected(model(
            "mistral/a-model",
            effort_modes=["none", "persistent", "high", "foo", "high"],
        ))
        self.assertEqual(
            [entry["effort"] for entry in rows["mistral/a-model"]["supported_reasoning_levels"]],
            ["none", "high", "ultra"],
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
            ["low", "medium", "high", "xhigh", "ultra"],
        )
        self.assertEqual(rows["grok/unknown"]["service_tiers"], [])
        self.assertNotIn("Fast", rows["grok/unknown"]["description"])
        self.assertEqual(
            [entry["effort"] for entry in rows["muse/muse-spark-1.3"]["supported_reasoning_levels"]],
            ["minimal", "low", "medium", "high", "xhigh", "max", "ultra"],
        )
        self.assertEqual(rows["muse/muse-spark-1.3"]["service_tiers"][0]["id"], "fast")
        self.assertEqual(
            [entry["effort"] for entry in rows["ollama/thinker:latest"]["supported_reasoning_levels"]],
            ["none", "high", "ultra"],
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
            ["minimal", "low", "medium", "high", "xhigh", "max", "ultra"],
        )
        self.assertEqual(row["default_reasoning_level"], "high")
        self.assertEqual(row["service_tiers"], [])


class ComposerLabelTests(unittest.TestCase):
    def projected(self, *models):
        selected = settings(codex_model=models[0]["id"])
        return {row["slug"]: row for row in project_codex(selected, inventory(*models))["models"]}

    def test_unique_names_stand_alone_without_provider_suffix(self):
        rows = self.projected(
            model("kimi/k3", name="K3"),
            model("mistral/mistral-medium-3-5", name="Mistral Medium 3.5"),
            model("ollama/deepseek-v4-pro:cloud", name="DeepSeek V4 Pro · Cloud"),
            model("openrouter/thinkingmachines/inkling:free", name="Inkling · Free"),
        )
        self.assertEqual(rows["kimi/k3"]["display_name"], "K3")
        self.assertEqual(rows["mistral/mistral-medium-3-5"]["display_name"], "Mistral Medium 3.5")
        self.assertEqual(
            rows["ollama/deepseek-v4-pro:cloud"]["display_name"], "DeepSeek V4 Pro · Cloud")
        self.assertEqual(
            rows["openrouter/thinkingmachines/inkling:free"]["display_name"], "Inkling · Free")

    def test_cross_provider_collision_gains_provider_suffix(self):
        rows = self.projected(
            model("kimi/k3", name="K3"),
            model("ollama/k3-local", name="K3"),
        )
        self.assertEqual(rows["kimi/k3"]["display_name"], "K3 · Kimi")
        self.assertEqual(rows["ollama/k3-local"]["display_name"], "K3 · Ollama")

    def test_same_provider_collision_falls_back_to_route(self):
        rows = self.projected(
            model("ollama/alpha", name="Same"),
            model("ollama/beta", name="Same"),
        )
        self.assertEqual(rows["ollama/alpha"]["display_name"], "Same · ollama/alpha")
        self.assertEqual(rows["ollama/beta"]["display_name"], "Same · ollama/beta")

    def test_collision_matching_is_case_insensitive(self):
        rows = self.projected(
            model("mistral/one", name="Dup"),
            model("kimi/two", name="dup"),
        )
        self.assertEqual(rows["mistral/one"]["display_name"], "Dup · Mistral")
        self.assertEqual(rows["kimi/two"]["display_name"], "dup · Kimi")

    def test_synthesized_ultra_description_is_native_text(self):
        rows = self.projected(
            model("mistral/narrow", effort_modes=["none", "low", "high"]),
            model("mistral/ultra-native", effort_modes=["none", "low", "ultra"]),
        )
        narrow = [row for row in rows["mistral/narrow"]["supported_reasoning_levels"]
                  if row["effort"] == "ultra"]
        native_text = "Maximum reasoning with automatic task delegation"
        self.assertEqual([row["description"] for row in narrow], [native_text])
        native = [row for row in rows["mistral/ultra-native"]["supported_reasoning_levels"]
                  if row["effort"] == "ultra"]
        self.assertEqual([row["description"] for row in native], [native_text])


if __name__ == "__main__":
    unittest.main()
