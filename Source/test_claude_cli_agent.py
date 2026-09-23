"""Tests for the Claude Code CLI model route (Source/claude_cli_agent.py).

unittest only (pytest is not part of the uv environment). No real CLI is ever
spawned and no network is touched: read-only probes are driven by injected
``capture`` callables and turns are driven through a fake ``StdioSession``
injected by monkeypatching the module's ``StdioSession`` name.
"""
from __future__ import annotations

import json
import unittest
from unittest import mock

import claude_cli_agent as m


class _FakeStdin:
    def write(self, data):
        pass

    def flush(self):
        pass

    def close(self):
        pass


class FakeSession:
    """Minimal stand-in for cli_session.StdioSession."""

    def __init__(self, argv, env=None, timeout=None, spawner=None, stderr=None):
        self.argv = list(argv)
        self.env = env
        self.timeout = timeout
        self.spawner = spawner
        self.stderr = stderr
        self.lines = []
        self.returncode = 0
        self.process = None
        self.stdin = _FakeStdin()
        self._events_exc = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def events(self, timeout=None):
        if self._events_exc is not None:
            raise self._events_exc
        for line in self.lines:
            yield line

    def close(self):
        pass

    def send(self, *args, **kwargs):
        pass


def _cap(rc, out="", err=""):
    """An injected capture callable returning (returncode, stdout, stderr)."""
    return lambda argv, **kwargs: (rc, out, err)


def _boom(argv, **kwargs):
    raise RuntimeError("capture exploded")


def _se(delta_type, key, text):
    return {"type": "stream_event",
            "event": {"type": "content_block_delta",
                      "delta": {"type": delta_type, key: text}}}


def _result_event(subtype="success", stop_reason="end_turn", result="full answer"):
    return {"type": "result", "subtype": subtype,
            "stop_reason": stop_reason, "result": result}


