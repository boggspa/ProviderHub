"""Unit tests for the Grok CLI agent adapter."""
from __future__ import annotations

import json
import queue
import tempfile
import threading
import time
import tomllib
import unittest
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

# Import the module under test
import grok_cli_agent as module
import host_tools_mcp
from codex_session_pool import SessionPool


class TestProviderIdentity(unittest.TestCase):
    """Test provider identity constants."""

    def test_provider_id(self):
        self.assertEqual(module.PROVIDER_ID, "grok")

    def test_provider_name(self):
        self.assertEqual(module.PROVIDER_NAME, "Grok (Grok Build CLI)")

    def test_transport(self):
        self.assertEqual(module.TRANSPORT, "print")

    def test_binary_names(self):
        self.assertEqual(module.BINARY_NAMES, ("grok",))

    def test_system_prompt_transport(self):
        self.assertEqual(module.SYSTEM_PROMPT_TRANSPORT, "flag")



class TestEffortMapping(unittest.TestCase):
    """Test effort level mapping."""

    def test_grok_efforts(self):
        self.assertEqual(module.GROK_EFFORTS, ["low", "medium", "high", "xhigh"])

    def test_effort_aliases_desktop_to_grok(self):
        # none -> low
        self.assertEqual(module.GROK_EFFORT_ALIASES["none"], "low")
        self.assertEqual(module.GROK_EFFORT_ALIASES["minimal"], "low")
        # Pass-through
        self.assertEqual(module.GROK_EFFORT_ALIASES["low"], "low")
        self.assertEqual(module.GROK_EFFORT_ALIASES["medium"], "medium")
        self.assertEqual(module.GROK_EFFORT_ALIASES["high"], "high")
        self.assertEqual(module.GROK_EFFORT_ALIASES["xhigh"], "xhigh")
        # max/ultra -> xhigh (grok's top)
        self.assertEqual(module.GROK_EFFORT_ALIASES["max"], "xhigh")
        self.assertEqual(module.GROK_EFFORT_ALIASES["ultra"], "xhigh")


class TestValidateModel(unittest.TestCase):
    """Test model validation."""

    def test_none_returns_default(self):
        result = module._validate_model(None)
        self.assertEqual(result, "grok-4.7")

    def test_empty_string_returns_default(self):
        result = module._validate_model("")
        self.assertEqual(result, "grok-4.7")

    def test_whitespace_only_returns_default(self):
        result = module._validate_model("   ")
        self.assertEqual(result, "grok-4.7")

    def test_valid_model_pass_through(self):
        result = module._validate_model("grok-4.5")
        self.assertEqual(result, "grok-4.5")

    def test_known_models_match_grok_models_listing(self):
        self.assertEqual(module.KNOWN_MODELS,
                         ("grok-4.7", "grok-4.7-build-fast", "grok-4.6", "grok-4.5"))
        self.assertEqual(module.DEFAULT_MODEL, "grok-4.7")
        for model in module.KNOWN_MODELS:
            argv = module.build_argv(model, stream=True)
            self.assertEqual(argv[argv.index("--model") + 1], model)

    def test_model_stripped(self):
        result = module._validate_model("  grok-4.6  ")
        self.assertEqual(result, "grok-4.6")

    def test_non_string_raises(self):
        with self.assertRaises(module.GrokCliAgentError):
            module._validate_model(123)


class TestValidateEffort(unittest.TestCase):
    """Test effort validation."""

    def test_none_returns_none(self):
        result = module._validate_effort(None)
        self.assertIsNone(result)

    def test_valid_efforts_pass_through(self):
        for effort in module.GROK_EFFORTS:
            result = module._validate_effort(effort)
            self.assertEqual(result, effort)

    def test_desktop_efforts_mapped(self):
        # none -> low
        self.assertEqual(module._validate_effort("none"), "low")
        self.assertEqual(module._validate_effort("minimal"), "low")
        # max/ultra -> xhigh
        self.assertEqual(module._validate_effort("max"), "xhigh")
        self.assertEqual(module._validate_effort("ultra"), "xhigh")

    def test_unknown_effort_raises(self):
        with self.assertRaises(module.GrokCliAgentError) as ctx:
            module._validate_effort("invalid_effort")
        self.assertIn("unknown effort level", str(ctx.exception))

    def test_non_string_raises(self):
        with self.assertRaises(module.GrokCliAgentError):
            module._validate_effort(123)


class TestBuildArgv(unittest.TestCase):
    """Test argv construction."""

    def test_basic_argv(self):
        argv = module.build_argv("grok-4.6", stream=True)
        self.assertEqual(argv[0], "grok")
        self.assertIn("--no-auto-update", argv)
        self.assertIn("--single", argv)
        self.assertIn("--output-format", argv)
        self.assertIn("streaming-messages-json", argv)
        self.assertIn("--permission-mode", argv)
        self.assertIn("dontAsk", argv)
        self.assertIn("--no-subagents", argv)
        self.assertIn("--model", argv)
        self.assertIn("grok-4.6", argv)
        self.assertIn("--tools", argv)

    def test_no_auto_update_invariant(self):
        """Verify --no-auto-update is always present."""
        for model in [None, "grok-4.6", "grok-4.5"]:
            for effort in [None, "low", "medium", "high", "xhigh"]:
                for stream in [True, False]:
                    argv = module.build_argv(model, effort=effort, stream=stream)
                    self.assertIn("--no-auto-update", argv,
                                f"Missing --no-auto-update for model={model}, effort={effort}, stream={stream}")

    def test_forbidden_flags_asserted(self):
        """Test that forbidden flags cannot be constructed."""
        # The build_argv function should never produce these flags
        forbidden = module._FORBIDDEN_FLAGS
        for model in ["grok-4.6", "grok-4.5"]:
            for effort in [None, "low", "high"]:
                argv = module.build_argv(model, effort=effort, stream=True)
                for flag in forbidden:
                    self.assertNotIn(flag, argv,
                                    f"Forbidden flag {flag} found in argv")
                    # Also check prefixes
                    for arg in argv:
                        self.assertFalse(arg.startswith(flag),
                                        f"Flag prefix {flag} found in arg {arg}")

    def test_variadic_tools_last(self):
        """Test that --tools is always the last flag before its value."""
        argv = module.build_argv("grok-4.6", effort="low", stream=True)
        # Find --tools
        tools_idx = argv.index("--tools")
        # It should be the last flag (before its empty value)
        self.assertEqual(argv[tools_idx + 1], "")
        # Nothing should come after --tools ""
        self.assertEqual(tools_idx + 2, len(argv))

    def test_effort_in_argv(self):
        argv = module.build_argv("grok-4.6", effort="high", stream=True)
        self.assertIn("--reasoning-effort", argv)
        self.assertIn("high", argv)

    def test_system_in_argv_simple(self):
        argv = module.build_argv("grok-4.6", system="Be helpful", stream=True)
        self.assertIn("--system-prompt-override", argv)
        self.assertIn("Be helpful", argv)

    def test_system_in_argv_with_dash(self):
        argv = module.build_argv("grok-4.6", system="-custom", stream=True)
        # Should use attached form
        self.assertIn("--system-prompt-override=-custom", argv)

    def test_stream_false_uses_json(self):
        argv = module.build_argv("grok-4.6", stream=False)
        self.assertIn("--output-format", argv)
        self.assertIn("json", argv)

    def test_model_default(self):
        argv = module.build_argv(None, stream=True)
        self.assertIn("grok-4.7", argv)

    def test_search_keeps_web_search_alone(self):
        plain = module.build_argv("grok-4.7", stream=True)
        argv = module.build_argv("grok-4.7", stream=True, search=True)
        self.assertEqual(argv[-2:], ["--tools", "web_search"])
        self.assertIn("--disable-web-search", plain)
        self.assertNotIn("--disable-web-search", argv)
        removed = set(argv[argv.index("--disallowed-tools") + 1].split(","))
        self.assertEqual(set(plain[plain.index("--disallowed-tools") + 1].split(",")) - removed,
                         {"web_search"})
        self.assertIn("web_fetch", removed)
        self.assertEqual(argv[argv.index("WebFetch") - 1], "--deny")

    def test_extra_removals_extend_the_removal_list_safely(self):
        self.assertIn("sports_search", module._DISALLOWED_TOOLS)
        argv = module.build_argv("grok-4.7", stream=True, extra_removals=[
            "new_tool", "-flag", "bad name", "a,b", "web_search", "read_file", 7])
        removed = argv[argv.index("--disallowed-tools") + 1].split(",")
        self.assertIn("new_tool", removed)
        self.assertNotIn("-flag", removed)
        self.assertNotIn("bad name", removed)
        self.assertNotIn("a", removed)
        self.assertEqual(removed.count("read_file"), 1)
        self.assertEqual(argv[-2:], ["--tools", ""])
        search = module.build_argv("grok-4.7", stream=True, search=True,
                                   extra_removals=["web_search", "sports_search"])
        kept = search[search.index("--disallowed-tools") + 1].split(",")
        self.assertNotIn("web_search", kept)
        self.assertIn("sports_search", kept)


