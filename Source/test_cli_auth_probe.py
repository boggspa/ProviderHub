"""Process-free, socket-free tests for the read-only CLI capability probe.

Every test injects a fake ``capture``, so nothing here spawns a real CLI,
touches the network, or reads a credential file. The one test that inspects the
real filesystem reads this repo's own module source through ``ast`` to prove
the read-only doctrine holds - it never looks at a provider home.

Version, auth and model strings below are the verbatim output of the installed
CLIs, with account identifiers replaced by placeholders.
"""
from __future__ import annotations

import ast
import subprocess
import unittest
from pathlib import Path

import cli_auth_probe
from cli_auth_probe import (ALLOWED_VERBS, KNOWN_TURN_TRANSPORTS, SUPPORTED_PROVIDERS,
                            CliProbeError, _argv, _usable_for_routes, normalize_version,
                            probe_all, probe_provider, summarize)

#: Real strings, quoted from the installed binaries.
CODEX_VERSION = "codex-cli 0.153.0"
CLAUDE_VERSION = "2.1.276 (Claude Code)"
MUSE_VERSION = "Muse Code 1.3.0 (1.3.0-R3401.1)"
GROK_VERSION = "grok 1.0.34 (3736acbc8658) [stable]"
AGY_VERSION = "1.2.7"
CODEX_LOGIN_STATUS = "Logged in using ChatGPT"

#: ``claude auth status`` answers with pretty-printed JSON. Placeholders stand
#: in for the account email and org id so no live identity is committed here.
CLAUDE_AUTH_JSON = """{
  "loggedIn": true,
  "authMethod": "claude.ai",
  "apiProvider": "firstParty",
  "analyticsDisabled": false,
  "projectsDirectory": "/home/someone/.claude/projects",
  "configDirectory": "/home/someone/.claude",
  "email": "someone@example.com",
  "orgId": "00000000-0000-0000-0000-000000000000",
  "orgName": "Someone's Organization",
  "subscriptionType": "max"
}"""

CLAUDE_AUTH_SIGNED_OUT = '{"loggedIn": false, "authMethod": "", "apiProvider": "firstParty"}'

#: ``agy models`` prints a banner before the table. It goes to stderr in the
#: installed build, so both placements are covered.
AGY_MODELS_TABLE = (
    "gemini-3.8-flash-high\tGemini 3.8 Flash (High)\n"
    "gemini-3.1-pro-high\tGemini 3.1 Pro (High)\n"
    "claude-sonnet-4-6\tClaude Sonnet 4.6 (Thinking)\n"
    "claude-opus-4-6-thinking\tClaude Opus 4.6 (Thinking)\n"
    "gpt-oss-120b-medium\tGPT-OSS 120B (Medium)\n"
)
AGY_MODELS_STDOUT = "Fetching available models...\n" + AGY_MODELS_TABLE
AGY_BANNER = "Fetching available models..."

#: ``grok --no-auto-update models`` answers with the login line first, then the
#: listing, with the default model marked in place.
GROK_MODELS_OUTPUT = (
    "You are logged in with grok.com.\n"
    "\n"
    "Default model: grok-4.6\n"
    "\n"
    "Available models:\n"
    "  * grok-4.6 (default)\n"
    "  - grok-4.5\n"
)


def _tail(argv):
    """The verb part of an argv: everything after the binary, sans the grok prefix."""
    return tuple(part for part in argv[1:] if part != "--no-auto-update")


class FakeCapture:
    """A scripted stand-in for a subprocess call, recording every argv it sees.

    Keyed on the verb rather than the whole argv so a test does not have to
    restate each binary name, and so the grok ``--no-auto-update`` prefix cannot
    silently change which script entry matches.
    """

    def __init__(self, script=None, default=(1, "", "")):
        self.script = dict(script or {})
        self.default = default
        self.calls = []

    def __call__(self, argv, *, timeout):
        self.calls.append((list(argv), timeout))
        answer = self.script.get(" ".join(_tail(argv)), self.default)
        if isinstance(answer, BaseException):
            raise answer
        if callable(answer):
            return answer(argv)
        return answer

    @property
    def argvs(self):
        return [argv for argv, _ in self.calls]

    @property
    def timeouts(self):
        return [timeout for _, timeout in self.calls]


def answered(**script):
    """A capture whose every verb answers rc 0 with the given stdout."""
    return FakeCapture({verb: (0, text, "") for verb, text in script.items()})


