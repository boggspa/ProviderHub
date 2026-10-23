"""Extra CLI accounts: each is a config folder the CLI itself signed in to."""
import os
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import claude_cli_agent
import bridge_core
import cli_routes
import codex_cli_agent
import gateway
from bridge_core import SLOTS
from cli_routes import CliRouteError, plan_turn
from hub_config import cli_account_dir, connection_signature, normalize

WORK = "/Users/someone/.claude-work"


def settings(provider="claude", accounts=None, active=None):
    connection = {"credential_mode": "vibe" if provider == "mistral" else "cli"}
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


class VibeAccountTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.default = self.root / "default"
        self.work = self.root / "work"
        self.default.mkdir()
        self.work.mkdir()
        self.env = mock.patch.dict(os.environ, {"VIBE_HOME": str(self.default), "MISTRAL_API_KEY": "default-environment-key"})
        self.env.start()
        self.addCleanup(self.env.stop)
        keychain = mock.patch.object(bridge_core, "keychain_read", return_value="default-keychain-key")
        self.keychain = keychain.start()
        self.addCleanup(keychain.stop)

    def chosen(self, folder=None):
        return settings("mistral", [{"id": "work", "label": "Work", "config_dir": str(folder or self.work)}], "work")

    def test_settings_keep_vibe_mode_and_default_login(self):
        selected = self.chosen()
        self.assertEqual(selected["providers"]["mistral"]["credential_mode"], "vibe")
        self.assertEqual(cli_account_dir(selected, "mistral"), str(self.work))
        self.assertEqual(bridge_core.credentials(settings("mistral")), ("default-environment-key", "Vibe environment"))

    def test_selected_home_reads_its_own_config_and_env(self):
        (self.work / "config.toml").write_text('active_model = "work-model"\n[[models]]\nname = "work-model"\nprovider = "mistral"\n')
        (self.work / ".env").write_text('MISTRAL_API_KEY="work-key"\n')
        self.assertEqual(bridge_core.vibe_settings(str(self.work))["active_model"], "work-model")
        self.assertEqual(bridge_core.credentials(self.chosen()), ("work-key", "Vibe .env · Work"))
        self.keychain.assert_not_called()

    def test_empty_home_cannot_fall_back_to_default_credentials(self):
        with self.assertRaises(bridge_core.BridgeError):
            bridge_core.credentials(self.chosen())
        self.keychain.assert_not_called()
        state = bridge_core.vibe_auth_state(str(self.work))
        self.assertEqual(state["state"], "missing")
        self.assertNotIn("default-environment-key", json.dumps(state))

    def test_new_home_has_a_distinct_native_keychain_account(self):
        prepared = bridge_core.prepare_vibe_account(str(self.work))
        other = bridge_core.prepare_vibe_account(str(self.root / "other"))
        self.assertNotEqual(prepared["key_name"], other["key_name"])
        self.assertNotEqual(prepared["key_name"], "MISTRAL_API_KEY")
        self.assertEqual(bridge_core.prepare_vibe_account(str(self.work)), prepared)
        self.assertEqual(bridge_core.vibe_settings(str(self.work))["key_name"], prepared["key_name"])
        with mock.patch.dict(os.environ, {prepared["key_name"]: "wrong-inherited-key"}):
            self.keychain.return_value = "work-keychain-key"
            self.assertEqual(bridge_core.credentials(self.chosen()), ("work-keychain-key", "Vibe Keychain · Work"))
        self.keychain.assert_called_once_with("ai.mistral.vibe", prepared["key_name"])

    def test_setup_preserves_config_and_refuses_shared_keychain_names(self):
        config = self.work / "config.toml"
        original = '# Keep my theme\ntheme = "dracula"\n'
        config.write_text(original)
        bridge_core.prepare_vibe_account(str(self.work))
        self.assertTrue(config.read_text().startswith(original))
        shared = '[[providers]]\nname = "mistral"\napi_key_env_var = "MISTRAL_API_KEY"\n'
        config.write_text(shared)
        with self.assertRaisesRegex(bridge_core.BridgeError, "shared Keychain"):
            bridge_core.prepare_vibe_account(str(self.work))
        self.assertEqual(config.read_text(), shared)
        with self.assertRaisesRegex(bridge_core.BridgeError, "Default row"):
            bridge_core.prepare_vibe_account(str(self.default))
        with self.assertRaises(bridge_core.BridgeError):
            bridge_core.prepare_vibe_account("relative")

    def test_custom_default_keychain_name_is_also_shared(self):
        config = '[[providers]]\nname = "mistral"\napi_key_env_var = "CUSTOM_DEFAULT_KEY"\n'
        (self.default / "config.toml").write_text(config)
        (self.work / "config.toml").write_text(config)
        with self.assertRaises(bridge_core.BridgeError):
            bridge_core.credentials(self.chosen())
        self.keychain.assert_not_called()

    def test_opening_an_imported_home_does_not_rewrite_it(self):
        original = '# Keep the imported login\ntheme = "dracula"\n'
        config = self.work / "config.toml"
        config.write_text(original)
        with self.assertRaisesRegex(bridge_core.BridgeError, "shared Keychain"):
            bridge_core.prepare_vibe_account(str(self.work), setup=False)
        self.assertEqual(config.read_text(), original)
        self.assertEqual(list(self.work.iterdir()), [config])

    def test_probes_report_each_home_without_keys_or_cli_adapter(self):
        (self.work / ".env").write_text("MISTRAL_API_KEY=work-secret\n")
        with mock.patch.object(cli_routes, "adapter_for", side_effect=AssertionError("Vibe uses its saved key")):
            states = cli_routes.account_states(self.chosen(), "mistral")
        self.assertEqual([(s["id"], s["state"], s["active"]) for s in states],
                         [(None, "authenticated", False), ("work", "authenticated", True)])
        self.assertNotIn("work-secret", json.dumps(states))
        self.assertNotIn("default-environment-key", json.dumps(states))

    def test_account_selection_and_folder_change_invalidate_cached_credentials(self):
        connection = self.chosen()["providers"]["mistral"]
        signature = connection_signature("mistral", connection)
        self.assertNotEqual(signature, connection_signature("mistral", settings("mistral")["providers"]["mistral"]))
        moved = self.chosen(self.root / "moved")["providers"]["mistral"]
        self.assertNotEqual(signature, connection_signature("mistral", moved))
        connection["cli_accounts"][0]["label"] = "Renamed"
        self.assertEqual(signature, connection_signature("mistral", connection))

    def test_running_gateway_switches_keys_on_the_next_request(self):
        (self.work / ".env").write_text("MISTRAL_API_KEY=work-key\n")
        runtime = gateway.Runtime.__new__(gateway.Runtime)
        runtime.root, runtime.lock = self.root, threading.Lock()
        runtime.settings, runtime.key, runtime.credential_cache = settings("mistral"), None, {}
        path = self.root / "settings.json"

        def save(value):
            bridge_core.atomic_json(path, value)

        save(runtime.settings)
        self.assertEqual(runtime.provider_key("mistral"), "default-environment-key")
        save(self.chosen())
        self.assertEqual(runtime.provider_key("mistral"), "work-key")
        save(settings("mistral"))
        self.assertEqual(runtime.provider_key("mistral"), "default-environment-key")
        save(self.chosen(self.root / "missing"))
        with self.assertRaises(bridge_core.BridgeError):
            runtime.provider_key("mistral")


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
