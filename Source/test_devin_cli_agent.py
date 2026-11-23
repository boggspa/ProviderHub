"""Tests for the Devin CLI ACP model route (Source/devin_cli_agent.py).

unittest only (pytest is not part of the uv environment). No real CLI is ever
spawned and no network is touched: read-only probes are driven by injected
``capture`` callables and turns are driven through a fake ``StdioSession``
patched over the module's own reference, exactly like the muse/grok adapter
tests.
"""
from __future__ import annotations

import itertools
import json
from pathlib import Path
import shlex
import unittest
from unittest import mock

import devin_cli_agent as m


class _FakeStdin:
    def write(self, data):
        pass

    def flush(self):
        pass

    def close(self):
        pass


class FakeSession:
    """Minimal stand-in for cli_session.StdioSession.

    ``lines`` is the outbound (agent->client) script: dicts are yielded once
    by ``events()`` in order, raw strings are ignored by the adapter just
    like real diagnostic lines. ``request_script`` answers the initialize and
    session/new calls by method name.
    """

    def __init__(self, argv, *, env=None, cwd=None, timeout=None,
                 spawner=None, stderr=None):
        self.argv = list(argv)
        self.env = env
        self.cwd = cwd
        self.timeout = timeout
        self.spawner = spawner
        self.stderr = stderr
        self.lines = []
        self.sent = []
        self.requests = []
        self.notifications = []
        self.request_script = {}
        self.returncode = None
        self._eof = False
        self._ids = itertools.count(100)
        self.process = None
        self.stdin = _FakeStdin()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def events(self, timeout=None):
        while self.lines:
            yield self.lines.pop(0)

    def close(self):
        pass

    def send(self, payload):
        self.sent.append(payload)

    def request(self, method, params=None, timeout=None):
        self.requests.append((method, params))
        return self.request_script.get(method, {"result": {}})

    def notify(self, method, params=None):
        self.notifications.append((method, params))


def _update(kind, text):
    return {"jsonrpc": "2.0", "method": "session/update",
            "params": {"sessionId": "s1",
                       "update": {"sessionUpdate": kind,
                                  "content": {"type": "text", "text": text}}}}


def _prompt_response(rpc_id=100, stop="end_turn", error=None):
    response = {"jsonrpc": "2.0", "id": rpc_id}
    if error is not None:
        response["error"] = error
    else:
        response["result"] = {"stopReason": stop}
    return response


def _run(fake, request=None, *, timeout=30):
    """Drive run_turn against the fake session; returns (events, fake)."""
    sessions = []

    def factory(argv, **kwargs):
        fake.argv = list(argv)
        fake.env = kwargs.get("env")
        fake.timeout = kwargs.get("timeout")
        fake.spawner = kwargs.get("spawner")
        fake.cwd = kwargs.get("cwd")
        sessions.append(fake)
        return fake

    body = request or {"model": "swe-2",
                       "messages": [{"role": "user", "content": "hi"}]}
    with mock.patch.object(m, "StdioSession", side_effect=factory), \
            mock.patch.object(m, "_resolve_binary", return_value="/fake/devin"):
        events = list(m.run_turn(body, timeout=timeout))
    assert len(sessions) == 1
    return events, fake


class BuildArgvTests(unittest.TestCase):
    def test_structure(self):
        argv = m.build_argv("claude-opus-5.5")
        self.assertEqual(argv, ["devin", "acp", "--model", "claude-opus-5.5"])

    def test_model_defaults_and_validates(self):
        self.assertEqual(m.build_argv(None)[-1], m.DEFAULT_MODEL)
        self.assertEqual(m.build_argv("   ")[-1], m.DEFAULT_MODEL)
        with self.assertRaises(m.DevinCliAgentError):
            m.build_argv("-evil-flag")
        with self.assertRaises(m.DevinCliAgentError):
            m.build_argv(123)

    def test_system_never_reaches_argv(self):
        argv = m.build_argv("swe-2", system="SECRET SYSTEM TEXT")
        self.assertNotIn("SECRET SYSTEM TEXT", argv)