class VersionNormalizationTests(unittest.TestCase):
    def test_all_five_real_version_shapes_reduce_to_a_bare_version(self):
        self.assertEqual(normalize_version(CODEX_VERSION), "0.153.0")
        self.assertEqual(normalize_version(CLAUDE_VERSION), "2.1.276")
        self.assertEqual(normalize_version(MUSE_VERSION), "1.3.0")
        self.assertEqual(normalize_version(GROK_VERSION), "1.0.34")
        self.assertEqual(normalize_version(AGY_VERSION), "1.2.7")

    def test_the_first_version_wins_over_a_build_id_in_parentheses(self):
        # Muse prints both the release and the build; the release is the fact.
        self.assertEqual(normalize_version("Muse Code 1.3.0 (1.3.0-R3401.1)"), "1.3.0")
        self.assertEqual(normalize_version("Muse Code 2.0.1 (9.9.9-R1.2)"), "2.0.1")

    def test_surrounding_prose_and_multiline_output_are_ignored(self):
        self.assertEqual(normalize_version("\n\n  codex-cli 0.153.0\nextra\n"), "0.153.0")

    def test_a_lone_number_is_still_a_version(self):
        self.assertEqual(normalize_version("build 7"), "7")

    def test_nothing_readable_normalizes_to_empty_rather_than_a_guess(self):
        self.assertEqual(normalize_version("unknown"), "")
        self.assertEqual(normalize_version(""), "")
        self.assertEqual(normalize_version("   \n  "), "")
        self.assertEqual(normalize_version(None), "")
        self.assertEqual(normalize_version(17), "")


class CodexProbeTests(unittest.TestCase):
    def test_happy_path_reads_the_real_version_and_login_line(self):
        capture = answered(**{"--version": CODEX_VERSION, "login status": CODEX_LOGIN_STATUS})
        probe = probe_provider("codex", capture=capture)
        self.assertEqual(probe["provider"], "codex")
        self.assertTrue(probe["installed"])
        self.assertEqual(probe["version"], "0.153.0")
        self.assertEqual(probe["version_raw"], CODEX_VERSION)
        self.assertEqual(probe["auth_state"], "authenticated")
        self.assertEqual(probe["auth_detail"], "Logged in using ChatGPT")
        self.assertEqual(probe["models"], [])

    def test_no_model_list_is_reported_as_a_warning_not_a_gap(self):
        capture = answered(**{"--version": CODEX_VERSION, "login status": CODEX_LOGIN_STATUS})
        probe = probe_provider("codex", capture=capture)
        self.assertTrue(any("no read-only model list" in note for note in probe["warnings"]))

    def test_a_non_zero_login_status_is_signed_out_not_unknown(self):
        capture = FakeCapture({"--version": (0, CODEX_VERSION, ""),
                               "login status": (1, "", "Not logged in")})
        probe = probe_provider("codex", capture=capture)
        self.assertTrue(probe["installed"])
        self.assertEqual(probe["auth_state"], "missing")
        self.assertIn("Not logged in", probe["auth_detail"])
        self.assertTrue(any("exited 1" in note for note in probe["warnings"]))

    def test_a_silent_success_does_not_claim_a_login(self):
        capture = answered(**{"--version": CODEX_VERSION, "login status": ""})
        probe = probe_provider("codex", capture=capture)
        self.assertEqual(probe["auth_state"], "unknown")
        self.assertTrue(any("without saying" in note for note in probe["warnings"]))


class ClaudeProbeTests(unittest.TestCase):
    def test_happy_path_parses_the_real_json_status(self):
        capture = answered(**{"--version": CLAUDE_VERSION, "auth status": CLAUDE_AUTH_JSON})
        probe = probe_provider("claude", capture=capture)
        self.assertTrue(probe["installed"])
        self.assertEqual(probe["version"], "2.1.276")
        self.assertEqual(probe["auth_state"], "authenticated")
        self.assertEqual(probe["auth_detail"], "claude.ai / max")
        self.assertEqual(probe["models"], [])

    def test_account_identifiers_are_not_carried_into_the_result(self):
        # The probe dict is rendered in a settings pane; an email and an org id
        # have no business appearing there from a capability check.
        capture = answered(**{"--version": CLAUDE_VERSION, "auth status": CLAUDE_AUTH_JSON})
        rendered = repr(probe_provider("claude", capture=capture))
        self.assertNotIn("someone@example.com", rendered)
        self.assertNotIn("00000000-0000-0000-0000-000000000000", rendered)
        self.assertNotIn("/home/someone/.claude", rendered)

    def test_a_signed_out_login_flag_is_missing(self):
        capture = answered(**{"--version": CLAUDE_VERSION, "auth status": CLAUDE_AUTH_SIGNED_OUT})
        probe = probe_provider("claude", capture=capture)
        self.assertEqual(probe["auth_state"], "missing")

    def test_unparseable_json_degrades_to_unknown_without_raising(self):
        for junk in ("not json at all", "{", "[1,2,3]", '"a string"', "null"):
            with self.subTest(junk=junk):
                capture = answered(**{"--version": CLAUDE_VERSION, "auth status": junk})
                probe = probe_provider("claude", capture=capture)
                self.assertEqual(probe["auth_state"], "unknown")
                self.assertTrue(probe["installed"])
                self.assertTrue(probe["warnings"])

    def test_a_non_zero_status_is_signed_out(self):
        capture = FakeCapture({"--version": (0, CLAUDE_VERSION, ""),
                               "auth status": (2, "", "Invalid authentication")})
        probe = probe_provider("claude", capture=capture)
        self.assertEqual(probe["auth_state"], "missing")
        self.assertTrue(any("exited 2" in note for note in probe["warnings"]))


