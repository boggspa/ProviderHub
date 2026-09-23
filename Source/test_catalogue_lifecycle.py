"""Deterministic catalogue lifecycle tests; no real provider calls or keys."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

import gateway
from bridge_core import BridgeError, SLOTS, atomic_json, cached_catalogue, credentials
from catalogue_lifecycle import (
    FRESH_SECONDS,
    STALE_FALLBACK_SECONDS,
    catalogue_fingerprint,
    prepare_launch,
    refresh_all,
    runtime_fingerprint_error,
    validate_prepared_launch,
)
from gateway import Runtime
from hub_config import connection_signature, defaults, provider_label
from providers import PROVIDERS


NOW = datetime(2026, 9, 12, 10, 0, tzinfo=timezone.utc)


def settings_for(route="deepseek/deepseek-chat"):
    settings = defaults(SLOTS, "mistral-test")
    settings["mappings"] = {slot_id: route for slot_id, *_ in SLOTS}
    return settings


def model(model_id, *, context=131072, **extra):
    value = {
        "id": model_id,
        "canonical_id": model_id,
        "display_name": model_id,
        "aliases": [model_id],
        "context": context,
        "tools": True,
        "vision": False,
        "reasoning": True,
        "effort_modes": ["none", "high"],
        "fast_mode": False,
    }
    value.update(extra)
    return value


def write_catalogue(root, settings, provider_id, models, *, fetched_at=NOW, **extra):
    inventory = {
        "provider_id": provider_id,
        "source": "test_provider_api",
        "fetched_at": fetched_at.isoformat() if isinstance(fetched_at, datetime) else fetched_at,
        "models": models,
        "warnings": [],
        "connection_signature": connection_signature(
            provider_id, settings["providers"][provider_id]),
    }
    inventory.update(extra)
    atomic_json(Path(root) / "catalogues" / f"{provider_id}.json", inventory)
    return inventory


def credentials_ok(_settings, provider_id):
    return "TEST-ONLY-KEY", f"Mock {provider_id} credential"


class CatalogueLifecycleTests(unittest.TestCase):
    def test_key_revision_invalidates_cache_and_prepare_refreshes_selected_route(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = settings_for()
            write_catalogue(root, settings, "deepseek", [model("deepseek-chat")])
            settings["providers"]["deepseek"]["credential_revision"] += 1
            atomic_json(root / "settings.json", settings)
            calls = []

            def discover(current, provider_id, target):
                calls.append(provider_id)
                write_catalogue(target, current, provider_id, [model("deepseek-chat")])
                return "Mock key"

            result = prepare_launch(
                settings, root, now=NOW, credentials_fn=credentials_ok,
                discover_fn=discover,
            )

            self.assertTrue(result["ready"])
            self.assertEqual(calls, ["deepseek"])
            self.assertEqual(result["providers"]["deepseek"]["status"], "refreshed")
            self.assertEqual(result["errors"], [])

    def test_current_valid_cache_prepares_without_discovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = settings_for()
            write_catalogue(root, settings, "deepseek", [model("deepseek-chat")])

            def unexpected_discovery(*_):
                self.fail("fresh exact metadata must not trigger discovery")

            result = prepare_launch(
                settings, root, now=NOW, credentials_fn=credentials_ok,
                discover_fn=unexpected_discovery,
            )

            self.assertTrue(result["ready"])
            provider = result["providers"]["deepseek"]
            self.assertEqual(provider["status"], "current")
            self.assertLessEqual(provider["age_seconds"], FRESH_SECONDS)

    def test_recent_same_connection_cache_survives_transient_provider_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = settings_for()
            write_catalogue(
                root, settings, "deepseek", [model("deepseek-chat")],
                fetched_at=NOW - timedelta(days=2),
            )

            def unavailable(*_):
                raise ValueError("DeepSeek model discovery returned HTTP 503")

            result = prepare_launch(
                settings, root, now=NOW, credentials_fn=credentials_ok,
                discover_fn=unavailable,
            )

            self.assertTrue(result["ready"])
            self.assertEqual(
                result["providers"]["deepseek"]["status"],
                "cached_after_transient_error",
            )
            self.assertEqual(result["cached_provider_ids"], ["deepseek"])
            self.assertEqual(result["warnings"][0]["code"], "using_recent_cache")
            self.assertLessEqual(
                result["providers"]["deepseek"]["age_seconds"],
                STALE_FALLBACK_SECONDS,
            )

    def test_spent_quota_falls_back_to_recent_same_connection_cache(self):
        """A depleted quota is a billing state, not a metadata invalidation."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = settings_for()
            write_catalogue(
                root, settings, "deepseek", [model("deepseek-chat")],
                fetched_at=NOW - timedelta(seconds=137304),
            )

            def out_of_credit(*_):
                raise ValueError("DeepSeek model discovery returned HTTP 402.")

            result = prepare_launch(
                settings, root, now=NOW, credentials_fn=credentials_ok,
                discover_fn=out_of_credit,
            )

            self.assertTrue(result["ready"])
            self.assertEqual(
                result["providers"]["deepseek"]["status"],
                "cached_after_transient_error",
            )
            self.assertEqual(result["errors"], [])
            warning = result["warnings"][0]
            self.assertEqual(warning["code"], "using_recent_cache")
            # The quota itself still has to reach the user, on the provider card.
            self.assertIn("HTTP 402", warning["message"])

    def test_quota_beyond_the_fallback_window_still_blocks_launch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = settings_for()
            write_catalogue(
                root, settings, "deepseek", [model("deepseek-chat")],
                fetched_at=NOW - timedelta(days=8),
            )

            def out_of_credit(*_):
                raise ValueError("DeepSeek model discovery returned HTTP 402.")

            result = prepare_launch(
                settings, root, now=NOW, credentials_fn=credentials_ok,
                discover_fn=out_of_credit,
            )

            self.assertFalse(result["ready"])
            self.assertIn(
                "outside the seven-day fallback window",
                result["errors"][0]["message"],
            )

    def test_blocker_message_names_the_reason_that_actually_applies(self):
        """A cache inside the window must not be blamed on the stale window."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = settings_for()
            write_catalogue(
                root, settings, "deepseek", [model("deepseek-chat")],
                fetched_at=NOW - timedelta(days=2),
            )

            def forbidden(*_):
                raise ValueError("DeepSeek model discovery returned HTTP 403")

            result = prepare_launch(
                settings, root, now=NOW, credentials_fn=credentials_ok,
                discover_fn=forbidden,
            )

            message = result["errors"][0]["message"]
            self.assertFalse(result["ready"])
            self.assertNotIn("seven-day fallback window", message)
            self.assertIn("inside the seven-day window", message)
            self.assertIn("credential no longer covers these routes", message)

    def test_auth_error_never_falls_back_to_cached_account_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = settings_for()
            write_catalogue(
                root, settings, "deepseek", [model("deepseek-chat")],
                fetched_at=NOW - timedelta(days=2),
            )

            def forbidden(*_):
                raise ValueError("DeepSeek model discovery returned HTTP 403")

            result = prepare_launch(
                settings, root, now=NOW, credentials_fn=credentials_ok,
                discover_fn=forbidden,
            )

            self.assertFalse(result["ready"])
            self.assertEqual(result["providers"]["deepseek"]["status"], "blocked")
            self.assertEqual(result["errors"][0]["code"], "discovery_failed")
            self.assertIn("HTTP 403", result["errors"][0]["message"])

    def test_missing_credentials_block_even_with_fresh_exact_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = settings_for()
            write_catalogue(root, settings, "deepseek", [model("deepseek-chat")])

            def missing(*_):
                raise ValueError("Add the DeepSeek API key in Providers")

            result = prepare_launch(
                settings, root, now=NOW, credentials_fn=missing,
                discover_fn=lambda *_: self.fail("must not discover without credentials"),
            )

            issue = result["errors"][0]
            self.assertFalse(result["ready"])
            self.assertEqual(issue["code"], "missing_credentials")
            self.assertEqual(issue["routes"], ["deepseek/deepseek-chat"])
            self.assertEqual(issue["slots"], sorted(slot[0] for slot in SLOTS))
            self.assertIn("deepseek/deepseek-chat", issue["message"])

    def test_successful_refresh_that_removed_model_reports_exact_route_and_slots(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = settings_for("deepseek/deepseek-removed")
            write_catalogue(
                root, settings, "deepseek", [model("deepseek-removed")],
                fetched_at=NOW - timedelta(days=2),
            )

            def discover(current, provider_id, target):
                write_catalogue(target, current, provider_id, [model("deepseek-chat")])
                return "Mock key"

            result = prepare_launch(
                settings, root, now=NOW, credentials_fn=credentials_ok,
                discover_fn=discover,
            )

            issue = result["errors"][0]
            self.assertFalse(result["ready"])
            self.assertEqual(issue["code"], "route_not_advertised")
            self.assertEqual(issue["provider_id"], "deepseek")
            self.assertEqual(issue["routes"], ["deepseek/deepseek-removed"])
            self.assertEqual(issue["slots"], sorted(slot[0] for slot in SLOTS))
            self.assertIn("deepseek/deepseek-removed", issue["message"])
            self.assertNotIn("deepseek/deepseek-chat", issue["message"])
            self.assertEqual(settings["mappings"], {
                slot_id: "deepseek/deepseek-removed" for slot_id, *_ in SLOTS
            })

    def test_cli_mode_blockers_name_the_installed_cli_not_the_api(self):
        # Seen live: a CLI-mode blocker read "Codex (OpenAI API) catalogue does
        # not contain ...", sending the reader after an API catalogue that
        # discovery never contacted. Key-based sources keep the API name.
        def discover(current, provider_id, target):
            write_catalogue(target, current, provider_id, [model("gpt-6-astra")])
            return "Mock login"

        for mode, name in (("cli", "Codex (installed CLI)"), ("keychain", "Codex (OpenAI API)")):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                settings = settings_for("codex/gpt-6-luna")
                settings["providers"]["codex"]["credential_mode"] = mode

                prepared = prepare_launch(
                    settings, root, now=NOW, credentials_fn=credentials_ok,
                    discover_fn=discover,
                )
                validated = validate_prepared_launch(
                    settings, root, now=NOW, credentials_fn=credentials_ok)
                refreshed = refresh_all(
                    settings, root, provider_ids=["codex"], now=NOW,
                    credentials_fn=credentials_ok, discover_fn=discover,
                )

                issue = prepared["errors"][0]
                self.assertEqual(issue["code"], "route_not_advertised")
                self.assertEqual(issue["provider_name"], name)
                self.assertTrue(issue["message"].startswith(
                    f"{name} catalogue does not contain selected route codex/gpt-6-luna "))
                self.assertTrue(validated["errors"][0]["message"].startswith(
                    f"{name} catalogue does not contain selected route codex/gpt-6-luna."))
                self.assertEqual(refreshed["providers"]["codex"]["provider_name"], name)

    def test_provider_label_names_the_active_credential_source(self):
        settings = settings_for()
        for provider_id, descriptor in PROVIDERS.items():
            if settings["providers"][provider_id]["credential_mode"] != "cli":
                self.assertEqual(provider_label(settings, provider_id), descriptor["name"])
        cli_providers = ("codex", "claude", "muse", "grok", "antigravity")
        for provider_id in cli_providers:
            settings["providers"][provider_id]["credential_mode"] = "cli"
        self.assertEqual({provider_id: provider_label(settings, provider_id)
                          for provider_id in cli_providers}, {
            "codex": "Codex (installed CLI)",
            "claude": "Claude (installed CLI)",
            "muse": "Muse (installed CLI)",
            "grok": "Grok (installed CLI)",
            "antigravity": "AntiGravity (installed CLI)",
        })
        self.assertEqual(credentials(settings, "codex"), ("", "Codex CLI login"))
        # A rename in Appearance is the name the Providers pane lists.
        settings["branding_overrides"] = {"codex": {"displayProvider": "ChatGPT"}}
        self.assertEqual(provider_label(settings, "codex"), "ChatGPT (installed CLI)")
        self.assertEqual(credentials(settings, "codex"), ("", "ChatGPT CLI login"))

    def test_provider_error_without_usable_cache_is_an_exact_blocker(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = settings_for("cerebras/llama-test")

            def provider_error(*_):
                raise ValueError("Cerebras model discovery returned HTTP 503")

            result = prepare_launch(
                settings, root, now=NOW, credentials_fn=credentials_ok,
                discover_fn=provider_error,
            )

            issue = result["errors"][0]
            self.assertFalse(result["ready"])
            self.assertEqual(issue["provider_id"], "cerebras")
            self.assertEqual(issue["routes"], ["cerebras/llama-test"])
            self.assertIn("cerebras/llama-test", issue["message"])
            self.assertIn("HTTP 503", issue["message"])

    def test_unrelated_refresh_403_does_not_block_valid_selected_route(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = settings_for()

            def discover(current, provider_id, target):
                if provider_id == "cerebras":
                    raise ValueError("Cerebras model discovery returned HTTP 403")
                write_catalogue(target, current, provider_id, [model("deepseek-chat")])
                return "Mock key"

            refreshed = refresh_all(
                settings, root, provider_ids=["deepseek", "cerebras"], now=NOW,
                credentials_fn=credentials_ok, discover_fn=discover,
            )
            self.assertEqual(refreshed["providers"]["cerebras"]["status"], "error")
            self.assertEqual(refreshed["providers"]["deepseek"]["status"], "refreshed")
            self.assertEqual(refreshed["errors"][0]["provider_id"], "cerebras")

            prepared = prepare_launch(
                settings, root, now=NOW, credentials_fn=credentials_ok,
                discover_fn=lambda *_: self.fail("selected cache is already current"),
            )
            self.assertTrue(prepared["ready"])
            self.assertEqual(set(prepared["providers"]), {"deepseek"})

    def test_real_refresh_workers_are_terminated_at_one_batch_deadline(self):
        if "fork" not in __import__("multiprocessing").get_all_start_methods():
            self.skipTest("Provider Hub's bounded worker isolation is macOS/fork based")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = settings_for()

            def slow_discovery(*_):
                time.sleep(5)

            started = time.monotonic()
            with patch("bridge_core.credentials", side_effect=credentials_ok), \
                    patch("bridge_core.discover_provider", side_effect=slow_discovery), \
                    patch("catalogue_lifecycle.BATCH_TIMEOUT_SECONDS", 1):
                result = refresh_all(settings, root, provider_ids=["deepseek"], now=NOW)

            self.assertLess(time.monotonic() - started, 2.5)
            issue = result["errors"][0]
            self.assertEqual(issue["code"], "refresh_timeout")
            self.assertEqual(issue["provider_id"], "deepseek")

    def test_batch_timeout_can_use_recent_exact_same_connection_cache(self):
        if "fork" not in __import__("multiprocessing").get_all_start_methods():
            self.skipTest("Provider Hub's bounded worker isolation is macOS/fork based")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = settings_for()
            write_catalogue(
                root, settings, "deepseek", [model("deepseek-chat")],
                fetched_at=NOW - timedelta(days=2),
            )

            def slow_discovery(*_):
                time.sleep(5)

            with patch("bridge_core.credentials", side_effect=credentials_ok), \
                    patch("bridge_core.discover_provider", side_effect=slow_discovery), \
                    patch("catalogue_lifecycle.BATCH_TIMEOUT_SECONDS", 1):
                result = prepare_launch(settings, root, now=NOW)

            self.assertTrue(result["ready"])
            self.assertEqual(
                result["providers"]["deepseek"]["status"],
                "cached_after_transient_error",
            )
            self.assertEqual(result["warnings"][0]["code"], "using_recent_cache")

            validated = validate_prepared_launch(
                settings, root, now=NOW, credentials_fn=credentials_ok)
            self.assertTrue(validated["ready"])
            self.assertEqual(
                validated["providers"]["deepseek"]["status"],
                "recent_cache_validated",
            )

    def test_connection_signature_mismatch_cannot_be_transient_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = settings_for()
            write_catalogue(
                root, settings, "deepseek", [model("deepseek-chat")],
                fetched_at=NOW - timedelta(days=2),
            )
            settings["providers"]["deepseek"]["credential_revision"] += 1

            def unavailable(*_):
                raise ValueError("Could not reach DeepSeek model discovery")

            result = prepare_launch(
                settings, root, now=NOW, credentials_fn=credentials_ok,
                discover_fn=unavailable,
            )

            self.assertFalse(result["ready"])
            self.assertIn("different connection", result["errors"][0]["message"])

    def test_runtime_fingerprint_tracks_selected_planning_metadata_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = settings_for()
            selected = write_catalogue(
                root, settings, "deepseek", [model("deepseek-chat", context=8192)])
            first = catalogue_fingerprint(settings, root, now=NOW)

            selected["fetched_at"] = (NOW + timedelta(hours=1)).isoformat()
            selected["source"] = "background_refresh"
            selected["models"][0]["inference_status"] = "responded"
            selected["models"][0]["last_success"] = NOW.isoformat()
            atomic_json(root / "catalogues/deepseek.json", selected)
            write_catalogue(root, settings, "cerebras", [model("unrelated")])
            self.assertEqual(first, catalogue_fingerprint(settings, root, now=NOW))

            selected["models"][0]["context"] = 16384
            atomic_json(root / "catalogues/deepseek.json", selected)
            self.assertNotEqual(first, catalogue_fingerprint(settings, root, now=NOW))

    def test_runtime_fingerprint_includes_mapping_options_only_when_set(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = settings_for()
            write_catalogue(root, settings, "deepseek", [model("deepseek-chat", context=8192)])
            first = catalogue_fingerprint(settings, root, now=NOW)
            settings["mapping_options"] = {}
            self.assertEqual(first, catalogue_fingerprint(settings, root, now=NOW))
            settings["mapping_options"] = {"claude-fable-5": {"omit_system": True, "omit_tools": False}}
            self.assertNotEqual(first, catalogue_fingerprint(settings, root, now=NOW))


    def test_runtime_startup_snapshot_requires_restart_after_planning_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = settings_for()
            inventory = write_catalogue(
                root, settings, "deepseek", [model("deepseek-chat", context=8192)])
            atomic_json(root / "settings.json", settings)
            with patch("bridge_core.vibe_settings", return_value={"active_model": "mistral-test"}):
                runtime = Runtime(root, key="TEST-ONLY-KEY")

            inventory["models"][0]["context"] = 16384
            atomic_json(root / "catalogues/deepseek.json", inventory)
            expected = catalogue_fingerprint(settings, root, now=NOW)

            self.assertNotEqual(runtime.status()["catalogue_fingerprint"], expected)
            message = runtime_fingerprint_error(
                expected, runtime.status()["catalogue_fingerprint"], settings)
            self.assertIn("deepseek/deepseek-chat", message)
            self.assertIn("Restart the gateway", message)
            self.assertIsNone(runtime_fingerprint_error(expected, expected, settings))

    def test_activate_checks_runtime_fingerprint_before_profile_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = settings_for()
            prepared = {
                "mode": "prepare_launch", "ready": True,
                "catalogue_fingerprint": "new-planning-snapshot",
                "providers": {}, "warnings": [], "errors": [],
            }
            response = io.BytesIO(json.dumps({
                "running": True,
                "catalogue_fingerprint": "old-planning-snapshot",
            }).encode())
            with patch("sys.argv", ["gateway.py", "activate"]), \
                    patch("gateway.state_root", return_value=root), \
                    patch("gateway.load_settings", return_value=settings), \
                    patch("gateway.validate_prepared_launch", return_value=prepared), \
                    patch("gateway.gateway_token", return_value="T" * 40), \
                    patch("gateway.urllib.request.urlopen", return_value=response), \
                    patch("gateway.ClaudeProfile.activate") as activate:
                with self.assertRaisesRegex(BridgeError, "older catalogue snapshot"):
                    gateway.main()
            activate.assert_not_called()

    def test_legacy_projection_preserves_original_fetch_time(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = defaults(SLOTS, "mistral-legacy")
            fetched_at = (NOW - timedelta(days=30)).isoformat()
            atomic_json(root / "catalog.json", {
                "schema_version": 2,
                "fetched_at": fetched_at,
                "raw": {"data": [{
                    "id": "mistral-legacy",
                    "name": "mistral-legacy",
                    "max_context_length": 32768,
                    "capabilities": {
                        "completion_chat": True,
                        "reasoning": False,
                        "vision": False,
                        "function_calling": True,
                    },
                }]},
            })
            vibe = {
                "active_model": "mistral-legacy",
                "active_display_name": "Mistral Legacy",
                "configured_models": ["mistral-legacy"],
            }
            with patch("bridge_core.vibe_settings", return_value=vibe):
                summary = cached_catalogue(settings, root)["providers"]["mistral"]
            self.assertEqual(summary["fetched_at"], fetched_at)


if __name__ == "__main__":
    unittest.main()
