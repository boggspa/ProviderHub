"""Migration and identity tests for provider-aware hub settings."""
import copy
import unittest

from branding import BrandingError
from effort_map import MISTRAL_NARROW_EFFORTS, MISTRAL_REASONING_EFFORTS
from hub_config import (
    CLAUDE_TIER_MODELS,
    CLAUDE_TIERS,
    _FOREIGN_FAMILY,
    _row_slug_digest,
    claude_catalogue_rows,
    claude_row_id,
    claude_routes,
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

    def test_claude_catalogue_rows_validate_dedupe_and_pick_tier_defaults(self):
        base = defaults(SLOTS, "mistral-test")
        self.assertIsNone(base["claude_catalogue"])
        self.assertIs(base["claude_code_settings"], True)
        self.assertIs(base["claude_workflows"], False)
        value = {"claude_catalogue": [
            {"route": "mistral/mistral-small-4", "tier": "sonnet"},
            {"route": "mistral/mistral-small-4", "tier": "opus"},
            {"route": "kimi/k3", "tier": "fable", "compact_limit": 200000},
            {"route": "ollama/deepseek-v4-flash:cloud", "tier": "sonnet", "tier_default": True},
        ], "claude_workflows": True}
        normalized = normalize(value, SLOTS, "mistral-test")
        rows = normalized["claude_catalogue"]
        self.assertEqual([row["route"] for row in rows],
                         ["mistral/mistral-small-4", "kimi/k3", "ollama/deepseek-v4-flash:cloud"])
        self.assertEqual([row["tier_default"] for row in rows], [False, True, True])
        self.assertEqual(rows[1]["compact_limit"], 200000)
        self.assertNotIn("compact_limit", rows[0])
        self.assertIs(normalized["claude_workflows"], True)
        # The slot mappings stay saved for a switch back to mapping mode.
        self.assertEqual(set(normalized["mappings"]), {slot[0] for slot in SLOTS})
        for bad in ([], "mistral/x", [{"route": "mistral/x"}], [{"route": "mistral/x", "tier": "gpt"}],
                    [{"route": "mistral/x", "tier": "opus", "extra": 1}],
                    [{"route": "mistral/x", "tier": "opus", "compact_limit": 10}],
                    [{"route": "mistral/x", "tier": "opus", "tier_default": "yes"}], [None]):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                normalize({"claude_catalogue": bad}, SLOTS, "mistral-test")
        with self.assertRaises(ValueError):
            normalize({"claude_code_settings": "on"}, SLOTS, "mistral-test")
        self.assertIs(base["codex_accent_slider"], False)
        self.assertIs(normalize({"codex_accent_slider": True}, SLOTS, "mistral-test")["codex_accent_slider"], True)
        with self.assertRaises(ValueError):
            normalize({"codex_accent_slider": 1}, SLOTS, "mistral-test")
        self.assertIs(base["codex_hide_usage_banner"], False)
        self.assertIs(normalize({"codex_hide_usage_banner": True}, SLOTS, "mistral-test")["codex_hide_usage_banner"], True)
        with self.assertRaises(ValueError):
            normalize({"codex_hide_usage_banner": "yes"}, SLOTS, "mistral-test")

    def test_claude_row_ids_embed_the_tier_model_and_stay_unique(self):
        self.assertEqual(claude_row_id("mistral/mistral-vibe-cli-latest", "sonnet"), "claude-sonnet-5-mis-tral-vibe-cli-latest")
        self.assertEqual(claude_row_id("ollama/minimax-m3:cloud", "haiku"), "claude-haiku-4-5-oll-ama-min-imax-m3-cloud")
        self.assertEqual(claude_row_id("kimi/k3", "fable"), "claude-fable-5-ki-mi-k3")
        settings = {"claude_catalogue": [
            {"route": "ollama/minimax-m3:cloud", "tier": "opus", "tier_default": True},
            {"route": "ollama/minimax-m3-cloud", "tier": "opus", "tier_default": False}]}
        self.assertEqual([row["id"] for row in claude_catalogue_rows(settings)],
                         ["claude-opus-5-oll-ama-min-imax-m3-cloud", "claude-opus-5-oll-ama-min-imax-m3-cloud-2"])
        self.assertEqual(claude_catalogue_rows({"claude_catalogue": None}), [])

    def test_claude_row_ids_never_spell_a_family_desktop_refuses(self):
        # Desktop drops a picker row whose id names a third-party family, and
        # says nothing about why: the whole picker just empties.  Every route
        # below is a name it would have refused verbatim.
        routes = ["mistral/glm-5-2", "kimi/kimi-for-coding", "ollama/deepseek-v4-flash:cloud",
                  "gemini/gemini-3.8-flash", "qwen-token-plan/qwen3.8-max", "openai/gpt-5-codex",
                  "x/ds-pro", "y/abab-6", "z/k2.5-turbo", "a/ark-code-v2", "b/phi4-ling-unic",
                  "c/amazon.nova-pro", "d/mistral-mixtral-ministral"]
        for route in routes:
            for tier in CLAUDE_TIERS:
                identifier = claude_row_id(route, tier)
                self.assertIsNone(_FOREIGN_FAMILY.search(identifier), identifier)
                self.assertTrue(identifier.startswith(CLAUDE_TIER_MODELS[tier] + "-"), identifier)
        # A slug that cannot be hyphenated clear falls back to the digest, which
        # hex makes safe by construction.
        self.assertIsNone(_FOREIGN_FAMILY.search(_row_slug_digest("mistral/glm-5-2")))

    def test_claude_routes_cover_family_slots_with_tier_stand_ins(self):
        settings = {"mappings": {"claude-fable-5": "mistral/a"}, "claude_catalogue": None}
        self.assertEqual(claude_routes(settings), {"claude-fable-5": "mistral/a"})
        settings["claude_catalogue"] = [
            {"route": "kimi/k3", "tier": "opus", "tier_default": True},
            {"route": "mistral/mistral-small-4", "tier": "sonnet", "tier_default": True},
            {"route": "mistral/glm-5-2", "tier": "sonnet", "tier_default": False}]
        routes = claude_routes(settings)
        self.assertEqual(routes["claude-opus-5-ki-mi-k3"], "kimi/k3")
        self.assertEqual(routes["claude-sonnet-5-mis-tral-g-lm-5-2"], "mistral/glm-5-2")
        self.assertEqual(routes["claude-sonnet-5"], "mistral/mistral-small-4")
        self.assertEqual(routes["claude-sonnet-4-6"], "mistral/mistral-small-4")
        # No fable row: opus stands in. No haiku row: sonnet stands in.
        self.assertEqual(routes["claude-fable-5"], "kimi/k3")
        self.assertEqual(routes["claude-haiku-4-5"], "mistral/mistral-small-4")
        self.assertNotIn("mistral/a", routes.values())

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

    def test_codex_subagent_ranks_are_optional_qualified_and_bounded(self):
        self.assertIsNone(defaults(SLOTS, "mistral-test")["codex_subagent_rank"])
        self.assertIsNone(normalize({}, SLOTS, "mistral-test")["codex_subagent_rank"])

        normalized = normalize(
            {"codex_subagent_rank": {"mistral/mistral-small-4": 1,
                                     "ollama/deepseek-v4-flash:cloud": 5}},
            SLOTS, "mistral-test")
        self.assertEqual(normalized["codex_subagent_rank"],
                         {"mistral/mistral-small-4": 1, "ollama/deepseek-v4-flash:cloud": 5})
        # An empty mapping is the same statement as no mapping.
        self.assertIsNone(normalize({"codex_subagent_rank": {}}, SLOTS, "mistral-test")["codex_subagent_rank"])

        # 0 and 6 are outside the pool, and True is an int in Python but not a
        # rank anyone typed.
        for ranks in ({"mistral/a": 0}, {"mistral/a": 6}, {"mistral/a": -1},
                      {"mistral/a": "1"}, {"mistral/a": 1.0}, {"mistral/a": True},
                      ["mistral/a"], "mistral/a"):
            with self.subTest(ranks=ranks), self.assertRaises(ValueError):
                normalize({"codex_subagent_rank": ranks}, SLOTS, "mistral-test")

    def test_codex_chatgpt_account_defaults_off_and_must_be_boolean(self):
        self.assertIs(defaults(SLOTS, "mistral-test")["codex_chatgpt_account"], False)
        self.assertIs(normalize({}, SLOTS, "mistral-test")["codex_chatgpt_account"], False)
        self.assertIs(normalize({"codex_chatgpt_account": True}, SLOTS, "mistral-test")["codex_chatgpt_account"], True)
        for value in (1, "yes", None):
            with self.assertRaises(ValueError):
                normalize({"codex_chatgpt_account": value}, SLOTS, "mistral-test")

    def test_codex_apply_patch_is_optional_deduped_and_defaults_off(self):
        self.assertNotIn("codex_apply_patch", defaults(SLOTS, "mistral-test"))
        self.assertNotIn("codex_apply_patch", normalize({}, SLOTS, "mistral-test"))
        normalized = normalize({"codex_apply_patch": [
            "mistral/mistral-small-4", "mistral/mistral-small-4"]}, SLOTS, "mistral-test")
        self.assertEqual(normalized["codex_apply_patch"], ["mistral/mistral-small-4"])
        # An explicit empty list qualifies nothing and stays valid.
        self.assertEqual(normalize({"codex_apply_patch": []}, SLOTS, "mistral-test")["codex_apply_patch"], [])
        for value in ("mistral/mistral-small-4", [None], ["mistral/"]):
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize({"codex_apply_patch": value}, SLOTS, "mistral-test")

    def test_codex_apply_patch_all_defaults_off_and_must_be_boolean(self):
        self.assertIs(defaults(SLOTS, "mistral-test")["codex_apply_patch_all"], False)
        self.assertIs(normalize({}, SLOTS, "mistral-test")["codex_apply_patch_all"], False)
        self.assertIs(normalize({"codex_apply_patch_all": True}, SLOTS, "mistral-test")["codex_apply_patch_all"], True)
        for value in (1, "true", None, []):
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize({"codex_apply_patch_all": value}, SLOTS, "mistral-test")

    def test_codex_apply_patch_exclude_is_optional_deduped_and_validated(self):
        self.assertNotIn("codex_apply_patch_exclude", defaults(SLOTS, "mistral-test"))
        self.assertNotIn("codex_apply_patch_exclude", normalize({}, SLOTS, "mistral-test"))
        normalized = normalize({"codex_apply_patch_all": True, "codex_apply_patch_exclude": [
            "mistral/mistral-small-4", "mistral/mistral-small-4"]}, SLOTS, "mistral-test")
        self.assertEqual(normalized["codex_apply_patch_exclude"], ["mistral/mistral-small-4"])
        self.assertEqual(normalize({"codex_apply_patch_exclude": []}, SLOTS, "mistral-test")["codex_apply_patch_exclude"], [])
        for value in ("mistral/mistral-small-4", [None], ["mistral/"]):
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize({"codex_apply_patch_exclude": value}, SLOTS, "mistral-test")

    def test_claude_features_default_off_and_validate(self):
        expected = {"dictation": False, "builtin_browser": False, "claude_in_chrome": False,
                    "scheduled_tasks": False, "cowork_tab": False}
        self.assertEqual(defaults(SLOTS, "mistral-test")["claude_features"], expected)
        self.assertEqual(normalize({}, SLOTS, "mistral-test")["claude_features"], expected)
        enabled = normalize({"claude_features": {"dictation": True, "cowork_tab": True}}, SLOTS, "mistral-test")
        self.assertEqual(enabled["claude_features"], {**expected, "dictation": True, "cowork_tab": True})
        for value in ({"dictation": 1}, {"unknown": True}, ["dictation"], "dictation"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize({"claude_features": value}, SLOTS, "mistral-test")

    def test_auto_flags_require_json_booleans(self):
        for key in ("auto_mode", "auto_stop"):
            for value in ("false", "true", 0, 1, None, [], {}):
                with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                    normalize({key: value}, SLOTS, "mistral-test")
        self.assertFalse(normalize({"auto_mode": False}, SLOTS, "mistral-test")["auto_mode"])
        self.assertTrue(normalize({"auto_mode": True}, SLOTS, "mistral-test")["auto_mode"])

    def test_mapping_options_default_off_and_persist_only_true_omits(self):
        empty = normalize({}, SLOTS, "mistral-test")
        self.assertEqual(empty["mapping_options"], {})
        self.assertEqual(
            normalize({"mapping_options": {}}, SLOTS, "mistral-test")["mapping_options"], {})
        both = normalize({
            "mapping_options": {
                "claude-fable-5": {"omit_system": True, "omit_tools": True},
                "claude-opus-5": {"omit_system": False, "omit_tools": False},
            },
        }, SLOTS, "mistral-test")
        self.assertEqual(both["mapping_options"], {
            "claude-fable-5": {"omit_system": True, "omit_tools": True},
        })
        system_only = normalize({
            "mapping_options": {"claude-opus-5": {"omit_system": True}},
        }, SLOTS, "mistral-test")
        self.assertEqual(system_only["mapping_options"], {
            "claude-opus-5": {"omit_system": True, "omit_tools": False},
        })
        ignored = normalize({
            "mapping_options": {"claude-retired": {"omit_tools": True}},
        }, SLOTS, "mistral-test")
        self.assertEqual(ignored["mapping_options"], {})
        for value in (True, [], {"claude-fable-5": True},
                      {"claude-fable-5": {"omit_system": 1}},
                      {"claude-fable-5": {"omit_tools": "true"}},
                      {"claude-fable-5": {"drop": True}}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize({"mapping_options": value}, SLOTS, "mistral-test")

    def test_mapping_options_compact_limit_is_validated_and_persisted(self):
        only = normalize({
            "mapping_options": {"claude-fable-5": {"compact_limit": 100000}},
        }, SLOTS, "mistral-test")
        self.assertEqual(only["mapping_options"], {
            "claude-fable-5": {"omit_system": False, "omit_tools": False, "compact_limit": 100000},
        })
        # Null from an older or hand-edited file means "no override".
        cleared = normalize({
            "mapping_options": {"claude-fable-5": {"compact_limit": None}},
        }, SLOTS, "mistral-test")
        self.assertEqual(cleared["mapping_options"], {})
        for value in (0, -100, 999, 15000001, True, 100000.0, "100000", [100000]):
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize({"mapping_options": {"claude-fable-5": {"compact_limit": value}}},
                          SLOTS, "mistral-test")


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

    def test_stale_snapshot_cannot_override_verified_mistral_ladders(self):
        settings = self.settings("mistral/mistral-medium-2604")
        rows = [
            # Narrow-era bytes for full-ladder models: projection widens.
            {"id": "mistral-medium-2604", "canonical_id": "mistral-medium",
             "display_name": "Mistral Medium 3.5", "context": 262144,
             "tools": True, "vision": True, "reasoning": True,
             "effort_modes": ["none", "high"]},
            {"id": "mistral-small-2603", "canonical_id": "mistral-small",
             "display_name": "Mistral Small 4", "context": 262144,
             "tools": True, "vision": True, "reasoning": True,
             "effort_modes": ["none", "high"]},
            {"id": "glm-5-2", "canonical_id": "glm-5-2",
             "display_name": "GLM 5.2", "context": 262144,
             "tools": True, "vision": True, "reasoning": True,
             "effort_modes": ["none", "high"]},
            # An opaque raw ID is rescued by its canonical and billing names.
            {"id": "version-a", "canonical_id": "mistral-medium",
             "display_name": "version-a",
             "billing_model_name": "mistral-medium-3-5", "context": 262144,
             "tools": True, "vision": True, "reasoning": True,
             "effort_modes": []},
            # Full-era bytes for narrow models: projection narrows back, so a
            # stale snapshot cannot cause "reasoning_effort max" 400s.
            {"id": "mistral-large-2512", "canonical_id": "mistral-large",
             "display_name": "Mistral Large 3", "context": 262144,
             "tools": True, "vision": True, "reasoning": True,
             "effort_modes": ["none", "low", "medium", "high", "max"]},
            {"id": "codestral-2508", "canonical_id": "codestral",
             "display_name": "Codestral", "context": 262144,
             "tools": True, "vision": False, "reasoning": True,
             "effort_modes": ["none", "low", "medium", "high", "max"]},
            # Non-reasoning models still advertise no ladder.
            {"id": "mistral-small-2501", "canonical_id": "mistral-small",
             "display_name": "Mistral Small", "context": 262144,
             "tools": True, "vision": False, "reasoning": False,
             "effort_modes": ["none", "high"]},
        ]
        by_id = {item["model_id"]: item["effort_modes"] for item in project_catalogue(
            "mistral", {"source": "provider_api", "models": rows}, settings)}
        full = list(MISTRAL_REASONING_EFFORTS)
        narrow = list(MISTRAL_NARROW_EFFORTS)
        for model_id in ("mistral-medium-2604", "mistral-small-2603", "glm-5-2",
                         "version-a"):
            with self.subTest(model_id=model_id):
                self.assertEqual(by_id[model_id], full)
        for model_id in ("mistral-large-2512", "codestral-2508"):
            with self.subTest(model_id=model_id):
                self.assertEqual(by_id[model_id], narrow)
        self.assertEqual(by_id["mistral-small-2501"], [])

    def test_live_vibe_alias_cannot_inherit_a_narrow_canonical_twin(self):
        # The Codex desktop routes Mistral Medium 3.5 as
        # mistral/mistral-vibe-cli-latest, an alias the live Mistral API
        # advertises on the narrow "magistral-medium" card whose canonical
        # and billing names carry the same Mistral Medium 3.5 identity as
        # the full-ladder card. The configured alias must be re-derived from
        # that identity instead of inheriting the stale narrow bytes, and
        # the gateway spec built from the projection must agree.
        settings = self.settings("mistral/mistral-vibe-cli-latest")
        shared = {
            "context": 262_144, "tools": True, "vision": True,
            "reasoning": True, "canonical_id": "mistral-medium-latest",
            "display_name": "mistral-medium-latest",
            "billing_model_name": "mistral-medium-3-5",
        }
        rows = [
            {**shared, "id": "magistral-medium-latest",
             "aliases": ["magistral-medium-latest", "mistral-vibe-cli-latest",
                         "mistral-vibe-cli-with-tools"],
             "effort_modes": ["none", "high"]},
            {**shared, "id": "mistral-medium-latest",
             "aliases": ["mistral-medium-latest", "mistral-medium-3-5"],
             "effort_modes": ["none", "low", "medium", "high", "max"]},
        ]
        projected = project_catalogue(
            "mistral", {"source": "provider_api", "models": rows}, settings)
        vibe = next(item for item in projected
                    if item["model_id"] == "mistral-vibe-cli-latest")
        self.assertEqual(vibe["effort_modes"], list(MISTRAL_REASONING_EFFORTS))
        specs = route_specs({"models": projected})
        self.assertEqual(
            specs["mistral/mistral-vibe-cli-latest"]["effort_modes"],
            list(MISTRAL_REASONING_EFFORTS))

    def test_catalogue_cannot_claim_another_provider_identity(self):
        settings = self.settings("mistral/model-a")
        with self.assertRaisesRegex(ValueError, "identity"):
            project_catalogue("mistral", {
                "provider_id": "deepseek",
                "models": [{"id": "model-a", "aliases": []}],
            }, settings)


if __name__ == "__main__":
    unittest.main()