class MuseProbeTests(unittest.TestCase):
    def test_help_availability_yields_unknown_with_an_explanation(self):
        capture = answered(**{"--version": MUSE_VERSION, "--help": "muse - interactive terminal coding agent"})
        probe = probe_provider("muse", capture=capture)
        self.assertTrue(probe["installed"])
        self.assertEqual(probe["version"], "1.3.0")
        self.assertEqual(probe["auth_state"], "unknown")
        self.assertIn("keeps its own login", probe["auth_detail"])
        self.assertEqual(probe["models"], [])

    def test_no_credential_storing_verb_is_ever_run(self):
        # `muse auth` stores credentials and `muse login` opens a browser; a
        # probe must reach for neither.
        capture = answered(**{"--version": MUSE_VERSION, "--help": "usage: muse"})
        probe_provider("muse", capture=capture)
        self.assertEqual([_tail(argv) for argv in capture.argvs], [("--version",), ("--help",)])


class GrokProbeTests(unittest.TestCase):
    def test_every_grok_probe_carries_the_no_auto_update_prefix(self):
        # A capability check must not be the thing that swaps the binary the hub
        # is about to route a turn through.
        capture = answered(**{"--version": GROK_VERSION, "models": GROK_MODELS_OUTPUT})
        probe_provider("grok", capture=capture)
        self.assertTrue(capture.argvs)
        for argv in capture.argvs:
            self.assertEqual(argv[1], "--no-auto-update", f"unprefixed grok probe: {argv}")

    def test_happy_path_reads_the_real_version(self):
        capture = answered(**{"--version": GROK_VERSION, "models": GROK_MODELS_OUTPUT})
        probe = probe_provider("grok", capture=capture)
        self.assertTrue(probe["installed"])
        self.assertEqual(probe["version"], "1.0.34")
        self.assertEqual(probe["version_raw"], GROK_VERSION)

    def test_models_output_yields_both_the_login_and_the_listing(self):
        capture = answered(**{"--version": GROK_VERSION, "models": GROK_MODELS_OUTPUT})
        probe = probe_provider("grok", capture=capture)
        self.assertEqual(probe["auth_state"], "authenticated")
        self.assertEqual(probe["auth_detail"], "You are logged in with grok.com.")
        self.assertEqual([model["id"] for model in probe["models"]], ["grok-4.6", "grok-4.5"])

    def test_the_in_place_default_marker_does_not_leak_into_an_id(self):
        capture = answered(**{"--version": GROK_VERSION, "models": GROK_MODELS_OUTPUT})
        probe = probe_provider("grok", capture=capture)
        self.assertNotIn("(default)", [model["id"] for model in probe["models"]])

    def test_no_login_line_leaves_auth_unknown_rather_than_missing(self):
        # The signed-out shape of `grok models` has not been observed, so the
        # probe reports absence of evidence instead of inventing a negative.
        capture = answered(**{"--version": GROK_VERSION,
                              "models": "Available models:\n  * grok-4.6\n"})
        probe = probe_provider("grok", capture=capture)
        self.assertEqual(probe["auth_state"], "unknown")
        self.assertTrue(any("did not confirm a login" in note for note in probe["warnings"]))

    def test_a_failing_models_call_degrades_to_unknown_and_no_models(self):
        capture = FakeCapture({"--version": (0, GROK_VERSION, ""),
                               "models": (1, "", "network unreachable")})
        probe = probe_provider("grok", capture=capture)
        self.assertTrue(probe["installed"])
        self.assertEqual(probe["auth_state"], "unknown")
        self.assertEqual(probe["models"], [])

    def test_only_a_default_model_still_advertises_one_id(self):
        capture = answered(**{"--version": GROK_VERSION,
                              "models": "You are logged in with grok.com.\n\nDefault model: grok-4.6\n"})
        probe = probe_provider("grok", capture=capture)
        self.assertEqual([model["id"] for model in probe["models"]], ["grok-4.6"])