class TestSearchModels(unittest.TestCase):
    """Backend search capability comes from the CLI's own model cache."""

    def test_backend_search_models_come_from_the_cli_cache(self):
        with tempfile.TemporaryDirectory() as root:
            cache = Path(root) / "models_cache.json"
            cache.write_text(json.dumps({"models": {
                "grok-4.7": {"info": {"id": "grok-4.7", "supports_backend_search": True}},
                "grok-4.5": {"info": {"id": "grok-4.5", "supports_backend_search": False}},
                "grok-odd": {"info": "not a row"}}}))
            with patch.object(module, "_MODELS_CACHE", cache):
                self.assertEqual(module.search_models(), frozenset({"grok-4.7"}))
                cache.write_text("{torn")
                self.assertEqual(module.search_models(), frozenset())
                cache.write_text("[]")
                self.assertEqual(module.search_models(), frozenset())
            with patch.object(module, "_MODELS_CACHE", Path(root) / "missing.json"):
                self.assertEqual(module.search_models(), frozenset())


class TestPromptRendering(unittest.TestCase):
    """Test prompt rendering for stateless conversation."""

    def test_single_user_message(self):
        messages = [{"role": "user", "content": "Hello"}]
        prompt = module.render_prompt(messages)
        self.assertEqual(prompt, "Hello")

    def test_single_user_message_with_system(self):
        messages = [{"role": "user", "content": "Hello"}]
        prompt = module.render_prompt(messages, system="Be helpful")
        self.assertIn("<system>", prompt)
        self.assertIn("Be helpful", prompt)
        self.assertIn("Hello", prompt)
        self.assertIn(module._TRANSCRIPT_HEADER, prompt)
        self.assertIn(module._TRANSCRIPT_FOOTER, prompt)

    def test_multi_turn_conversation(self):
        messages = [
            {"role": "user", "content": "Q1"},
            {"role": "assistant", "content": "A1"},
            {"role": "user", "content": "Q2"},
        ]
        prompt = module.render_prompt(messages)
        self.assertIn("Q1", prompt)
        self.assertIn("A1", prompt)
        self.assertIn("Q2", prompt)
        self.assertIn(module._TRANSCRIPT_HEADER, prompt)
        self.assertIn(module._TRANSCRIPT_FOOTER, prompt)

    def test_no_user_message_raises(self):
        messages = [{"role": "assistant", "content": "Hello"}]
        with self.assertRaises(module.GrokCliAgentError) as ctx:
            module.render_prompt(messages)
        self.assertIn("no user message", str(ctx.exception))

    def test_empty_messages_raises(self):
        with self.assertRaises(module.GrokCliAgentError):
            module.render_prompt([])

    def test_non_list_messages_raises(self):
        with self.assertRaises(module.GrokCliAgentError):
            module.render_prompt("not a list")

    def test_non_dict_message_raises(self):
        with self.assertRaises(module.GrokCliAgentError):
            module.render_prompt(["not a dict"])

    def test_unsupported_role_raises(self):
        with self.assertRaises(module.GrokCliAgentError) as ctx:
            module.render_prompt([{"role": "system", "content": "test"}])
        self.assertIn("unsupported role", str(ctx.exception))


class TestCoerceMessages(unittest.TestCase):
    """Test message normalization."""

    def test_normalizes_content_to_string(self):
        messages = [{"role": "user", "content": 123}]
        result = module._coerce_messages(messages)
        self.assertEqual(result[0]["content"], "123")

    def test_none_content_becomes_empty_string(self):
        messages = [{"role": "user", "content": None}]
        result = module._coerce_messages(messages)
        self.assertEqual(result[0]["content"], "")

    def test_tuple_input_accepted(self):
        messages = ({"role": "user", "content": "test"},)
        result = module._coerce_messages(messages)
        self.assertEqual(len(result), 1)


class TestAssertSafe(unittest.TestCase):
    """Test safety assertion."""

    def test_passes_valid_argv(self):
        argv = ["grok", "--no-auto-update", "--single", "test"]
        result = module._assert_safe(argv)
        self.assertEqual(result, argv)

    def test_raises_on_forbidden_flag(self):
        argv = ["grok", "--always-approve", "--single", "test"]
        with self.assertRaises(module.GrokCliAgentError) as ctx:
            module._assert_safe(argv)
        self.assertIn("forbidden flag", str(ctx.exception))

    def test_raises_on_forbidden_prefix(self):
        argv = ["grok", "--always-approve-extra", "--single", "test"]
        with self.assertRaises(module.GrokCliAgentError) as ctx:
            module._assert_safe(argv)
        self.assertIn("prefix", str(ctx.exception))


class TestResolveBinaryExpansion(unittest.TestCase):
    """Test that _resolve_binary honours resolve_binary's documented contract.

    cli_session.resolve_binary documents ``extra_dirs`` as "already
    user-expanded absolute directories", but _EXTRA_BIN_DIRS holds
    "~/.grok/bin" and "~/.local/bin", so this adapter must expand them
    itself. Every other CLI adapter already does; grok did not, so those two
    entries could never match and a grok installed under the home directory
    was reported as absent.
    """

    def test_tilde_extra_dirs_are_expanded(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            bin_dir = home / ".grok" / "bin"
            bin_dir.mkdir(parents=True)
            binary = bin_dir / "grok"
            binary.write_text("#!/bin/sh\nexit 0\n")
            binary.chmod(0o755)
            # Path.expanduser() reads $HOME, not Path.home(). PATH points at a
            # directory that does not exist so shutil.which cannot resolve a
            # real grok ahead of the one we planted.
            with patch.dict("os.environ", {"HOME": str(home),
                                           "PATH": str(home / "empty")}):
                resolved = module._resolve_binary()
            self.assertEqual(resolved, str(binary))

    def test_extra_dirs_passed_to_resolve_binary_are_absolute(self):
        captured = {}

        def fake_resolve(names, extra_dirs=()):
            captured["names"] = tuple(names)
            captured["extra_dirs"] = tuple(extra_dirs)
            return None

        with patch.object(module, "resolve_binary", fake_resolve):
            module._resolve_binary()

        self.assertEqual(captured["names"], module.BINARY_NAMES)
        self.assertTrue(captured["extra_dirs"], "extra_dirs must not be empty")
        for directory in captured["extra_dirs"]:
            self.assertFalse(directory.startswith("~"),
                             f"{directory} was not user-expanded")
            self.assertTrue(Path(directory).is_absolute(),
                            f"{directory} is not an absolute path")


class TestDiscover(unittest.TestCase):
    """Test CLI discovery."""

    @patch.object(module, '_resolve_binary')
    @patch.object(module, '_invoke')
    def test_discover_installed(self, mock_invoke, mock_resolve):
        mock_resolve.return_value = "/fake/path/grok"
        mock_invoke.return_value = (0, "grok 1.0.34", "")
        result = module.discover()
        self.assertTrue(result["installed"])
        self.assertEqual(result["binary"], "/fake/path/grok")
        self.assertEqual(result["version"], "grok 1.0.34")

    @patch.object(module, '_resolve_binary')
    def test_discover_not_installed(self, mock_resolve):
        mock_resolve.return_value = None
        result = module.discover()
        self.assertFalse(result["installed"])
        self.assertIsNone(result["binary"])
        self.assertIsNone(result["version"])


class TestAuthState(unittest.TestCase):
    """Test authentication state checking."""

    @patch.object(module, '_resolve_binary')
    @patch.object(module, '_invoke')
    def test_auth_state_authenticated(self, mock_invoke, mock_resolve):
        mock_resolve.return_value = "/fake/path/grok"
        mock_invoke.return_value = (0, "You are logged in with grok.com.", "")
        result = module.auth_state()
        self.assertEqual(result["state"], "authenticated")

    @patch.object(module, '_resolve_binary')
    @patch.object(module, '_invoke')
    def test_auth_state_not_logged_in(self, mock_invoke, mock_resolve):
        mock_resolve.return_value = "/fake/path/grok"
        mock_invoke.return_value = (1, "", "Error: not logged in")
        result = module.auth_state()
        self.assertEqual(result["state"], "missing")

    @patch.object(module, '_resolve_binary')
    def test_auth_state_not_installed(self, mock_resolve):
        mock_resolve.return_value = None
        result = module.auth_state()
        self.assertEqual(result["state"], "missing")


class TestCatalogue(unittest.TestCase):
    """Test model catalogue."""

    def test_catalogue_returns_empty(self):
        models, warnings = module.catalogue()
        self.assertEqual(models, [])
        self.assertEqual(len(warnings), 1)
        self.assertIn("no non-interactive model list", warnings[0])


class TestIterEvents(unittest.TestCase):
    """Test event iteration."""

    def test_skips_ansi_lines(self):
        mock_session = MagicMock()
        mock_session.events.return_value = ["\x1b[33mWARN\x1b[0m test\n", '{"type": "test"}\n']
        events = list(module._iter_events(mock_session))
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0][0], "parsed")

    def test_skips_non_json_lines(self):
        mock_session = MagicMock()
        mock_session.events.return_value = ["Some log line\n", '{"type": "test"}\n']
        events = list(module._iter_events(mock_session))
        self.assertEqual(len(events), 1)

    def test_parses_json_lines(self):
        mock_session = MagicMock()
        mock_session.events.return_value = ['{"type": "assistant", "message": {}}\n']
        events = list(module._iter_events(mock_session))
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0][0], "parsed")
        self.assertIn("type", events[0][1])

    def test_raw_for_invalid_json(self):
        mock_session = MagicMock()
        mock_session.events.return_value = ["not json at all\n"]
        events = list(module._iter_events(mock_session))
        # Non-JSON lines that don't start with { are skipped
        self.assertEqual(len(events), 0)