class BuildArgvTests(unittest.TestCase):
    def test_structure_and_variadic_termination(self):
        argv = m.build_argv("sonnet", effort="high")
        self.assertEqual(argv[0], "claude")
        self.assertEqual(argv[1], "-p")
        self.assertIn("--output-format", argv)
        self.assertIn("stream-json", argv)
        for flag in ("--permission-mode", "default", "--permission-prompts", "none",
                     "--strict-mcp-config", "--disable-slash-commands",
                     "--no-session-persistence"):
            self.assertIn(flag, argv)
        self.assertIn("--model", argv)
        self.assertEqual(argv[argv.index("--model") + 1], "claude-sonnet-5")
        # The variadic --tools flag must terminate argv.
        self.assertEqual(argv[-2], "--tools")
        self.assertEqual(argv[-1], "")
        # Safety invariants hold by construction.
        for flag in m._FORBIDDEN_FLAGS:
            self.assertNotIn(flag, argv)

    def test_forbidden_flag_assertion_fires(self):
        for bad in ([*list(m._FORBIDDEN_FLAGS)],
                    ["claude", "--always-approve"],
                    ["claude", "--dangerously-skip-permissions=yes"],
                    ["claude", "--approve-for-me"]):
            with self.assertRaises(m.ClaudeCliAgentError):
                m._assert_safe(bad)
        self.assertEqual(m._assert_safe(["claude", "-p"]), ["claude", "-p"])


    def test_permission_mode_default_no_persona(self):
        """Verify that --permission-mode default does NOT inject plan-mode persona.
        
        This test asserts the fix for the bug where plan mode's persona
        ("I'm in plan mode...") was leaking through to users despite tools
        being disabled via --tools "". The chosen mode is "default" which keeps
        tools: [] and maintains fail-closed posture via --permission-prompts none.
        """
        argv = m.build_argv("sonnet")
        # Must use default, not plan
        mode_idx = argv.index("--permission-mode")
        self.assertEqual(argv[mode_idx + 1], "default")
        # Must still have fail-closed posture
        self.assertIn("--permission-prompts", argv)
        self.assertIn("none", argv)
        # Must still disable tools
        self.assertIn("--tools", argv)
        self.assertEqual(argv[argv.index("--tools") + 1], "")
        # Must still have other read-only flags
        for flag in ("--strict-mcp-config", "--disable-slash-commands",
                      "--no-session-persistence"):
            self.assertIn(flag, argv)

    def test_effort_accepted_mapped_rejected(self):
        for rank in m.CLAUDE_EFFORTS:
            argv = m.build_argv("sonnet", effort=rank)
            self.assertEqual(argv[argv.index("--effort") + 1], rank)
        # Desktop aliases fold onto the claude ladder.
        for requested, expected in (("none", "low"), ("minimal", "low"),
                                   ("ultra", "max")):
            argv = m.build_argv("sonnet", effort=requested)
            self.assertEqual(argv[argv.index("--effort") + 1], expected)
        # No effort flag when none was requested.
        self.assertNotIn("--effort", m.build_argv("sonnet", effort=None))
        with self.assertRaises(m.ClaudeCliAgentError):
            m.build_argv("sonnet", effort="bogus")
        with self.assertRaises(m.ClaudeCliAgentError):
            m.build_argv("sonnet", effort=123)

    def test_stream_vs_non_stream_flags(self):
        streaming = m.build_argv("sonnet", stream=True)
        self.assertIn("stream-json", streaming)
        self.assertIn("--include-partial-messages", streaming)
        self.assertIn("--verbose", streaming)
        plain = m.build_argv("sonnet", stream=False)
        self.assertIn("text", plain)
        self.assertNotIn("stream-json", plain)
        self.assertNotIn("--include-partial-messages", plain)
        self.assertNotIn("--verbose", plain)

    def test_system_flag_forms(self):
        argv = m.build_argv("sonnet", system="be nice")
        idx = argv.index("--append-system-prompt")
        self.assertEqual(argv[idx + 1], "be nice")
        # Leading-dash text is attached so it cannot be mistaken for a flag.
        argv2 = m.build_argv("sonnet", system="-leading")
        self.assertIn("--append-system-prompt=-leading", argv2)
        self.assertNotIn("--append-system-prompt", m.build_argv("sonnet"))
        self.assertNotIn("--append-system-prompt", m.build_argv("sonnet", system="   "))
        with self.assertRaises(m.ClaudeCliAgentError):
            m.build_argv("sonnet", system=123)

    def test_model_validation(self):
        with self.assertRaises(m.ClaudeCliAgentError):
            m.build_argv("", effort=None)
        with self.assertRaises(m.ClaudeCliAgentError):
            m.build_argv("has a space", effort=None)


class ValidationTests(unittest.TestCase):
    def test_validate_model(self):
        self.assertEqual(m._validate_model("sonnet"), "sonnet")
        self.assertEqual(m._validate_model("  opus  "), "opus")
        for bad in ("", "   ", "has a space", "bad;rm"):
            with self.assertRaises(m.ClaudeCliAgentError):
                m._validate_model(bad)

    def test_validate_effort(self):
        self.assertIsNone(m._validate_effort(None))
        for rank in m.CLAUDE_EFFORTS:
            self.assertEqual(m._validate_effort(rank), rank)
        self.assertEqual(m._validate_effort("none"), "low")
        self.assertEqual(m._validate_effort("minimal"), "low")
        self.assertEqual(m._validate_effort("ultra"), "max")
        with self.assertRaises(m.ClaudeCliAgentError):
            m._validate_effort("bogus")
        with self.assertRaises(m.ClaudeCliAgentError):
            m._validate_effort(123)


