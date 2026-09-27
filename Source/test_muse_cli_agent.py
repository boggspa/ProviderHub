"""Tests for the Muse Code CLI model route (Source/muse_cli_agent.py).

unittest only (pytest is not part of the uv environment). No real CLI is ever
spawned and no network is touched: read-only probes are driven by injected
``capture`` callables and turns are driven through a fake ``StdioSession``
injected via ``spawner``/``mock``.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest

from cli_tool_call import TRANSCRIPT_FOOTER
from unittest import mock

import muse_cli_agent as m


class _FakeStdin:
    def write(self, data):
        pass

    def flush(self):
        pass

    def close(self):
        pass


class FakeSession:
    """Minimal stand-in for cli_session.StdioSession."""

    def __init__(self, argv, env=None, timeout=None, spawner=None, stderr=None,
                 cwd=None):
        self.argv = list(argv)
        self.env = env
        self.timeout = timeout
        self.spawner = spawner
        self.stderr = stderr
        self.cwd = cwd
        self.lines = []
        self.returncode = 0
        self.process = None
        self.stdin = _FakeStdin()
        self._events_exc = None
        self.request_script = {}
        self.requests = []
        self.notifications = []
        self._request_exc = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def events(self, timeout=None):
        # Consumes like the real session's queue: run_turn polls events() in
        # short windows, and a replaying fake would feed each line forever.
        if self._events_exc is not None:
            raise self._events_exc
        while self.lines:
            yield self.lines.pop(0)

    def close(self):
        pass

    def send(self, *args, **kwargs):
        pass

    def request(self, method, params=None, timeout=None):
        self.requests.append((method, params))
        if self._request_exc is not None:
            raise self._request_exc
        return self.request_script.get(method, {})

    def notify(self, method, params=None):
        self.notifications.append((method, params))


class _Ev:
    def __init__(self, items):
        self.items = items

    def events(self, timeout=None):
        yield from self.items


def _delta(text):
    return {"payload_type": "run.output.delta",
            "payload": {"kind": "run_output_delta", "text": text}}


def _terminal(terminal, text=None, reason=None):
    return {"payload_type": f"run.terminal.{terminal}",
            "payload": {"kind": "run_terminal", "terminal": terminal,
                        "text": text, "reason": reason}}


def _scoped(event, run_id):
    # Exact exec JSONL shape from the offline Muse 1.3.0 echo provider: the
    # envelope is a session stream; the payload identifies the owning run.
    return {**event, "stream": {"kind": "session", "id": "session"},
            "payload": {**event["payload"], "command_id": run_id,
                        "run_stream": {"kind": "run", "id": run_id}}}


def _linked(run_id):
    return _scoped({"payload_type": "session.run.linked",
                    "payload": {"kind": "session_run_linked"}}, run_id)


class BuildArgvTests(unittest.TestCase):
    def test_structure(self):
        argv = m.build_argv("mistral-medium", effort="high")
        self.assertEqual(argv[0], "muse")
        self.assertEqual(argv[1], "exec")
        self.assertIn("--json", argv)
        for flag in ("--disable-shell", "--disable-write", "--disable-web-tools",
                     "--no-foreign-personal-context", "--no-session-log"):
            self.assertIn(flag, argv)
        self.assertIn("--model", argv)
        self.assertEqual(argv[argv.index("--model") + 1], "mistral-medium")
        self.assertIn("--reasoning-effort", argv)
        self.assertEqual(argv[argv.index("--reasoning-effort") + 1], "high")
        # Safety invariants hold by construction.
        for flag in m._FORBIDDEN_FLAGS:
            self.assertNotIn(flag, argv)
        for flag in m._VARIADIC_TOOL_FLAGS:
            self.assertNotIn(flag, argv)

    def test_effort_optional(self):
        self.assertNotIn("--reasoning-effort", m.build_argv("m", effort=None))

    def test_system_is_not_in_argv(self):
        # SYSTEM_PROMPT_TRANSPORT == "prompt": the system text must never reach
        # argv, or it would leak into the process listing and double-charge.
        argv = m.build_argv("m", system="SECRET SYSTEM TEXT")
        self.assertNotIn("SECRET SYSTEM TEXT", argv)

    def test_assert_safe_rejects_forbidden(self):
        for bad in (["muse", "--yolo"],
                    ["muse", "--disable-approval"],
                    ["muse", "--disable-sandbox"],
                    ["muse", "--approval-mode", "never"],
                    ["muse", "--approval-mode=never"]):
            with self.assertRaises(m.MuseCliAgentError):
                m._assert_safe(bad)
        self.assertEqual(m._assert_safe(["muse", "--disable-shell"]),
                         ["muse", "--disable-shell"])

    def test_validation_is_enforced(self):
        with self.assertRaises(m.MuseCliAgentError):
            m.build_argv("", effort=None)
        with self.assertRaises(m.MuseCliAgentError):
            m.build_argv("mistral", effort="bogus")


class ValidationTests(unittest.TestCase):
    def test_model(self):
        self.assertEqual(m._validate_model("mistral-medium"), "mistral-medium")
        for bad in ("", "   ", "has a space"):
            with self.assertRaises(m.MuseCliAgentError):
                m._validate_model(bad)

    def test_effort_full_ladder_is_identity(self):
        self.assertEqual(m.MUSE_EFFORTS,
                         ["none", "minimal", "low", "medium", "high", "xhigh",
                          "max", "ultra"])
        for rank in m.MUSE_EFFORTS:
            self.assertEqual(m._validate_effort(rank), rank)
        self.assertIsNone(m._validate_effort(None))
        with self.assertRaises(m.MuseCliAgentError):
            m._validate_effort("bogus")
        with self.assertRaises(m.MuseCliAgentError):
            m._validate_effort(123)

    def test_coerce_messages(self):
        self.assertEqual(
            m._coerce_messages([{"role": "user", "content": "hi"}]),
            [{"role": "user", "content": "hi"}],
        )
        with self.assertRaises(m.MuseCliAgentError):
            m._coerce_messages("not a list")
        with self.assertRaises(m.MuseCliAgentError):
            m._coerce_messages([{"role": "system", "content": "x"}])
        with self.assertRaises(m.MuseCliAgentError):
            m._coerce_messages([{"role": "assistant", "content": "x"}])


class RenderPromptTests(unittest.TestCase):
    def test_single_user_passthrough(self):
        self.assertEqual(
            m.render_prompt([{"role": "user", "content": "hi"}]),
            "hi",
        )

    def test_system_and_history_are_framed(self):
        prompt = m.render_prompt(
            [{"role": "user", "content": "hi"},
             {"role": "assistant", "content": "hello"}],
            system="SYS",
        )
        self.assertIn(m._TRANSCRIPT_HEADER, prompt)
        self.assertIn("<system>", prompt)
        self.assertIn("SYS", prompt)
        self.assertIn("hi", prompt)
        self.assertIn(TRANSCRIPT_FOOTER, prompt)


class TranslateTests(unittest.TestCase):
    def test_text_delta(self):
        state = m._TurnState()
        events = m._translate(_delta("hello"), state)
        self.assertEqual(events, [{"type": "text_delta", "text": "hello"}])
        self.assertTrue(state.emitted_text)

    def test_terminal_completed(self):
        state = m._TurnState()
        events = m._translate(_terminal("completed", text="full"), state)
        self.assertEqual(events, [])
        self.assertEqual(state.stop_reason, "completed")
        self.assertEqual(state.result_text, "full")
        self.assertIsNone(state.failure)

    def test_terminal_failed(self):
        state = m._TurnState()
        m._translate(_terminal("failed", reason="kaboom"), state)
        self.assertIsNotNone(state.failure)
        self.assertIn("kaboom", state.failure)

    def test_background_task_failure_is_ignored(self):
        state = m._TurnState()
        payload = {"payload_type": "task.lifecycle.failed",
                   "payload": {"kind": "task_lifecycle",
                               "event": {"kind": "failed",
                                         "reason": "sub-task noise"}}}
        self.assertEqual(m._translate(payload, state), [])
        self.assertIsNone(state.failure)

    def test_bookkeeping_is_ignored(self):
        state = m._TurnState()
        for payload in (
            {"payload_type": "turn.input.user",
             "payload": {"kind": "turn_input_user", "prompt": "x"}},
            {"payload_type": "session.workspace_branch.observed",
             "payload": {"kind": "workspace_branch_observed"}},
        ):
            self.assertEqual(m._translate(payload, state), [])


class IterEventsTests(unittest.TestCase):
    def test_parses_dicts_strings_and_raw(self):
        self.assertEqual(list(m._iter_events(_Ev([{"a": 1}]), timeout=1)),
                         [("json", {"a": 1})])
        self.assertEqual(list(m._iter_events(_Ev(['{"a": 2}']), timeout=1)),
                         [("json", {"a": 2})])
        self.assertEqual(list(m._iter_events(_Ev(["plain diagnostic"]),
                                             timeout=1)),
                         [("raw", "plain diagnostic")])


class DiscoverTests(unittest.TestCase):
    @mock.patch.object(m, "_resolve_binary", return_value="/fake/muse")
    def test_installed_with_version(self, _rb):
        out = "Muse Code 1.3.0 (1.3.0-R3401.1)"
        d = m.discover(capture=lambda argv, **k: (0, out, ""))
        self.assertTrue(d["installed"])
        self.assertEqual(d["binary"], "/fake/muse")
        self.assertEqual(d["version"], "1.3.0")

    @mock.patch.object(m, "_resolve_binary", return_value="/fake/muse")
    def test_installed_but_versionless(self, _rb):
        d = m.discover(capture=lambda argv, **k: (2, "", "bad flag"))
        self.assertTrue(d["installed"])
        self.assertIsNone(d["version"])

    @mock.patch.object(m, "_resolve_binary", return_value=None)
    def test_not_installed(self, _rb):
        d = m.discover()
        self.assertFalse(d["installed"])
        self.assertIsNone(d["binary"])


class AuthStateTests(unittest.TestCase):
    @mock.patch.object(m, "_resolve_binary", return_value="/fake/muse")
    def test_unsupported_without_spawning(self, _rb):
        # muse exposes no read-only auth-status subcommand; this must not spawn.
        self.assertEqual(m.auth_state()["state"], "unsupported")

    @mock.patch.object(m, "_resolve_binary", return_value=None)
    def test_missing(self, _rb):
        self.assertEqual(m.auth_state()["state"], "missing")


class CatalogueTests(unittest.TestCase):
    def _session(self, models, *, init_result=None, error=None, request_exc=None):
        fake = FakeSession([])
        fake.request_script = {
            "initialize": init_result if init_result is not None else {
                "result": {"serverInfo": {"name": "muse", "version": "1.3.0"}}},
            "model/list": {"error": error} if error is not None else {
                "result": {"models": models, "providerId": "meta",
                           "profileId": "tbh", "source": "providerCatalog"}},
        }
        fake._request_exc = request_exc
        return fake

    def _run(self, fake, *, spawner="spawner-stub"):
        sessions = []

        def factory(argv, **kwargs):
            fake.argv = list(argv)
            fake.env = kwargs.get("env")
            fake.timeout = kwargs.get("timeout")
            fake.spawner = kwargs.get("spawner")
            fake.cwd = kwargs.get("cwd")
            sessions.append(fake)
            return fake

        with mock.patch.object(m, "StdioSession", side_effect=factory), \
                mock.patch.object(m, "_resolve_binary", return_value="/fake/muse"):
            rows, notes = m.catalogue(spawner=spawner, timeout=10)
        return rows, notes, fake, sessions

    def test_live_rows_are_normalized(self):
        raw = [
            {"modelId": "muse-spark-1.3", "displayLabel": "muse-spark-1.3",
             "contextLimit": 1007997, "outputLimit": 128000, "description": None,
             "isDefault": False, "isActive": False},
            {"modelId": "muse-spark-1.3-contributor",
             "displayLabel": "muse-spark-1.3-contributor",
             "contextLimit": 1007997, "outputLimit": 128000,
             "description": "Your content, including inter-session messages, "
                             "may be used for product improvement.",
             "isDefault": True, "isActive": False},
        ]
        rows, notes, session, _ = self._run(self._session(raw))
        self.assertEqual(notes, [])
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["id"], "muse-spark-1.3")
        self.assertEqual(rows[0]["display_name"], "muse-spark-1.3")
        self.assertEqual(rows[0]["context"], 1007997)
        self.assertEqual(rows[0]["max_output"], 128000)
        self.assertEqual(rows[0]["reasoning_levels"], m.MUSE_EFFORTS)
        self.assertEqual(rows[0]["default_effort"], "high")
        self.assertIn("Your content", rows[1]["description"])
        # The handshake drove initialize -> initialized -> model/list.
        self.assertEqual([call[0] for call in session.requests],
                         ["initialize", "model/list"])
        self.assertEqual([call[0] for call in session.notifications],
                         ["initialized"])

    def test_empty_models_yields_note(self):
        rows, notes, _, _ = self._run(self._session([]))
        self.assertEqual(rows, [])
        self.assertEqual(len(notes), 1)

    def test_missing_binary_degrades(self):
        with mock.patch.object(m, "_resolve_binary", return_value=None):
            rows, notes = m.catalogue()
        self.assertEqual(rows, [])
        self.assertTrue(notes)

    def test_error_result_degrades(self):
        rows, notes, _, _ = self._run(
            self._session([], error={"code": -32603, "message": "boom"}))
        self.assertEqual(rows, [])
        self.assertTrue(any("boom" in note for note in notes))

    def test_session_break_degrades(self):
        rows, notes, _, _ = self._run(
            self._session([], request_exc=RuntimeError("wedged")))
        self.assertEqual(rows, [])
        self.assertTrue(any("wedged" in note for note in notes))

    def test_malformed_rows_are_dropped(self):
        raw = [
            {"modelId": "muse-spark-1.3", "displayLabel": "muse-spark-1.3"},
            {"displayLabel": "no id"},
            "not a dict",
            {"modelId": "", "displayLabel": "empty id"},
        ]
        rows, notes, _, _ = self._run(self._session(raw))
        self.assertEqual([row["id"] for row in rows], ["muse-spark-1.3"])

    def test_row_normalization_unit(self):
        row = m._row({"modelId": "muse-spark-1.3", "displayLabel": None,
                      "contextLimit": 1007997, "outputLimit": 128000,
                      "description": None})
        self.assertEqual(row["id"], "muse-spark-1.3")
        self.assertEqual(row["display_name"], "muse-spark-1.3")
        self.assertEqual(row["context"], 1007997)
        self.assertEqual(row["max_output"], 128000)
        self.assertIsNone(m._row({"displayLabel": "no id"}))
        self.assertIsNone(m._row("not a dict"))


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
            fake.cwd = kwargs.get("cwd")
            sessions.append(fake)
            return fake

        with mock.patch.object(m, "StdioSession", side_effect=factory), \
                mock.patch.object(m, "_resolve_binary", return_value="/fake/muse"):
            events = list(m.run_turn(request, spawner=spawner))
        return events, fake, sessions

    def test_bad_request_degrades(self):
        with mock.patch.object(m, "_resolve_binary", return_value="/fake/muse"):
            events = list(m.run_turn("not an object"))
        self.assertEqual([e["type"] for e in events], ["error"])

    def test_missing_binary_degrades(self):
        with mock.patch.object(m, "_resolve_binary", return_value=None):
            events = list(m.run_turn(
                {"model": "m", "messages": [{"role": "user", "content": "hi"}]}))
        self.assertEqual([e["type"] for e in events], ["error"])
        self.assertIn("not found", events[0]["message"])

    def test_streams_text_and_stop(self):
        fake = self._stream([json.dumps(_delta("hello")),
                             json.dumps(_terminal("completed", text="hello"))])
        events, session, _ = self._run(
            {"model": "mistral",
             "messages": [{"role": "user", "content": "hi"}]}, fake)
        self.assertEqual([e["type"] for e in events],
                         ["text_delta", "message_stop"])
        self.assertEqual(events[0]["text"], "hello")
        self.assertEqual(events[1]["stop_reason"], "completed")
        # Prompt is never positional and never in argv: it travels by file.
        self.assertIn("--prompt-file", session.argv)
        self.assertNotIn("hi", session.argv)
        idx = session.argv.index("--prompt-file")
        self.assertFalse(os.path.exists(session.argv[idx + 1]))
        self.assertEqual(session.spawner, "spawner-stub")

    def test_workspace_pinned_and_cleaned_up(self):
        # The TMPDIR/workspace invariant: muse derives its tool-output root
        # from TMPDIR and defaults its workspace to cwd, so a turn whose
        # inherited cwd is TMPDIR (or an ancestor of it) fails before any
        # output. run_turn must pin both --workspace and the child cwd to a
        # fresh private directory and remove it when the turn ends.
        fake = self._stream([json.dumps(_terminal("completed", text="ok"))])
        events, session, _ = self._run(
            {"model": "m", "messages": [{"role": "user", "content": "hi"}]},
            fake)
        self.assertEqual([e["type"] for e in events],
                         ["text_delta", "message_stop"])
        self.assertIn("--workspace", session.argv)
        ws = session.argv[session.argv.index("--workspace") + 1]
        self.assertTrue(os.path.isabs(ws))
        self.assertEqual(session.cwd, ws)
        # The workspace is a strict child of TMPDIR, never TMPDIR itself and
        # never an ancestor of TMPDIR, so the TMPDIR-derived tool-output root
        # is a sibling of the workspace rather than a child of it.
        tmp = os.path.realpath(tempfile.gettempdir())
        wsr = os.path.realpath(ws)
        self.assertNotEqual(wsr, tmp)
        self.assertEqual(os.path.commonpath([wsr, tmp]), tmp)
        # And the private workspace directory has been removed after the turn.
        self.assertFalse(os.path.exists(ws))

    def test_workspace_cleaned_up_on_session_break(self):
        fake = self._stream([], events_exc=RuntimeError("boom"))
        events, session, _ = self._run(
            {"model": "m", "messages": [{"role": "user", "content": "hi"}]},
            fake)
        self.assertEqual([e["type"] for e in events], ["error"])
        ws = session.argv[session.argv.index("--workspace") + 1]
        self.assertFalse(os.path.exists(ws))

    def test_terminal_only_falls_back_to_full_text(self):
        fake = self._stream([json.dumps(_terminal("completed", text="full answer"))])
        events, _, _ = self._run(
            {"model": "m", "messages": [{"role": "user", "content": "hi"}]},
            fake)
        self.assertEqual([e["type"] for e in events],
                         ["text_delta", "message_stop"])
        self.assertEqual(events[0]["text"], "full answer")

    def test_failed_terminal_is_error(self):
        fake = self._stream([json.dumps(_terminal("failed", reason="kaboom"))])
        events, _, _ = self._run(
            {"model": "m", "messages": [{"role": "user", "content": "hi"}]},
            fake)
        self.assertEqual([e["type"] for e in events], ["error"])
        self.assertIn("kaboom", events[0]["message"])

    def test_terminal_recovers_missing_suffix_and_host_tool_call(self):
        from cli_tool_call import ToolCallParser

        intro = "I'll find the test files.\n"
        call = ('<<<tool_call>>>\n'
                '{"name":"find_files","input":{"pattern":"test_*.py"}}'
                '\n<<</tool_call>>>')
        # Ephemeral deltas can be incomplete; the terminal's complete text
        # must restore the rest, including a host tool handoff after an intro.
        fake = self._stream([_linked("root"),
                             _scoped(_delta(intro), "root"),
                             _scoped(_terminal("completed", intro + call), "root")])
        events, _, _ = self._run(
            {"model": "m", "messages": [{"role": "user", "content": "hi"}]},
            fake)
        self.assertEqual([e["type"] for e in events],
                         ["text_delta", "text_delta", "message_stop"])
        self.assertEqual("".join(e["text"] for e in events if "text" in e),
                         intro + call)
        parser = ToolCallParser()
        parsed = []
        for event in events:
            if event["type"] == "text_delta":
                parsed.extend(parser.feed(event["text"]))
        parsed.extend(parser.finish())
        calls = [event for kind, event in parsed if kind == "call"]
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["name"], "find_files")

    def test_terminal_does_not_duplicate_multiple_streamed_fragments(self):
        fake = self._stream([_delta("hello"), _delta(" world"),
                             _terminal("completed", text="hello world")])
        events, _, _ = self._run(
            {"model": "m", "messages": [{"role": "user", "content": "hi"}]},
            fake)
        self.assertEqual([e["type"] for e in events],
                         ["text_delta", "text_delta", "message_stop"])
        self.assertEqual("".join(e["text"] for e in events if "text" in e),
                         "hello world")

    def test_terminal_preserves_a_separate_final_after_commentary(self):
        fake = self._stream([_delta("I'll check.\n"),
                             _terminal("completed", text="Here is the answer.")])
        events, _, _ = self._run(
            {"model": "m", "messages": [{"role": "user", "content": "hi"}]},
            fake)
        self.assertEqual([e["type"] for e in events],
                         ["text_delta", "text_delta", "message_stop"])
        self.assertEqual("".join(e["text"] for e in events if "text" in e),
                         "I'll check.\nHere is the answer.")

    def test_terminal_final_message_already_streamed_is_not_replayed(self):
        fake = self._stream([_delta("I'll check.\n"),
                             _delta("Here is the answer."),
                             _terminal("completed", text="Here is the answer.")])
        events, _, _ = self._run(
            {"model": "m", "messages": [{"role": "user", "content": "hi"}]},
            fake)
        self.assertEqual([e["type"] for e in events],
                         ["text_delta", "text_delta", "message_stop"])
        self.assertEqual("".join(e["text"] for e in events if "text" in e),
                         "I'll check.\nHere is the answer.")

    def test_conflicting_terminal_after_host_tool_call_is_error(self):
        first = ('<<<tool_call>>>\n{"name":"first","input":{}}'
                 '\n<<</tool_call>>>')
        second = ('<<<tool_call>>>\n{"name":"second","input":{}}'
                  '\n<<</tool_call>>>')
        fake = self._stream([_delta(first), _terminal("completed", text=second)])
        events, _, _ = self._run(
            {"model": "m", "messages": [{"role": "user", "content": "hi"}]},
            fake)
        self.assertEqual([e["type"] for e in events], ["text_delta", "error"])
        self.assertIn("conflicts", events[-1]["message"])
        self.assertNotIn(second, "".join(e.get("text", "") for e in events))

    def test_empty_completed_terminal_is_error(self):
        fake = self._stream([_terminal("completed")])
        events, _, _ = self._run(
            {"model": "m", "messages": [{"role": "user", "content": "hi"}]},
            fake)
        self.assertEqual([e["type"] for e in events], ["error"])
        self.assertIn("no output", events[0]["message"])

    def test_unrelated_run_cannot_emit_text_or_finish_the_root_run(self):
        for terminal in ("completed", "failed", "cancelled"):
            with self.subTest(terminal=terminal):
                fake = self._stream([
                    _linked("root"),
                    _linked("child"),
                    _scoped(_delta("child text"), "child"),
                    _scoped(_terminal(terminal, "child final"), "child"),
                    _scoped(_delta("root answer"), "root"),
                    _scoped(_terminal("completed", "root answer"), "root"),
                ])
                events, _, _ = self._run(
                    {"model": "m", "messages": [{"role": "user", "content": "hi"}]},
                    fake)
                self.assertEqual(events, [
                    {"type": "text_delta", "text": "root answer"},
                    {"type": "message_stop", "stop_reason": "completed"},
                ])

    def test_unscoped_terminal_cannot_finish_a_scoped_run(self):
        fake = self._stream([
            _linked("root"),
            _terminal("completed", text="unscoped answer"),
            _scoped(_terminal("completed", text="root answer"), "root"),
        ])
        events, _, _ = self._run(
            {"model": "m", "messages": [{"role": "user", "content": "hi"}]},
            fake)
        self.assertEqual(events, [
            {"type": "text_delta", "text": "root answer"},
            {"type": "message_stop", "stop_reason": "completed"},
        ])

    def test_body_terminal_failure_is_not_overridden_by_completed_envelope(self):
        terminal = _terminal("failed", reason="body failure")
        terminal["payload_type"] = "run.terminal.completed"
        fake = self._stream([terminal])
        events, _, _ = self._run(
            {"model": "m", "messages": [{"role": "user", "content": "hi"}]},
            fake)
        self.assertEqual([e["type"] for e in events], ["error"])
        self.assertIn("body failure", events[0]["message"])

    def test_nonzero_exit_without_text_is_error(self):
        fake = self._stream([], returncode=2)
        events, _, _ = self._run(
            {"model": "m", "messages": [{"role": "user", "content": "hi"}]},
            fake)
        self.assertEqual([e["type"] for e in events], ["error"])
        self.assertIn("2", events[0]["message"])

    def test_unparsable_stream_is_an_error(self):
        fake = self._stream(["plain diagnostic"])
        events, _, _ = self._run(
            {"model": "m", "messages": [{"role": "user", "content": "hi"}]},
            fake)
        self.assertEqual([e["type"] for e in events], ["error"])
        self.assertIn("before completing", events[0]["message"])

    def test_session_break_is_error(self):
        fake = self._stream([], events_exc=RuntimeError("boom"))
        events, _, _ = self._run(
            {"model": "m", "messages": [{"role": "user", "content": "hi"}]},
            fake)
        self.assertEqual([e["type"] for e in events], ["error"])
        self.assertIn("boom", events[0]["message"])

    def test_terminal_event_finishes_without_waiting_for_eof(self):
        fake = self._stream([])
        def events(**kwargs):
            yield _terminal("completed", text="done")
            self.fail("Must not wait for the process after its terminal event")
        fake.events = events
        result, _, _ = self._run({"model": "m", "messages": [{"role": "user", "content": "hi"}]}, fake)
        self.assertEqual(result[-1], {"type": "message_stop", "stop_reason": "completed"})


class _Clock:
    """A monotonic clock the fake session advances instead of sleeping."""

    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def _approval_wait(run_id, tool="bash"):
    # Session-log vocabulary of Muse 1.4.0 (approval_wait.effect.*), scoped to
    # the owning run the way the exec stream scopes run.output.delta.
    return {"payload_type": "approval_wait.effect.started",
            "stream": {"kind": "session", "id": "session"},
            "payload": {"kind": "approval_wait_effect", "run_id": run_id,
                        "record": {"kind": "started", "pending_action_id": "pa-1",
                                   "run_stream": {"kind": "run", "id": run_id},
                                   "tool_name": tool}}}


def _approval_done(run_id):
    return {"payload_type": "approval_wait.effect.terminal",
            "stream": {"kind": "session", "id": "session"},
            "payload": {"kind": "approval_wait_effect", "run_id": run_id,
                        "record": {"kind": "terminal", "pending_action_id": "pa-1",
                                   "outcome": {"kind": "denied"}}}}


class QuietSession(FakeSession):
    """A live CLI that prints the scripted records, then nothing at all.

    ``script`` holds one list of records per events() window; once it runs
    out every window is quiet. Each window advances ``clock`` by its full
    length, as a real quiet poll would, and ``_eof`` stays False because the
    child never exits on its own - exactly the race's hung muse runs.
    """

    def __init__(self, clock, script=()):
        super().__init__([])
        self.clock = clock
        self.script = [list(window) for window in script]
        self._eof = False
        self.returncode = None
        self.windows = []

    def events(self, timeout=None):
        self.windows.append(timeout)
        records = self.script.pop(0) if self.script else []
        for record in records:
            yield record
        self.clock.now += float(timeout)


class MuseStallTests(unittest.TestCase):
    """A headless muse run that waits on something nobody will provide.

    Six race turns (26-27 Sep 2026) had muse call its native bash tool, ask
    for a human approval (presentation human_pending) and wait until the
    hub's 1140 s turn limit SIGTERMed it (exit 143). A normal run on the same
    seats never went quiet for more than 105 s and never lasted 240 s.
    """

    REQUEST = {"model": "m", "messages": [{"role": "user", "content": "hi"}]}

    def _run(self, fake, *, timeout=1140):
        def factory(argv, **kwargs):
            fake.argv = list(argv)
            return fake

        with mock.patch.object(m, "StdioSession", side_effect=factory), \
                mock.patch.object(m, "_resolve_binary", return_value="/fake/muse"), \
                mock.patch.object(m, "_monotonic", fake.clock):
            started = fake.clock.now
            events = list(m.run_turn(self.REQUEST, timeout=timeout))
        return events, fake.clock.now - started

    def test_a_silent_cli_is_stopped_long_before_the_turn_limit(self):
        clock = _Clock()
        events, elapsed = self._run(QuietSession(clock, [[_linked("run-1")]]))
        self.assertEqual([e["type"] for e in events], ["error"])
        self.assertIn("printed nothing", events[0]["message"])
        self.assertNotIn("code 143", events[0]["message"])
        self.assertGreaterEqual(elapsed, m.STALL_SECONDS)
        self.assertLess(elapsed, m.STALL_SECONDS + 2 * m.POLL_SECONDS)

    def test_an_unanswered_approval_ends_the_turn_within_the_grace(self):
        clock = _Clock()
        events, elapsed = self._run(QuietSession(clock, [[_linked("run-1"), _approval_wait("run-1")]]))
        self.assertEqual([e["type"] for e in events], ["error"])
        self.assertIn("approval", events[0]["message"])
        self.assertIn("bash", events[0]["message"])
        self.assertLess(elapsed, m.APPROVAL_GRACE_SECONDS + 2 * m.POLL_SECONDS)

    def test_an_approval_that_resolves_is_not_a_failure(self):
        clock = _Clock()
        fake = QuietSession(clock, [[_linked("run-1"), _approval_wait("run-1")],
                                    [_approval_done("run-1")],
                                    # Well past the grace: the denied call's
                                    # model step keeps muse busy but quiet.
                                    [], [], [], [], [], [],
                                    [_scoped(_delta("done"), "run-1"),
                                     _scoped(_terminal("completed", text="done"), "run-1")]])
        events, _ = self._run(fake)
        self.assertEqual([e["type"] for e in events], ["text_delta", "message_stop"])

    def test_another_runs_approval_does_not_stop_this_turn(self):
        # Muse's reminder observer runs as its own run and raises (and at once
        # cancels) an approval for submit_reminder_decision on every turn.
        clock = _Clock()
        fake = QuietSession(clock, [[_linked("run-1"), _approval_wait("reminder", "submit_reminder_decision")],
                                    [], [], [], [], [], [], [],
                                    [_scoped(_terminal("completed", text="ok"), "run-1")]])
        events, _ = self._run(fake)
        self.assertEqual([e["type"] for e in events], ["text_delta", "message_stop"])

    def test_the_turn_limit_is_reported_as_the_hubs_stop(self):
        clock = _Clock()
        busy = [[_linked("run-1")]] + [[{"payload_type": "task.lifecycle.started",
                                          "payload": {"kind": "task_lifecycle"}}]] * 40
        events, elapsed = self._run(QuietSession(clock, busy), timeout=60)
        self.assertEqual([e["type"] for e in events], ["error"])
        self.assertIn("did not finish within 60s", events[0]["message"])
        self.assertNotIn("code 143", events[0]["message"])
        self.assertLessEqual(elapsed, 60 + m.POLL_SECONDS)


if __name__ == "__main__":
    unittest.main()