class AntigravityProbeTests(unittest.TestCase):
    def test_happy_path_reads_the_real_version_and_model_table(self):
        capture = FakeCapture({
            "--version": (0, AGY_VERSION, ""),
            "auth status": (2, "", 'Error: unexpected argument "auth".'),
            "models": (0, AGY_MODELS_TABLE, AGY_BANNER),
        })
        probe = probe_provider("antigravity", capture=capture)
        self.assertEqual(probe["binary"], "agy")
        self.assertTrue(probe["installed"])
        self.assertEqual(probe["version"], "1.2.7")
        self.assertEqual(probe["auth_state"], "unknown")
        self.assertEqual([model["id"] for model in probe["models"]],
                         ["gemini-3.8-flash-high", "gemini-3.1-pro-high", "claude-sonnet-4-6",
                          "claude-opus-4-6-thinking", "gpt-oss-120b-medium"])
        self.assertEqual(probe["models"][0],
                         {"id": "gemini-3.8-flash-high", "display_name": "Gemini 3.8 Flash (High)"})

    def test_the_banner_on_stdout_is_skipped(self):
        # The installed build puts the banner on stderr, but a build that folds
        # it into stdout must not produce a model called "Fetching".
        capture = FakeCapture({"--version": (0, AGY_VERSION, ""),
                               "auth status": (2, "", "unexpected argument"),
                               "models": (0, AGY_MODELS_STDOUT, "")})
        probe = probe_provider("antigravity", capture=capture)
        ids = [model["id"] for model in probe["models"]]
        self.assertNotIn("Fetching available models...", ids)
        self.assertEqual(len(ids), 5)
        self.assertFalse(any("Fetching" in model["display_name"] for model in probe["models"]))

    def test_blank_lines_and_tab_less_lines_are_not_models(self):
        noisy = ("\n" + AGY_BANNER + "\n\n"
                 "gemini-3.8-flash-high\tGemini 3.8 Flash (High)\n"
                 "a line with no tab at all\n"
                 "\t\n"
                 "   \n"
                 "claude-sonnet-4-6\tClaude Sonnet 4.6 (Thinking)\n"
                 "trailing prose\n")
        capture = FakeCapture({"--version": (0, AGY_VERSION, ""),
                               "auth status": (2, "", "unexpected argument"),
                               "models": (0, noisy, "")})
        probe = probe_provider("antigravity", capture=capture)
        self.assertEqual([model["id"] for model in probe["models"]],
                         ["gemini-3.8-flash-high", "claude-sonnet-4-6"])

    def test_a_repeated_model_is_listed_once(self):
        capture = FakeCapture({"--version": (0, AGY_VERSION, ""),
                               "auth status": (2, "", "unexpected argument"),
                               "models": (0, "a\tA\na\tA again\n", "")})
        probe = probe_provider("antigravity", capture=capture)
        self.assertEqual([model["id"] for model in probe["models"]], ["a"])

    def test_a_model_with_no_display_name_falls_back_to_its_id(self):
        capture = FakeCapture({"--version": (0, AGY_VERSION, ""),
                               "auth status": (2, "", "unexpected argument"),
                               "models": (0, "gpt-oss-120b-medium\t\n", "")})
        probe = probe_provider("antigravity", capture=capture)
        self.assertEqual(probe["models"][0]["display_name"], "gpt-oss-120b-medium")

    def test_a_failing_models_call_reports_no_models_with_a_warning(self):
        capture = FakeCapture({"--version": (0, AGY_VERSION, ""),
                               "auth status": (2, "", "unexpected argument"),
                               "models": (1, "", "Fetching available models...\nError: unauthorized")})
        probe = probe_provider("antigravity", capture=capture)
        self.assertEqual(probe["models"], [])
        self.assertTrue(any("agy advertised no models" in note for note in probe["warnings"]))

    def test_an_auth_status_that_starts_working_is_believed(self):
        capture = FakeCapture({"--version": (0, AGY_VERSION, ""),
                               "auth status": (0, "Signed in as someone@example.com", ""),
                               "models": (0, AGY_MODELS_TABLE, "")})
        probe = probe_provider("antigravity", capture=capture)
        self.assertEqual(probe["auth_state"], "authenticated")