class PromptTests(unittest.TestCase):
    def test_single_user_turn_passes_through(self):
        self.assertEqual(m.render_prompt([{"role": "user", "content": "hi"}]), "hi")

    def test_transcript_shape(self):
        prompt = m.render_prompt(
            [{"role": "user", "content": "one"},
             {"role": "assistant", "content": "two"},
             {"role": "user", "content": "three"}],
            system="be brief")
        self.assertIn("<system>\nbe brief\n</system>", prompt)
        self.assertIn('<turn role="user">\none\n</turn>', prompt)
        self.assertIn('<turn role="assistant">\ntwo\n</turn>', prompt)
        self.assertLess(prompt.index('<turn role="user">\nthree'),
                        prompt.index("Respond now to the final"))

    def test_tool_result_footer(self):
        messages = [{"role": "user", "content": "result",
                     m.TOOL_RESULTS_KEY: True}]
        prompt = m.render_prompt(messages, system="sys")
        self.assertIn("holds host tool results", prompt)

    def test_coerce_rejects_bad_roles_and_missing_user(self):
        with self.assertRaises(m.DevinCliAgentError):
            m._coerce_messages([{"role": "tool", "content": "x"}])
        with self.assertRaises(m.DevinCliAgentError):
            m._coerce_messages([{"role": "assistant", "content": "x"}])
        with self.assertRaises(m.DevinCliAgentError):
            m._coerce_messages("not a list")


class ProbeTests(unittest.TestCase):
    @mock.patch.object(m, "_resolve_binary", return_value=None)
    def test_missing_binary(self, _rb):
        self.assertEqual(m.discover()["installed"], False)
        self.assertEqual(m.auth_state()["state"], "missing")
        rows, notes = m.catalogue()
        self.assertEqual(rows, [])
        self.assertTrue(notes)

    @mock.patch.object(m, "_resolve_binary", return_value="/fake/devin")
    def test_discover_version(self, _rb):
        d = m.discover(capture=lambda argv, **k: (0, "devin 3000.11.1\n", ""))
        self.assertTrue(d["installed"])
        self.assertEqual(d["version"], "devin 3000.11.1")

    @mock.patch.object(m, "_resolve_binary", return_value="/fake/devin")
    def test_auth_signed_out(self, _rb):
        state = m.auth_state(capture=lambda argv, **k: (
            0, "", "Not logged in.\nRun `devin auth login` to authenticate."))
        self.assertEqual(state["state"], "missing")

    @mock.patch.object(m, "_resolve_binary", return_value="/fake/devin")
    def test_auth_signed_in(self, _rb):
        state = m.auth_state(capture=lambda argv, **k: (
            0, "Logged in as dev@example.com\n", ""))
        self.assertEqual(state["state"], "authenticated")
        self.assertIn("dev@example.com", state["detail"])

    @mock.patch.object(m, "_resolve_binary", return_value="/fake/devin")
    def test_auth_config_dir_points_xdg_data_home(self, _rb):
        seen = {}

        def capture(argv, **kwargs):
            seen.update(kwargs)
            return (0, "Logged in as dev@example.com\n", "")

        state = m.auth_state(capture=capture, config_dir="/tmp/devin-acct")
        self.assertEqual(state["state"], "authenticated")
        self.assertEqual((seen.get("env") or {}).get("XDG_DATA_HOME"),
                         "/tmp/devin-acct")

    @mock.patch.object(m, "_resolve_binary", return_value="/fake/devin")
    def test_auth_default_account_sends_no_env_override(self, _rb):
        seen = {}

        def capture(argv, **kwargs):
            seen.update(kwargs)
            return (0, "Logged in\n", "")

        m.auth_state(capture=capture)
        self.assertFalse(seen.get("env"))

    @mock.patch.object(m, "_resolve_binary", return_value="/fake/devin")
    def test_auth_config_dir_must_be_absolute(self, _rb):
        # A relative folder is a caller bug, not a login state: the probe
        # reports unknown rather than guessing (same contract as claude).
        state = m.auth_state(capture=lambda argv, **k: (0, "", ""),
                             config_dir="relative/dir")
        self.assertEqual(state["state"], "unknown")

    @mock.patch.object(m, "_resolve_binary", return_value="/fake/devin")
    def test_catalogue_config_dir_points_xdg_data_home(self, _rb):
        seen = {}

        def capture(argv, **kwargs):
            seen.update(kwargs)
            return (0, json.dumps({"families": [{"slug": "swe-2"}]}), "")

        rows, _ = m.catalogue(capture=capture, config_dir="/tmp/devin-acct")
        self.assertTrue(rows)
        self.assertEqual((seen.get("env") or {}).get("XDG_DATA_HOME"),
                         "/tmp/devin-acct")

    @mock.patch.object(m, "_resolve_binary", return_value="/fake/devin")
    def test_catalogue_signed_out_is_a_note_not_an_error(self, _rb):
        rows, notes = m.catalogue(capture=lambda argv, **k: (
            1, "", "Error: Not logged in. Run `devin auth login`."))
        self.assertEqual(rows, [])
        self.assertTrue(any("auth login" in note for note in notes))

    @mock.patch.object(m, "_resolve_binary", return_value="/fake/devin")
    def test_catalogue_family_rows_preferred(self, _rb):
        payload = {"families": [
            {"slug": "swe-2", "family_label": "SWE-2",
             "variants": [{"model_uid": "swe-2-high", "label": "SWE-2 High"},
                          {"model_uid": "swe-2-max", "label": "SWE-2 Max"}]},
            {"slug": "claude-opus-5.5", "family_label": "Claude Opus 5.5"},
        ]}
        rows, notes = m.catalogue(capture=lambda argv, **k: (0, json.dumps(payload), ""))
        self.assertEqual(notes, [])
        self.assertEqual([r["id"] for r in rows], ["swe-2", "claude-opus-5.5"])
        self.assertEqual(rows[0]["display_name"], "SWE-2")

    @mock.patch.object(m, "_resolve_binary", return_value="/fake/devin")
    def test_catalogue_flat_model_rows(self, _rb):
        payload = [{"model_uid": "swe-2-high", "label": "SWE-2 High"},
                   {"model_uid": "swe-2-max", "label": "SWE-2 Max"}]
        rows, _ = m.catalogue(capture=lambda argv, **k: (0, json.dumps(payload), ""))
        self.assertEqual([r["id"] for r in rows], ["swe-2-high", "swe-2-max"])

    @mock.patch.object(m, "_resolve_binary", return_value="/fake/devin")
    def test_catalogue_bad_json_is_a_note(self, _rb):
        rows, notes = m.catalogue(capture=lambda argv, **k: (0, "not json", ""))
        self.assertEqual(rows, [])
        self.assertTrue(notes)


