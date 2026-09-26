"""Tests for the Claude Code CLI model route (Source/claude_cli_agent.py).

unittest only (pytest is not part of the uv environment). No real CLI is ever
spawned and no network is touched: read-only probes are driven by injected
``capture`` callables and turns are driven through a fake ``StdioSession``
injected by monkeypatching the module's ``StdioSession`` name.
"""
from __future__ import annotations

import json
import queue
import threading
import time
import unittest

from cli_tool_call import TRANSCRIPT_FOOTER
from pathlib import Path
from unittest import mock

import claude_cli_agent as m
import host_tools_mcp
from codex_session_pool import SessionPool, digest, history_blocks


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


_HOST_TOOLS = [
    {"name": "exec_command", "description": "Run a shell command on the host.",
     "input_schema": {"type": "object", "properties": {"cmd": {"type": "string"}}, "required": ["cmd"]}},
    {"name": "get_goal", "description": "Read the thread goal.",
     "input_schema": {"type": "object", "properties": {}}},
]


def _init(tools):
    return {"type": "system", "subtype": "init", "tools": tools, "mcp_servers": []}


def _block_start(index, block):
    return {"type": "stream_event", "event": {"type": "content_block_start", "index": index,
                                              "content_block": block}}


def _json_delta(index, partial):
    return {"type": "stream_event", "event": {"type": "content_block_delta", "index": index,
                                              "delta": {"type": "input_json_delta", "partial_json": partial}}}


def _block_stop(index):
    return {"type": "stream_event", "event": {"type": "content_block_stop", "index": index}}


def _message_delta(stop_reason):
    return {"type": "stream_event", "event": {"type": "message_delta", "delta": {"stop_reason": stop_reason}}}


def _no_such_tool(identifier, name):
    """Claude Code 2.1.280's own reply to a tool it does not register."""
    return {"type": "user", "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": identifier, "is_error": True,
         "content": f"<tool_use_error>Error: No such tool available: {name}</tool_use_error>"}]}}


def _denied(identifier):
    """Claude Code 2.1.280's reply to an attached MCP tool that nothing pre-approved."""
    return {"type": "user", "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": identifier, "is_error": True,
         "content": "Permission for this tool use was denied. It requires approval, and this session "
                    "has no approval surface"}]}}


#: init once the host tools' MCP server has attached (as on claude 2.1.280).
_MCP_INIT = {"type": "system", "subtype": "init", "tools": [
    "WebSearch", "mcp__host__exec_command", "mcp__host__get_goal"],
    "mcp_servers": [{"name": "host", "status": "connected", "source": "dynamic"}]}