# grok's documented inline backend search: a server_tool_use, then the
# web_search_tool_result whose hits are {type, url, title}.
_SEARCH_HITS = [{"type": "web_search_result", "url": "https://blog.rust-lang.org/", "title": "Rust Blog"},
                {"type": "web_search_result", "url": "file:///etc/hosts", "title": "local"}]


def _search_snapshot():
    return {"type": "assistant", "message": {"id": "m1", "content": [
        {"type": "server_tool_use", "id": "srvtoolu_1", "name": "web_search",
         "input": {"query": "rust stable release"}},
        {"type": "web_search_tool_result", "tool_use_id": "srvtoolu_1", "content": _SEARCH_HITS},
        {"type": "text", "text": "Rust 1.99 is out."}]}}


def _expected_search():
    return [{"type": "web_search", "status": "in_progress", "id": "srvtoolu_1"},
            {"type": "web_search", "status": "completed", "id": "srvtoolu_1",
             "action": {"type": "search", "query": "rust stable release"},
             "results": [{"url": "https://blog.rust-lang.org/", "title": "Rust Blog"}]},
            {"type": "text_delta", "text": "Rust 1.99 is out."}]


class TestTranslate(unittest.TestCase):
    """Test event translation from grok format to route events."""

    def test_assistant_with_thinking(self):
        state = module._TurnState()
        payload = {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "thinking", "thinking": "Let me think..."}
                ]
            }
        }
        events = list(module._translate(payload, state))
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["type"], "thinking_delta")
        self.assertEqual(events[0]["text"], "Let me think...")

    def test_assistant_with_text(self):
        state = module._TurnState()
        payload = {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "text", "text": "Hello!"}
                ]
            }
        }
        events = list(module._translate(payload, state))
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["type"], "text_delta")
        self.assertEqual(events[0]["text"], "Hello!")
        self.assertTrue(state.emitted_text)

    def test_end_event(self):
        state = module._TurnState()
        payload = {"type": "end", "stopReason": "end_turn"}
        list(module._translate(payload, state))
        self.assertEqual(state.stop_reason, "end_turn")

    def test_error_event(self):
        state = module._TurnState()
        payload = {"type": "error", "message": "Something went wrong"}
        list(module._translate(payload, state))
        self.assertIn("grok CLI error", state.failure)

    def test_result_event(self):
        state = module._TurnState()
        payload = {"type": "result", "result": "Final answer"}
        list(module._translate(payload, state))
        self.assertEqual(state.result_text, "Final answer")

    def test_ignores_available_commands(self):
        state = module._TurnState()
        payload = {"type": "available_commands", "tools": ["tool1", "tool2"]}
        events = list(module._translate(payload, state))
        self.assertEqual(events, [])

    def test_ignores_usage(self):
        state = module._TurnState()
        payload = {"type": "usage", "usage": {"input_tokens": 100}}
        events = list(module._translate(payload, state))
        self.assertEqual(events, [])

    def test_cancelled_end_and_failed_result_are_errors(self):
        for payload in ({"type": "end", "stopReason": "cancelled"},
                        {"type": "result", "subtype": "error_max_turns", "is_error": True}):
            state = module._TurnState()
            list(module._translate(payload, state))
            self.assertIsNotNone(state.failure)

    def test_native_tools_are_reported_instead_of_discarded(self):
        for payload in (
                {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "bash"}]}},
                {"type": "stream_event", "event": {"type": "content_block_start",
                 "content_block": {"type": "tool_use", "name": "bash"}}}):
            state = module._TurnState()
            self.assertEqual(list(module._translate(payload, state)), [])
            self.assertIn("native CLI tool", state.failure)

    def test_partial_messages_are_not_duplicated_by_whole_message(self):
        state = module._TurnState()
        stream = [{"type": "stream_event", "event": {"type": "message_start"}},
                  {"type": "stream_event", "event": {"type": "content_block_delta",
                   "delta": {"type": "text_delta", "text": "Hello"}}},
                  {"type": "assistant", "message": {"content": [{"type": "text", "text": "Hello"}]}}]
        events = [event for payload in stream for event in module._translate(payload, state)]
        self.assertEqual(events, [{"type": "text_delta", "text": "Hello"}])

    def test_repeated_snapshot_is_deduplicated_by_message_id(self):
        state = module._TurnState()
        snapshot = {"type": "assistant", "message": {"id": "m1", "content": [
            {"type": "thinking", "thinking": "Published thinking."},
            {"type": "text", "text": "I'll list the files."}]}}
        events = [event for payload in [snapshot, snapshot]
                  for event in module._translate(payload, state)]
        self.assertEqual(events, [{"type": "thinking_delta", "text": "Published thinking."},
                                  {"type": "text_delta", "text": "I'll list the files."}])

    def test_snapshot_recovers_suffix_and_unstreamed_blocks(self):
        state = module._TurnState()
        stream = [
            {"type": "stream_event", "event": {"type": "message_start", "message": {"id": "m1"}}},
            {"type": "stream_event", "event": {"type": "content_block_delta", "index": 0,
             "delta": {"type": "thinking_delta", "thinking": "Think"}}},
            {"type": "stream_event", "event": {"type": "content_block_delta", "index": 1,
             "delta": {"type": "text_delta", "text": "First"}}},
            {"type": "assistant", "message": {"id": "m1", "content": [
                {"type": "thinking", "thinking": "Thinking."},
                {"type": "text", "text": "First block."},
                {"type": "text", "text": "Second block."},
                {"type": "thinking", "thinking": "More published thinking."}]}},
        ]
        events = [event for payload in stream for event in module._translate(payload, state)]
        self.assertEqual(events, [
            {"type": "thinking_delta", "text": "Think"},
            {"type": "text_delta", "text": "First"},
            {"type": "thinking_delta", "text": "ing."},
            {"type": "text_delta", "text": " block."},
            {"type": "text_delta", "text": "Second block."},
            {"type": "thinking_delta", "text": "More published thinking."},
        ])

    def test_identical_text_in_distinct_messages_is_preserved(self):
        for identifiers in [("m1", "m2"), (None, None)]:
            with self.subTest(identifiers=identifiers):
                state = module._TurnState()
                events = []
                for identifier in identifiers:
                    events.extend(module._translate({"type": "assistant", "message": {
                        "id": identifier, "content": [{"type": "text", "text": "Again."}]}}, state))
                self.assertEqual(events, [{"type": "text_delta", "text": "Again."}] * 2)

    def test_late_snapshot_does_not_reassign_active_stream(self):
        state = module._TurnState()
        stream = [
            {"type": "stream_event", "event": {"type": "message_start", "message": {"id": "m1"}}},
            {"type": "stream_event", "event": {"type": "content_block_delta", "index": 0,
             "delta": {"type": "text_delta", "text": "First."}}},
            {"type": "stream_event", "event": {"type": "message_start", "message": {"id": "m2"}}},
            {"type": "assistant", "message": {"id": "m1", "content": [
                {"type": "text", "text": "First."}]}},
            {"type": "stream_event", "event": {"type": "content_block_delta", "index": 0,
             "delta": {"type": "text_delta", "text": "Second."}}},
            {"type": "assistant", "message": {"id": "m2", "content": [
                {"type": "text", "text": "Second."}]}},
        ]
        events = [event for payload in stream for event in module._translate(payload, state)]
        self.assertEqual(events, [{"type": "text_delta", "text": "First."},
                                  {"type": "text_delta", "text": "Second."}])

    def test_initial_block_text_and_stale_snapshots_do_not_repeat(self):
        state = module._TurnState()
        stream = [
            {"type": "stream_event", "event": {"type": "message_start", "message": {"id": "m1"}}},
            {"type": "stream_event", "event": {"type": "content_block_start", "index": 0,
             "content_block": {"type": "text", "text": "Hello"}}},
            {"type": "stream_event", "event": {"type": "content_block_delta", "index": 0,
             "delta": {"type": "text_delta", "text": " world"}}},
            {"type": "assistant", "message": {"id": "m1", "content": [{"type": "text", "text": "Hello"}]}},
            {"type": "assistant", "message": {"id": "m1", "content": [{"type": "text", "text": "Hello world"}]}},
        ]
        events = [event for payload in stream for event in module._translate(payload, state)]
        self.assertEqual(events, [{"type": "text_delta", "text": "Hello"},
                                  {"type": "text_delta", "text": " world"}])

    def test_conflicting_snapshot_is_reported_instead_of_appended(self):
        state = module._TurnState()
        list(module._translate({"type": "stream_event", "event": {
            "type": "message_start", "message": {"id": "m1"}}}, state))
        list(module._translate({"type": "stream_event", "event": {
            "type": "content_block_delta", "delta": {"type": "text_delta", "text": "First."}}}, state))
        events = list(module._translate({"type": "assistant", "message": {"id": "m1", "content": [
            {"type": "text", "text": "Unrelated replacement."}]}}, state))
        self.assertEqual(events, [])
        self.assertIn("conflicted", state.failure)

    def test_thinking_never_becomes_answer_fallback(self):
        state = module._TurnState()
        list(module._translate({"type": "assistant", "message": {"content": [
            {"type": "thinking", "thinking": "private reasoning"}]}}, state))
        self.assertEqual(state.fallback_text(), "")

    def test_init_must_confirm_native_tools_are_disabled(self):
        state = module._TurnState()
        list(module._translate({"type": "system", "subtype": "init", "tools": ["read_file"]}, state))
        self.assertIn("did not disable", state.failure)

    def test_init_names_the_tools_that_stayed_registered(self):
        state = module._TurnState()
        list(module._translate({"type": "system", "subtype": "init",
                                "tools": ["write_file", "read_file"]}, state))
        self.assertEqual(state.native_tools_seen, ["read_file", "write_file"])
        self.assertIn("(read_file, write_file)", state.failure)
        self.assertNotIn("respawn", state.failure)
        state = module._TurnState()
        state.retried = True
        list(module._translate({"type": "system", "subtype": "init", "tools": ["read_file"]}, state))
        self.assertIn("even after a respawn", state.failure)
        # Order never matters; a search turn keeps web_search and nothing else.
        state = module._TurnState()
        state.search = True
        list(module._translate({"type": "system", "subtype": "init", "tools": ["web_search"]}, state))
        self.assertIsNone(state.failure)
        self.assertTrue(state.native_tools_disabled)
        state = module._TurnState()
        state.search = True
        list(module._translate({"type": "system", "subtype": "init",
                                "tools": ["web_search", "sports_search"]}, state))
        self.assertEqual(state.native_tools_seen, ["sports_search"])
        # No usable registry is a failure, not a retry.
        state = module._TurnState()
        list(module._translate({"type": "system", "subtype": "init", "tools": "read_file"}, state))
        self.assertIn("did not report", state.failure)
        self.assertIsNone(state.native_tools_seen)

    def test_isolated_native_request_is_forwarded_to_offered_host_tool(self):
        state = module._TurnState()
        state.host_tools = [{"name": "read_file"}]
        payloads = [
            {"type": "system", "subtype": "init", "tools": []},
            {"type": "stream_event", "event": {"type": "content_block_start", "index": 0,
             "content_block": {"type": "tool_use", "id": "call_1", "name": "read_file", "input": {}}}},
            {"type": "stream_event", "event": {"type": "content_block_delta", "index": 0,
             "delta": {"type": "input_json_delta", "partial_json": '{"path":"file"}'}}},
            {"type": "stream_event", "event": {"type": "content_block_stop", "index": 0}},
        ]
        events = [event for payload in payloads for event in module._translate(payload, state)]
        self.assertEqual(events, [{"type": "tool_call", "id": "call_1", "name": "read_file",
                                   "input": {"path": "file"}}])
        # A parallel call may still follow in the same message.
        self.assertFalse(state.host_handoff)
        list(module._translate({"type": "stream_event", "event": {
            "type": "message_delta", "delta": {"stop_reason": "tool_use"}}}, state))
        self.assertTrue(state.host_handoff)

    def test_isolated_runtime_cannot_forward_an_unoffered_tool(self):
        state = module._TurnState()
        state.native_tools_disabled = True
        with self.assertRaises(module.ToolCallError):
            list(module._translate({"type": "assistant", "message": {"content": [
                {"type": "tool_use", "id": "call_1", "name": "unoffered", "input": {}}]}}, state))

    def test_backend_search_is_visible_progress_and_never_a_host_call(self):
        state = module._TurnState()
        state.native_tools_disabled = True
        payload = {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "backend_1", "name": "X search:",
             "input": {"variant": "XSearch", "backend": True}}]}}
        events = list(module._translate(payload, state))
        self.assertEqual(events[0]["type"], "thinking_delta")
        self.assertFalse(state.host_handoff)
        self.assertEqual(list(module._translate(payload, state)), [])

    def test_streamed_backend_search_is_relayed_once(self):
        state = module._TurnState()
        state.search = True

        def stream(event):
            return {"type": "stream_event", "event": event}
        payloads = [
            {"type": "system", "subtype": "init", "tools": ["web_search"]},
            stream({"type": "message_start", "message": {"id": "m1"}}),
            stream({"type": "content_block_start", "index": 0, "content_block": {
                "type": "server_tool_use", "id": "srvtoolu_1", "name": "web_search", "input": {}}}),
            stream({"type": "content_block_delta", "index": 0, "delta": {
                "type": "input_json_delta", "partial_json": '{"query":"rust stable release"}'}}),
            stream({"type": "content_block_stop", "index": 0}),
            stream({"type": "content_block_start", "index": 1, "content_block": {
                "type": "web_search_tool_result", "tool_use_id": "srvtoolu_1", "content": _SEARCH_HITS}}),
            stream({"type": "content_block_stop", "index": 1}),
            stream({"type": "content_block_start", "index": 2, "content_block": {"type": "text", "text": ""}}),
            stream({"type": "content_block_delta", "index": 2, "delta": {
                "type": "text_delta", "text": "Rust 1.99 is out."}}),
            stream({"type": "content_block_stop", "index": 2}),
            stream({"type": "message_delta", "delta": {"stop_reason": "end_turn"}}),
            _search_snapshot(),
        ]
        events = [event for payload in payloads for event in module._translate(payload, state)]
        self.assertIsNone(state.failure)
        self.assertEqual(events, _expected_search())
        self.assertFalse(state.host_handoff)

    def test_snapshot_only_search_and_failed_search(self):
        state = module._TurnState()
        state.search = True
        state.native_tools_disabled = True
        self.assertEqual(list(module._translate(_search_snapshot(), state)), _expected_search())
        failed = {"type": "assistant", "message": {"id": "m2", "content": [
            {"type": "server_tool_use", "id": "srvtoolu_2", "name": "web_search", "input": {"query": "x"}},
            {"type": "web_search_tool_result", "tool_use_id": "srvtoolu_2",
             "content": {"type": "web_search_tool_result_error", "error_code": "unavailable"}}]}}
        events = list(module._translate(failed, state))
        self.assertEqual([event["status"] for event in events], ["in_progress", "completed"])
        self.assertEqual(events[1]["results"], [])

    def test_search_blocks_are_refused_when_search_was_not_requested(self):
        state = module._TurnState()
        list(module._translate({"type": "system", "subtype": "init", "tools": ["web_search"]}, state))
        self.assertIn("did not disable", state.failure)
        state = module._TurnState()
        state.native_tools_disabled = True
        with self.assertRaises(module.ToolCallError):
            list(module._translate(_search_snapshot(), state))