class FailureDegradationTests(unittest.TestCase):
    def test_a_missing_binary_is_not_an_error(self):
        capture = FakeCapture(default=FileNotFoundError(2, "No such file or directory"))
        for provider_id in SUPPORTED_PROVIDERS:
            with self.subTest(provider=provider_id):
                probe = probe_provider(provider_id, capture=capture)
                self.assertFalse(probe["installed"])
                self.assertEqual(probe["auth_state"], "unsupported")
                self.assertEqual(probe["models"], [])
                self.assertTrue(any("was not run" in note for note in probe["warnings"]))

    def test_the_missing_binary_warning_names_the_path_to_check(self):
        capture = FakeCapture(default=FileNotFoundError(2, "No such file or directory"))
        self.assertTrue(any("/opt/homebrew/bin/codex" in note
                            for note in probe_provider("codex", capture=capture)["warnings"]))
        self.assertTrue(any("~/.grok/bin/grok" in note
                            for note in probe_provider("grok", capture=capture)["warnings"]))

    def test_a_timeout_degrades_with_a_warning_instead_of_hanging_the_caller(self):
        capture = FakeCapture(default=subprocess.TimeoutExpired(cmd="agy", timeout=25))
        probe = probe_provider("antigravity", capture=capture)
        self.assertFalse(probe["installed"])
        self.assertEqual(probe["auth_state"], "unsupported")
        self.assertTrue(any("did not answer within" in note for note in probe["warnings"]))

    def test_a_timeout_on_the_auth_probe_alone_still_reports_the_install(self):
        capture = FakeCapture({"--version": (0, AGY_VERSION, ""),
                               "auth status": subprocess.TimeoutExpired(cmd="agy", timeout=25),
                               "models": (0, AGY_MODELS_TABLE, "")})
        probe = probe_provider("antigravity", capture=capture)
        self.assertTrue(probe["installed"])
        self.assertEqual(probe["auth_state"], "unknown")
        self.assertEqual(len(probe["models"]), 5)

    def test_an_unexpected_exception_never_reaches_the_caller(self):
        for boom in (RuntimeError("boom"), ValueError("bad"), OSError("io"),
                     KeyError("nope"), ZeroDivisionError("divide")):
            with self.subTest(error=boom):
                probe = probe_provider("codex", capture=FakeCapture(default=boom))
                self.assertFalse(probe["installed"])
                self.assertTrue(probe["warnings"])

    def test_a_garbage_answer_shape_is_absorbed(self):
        capture = FakeCapture({"--version": (None, None, None)})
        probe = probe_provider("codex", capture=capture)
        self.assertTrue(probe["installed"])
        self.assertEqual(probe["version"], "")
        self.assertEqual(probe["version_raw"], "")

    def test_a_non_zero_version_still_counts_as_installed(self):
        capture = FakeCapture({"--version": (2, "", "bad flag"), "login status": (0, CODEX_LOGIN_STATUS)})
        probe = probe_provider("codex", capture=capture)
        self.assertTrue(probe["installed"])
        self.assertTrue(any("`--version` exited 2" in note for note in probe["warnings"]))

    def test_the_timeout_is_passed_through_to_every_subprocess(self):
        capture = answered(**{"--version": AGY_VERSION, "auth status": "", "models": ""})
        probe_provider("antigravity", capture=capture, timeout=3)
        self.assertEqual(capture.timeouts, [3, 3, 3])

    def test_an_unusable_timeout_falls_back_to_the_default(self):
        capture = answered(**{"--version": CODEX_VERSION, "login status": CODEX_LOGIN_STATUS})
        probe_provider("codex", capture=capture, timeout=None)
        self.assertEqual(capture.timeouts[0], cli_auth_probe.DEFAULT_TIMEOUT)


class ProbeAllTests(unittest.TestCase):
    def test_all_five_are_returned_even_when_every_capture_raises(self):
        capture = FakeCapture(default=RuntimeError("everything is on fire"))
        probes = probe_all(capture=capture)
        self.assertEqual(tuple(probes), SUPPORTED_PROVIDERS)
        for provider_id, probe in probes.items():
            self.assertEqual(probe["provider"], provider_id)
            self.assertFalse(probe["installed"])
            self.assertEqual(probe["auth_state"], "unsupported")
            self.assertEqual(probe["models"], [])
            self.assertTrue(probe["warnings"])

    def test_one_wedged_cli_does_not_cost_the_others_their_row(self):
        def selective(argv):
            if argv[0] == "muse":
                raise subprocess.TimeoutExpired(cmd="muse", timeout=25)
            return (0, "1.0.0", "")

        probes = probe_all(capture=FakeCapture(default=selective))
        self.assertEqual(len(probes), 5)
        self.assertFalse(probes["muse"]["installed"])
        self.assertTrue(probes["codex"]["installed"])

    def test_a_healthy_machine_summarizes_to_five_usable_routes(self):
        capture = FakeCapture({
            "--version": (0, "1.0.0", ""),
            "login status": (0, CODEX_LOGIN_STATUS, ""),
            "auth status": (0, CLAUDE_AUTH_JSON, ""),
            "--help": (0, "usage", ""),
            "models": (0, AGY_MODELS_TABLE, AGY_BANNER),
        })
        probes = probe_all(capture=capture)
        summary = summarize(probes)
        self.assertEqual(summary["installed"], list(SUPPORTED_PROVIDERS))
        self.assertEqual(summary["missing"], [])
        self.assertEqual(len(summary["usable_for_routes"]), 5)
        # grok's models answer is read as agy's shape by this shared script, so
        # only the count that is independent of that aliasing is asserted.
        self.assertGreaterEqual(summary["model_count"], 5)


