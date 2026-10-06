"""The launch projection: what the desktop apps get when the saved selection
outlives what providers advertise."""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from bridge_core import SLOTS
from hub_config import defaults
from launch_selection import (apply_plan, effective_launch_settings, plan_blocked,
                              require_launchable, servable_shortfall, write_plan)
from catalogue_lifecycle import CataloguePreparationError


def inventory(*routes):
    return {"models": [{"id": route, "provider_id": route.split("/", 1)[0]} for route in routes]}


def base_settings():
    return defaults(SLOTS, "mistral-large-latest")


class CatalogueModeTests(unittest.TestCase):
    def test_unadvertised_row_is_left_out_and_tier_default_moves(self):
        settings = base_settings()
        settings["claude_catalogue"] = [
            {"route": "kimi/kimi-k3", "tier": "fable", "tier_default": True},
            {"route": "kimi/kimi-k3-thinking", "tier": "fable", "tier_default": False},
            {"route": "deepseek/deepseek-flash", "tier": "sonnet", "tier_default": True},
        ]
        projected, omissions = effective_launch_settings(
            settings, inventory("kimi/kimi-k3-thinking", "deepseek/deepseek-flash"))

        self.assertEqual([row["route"] for row in projected["claude_catalogue"]],
                         ["kimi/kimi-k3-thinking", "deepseek/deepseek-flash"])
        self.assertTrue(projected["claude_catalogue"][0]["tier_default"])
        self.assertEqual(len(omissions), 1)
        self.assertEqual(omissions[0]["route"], "kimi/kimi-k3")
        self.assertEqual(omissions[0]["surface"], "claude")
        self.assertIn("no longer advertises kimi-k3", omissions[0]["reason"])
        # The saved selection is untouched.
        self.assertEqual(len(settings["claude_catalogue"]), 3)
        self.assertEqual(servable_shortfall(projected, inventory("kimi/kimi-k3-thinking", "deepseek/deepseek-flash"),
                                            surface="claude"), [])

    def test_blocked_provider_rows_are_left_out_with_its_reason(self):
        settings = base_settings()
        settings["claude_catalogue"] = [
            {"route": "kimi/kimi-k3", "tier": "fable", "tier_default": True},
            {"route": "deepseek/deepseek-flash", "tier": "sonnet", "tier_default": True},
        ]
        projected, omissions = effective_launch_settings(
            settings, inventory("kimi/kimi-k3", "deepseek/deepseek-flash"),
            blocked={"kimi": "Kimi catalogue refresh failed: 401"})
        self.assertEqual([row["route"] for row in projected["claude_catalogue"]], ["deepseek/deepseek-flash"])
        self.assertEqual(omissions[0]["reason"], "Kimi catalogue refresh failed: 401")

    def test_everything_gone_is_a_shortfall(self):
        settings = base_settings()
        settings["claude_catalogue"] = [{"route": "kimi/kimi-k3", "tier": "fable", "tier_default": True}]
        projected, _ = effective_launch_settings(settings, inventory("deepseek/deepseek-flash"))
        self.assertEqual(projected["claude_catalogue"], [])
        self.assertEqual(servable_shortfall(projected, inventory("deepseek/deepseek-flash"), surface="claude", saved=settings),
                         ["kimi/kimi-k3"])


class MappingModeTests(unittest.TestCase):
    def test_sunset_slot_route_is_replaced_by_the_nearest_tier(self):
        settings = base_settings()
        settings["mappings"] = {
            "claude-fable-5": "kimi/kimi-k3", "claude-opus-5": "mistral/mistral-large-3",
            "claude-sonnet-5": "deepseek/deepseek-flash", "claude-haiku-4-5": "deepseek/deepseek-flash",
            "claude-sonnet-4-6": "deepseek/deepseek-flash",
        }
        projected, omissions = effective_launch_settings(
            settings, inventory("mistral/mistral-large-3", "deepseek/deepseek-flash"))
        self.assertEqual(projected["mappings"]["claude-fable-5"], "mistral/mistral-large-3")
        self.assertEqual(omissions[0]["slot"], "claude-fable-5")
        self.assertEqual(omissions[0]["replacement"], "mistral/mistral-large-3")
        self.assertEqual(settings["mappings"]["claude-fable-5"], "kimi/kimi-k3")

    def test_no_stand_in_leaves_the_slot_and_reports_a_shortfall(self):
        settings = base_settings()
        settings["mappings"] = {slot: "kimi/kimi-k3" for slot, *_ in SLOTS}
        projected, omissions = effective_launch_settings(settings, inventory("deepseek/deepseek-flash"))
        self.assertEqual(omissions, [])
        self.assertEqual(servable_shortfall(projected, inventory("deepseek/deepseek-flash"), surface="claude"),
                         ["kimi/kimi-k3"])