class TestDescribe(unittest.TestCase):
    """Test error description generation."""

    def test_timeout_error(self):
        import subprocess
        state = None
        result = module._describe(subprocess.TimeoutExpired('test', 10), state, timeout=10)
        self.assertIn("did not finish", result)
        self.assertIn("10s", result)

    def test_cli_session_error(self):
        state = None
        result = module._describe(module.CliSessionError("test error"), state)
        self.assertIn("grok CLI session error", result)

    def test_generic_error(self):
        state = None
        result = module._describe(ValueError("bad value"), state)
        self.assertIn("grok CLI turn failed", result)


class TestTurnState(unittest.TestCase):
    """Test turn state accumulation."""

    def test_fallback_text_from_assistant(self):
        state = module._TurnState()
        state.assistant_text = ["part1", "part2"]
        self.assertEqual(state.fallback_text(), "part1part2")

    def test_fallback_text_from_result(self):
        state = module._TurnState()
        state.result_text = "final"
        self.assertEqual(state.fallback_text(), "final")

    def test_diagnostics_empty(self):
        state = module._TurnState()
        self.assertEqual(state.diagnostics(), "")

    def test_diagnostics_with_lines(self):
        state = module._TurnState()
        state.raw_lines = ["line1", "line2", "line3", "line4", "line5", "line6"]
        result = state.diagnostics()
        self.assertIn("line2", result)
        self.assertIn("line6", result)

    def test_stderr_tail_empty(self):
        state = module._TurnState()
        self.assertEqual(state.stderr_tail(), "")

    def test_stderr_tail_with_handle(self):
        state = module._TurnState()
        with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as f:
            f.write("Error: test error\nMore details\n")
            f.seek(0)
            state.stderr_handle = f
            result = state.stderr_tail()
            self.assertIn("Error: test error", result)
            self.assertIn("stderr:", result)


