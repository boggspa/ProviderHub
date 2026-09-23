"""Unit tests for the Grok CLI agent adapter."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# Import the module under test
import grok_cli_agent as module


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
        self.returncode = 0
        self.process = None
        self.lines = []

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



if __name__ == "__main__":
    unittest.main()
