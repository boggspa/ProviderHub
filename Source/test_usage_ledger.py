"""The permanent usage ledger: one line per completed request, with request and account ids."""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import time
import unittest

import test_gateway_hub as hub
import usage_ledger


NOV_8 = 1_794_099_600.25  # 2026-11-08T01:00:00.250Z


class UsageLedgerUnitTests(unittest.TestCase):
    def test_folder_accounts_resolve_by_the_folder_the_turn_ran_in(self):
        connection = {"credential_mode": "cli", "cli_account": "work",
                      "cli_accounts": [{"id": "work", "label": "Work", "config_dir": "/tmp/lc-work"},
                                       {"id": "media", "label": "Media", "config_dir": "/tmp/lc-media"}]}
        ran_in = usage_ledger.account("claude", connection, "/tmp/lc-media")
        self.assertEqual((ran_in["id"], ran_in["label"], ran_in["source"]), ("media", "Media", "cli"))
        self.assertEqual(ran_in["folder_sha256"], usage_ledger.folder_digest("/tmp/lc-media"))
        self.assertNotIn("/tmp/lc-media", json.dumps(ran_in), "folder paths never reach the ledger")
        active = usage_ledger.account("claude", connection)
        self.assertEqual(active["id"], "work", "without a folder the active account is named")
        default = usage_ledger.account("codex", {"credential_mode": "cli"})
        self.assertEqual((default["id"], default["folder_sha256"]), ("default", None))

    def test_vibe_uses_folder_accounts_and_api_keys_use_key_accounts(self):
        vibe = usage_ledger.account("mistral", {"credential_mode": "vibe", "cli_account": "b",
                                                "cli_accounts": [{"id": "b", "label": "B", "config_dir": "/tmp/v"}]})
        self.assertEqual((vibe["id"], vibe["source"]), ("b", "vibe"))
        keyed = usage_ledger.account("kimi", {"credential_mode": "keychain", "key_account": "team",
                                              "key_accounts": [{"id": "team", "label": "Team"}]})
        self.assertEqual((keyed["id"], keyed["label"], keyed["folder_sha256"]), ("team", "Team", None))
        self.assertEqual(usage_ledger.account("kimi", {"credential_mode": "keychain"})["id"], "default")

    def test_entry_keeps_only_token_counts_and_flags_estimates(self):
        usage = {"input_tokens": 10, "output_tokens": 4, "cache_read_input_tokens": 3, "output_tokens_details":
                 {"reasoning_tokens": 2}, "service_tier": "default", "text": "never copied", "input_tokens_bad": -1}
        record = usage_ledger.entry("r1", "claude/opus", "claude", None, usage, surface="messages", now=NOV_8)
        self.assertEqual(record["usage"], {"input_tokens": 10, "output_tokens": 4, "cache_read_input_tokens": 3,
                                           "reasoning_output_tokens": 2})
        self.assertEqual(record["time"], "2026-11-08T01:00:00.250Z")
        self.assertEqual(record["account"]["id"], "unknown")
        self.assertFalse(record["estimated"])
        estimated = usage_ledger.entry("r2", "claude/opus", "claude", None,
                                       {"input_tokens": 9, "provider_hub_usage_source": "estimate"}, now=NOV_8)
        self.assertTrue(estimated["estimated"])

    def test_append_writes_private_monthly_files_and_never_rotates(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for index in range(3):
                self.assertTrue(usage_ledger.append(root, {"request_id": str(index)}, now=NOV_8))
            self.assertTrue(usage_ledger.append(root, {"request_id": "dec"}, now=NOV_8 + 31 * 86400))
            november = root / "usage-ledger" / "2026-11.jsonl"
            self.assertEqual([json.loads(line)["request_id"] for line in november.read_text().splitlines()], ["0", "1", "2"])
            self.assertTrue((root / "usage-ledger" / "2026-12.jsonl").exists())
            self.assertEqual(november.stat().st_mode & 0o777, 0o600)
            self.assertEqual((root / "usage-ledger").stat().st_mode & 0o777, 0o700)

    def test_append_failure_is_reported_not_raised(self):
        with tempfile.TemporaryDirectory() as temp:
            blocker = Path(temp) / "usage-ledger"
            blocker.write_text("a file where the folder should be")
            self.assertFalse(usage_ledger.append(Path(temp), {"request_id": "x"}))


class UsageLedgerGatewayTests(unittest.TestCase):
    """End to end through the real gateway, reusing the hub suite's mock upstream."""

    setUp = hub.GatewayHubHTTPTests.setUp
    tearDown = hub.GatewayHubHTTPTests.tearDown
    start_gateway = hub.GatewayHubHTTPTests.start_gateway
    request = hub.GatewayHubHTTPTests.request

    def test_every_completed_request_gets_one_permanent_line(self):
        route = self.start_gateway("deepseek", "deepseek-flash", {"reasoning_history": "native"})
        payload = {"model": "claude-fable-5", "max_tokens": 64,
                   "messages": [{"role": "user", "content": hub.LOCAL_REQUEST_TEXT}]}
        for _ in range(2):
            status, raw, _ = self.request(payload)
            self.assertEqual(status, 200, raw)
        files = list((self.root / "usage-ledger").glob("*.jsonl"))
        self.assertEqual(len(files), 1)
        text = files[0].read_text()
        lines = [json.loads(line) for line in text.splitlines()]
        self.assertEqual(len(lines), 2)
        self.assertEqual(len({line["request_id"] for line in lines}), 2, "request ids are unique")
        for line in lines:
            self.assertEqual((line["schema"], line["route"], line["provider_id"]), (1, route, "deepseek"))
            self.assertEqual(line["account"]["id"], "default")
            self.assertEqual(line["surface"], "messages")
            self.assertGreater(line["usage"].get("input_tokens", 0), 0)
        self.assertNotIn(hub.LOCAL_REQUEST_TEXT, text)
        self.assertNotIn(hub.PROVIDER_KEY, text)
        self.assertNotIn(str(self.root), text)

    def test_rotating_the_activity_log_leaves_the_ledger_intact(self):
        self.start_gateway("deepseek", "deepseek-flash", {"reasoning_history": "native"})
        (self.root / "activity.jsonl").write_bytes(b"x" * 600_000)
        status, raw, _ = self.request({"model": "claude-fable-5", "max_tokens": 64,
                                       "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(status, 200, raw)
        self.assertTrue((self.root / "activity.previous.jsonl").exists(), "activity rotated")
        ledger = next((self.root / "usage-ledger").glob("*.jsonl")).read_text().splitlines()
        self.assertEqual(len(ledger), 1)


if __name__ == "__main__":
    unittest.main()