class TestRunTurnValidation(unittest.TestCase):
    """Test run_turn input validation."""

    def test_request_not_dict_raises(self):
        events = list(module.run_turn("not a dict", timeout=1))
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["type"], "error")
        self.assertIn("must be an object", events[0]["message"])

    def test_max_tokens_invalid_raises(self):
        request = {
            "model": "grok-4.6",
            "messages": [{"role": "user", "content": "test"}],
            "max_tokens": -1
        }
        events = list(module.run_turn(request, timeout=1))
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["type"], "error")
        self.assertIn("positive", events[0]["message"])

    def test_max_tokens_non_int_raises(self):
        request = {
            "model": "grok-4.6",
            "messages": [{"role": "user", "content": "test"}],
            "max_tokens": "invalid"
        }
        events = list(module.run_turn(request, timeout=1))
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["type"], "error")
        self.assertIn("integer", events[0]["message"])

    def test_system_non_string_raises(self):
        request = {
            "model": "grok-4.6",
            "messages": [{"role": "user", "content": "test"}],
            "system": 123
        }
        events = list(module.run_turn(request, timeout=1))
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["type"], "error")
        self.assertIn("string", events[0]["message"])


class _FakeSession:
    """Minimal stand-in for cli_session.StdioSession."""

    def __init__(self, argv, **kwargs):
        self.argv = list(argv)
        self.kwargs = kwargs
        self.returncode = 0
        self.process = None
        self.lines = []
        # The workspace is released when the turn ends, so read the prompt
        # file at spawn time, as the real CLI would.
        self.prompt_file = None
        if "--prompt-file" in self.argv:
            path = Path(self.argv[self.argv.index("--prompt-file") + 1])
            self.prompt_file = path.read_text(encoding="utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def events(self, timeout=None):
        yield from self.lines


class TestRunTurnSearch(unittest.TestCase):
    """Hosted search reaches grok only when it cannot widen the request."""

    def _run(self, web_search, lines):
        sessions = []

        def factory(argv, **kwargs):
            session = _FakeSession(argv, **kwargs)
            session.lines = lines
            sessions.append(session)
            return session
        with patch.object(module, "StdioSession", side_effect=factory), \
                patch.object(module, "_resolve_binary", return_value="/fake/grok"):
            events = list(module.run_turn({"model": "grok-4.7", "web_search": web_search,
                                           "messages": [{"role": "user", "content": "rust?"}]}))
        return events, sessions[0]

    def test_requested_search_runs_web_search_alone_and_streams_searches(self):
        lines = [{"type": "system", "subtype": "init", "tools": ["web_search"]},
                 _search_snapshot(),
                 {"type": "result", "subtype": "success", "result": "Rust 1.99 is out."}]
        events, session = self._run({"context_size": None, "allowed_domains": [], "live": True}, lines)
        self.assertEqual(session.argv[-2:], ["--tools", "web_search"])
        self.assertEqual([event["type"] for event in events],
                         ["web_search", "web_search", "text_delta", "message_stop"])
        self.assertEqual(events[1]["results"], [{"url": "https://blog.rust-lang.org/", "title": "Rust Blog"}])

    def test_search_never_widens_a_cached_or_domain_limited_request(self):
        lines = [{"type": "system", "subtype": "init", "tools": []},
                 {"type": "assistant", "message": {"id": "m1", "content": [{"type": "text", "text": "No search."}]}},
                 {"type": "result", "subtype": "success", "result": "No search."}]
        for web_search in (None, {"live": False, "allowed_domains": []},
                           {"live": True, "allowed_domains": ["blog.rust-lang.org"]}):
            with self.subTest(web_search=web_search):
                events, session = self._run(web_search, lines)
                self.assertEqual(session.argv[-2:], ["--tools", ""])
                self.assertIn("--disable-web-search", session.argv)
                self.assertEqual([event["type"] for event in events], ["text_delta", "message_stop"])


_INIT_EMPTY = {"type": "system", "subtype": "init", "tools": []}
_OK_TURN = [_INIT_EMPTY,
            {"type": "assistant", "message": {"id": "m1", "content": [{"type": "text", "text": "Hello."}]}},
            {"type": "result", "subtype": "success", "result": "Hello."}]


class TestRunTurnRegistryRespawn(unittest.TestCase):
    """A registry that is not empty at init is respawned once, names removed."""

    def _run(self, batches, request=None, learned=None):
        sessions = []
        queue = [list(batch) for batch in batches]

        def factory(argv, **kwargs):
            session = _FakeSession(argv, **kwargs)
            session.lines = queue.pop(0)
            sessions.append(session)
            return session
        request = request or {"model": "grok-4.7", "messages": [{"role": "user", "content": "hi"}]}
        with patch.object(module, "StdioSession", side_effect=factory), \
                patch.object(module, "_resolve_binary", return_value="/fake/grok"), \
                patch.object(module, "_LEARNED_REMOVALS", set(learned or ())):
            events = list(module.run_turn(request))
            remembered = set(module._LEARNED_REMOVALS)
        return events, sessions, remembered

    @staticmethod
    def _removed(session):
        return set(session.argv[session.argv.index("--disallowed-tools") + 1].split(","))

    def test_respawn_removes_the_named_tools_and_remembers_them(self):
        events, sessions, learned = self._run([
            [{"type": "system", "subtype": "init", "tools": ["sports_search", "new_tool"]}], _OK_TURN])
        self.assertEqual([event["type"] for event in events], ["text_delta", "message_stop"])
        self.assertEqual(events[0]["text"], "Hello.")
        self.assertEqual(len(sessions), 2)
        self.assertNotIn("new_tool", self._removed(sessions[0]))
        self.assertLessEqual({"sports_search", "new_tool"}, self._removed(sessions[1]))
        self.assertEqual(sessions[1].argv[-2:], ["--tools", ""])
        self.assertIn("new_tool", learned)

    def test_learned_removal_applies_to_the_next_turn_first_time(self):
        events, sessions, learned = self._run([_OK_TURN], learned={"new_tool"})
        self.assertEqual(len(sessions), 1)
        self.assertIn("new_tool", self._removed(sessions[0]))
        self.assertEqual([event["type"] for event in events], ["text_delta", "message_stop"])

    def test_second_failure_names_the_tools_and_stops(self):
        events, sessions, learned = self._run([
            [{"type": "system", "subtype": "init", "tools": ["mcp_tool"]}],
            [{"type": "system", "subtype": "init", "tools": ["mcp_tool"]}]])
        self.assertEqual(len(sessions), 2)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["type"], "error")
        self.assertIn("(mcp_tool)", events[0]["message"])
        self.assertIn("even after a respawn", events[0]["message"])
        self.assertEqual(learned, set())

    def test_unsafe_names_never_reach_argv(self):
        events, sessions, learned = self._run([
            [{"type": "system", "subtype": "init", "tools": ["--always-approve", "ok_name"]}], _OK_TURN])
        removed = self._removed(sessions[1])
        self.assertIn("ok_name", removed)
        self.assertNotIn("--always-approve", removed)
        self.assertFalse(any(arg.startswith("--always-approve") for arg in sessions[1].argv))
        self.assertEqual(learned, {"ok_name"} | (learned & set(module._DISALLOWED_TOOLS)))
        self.assertNotIn("--always-approve", learned)

    def test_child_env_disables_compat_mcp_scans_only(self):
        with patch.dict("os.environ", {"XAI_API_KEY": "secret", "PATH": "/usr/bin"}, clear=False):
            events, sessions, _ = self._run([_OK_TURN])
        env = sessions[0].kwargs["env"]
        self.assertEqual(env["GROK_CURSOR_MCPS_ENABLED"], "0")
        self.assertEqual(env["GROK_CLAUDE_MCPS_ENABLED"], "0")
        self.assertNotIn("XAI_API_KEY", env)
        self.assertIn("PATH", env)

    def test_init_without_a_registry_is_not_retried(self):
        events, sessions, _ = self._run([[{"type": "system", "subtype": "init"}]])
        self.assertEqual(len(sessions), 1)
        self.assertEqual(events, [{"type": "error", "message":
                                   "grok did not report its native tool registry; "
                                   "this runtime cannot safely forward host calls."}])

    def test_registry_failure_after_output_is_not_retried(self):
        late = [{"type": "assistant", "message": {"id": "m1", "content": [{"type": "text", "text": "Hi"}]}},
                {"type": "system", "subtype": "init", "tools": ["read_file"]}]
        events, sessions, _ = self._run([late])
        self.assertEqual(len(sessions), 1)
        self.assertEqual([event["type"] for event in events], ["text_delta", "error"])


