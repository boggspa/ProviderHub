"""Wonder discovery keeps API membership and CLI subscription models separate."""
import unittest
from unittest.mock import patch

import muse_cli_agent
from cli_routes import discover_via_cli, _hub_row
from provider_discovery import discover
from provider_requests import prepare_request


class MuseWonderTests(unittest.TestCase):
    def test_api_refresh_detects_wonder_and_adds_documented_capabilities(self):
        result = discover("muse", {}, "mock-key", transport=lambda _: {
            "data": [{"id": "muse-wonder-1.0"}]})
        row = result["models"][0]
        self.assertEqual([item["id"] for item in result["models"]], ["muse-wonder-1.0"])
        self.assertEqual(row["display_name"], "Muse Wonder 1.0")
        self.assertEqual(row["context"], 1048576)
        self.assertEqual(row["context_kind"], "verified_documentation")
        self.assertIsNone(row["max_output"])
        self.assertTrue(row["tools"])
        self.assertTrue(row["vision"])
        self.assertTrue(row["reasoning"])
        self.assertTrue(row["streaming"])
        self.assertEqual(row["effort_modes"], ["minimal", "low", "medium", "high", "xhigh", "max"])

    def test_api_account_metadata_overrides_documented_defaults(self):
        result = discover("muse", {}, "mock-key", transport=lambda _: {"data": [{
            "id": "muse-wonder-1.0", "display_name": "Account Wonder",
            "limits": {"max_context_length": 500000, "max_completion_tokens": 32768},
            "effort_modes": ["high"], "capabilities": {"vision": False}}]})
        row = result["models"][0]
        self.assertEqual(row["context"], 500000)
        self.assertEqual(row["max_output"], 32768)
        self.assertEqual(row["effort_modes"], ["high"])
        self.assertFalse(row["vision"])
        self.assertEqual(row["display_name"], "Account Wonder")

    def test_api_does_not_invent_wonder_or_subscription_full_membership(self):
        result = discover("muse", {}, "mock-key", transport=lambda _: {
            "data": [{"id": "muse-spark-1.3"}]})
        self.assertEqual([row["id"] for row in result["models"]], ["muse-spark-1.3"])
        # If the API later offers Full, trust that account's list rather than
        # hard-blocking the ID; don't borrow the CLI's limits/capabilities.
        future = discover("muse", {}, "mock-key", transport=lambda _: {
            "data": [{"id": "muse-wonder-full-1.0"}]})["models"][0]
        self.assertEqual(future["id"], "muse-wonder-full-1.0")
        self.assertIsNone(future["context"])
        self.assertEqual(future["effort_modes"], [])

    def test_live_cli_rows_preserve_both_exact_ids_and_cli_limits(self):
        for identifier in ("muse-wonder-1.0", "muse-wonder-full-1.0"):
            with self.subTest(identifier=identifier):
                row = muse_cli_agent._row({"modelId": identifier, "displayLabel": identifier,
                    "contextLimit": 1007997, "outputLimit": 128000})
                projected = _hub_row("muse", row)
                self.assertEqual(projected["id"], identifier)
                self.assertEqual(projected["context"], 1007997)
                self.assertEqual(projected["max_output"], 128000)
                self.assertEqual(projected["effort_modes"], muse_cli_agent.MUSE_EFFORTS)
                argv = muse_cli_agent.build_argv(identifier, effort="ultra")
                self.assertEqual(argv[argv.index("--model") + 1], identifier)
                self.assertEqual(argv[argv.index("--reasoning-effort") + 1], "ultra")

    def test_cli_fallback_contains_both_but_live_membership_takes_precedence(self):
        with patch.object(muse_cli_agent, "auth_state", return_value={"state": "unknown"}), \
                patch.object(muse_cli_agent, "catalogue", return_value=([], ["model/list unavailable"])):
            fallback = discover_via_cli("muse")
        rows = {row["id"]: row for row in fallback["models"]}
        self.assertIn("muse-wonder-1.0", rows)
        self.assertIn("muse-wonder-full-1.0", rows)
        self.assertEqual(rows["muse-wonder-full-1.0"]["context"], 1007997)
        self.assertTrue(fallback["warnings"])
        live = muse_cli_agent._row({"modelId": "muse-wonder-1.0", "contextLimit": 800000})
        with patch.object(muse_cli_agent, "auth_state", return_value={"state": "unknown"}), \
                patch.object(muse_cli_agent, "catalogue", return_value=([live], [])):
            result = discover_via_cli("muse")
        self.assertEqual([row["id"] for row in result["models"]], ["muse-wonder-1.0"])
        self.assertEqual(result["models"][0]["context"], 800000)

    def test_api_effort_does_not_switch_wonder_to_full(self):
        row = discover("muse", {}, "mock-key", transport=lambda _: {
            "data": [{"id": "muse-wonder-1.0"}]})["models"][0]
        for effort in row["effort_modes"] + ["ultra"]:
            with self.subTest(effort=effort):
                payload = {"model": "muse-wonder-1.0", "max_tokens": 1024,
                    "messages": [{"role": "user", "content": "Question"}],
                    "output_config": {"effort": effort}}
                request = prepare_request("muse", {}, "mock-key", payload, "muse-wonder-1.0", row)
                self.assertEqual(request["body"]["model"], "muse-wonder-1.0")
                self.assertEqual(request["body"]["output_config"]["effort"], "max" if effort == "ultra" else effort)


if __name__ == "__main__":
    unittest.main()