class UnknownProviderTests(unittest.TestCase):
    def test_an_unknown_provider_id_raises(self):
        for bad in ("devin", "gemini", "Codex", "", "agy", None, 7):
            with self.subTest(provider=bad):
                with self.assertRaises(CliProbeError):
                    probe_provider(bad, capture=answered())

    def test_the_error_names_what_is_supported(self):
        with self.assertRaises(CliProbeError) as caught:
            probe_provider("devin", capture=answered())
        self.assertIn("codex", str(caught.exception))


class AllowlistTests(unittest.TestCase):
    #: Verbs that would start a turn, mutate a provider home, or hand the CLI a
    #: prompt. None may ever reach an argv.
    FORBIDDEN = (
        (), ("exec",), ("exec", "--json"), ("agent",), ("serve",), ("app-server",),
        ("login",), ("logout",), ("auth",), ("auth", "login"), ("mcp",), ("update",),
        ("-p", "write a file"), ("--print", "hi"), ("--single", "hi"), ("-i",),
        ("--prompt-interactive",), ("resume", "--last"), ("setup",), ("init",),
        ("--help", "--dangerous"), ("models", "--json"), ("--version", "--extra"),
        ("config",), ("doctor",), ("delete",),
    )

    def test_the_allowlist_is_exactly_the_read_only_verbs(self):
        self.assertEqual(set(ALLOWED_VERBS),
                         {("--version",), ("--help",), ("login", "status"),
                          ("auth", "status"), ("models",)})

    def test_every_allowed_verb_builds_for_every_provider(self):
        for provider_id in SUPPORTED_PROVIDERS:
            for verb in ALLOWED_VERBS:
                with self.subTest(provider=provider_id, verb=verb):
                    argv = _argv(provider_id, verb)
                    self.assertEqual(_tail(argv), verb)

    def test_nothing_off_the_allowlist_can_be_constructed(self):
        for provider_id in SUPPORTED_PROVIDERS:
            for verb in self.FORBIDDEN:
                with self.subTest(provider=provider_id, verb=verb):
                    with self.assertRaises(CliProbeError):
                        _argv(provider_id, verb)

    def test_no_probe_ever_emits_an_argv_off_the_allowlist(self):
        # The structural guarantee: run every provider and audit what actually
        # left the module, rather than trusting the verb table to be complete.
        capture = FakeCapture(default=(0, "1.0.0", ""))
        probe_all(capture=capture)
        self.assertTrue(capture.argvs)
        for argv in capture.argvs:
            self.assertIn(_tail(argv), ALLOWED_VERBS, f"unallowlisted argv: {argv}")

    def test_no_probe_argv_carries_a_prompt_or_a_path(self):
        capture = FakeCapture(default=(0, "1.0.0", ""))
        probe_all(capture=capture)
        for argv in capture.argvs:
            for part in argv:
                self.assertNotIn("/", part)
                self.assertNotIn("~", part)
                self.assertLessEqual(len(part), 24)

    def test_an_unknown_provider_cannot_reach_the_argv_builder(self):
        with self.assertRaises(CliProbeError):
            _argv("devin", ("--version",))


class InjectedCaptureIsolationTests(unittest.TestCase):
    def test_an_injected_capture_keeps_the_probe_off_the_real_path(self):
        # Resolving a binary walks PATH; with a capture injected the caller owns
        # execution, so the probe reports the bare name and touches nothing.
        capture = answered(**{"--version": CODEX_VERSION, "login status": CODEX_LOGIN_STATUS})
        self.assertEqual(probe_provider("codex", capture=capture)["binary"], "codex")
        self.assertEqual(probe_provider("antigravity", capture=capture)["binary"], "agy")

    def test_the_default_capture_is_never_called_when_one_is_injected(self):
        def explode(*args, **kwargs):
            raise AssertionError("a probe with an injected capture must not spawn a process")

        original = cli_auth_probe.default_capture
        cli_auth_probe.default_capture = explode
        try:
            probes = probe_all(capture=answered(**{"--version": "1.0.0"}))
        finally:
            cli_auth_probe.default_capture = original
        self.assertEqual(len(probes), 5)

    def test_the_argv_handed_to_a_capture_starts_with_the_bare_binary_name(self):
        capture = answered(**{"--version": CODEX_VERSION, "login status": CODEX_LOGIN_STATUS})
        probe_provider("codex", capture=capture)
        self.assertEqual([argv[0] for argv in capture.argvs], ["codex", "codex"])


