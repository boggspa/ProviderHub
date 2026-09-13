"""Migration and identity tests for provider-aware hub settings."""
import copy
import unittest

from branding import BrandingError
from hub_config import (
    connection_signature,
    defaults,
    normalize,
    project_catalogue,
)
from catalogue import route_specs


SLOTS = [
    ("claude-fable-5", "Fable 5", "fable", True),
    ("claude-opus-5", "Opus 5", "opus", True),
]


class SettingsMigrationTests(unittest.TestCase):
    def test_schema_version_is_an_exact_non_negative_integer(self):
        for value in ("3", 3.0, True, -1):
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize({"schema_version": value}, SLOTS, "mistral-test")
        self.assertEqual(normalize({}, SLOTS, "mistral-test")["schema_version"], 3)
        self.assertEqual(
            normalize({"schema_version": 3}, SLOTS, "mistral-test")["schema_version"], 3)
        with self.assertRaisesRegex(ValueError, "newer version"):
            normalize({"schema_version": 4}, SLOTS, "mistral-test")

    def test_codex_catalogue_is_optional_deduped_and_binds_the_default(self):
        base = defaults(SLOTS, "mistral-test")
        self.assertIsNone(base["codex_catalogue"])
        self.assertIsNone(normalize({}, SLOTS, "mistral-test")["codex_catalogue"])

        value = {
            "codex_model": "mistral/mistral-small-4",
            "codex_catalogue": [
                "mistral/mistral-small-4",
                "mistral/mistral-small-4",
                "ollama/deepseek-v4-flash:cloud",
            ],
        }
        normalized = normalize(value, SLOTS, "mistral-test")
        self.assertEqual(
            normalized["codex_catalogue"],
            ["mistral/mistral-small-4", "ollama/deepseek-v4-flash:cloud"],
        )
        self.assertEqual(normalized["codex_model"], "mistral/mistral-small-4")

        for catalogue in ([], "mistral/mistral-small-4", [None], ["mistral/"]):
            with self.subTest(catalogue=catalogue), self.assertRaises(ValueError):
                normalize({"codex_catalogue": catalogue}, SLOTS, "mistral-test")

        outside_default = {
            "codex_model": "mistral/other-model",
            "codex_catalogue": ["mistral/mistral-small-4"],
        }
        with self.assertRaisesRegex(ValueError, "from the catalogue"):
            normalize(outside_default, SLOTS, "mistral-test")

        # A catalogue selection without a default remains valid; the default
        # picker in the app only requires the default once one is chosen.
        without_default = normalize(
            {"codex_catalogue": ["mistral/mistral-small-4"]}, SLOTS, "mistral-test")
        self.assertIsNone(without_default["codex_model"])
        self.assertEqual(
            without_default["codex_catalogue"], ["mistral/mistral-small-4"])

    def test_auto_flags_require_json_booleans(self):
        for key in ("auto_mode", "auto_stop"):
            for value in ("false", "true", 0, 1, None, [], {}):
                with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                    normalize({key: value}, SLOTS, "mistral-test")
        self.assertFalse(normalize({"auto_mode": False}, SLOTS, "mistral-test")["auto_mode"])
        self.assertTrue(normalize({"auto_mode": True}, SLOTS, "mistral-test")["auto_mode"])

    def test_legacy_credential_mode_is_validated_before_migration(self):
        vibe = normalize({"credential_mode": "vibe"}, SLOTS, "mistral-test")
        separate = normalize({"credential_mode": "separate"}, SLOTS, "mistral-test")
        self.assertEqual(vibe["providers"]["mistral"]["credential_mode"], "vibe")
        self.assertEqual(separate["providers"]["mistral"]["credential_mode"], "keychain")
        for value in ("bogus", "keychain", None, True):
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize({"credential_mode": value}, SLOTS, "mistral-test")

    def test_region_only_mimo_setting_selects_its_matching_endpoint(self):
        settings = normalize(
            {"providers": {"mimo": {"region": "sgp"}}}, SLOTS, "mistral-test")
        self.assertEqual(settings["providers"]["mimo"]["region"], "sgp")
        self.assertEqual(
            settings["providers"]["mimo"]["base_url"],
            "https://token-plan-sgp.xiaomimimo.com/anthropic",
        )

    def test_credential_generation_and_mode_invalidate_catalogue_signature(self):
        connection = defaults(SLOTS, "mistral-test")["providers"]["mistral"]
        original = connection_signature("mistral", connection)

        replaced_key = copy.deepcopy(connection)
        replaced_key["credential_revision"] += 1
        self.assertNotEqual(original, connection_signature("mistral", replaced_key))

        changed_account_source = copy.deepcopy(connection)
        changed_account_source["credential_mode"] = "keychain"
        self.assertNotEqual(original, connection_signature("mistral", changed_account_source))

        for revision in (-1, True, 9_007_199_254_740_992):
            invalid = copy.deepcopy(connection)
            invalid["credential_revision"] = revision
            with self.subTest(revision=revision), self.assertRaises(ValueError):
                connection_signature("mistral", invalid)