class CodexTests(unittest.TestCase):
    def test_curated_routes_and_default_model_fall_back(self):
        settings = base_settings()
        settings["codex_catalogue"] = ["kimi/kimi-k3", "deepseek/deepseek-flash"]
        settings["codex_model"] = "kimi/kimi-k3"
        inv = {"models": [{"id": "deepseek/deepseek-flash", "provider_id": "deepseek", "slug": "x",
                           "display_name": "DeepSeek Flash", "context": 131072, "tools": True,
                           "aliases": ["deepseek/deepseek-flash"]}]}
        projected, omissions = effective_launch_settings(settings, inv)
        self.assertEqual(projected["codex_catalogue"], ["deepseek/deepseek-flash"])
        self.assertEqual(projected["codex_model"], "deepseek/deepseek-flash")
        codex = [item for item in omissions if item["surface"] == "codex"]
        self.assertEqual(len(codex), 1)
        self.assertEqual(codex[0]["replacement"], "deepseek/deepseek-flash")
        self.assertEqual(servable_shortfall(projected, inv, surface="codex"), [])

    def test_nothing_usable_is_a_shortfall(self):
        settings = base_settings()
        settings["codex_catalogue"] = ["kimi/kimi-k3"]
        settings["codex_model"] = "kimi/kimi-k3"
        projected, _ = effective_launch_settings(settings, inventory("deepseek/deepseek-flash"))
        self.assertEqual(projected["codex_model"], "kimi/kimi-k3")
        self.assertEqual(servable_shortfall(projected, inventory("deepseek/deepseek-flash"), surface="codex"),
                         ["kimi/kimi-k3"])


class PlanTests(unittest.TestCase):
    def test_require_launchable_raises_only_when_nothing_is_usable(self):
        blocked = {"status": "blocked", "error": {"message": "refresh failed"}}
        with self.assertRaises(CataloguePreparationError):
            require_launchable({"providers": {"kimi": blocked}, "errors": [blocked["error"]]})
        require_launchable({"providers": {"kimi": blocked, "deepseek": {"status": "refreshed"}},
                            "errors": [blocked["error"]]})

    def test_plan_round_trip_and_recovery_clears_a_block(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lifecycle = {"providers": {"kimi": {"status": "blocked", "error": {"message": "401"}},
                                       "deepseek": {"status": "refreshed"}}}
            write_plan(root, "claude", lifecycle, [], "now")
            self.assertEqual(plan_blocked(root), {"kimi": "401"})
            settings = base_settings()
            settings["claude_catalogue"] = [
                {"route": "kimi/kimi-k3", "tier": "fable", "tier_default": True},
                {"route": "deepseek/deepseek-flash", "tier": "sonnet", "tier_default": True},
            ]
            projected, omissions = apply_plan(settings, inventory("kimi/kimi-k3", "deepseek/deepseek-flash"), root)
            self.assertEqual([row["route"] for row in projected["claude_catalogue"]], ["deepseek/deepseek-flash"])
            self.assertEqual(omissions[0]["reason"], "401")
            # Kimi prepares fine on the other surface: its block is cleared everywhere.
            write_plan(root, "codex", {"providers": {"kimi": {"status": "refreshed"}}}, [], "later")
            self.assertEqual(plan_blocked(root), {})
            plan = json.loads((root / "launch-plan.json").read_text())
            self.assertEqual(set(plan), {"claude", "codex"})


if __name__ == "__main__":
    unittest.main()
