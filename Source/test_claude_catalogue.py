"""Versioned Claude rows across cached discovery and both desktop pickers."""
import copy
import unittest
from unittest.mock import patch

import claude_cli_agent
import cli_routes
from bridge_core import SLOTS
from catalogue import route_specs
from codex_catalogue import project_codex
from hub_config import defaults, project_catalogue
from protocol import identity_note, model_catalog


EXPECTED = {
    "claude-fable-5-1": "Claude Fable 5.1",
    "claude-fable-5": "Claude Fable 5 (Legacy)",
    "claude-opus-5-5": "Claude Opus 5.5",
    "claude-opus-5": "Claude Opus 5 (Legacy)",
    "claude-opus-4-8": "Claude Opus 4.8 (Legacy)",
    "claude-opus-4-7": "Claude Opus 4.7 (Legacy)",
    "claude-opus-4-6": "Claude Opus 4.6 (Legacy)",
    "claude-sonnet-5-5": "Claude Sonnet 5.5",
    "claude-sonnet-5": "Claude Sonnet 5 (Legacy)",
    "claude-sonnet-4-6": "Claude Sonnet 4.6 (Legacy)",
    "claude-haiku-5-5": "Claude Haiku 5.5",
    "claude-haiku-4-5": "Claude Haiku 4.5 (Legacy)",
}


def old_inventory():
    return {"provider_id": "claude", "source": "cli", "fetched_at": "2026-09-18T12:00:00Z",
            "models": [{"id": identifier, "canonical_id": identifier,
                        "display_name": label, "aliases": [identifier],
                        "context": None, "max_output": None, "tools": True,
                        "vision": None, "reasoning": True,
                        "effort_modes": ["low", "medium", "high", "xhigh", "max"]}
                       for identifier, label in (
                           ("fable", "Claude Fable (alias)"),
                           ("opus", "Claude Opus (alias)"),
                           ("sonnet", "Claude Sonnet (alias)"),
                           ("claude-fable-5", "Claude Fable 5"),
                           ("claude-sonnet-5", "Claude Sonnet 5"))]}