class _RecordingStdin(_FakeStdin):
    def __init__(self):
        self.written = []

    def write(self, data):
        self.written.append(data)

    @property
    def text(self):
        return "".join(self.written)


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

    def test_live_sessions_pre_approve_only_the_host_server(self):
        config = '{"mcpServers":{}}'
        argv = m.build_argv("sonnet", mcp_config=config, run_host_tools=True)
        self.assertEqual(argv[-4:], ["--allowedTools", "mcp__host", "--tools", ""])
        argv = m.build_argv("sonnet", search=True, mcp_config=config, run_host_tools=True)
        self.assertEqual(argv[-4:], ["--allowedTools", "WebSearch,mcp__host", "--tools", "WebSearch"])
        self.assertNotIn("--allowedTools", m.build_argv("sonnet", mcp_config=config))
        with self.assertRaises(m.ClaudeCliAgentError):
            m.build_argv("sonnet", run_host_tools=True)

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
        self.assertIn(TRANSCRIPT_FOOTER, prompt)
        self.assertIn('<turn role="user">', prompt)
        self.assertIn('<turn role="assistant">', prompt)

    def test_system_block_when_present(self):
        prompt = m.render_prompt([{"role": "user", "content": "hi"}],
                                 system="be nice")
        self.assertIn(m._TRANSCRIPT_HEADER, prompt)
        self.assertIn("<system>\nbe nice\n</system>", prompt)
        self.assertIn(TRANSCRIPT_FOOTER, prompt)

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

    # -- host tools as native MCP tools -----------------------------------------

    def _attempts(self, request, scripts):
        """Run a turn whose spawns replay ``scripts`` in order, one fresh session each."""
        sessions = []

        def factory(argv, **kwargs):
            fake = FakeSession(argv, **kwargs)
            fake.lines = [json.dumps(line) for line in scripts[len(sessions)]]
            fake.process = mock.Mock()
            fake.stdin = _RecordingStdin()
            fake.mcp = fake.served = None
            if "--mcp-config" in argv:
                fake.mcp = json.loads(argv[argv.index("--mcp-config") + 1])
                with open(fake.mcp["mcpServers"]["host"]["args"][-1], encoding="utf-8") as handle:
                    fake.served = json.load(handle)
            sessions.append(fake)
            return fake

        with mock.patch.object(m, "StdioSession", side_effect=factory), \
                mock.patch.object(m, "_resolve_binary", return_value="/fake/claude"):
            events = list(m.run_turn(request, spawner="spawner-stub"))
        return events, sessions

    def _host_turn(self, lines, *, tools=_HOST_TOOLS, search=True):
        request = {"model": "sonnet", "messages": [{"role": "user", "content": "read the emulator state"}],
                   "tools": tools}
        if search:
            request["web_search"] = {"context_size": None, "allowed_domains": [], "live": True}
        events, sessions = self._attempts(request, [lines, lines])
        return events, sessions[-1]

    def test_host_tools_attach_as_a_native_mcp_server(self):
        events, sessions = self._attempts(
            {"model": "sonnet", "system": "Be brief.", "tools": _HOST_TOOLS,
             "messages": [{"role": "user", "content": "hi"}]},
            [[_MCP_INIT, _se("text_delta", "text", "hello"), _result_event(result="hello")]])
        self.assertEqual([e["type"] for e in events], ["text_delta", "message_stop"])
        [session] = sessions
        server = session.mcp["mcpServers"]["host"]
        self.assertEqual(server["type"], "stdio")
        self.assertEqual(server["args"][:3], ["-I", "-B", str(Path(m.__file__).resolve().with_name("host_tools_mcp.py"))])
        self.assertEqual([tool["name"] for tool in session.served], ["exec_command", "get_goal"])
        self.assertEqual(session.served[0]["input_schema"], _HOST_TOOLS[0]["input_schema"])
        self.assertEqual(session.argv[-2:], ["--tools", ""])
        self.assertIn("--strict-mcp-config", session.argv)
        self.assertNotIn("--allowedTools", session.argv)
        self.assertEqual({key: session.env[key] for key in m._MCP_ENV}, m._MCP_ENV)
        system = session.argv[session.argv.index("--append-system-prompt") + 1]
        self.assertIn("`mcp__host__exec_command`", system)
        self.assertTrue(system.endswith("Be brief."))
        self.assertNotIn("<<<tool_call>>>", system + session.stdin.text)
        # The tools file lives only as long as the child.
        self.assertFalse(Path(server["args"][-1]).exists())

    def test_turns_without_host_tools_attach_nothing(self):
        _, [session] = self._attempts({"model": "sonnet", "messages": [{"role": "user", "content": "hi"}]},
                                      [[_se("text_delta", "text", "hello"), _result_event(result="hello")]])
        self.assertIsNone(session.mcp)
        self.assertNotIn("ENABLE_TOOL_SEARCH", session.env)

    def test_native_host_call_is_forwarded_before_the_cli_refuses_it(self):
        # Sonnet 5 under Codex, 24 Sep 2026, called host tools natively; the
        # CLI's refusal convinced it that it had no tools. Now the call is
        # handed over and the refusal never reaches the model.
        events, session = self._host_turn([
            _MCP_INIT,
            _se("text_delta", "text", "Reading the live state."),
            _block_start(1, {"type": "tool_use", "id": "toolu_9", "name": "mcp__host__exec_command", "input": {}}),
            _json_delta(1, ""),
            _json_delta(1, '{"cmd": "curl -s 127.0.0.1'),
            _json_delta(1, ':8765/state"}'),
            _block_stop(1),
            _message_delta("tool_use"),
            _denied("toolu_9"),
            _se("text_delta", "text", "I have no shell access in this session."),
            _result_event(result="I have no shell access in this session.")])
        self.assertEqual([e["type"] for e in events], ["text_delta", "tool_call", "message_stop"])
        self.assertEqual(events[1], {"type": "tool_call", "id": "toolu_9", "name": "exec_command",
                                     "input": {"cmd": "curl -s 127.0.0.1:8765/state"}})
        self.assertEqual(events[2]["stop_reason"], "tool_use")
        self.assertNotIn("no shell access", "".join(e.get("text", "") for e in events))
        session.process.terminate.assert_called_once()

    def test_plain_named_native_call_is_forwarded_once_the_registry_lacks_it(self):
        events, _ = self._host_turn([
            _MCP_INIT,
            _block_start(0, {"type": "tool_use", "id": "toolu_8", "name": "exec_command", "input": {}}),
            _json_delta(0, '{"cmd": "ls"}'),
            _block_stop(0),
            _message_delta("tool_use"),
            _no_such_tool("toolu_8", "exec_command")])
        self.assertEqual([(e["type"], e.get("name")) for e in events],
                         [("tool_call", "exec_command"), ("message_stop", None)])

    def test_parallel_native_calls_hand_off_together(self):
        events, _ = self._host_turn([
            _MCP_INIT,
            _block_start(0, {"type": "tool_use", "id": "toolu_1", "name": "mcp__host__get_goal", "input": {}}),
            _block_stop(0),
            _block_start(1, {"type": "tool_use", "id": "toolu_2", "name": "mcp__host__exec_command", "input": {}}),
            _json_delta(1, '{"cmd": "ls"}'),
            _block_stop(1),
            _message_delta("tool_use")])
        self.assertEqual([(e["type"], e.get("name")) for e in events],
                         [("tool_call", "get_goal"), ("tool_call", "exec_command"), ("message_stop", None)])
        self.assertEqual(events[0]["input"], {})

    def test_snapshot_and_stream_report_one_call_once(self):
        snapshot = {"type": "assistant", "message": {"id": "msg_1", "content": [
            {"type": "tool_use", "id": "toolu_3", "name": "mcp__host__exec_command", "input": {"cmd": "pwd"}}]}}
        for lines in (
                # Claude Code 2.1.280 sends the snapshot before content_block_stop.
                [_MCP_INIT,
                 _block_start(0, {"type": "tool_use", "id": "toolu_3", "name": "mcp__host__exec_command",
                                  "input": {}}),
                 _json_delta(0, '{"cmd": "pwd"}'), snapshot, _block_stop(0), _message_delta("tool_use")],
                # A snapshot alone, ended by the CLI's own tool result.
                [_MCP_INIT, snapshot, _denied("toolu_3")]):
            with self.subTest(stream=len(lines) > 3):
                events, _ = self._host_turn(lines)
                self.assertEqual([e["type"] for e in events], ["tool_call", "message_stop"])
                self.assertEqual(events[0]["name"], "exec_command")
                self.assertEqual(events[0]["input"], {"cmd": "pwd"})

    def test_plain_names_are_never_forwarded_without_a_reported_registry(self):
        events, session = self._host_turn([
            _block_start(0, {"type": "tool_use", "id": "toolu_4", "name": "exec_command", "input": {}}),
            _json_delta(0, '{"cmd": "ls"}'),
            _block_stop(0),
            _message_delta("tool_use"),
            _no_such_tool("toolu_4", "exec_command"),
            _se("text_delta", "text", "Done differently."),
            _result_event(result="Done differently.")])
        self.assertEqual([e["type"] for e in events], ["text_delta", "message_stop"])
        session.process.terminate.assert_not_called()

    def test_a_tool_the_cli_registered_itself_is_never_forwarded(self):
        # The CLI would have run it already; forwarding would run it twice.
        events, _ = self._host_turn([
            _init(["WebSearch", "exec_command", "mcp__host__exec_command", "mcp__host__get_goal"]),
            _block_start(0, {"type": "tool_use", "id": "toolu_5", "name": "exec_command", "input": {}}),
            _json_delta(0, '{"cmd": "ls"}'),
            _block_stop(0),
            _message_delta("tool_use"),
            _se("text_delta", "text", "done"),
            _result_event(result="done")])
        self.assertEqual([e["type"] for e in events], ["text_delta", "message_stop"])

    def test_stray_native_call_is_a_protocol_error_only_when_host_tools_exist(self):
        lines = [
            _MCP_INIT,
            _block_start(0, {"type": "tool_use", "id": "toolu_6", "name": "Bash", "input": {}}),
            _json_delta(0, '{"command": "ls"}'),
            _block_stop(0),
            _message_delta("tool_use"),
            _no_such_tool("toolu_6", "Bash"),
            _se("text_delta", "text", "No tools here."),
            _result_event(result="No tools here.")]
        events, _ = self._host_turn(lines)
        self.assertEqual([e["type"] for e in events], ["error"])
        self.assertEqual(events[0]["code"], "invalid_cli_tool_call")
        self.assertIn("Bash", events[0]["message"])
        events, _ = self._host_turn([_init(["WebSearch"]), *lines[1:]], tools=[])
        self.assertEqual([e["type"] for e in events], ["text_delta", "message_stop"])

    def test_invalid_native_arguments_are_a_protocol_error(self):
        events, _ = self._host_turn([
            _MCP_INIT,
            _block_start(0, {"type": "tool_use", "id": "toolu_7", "name": "mcp__host__exec_command", "input": {}}),
            _json_delta(0, '{"cmd": '),
            _block_stop(0)])
        self.assertEqual([e["type"] for e in events], ["error"])
        self.assertEqual(events[0]["code"], "invalid_cli_tool_call")

    def test_unattached_host_tools_fall_back_to_the_text_manifest_invisibly(self):
        # init is the stream's first line: when the server's tools are not in
        # it, nothing has reached the client and the turn is spawned again.
        events, sessions = self._attempts(
            {"model": "sonnet", "tools": _HOST_TOOLS, "tool_choice": {"type": "any"},
             "messages": [{"role": "user", "content": "list files"}]},
            [[_init([]), _se("text_delta", "text", "never shown"), _result_event(result="never shown")],
             [_init([]), _se("text_delta", "text", "Listing."), _result_event(result="Listing.")]])
        self.assertEqual([(e["type"], e.get("text")) for e in events],
                         [("text_delta", "Listing."), ("message_stop", None)])
        first, second = sessions
        first.process.terminate.assert_called_once()
        self.assertIsNotNone(first.mcp)
        self.assertIsNone(second.mcp)
        self.assertNotIn("ENABLE_TOOL_SEARCH", second.env)
        system = second.argv[second.argv.index("--append-system-prompt") + 1]
        self.assertIn("<<<tool_call>>>", system)
        self.assertIn("You MUST call at least one tool", system)
        self.assertIn("<host_note>", second.stdin.text)

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