class RenderPromptTests(unittest.TestCase):
    def test_single_bare_user_is_verbatim(self):
        prompt = m.render_prompt([{"role": "user", "content": "hi"}])
        self.assertEqual(prompt, "hi")
        self.assertNotIn(m._TRANSCRIPT_HEADER, prompt)

    def test_multi_turn_has_header_and_footer(self):
        prompt = m.render_prompt(
            [{"role": "user", "content": "hello"},
             {"role": "assistant", "content": "hi there"}])
        self.assertIn(m._TRANSCRIPT_HEADER, prompt)
        self.assertIn(m._TRANSCRIPT_FOOTER, prompt)
        self.assertIn('<turn role="user">', prompt)
        self.assertIn('<turn role="assistant">', prompt)

    def test_system_block_when_present(self):
        prompt = m.render_prompt([{"role": "user", "content": "hi"}],
                                 system="be nice")
        self.assertIn(m._TRANSCRIPT_HEADER, prompt)
        self.assertIn("<system>\nbe nice\n</system>", prompt)
        self.assertIn(m._TRANSCRIPT_FOOTER, prompt)

    def test_coerce_messages_roles_and_content(self):
        self.assertEqual(
            m._coerce_messages([{"role": "user", "content": "a"},
                                {"role": "assistant", "content": 123}]),
            [{"role": "user", "content": "a"},
             {"role": "assistant", "content": "123"}])
        self.assertEqual(
            m._coerce_messages([{"role": "user", "content": None}]),
            [{"role": "user", "content": ""}])
        with self.assertRaises(m.ClaudeCliAgentError):
            m._coerce_messages([{"role": "system", "content": "x"}])
        with self.assertRaises(m.ClaudeCliAgentError):
            m._coerce_messages([{"role": "user", "content": "x"},
                                {"role": "tool", "content": "y"}])
        with self.assertRaises(m.ClaudeCliAgentError):
            m._coerce_messages([{"role": "assistant", "content": "only"}])
        with self.assertRaises(m.ClaudeCliAgentError):
            m._coerce_messages("not a list")


class DiscoverTests(unittest.TestCase):
    @mock.patch.object(m, "_resolve_binary", return_value="/fake/claude")
    def test_installed_with_version(self, _rb):
        d = m.discover(capture=_cap(0, "claude 2.1.276\n", ""))
        self.assertEqual(d, {"installed": True, "binary": "/fake/claude",
                             "version": "2.1.276"})

    @mock.patch.object(m, "_resolve_binary", return_value=None)
    def test_missing(self, _rb):
        self.assertEqual(m.discover(),
                         {"installed": False, "binary": None, "version": None})

    @mock.patch.object(m, "_resolve_binary", return_value="/fake/claude")
    def test_capture_failure_is_still_installed(self, _rb):
        d = m.discover(capture=_boom)
        self.assertEqual(d["installed"], True)
        self.assertIsNone(d["version"])


class AuthStateTests(unittest.TestCase):
    @mock.patch.object(m, "_resolve_binary", return_value="/fake/claude")
    def test_authenticated(self, _rb):
        payload = json.dumps({"loggedIn": True, "authMethod": "oauth",
                              "subscriptionType": "Pro"})
        state = m.auth_state(capture=_cap(0, payload, ""))
        self.assertEqual(state["state"], "authenticated")
        self.assertIn("oauth", state["detail"])
        self.assertIn("Pro", state["detail"])

    @mock.patch.object(m, "_resolve_binary", return_value="/fake/claude")
    def test_missing(self, _rb):
        state = m.auth_state(capture=_cap(0, json.dumps({"loggedIn": False}), ""))
        self.assertEqual(state["state"], "missing")

    @mock.patch.object(m, "_resolve_binary", return_value="/fake/claude")
    def test_unparseable_json(self, _rb):
        state = m.auth_state(capture=_cap(0, "not json", ""))
        self.assertEqual(state["state"], "unknown")

    @mock.patch.object(m, "_resolve_binary", return_value="/fake/claude")
    def test_non_object_payload(self, _rb):
        state = m.auth_state(capture=_cap(0, json.dumps(["list"]), ""))
        self.assertEqual(state["state"], "unknown")

    @mock.patch.object(m, "_resolve_binary", return_value="/fake/claude")
    def test_no_logged_in_field(self, _rb):
        state = m.auth_state(capture=_cap(0, json.dumps({}), ""))
        self.assertEqual(state["state"], "unknown")

    @mock.patch.object(m, "_resolve_binary", return_value="/fake/claude")
    def test_nonzero_rc_is_unknown(self, _rb):
        state = m.auth_state(capture=_cap(1, "", "auth blew up"))
        self.assertEqual(state["state"], "unknown")
        self.assertIn("1", state["detail"])

    @mock.patch.object(m, "_resolve_binary", return_value="/fake/claude")
    def test_probe_exception_is_unknown(self, _rb):
        state = m.auth_state(capture=_boom)
        self.assertEqual(state["state"], "unknown")

    @mock.patch.object(m, "_resolve_binary", return_value=None)
    def test_missing_binary(self, _rb):
        self.assertEqual(m.auth_state()["state"], "missing")


