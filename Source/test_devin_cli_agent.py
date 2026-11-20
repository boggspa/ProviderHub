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

    def test_argv_carries_model_and_acp(self):
        fake = self._scripted([_prompt_response(100)])
        _run(fake, {"model": "gpt-5.6-sol",
                    "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(fake.argv[1:], ["acp", "--model", "gpt-5.6-sol"])
        self.assertTrue(fake.argv[0].endswith("devin"))

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
    """cli_routes.plan_turn must drop client tools for this provider."""

    def test_plan_turn_suppresses_host_tools(self):
        from cli_routes import plan_turn
        payload = {"messages": [{"role": "user", "content": "hi"}],
                   "tools": [{"name": "get_weather", "description": "Fetch.",
                              "input_schema": {"type": "object",
                                               "properties": {"city": {"type": "string"}}}}]}
        plan = plan_turn("devin-cli", "swe-2", payload, {}, wanted_output=64)
        self.assertFalse(plan["cli_tool_calls"])
        self.assertEqual(plan["body"]["tools"], [])
        # No tool manifest leaked into the system text or the user turn.
        self.assertNotIn("host_note", plan["body"]["messages"][-1]["content"])
        self.assertNotIn("get_weather", plan["body"].get("system") or "")

    def test_adapter_registered(self):
        from cli_routes import adapter_for
        module = adapter_for("devin-cli")
        self.assertIs(module, m)
        self.assertEqual(module.PROVIDER_ID, "devin-cli")
        self.assertEqual(module.TRANSPORT, "acp_stdio")
        self.assertEqual(module.SYSTEM_PROMPT_TRANSPORT, "prompt")


if __name__ == "__main__":
    unittest.main()
