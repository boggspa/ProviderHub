"""Extra CLI accounts: each is a config folder the CLI itself signed in to."""
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import claude_cli_agent
import cli_routes
import codex_cli_agent
import gateway
from bridge_core import SLOTS
from cli_routes import CliRouteError, plan_turn
from hub_config import cli_account_dir, normalize

WORK = "/Users/someone/.claude-work"


def settings(provider="claude", accounts=None, active=None):
    connection = {"credential_mode": "cli"}
    if accounts is not None:
        connection["cli_accounts"] = accounts
    if active is not None:
        connection["cli_account"] = active
    return normalize({"providers": {provider: connection}}, SLOTS, "mistral-test")


class SettingsTests(unittest.TestCase):
    def test_accounts_round_trip_and_resolve_the_active_folder(self):
        value = settings(accounts=[{"id": "work", "label": " Work ", "config_dir": WORK + "/"}], active="work")
        connection = value["providers"]["claude"]
        self.assertEqual(connection["cli_accounts"], [{"id": "work", "label": "Work", "config_dir": WORK}])
        self.assertEqual(cli_account_dir(value, "claude"), WORK)

    def test_no_active_account_means_the_default_login(self):
        value = settings(accounts=[{"id": "work", "label": "Work", "config_dir": WORK}])
        self.assertNotIn("cli_account", value["providers"]["claude"])
        self.assertIsNone(cli_account_dir(value, "claude"))
        # Providers without accounts do not grow the fields at all.
        self.assertNotIn("cli_accounts", value["providers"]["codex"])

    def test_home_relative_folders_expand(self):
        value = settings("codex", [{"id": "lite", "label": "Pro Lite", "config_dir": "~/.codex-lite"}], "lite")
        self.assertEqual(cli_account_dir(value, "codex"), os.path.expanduser("~/.codex-lite"))

    def test_rejections(self):
        cases = [
            ("claude", [{"id": "Work", "label": "W", "config_dir": WORK}], None),
            ("claude", [{"id": "w", "label": "", "config_dir": WORK}], None),
            ("claude", [{"id": "w", "label": "W", "config_dir": "relative/dir"}], None),
            ("claude", [{"id": "w", "label": "W", "config_dir": WORK, "token": "x"}], None),
            ("claude", [{"id": "w", "label": "A", "config_dir": WORK},
                        {"id": "w", "label": "B", "config_dir": WORK + "2"}], None),
            ("claude", [{"id": "a", "label": "A", "config_dir": WORK},
                        {"id": "b", "label": "B", "config_dir": WORK}], None),
            ("claude", [{"id": "w", "label": "W", "config_dir": WORK}], "other"),
            ("claude", None, "w"),
            ("grok", [{"id": "w", "label": "W", "config_dir": WORK}], None),
        ]
        for provider, accounts, active in cases:
            with self.subTest(provider=provider, accounts=accounts, active=active):
                with self.assertRaises(ValueError):
                    settings(provider, accounts, active)


class PlanTests(unittest.TestCase):
    payload = {"messages": [{"role": "user", "content": "hi"}]}

    def test_config_dir_reaches_the_adapter_request(self):
        plan = plan_turn("claude", "claude-sonnet-5", self.payload, {}, wanted_output=64, config_dir=WORK)
        self.assertEqual(plan["body"]["config_dir"], WORK)
        plan = plan_turn("claude", "claude-sonnet-5", self.payload, {}, wanted_output=64)
        self.assertNotIn("config_dir", plan["body"])

    def test_other_cli_providers_refuse_a_config_dir(self):
        with self.assertRaises(CliRouteError):
            plan_turn("grok", "grok-5", self.payload, {}, wanted_output=64, config_dir=WORK)


class AccountStatesTests(unittest.TestCase):
    def test_probes_the_default_and_every_account_folder(self):
        value = settings(accounts=[{"id": "w", "label": "Work", "config_dir": WORK}], active="w")

        def auth_state(config_dir=None):
            return {"state": "authenticated" if config_dir else "missing", "detail": str(config_dir)}

        with mock.patch.object(claude_cli_agent, "auth_state", side_effect=auth_state):
            states = cli_routes.account_states(value, "claude")
        self.assertEqual([(s["id"], s["state"], s["active"]) for s in states],
                         [(None, "missing", False), ("w", "authenticated", True)])
        self.assertEqual(states[1]["detail"], WORK)
        with self.assertRaises(CliRouteError):
            cli_routes.account_states(value, "grok")


