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
        self.assertEqual(result, "grok-4.6")

    def test_empty_string_returns_default(self):
        result = module._validate_model("")
        self.assertEqual(result, "grok-4.6")

    def test_whitespace_only_returns_default(self):
        result = module._validate_model("   ")
        self.assertEqual(result, "grok-4.6")

    def test_valid_model_pass_through(self):
        result = module._validate_model("grok-4.5")
        self.assertEqual(result, "grok-4.5")

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
        self.assertIn("plan", argv)
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
        self.assertIn("grok-4.6", argv)


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



if __name__ == "__main__":
    unittest.main()