class TestRunTurnPromptTransport(unittest.TestCase):
    """Oversized prompts travel in the workspace prompt file, never on argv."""

    def _run(self, request):
        sessions = []

        def factory(argv, **kwargs):
            session = _FakeSession(argv, **kwargs)
            session.lines = [_INIT_EMPTY, {"type": "result", "subtype": "success", "result": "OK"}]
            sessions.append(session)
            return session
        with patch.object(module, "StdioSession", side_effect=factory), \
                patch.object(module, "_resolve_binary", return_value="/fake/grok"):
            events = list(module.run_turn(request))
        return events, sessions[0]

    def test_small_prompt_stays_on_argv(self):
        events, session = self._run({"model": "grok-4.7", "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(session.argv[session.argv.index("--single") + 1], "hi")
        self.assertNotIn("--prompt-file", session.argv)
        self.assertIsNone(session.prompt_file)
        self.assertEqual([event["type"] for event in events], ["text_delta", "message_stop"])

    def test_oversized_prompt_moves_to_the_prompt_file(self):
        big = "x" * (module._MAX_PROMPT_ARGV_BYTES + 1)
        events, session = self._run({"model": "grok-4.7", "messages": [{"role": "user", "content": big}]})
        self.assertEqual([event["type"] for event in events], ["text_delta", "message_stop"])
        self.assertNotIn("--single", session.argv)
        self.assertIn("--prompt-file", session.argv)
        self.assertEqual(json.loads(session.prompt_file), [{"type": "text", "text": big}])
        self.assertTrue(session.argv[session.argv.index("--prompt-file") + 1].endswith("/prompt.json"))
        self.assertEqual(session.argv[-2:], ["--tools", ""])
        self.assertEqual(session.kwargs["cwd"], str(Path(session.argv[session.argv.index("--prompt-file") + 1]).parent))

    def test_system_override_counts_toward_the_argv_budget(self):
        half = "y" * (module._MAX_PROMPT_ARGV_BYTES // 2 + 1)
        events, session = self._run({"model": "grok-4.7", "system": half,
                                     "messages": [{"role": "user", "content": half}]})
        self.assertIn("--prompt-file", session.argv)
        self.assertEqual(session.argv[session.argv.index("--system-prompt-override") + 1], half)
        self.assertEqual(json.loads(session.prompt_file), [{"type": "text", "text": half}])
        self.assertEqual([event["type"] for event in events], ["text_delta", "message_stop"])


_HOST_TOOLS = [
    {"name": "exec_command", "description": "Run a command on the host.",
     "input_schema": {"type": "object", "properties": {"cmd": {"type": "string"},
                                                       "workdir": {"type": "string"}}, "required": ["cmd"]}},
    {"name": "get_goal", "description": "Read the thread goal.", "input_schema": {"type": "object", "properties": {}}},
]


def _use_tool(index, identifier, arguments):
    """One use_tool call as grok 1.0.41 streams it: a single input_json_delta."""
    return [
        {"type": "stream_event", "event": {"type": "content_block_start", "index": index, "content_block": {
            "type": "tool_use", "id": identifier, "name": "use_tool", "input": {}}}},
        {"type": "stream_event", "event": {"type": "content_block_delta", "index": index, "delta": {
            "type": "input_json_delta", "partial_json": json.dumps(arguments)}}},
        {"type": "stream_event", "event": {"type": "content_block_stop", "index": index}},
    ]


def _refused_message(identifiers):
    """What grok sends once the message ends: stop reason, snapshot, then its refusal."""
    return [
        {"type": "stream_event", "event": {"type": "message_delta", "delta": {"stop_reason": "tool_use"}}},
        {"type": "stream_event", "event": {"type": "message_stop"}},
        {"type": "assistant", "message": {"id": "msg_0", "content": [
            {"type": "tool_use", "id": identifier, "name": "use_tool",
             "input": {"tool_name": "get_goal", "tool_input": {}}} for identifier in identifiers]}},
        {"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": identifier, "content": [{"type": "content", "content": {
                "type": "text", "text": "Tool `use_tool` was not executed: Denied by permission policy: "
                                        "deny rule on mcp"}}]} for identifier in identifiers]}},
        {"type": "assistant", "message": {"id": "msg_1", "content": [
            {"type": "text", "text": "The permission policy blocked it."}]}},
        {"type": "result", "subtype": "success", "result": "The permission policy blocked it."},
    ]


_USE_TOOL_INIT = {"type": "system", "subtype": "init", "tools": ["use_tool"], "mcp_servers": []}


class TestRunTurnHostTools(unittest.TestCase):
    """Host tools ride grok's own use_tool dispatcher as native structured calls."""

    def _run(self, batches, *, tools=_HOST_TOOLS, web_search=None, tool_choice=None):
        sessions = []
        queue = [list(batch) for batch in batches]

        def factory(argv, **kwargs):
            session = _FakeSession(argv, **kwargs)
            session.lines = queue.pop(0)
            session.process = Mock()
            sessions.append(session)
            return session
        request = {"model": "grok-4.7", "messages": [{"role": "user", "content": "which branch?"}],
                   "tools": tools, "web_search": web_search, "tool_choice": tool_choice}
        with patch.object(module, "StdioSession", side_effect=factory), \
                patch.object(module, "_resolve_binary", return_value="/fake/grok"), \
                patch.object(module, "_LEARNED_REMOVALS", set()):
            events = list(module.run_turn(request))
        return events, sessions

    @staticmethod
    def _removed(session):
        return set(session.argv[session.argv.index("--disallowed-tools") + 1].split(","))

    @staticmethod
    def _system(session):
        return session.argv[session.argv.index("--system-prompt-override") + 1]

    def test_host_call_rides_use_tool_and_hands_off_at_the_end_of_the_message(self):
        events, [session] = self._run([[
            _USE_TOOL_INIT,
            {"type": "stream_event", "event": {"type": "message_start", "message": {"id": "msg_0"}}},
            *_use_tool(1, "call-a-0", {"tool_name": "exec_command",
                                       "tool_input": {"cmd": "git branch --show-current", "workdir": "/repo"}}),
            *_refused_message(["call-a-0"])]])
        self.assertEqual(events, [
            {"type": "tool_call", "id": "call-a-0", "name": "exec_command",
             "input": {"cmd": "git branch --show-current", "workdir": "/repo"}},
            {"type": "message_stop", "stop_reason": "tool_use"}])
        session.process.terminate.assert_called_once()
        self.assertNotIn("use_tool", self._removed(session))
        self.assertIn("search_tool", self._removed(session))
        self.assertEqual(session.argv[session.argv.index("MCPTool") - 1], "--deny")
        system = self._system(session)
        self.assertIn("calling your `use_tool` tool", system)
        self.assertIn('"workdir"', system)
        self.assertNotIn("<<<tool_call>>>", system)

    def test_parallel_calls_hand_off_together_and_the_host_key_spelling_resolves(self):
        events, _ = self._run([[
            _USE_TOOL_INIT,
            *_use_tool(0, "call-b-0", {"tool_name": "host__get_goal", "tool_input": {}}),
            *_use_tool(1, "call-b-1", {"tool_name": "exec_command", "tool_input": '{"cmd": "ls"}'}),
            *_refused_message(["call-b-0", "call-b-1"])]])
        self.assertEqual([(event["type"], event.get("name"), event.get("input")) for event in events],
                         [("tool_call", "get_goal", {}), ("tool_call", "exec_command", {"cmd": "ls"}),
                          ("message_stop", None, None)])

    def test_unoffered_names_and_file_arguments_are_protocol_errors(self):
        for arguments in ({"tool_name": "Bash", "tool_input": {"command": "ls"}},
                          {"tool_name": "exec_command", "file": "/tmp/mcp-call.json"},
                          {"tool_name": "exec_command", "tool_input": "not json"},
                          {"tool_input": {"cmd": "ls"}}):
            with self.subTest(arguments=arguments):
                events, _ = self._run([[_USE_TOOL_INIT, *_use_tool(0, "call-c-0", arguments),
                                        *_refused_message(["call-c-0"])]])
                self.assertEqual([event["type"] for event in events], ["error"])
                self.assertEqual(events[0]["code"], "invalid_cli_tool_call")

    def test_search_and_host_tools_share_the_registry(self):
        events, [session] = self._run([[
            {"type": "system", "subtype": "init", "tools": ["web_search", "use_tool"]},
            *_use_tool(0, "call-d-0", {"tool_name": "get_goal", "tool_input": {}}),
            *_refused_message(["call-d-0"])]],
            web_search={"context_size": None, "allowed_domains": [], "live": True})
        self.assertEqual([event["type"] for event in events], ["tool_call", "message_stop"])
        self.assertEqual(session.argv[-2:], ["--tools", "web_search"])

    def test_missing_use_tool_falls_back_to_the_envelope_before_any_output(self):
        events, sessions = self._run([
            [_INIT_EMPTY, {"type": "assistant", "message": {"id": "m0", "content": [
                {"type": "text", "text": "never shown"}]}}],
            [_INIT_EMPTY, {"type": "assistant", "message": {"id": "m1", "content": [
                {"type": "text", "text": "Listing."}]}},
             {"type": "result", "subtype": "success", "result": "Listing."}]],
            tool_choice={"type": "any"})
        self.assertEqual([(event["type"], event.get("text")) for event in events],
                         [("text_delta", "Listing."), ("message_stop", None)])
        first, second = sessions
        self.assertNotIn("use_tool", self._removed(first))
        self.assertIn("use_tool", self._removed(second))
        self.assertIn("<<<tool_call>>>", self._system(second))
        self.assertIn("You MUST call at least one tool", self._system(second))
        self.assertIn("<host_note>", second.argv[second.argv.index("--single") + 1])

    def test_turns_without_host_tools_keep_use_tool_removed(self):
        events, [session] = self._run([list(_OK_TURN)], tools=[])
        self.assertEqual([event["type"] for event in events], ["text_delta", "message_stop"])
        self.assertIn("use_tool", self._removed(session))
        self.assertNotIn("--system-prompt-override", session.argv)


