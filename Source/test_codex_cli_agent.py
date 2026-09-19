"""Tests for the Codex CLI model route (Source/codex_cli_agent.py).

unittest only (pytest is not part of the uv environment). No real CLI is ever
spawned and no network is touched: read-only probes use injected ``capture``
callables, turns are driven through a scripted fake session injected by
monkeypatching the module's ``StdioSession`` name, and the coarse fallback uses
an injected ``spawner`` returning a pipe-backed fake process.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from unittest import mock

import codex_cli_agent as codex


class _FakeStdin:
    def write(self, data):
        pass

    def flush(self):
        pass

    def close(self):
        pass


class FakeCodexSession:
    """Scripted stand-in for cli_session.StdioSession."""

    def __init__(self, argv, env=None, cwd=None, timeout=None, spawner=None,
                 stderr=None):
        self.argv = list(argv)
        self.env = env
        self.cwd = cwd
        self.timeout = timeout
        self.spawner = spawner
        self.stderr = stderr
        self.returncode = None
        self.process = None
        self.stdin = _FakeStdin()
        self.requests = []
        self.notifications = []
        self.script = []
        self.responses = {
            "initialize": {"result": {}},
            "thread/start": {"result": {"thread": {"id": "t1"}}},
            "turn/start": {"result": {"turn": {"id": "t1"}}},
        }
        self.closed = False

    def request(self, method, params, timeout=None):
        self.requests.append((method, params, timeout))
        return self.responses.get(method, {"result": {}})

    def notify(self, method, params):
        self.notifications.append((method, params))

    def events(self, timeout=None):
        for event in self.script:
            yield event

    def close(self):
        self.closed = True


def _ev(method, params):
    return {"method": method, "params": params}


def _delta_ev(text):
    return _ev("item/agentMessage/delta",
               {"delta": text, "itemId": "i", "threadId": "t", "turnId": "t1"})


def _reasoning_ev(text, method="item/reasoning/textDelta"):
    return _ev(method, {"delta": text, "contentIndex": 0})


def _completed_turn(status="completed", **extra):
    turn = {"id": "t1", "status": status}
    turn.update(extra)
    return _ev("turn/completed", {"turn": turn})


def _cap(rc, out="", err=""):
    return lambda argv, **kwargs: (rc, out, err)


def _boom(argv, **kwargs):
    raise RuntimeError("capture exploded")


class _ExecFake:
    """A pipe-backed process stand-in for the exec --json fallback."""

    def __init__(self, stdout):
        self.stdout = stdout
        self._done = False

    def poll(self):
        return 0 if self._done else None

    def terminate(self):
        self._done = True

    def kill(self):
        self._done = True

    def wait(self, timeout=None):
        self._done = True
        return 0


class PureFunctionTests(unittest.TestCase):
    def test_result_envelope_and_bare_tolerance(self):
        self.assertEqual(codex._result({"result": {"data": []}}, context="x"),
                         {"data": []})
        # Bare payloads (no "result" key) pass straight through.
        self.assertEqual(codex._result({"data": []}, context="x"), {"data": []})
        # A non-dict result unwraps to {} rather than raising.
        self.assertEqual(codex._result({"result": "nope"}, context="x"), {})

    def test_result_error_raising(self):
        with self.assertRaises(codex.CodexCliAgentError) as ctx:
            codex._result({"error": {"message": "boom", "code": 123}},
                          context="model/list")
        self.assertIn("boom", str(ctx.exception))
        self.assertIn("code 123", str(ctx.exception))
        with self.assertRaises(codex.CodexCliAgentError):
            codex._result("garbage", context="x")

    def test_parse_model_list_drops_junk_and_normalises(self):
        rows = [
            {"model": "gpt-5.6-sol", "displayName": "GPT-5.6 Sol",
             "hidden": False, "isDefault": True,
             "supportedReasoningEfforts": [
                 {"reasoningEffort": "low", "description": "least thinking"}],
             "serviceTiers": [{"id": "plus", "name": "Plus"}],
             "inputModalities": ["text", "image"]},
            None, "junk", 42, {"foo": "bar"},
            {"id": "only-id"},
        ]
        parsed = codex.parse_model_list(rows)
        self.assertEqual(len(parsed), 2)
        first = parsed[0]
        self.assertEqual(first["model"], "gpt-5.6-sol")
        self.assertEqual(first["id"], "gpt-5.6-sol")
        self.assertEqual(first["displayName"], "GPT-5.6 Sol")
        self.assertIs(first["hidden"], False)
        self.assertIs(first["isDefault"], True)
        self.assertEqual(first["supportedReasoningEfforts"],
                         [{"reasoningEffort": "low",
                           "description": "least thinking"}])
        self.assertEqual(first["serviceTiers"],
                         [{"id": "plus", "name": "Plus", "description": ""}])
        self.assertEqual(first["inputModalities"], ["text", "image"])
        # id-only rows fall back to the id for model/displayName.
        self.assertEqual(parsed[1]["model"], "only-id")
        self.assertEqual(parsed[1]["id"], "only-id")
        self.assertEqual(parsed[1]["displayName"], "only-id")

    def test_checked_model_and_effort_reject_injection(self):
        self.assertEqual(codex._checked_model("gpt-5.6-sol"), "gpt-5.6-sol")
        for bad in ("", "bad model", 'x"; sandbox_mode="danger-full-access"',
                    "model=foo"):
            with self.assertRaises(codex.CodexCliAgentError):
                codex._checked_model(bad)
        self.assertEqual(codex._checked_effort("high"), "high")
        self.assertIsNone(codex._checked_effort(None))
        self.assertIsNone(codex._checked_effort("   "))
        with self.assertRaises(codex.CodexCliAgentError):
            codex._checked_effort("bad effort")

    def test_thread_params(self):
        home = mock.Mock()
        home.cwd = "/tmp/work"
        params = codex._thread_params({"model": "gpt-5.6-sol", "effort": "high"},
                                      home)
        self.assertEqual(params["cwd"], "/tmp/work")
        self.assertEqual(params["model"], "gpt-5.6-sol")
        self.assertEqual(params["sandbox"], "read-only")
        self.assertEqual(params["approvalPolicy"], "never")
        self.assertIs(params["ephemeral"], True)
        self.assertEqual(params["config"], {"model_reasoning_effort": "high"})
        no_effort = codex._thread_params({"model": "gpt-5.6-sol",
                                          "effort": None}, home)
        self.assertNotIn("config", no_effort)

    def test_turn_params(self):
        params = codex._turn_params(
            {"model": "gpt-5.6-sol", "effort": "high", "prompt": "hi"}, "t1")
        self.assertEqual(params["threadId"], "t1")
        self.assertEqual(params["input"],
                         [{"type": "text", "text": "hi", "text_elements": []}])
        self.assertEqual(params["approvalPolicy"], "never")
        self.assertEqual(params["sandboxPolicy"],
                         {"type": "readOnly", "networkAccess": False})
        self.assertEqual(params["model"], "gpt-5.6-sol")
        self.assertEqual(params["effort"], "high")
        bare = codex._turn_params({"model": None, "effort": None, "prompt": "hi"},
                                  "t1")
        self.assertNotIn("model", bare)
        self.assertNotIn("effort", bare)


class ArgvTests(unittest.TestCase):
    def test_build_argv_signature_parity(self):
        with mock.patch.object(codex, "runtime_binary", return_value="/fake/codex"):
            base = codex.build_argv("gpt-5.6-sol")
            # system and stream are accepted and ignored.
            same = codex.build_argv("gpt-5.6-sol", effort="high",
                                    system="ignored", stream=False)
            self.assertEqual(same, codex.build_argv("gpt-5.6-sol",
                                                    effort="high"))
            self.assertNotIn("ignored", same)
            self.assertIn("app-server", base)
            self.assertIn('sandbox_mode="read-only"', base)
            self.assertIn('approval_policy="never"', base)
            for flag in codex._FORBIDDEN_FLAGS:
                self.assertNotIn(flag, base)

    def test_forbidden_assertion_fires(self):
        for bad in (["codex", "danger-full-access"],
                    ["codex", "--approve-for-me"],
                    ["codex", "workspace-write"],
                    ["codex", "--dangerously-bypass-approvals-and-sandbox"]):
            with self.assertRaises(codex.CodexCliAgentError):
                codex._assert_safe_argv(bad, context="exec")

    def test_app_server_argv_shapes(self):
        with mock.patch.object(codex, "runtime_binary", return_value="/fake/codex"):
            argv = codex._app_server_argv()
            self.assertEqual(argv[0], "/fake/codex")
            self.assertIn("-c", argv)
            self.assertIn('cli_auth_credentials_store="file"', argv)
            self.assertIn('model_provider="openai"', argv)
            self.assertIn('sandbox_mode="read-only"', argv)
            self.assertIn('approval_policy="never"', argv)
            self.assertEqual(argv[-1], "app-server")
            argv2 = codex._app_server_argv(model="gpt-5.6-sol", effort="max")
            self.assertIn('model="gpt-5.6-sol"', argv2)
            self.assertIn('model_reasoning_effort="max"', argv2)

    def test_build_exec_argv_shape(self):
        with mock.patch.object(codex, "runtime_binary", return_value="/fake/codex"):
            argv = codex.build_exec_argv("gpt-5.6-sol", effort="high")
            self.assertEqual(argv[0], "/fake/codex")
            self.assertEqual(argv[1], "exec")
            self.assertIn('cli_auth_credentials_store="file"', argv)
            self.assertIn('model_provider="openai"', argv)
            self.assertIn('approval_policy="never"', argv)
            self.assertIn("-s", argv)
            self.assertIn("read-only", argv)
            self.assertIn("--skip-git-repo-check", argv)
            self.assertIn("--ephemeral", argv)
            self.assertIn("--json", argv)
            self.assertIn("-m", argv)
            self.assertEqual(argv[argv.index("-m") + 1], "gpt-5.6-sol")
            self.assertIn('model_reasoning_effort="high"', argv)


class CodexTurnWorkspaceTests(unittest.TestCase):
    def test_env_drops_keys_and_has_no_codex_home(self):
        workspace = codex.CodexTurnWorkspace(prefix="test-codex-")
        workspace.open()
        try:
            with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "secret"},
                                 clear=False):
                env = workspace.env()
            self.assertNotIn("CODEX_HOME", env)
            self.assertIn("PATH", env)
            self.assertNotIn("OPENAI_API_KEY", env)
        finally:
            workspace.close()

    def test_env_raises_before_open(self):
        workspace = codex.CodexTurnWorkspace(prefix="test-codex-")
        with self.assertRaises(codex.CodexCliAgentError):
            workspace.env()

    def test_close_idempotent_and_rmtree(self):
        workspace = codex.CodexTurnWorkspace(prefix="test-codex-")
        workspace.open()
        base = workspace.base
        self.assertTrue(base.exists())
        workspace.close()
        self.assertFalse(base.exists())
        self.assertIsNone(workspace.base)
        workspace.close()  # idempotent

    def test_context_manager(self):
        workspace = codex.CodexTurnWorkspace(prefix="test-codex-")
        with workspace as entered:
            self.assertTrue(entered.base.exists())
            base = entered.base
        self.assertFalse(base.exists())

    def test_diagnostics_handle_lifecycle(self):
        workspace = codex.CodexTurnWorkspace(prefix="test-codex-", diagnostics=True)
        workspace.open()
        self.assertIsNotNone(workspace.diagnostics_path)
        self.assertIsNone(workspace._diagnostics_handle)
        handle = workspace.diagnostics_path.open("w", encoding="utf-8")
        workspace._diagnostics_handle = handle
        handle.write("diag line\n")
        handle.flush()
        workspace.close()
        self.assertIsNone(workspace._diagnostics_handle)
        self.assertTrue(handle.closed)

    def test_diagnostics_disabled_path_none(self):
        workspace = codex.CodexTurnWorkspace(prefix="test-codex-")
        workspace.open()
        try:
            self.assertIsNone(workspace.diagnostics_path)
        finally:
            workspace.close()

    def test_openai_catalog_override_uses_cache_when_present(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = codex.Path(tmp)
            with mock.patch.object(codex.Path, "home", return_value=base):
                self.assertIsNone(codex._openai_catalog_override())
            cache = base / ".codex" / "models_cache.json"
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text('{"models": [{"slug": "gpt-5.6-sol"}]}',
                             encoding="utf-8")
            with mock.patch.object(codex.Path, "home", return_value=base):
                self.assertEqual(codex._openai_catalog_override(),
                                 ("model_catalog_json", str(cache)))
            # A present-but-empty catalog must not be used: pointing the
            # app-server at it would make startup fail.
            cache.write_text('{"models": []}', encoding="utf-8")
            with mock.patch.object(codex.Path, "home", return_value=base):
                self.assertIsNone(codex._openai_catalog_override())


class DiscoverAuthStateTests(unittest.TestCase):
    def test_discover_installed(self):
        d = codex.discover(capture=_cap(0, "codex-cli 0.153.0\n", ""))
        self.assertEqual(d, {"installed": True, "binary": "codex",
                             "version": "0.153.0"})

    def test_discover_nonzero_rc_is_not_installed(self):
        d = codex.discover(capture=_cap(1, "", "nope"))
        self.assertEqual(d, {"installed": False, "binary": None, "version": None})

    def test_discover_exception_is_not_installed(self):
        d = codex.discover(capture=_boom)
        self.assertEqual(d, {"installed": False, "binary": None, "version": None})

    def test_auth_state_authenticated(self):
        state = codex.auth_state(capture=_cap(0, "You are logged in to codex.", ""))
        self.assertEqual(state["state"], "authenticated")

    def test_auth_state_missing(self):
        state = codex.auth_state(capture=_cap(1, "Not logged in.", ""))
        self.assertEqual(state["state"], "missing")

    def test_auth_state_unparseable(self):
        state = codex.auth_state(capture=_cap(0, "garbage text", ""))
        self.assertEqual(state["state"], "unknown")

    def test_auth_state_empty_output(self):
        state = codex.auth_state(capture=_cap(0, "", ""))
        self.assertEqual(state["state"], "unknown")

    def test_auth_state_probe_exception(self):
        state = codex.auth_state(capture=_boom)
        self.assertEqual(state["state"], "unknown")

    def test_auth_state_no_runtime(self):
        with mock.patch.object(codex, "runtime_binary",
                               side_effect=codex.CodexCliAgentError("nope")):
            state = codex.auth_state()
        self.assertEqual(state["state"], "unknown")
        self.assertIn("No Codex runtime", state["detail"])


class FetchModelsTests(unittest.TestCase):
    class _FetchSession:
        def __init__(self, pages):
            self.pages = list(pages)
            self.list_calls = []
            self.requests = []
            self.closed = False
            self.process = None
            self.returncode = None

        def request(self, method, params, timeout=None):
            self.requests.append((method, params, timeout))
            if method == "initialize":
                return {"result": {}}
            if method == "model/list":
                idx = len(self.list_calls)
                self.list_calls.append(params)
                return self.pages[idx]
            return {"result": {}}

        def notify(self, method, params):
            pass

        def events(self, timeout=None):
            return iter(())

        def close(self):
            self.closed = True

    def test_cursor_paging_shape(self):
        fake = self._FetchSession([
            {"result": {"data": [{"model": "gpt-a"}, {"model": "gpt-b"}],
                        "nextCursor": "1"}},
            {"result": {"data": [{"model": "gpt-c"}], "nextCursor": None}},
        ])
        with mock.patch.object(codex, "StdioSession",
                               side_effect=lambda argv, **kw: fake), \
                mock.patch.object(codex, "runtime_binary",
                                  return_value="/fake/codex"):
            rows = codex.fetch_models(binary="/fake/codex", spawner="stub",
                                      timeout=30)
        self.assertEqual([r["model"] for r in rows],
                         ["gpt-a", "gpt-b", "gpt-c"])
        self.assertEqual(len(fake.list_calls), 2)
        self.assertIsNone(fake.list_calls[0]["cursor"])
        self.assertEqual(fake.list_calls[1]["cursor"], "1")
        self.assertTrue(fake.closed)


class RunTurnTests(unittest.TestCase):
    def _run(self, request, fake, *, spawner="spawner-stub"):
        sessions = []

        def factory(argv, **kwargs):
            fake.argv = list(argv)
            fake.env = kwargs.get("env")
            fake.cwd = kwargs.get("cwd")
            fake.timeout = kwargs.get("timeout")
            fake.spawner = kwargs.get("spawner")
            fake.stderr = kwargs.get("stderr")
            sessions.append(fake)
            return fake

        with mock.patch.object(codex, "StdioSession", side_effect=factory):
            events = list(codex.run_turn(request, spawner=spawner))
        return events, fake, sessions

    def _request(self, **extra):
        request = {"model": "gpt-5.6-sol",
                   "messages": [{"role": "user", "content": "hi"}]}
        request.update(extra)
        return request

    def test_streams_text_delta_and_stop(self):
        fake = FakeCodexSession([])
        fake.script = [_delta_ev("hello"), _completed_turn()]
        events, session, _ = self._run(self._request(), fake)
        self.assertEqual([e["type"] for e in events],
                         ["text_delta", "message_stop"])
        self.assertEqual(events[0]["text"], "hello")
        self.assertEqual(events[1]["stop_reason"], "end_turn")
        self.assertEqual(session.spawner, "spawner-stub")
        methods = [req[0] for req in session.requests]
        self.assertEqual(methods, ["initialize", "thread/start", "turn/start"])
        self.assertIn("initialized", [n[0] for n in session.notifications])

    def test_thinking_delta(self):
        fake = FakeCodexSession([])
        fake.script = [_reasoning_ev("thinking hard"),
                       _reasoning_ev("more", method="item/reasoning/summaryTextDelta"),
                       _completed_turn()]
        events, _, _ = self._run(self._request(), fake)
        self.assertEqual([e["type"] for e in events],
                         ["thinking_delta", "thinking_delta", "message_stop"])
        self.assertEqual(events[0]["text"], "thinking hard")
        self.assertEqual(events[1]["text"], "more")

    def test_item_completed_fallback_without_deltas(self):
        fake = FakeCodexSession([])
        fake.script = [_ev("item/completed",
                           {"item": {"type": "agentMessage",
                                     "text": "full answer"}}),
                       _completed_turn()]
        events, _, _ = self._run(self._request(), fake)
        self.assertEqual([e["type"] for e in events],
                         ["text_delta", "message_stop"])
        self.assertEqual(events[0]["text"], "full answer")

    def test_error_will_retry_ignored_then_completed(self):
        fake = FakeCodexSession([])
        fake.script = [_ev("error", {"willRetry": True,
                                     "error": {"message": "reconnecting"}}),
                       _ev("error", {"error": {"message": "last error"}}),
                       _completed_turn()]
        events, _, _ = self._run(self._request(), fake)
        self.assertEqual([e["type"] for e in events], ["message_stop"])
        self.assertEqual(events[0]["stop_reason"], "end_turn")

    def test_turn_failed_is_error(self):
        fake = FakeCodexSession([])
        fake.script = [_ev("turn/failed",
                           {"turn": {"id": "t1", "status": "failed",
                                     "error": {"message": "kaboom"}}})]
        events, _, _ = self._run(self._request(), fake)
        self.assertEqual([e["type"] for e in events], ["error"])
        self.assertIn("kaboom", events[0]["message"])

    def test_turn_completed_failed_status_is_error(self):
        fake = FakeCodexSession([])
        fake.script = [_completed_turn(status="failed",
                                       error={"message": "kapow"})]
        events, _, _ = self._run(self._request(), fake)
        self.assertEqual([e["type"] for e in events], ["error"])
        self.assertIn("kapow", events[0]["message"])

    def test_interrupted_status_is_error(self):
        fake = FakeCodexSession([])
        fake.script = [_completed_turn(status="interrupted")]
        events, _, _ = self._run(self._request(), fake)
        self.assertEqual([e["type"] for e in events], ["error"])
        self.assertIn("interrupted", events[0]["message"])

    def test_app_server_exit_mid_turn_is_error(self):
        fake = FakeCodexSession([])
        fake.returncode = 3
        events, _, _ = self._run(self._request(), fake)
        self.assertEqual([e["type"] for e in events], ["error"])
        self.assertIn("rc=3", events[0]["message"])

    def test_stream_not_terminating_guard(self):
        fake = FakeCodexSession([])
        with mock.patch.object(codex, "_MAX_STREAM_LOOPS", 3):
            events, _, _ = self._run(self._request(), fake)
        self.assertEqual([e["type"] for e in events], ["error"])
        self.assertIn("iteration budget", events[0]["message"])

    def test_exactly_one_terminal_event(self):
        fake = FakeCodexSession([])
        fake.script = [_delta_ev("a"), _reasoning_ev("t"), _delta_ev("b"),
                       _completed_turn()]
        events, _, _ = self._run(self._request(), fake)
        terminals = [e for e in events if e["type"] in ("message_stop", "error")]
        self.assertEqual(len(terminals), 1)
        self.assertEqual(events[-1]["type"], "message_stop")

    def test_request_validation_never_raises(self):
        for request in ("not a dict", {}, {"model": "gpt-5.6-sol"}):
            events = list(codex.run_turn(request))
            self.assertEqual([e["type"] for e in events], ["error"])


class RunTurnFallbackTests(unittest.TestCase):
    def _spawner_recording(self, captured):
        def spawner(argv, **kwargs):
            captured["argv"] = list(argv)
            captured["kwargs"] = kwargs
            r, w = os.pipe()
            os.write(w, b'{"type": "turn.completed"}\n')
            os.close(w)
            proc = _ExecFake(os.fdopen(r, "r"))
            captured["proc"] = proc
            return proc
        return spawner

    def test_prompt_containing_workspace_write_does_not_trip_scan(self):
        captured = {}
        request = {"model": "gpt-5.6-sol",
                   "messages": [{"role": "user",
                                 "content": "please workspace-write this"}]}
        with mock.patch.object(codex, "runtime_binary",
                               return_value="/fake/codex"):
            events = list(codex.run_turn_fallback(
                request, spawner=self._spawner_recording(captured), timeout=30))
        try:
            self.assertEqual([e["type"] for e in events], ["message_stop"])
            self.assertIn("workspace-write", captured["argv"][-1])
            self.assertNotIn("workspace-write",
                             " ".join(captured["argv"][:-1]))
        finally:
            captured["proc"].stdout.close()

    def test_silent_child_hits_deadline_instead_of_hanging(self):
        captured = {}
        r, w = os.pipe()

        def silent_spawner(argv, **kwargs):
            captured["argv"] = list(argv)
            proc = _ExecFake(os.fdopen(r, "r"))
            captured["proc"] = proc
            return proc

        with mock.patch.object(codex, "runtime_binary",
                               return_value="/fake/codex"), \
                mock.patch.object(codex, "_bounded_timeout", return_value=0.2):
            events = list(codex.run_turn_fallback(
                self._request_fallback(), spawner=silent_spawner, timeout=30))
        try:
            self.assertEqual([e["type"] for e in events], ["error"])
            self.assertIn("time budget", events[0]["message"])
        finally:
            captured["proc"].stdout.close()
            os.close(w)

    def _request_fallback(self):
        return {"model": "gpt-5.6-sol",
                "messages": [{"role": "user", "content": "hi"}]}


if __name__ == "__main__":
    unittest.main()