class SummarizeTests(unittest.TestCase):
    def row(self, provider_id, *, installed=True, auth_state="authenticated", models=()):
        return {"provider": provider_id, "installed": installed, "binary": provider_id,
                "version": "1.0.0", "version_raw": "1.0.0", "auth_state": auth_state,
                "auth_detail": "", "models": [{"id": m, "display_name": m} for m in models],
                "warnings": []}

    def healthy(self, **overrides):
        rows = {provider_id: self.row(provider_id) for provider_id in SUPPORTED_PROVIDERS}
        rows.update(overrides)
        return rows

    def test_the_rollup_shape_is_complete_and_ordered(self):
        summary = summarize(self.healthy())
        self.assertEqual(set(summary), {"installed", "authenticated", "missing",
                                        "model_count", "usable_for_routes"})
        self.assertEqual(summary["installed"], list(SUPPORTED_PROVIDERS))
        self.assertEqual(summary["authenticated"], list(SUPPORTED_PROVIDERS))
        self.assertEqual(summary["missing"], [])
        self.assertEqual(summary["model_count"], 0)
        self.assertEqual(summary["usable_for_routes"], list(SUPPORTED_PROVIDERS))

    def test_a_provider_with_no_binary_is_missing_and_unusable(self):
        summary = summarize(self.healthy(
            muse=self.row("muse", installed=False, auth_state="unsupported")))
        self.assertEqual(summary["installed"], ["codex", "claude", "grok", "antigravity"])
        self.assertEqual(summary["missing"], ["muse"])
        self.assertNotIn("muse", summary["usable_for_routes"])

    def test_a_signed_out_provider_is_missing_and_unusable(self):
        summary = summarize(self.healthy(claude=self.row("claude", auth_state="missing")))
        self.assertEqual(summary["missing"], ["claude"])
        self.assertIn("claude", summary["installed"])
        self.assertNotIn("claude", summary["authenticated"])
        self.assertNotIn("claude", summary["usable_for_routes"])

    def test_an_unknown_auth_state_still_counts_as_usable(self):
        # Route 2 means the CLI owns a login this probe cannot see; "unknown" is
        # the design working, not a reason to withhold the route.
        summary = summarize(self.healthy(muse=self.row("muse", auth_state="unknown")))
        self.assertIn("muse", summary["usable_for_routes"])
        self.assertEqual(summary["missing"], [])

    def test_model_count_totals_every_provider(self):
        summary = summarize(self.healthy(
            antigravity=self.row("antigravity", auth_state="unknown",
                                 models=("gemini-3.8-flash-high", "claude-sonnet-4-6")),
            grok=self.row("grok", models=("grok-4.6", "grok-4.5"))))
        self.assertEqual(summary["model_count"], 4)

    def test_a_provider_with_neither_models_nor_a_transport_is_not_usable(self):
        # Every real provider has a named transport, so the branch is only
        # reachable with a row that names none - asserted directly rather than
        # smuggled through summarize, which supplies the id itself.
        row = self.row("codex")
        self.assertTrue(_usable_for_routes(row))
        row["provider"] = "devin"
        self.assertFalse(_usable_for_routes(row))
        row["models"] = [{"id": "x", "display_name": "X"}]
        self.assertTrue(_usable_for_routes(row))

    def test_usability_requires_an_install_and_a_plausible_login(self):
        self.assertFalse(_usable_for_routes(self.row("codex", installed=False)))
        self.assertFalse(_usable_for_routes(self.row("codex", auth_state="missing")))
        self.assertFalse(_usable_for_routes(self.row("codex", auth_state="unsupported")))
        self.assertTrue(_usable_for_routes(self.row("codex", auth_state="unknown")))

    def test_junk_entries_are_ignored_rather_than_raised_on(self):
        probes = self.healthy()
        probes["devin"] = self.row("devin")
        probes["claude"] = None
        summary = summarize(probes)
        self.assertNotIn("devin", summary["installed"])
        self.assertNotIn("claude", summary["installed"])
        self.assertEqual(len(summary["installed"]), 4)

    def test_a_partially_built_result_still_summarizes(self):
        summary = summarize({"codex": self.row("codex")})
        self.assertEqual(summary["installed"], ["codex"])
        self.assertEqual(summary["usable_for_routes"], ["codex"])
        self.assertEqual(summary["model_count"], 0)

    def test_every_provider_has_a_named_turn_transport(self):
        self.assertEqual(set(KNOWN_TURN_TRANSPORTS), set(SUPPORTED_PROVIDERS))
        for transport in KNOWN_TURN_TRANSPORTS.values():
            self.assertIsInstance(transport, str)
            self.assertTrue(transport)