# -- live sessions ------------------------------------------------------------

_EXIT = object()


def _calls(*calls):
    """A script step: grok dispatches these use_tool calls to the host server and waits.

    Each is (the stream's call id, served tool name, arguments). Grok's MCP
    client sends no call id, only a progress token (1.0.41).
    """
    return ("calls", calls)


def _echo(*call_ids):
    """A script step: grok's own user line with the results its calls returned."""
    return ("echo", call_ids)


def _message(identifier, *blocks, stop="end_turn"):
    """A whole grok message with text blocks, as 1.0.41 streams it."""
    lines = [{"type": "stream_event", "event": {"type": "message_start", "message": {"id": identifier}}}]
    for index, text in enumerate(blocks):
        lines.append({"type": "stream_event", "event": {"type": "content_block_delta", "index": index,
                                                        "delta": {"type": "text_delta", "text": text}}})
    lines += [{"type": "stream_event", "event": {"type": "message_delta", "delta": {"stop_reason": stop}}},
              {"type": "stream_event", "event": {"type": "message_stop"}},
              {"type": "assistant", "message": {"id": identifier, "content": [
                  {"type": "text", "text": text} for text in blocks]}}]
    return lines


def _handoff(identifier, *calls):
    """A message of use_tool calls as grok 1.0.41 streams it.

    The blocks come at once; the message's end (message_delta, message_stop
    and the snapshot) only once every call has returned.
    """
    blocks = [{"type": "stream_event", "event": {"type": "message_start", "message": {"id": identifier}}}]
    for index, (call_id, key, arguments) in enumerate(calls):
        blocks += _use_tool(index, call_id, {"tool_name": key, "tool_input": arguments})
    end = [{"type": "stream_event", "event": {"type": "message_delta", "delta": {"stop_reason": "tool_use"}}},
           {"type": "stream_event", "event": {"type": "message_stop"}},
           {"type": "assistant", "message": {"id": identifier, "content": [
               {"type": "tool_use", "id": call_id, "name": "use_tool",
                "input": {"tool_name": key, "tool_input": arguments}} for call_id, key, arguments in calls]}}]
    return blocks, end


class _LiveGrok:
    """A scripted grok CLI whose use_tool calls really wait in the hub's bridge.

    It finds its host server the way grok does, in the workspace's
    ``.grok/config.toml``; ``_calls`` steps forward calls as that server
    would, and the results come back as grok's own user line.
    """

    def __init__(self, argv, script, **kwargs):
        self.argv = list(argv)
        self.env = kwargs.get("env")
        self.cwd = kwargs.get("cwd")
        with open(Path(self.cwd) / ".grok" / "config.toml", "rb") as handle:
            self.config = tomllib.load(handle)["mcp_servers"]["host"]
        with open(self.config["args"][-1], encoding="utf-8") as handle:
            self.served = json.load(handle)
        self.lines = queue.Queue()
        self.returncode = None
        self.process = Mock()
        self.process.terminate.side_effect = self._stop
        self.closed = threading.Event()
        self.resume = threading.Event()
        self.results = {}
        threading.Thread(target=self._play, args=(script,), daemon=True).start()

    def _play(self, script):
        for item in script:
            if item is _EXIT:
                self.returncode = 0
                self.lines.put(_EXIT)
            elif isinstance(item, tuple) and item[0] == "calls":
                self._call(item[1])
            elif isinstance(item, tuple) and item[0] == "pause":
                self.resume.wait(30)
            elif isinstance(item, tuple) and item[0] == "echo":
                if self.returncode is None:
                    self.lines.put({"type": "user", "message": {"role": "user", "content": [
                        {"type": "tool_result", "tool_use_id": call_id, "content": json.dumps(self.results[call_id])}
                        for call_id in item[1]]}})
            else:
                self.lines.put(item)

    def _call(self, calls):
        def forward(number, call_id, name, arguments):
            request = {"jsonrpc": "2.0", "id": number, "method": "tools/call", "params": {
                "_meta": {"progressToken": number}, "name": name, "arguments": arguments}}
            self.results[call_id] = host_tools_mcp.forward(request, self.served["bridge"])

        threads = [threading.Thread(target=forward, args=(number, *call), daemon=True)
                   for number, call in enumerate(calls, 1)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)

    def events(self, timeout=None):
        deadline = time.monotonic() + (timeout or 5)
        while time.monotonic() < deadline:
            try:
                item = self.lines.get(timeout=0.02)
            except queue.Empty:
                continue
            if item is _EXIT:
                self.lines.put(_EXIT)
                return
            yield item

    def _stop(self):
        if self.returncode is None:
            self.returncode = -15
        self.lines.put(_EXIT)

    def close(self):
        self._stop()
        self.closed.set()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


_TASK = [{"role": "user", "content": [{"type": "text", "text": "which branch?"}]}]


def _answered(history, calls, results):
    """``history`` continued the way the host sends it: the handoff message, then its results."""
    return [*history,
            {"role": "assistant", "content": [{"type": "tool_use", "id": call_id, "name": name, "input": arguments}
                                              for call_id, name, arguments in calls]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": call_id, "content": result}
                                         for call_id, result in results]}]