class ClaudeAdapterTests(unittest.TestCase):
    request = {"model": "claude-sonnet-5", "messages": [{"role": "user", "content": "hi"}]}

    def test_turns_point_the_cli_at_the_account_folder(self):
        plan = claude_cli_agent._plan_turn({**self.request, "config_dir": WORK})
        self.assertEqual(plan["account_env"], {"CLAUDE_CONFIG_DIR": WORK})
        self.assertEqual(claude_cli_agent._plan_turn(self.request)["account_env"], {})
        with self.assertRaises(claude_cli_agent.ClaudeCliAgentError):
            claude_cli_agent._plan_turn({**self.request, "config_dir": "relative"})

    def test_live_sessions_never_cross_accounts(self):
        # Live sessions only exist for turns that offer host tools.
        tools = [{"name": "read", "description": "Read a file",
                  "input_schema": {"type": "object", "properties": {}}}]
        default = claude_cli_agent._plan_turn({**self.request, "tools": tools})
        work = claude_cli_agent._plan_turn({**self.request, "tools": tools, "config_dir": WORK})
        self.assertNotEqual(claude_cli_agent._pool_key(default), claude_cli_agent._pool_key(work))

    def test_auth_state_asks_about_the_account_folder(self):
        seen = {}

        def capture(argv, *, timeout, env=None):
            seen["env"] = env
            return 0, '{"loggedIn": true, "authMethod": "claude.ai", "email": "a@b.c", "subscriptionType": "max"}', ""

        with mock.patch.object(claude_cli_agent, "_resolve_binary", return_value="claude"):
            state = claude_cli_agent.auth_state(capture=capture, config_dir=WORK)
        self.assertEqual(seen["env"], {"CLAUDE_CONFIG_DIR": WORK})
        self.assertEqual(state["state"], "authenticated")
        self.assertIn("a@b.c", state["detail"])

    def test_a_signed_out_folder_reads_as_missing_despite_exit_1(self):
        def capture(argv, *, timeout, env=None):
            return 1, '{"loggedIn": false, "authMethod": "none"}', ""

        with mock.patch.object(claude_cli_agent, "_resolve_binary", return_value="claude"):
            self.assertEqual(claude_cli_agent.auth_state(capture=capture, config_dir=WORK)["state"], "missing")


class CodexAdapterTests(unittest.TestCase):
    def test_workspace_env_and_home_paths_follow_the_account(self):
        home = "/Users/someone/.codex-lite"
        with codex_cli_agent.CodexTurnWorkspace(codex_home=home) as workspace:
            self.assertEqual(workspace.env()["CODEX_HOME"], home)
        with codex_cli_agent.CodexTurnWorkspace() as workspace:
            self.assertNotIn("CODEX_HOME", workspace.env())
        self.assertEqual(codex_cli_agent._models_cache(home), Path(home) / "models_cache.json")
        self.assertEqual(codex_cli_agent._models_cache(), Path.home() / ".codex" / "models_cache.json")

    def test_request_carries_the_account_home(self):
        request = {"model": "gpt-6", "messages": [{"role": "user", "content": "hi"}]}
        payload = codex_cli_agent._normalise_request({**request, "config_dir": "/tmp/codex-b"})
        self.assertEqual(payload["codex_home"], "/tmp/codex-b")
        self.assertIsNone(codex_cli_agent._normalise_request(request)["codex_home"])

    def test_auth_state_asks_about_the_account_home(self):
        seen = {}

        def capture(argv, *, timeout, env=None):
            seen["env"] = env
            return 0, "Logged in using ChatGPT", ""

        state = codex_cli_agent.auth_state(capture=capture, config_dir="/tmp/codex-b")
        self.assertEqual(seen["env"], {"CODEX_HOME": "/tmp/codex-b"})
        self.assertEqual(state["state"], "authenticated")


class LiveSwitchTests(unittest.TestCase):
    def test_gateway_rereads_the_active_account_when_settings_change(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            path = root / "settings.json"
            path.write_text("{}")
            runtime = gateway.Runtime.__new__(gateway.Runtime)
            runtime.root, runtime.lock = root, threading.Lock()
            runtime.settings = settings()
            chosen = {"value": settings(accounts=[{"id": "w", "label": "W", "config_dir": WORK}], active="w")}
            with mock.patch.object(gateway, "load_settings", side_effect=lambda _root: chosen["value"]) as load:
                self.assertEqual(runtime.cli_account_dir("claude"), WORK)
                self.assertEqual(runtime.cli_account_dir("claude"), WORK)
                self.assertEqual(load.call_count, 1)  # unchanged file: no re-read
                chosen["value"] = settings()
                os.utime(path, ns=(path.stat().st_atime_ns, path.stat().st_mtime_ns + 1_000_000))
                self.assertIsNone(runtime.cli_account_dir("claude"))


if __name__ == "__main__":
    unittest.main()