# -- live sessions ------------------------------------------------------------

_EXIT = object()


def _calls(*calls):
    """A script step: the CLI makes these MCP calls, as (tool_use id, served name, arguments)."""
    return ("calls", calls)


def _message_start(identifier):
    return {"type": "stream_event", "event": {"type": "message_start", "message": {"id": identifier}}}


def _text(index, text):
    return {"type": "stream_event", "event": {"type": "content_block_delta", "index": index,
                                              "delta": {"type": "text_delta", "text": text}}}


class _LiveCli:
    """A scripted Claude CLI whose host calls really wait in the hub's bridge.

    Script items are stream lines, ``_calls(...)`` to make MCP calls the way
    host_tools_mcp does (each blocks in the bridge until the hub delivers its
    result, then the CLI echoes the results as Claude Code 2.1.280 does), or
    ``_EXIT`` for a clean exit.
    """

    def __init__(self, argv, script, **kwargs):
        self.argv = list(argv)
        self.env = kwargs.get("env")
        self.lines = queue.Queue()
        self.returncode = None
        self.process = mock.Mock()
        self.process.terminate.side_effect = self._stop
        self.stdin = _RecordingStdin()
        self.closed = threading.Event()
        self.results = {}
        config = json.loads(argv[argv.index("--mcp-config") + 1])
        self.server = config["mcpServers"]["host"]
        with open(self.server["args"][-1], encoding="utf-8") as handle:
            self.served = json.load(handle)
        threading.Thread(target=self._play, args=(script,), daemon=True).start()

    def _play(self, script):
        for item in script:
            if item is _EXIT:
                self.returncode = 0
                self.lines.put(_EXIT)
            elif isinstance(item, tuple) and item[0] == "calls":
                self._call(item[1])
            else:
                self.lines.put(item)

    def _call(self, calls):
        def forward(identifier, name, arguments):
            request = {"jsonrpc": "2.0", "id": identifier, "method": "tools/call", "params": {
                "name": name, "arguments": arguments, "_meta": {host_tools_mcp.TOOL_USE_META: identifier}}}
            self.results[identifier] = host_tools_mcp.forward(request, self.served["bridge"])

        threads = [threading.Thread(target=forward, args=call, daemon=True) for call in calls]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        if self.returncode is None:
            for identifier, _, _ in calls:
                self.lines.put({"type": "user", "message": {"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": identifier,
                     "content": self.results[identifier]["content"]}]}})

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