class TestLiveSessions(unittest.TestCase):
    def setUp(self):
        self.pool = SessionPool(module.live_session.dispose, capacity=2, idle_ttl=0, pending_ttl=60, label="Grok")
        self.addCleanup(self.pool.close)
        self.spawned, self.scripts = [], []

        def factory(argv, **kwargs):
            session = _LiveGrok(argv, self.scripts[len(self.spawned)], **kwargs)
            self.spawned.append(session)
            return session

        for patcher in (patch.object(module, "StdioSession", side_effect=factory),
                        patch.object(module, "_resolve_binary", return_value="/fake/grok"),
                        patch.object(module, "_LEARNED_REMOVALS", set()),
                        patch.object(module, "_LIVE_CONFIRM_SECONDS", 1.0)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def turn(self, history, tools=_HOST_TOOLS):
        request = {"model": "grok-4.7", "messages": [{"role": "user", "content": "which branch?"}],
                   "tools": tools, "history": history}
        return list(module.run_turn(request, pool=self.pool))

    def test_grok_dispatches_to_the_bridge_and_the_result_resumes_it(self):
        blocks, end = _handoff("msg_0", ("call-a-0", "host__exec_command", {"cmd": "git branch"}))
        # The message's end arrives only after its call returns, on the next
        # leg, which must neither fail on it nor repeat what it carries.
        self.scripts = [[_USE_TOOL_INIT, *blocks, _calls(("call-a-0", "exec_command", {"cmd": "git branch"})),
                         *end, _echo("call-a-0"), *_message("msg_1", "On main."),
                         {"type": "result", "subtype": "success", "result": "On main."}, _EXIT]]
        first = self.turn(_TASK)
        self.assertEqual(first, [{"type": "tool_call", "id": "call-a-0", "name": "exec_command",
                                  "input": {"cmd": "git branch"}},
                                 {"type": "message_stop", "stop_reason": "tool_use"}])
        [cli] = self.spawned
        cli.process.terminate.assert_not_called()
        mcp = cli.argv.index("MCPTool(host__*)")
        self.assertEqual(cli.argv[mcp - 1], "--allow")
        self.assertNotIn("MCPTool", cli.argv)
        self.assertEqual(cli.argv[cli.argv.index("--deny") + 1], "Bash")
        self.assertEqual({key: cli.env[key] for key in module._LIVE_ENV_EXTRA}, module._LIVE_ENV_EXTRA)
        self.assertEqual(cli.config["tool_timeout_sec"], module._LIVE_CALL_TIMEOUT_SEC)
        self.assertEqual(cli.config["args"][:2], ["-I", "-B"])
        system = cli.argv[cli.argv.index("--system-prompt-override") + 1]
        self.assertIn("1. host__exec_command", system)
        self.assertIn("exactly as listed", system)

        second = self.turn(_answered(_TASK, [("call-a-0", "exec_command", {"cmd": "git branch"})],
                                     [("call-a-0", "main")]))
        self.assertEqual(second, [{"type": "text_delta", "text": "On main."},
                                  {"type": "message_stop", "stop_reason": "end_turn"}])
        self.assertEqual(len(self.spawned), 1)
        self.assertEqual(cli.results["call-a-0"], {"content": [{"type": "text", "text": "main"}], "isError": False})
        self.assertTrue(cli.closed.wait(5))

    def test_catalog_unsafe_names_are_served_under_aliases_that_map_back(self):
        tools = [*_HOST_TOOLS, {"name": "ph_mcp__codex_apps__create_key", "description": "Create a key.",
                                "input_schema": {"type": "object", "properties": {}}}]
        toolset = module._plan_turn({"model": "grok-4.7", "messages": [{"role": "user", "content": "x"}],
                                     "tools": tools})["toolset"]
        key = toolset.model_name("ph_mcp__codex_apps__create_key")
        self.assertTrue(key.startswith("host__ph_mcp_codex_apps_create_key_"))
        self.assertIsNotNone(module._CATALOG_TOOL.fullmatch(key[len("host__"):]))
        served = key[len("host__"):]
        blocks, end = _handoff("msg_0", ("call-k-0", key, {}))
        self.scripts = [[_USE_TOOL_INIT, *blocks, _calls(("call-k-0", served, {})), *end, _echo("call-k-0"),
                         *_message("msg_1", "Made it."), {"type": "result", "subtype": "success"}, _EXIT]]
        first = self.turn(_TASK, tools)
        self.assertEqual(first[0]["name"], "ph_mcp__codex_apps__create_key")
        second = self.turn(_answered(_TASK, [("call-k-0", "ph_mcp__codex_apps__create_key", {})],
                                     [("call-k-0", "key-1")]), tools)
        self.assertEqual([e.get("text") for e in second if e["type"] == "text_delta"], ["Made it."])
        self.assertEqual(len(self.spawned), 1)

    def test_parallel_calls_pair_by_tool_and_arguments_without_ids(self):
        blocks, end = _handoff("msg_0", ("call-p-0", "host__exec_command", {"cmd": "ls"}),
                               ("call-p-1", "host__get_goal", {}))
        self.scripts = [[_USE_TOOL_INIT, *blocks,
                         _calls(("call-p-1", "get_goal", {}), ("call-p-0", "exec_command", {"cmd": "ls"})),
                         *end, _echo("call-p-0", "call-p-1"), *_message("msg_1", "Done."),
                         {"type": "result", "subtype": "success"}, _EXIT]]
        self.turn(_TASK)
        self.turn(_answered(_TASK, [("call-p-0", "exec_command", {"cmd": "ls"}), ("call-p-1", "get_goal", {})],
                            [("call-p-0", "a.txt"), ("call-p-1", "ship it")]))
        [cli] = self.spawned
        self.assertEqual(cli.results["call-p-0"]["content"], [{"type": "text", "text": "a.txt"}])
        self.assertEqual(cli.results["call-p-1"]["content"], [{"type": "text", "text": "ship it"}])

    def test_init_listing_the_bridged_catalog_is_not_a_stray_tool(self):
        # The server can finish connecting before the turn starts (1.0.41),
        # and init then lists its catalog keys; the model still sees use_tool.
        init = {"type": "system", "subtype": "init", "tools": ["use_tool", "host__exec_command", "host__get_goal"],
                "mcp_servers": [{"name": "host", "status": "connected"}]}
        self.scripts = [[init, *_message("msg_0", "Hi."), {"type": "result", "subtype": "success"}, _EXIT]]
        events = self.turn(_TASK)
        self.assertEqual([e["type"] for e in events], ["text_delta", "message_stop"])
        self.assertEqual(len(self.spawned), 1)

    def test_a_call_that_streams_in_after_the_handoff_is_handed_over_next(self):
        first, end = _handoff("msg_0", ("call-x-0", "host__exec_command", {"cmd": "ls"}))
        late = _use_tool(1, "call-x-1", {"tool_name": "host__get_goal", "tool_input": {}})
        self.scripts = [[_USE_TOOL_INIT, *first, _calls(("call-x-0", "exec_command", {"cmd": "ls"})),
                         ("pause",), *late, _calls(("call-x-1", "get_goal", {})), *end,
                         _echo("call-x-0", "call-x-1"), *_message("msg_1", "Both done."),
                         {"type": "result", "subtype": "success"}, _EXIT]]
        leg1 = self.turn(_TASK)
        self.assertEqual([(e["type"], e.get("id")) for e in leg1], [("tool_call", "call-x-0"), ("message_stop", None)])
        history = _answered(_TASK, [("call-x-0", "exec_command", {"cmd": "ls"})], [("call-x-0", "a.txt")])
        self.spawned[0].resume.set()
        leg2 = self.turn(history)
        self.assertEqual([(e["type"], e.get("id")) for e in leg2], [("tool_call", "call-x-1"), ("message_stop", None)])
        leg3 = self.turn(_answered(history, [("call-x-1", "get_goal", {})], [("call-x-1", "ship it")]))
        self.assertEqual([e.get("text") for e in leg3 if e["type"] == "text_delta"], ["Both done."])
        self.assertEqual(len(self.spawned), 1)

    def test_screenshots_and_new_input_replay_into_a_fresh_cli(self):
        png = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")
        image = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": png}}
        calls = [("call-s-0", "exec_command", {"cmd": "ls"})]
        for label, history in (("screenshot", _answered(_TASK, calls, [("call-s-0", [image])])),
                               ("new input", [*_answered(_TASK, calls, [("call-s-0", "a.txt")]),
                                              {"role": "user", "content": [{"type": "text", "text": "stop"}]}])):
            with self.subTest(label):
                self.spawned.clear()
                blocks, _ = _handoff("msg_0", ("call-s-0", "host__exec_command", {"cmd": "ls"}))
                self.scripts = [[_USE_TOOL_INIT, *blocks, _calls(("call-s-0", "exec_command", {"cmd": "ls"}))],
                                [_USE_TOOL_INIT, *_message("msg_0", "Fresh."),
                                 {"type": "result", "subtype": "success"}, _EXIT]]
                self.turn(_TASK)
                replayed = self.turn(history)
                self.assertEqual([e.get("text") for e in replayed if e["type"] == "text_delta"], ["Fresh."])
                self.assertEqual(len(self.spawned), 2)

    def test_a_refused_dispatch_falls_back_to_the_stateless_handoff(self):
        blocks, end = _handoff("msg_0", ("call-r-0", "exec_command", {"cmd": "ls"}))
        refusal = {"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "call-r-0", "is_error": True,
             "content": "Tool `exec_command` not found in the catalog"}]}}
        self.scripts = [[_USE_TOOL_INIT, *blocks, *end, refusal]]
        events = self.turn(_TASK)
        self.assertEqual([e["type"] for e in events], ["tool_call", "message_stop"])
        self.spawned[0].process.terminate.assert_called()
        self.assertEqual([lease for lease in self.pool.leases if lease.pending], [])

    def test_live_argv_needs_the_dispatcher(self):
        with self.assertRaises(module.GrokCliAgentError):
            module.build_argv("grok-4.7", run_host_tools=True)
        argv = module.build_argv("grok-4.7", host_tools=True, run_host_tools=True, search=True)
        self.assertEqual(argv[argv.index("MCPTool(host__*)") - 1], "--allow")
        self.assertEqual(argv[-2:], ["--tools", "web_search"])


if __name__ == "__main__":
    unittest.main()
