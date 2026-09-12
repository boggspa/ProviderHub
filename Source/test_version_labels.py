"""Reported model versions must survive refresh without rewriting API routes."""
import copy
import unittest

from hub_config import project_catalogue
from model_names import friendly_model_name
from providers import discover


class VersionLabelTests(unittest.TestCase):
    def settings(self, route):
        return {"mappings": {"slot": "mistral/" + route}, "branding_overrides": {}}

    def test_raw_provider_names_do_not_override_known_product_versions(self):
        for identifier, expected in [
            ("mistral-small-2603", "Mistral Small 4"),
            ("mistral-large-2512", "Mistral Large 3"),
            ("ministral-14b-2512", "Ministral 3 · 14B"),
            ("labs-leanstral-1-5-1", "Leanstral 1.5.1 · Labs"),
        ]:
            with self.subTest(identifier=identifier):
                self.assertEqual(friendly_model_name(identifier, identifier), expected)
                rows = project_catalogue("mistral", {"models": [{
                    "id": identifier, "canonical_id": identifier,
                    "display_name": identifier, "billing_model_name": identifier,
                }]}, self.settings(identifier))
                self.assertEqual(rows[0]["display_name"], expected)
                self.assertEqual(rows[0]["id"], "mistral/" + identifier)
        # A shared billing family must not discard a known model patch version.
        leanstral = project_catalogue("mistral", {"models": [{
            "id": "labs-leanstral-1-5-1", "display_name": "labs-leanstral-1-5-1",
            "billing_model_name": "labs-leanstral-1-5",
        }]}, self.settings("labs-leanstral-1-5-1"))[0]
        self.assertEqual(leanstral["display_name"], "Leanstral 1.5.1 · Labs")

    def test_latest_alias_uses_reported_version_without_changing_route(self):
        row = {
            "id": "mistral-medium-latest", "canonical_id": "mistral-medium-latest",
            "display_name": "mistral-medium-latest", "billing_model_name": "mistral-medium-3-5",
            "aliases": ["mistral-medium-latest", "mistral-vibe-cli-latest"],
        }
        original = copy.deepcopy(row)
        settings = self.settings("mistral-vibe-cli-latest")
        result = project_catalogue("mistral", {"models": [row]}, settings)[0]
        self.assertEqual(result["display_name"], "Mistral Medium 3.5")
        self.assertEqual(result["model_id"], "mistral-vibe-cli-latest")
        self.assertIn("mistral/mistral-medium-latest", result["aliases"])
        self.assertEqual(row, original)
        row["billing_model_name"] = "mistral-medium-3-6"
        self.assertEqual(project_catalogue("mistral", {"models": [row]}, settings)[0]["display_name"], "Mistral Medium 3.6")

    def test_missing_version_is_not_guessed_and_user_override_still_wins(self):
        row = {"id": "mistral-medium-latest", "display_name": "mistral-medium-latest"}
        settings = self.settings(row["id"])
        self.assertEqual(project_catalogue("mistral", {"models": [row]}, settings)[0]["display_name"], "Mistral Medium · Latest")
        row["billing_model_name"] = "mistral-medium-3-5"
        settings["branding_overrides"] = {"mistral": {"modelLabels": {row["id"]: "My coding model"}}}
        self.assertEqual(project_catalogue("mistral", {"models": [row]}, settings)[0]["display_name"], "My coding model")

    def test_distinct_reported_versions_cannot_collapse_into_one_model(self):
        raw = {"data": [{
            "id": identifier, "name": "mistral-medium", "max_context_length": 262144,
            "billing_model_name": version,
            "capabilities": {"completion_chat": True, "function_calling": True},
        } for identifier, version in [("version-a", "mistral-medium-3"), ("version-b", "mistral-medium-3-5")]]}
        inventory = discover("mistral", {}, "mock-key", transport=lambda request: raw)
        self.assertEqual(len(inventory["models"]), 2)
        rows = project_catalogue("mistral", inventory, self.settings("version-a"))
        self.assertEqual({row["display_name"] for row in rows}, {"Mistral Medium 3", "Mistral Medium 3.5"})
        self.assertEqual({row["id"] for row in rows}, {"mistral/version-a", "mistral/version-b"})


if __name__ == "__main__":
    unittest.main()