class CataloguePresentationTests(unittest.TestCase):
    def settings(self, route="mistral/model-a"):
        return normalize({
            "mappings": {
                "claude-fable-5": route,
                "claude-opus-5": route,
            },
        }, SLOTS, "mistral-test")

    def test_explicit_model_label_override_precedes_provider_metadata(self):
        settings = normalize({
            "mappings": {
                "claude-fable-5": "mistral/model-a",
                "claude-opus-5": "mistral/model-a",
            },
            "branding_overrides": {
                "mistral": {
                    "displayProvider": "Private Label",
                    "modelLabels": {
                        "mistral/model-a": "Chosen model label",
                    },
                },
            },
        }, SLOTS, "mistral-test")
        projected = project_catalogue("mistral", {
            "source": "provider_api",
            "models": [{
                "id": "model-a",
                "aliases": [],
                "display_name": "Provider metadata label",
            }],
        }, settings)

        self.assertEqual(len(projected), 1)
        model = projected[0]
        self.assertEqual(model["id"], "mistral/model-a")
        self.assertEqual(model["model_id"], "model-a")
        self.assertEqual(model["provider_id"], "mistral")
        self.assertEqual(model["display_name"], "Chosen model label")
        self.assertEqual(model["presentation"]["runtimeProvider"], "mistral")
        self.assertEqual(model["presentation"]["displayProvider"], "Private Label")

    def test_branding_cannot_change_route_or_credential_identity(self):
        value = {
            "mappings": {
                "claude-fable-5": "deepseek/deepseek-flash",
                "claude-opus-5": "deepseek/deepseek-flash",
            },
            "branding_overrides": {
                "deepseek": {
                    "displayProvider": "Mistral",
                    "hueKey": "mistral",
                    "accent": "#112233",
                },
            },
        }
        settings = normalize(value, SLOTS, "mistral-test")
        signature = connection_signature("deepseek", settings["providers"]["deepseek"])
        without_branding = normalize({
            "mappings": value["mappings"],
        }, SLOTS, "mistral-test")

        self.assertEqual(settings["mappings"], value["mappings"])
        self.assertEqual(
            signature,
            connection_signature("deepseek", without_branding["providers"]["deepseek"]),
        )
        projected = project_catalogue("deepseek", {
            "models": [{"id": "deepseek-flash", "aliases": []}],
        }, settings)
        self.assertEqual(projected[0]["id"], "deepseek/deepseek-flash")
        self.assertEqual(projected[0]["provider_id"], "deepseek")
        self.assertEqual(projected[0]["presentation"]["runtimeProvider"], "deepseek")
        with self.assertRaises(BrandingError):
            normalize({"branding_overrides": {
                "deepseek": {"runtimeProvider": "mistral"},
            }}, SLOTS, "mistral-test")

    def test_equivalent_canonical_rows_coalesce_deterministically(self):
        settings = self.settings("mistral/model-latest")
        rows = [
            {
                "id": "model-2026-01", "canonical_id": "model-family",
                "aliases": ["model-latest"], "display_name": "Family",
                "context": 100_000, "tools": True, "vision": False,
                "reasoning": True, "effort_modes": ["none", "high"],
                "fast_mode": False, "source": "provider_api",
                "evidence": "https://provider.example/models/2026-01",
            },
            {
                "id": "model-2026-02", "canonical_id": "model-family",
                "aliases": ["model-latest"], "display_name": "Family",
                "context": 100_000, "tools": True, "vision": False,
                "reasoning": True, "effort_modes": ["none", "high"],
                "fast_mode": False, "source": "provider_api",
                "evidence": "https://provider.example/models/2026-02",
            },
        ]

        forward = project_catalogue("mistral", {"source": "provider_api", "models": rows}, settings)
        reverse = project_catalogue(
            "mistral", {"source": "provider_api", "models": list(reversed(rows))}, settings)
        self.assertEqual(forward, reverse)
        self.assertEqual(len(forward), 1)
        self.assertEqual(forward[0]["id"], "mistral/model-latest")
        self.assertEqual(forward[0]["model_id"], "model-latest")
        self.assertEqual(set(forward[0]["aliases"]), {
            "mistral/model-latest", "mistral/model-2026-01", "mistral/model-2026-02",
        })
        self.assertEqual(forward[0]["source"], "provider_api")
        self.assertEqual(forward[0]["discovery_source"], "provider_api")
        self.assertEqual(forward[0]["provider_id"], "mistral")

    def test_conflicting_cross_group_alias_is_removed_without_order_dependence(self):
        settings = self.settings("mistral/shared")
        rows = [
            {
                "id": "model-a", "canonical_id": "family-a", "aliases": ["shared"],
                "display_name": "A", "context": 100_000, "tools": True,
                "vision": False, "reasoning": False, "effort_modes": [],
                "fast_mode": False,
            },
            {
                "id": "model-b", "canonical_id": "family-b", "aliases": ["shared"],
                "display_name": "B", "context": 200_000, "tools": False,
                "vision": False, "reasoning": False, "effort_modes": [],
                "fast_mode": False,
            },
        ]
        forward = project_catalogue("mistral", {"models": rows}, settings)
        reverse = project_catalogue("mistral", {"models": list(reversed(rows))}, settings)

        self.assertEqual(forward, reverse)
        self.assertEqual([item["id"] for item in forward], ["mistral/model-a", "mistral/model-b"])
        self.assertTrue(all("mistral/shared" not in item["aliases"] for item in forward))
        self.assertTrue(all(item["ambiguous_aliases_removed"] == ["shared"] for item in forward))
        self.assertNotIn("mistral/shared", route_specs({"models": forward}))

    def test_actual_raw_id_owns_a_conflicting_alias(self):
        settings = self.settings("mistral/shared")
        projected = project_catalogue("mistral", {"models": [
            {
                "id": "shared", "canonical_id": "owner", "aliases": [],
                "display_name": "Owner", "context": 100_000, "tools": True,
            },
            {
                "id": "other", "canonical_id": "other", "aliases": ["shared"],
                "display_name": "Other", "context": 200_000, "tools": False,
            },
        ]}, settings)
        specs = route_specs({"models": projected})
        self.assertEqual(specs["mistral/shared"]["canonical_id"], "owner")
        other = next(item for item in projected if item["canonical_id"] == "other")
        self.assertNotIn("mistral/shared", other["aliases"])

    def test_one_raw_id_with_conflicting_capabilities_fails_closed(self):
        settings = self.settings("mistral/model-a")
        rows = [
            {"id": "model-a", "canonical_id": "family", "aliases": [],
             "display_name": "A", "context": 100_000, "tools": True},
            {"id": "model-a", "canonical_id": "family", "aliases": [],
             "display_name": "A", "context": 100_000, "tools": False},
        ]
        with self.assertRaisesRegex(ValueError, "conflicting specifications"):
            project_catalogue("mistral", {"models": rows}, settings)

    def test_catalogue_cannot_claim_another_provider_identity(self):
        settings = self.settings("mistral/model-a")
        with self.assertRaisesRegex(ValueError, "identity"):
            project_catalogue("mistral", {
                "provider_id": "deepseek",
                "models": [{"id": "model-a", "aliases": []}],
            }, settings)


if __name__ == "__main__":
    unittest.main()
