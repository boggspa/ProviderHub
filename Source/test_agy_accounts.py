"""Account isolation across sign-in, discovery, routing and retained agy turns."""
import os
from pathlib import Path
import shlex
import tempfile
import unittest
from unittest.mock import patch

import agy_accounts
import agy_cli_agent as adapter
import cli_routes
from codex_session_pool import SessionPool
from bridge_core import SLOTS
from hub_config import cli_account_dir, normalize
from test_agy_native_params import AgyResumedSession
from test_agy_protocols import PromptSession


class AccountHomeTests(unittest.TestCase):
    def test_rejects_default_home_ancestors_default_gemini_and_symlink_aliases(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "user"
            home.mkdir()
            alias = Path(directory) / "alias"
            alias.symlink_to(home / ".gemini", target_is_directory=True)
            with patch.object(agy_accounts.Path, "home", return_value=home):
                for value in ("", "relative", str(home), str(home.parent), "/",
                              str(home / ".gemini"), str(home / ".gemini/antigravity-cli"), str(alias)):
                    with self.subTest(value=value), self.assertRaises(ValueError):
                        agy_accounts.account_environment(value)

    def test_normalization_accepts_a_separate_home_and_rejects_nested_accounts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = str((Path(directory) / "profile").resolve())
            settings = normalize({}, SLOTS, "mistral-test")
            connection = settings["providers"]["antigravity"]
            connection["cli_accounts"] = [{"id": "work", "label": "Work", "config_dir": root}]
            connection["cli_account"] = "work"
            settings = normalize(settings, SLOTS, "mistral-test")
            self.assertEqual(cli_account_dir(settings, "antigravity"), root)
            connection = settings["providers"]["antigravity"]
            connection["cli_accounts"].append({"id": "other", "label": "Other", "config_dir": root + "/child"})
            with self.assertRaisesRegex(ValueError, "non-overlapping"):
                normalize(settings, SLOTS, "mistral-test")

    def test_existing_profile_cannot_redirect_its_state_into_another_account(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            profile = root / "profile"
            profile.mkdir()
            (profile / ".gemini").symlink_to(root / "other", target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "inside their account home"):
                agy_accounts.account_environment(str(profile))

    def test_sign_in_command_quotes_paths_and_removes_ambient_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            root = str((Path(directory) / "work's $(literal) home").resolve())
            with patch.dict(os.environ, {"GEMINI_API_KEY": "must-not-leak", "AGY_ACCOUNT": "wrong-account",
                                        "XDG_CONFIG_HOME": "/default-config"}), \
                    patch.object(agy_accounts, "resolve_binary", return_value="/native/agy"):
                prepared = agy_accounts.prepare_login(root)
            words = shlex.split(prepared["script"])
            self.assertEqual(words[:5], ["cd", root, "&&", "env", "-i"])
            self.assertEqual(words[-1], "/native/agy")
            self.assertIn("HOME=" + root, words)
            self.assertIn("XDG_CONFIG_HOME=" + root + "/.config", words)
            self.assertNotIn("must-not-leak", prepared["script"])
            self.assertNotIn("AGY_ACCOUNT=", prepared["script"])
            self.assertEqual(Path(root).stat().st_mode & 0o777, 0o700)
            self.assertEqual((Path(root) / ".run").stat().st_mode & 0o777, 0o700)


class AdapterAccountTests(unittest.TestCase):
    request = {"model": "gemini-3.8-flash", "messages": [{"role": "user", "content": "Reply OK."}]}

    def test_catalogue_runs_in_the_selected_account_home(self):
        seen = {}

        def capture(argv, *, env=None, **kwargs):
            seen.update(argv=argv, env=env)
            return 0, "gemini-3.8-flash-medium\tGemini Flash\n", ""

        with patch.object(adapter, "_resolve_binary", return_value="/native/agy"):
            rows, _ = adapter.catalogue(capture=capture, config_dir="/tmp/agy-work")
        self.assertEqual(seen["env"]["HOME"], str(Path("/tmp/agy-work").resolve()))
        self.assertEqual(seen["argv"], ["/native/agy", "models"])
        self.assertTrue(rows)

    def test_turns_use_the_account_home_and_default_turns_keep_the_normal_home(self):
        for home in (None, "/tmp/agy-work"):
            with self.subTest(home=home):
                session = PromptSession([{"event": "result", "result": {"status": "SUCCESS", "response": "OK"}}])
                with patch.object(adapter, "_resolve_binary", return_value="/native/agy"), \
                        patch.object(adapter, "StdioSession", return_value=session) as spawn, \
                        patch.dict(os.environ, {"GEMINI_API_KEY": "must-not-leak", "XDG_CONFIG_HOME": "/default-config"}):
                    events = list(adapter.run_turn({**self.request, **({"config_dir": home} if home else {})}))
                self.assertEqual(events[-1]["type"], "message_stop")
                env = spawn.call_args.kwargs["env"]
                self.assertEqual(env["HOME"], str(Path(home).resolve()) if home else str(Path.home()))
                self.assertEqual(env["XDG_CONFIG_HOME"], str(Path(home).resolve() / ".config") if home else "/default-config")
                self.assertNotIn("GEMINI_API_KEY", env)

    def test_invalid_account_cannot_fall_back_to_the_default_login(self):
        with patch.object(adapter, "StdioSession") as spawn:
            events = list(adapter.run_turn({**self.request, "config_dir": "relative"}))
        self.assertEqual([event["type"] for event in events], ["error"])
        spawn.assert_not_called()

    def test_auth_check_verifies_models_in_the_selected_home_without_inference(self):
        seen = {}

        def capture(argv, **kwargs):
            seen.update(argv=argv, **kwargs)
            return 0, "gemini-3.8-flash-medium\tGemini Flash\n", ""

        with patch.object(adapter, "_resolve_binary", return_value="/native/agy"):
            state = adapter.auth_state(capture=capture, config_dir="/tmp/agy-work")
        self.assertEqual(state["state"], "authenticated")
        self.assertEqual(seen["argv"], ["/native/agy", "models"])
        self.assertEqual(seen["env"]["HOME"], str(Path("/tmp/agy-work").resolve()))
        self.assertEqual(seen["timeout"], 30)
        self.assertIn("Verify the account email", state["detail"])

    def test_auth_check_only_marks_explicit_sign_in_requests_missing(self):
        cases = [(1, "", "Please sign in to view available models.", "missing"),
                 (1, "", "Network unavailable", "unknown"),
                 (1, "gemini-3.8-flash\tGemini Flash", "request failed", "unknown"),
                 (0, "Fetching available models...", "", "unknown")]
        with patch.object(adapter, "_resolve_binary", return_value="/native/agy"):
            for rc, out, err, expected in cases:
                with self.subTest(rc=rc, out=out, err=err):
                    state = adapter.auth_state(capture=lambda *a, **kw: (rc, out, err))
                    self.assertEqual(state["state"], expected)
            with patch.object(adapter, "_invoke", side_effect=TimeoutError("private diagnostic")):
                state = adapter.auth_state()
                self.assertEqual(state["state"], "unknown")
                self.assertNotIn("private diagnostic", state["detail"])

    def test_account_states_checks_default_and_secondary_without_mixing_results(self):
        settings = {"providers": {"antigravity": {"cli_accounts": [
            {"id": "work", "label": "Work", "config_dir": "/tmp/agy-work"}]}}}

        def capture(argv, **kwargs):
            return (0, "gemini-3.8-flash\tGemini Flash", "") if kwargs.get("env") else (
                1, "", "Please sign in to view available models.")

        with patch.object(adapter, "_resolve_binary", return_value="/native/agy"), \
                patch.object(adapter, "_default_capture", side_effect=capture):
            rows = cli_routes.account_states(settings, "antigravity")
        self.assertEqual([(row["id"], row["state"]) for row in rows],
                         [(None, "missing"), ("work", "authenticated")])

    def test_route_discovery_and_turn_planning_forward_the_selected_home(self):
        with patch.object(adapter, "auth_state", return_value={"state": "unknown"}) as auth, \
                patch.object(adapter, "catalogue", return_value=([], [])) as catalogue:
            cli_routes.discover_via_cli("antigravity", config_dir="/tmp/agy-work")
        auth.assert_called_once_with(config_dir="/tmp/agy-work")
        self.assertEqual(catalogue.call_args.kwargs["config_dir"], "/tmp/agy-work")
        plan = cli_routes.plan_turn("antigravity", "gemini-3.8-flash", self.request, {},
                                    wanted_output=64, config_dir="/tmp/agy-work")
        self.assertEqual(plan["body"]["config_dir"], "/tmp/agy-work")

    def test_retained_processes_are_never_reused_after_switching_account(self):
        from test_provider_continuity import TASK, next_history
        pool = SessionPool(lambda entry: entry.session.close(), capacity=4, idle_ttl=0, pending_ttl=60, label="AGY")
        self.addCleanup(pool.close)
        spawned = []

        def spawn(argv, **kwargs):
            session = AgyResumedSession([], argv, **kwargs)
            session.account_home = kwargs["env"]["HOME"]
            spawned.append(session)
            return session

        with patch.object(adapter, "StdioSession", side_effect=spawn), \
                patch.object(adapter, "_streaming_supported", return_value=True), \
                patch.object(adapter, "_resolve_binary", return_value=__file__):
            for home, history in (("/tmp/agy-one", TASK), ("/tmp/agy-two", next_history())):
                events = list(adapter.run_turn({**self.request, "config_dir": home, "history": history,
                    "conversation": "chat:member", "host_tool_schema": {"type": "object"}}, pool=pool))
                self.assertEqual(events[-1]["type"], "message_stop", events)
        self.assertEqual([session.account_home for session in spawned],
                         [str(Path(home).resolve()) for home in ("/tmp/agy-one", "/tmp/agy-two")])


if __name__ == "__main__":
    unittest.main()
