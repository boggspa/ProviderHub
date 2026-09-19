"""Provider grouping at the Claude picker boundary, with disposable profiles."""
import copy
from pathlib import Path
import tempfile
import unittest

from bridge_core import ClaudeProfile, atomic_json, read_json
from hub_config import (SLOTS, claude_catalogue_rows, claude_picker_rows,
                        claude_routes, defaults, split_route)
from protocol import model_catalog


class ClaudePickerOrderTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        home = Path(temporary.name)
        self.profile = ClaudeProfile(home / "hub", home / "support", home / "claude")
        self.settings = defaults(SLOTS, "test-model")
        # Deliberately interleave accounts and keep Zulu before Alpha within
        # Mistral: provider grouping must not also sort the user's model order.
        self.settings["claude_catalogue"] = [
            {"route": "ollama/local-z", "tier": "haiku", "tier_default": True},
            {"route": "mistral/zulu", "tier": "opus", "tier_default": True},
            {"route": "kimi/long-context", "tier": "fable", "tier_default": True},
            {"route": "mistral/alpha", "tier": "opus", "tier_default": False},
            {"route": "ollama/local-a", "tier": "sonnet", "tier_default": True},
        ]
        self.settings["_model_specs"] = {}
        self.settings["_display_names"] = {}
        for entry in self.settings["claude_catalogue"]:
            route = entry["route"]
            self.add_spec(route, 1_048_576 if route.startswith("kimi/") else 262_144)

    def add_spec(self, route, context=262_144):
        provider, model = split_route(route)
        self.settings["_model_specs"][route] = {
            "id": route, "provider_id": provider, "model_id": model,
            "display_name": model, "context": context,
            "inference_status": "advertised",
        }
        self.settings["_display_names"][route] = model

    def test_discovery_groups_accounts_and_keeps_order_within_each_account(self):
        original_rows = claude_catalogue_rows(self.settings)
        expected_routes = ["kimi/long-context", "mistral/zulu", "mistral/alpha",
                           "ollama/local-z", "ollama/local-a"]
        expected_ids = {row["route"]: row["id"] for row in original_rows}
        expected_ids["kimi/long-context"] += "[1m]"

        catalogue = model_catalog(self.settings)

        self.assertEqual([row["id"] for row in catalogue["data"]],
                         [expected_ids[route] for route in expected_routes])
        self.assertEqual(catalogue["first_id"], expected_ids[expected_routes[0]])
        self.assertEqual(catalogue["last_id"], expected_ids[expected_routes[-1]])
        self.assertFalse(catalogue["has_more"])

    def test_managed_code_matches_discovery_and_preserves_user_picker_rows(self):
        user_rows = [{"model": "personal-z", "label": "Personal Z"},
                     {"model": "personal-a", "label": "Personal A"}]
        original = {"theme": "dark", "modelPicker": {
            "replaceBuiltInOptions": False, "options": user_rows}}
        atomic_json(self.profile.code_settings, original)

        self.profile.activate(self.settings, "local-test-token", require_closed=False)
        written = read_json(self.profile.code_settings)
        options = written["modelPicker"]["options"]

        self.assertEqual(options[:2], user_rows)
        self.assertEqual([row["model"] for row in options[2:]],
                         [row["id"] for row in model_catalog(self.settings)["data"]])
        self.assertEqual(options[2]["behavesAs"], "claude-fable-5[1m]")
        self.assertIs(written["modelPicker"]["replaceBuiltInOptions"], False)
        self.assertEqual(written["theme"], "dark")
        self.assertTrue(self.profile.restore(require_closed=False)["restored"])
        self.assertEqual(read_json(self.profile.code_settings), original)

    def test_sorting_preserves_colliding_ids_routing_defaults_and_saved_settings(self):
        entries = self.settings["claude_catalogue"]
        entries[0]["route"] = "ollama/alpha_beta"
        entries[-1]["route"] = "ollama/alpha-beta"
        entries[1]["compact_limit"] = 120_000
        self.add_spec("ollama/alpha_beta")
        self.add_spec("ollama/alpha-beta")
        original = copy.deepcopy(self.settings)
        original_rows = claude_catalogue_rows(self.settings)
        original_ids = {row["route"]: row["id"] for row in original_rows}
        original_routes = claude_routes(self.settings)
        self.assertEqual(original_ids["ollama/alpha-beta"],
                         original_ids["ollama/alpha_beta"] + "-2")

        ordered = claude_picker_rows(self.settings)
        model_catalog(self.settings)
        operation, error = self.profile.code_settings_operation(self.settings)

        self.assertIsNone(error)
        self.assertIsNotNone(operation)
        self.assertEqual({row["route"]: row["id"] for row in ordered}, original_ids)
        self.assertEqual(claude_routes(self.settings), original_routes)
        self.assertEqual(original_routes["claude-opus-5"], "mistral/zulu")
        self.assertEqual(original_routes["claude-haiku-4-5"], "ollama/alpha_beta")
        self.assertEqual(claude_catalogue_rows(self.settings), original_rows)
        self.assertEqual(self.settings, original)

    def test_branding_does_not_regroup_models_under_their_manufacturer(self):
        self.settings["_model_specs"]["ollama/local-z"]["presentation"] = {
            "displayProvider": "Aardvark"}
        self.settings["_model_specs"]["mistral/zulu"]["presentation"] = {
            "displayProvider": "Zebra"}

        rows = model_catalog(self.settings)["data"]

        expected_ids = {row["route"]: row["id"] for row in claude_catalogue_rows(self.settings)}
        self.assertEqual([row["id"].removesuffix("[1m]") for row in rows],
                         [expected_ids[route] for route in ("kimi/long-context", "mistral/zulu",
                          "mistral/alpha", "ollama/local-z", "ollama/local-a")])
        self.assertEqual(rows[1]["display_name"], "zulu · Zebra")
        self.assertEqual(rows[3]["display_name"], "local-z · Aardvark")

    def test_legacy_mapping_keeps_family_slot_order(self):
        self.settings["claude_catalogue"] = None
        for (slot, _, _, _), provider in zip(SLOTS, ("ollama", "mistral", "kimi", "mistral", "ollama")):
            route = f"{provider}/{slot}"
            self.settings["mappings"][slot] = route
            self.add_spec(route)
        original_routes = copy.deepcopy(self.settings["mappings"])

        self.assertEqual(claude_picker_rows(self.settings), [])
        self.assertEqual([row["id"] for row in model_catalog(self.settings)["data"]],
                         [slot for slot, _, _, _ in SLOTS])
        self.assertEqual(claude_routes(self.settings), original_routes)
        self.assertEqual(self.profile.code_settings_operation(self.settings), (None, None))


if __name__ == "__main__":
    unittest.main()