class ReadOnlyByConstructionTests(unittest.TestCase):
    """Audit the module's own source for the doctrine it claims.

    Reads this repo's ``Source/cli_auth_probe.py`` through ``ast`` - the only
    real-filesystem access in this file, and pointed at our own code rather
    than at any provider home.
    """

    #: Filesystem and unbounded-process entry points. ``subprocess.run`` is
    #: absent because it is the one sanctioned way out, asserted separately.
    FORBIDDEN_CALLS = {"open", "stat", "lstat", "read_text", "read_bytes", "write_text",
                       "write_bytes", "listdir", "scandir", "walk", "glob", "rglob",
                       "mkdir", "unlink", "remove", "rmtree", "chmod", "rename",
                       "find_generic_password", "Popen", "exec", "system", "spawn"}
    CREDENTIAL_HINTS = ("auth.json", "oauth_creds", "credentials.json", ".credentials",
                        "keychain", "security find", "id_rsa", ".netrc", "token")

    @classmethod
    def setUpClass(cls):
        source = (Path(__file__).resolve().parent / "cli_auth_probe.py").read_text()
        cls.tree = ast.parse(source)
        cls.docstrings = set()
        for node in ast.walk(cls.tree):
            if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                body = getattr(node, "body", [])
                if (body and isinstance(body[0], ast.Expr)
                        and isinstance(body[0].value, ast.Constant)
                        and isinstance(body[0].value.value, str)):
                    cls.docstrings.add(id(body[0].value))

    def test_no_forbidden_filesystem_or_process_call_appears_at_all(self):
        for node in ast.walk(self.tree):
            if not isinstance(node, ast.Call):
                continue
            target = node.func
            name = target.attr if isinstance(target, ast.Attribute) else getattr(target, "id", "")
            self.assertNotIn(name, self.FORBIDDEN_CALLS, f"the probe calls {name}()")

    def test_the_single_subprocess_call_is_bounded_and_cannot_read_stdin(self):
        # Matched on the qualified name, because `_Runner.run` - the channel
        # every probe goes through - is also a `.run` call and is not a process.
        runs = [node for node in ast.walk(self.tree)
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "run" and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "subprocess"]
        self.assertEqual(len(runs), 1, "expected exactly one subprocess.run in the module")
        keywords = {keyword.arg for keyword in runs[0].keywords}
        self.assertIn("timeout", keywords)
        # agy reads a prompt from stdin, so an inherited terminal is the
        # difference between a probe and a session.
        self.assertIn("stdin", keywords)
        self.assertIn("check", keywords)
        self.assertIn("env", keywords)

    def test_no_string_literal_names_a_credential_location(self):
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if id(node) in self.docstrings:
                    continue  # the doctrine is spelled out in prose on purpose
                lowered = node.value.lower()
                for hint in self.CREDENTIAL_HINTS:
                    self.assertNotIn(hint, lowered, f"credential-ish literal: {node.value!r}")

    def test_the_module_imports_only_the_standard_library(self):
        imported = set()
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                imported.add(node.module.split(".")[0])
        self.assertLessEqual(imported, {"__future__", "json", "re", "shutil", "subprocess",
                                        "typing", "cli_session"})

    def test_the_docstring_states_the_read_only_rule(self):
        doctrine = ast.get_docstring(self.tree) or ""
        self.assertIn("READ-ONLY BY CONSTRUCTION", doctrine)
        self.assertIn("credential file", doctrine)


class PublicApiTests(unittest.TestCase):
    def test_the_documented_surface_exists(self):
        self.assertEqual(SUPPORTED_PROVIDERS, ("codex", "claude", "muse", "grok", "antigravity"))
        self.assertTrue(issubclass(CliProbeError, RuntimeError))
        for name in ("probe_provider", "probe_all", "summarize"):
            self.assertTrue(callable(getattr(cli_auth_probe, name)))

    def test_the_probe_result_carries_exactly_the_documented_fields(self):
        capture = answered(**{"--version": CODEX_VERSION, "login status": CODEX_LOGIN_STATUS})
        self.assertEqual(set(probe_provider("codex", capture=capture)),
                         {"provider", "installed", "binary", "version", "version_raw",
                          "auth_state", "auth_detail", "models", "warnings"})

    def test_auth_states_are_drawn_from_the_documented_set(self):
        seen = set()
        for script in ({}, {"--version": (0, "1.0.0", "")},
                       {"--version": (0, "1.0.0", ""), "login status": (0, CODEX_LOGIN_STATUS, "")},
                       {"--version": (0, "1.0.0", ""), "auth status": (1, "", "nope")},
                       {"--version": (0, "1.0.0", ""), "models": (0, GROK_MODELS_OUTPUT, "")}):
            for provider_id in SUPPORTED_PROVIDERS:
                seen.add(probe_provider(provider_id, capture=FakeCapture(script))["auth_state"])
        self.assertLessEqual(seen, {"authenticated", "missing", "unknown", "unsupported"})


if __name__ == "__main__":
    unittest.main()