def _tool_call_update(tool_call_id, *, title=None, kind=None, raw_input=None,
                      meta=None):
    update = {"sessionUpdate": "tool_call", "toolCallId": tool_call_id}
    if title is not None:
        update["title"] = title
    if kind is not None:
        update["kind"] = kind
    if raw_input is not None:
        update["rawInput"] = raw_input
    if meta is not None:
        update["_meta"] = meta
    return {"jsonrpc": "2.0", "method": "session/update",
            "params": {"sessionId": "s1", "update": update}}


_TOOLS = [{"name": "get_weather",
           "description": "Fetch the weather.",
           "input_schema": {"type": "object",
                            "properties": {"city": {"type": "string"}}}}]


def _tools_request():
    return {"model": "swe-2",
            "messages": [{"role": "user", "content": "weather in Paris?"}],
            "tools": _TOOLS}


class TurnTests(unittest.TestCase):
    def _scripted(self, lines, *, session_id="s1"):
        fake = FakeSession([])
        fake.request_script = {
            "initialize": {"result": {"protocolVersion": 1}},
            "session/new": {"result": {"sessionId": session_id}},
        }
        fake.lines = list(lines)
        return fake

    def test_happy_path_streams_text_and_stops(self):
        fake = self._scripted([
            _update("agent_thought_chunk", "thinking…"),
            _update("agent_message_chunk", "Hello"),
            _update("agent_message_chunk", " world"),
            _update("tool_call", "ignored"),
            _prompt_response(100, "end_turn"),
        ])
        events, fake = _run(fake)
        kinds = [e["type"] for e in events]
        self.assertEqual(kinds, ["thinking_delta", "text_delta", "text_delta",
                                 "message_stop"])
        self.assertEqual(events[1]["text"], "Hello")
        self.assertEqual(events[-1]["stop_reason"], "end_turn")
        # The ACP handshake ran in order and the prompt carried the session id.
        self.assertEqual([m for m, _ in fake.requests],
                         ["initialize", "session/new"])
        prompt = json.loads(fake.sent[-1])
        self.assertEqual(prompt["method"], "session/prompt")
        self.assertEqual(prompt["params"]["sessionId"], "s1")
        self.assertEqual(prompt["params"]["prompt"][0]["type"], "text")
        self.assertIn("hi", prompt["params"]["prompt"][0]["text"])
        # session/new advertised no MCP servers and a private cwd.
        new_params = fake.requests[1][1]
        self.assertEqual(new_params["mcpServers"], [])
        self.assertTrue(new_params["cwd"].startswith("/"))

    def test_config_dir_scopes_the_child_env(self):
        fake = self._scripted([
            _update("agent_message_chunk", "hi"),
            _prompt_response(100, "end_turn"),
        ])
        events, fake = _run(fake, {"model": "swe-2", "config_dir": "/tmp/devin-acct",
                                   "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(events[-1]["type"], "message_stop")
        self.assertEqual((fake.env or {}).get("XDG_DATA_HOME"), "/tmp/devin-acct")

    def test_default_turn_sends_no_xdg_override(self):
        fake = self._scripted([
            _update("agent_message_chunk", "hi"),
            _prompt_response(100, "end_turn"),
        ])
        events, fake = _run(fake)
        self.assertEqual(events[-1]["type"], "message_stop")
        self.assertFalse((fake.env or {}).get("XDG_DATA_HOME"))

    def test_host_tool_call_ends_turn_with_tool_call_event(self):
        fake = self._scripted([
            _update("agent_message_chunk", "Let me check."),
            _tool_call_update("call-1", title="host__get_weather",
                              raw_input={"tool_name": "host__get_weather",
                                         "city": "Paris"}),
            # The prompt response never arrives: the turn ends at the call.
        ])
        events, _ = _run(fake, _tools_request())
        kinds = [e["type"] for e in events]
        self.assertEqual(kinds, ["text_delta", "tool_call", "message_stop"])
        call = events[1]
        self.assertEqual(call["id"], "call-1")
        self.assertEqual(call["name"], "get_weather")
        self.assertEqual(call["input"], {"city": "Paris"})
        self.assertEqual(events[-1]["stop_reason"], "tool_use")

    def test_host_tool_call_matches_bare_and_qualified_names(self):
        for qualifier in ("get_weather", "host.get_weather",
                          "host__get_weather", "mcp__host__get_weather"):
            fake = self._scripted([
                _tool_call_update("c", raw_input={"tool_name": qualifier,
                                                  "city": "Oslo"}),
            ])
            events, _ = _run(fake, _tools_request())
            calls = [e for e in events if e["type"] == "tool_call"]
            self.assertEqual(len(calls), 1, qualifier)
            self.assertEqual(calls[0]["name"], "get_weather", qualifier)

    def test_host_tool_call_identity_from_meta_envelope(self):
        # devin 3000.14 reports MCP calls only under _meta: the toolCall update
        # carries cognition.ai/toolName + inferenceToolName, rawInput holds
        # just the arguments, and the title is free text.
        fake = self._scripted([
            _tool_call_update(
                "call-9", title="Calling get_weather from host",
                raw_input={"city": "Paris"},
                meta={"cognition.ai/toolName": "mcp__host__get_weather",
                      "cognition.ai/inferenceToolName": "mcp__host__get_weather",
                      "cognition.ai/eventType": "mcp_tool_call"}),
        ])
        events, _ = _run(fake, _tools_request())
        calls = [e for e in events if e["type"] == "tool_call"]
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["name"], "get_weather")
        self.assertEqual(calls[0]["input"], {"city": "Paris"})
        self.assertEqual(events[-1]["stop_reason"], "tool_use")

    def test_meta_identity_never_matches_devin_native_tools(self):
        fake = self._scripted([
            _tool_call_update(
                "native-2", title="Running ls",
                raw_input={"command": "ls"},
                meta={"cognition.ai/inferenceToolName": "mcp_list_tools",
                      "cognition.ai/eventType": "mcp_tool_call"}),
            _update("agent_message_chunk", "done"),
            _prompt_response(100, "end_turn"),
        ])
        events, _ = _run(fake, _tools_request())
        self.assertNotIn("tool_call", [e["type"] for e in events])
        self.assertEqual(events[-1]["stop_reason"], "end_turn")

    def test_devin_native_tool_call_is_not_a_host_call(self):
        fake = self._scripted([
            _tool_call_update("native-1", title="bash", kind="execute",
                              raw_input={"command": "ls"}),
            _update("agent_message_chunk", "done"),
            _prompt_response(100, "end_turn"),
        ])
        events, _ = _run(fake, _tools_request())
        self.assertNotIn("tool_call", [e["type"] for e in events])
        self.assertEqual(events[-1]["stop_reason"], "end_turn")

    def test_tool_call_update_for_known_id_does_not_duplicate(self):
        fake = self._scripted([
            _tool_call_update("call-1", raw_input={"tool_name": "host__get_weather",
                                                  "city": "Paris"}),
        ])
        events, _ = _run(fake, _tools_request())
        self.assertEqual(len([e for e in events if e["type"] == "tool_call"]), 1)

    def test_host_tool_permission_is_allowed_native_still_denied(self):
        fake = self._scripted([
            {"jsonrpc": "2.0", "id": 557, "method": "session/request_permission",
             "params": {"sessionId": "s1",
                        "toolCall": {"toolCallId": "c1", "kind": "other",
                                     "rawInput": {"tool_name": "host__get_weather",
                                                  "city": "Paris"}},
                        "options": [{"optionId": "allow1", "kind": "allow_once"},
                                    {"optionId": "deny1", "kind": "reject_once"}]}},
            {"jsonrpc": "2.0", "id": 558, "method": "session/request_permission",
             "params": {"sessionId": "s1",
                        "toolCall": {"toolCallId": "c2", "kind": "execute",
                                     "rawInput": {"command": "rm -rf /"}},
                        "options": [{"optionId": "allow2", "kind": "allow_once"},
                                    {"optionId": "deny2", "kind": "reject_once"}]}},
            _prompt_response(100),
        ])
        _run(fake, _tools_request())
        answers = {a["id"]: a for a in
                   (json.loads(s) for s in fake.sent) if "result" in a}
        self.assertEqual(answers[557]["result"]["outcome"]["optionId"], "allow1")
        self.assertEqual(answers[558]["result"]["outcome"]["optionId"], "deny2")

    def test_permission_identity_from_meta_permission_rule(self):
        # devin 3000.14's request_permission toolCall carries only the call id
        # and _meta["cognition.ai/permissionRule"] — the qualified MCP name.
        fake = self._scripted([
            {"jsonrpc": "2.0", "id": 559, "method": "session/request_permission",
             "params": {"sessionId": "s1",
                        "toolCall": {"toolCallId": "c9",
                                     "_meta": {"cognition.ai/permissionRule":
                                               "mcp__host__get_weather"}},
                        "options": [{"optionId": "allow9", "kind": "allow_once"},
                                    {"optionId": "deny9", "kind": "reject_once"}]}},
            _prompt_response(100),
        ])
        _run(fake, _tools_request())
        answers = {a["id"]: a for a in
                   (json.loads(s) for s in fake.sent) if "result" in a}
        self.assertEqual(answers[559]["result"]["outcome"]["optionId"], "allow9")

    def test_tools_attach_list_only_host_mcp_server(self):
        fake = self._scripted([_prompt_response(100)])
        captured = {}
        original_request = fake.request

        def request(method, params=None, timeout=None):
            result = original_request(method, params, timeout)
            if method == "session/new":
                captured["params"] = params
                path = params["mcpServers"][0]["args"][-1]
                with open(path) as handle:
                    captured["manifest"] = json.load(handle)
            return result

        fake.request = request
        _run(fake, _tools_request())
        servers = captured["params"]["mcpServers"]
        self.assertEqual(len(servers), 1)
        server = servers[0]
        self.assertEqual(set(server), {"name", "command", "args", "env"})
        self.assertEqual(server["name"], "host")
        self.assertTrue(server["command"].endswith("python3")
                        or "python" in server["command"])
        self.assertIn("host_tools_mcp.py", " ".join(server["args"]))
        self.assertIn("get_weather", json.dumps(captured["manifest"]))
        # No bridge endpoint: list-only mode must not forward calls.
        self.assertNotIn("bridge", captured["manifest"])

    def test_no_tools_still_sends_empty_mcp_servers(self):
        fake = self._scripted([_prompt_response(100)])
        _run(fake)
        self.assertEqual(fake.requests[1][1]["mcpServers"], [])

    def test_argv_carries_model_and_acp(self):
        fake = self._scripted([_prompt_response(100)])
        original = fake.request

        def request(method, params=None, timeout=None):
            if method == "initialize":
                config = Path(fake.argv[2])
                self.assertEqual(config.parent.parent, Path(fake.env["XDG_CONFIG_HOME"]))
                settings = json.loads(config.read_text())
                self.assertEqual(set(settings["permissions"]["ask"]), {"exec", "read", "grep", "edit", "write"})
                self.assertFalse(any(settings["read_config_from"].values()))
                hook = settings["hooks"]["PreToolUse"][0]["hooks"][0]
                self.assertEqual(shlex.split(hook["command"])[1:4], ["-I", "-B", "-c"])
            return original(method, params, timeout)

        fake.request = request
        _run(fake, {"model": "gpt-5.6-sol",
                    "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(fake.argv[1], "--config")
        self.assertEqual(fake.argv[3:], ["acp", "--process-model", "in-process", "--model", "gpt-5.6-sol"])
        self.assertTrue(fake.argv[0].endswith("devin"))

    def test_native_calls_transfer_only_at_a_permission_or_guard_boundary(self):
        from chat_tools import TOOL_DEFINITIONS
        from cli_lifecycle import TurnTiming

        permission = {"jsonrpc": "2.0", "id": 7, "method": "session/request_permission",
                      "params": {"sessionId": "s1", "toolCall": {"toolCallId": "native"},
                                 "options": [{"kind": "allow_once", "optionId": "one"}]}}
        guard = {"method": "session/update", "params": {"sessionId": "s1", "update": {
            "sessionUpdate": "tool_call_update", "toolCallId": "native", "status": "failed",
            "content": [{"type": "content", "content": {"type": "text",
                         "text": "Tool rejected: " + m._NATIVE_GUARD_REASON}}]}}}
        for name, raw, boundary, expected in (
            ("exec", {"command": "printf fixture"}, permission, ("run_shell", {"command": "printf fixture"})),
            ("read", {"file_path": "fixture.txt", "limit": 2}, permission, ("read_file", {"path": "fixture.txt", "limit": 2})),
            ("grep", {"query": "witness"}, permission, ("search_files", {"pattern": "witness", "path": "."})),
            ("find_file_by_name", {"query": "fixture.txt"}, guard,
             ("search_files", {"pattern": "fixture.txt", "path": ".", "names_only": True})),
            ("edit", {"file_path": "fixture.txt"}, permission, None),
            ("exec", {"command": "printf fixture"}, _prompt_response(100), None),
        ):
            with self.subTest(name=name, boundary=boundary.get("method")):
                announcement = _tool_call_update("native", raw_input=raw, meta={"cognition.ai/inferenceToolName": name})
                fake = self._scripted([announcement, boundary])
                timing = TurnTiming("devin-cli", "swe-2")
                events, _ = _run(fake, {**_tools_request(), "tools": TOOL_DEFINITIONS, "_cli_timing": timing})
                self.assertEqual(timing.values["devin_native_attempts"], 1)
                self.assertFalse(any(json.loads(line).get("id") == 7 for line in fake.sent))
                calls = [(e["name"], e["input"]) for e in events if e["type"] == "tool_call"]
                self.assertEqual(calls, [expected] if expected else [])
                if expected:
                    self.assertEqual(timing.values["devin_native_forwarded"], 1)
                    self.assertEqual(events[-1]["stop_reason"], "tool_use")
                elif name == "edit":
                    self.assertEqual(timing.values["devin_native_redirected"], 1)
                    self.assertIn("The user did not deny it", events[-1]["message"])
                    self.assertIn("mcp__host__apply_patch", events[-1]["message"])

    def test_native_mapping_rebases_temporary_paths_and_rejects_unsupported_options(self):
        from chat_tools import TOOL_DEFINITIONS

        toolset = m.HostToolset(TOOL_DEFINITIONS, prefix="")
        cwd = "/synthetic/devin"
        self.assertEqual(m._translate_native("read", {"file_path": cwd + "/src/a.txt", "offset": 3}, cwd, toolset),
                         ("read_file", {"path": "src/a.txt", "offset": 3}))
        self.assertIsNone(m._translate_native("exec", {"command": "pwd", "workdir": "subfolder"}, cwd, toolset))
        self.assertIsNone(m._translate_native("grep", {"query": "a|b"}, cwd, toolset))
        self.assertIsNone(m._translate_native("read", {"file_path": "a.txt"}, cwd, None))

    def test_unmapped_native_call_retries_with_a_provider_hub_explanation(self):
        from cli_routes import _tool_turn

        redirect = {"type": "error", "code": "devin_native_tool_redirect",
                    "message": "Provider Hub stopped the built-in edit. The user did not deny it. Use the attached MCP tool."}
        delivered = []

        def run(request, *, timeout):
            delivered.append(request)
            if len(delivered) == 1:
                yield redirect
            else:
                yield {"type": "text_delta", "text": "Recovered"}
                yield {"type": "message_stop", "stop_reason": "end_turn"}

        request = _tools_request()
        with mock.patch.object(m, "run_turn", run):
            events = list(_tool_turn(m, request, timeout=30))
        self.assertEqual([e["type"] for e in events if e["type"] != "ping"], ["text_delta", "message_stop"])
        self.assertEqual(delivered[1]["messages"][-1]["content"], redirect["message"])
        self.assertEqual(len(request["messages"]), 1)

    def test_permission_request_is_denied_never_allowed(self):
        fake = self._scripted([
            {"jsonrpc": "2.0", "id": 555, "method": "session/request_permission",
             "params": {"sessionId": "s1", "toolCall": {"kind": "edit"},
                        "options": [{"optionId": "allow1", "kind": "allow_once"},
                                    {"optionId": "deny1", "kind": "reject_once"}]}},
            _prompt_response(100),
        ])
        events, fake = _run(fake)
        self.assertEqual(events[-1]["type"], "message_stop")
        answers = [json.loads(s) for s in fake.sent
                   if '"session/request_permission"' not in s]
        # The last sent frame before/around the prompt is the permission answer.
        permission_answers = [a for a in answers
                              if a.get("id") == 555 and "result" in a]
        self.assertEqual(len(permission_answers), 1)
        outcome = permission_answers[0]["result"]["outcome"]
        self.assertEqual(outcome["outcome"], "selected")
        self.assertEqual(outcome["optionId"], "deny1")

    def test_permission_request_without_reject_option_cancels(self):
        fake = self._scripted([
            {"jsonrpc": "2.0", "id": 556, "method": "session/request_permission",
             "params": {"sessionId": "s1",
                        "options": [{"optionId": "allow1", "kind": "allow_once"}]}},
            _prompt_response(100),
        ])
        _run(fake)
        answers = [json.loads(s) for s in fake.sent if '"id": 556' in s]
        self.assertEqual(answers[0]["result"]["outcome"], {"outcome": "cancelled"})

    def test_unknown_inbound_request_gets_method_not_found(self):
        fake = self._scripted([
            {"jsonrpc": "2.0", "id": 42, "method": "fs/read_text_file",
             "params": {}},
            _prompt_response(100),
        ])
        _run(fake)
        answers = [json.loads(s) for s in fake.sent if '"id": 42' in s]
        self.assertEqual(answers[0]["error"]["code"], -32601)

    def test_prompt_rpc_error_surfaces(self):
        fake = self._scripted([
            _prompt_response(100, error={"code": -32000, "message": "model refused"}),
        ])
        events, _ = _run(fake)
        self.assertEqual(events[-1]["type"], "error")
        self.assertIn("model refused", events[-1]["message"])

    def test_initialize_error_surfaces(self):
        fake = FakeSession([])
        fake.request_script = {"initialize": {"error": {"code": -32600,
                                                         "message": "bad params"}}}
        events, _ = _run(fake)
        self.assertEqual(events[-1]["type"], "error")
        self.assertIn("bad params", events[-1]["message"])
        # The prompt was never sent.
        self.assertFalse(any('"session/prompt"' in s for s in fake.sent))

    def test_missing_session_id_surfaces(self):
        fake = FakeSession([])
        fake.request_script = {
            "initialize": {"result": {}},
            "session/new": {"result": {}},
        }
        events, _ = _run(fake)
        self.assertEqual(events[-1]["type"], "error")
        self.assertIn("sessionId", events[-1]["message"])

    def test_eof_before_prompt_response_is_an_error(self):
        fake = self._scripted([_update("agent_message_chunk", "partial")])
        fake._eof = True
        events, _ = _run(fake)
        self.assertEqual(events[-1]["type"], "error")
        self.assertIn("ended before completing", events[-1]["message"])

    def test_timeout_cancels_and_errors(self):
        fake = self._scripted([])  # never answers the prompt
        events, fake = _run(fake, timeout=0.3)
        self.assertEqual(events[-1]["type"], "error")
        self.assertIn("did not finish", events[-1]["message"])
        cancels = [n for n in fake.notifications if n[0] == "session/cancel"]
        self.assertEqual(len(cancels), 1)
        self.assertEqual(cancels[0][1], {"sessionId": "s1"})

    def test_missing_binary_is_an_error_event(self):
        with mock.patch.object(m, "_resolve_binary", return_value=None):
            events = list(m.run_turn({"model": "swe-2",
                                      "messages": [{"role": "user", "content": "hi"}]}))
        self.assertEqual(events[-1]["type"], "error")
        self.assertIn("devin CLI", events[-1]["message"])

    def test_bad_request_is_an_error_event_not_a_raise(self):
        with mock.patch.object(m, "_resolve_binary", return_value="/fake/devin"):
            events = list(m.run_turn({"model": "swe-2",
                                      "messages": [{"role": "root", "content": "x"}]}))
        self.assertEqual(events[-1]["type"], "error")
        self.assertIn("role", events[-1]["message"])


class PlanTurnIntegrationTests(unittest.TestCase):
    """cli_routes.plan_turn hands client tools to this provider natively."""

    def test_plan_turn_passes_host_tools(self):
        from cli_routes import plan_turn
        payload = {"messages": [{"role": "user", "content": "hi"}],
                   "tools": [{"name": "get_weather", "description": "Fetch.",
                              "input_schema": {"type": "object",
                                               "properties": {"city": {"type": "string"}}}}]}
        plan = plan_turn("devin-cli", "swe-2", payload, {}, wanted_output=64)
        self.assertTrue(plan["cli_tool_calls"])
        self.assertEqual([t["name"] for t in plan["body"]["tools"]], ["get_weather"])
        # Native MCP transport: no text manifest leaks into system or user text.
        self.assertNotIn("<<<tool_call>>>", plan["body"].get("system") or "")
        self.assertNotIn("get_weather", plan["body"].get("system") or "")
        self.assertEqual(plan["compatibility"]["cli_host_tools"], "mcp")

    def test_adapter_registered(self):
        from cli_routes import adapter_for
        module = adapter_for("devin-cli")
        self.assertIs(module, m)
        self.assertEqual(module.PROVIDER_ID, "devin-cli")
        self.assertEqual(module.TRANSPORT, "acp_stdio")
        self.assertEqual(module.SYSTEM_PROMPT_TRANSPORT, "prompt")

    def test_only_chat_passes_a_conversation_scope(self):
        from cli_routes import plan_turn

        for surface in ("chat", "responses", "messages"):
            with self.subTest(surface=surface):
                messages = _tools_request()["messages"]
                payload = {"messages": messages, "tools": _TOOLS, "_provider_hub_surface": surface,
                           "_provider_hub_conversation": "chat:member", "_provider_hub_connection": "account"}
                body = plan_turn("devin-cli", "swe-2", payload, {}, wanted_output=64)["body"]
                self.assertEqual(body.get("conversation"), "chat:member" if surface == "chat" else None)
                self.assertEqual(body.get("history"), messages)


class LiveTurnTests(unittest.TestCase):
    def test_mcp_result_and_follow_up_reuse_one_process(self):
        from cli_host_bridge import HostCallBridge
        from host_tools_mcp import exchange
        from cli_routes import _messages_for_cli, relay_cli_turn, run_turn
        from tempfile import TemporaryDirectory
        from threading import Thread

        with TemporaryDirectory(prefix="phd-test-", dir="/tmp") as workspace:
            fake = FakeSession([])
            bridge = HostCallBridge(workspace, lambda name: name)
            self.addCleanup(bridge.close)
            stderr = mock.Mock()
            state = m._PromptState(stderr, session_id="s1", prompt_id=100)
            live = m.cli_live_session.LiveSession(fake, workspace, bridge, state, interrupt=lambda session: None)
            pool = m.SessionPool(m.cli_live_session.dispose, label="test Devin")
            try:
                call = _tool_call_update("weather", meta={"cognition.ai/inferenceToolName": "mcp__host__get_weather"},
                                         raw_input={"city": "Paris"})
                fake.lines.extend([call])
                replies = []
                request = {"params": {"name": "get_weather", "arguments": {"city": "Paris"}}}
                worker = Thread(target=lambda: replies.append(exchange(request, bridge.endpoint)), daemon=True)
                worker.start()

                history = [{"role": "user", "content": "weather in Paris?"}]

                def round_trip():
                    messages, _ = _messages_for_cli(history, None)
                    request = {**_tools_request(), "messages": messages, "history": history,
                               "conversation": "probe:seat"}
                    output = []
                    turn = run_turn("devin-cli", request, parse_tool_calls=True, timeout=3)
                    result = relay_cli_turn(turn, output.append, model="swe-2")
                    return result, output, turn.timing

                with mock.patch.object(m, "_POOL", pool), mock.patch.object(m, "_spawn_live", return_value=live) as spawn:
                    result, events, timing = round_trip()
                    self.assertEqual(result["stop_reason"], "tool_use")
                    self.assertTrue(timing.delivered)
                    blocks = [e["content_block"] for e in events if e["type"] == "content_block_start"]
                    self.assertEqual(blocks[-1]["id"], "weather")
                    self.assertFalse(replies)
                    history.extend([{"role": "assistant", "content": [{"type": "tool_use", "id": "weather",
                                     "name": "get_weather", "input": {"city": "Paris"}}]},
                                    {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "weather",
                                     "content": "Rain receipt", "is_error": True}]}])
                    fake.lines.extend([_update("agent_message_chunk", "Rain receipt"), _prompt_response(100)])
                    result, _, timing = round_trip()
                    worker.join(1)
                    self.assertEqual(replies, [{"isError": True, "content": [{"type": "text", "text": "Rain receipt"}]}])
                    self.assertEqual(result["stop_reason"], "end_turn")
                    self.assertEqual(timing.values["continuation_kind"], "tool_result")
                    self.assertFalse(fake.sent)
                    history.extend([{"role": "assistant", "content": "Rain receipt"},
                                    {"role": "user", "content": "Repeat the receipt"}])
                    fake._ids = itertools.count(101)
                    fake.lines.extend([_update("agent_message_chunk", "Rain receipt"), _prompt_response(101)])
                    result, _, timing = round_trip()
                    self.assertEqual(result["stop_reason"], "end_turn")
                    self.assertEqual(timing.values["continuation_kind"], "native_session")
                    self.assertEqual(json.loads(fake.sent[0])["params"], {
                        "sessionId": "s1", "prompt": [{"type": "text", "text": "Repeat the receipt"}]})
                    spawn.assert_called_once()
            finally:
                pool.close()


if __name__ == "__main__":
    unittest.main()