class ClaudeCatalogueTests(unittest.TestCase):
    def setUp(self):
        self.config = defaults(SLOTS, "mistral-test")
        self.config["providers"]["claude"]["credential_mode"] = "cli"
        self.adapter = patch.dict(cli_routes._cache, {"claude": claude_cli_agent})
        self.adapter.start()
        self.addCleanup(self.adapter.stop)

    def test_repeated_discovery_offers_twelve_versions_without_alias_rows(self):
        with patch.object(claude_cli_agent, "auth_state", return_value={"state": "authenticated"}):
            for _ in range(2):
                inventory = cli_routes.discover_via_cli("claude")
                self.assertEqual({row["id"]: row["display_name"] for row in inventory["models"]}, EXPECTED)
                for row in inventory["models"]:
                    argv = claude_cli_agent.build_argv(row["id"])
                    self.assertEqual(argv[argv.index("--model") + 1], row["id"])

    def test_saved_aliases_request_the_displayed_version(self):
        for alias, version in (("fable", "claude-fable-5-1"), ("opus", "claude-opus-5-5"),
                               ("sonnet", "claude-sonnet-5-5"), ("haiku", "claude-haiku-5-5"),
                               ("claude-fable-5", "claude-fable-5"),
                               ("claude-future", "claude-future")):
            with self.subTest(model=alias):
                argv = claude_cli_agent.build_argv(alias)
                self.assertEqual(argv[argv.index("--model") + 1], version)

    def test_old_cache_upgrades_without_mutation_or_duplicate_versions(self):
        inventory = old_inventory()
        before = copy.deepcopy(inventory)
        projected = project_catalogue("claude", inventory, self.config)
        self.assertEqual(inventory, before)
        self.assertEqual({row["model_id"]: row["display_name"] for row in projected}, EXPECTED)
        specs = route_specs({"models": projected})
        for alias, version in (("fable", "claude-fable-5-1"), ("opus", "claude-opus-5-5"),
                               ("sonnet", "claude-sonnet-5-5"), ("haiku", "claude-haiku-5-5")):
            self.assertIs(specs["claude/" + alias], specs["claude/" + version])
        self.assertIsNot(specs["claude/fable"], specs["claude/claude-fable-5"])
        self.assertTrue(specs["claude/haiku"]["reasoning"])
        self.assertEqual(specs["claude/haiku"]["effort_modes"], ["low", "medium", "high", "xhigh", "max"])
        self.assertFalse(specs["claude/claude-haiku-4-5"]["reasoning"])
        self.assertEqual(specs["claude/claude-haiku-4-5"]["effort_modes"], [])

    def test_saved_aliases_and_exact_versions_share_rows_in_both_desktops(self):
        for default in ("claude/opus", "claude/claude-opus-5"):
            with self.subTest(default=default):
                config = copy.deepcopy(self.config)
                routes = ["claude/opus", "claude/claude-opus-5", "claude/fable",
                          "claude/claude-fable-5", "claude/sonnet"]
                config["codex_model"] = default
                config["codex_catalogue"] = routes
                config["claude_catalogue"] = [
                    {"route": route, "tier": "opus", "tier_default": index == 0}
                    for index, route in enumerate(routes)]
                rows = project_catalogue("claude", old_inventory(), config)
                config["_model_specs"] = route_specs({"models": rows})
                codex = project_codex(config, {"models": rows})
                self.assertEqual(codex["excluded"], [])
                self.assertIn(default, {row["slug"] for row in codex["models"]})
                expected = {"Claude Opus 5.5", "Claude Opus 5 (Legacy)", "Claude Fable 5.1",
                            "Claude Fable 5 (Legacy)", "Claude Sonnet 5.5"}
                self.assertEqual([len(codex["models"]), len(model_catalog(config)["data"])], [5, 5])
                self.assertEqual({row["display_name"] for row in codex["models"]}, expected)
                self.assertEqual({row["display_name"] for row in model_catalog(config)["data"]}, expected)
                self.assertIn("Claude Fable 5.1", identity_note(config["_model_specs"]["claude/fable"]))

    def test_api_labels_do_not_seed_unadvertised_models_or_change_routing(self):
        identifier = "claude-haiku-4-5-20251001"
        inventory = {"provider_id": "claude", "source": "provider_api", "models": [
            {"id": "claude-opus-4-6", "display_name": "Claude Opus 4.6", "context": 200000},
            {"id": identifier, "display_name": "Claude Haiku 4.5", "context": 200000}]}
        rows = project_catalogue("claude", inventory, self.config)
        self.assertEqual({row["model_id"]: row["display_name"] for row in rows}, {
            "claude-opus-4-6": "Claude Opus 4.6 (Legacy)", identifier: "Claude Haiku 4.5 (Legacy)"})

    def test_custom_labels_exact_metadata_and_unknown_models_survive(self):
        inventory = old_inventory()
        inventory["models"][0]["context"] = 12345
        inventory["models"][3]["context"] = 200000
        inventory["models"].append({"id": "claude-future", "display_name": "Future model", "context": 333333})
        self.config["branding_overrides"] = {"claude": {"modelLabels": {"claude-fable-5-1": "My Fable"}}}
        specs = route_specs({"models": project_catalogue("claude", inventory, self.config)})
        self.assertEqual(specs["claude/fable"]["display_name"], "My Fable")
        self.assertEqual(specs["claude/fable"]["context"], 1000000)
        self.assertEqual(specs["claude/claude-fable-5"]["context"], 200000)
        self.assertEqual(specs["claude/claude-future"]["context"], 333333)

    def test_antigravity_keeps_its_metadata_and_identifies_its_transport(self):
        inventory = {"provider_id": "antigravity", "source": "cli", "models": [
            {"id": "claude-opus-5.5", "display_name": "Claude Opus 5.5", "tools": True,
             "context": 200000, "reasoning": True, "effort_modes": ["high"]}]}
        rows = project_catalogue("antigravity", inventory, self.config)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["display_name"], "Claude Opus 5.5")
        self.assertEqual(rows[0]["model_id"], "claude-opus-5.5")
        self.config["claude_catalogue"] = [{"route": rows[0]["id"], "tier": "opus", "tier_default": True}]
        self.config["_model_specs"] = route_specs({"models": rows})
        self.assertEqual(model_catalog(self.config)["data"][0]["display_name"], "Claude Opus 5.5 · AntiGravity")
        self.assertEqual(project_codex(self.config, {"models": rows})["models"][0]["display_name"],
                         "Claude Opus 5.5 · AntiGravity")


if __name__ == "__main__":
    unittest.main()