class CatalogueTests(unittest.TestCase):
    def test_always_empty_with_reason(self):
        rows, warnings = m.catalogue()
        self.assertEqual(rows, [])
        self.assertEqual(warnings, [m._NO_MODEL_LIST_WARNING])


class RunTurnTests(unittest.TestCase):
    def _stream(self, lines, returncode=0, events_exc=None):
        fake = FakeSession([])
        fake.lines = lines
        fake.returncode = returncode
        fake._events_exc = events_exc
        return fake

    def _run(self, request, fake, *, spawner="spawner-stub"):
        sessions = []

        def factory(argv, **kwargs):
            fake.argv = list(argv)
            fake.env = kwargs.get("env")
            fake.timeout = kwargs.get("timeout")
            fake.spawner = kwargs.get("spawner")
            fake.stderr = kwargs.get("stderr")
            sessions.append(fake)
            return fake

        with mock.patch.object(m, "StdioSession", side_effect=factory), \
                mock.patch.object(m, "_resolve_binary", return_value="/fake/claude"):
            events = list(m.run_turn(request, spawner=spawner))
        return events, fake, sessions

    def _search_turn(self, web_search):
        start = {"type": "stream_event", "event": {"type": "content_block_start", "index": 1, "content_block": {
            "type": "tool_use", "id": "toolu_1", "name": "WebSearch", "input": {}}}}
        result = {"type": "user", "message": {"role": "user", "content": [
                      {"type": "tool_result", "tool_use_id": "toolu_1", "content": "Web search results for ..."}]},
                  "tool_use_result": {"query": "tide times leith", "durationSeconds": 2.1, "results": [
                      {"tool_use_id": "srvtoolu_1", "content": [
                          {"title": "Leith tides", "url": "https://tides.example/leith"},
                          {"title": "local", "url": "file:///etc/hosts"}]},
                      "A plain-text commentary entry."]}}
        fake = self._stream([json.dumps(start), json.dumps(result),
                             json.dumps(_se("text_delta", "text", "High tide 14:02.")),
                             json.dumps(_result_event(result="High tide 14:02."))])
        return self._run({"model": "sonnet", "messages": [{"role": "user", "content": "tides?"}],
                          "web_search": web_search}, fake)

    def test_requested_search_runs_websearch_alone_and_streams_searches(self):
        events, session, _ = self._search_turn({"context_size": None, "allowed_domains": [], "live": True})
        self.assertEqual(session.argv[-4:], ["--allowedTools", "WebSearch", "--tools", "WebSearch"])
        self.assertEqual([e["type"] for e in events], ["web_search", "web_search", "text_delta", "message_stop"])
        self.assertEqual(events[0], {"type": "web_search", "status": "in_progress", "id": "toolu_1"})
        self.assertEqual(events[1]["id"], "toolu_1")
        self.assertEqual(events[1]["action"], {"type": "search", "query": "tide times leith"})
        self.assertEqual(events[1]["results"], [{"url": "https://tides.example/leith", "title": "Leith tides"}])

    def test_search_never_widens_a_cached_or_domain_limited_request(self):
        for web_search in (None, {"live": False, "allowed_domains": []},
                           {"live": True, "allowed_domains": ["tides.example"]}):
            with self.subTest(web_search=web_search):
                events, session, _ = self._search_turn(web_search)
                self.assertEqual(session.argv[-2:], ["--tools", ""])
                self.assertNotIn("WebSearch", session.argv)
                self.assertEqual([e["type"] for e in events], ["text_delta", "message_stop"])

    def test_bad_request_degrades(self):
        with mock.patch.object(m, "_resolve_binary", return_value="/fake/claude"):
            events = list(m.run_turn("not an object"))
        self.assertEqual([e["type"] for e in events], ["error"])

    def test_missing_binary_degrades(self):
        with mock.patch.object(m, "_resolve_binary", return_value=None):
            events = list(m.run_turn(
                {"model": "sonnet",
                 "messages": [{"role": "user", "content": "hi"}]}))
        self.assertEqual([e["type"] for e in events], ["error"])
        self.assertIn("not found", events[0]["message"])

    def test_streams_text_delta_and_stop(self):
        fake = self._stream([json.dumps(_se("text_delta", "text", "hello")),
                             json.dumps(_result_event(result="hello"))])
        events, session, _ = self._run(
            {"model": "sonnet",
             "messages": [{"role": "user", "content": "hi"}]}, fake)
        self.assertEqual([e["type"] for e in events],
                         ["text_delta", "message_stop"])
        self.assertEqual(events[0]["text"], "hello")
        self.assertEqual(events[1]["stop_reason"], "end_turn")
        self.assertEqual(session.spawner, "spawner-stub")
        self.assertEqual(session.argv[0], "/fake/claude")
        self.assertNotIn("hi", session.argv)

    def test_thinking_delta(self):
        fake = self._stream([json.dumps(_se("thinking_delta", "thinking", "hmm")),
                             json.dumps(_result_event(result="full answer"))])
        events, _, _ = self._run(
            {"model": "sonnet",
             "messages": [{"role": "user", "content": "hi"}]}, fake)
        # A thinking-only stream still falls back to the result text before the
        # single terminal event.
        self.assertEqual([e["type"] for e in events],
                         ["thinking_delta", "text_delta", "message_stop"])
        self.assertEqual(events[0]["text"], "hmm")
        self.assertEqual(events[1]["text"], "full answer")

    def test_message_stop_exactly_once(self):
        fake = self._stream([json.dumps(_se("text_delta", "text", "a")),
                             json.dumps(_se("thinking_delta", "thinking", "t")),
                             json.dumps(_se("text_delta", "text", "b")),
                             json.dumps(_result_event(result="ab"))])
        events, _, _ = self._run(
            {"model": "sonnet",
             "messages": [{"role": "user", "content": "hi"}]}, fake)
        terminals = [e for e in events if e["type"] in ("message_stop", "error")]
        self.assertEqual(len(terminals), 1)
        self.assertEqual(events[-1]["type"], "message_stop")
        self.assertEqual([e["type"] for e in events],
                         ["text_delta", "thinking_delta", "text_delta",
                          "message_stop"])

    def test_nonzero_exit_after_text_preserves_text(self):
        fake = self._stream([json.dumps(_se("text_delta", "text", "partial"))],
                            returncode=2)
        events, _, _ = self._run(
            {"model": "sonnet",
             "messages": [{"role": "user", "content": "hi"}]}, fake)
        self.assertEqual([e["type"] for e in events],
                         ["text_delta", "error"])
        self.assertEqual(events[0]["text"], "partial")
        self.assertIn("exited with code 2", events[1]["message"])

    def test_nonzero_exit_without_text_is_error(self):
        fake = self._stream([], returncode=2)
        events, _, _ = self._run(
            {"model": "sonnet",
             "messages": [{"role": "user", "content": "hi"}]}, fake)
        self.assertEqual([e["type"] for e in events], ["error"])
        self.assertIn("2", events[0]["message"])

    def test_zero_event_clean_exit_is_error(self):
        fake = self._stream([], returncode=0)
        events, _, _ = self._run(
            {"model": "sonnet",
             "messages": [{"role": "user", "content": "hi"}]}, fake)
        self.assertEqual([e["type"] for e in events], ["error"])
        self.assertIn("produced no output", events[0]["message"])

    def test_unparsable_stream_is_not_a_successful_turn(self):
        fake = self._stream(["plain diagnostic"])
        events, _, _ = self._run(
            {"model": "sonnet",
             "messages": [{"role": "user", "content": "hi"}]}, fake)
        self.assertEqual([e["type"] for e in events], ["error"])
        self.assertIn("before completing", events[0]["message"])

    def test_clean_eof_after_progress_without_result_is_an_error(self):
        fake = self._stream([_se("text_delta", "text", "I will inspect first.")])
        events, _, _ = self._run({"model": "sonnet", "messages": [
            {"role": "user", "content": "Inspect the file"}]}, fake)
        self.assertEqual([e["type"] for e in events], ["text_delta", "error"])
        self.assertIn("before completing", events[-1]["message"])

    def test_message_stop_alone_does_not_complete_the_cli_turn(self):
        fake = self._stream([_se("text_delta", "text", "Progress."),
                             {"type": "stream_event", "event": {"type": "message_delta",
                              "delta": {"stop_reason": "end_turn"}}},
                             {"type": "stream_event", "event": {"type": "message_stop"}}])
        events, _, _ = self._run({"model": "sonnet", "messages": [
            {"role": "user", "content": "Inspect the file"}]}, fake)
        self.assertEqual([e["type"] for e in events], ["text_delta", "error"])

    def test_cancelled_result_is_an_error(self):
        fake = self._stream([_result_event(subtype="cancelled", stop_reason=None, result="")])
        events, _, _ = self._run({"model": "sonnet", "messages": [
            {"role": "user", "content": "Inspect the file"}]}, fake)
        self.assertEqual([e["type"] for e in events], ["error"])
        self.assertIn("cancelled", events[0]["message"])

    def test_completed_snapshots_preserve_thinking_and_distinct_final_message(self):
        progress = {"type": "assistant", "message": {"id": "progress", "content": [
            {"type": "text", "text": "Progress."}]}}
        final = {"type": "assistant", "message": {"id": "final", "content": [
            {"type": "thinking", "thinking": "Published summary."},
            {"type": "text", "text": "Final answer."}]}}
        fake = self._stream([progress, progress, final, final, _result_event(result="Final answer.")])
        events, _, _ = self._run({"model": "sonnet", "messages": [
            {"role": "user", "content": "Inspect the file"}]}, fake)
        self.assertEqual(events, [{"type": "text_delta", "text": "Progress."},
                                  {"type": "thinking_delta", "text": "Published summary."},
                                  {"type": "text_delta", "text": "Final answer."},
                                  {"type": "message_stop", "stop_reason": "end_turn"}])

    def test_snapshot_recovers_missing_block_suffixes_once(self):
        fake = self._stream([
            {"type": "stream_event", "event": {"type": "message_start", "message": {"id": "m1"}}},
            {"type": "stream_event", "event": {"type": "content_block_delta", "index": 0,
             "delta": {"type": "thinking_delta", "thinking": "Think"}}},
            {"type": "stream_event", "event": {"type": "content_block_delta", "index": 1,
             "delta": {"type": "text_delta", "text": "Hello"}}},
            {"type": "assistant", "message": {"id": "m1", "content": [
                {"type": "thinking", "thinking": "Thinking."},
                {"type": "text", "text": "Hello world."},
                {"type": "text", "text": " Final."}]}},
            _result_event(result=" Final."),
        ])
        events, _, _ = self._run({"model": "sonnet", "messages": [
            {"role": "user", "content": "Hi"}]}, fake)
        self.assertEqual([e.get("text") for e in events[:-1]],
                         ["Think", "Hello", "ing.", " world.", " Final."])
        self.assertEqual([e["type"] for e in events[:-1]],
                         ["thinking_delta", "text_delta", "thinking_delta", "text_delta", "text_delta"])

    def test_per_block_snapshots_use_the_stream_index_after_thinking(self):
        def stream(kind, **fields):
            return {"type": "stream_event", "event": {"type": kind, **fields}}
        def snapshot(kind, text):
            return {"type": "assistant", "message": {"id": "m1", "content": [
                {"type": kind, kind: text}]}}
        text = "I'll inspect the repository."
        fake = self._stream([
            stream("message_start", message={"id": "m1"}),
            stream("content_block_start", index=0, content_block={"type": "thinking", "thinking": ""}),
            snapshot("thinking", ""), stream("content_block_stop", index=0),
            stream("content_block_start", index=1, content_block={"type": "text", "text": ""}),
            stream("content_block_delta", index=1, delta={"type": "text_delta", "text": text}),
            snapshot("text", text), snapshot("text", text),
            stream("content_block_stop", index=1),
            # Identical text in a genuinely new block must still be kept.
            stream("content_block_start", index=2, content_block={"type": "text", "text": ""}),
            stream("content_block_delta", index=2, delta={"type": "text_delta", "text": text[:10]}),
            snapshot("text", text), stream("content_block_stop", index=2),
            _result_event(result=text),
        ])
        events, _, _ = self._run({"model": "sonnet", "messages": [
            {"role": "user", "content": "Inspect the file"}]}, fake)
        self.assertEqual([e.get("text") for e in events[:-1]], [text, text[:10], text[10:]])
        self.assertEqual(events[-1]["type"], "message_stop")

    def test_result_restores_a_final_answer_after_earlier_text(self):
        for final in ("Progress. Final answer.", "A separate final answer."):
            with self.subTest(final=final):
                fake = self._stream([_se("text_delta", "text", "Progress."), _result_event(result=final)])
                events, _, _ = self._run({"model": "sonnet", "messages": [
                    {"role": "user", "content": "Inspect the file"}]}, fake)
                self.assertEqual(events[0], {"type": "text_delta", "text": "Progress."})
                self.assertEqual(events[1]["text"], final.removeprefix("Progress."))
                self.assertEqual(events[-1]["type"], "message_stop")

    def test_session_break_is_error(self):
        fake = self._stream([], events_exc=RuntimeError("boom"))
        events, _, _ = self._run(
            {"model": "sonnet",
             "messages": [{"role": "user", "content": "hi"}]}, fake)
        self.assertEqual([e["type"] for e in events], ["error"])
        self.assertIn("boom", events[0]["message"])

    def test_validation_errors(self):
        with mock.patch.object(m, "_resolve_binary", return_value="/fake/claude"):
            for request, needle in (
                ({"messages": [{"role": "user", "content": "hi"}]},
                 "model"),
                ({"model": "sonnet", "messages": "bad"},
                 "messages"),
                ({"model": "sonnet", "messages": [{"role": "user", "content": "x"}],
                  "effort": "bogus"},
                 "effort"),
                ({"model": "sonnet", "messages": [{"role": "user", "content": "x"}],
                  "system": 123},
                 "system"),
                ({"model": "sonnet", "messages": [{"role": "user", "content": "x"}],
                  "max_tokens": "big"},
                 "max_tokens"),
                ({"model": "sonnet", "messages": [{"role": "user", "content": "x"}],
                  "max_tokens": 0},
                 "max_tokens"),
            ):
                events = list(m.run_turn(request))
                self.assertEqual([e["type"] for e in events], ["error"])
                self.assertIn(needle, events[0]["message"])


if __name__ == "__main__":
    unittest.main()