_TASK = [{"role": "user", "content": [{"type": "text", "text": "list the files"}]}]


def _host_leg(identifier="toolu_1", arguments='{"cmd": "ls"}', text="Listing."):
    return [_message_start("msg_1"), _text(0, text),
            _block_start(1, {"type": "tool_use", "id": identifier, "name": "mcp__host__exec_command", "input": {}}),
            _json_delta(1, ""), _json_delta(1, arguments), _block_stop(1), _message_delta("tool_use")]


def _answered(history, calls, results, *, text="Listing."):
    """``history`` continued the way the host sends it: the handoff message, then its results."""
    return [*history,
            {"role": "assistant", "content": [{"type": "text", "text": text}, *[
                {"type": "tool_use", "id": identifier, "name": name, "input": arguments}
                for identifier, name, arguments in calls]]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": identifier, "content": result}
                                         for identifier, result in results]}]


class LiveSessionTests(unittest.TestCase):
    def setUp(self):
        self.pool = SessionPool(m._dispose_live, capacity=2, idle_ttl=0, pending_ttl=60, label="Claude")
        self.addCleanup(self.pool.close)
        self.spawned = []
        self.scripts = []

        def factory(argv, **kwargs):
            session = _LiveCli(argv, self.scripts[len(self.spawned)], **kwargs)
            self.spawned.append(session)
            return session

        for patcher in (mock.patch.object(m, "StdioSession", side_effect=factory),
                        mock.patch.object(m, "_resolve_binary", return_value="/fake/claude"),
                        mock.patch.object(m, "_LIVE_CONFIRM_SECONDS", 1.0)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def turn(self, history, **extra):
        request = {"model": "sonnet", "messages": [{"role": "user", "content": "list the files"}],
                   "tools": _HOST_TOOLS, "history": history, **extra}
        return list(m.run_turn(request, pool=self.pool))

    def test_the_cli_waits_in_its_host_call_and_the_result_resumes_it(self):
        self.scripts = [[_MCP_INIT, *_host_leg(), _calls(("toolu_1", "exec_command", {"cmd": "ls"})),
                         _message_start("msg_2"), _text(0, "Two files."),
                         _result_event(result="Two files."), _EXIT]]
        first = self.turn(_TASK)
        self.assertEqual([e["type"] for e in first], ["text_delta", "tool_call", "message_stop"])
        self.assertEqual(first[1], {"type": "tool_call", "id": "toolu_1", "name": "exec_command",
                                    "input": {"cmd": "ls"}})
        self.assertEqual(first[2]["stop_reason"], "tool_use")
        [cli] = self.spawned
        cli.process.terminate.assert_not_called()
        self.assertEqual(cli.argv[-4:], ["--allowedTools", "mcp__host", "--tools", ""])
        self.assertEqual(cli.server["timeout"], m._LIVE_CALL_TIMEOUT_MS)
        self.assertEqual({key: cli.env[key] for key in m._LIVE_ENV}, m._LIVE_ENV)
        self.assertEqual(cli.served["tools"][0]["annotations"], {"readOnlyHint": True})

        second = self.turn(_answered(_TASK, [("toolu_1", "exec_command", {"cmd": "ls"})],
                                     [("toolu_1", "a.txt\nb.txt")]))
        self.assertEqual(second, [{"type": "text_delta", "text": "Two files."},
                                  {"type": "message_stop", "stop_reason": "end_turn"}])
        self.assertEqual(len(self.spawned), 1)
        self.assertEqual(cli.results["toolu_1"], {"content": [{"type": "text", "text": "a.txt\nb.txt"}],
                                                  "isError": False})
        # A finished turn keeps nothing waiting.
        self.assertTrue(cli.closed.wait(5))

    def test_parallel_calls_wait_together_and_take_their_own_results(self):
        leg = [_MCP_INIT, _message_start("msg_1"),
               _block_start(0, {"type": "tool_use", "id": "toolu_a", "name": "mcp__host__get_goal", "input": {}}),
               _block_stop(0),
               _block_start(1, {"type": "tool_use", "id": "toolu_b", "name": "mcp__host__exec_command",
                                "input": {}}),
               _json_delta(1, '{"cmd": "pwd"}'), _block_stop(1), _message_delta("tool_use")]
        calls = [("toolu_a", "get_goal", {}), ("toolu_b", "exec_command", {"cmd": "pwd"})]
        self.scripts = [[*leg, _calls(*calls), _message_start("msg_2"), _text(0, "Done."),
                         _result_event(result="Done."), _EXIT]]
        first = self.turn(_TASK)
        self.assertEqual([(e["type"], e.get("id")) for e in first],
                         [("tool_call", "toolu_a"), ("tool_call", "toolu_b"), ("message_stop", None)])
        second = self.turn(_answered(_TASK, calls, [("toolu_b", [{"type": "text", "text": "/work"}]),
                                                    ("toolu_a", "ship it")], text=""))
        self.assertEqual([e["type"] for e in second], ["text_delta", "message_stop"])
        [cli] = self.spawned
        self.assertEqual(cli.results["toolu_a"]["content"], [{"type": "text", "text": "ship it"}])
        self.assertEqual(cli.results["toolu_b"]["content"], [{"type": "text", "text": "/work"}])

    def test_a_second_host_call_keeps_the_same_cli_waiting_again(self):
        self.scripts = [[_MCP_INIT, *_host_leg(), _calls(("toolu_1", "exec_command", {"cmd": "ls"})),
                         _message_start("msg_2"),
                         _block_start(0, {"type": "tool_use", "id": "toolu_2", "name": "mcp__host__exec_command",
                                          "input": {}}),
                         _json_delta(0, '{"cmd": "cat a.txt"}'), _block_stop(0), _message_delta("tool_use"),
                         _calls(("toolu_2", "exec_command", {"cmd": "cat a.txt"})),
                         _message_start("msg_3"), _text(0, "It says hi."), _result_event(result="It says hi."),
                         _EXIT]]
        self.turn(_TASK)
        history = _answered(_TASK, [("toolu_1", "exec_command", {"cmd": "ls"})], [("toolu_1", "a.txt")])
        second = self.turn(history)
        self.assertEqual([(e["type"], e.get("id")) for e in second],
                         [("tool_call", "toolu_2"), ("message_stop", None)])
        third = self.turn(_answered(history, [("toolu_2", "exec_command", {"cmd": "cat a.txt"})],
                                    [("toolu_2", "hi")], text=""))
        self.assertEqual([e.get("text") for e in third if e["type"] == "text_delta"], ["It says hi."])
        self.assertEqual(len(self.spawned), 1)

    def test_anything_but_an_exact_continuation_replays_into_a_fresh_cli(self):
        calls = [("toolu_1", "exec_command", {"cmd": "ls"})]
        variants = {
            "new user input": [*_answered(_TASK, calls, [("toolu_1", "a.txt")])[:-1],
                               {"role": "user", "content": [
                                   {"type": "tool_result", "tool_use_id": "toolu_1", "content": "a.txt"},
                                   {"type": "text", "text": "actually, stop"}]}],
            "edited history": _answered([{"role": "user", "content": [{"type": "text", "text": "other task"}]}],
                                        calls, [("toolu_1", "a.txt")]),
            "missing result": _answered(_TASK, calls, [])[:-1],
            "other call": _answered(_TASK, [("toolu_9", "exec_command", {"cmd": "ls"})], [("toolu_9", "a.txt")]),
        }
        for label, history in variants.items():
            with self.subTest(label):
                self.spawned.clear()
                self.scripts = [[_MCP_INIT, *_host_leg(), _calls(*calls)],
                                [_MCP_INIT, _message_start("msg_9"), _text(0, "Fresh."),
                                 _result_event(result="Fresh."), _EXIT]]
                self.turn(_TASK)
                replayed = self.turn(history)
                self.assertEqual([e.get("text") for e in replayed if e["type"] == "text_delta"], ["Fresh."])
                self.assertEqual(len(self.spawned), 2)
                self.assertEqual(self.spawned[0].results, {})

    def test_a_cli_that_does_not_wait_in_the_bridge_is_handed_off_statelessly(self):
        for label, tail in (("answered itself", [_denied("toolu_1")]), ("never called", [])):
            with self.subTest(label):
                self.spawned.clear()
                self.scripts = [[_MCP_INIT, *_host_leg(), *tail],
                                [_MCP_INIT, _message_start("msg_9"), _text(0, "Fresh."),
                                 _result_event(result="Fresh."), _EXIT]]
                first = self.turn(_TASK)
                self.assertEqual([e["type"] for e in first], ["text_delta", "tool_call", "message_stop"])
                self.spawned[0].process.terminate.assert_called()
                replayed = self.turn(_answered(_TASK, [("toolu_1", "exec_command", {"cmd": "ls"})],
                                               [("toolu_1", "a.txt")]))
                self.assertEqual([e.get("text") for e in replayed if e["type"] == "text_delta"], ["Fresh."])
                self.assertEqual(len(self.spawned), 2)

    def test_a_handoff_the_client_never_received_keeps_nothing_waiting(self):
        self.scripts = [[_MCP_INIT, *_host_leg(), _calls(("toolu_1", "exec_command", {"cmd": "ls"}))]]
        timing = mock.Mock(delivered=False)
        request = {"model": "sonnet", "messages": [{"role": "user", "content": "list the files"}],
                   "tools": _HOST_TOOLS, "history": _TASK, "_cli_timing": timing}
        events = list(m.run_turn(request, pool=self.pool))
        self.assertEqual(events[-1], {"type": "message_stop", "stop_reason": "tool_use"})
        self.assertTrue(self.spawned[0].closed.wait(5))
        self.assertEqual(self.spawned[0].results["toolu_1"]["content"][0]["text"], host_tools_mcp.NO_RESULT)

    def test_a_busy_pool_replays_and_a_cancelled_wait_is_an_error(self):
        self.scripts = [[_MCP_INIT, _message_start("msg_1"), _text(0, "hi"), _result_event(result="hi"), _EXIT]]
        with mock.patch.object(self.pool, "acquire", side_effect=RuntimeError("at capacity")):
            events = self.turn(_TASK)
        self.assertEqual([e["type"] for e in events], ["text_delta", "message_stop"])
        self.assertNotIn("mcp__host", self.spawned[0].argv)
        with mock.patch.object(self.pool, "acquire", side_effect=BrokenPipeError("The CLI request was cancelled.")):
            events = self.turn(_TASK)
        self.assertEqual([e["type"] for e in events], ["error"])
        self.assertIn("cancelled", events[0]["message"])
        self.assertEqual(len(self.spawned), 1)

    def test_without_typed_history_the_turn_stays_stateless(self):
        self.scripts = [[_MCP_INIT, _message_start("msg_1"), _text(0, "hi"), _result_event(result="hi"), _EXIT]]
        with mock.patch.object(m, "_live_turn") as live:
            request = {"model": "sonnet", "messages": [{"role": "user", "content": "hi"}], "tools": _HOST_TOOLS}
            events = list(m.run_turn(request, pool=self.pool))
        live.assert_not_called()
        self.assertEqual([e["type"] for e in events], ["text_delta", "message_stop"])
        self.assertNotIn("mcp__host", self.spawned[0].argv)


class ContinuationTests(unittest.TestCase):
    def lease(self, history, calls):
        blocks = history_blocks(history)
        return mock.Mock(pending={"calls": {identifier: {"id": identifier, "name": name, "input": arguments}
                                            for identifier, name, arguments in calls},
                                  "prefix": digest(blocks), "prefix_count": len(blocks)})

    def test_results_become_mcp_content(self):
        png = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")
        image = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": png}}
        self.assertEqual(m._mcp_result({"content": "a.txt"}), {"content": [{"type": "text", "text": "a.txt"}],
                                                               "isError": False})
        self.assertEqual(m._mcp_result({"content": [{"type": "text", "text": "shot"}, image], "is_error": True}),
                         {"content": [{"type": "text", "text": "shot"},
                                      {"type": "image", "data": png, "mimeType": "image/png"}], "isError": True})
        self.assertEqual(m._mcp_result({"content": ""}), {"content": [], "isError": False})
        for unsupported in ({"content": [{"type": "document", "source": {}}]},
                            {"content": [{"type": "image", "source": {"type": "url", "url": "https://x.test/a.png"}}]},
                            {"content": 7}):
            self.assertIsNone(m._mcp_result(unsupported))

    def test_only_the_handoff_message_and_its_results_may_follow(self):
        calls = [("toolu_1", "exec_command", {"cmd": "ls"})]
        lease = self.lease(_TASK, calls)
        exact = _answered(_TASK, calls, [("toolu_1", "a.txt")])
        self.assertEqual(m._continuation(lease, exact),
                         {"toolu_1": {"content": [{"type": "text", "text": "a.txt"}], "isError": False}})
        # Reasoning or no text in the handoff message is fine.
        self.assertIsNotNone(m._continuation(lease, _answered(_TASK, calls, [("toolu_1", "a.txt")], text="")))
        after_results = [*exact, {"role": "assistant", "content": [{"type": "text", "text": "more"}]}]
        renamed = _answered(_TASK, [("toolu_1", "get_goal", {"cmd": "ls"})], [("toolu_1", "a.txt")])
        twice = [*exact[:-1], {"role": "user", "content": exact[-1]["content"] * 2}]
        for history in (after_results, renamed, twice, _TASK, None, exact[:1] + exact[1:2]):
            self.assertIsNone(m._continuation(lease, history))
        self.assertIsNone(m._continuation(mock.Mock(pending=None), exact))


if __name__ == "__main__":
    unittest.main()
