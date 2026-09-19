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
               {"delta": text, "itemId": "i", "threadId": "t1", "turnId": "t1"})


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
        self.assertEqual(methods, ["initialize", "config/read", "thread/start", "turn/start"])
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

    def test_distinct_final_item_is_not_dropped_after_commentary(self):
        fake = FakeCodexSession([])
        fake.script = [_delta_ev("Progress."), _ev("item/completed", {"item": {
            "type": "agentMessage", "id": "final", "text": "Final answer."}}), _completed_turn()]
        events, _, _ = self._run(self._request(), fake)
        self.assertEqual([e["text"] for e in events if e["type"] == "text_delta"],
                         ["Progress.", "Final answer."])

    def test_completed_item_emits_only_missing_suffix_and_deduplicates_snapshot(self):
        fake = FakeCodexSession([])
        complete = _ev("item/completed", {"item": {"type": "agentMessage", "id": "i", "text": "Hello world"}})
        fake.script = [_delta_ev("Hello"), complete, complete, _completed_turn()]
        events, _, _ = self._run(self._request(), fake)
        self.assertEqual([e["text"] for e in events if e["type"] == "text_delta"], ["Hello", " world"])

    def test_reasoning_summary_fallback_stays_separate_from_answer_and_raw_reasoning(self):
        fake = FakeCodexSession([])
        fake.script = [_ev("item/reasoning/summaryTextDelta", {"itemId": "r", "summaryIndex": 0, "delta": "Summary"}),
                       _ev("item/completed", {"item": {"type": "reasoning", "id": "r", "summary": ["Summary done"],
                                                      "content": ["Provider reasoning"]}}), _completed_turn()]
        events, _, _ = self._run(self._request(), fake)
        thoughts = [e for e in events if e["type"] == "thinking_delta"]
        self.assertEqual([e["text"] for e in thoughts], ["Summary", " done", "Provider reasoning"])
        self.assertEqual([e["thinking_kind"] for e in thoughts], ["summary", "summary", "reasoning"])
        self.assertFalse(any(e["type"] == "text_delta" for e in events))

    def test_summary_mode_requests_and_emits_only_published_summaries(self):
        fake = FakeCodexSession([])
        fake.script = [_reasoning_ev("Raw provider reasoning"),
                       _reasoning_ev("Published summary", method="item/reasoning/summaryTextDelta"),
                       _completed_turn()]
        events, session, _ = self._run(self._request(reasoning_summary="concise"), fake)
        self.assertEqual([e["text"] for e in events if e["type"] == "thinking_delta"], ["Published summary"])
        turn = next(params for method, params, _ in session.requests if method == "turn/start")
        self.assertEqual(turn["summary"], "concise")

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

    def test_host_instructions_are_developer_instructions_and_tools_are_registered(self):
        fake = FakeCodexSession([])
        fake.script = [_completed_turn()]
        tool = {"name": "exec_command", "input_schema": {"type": "object"}}
        _, session, _ = self._run(self._request(system="Host policy", tools=[tool]), fake)
        params = dict((method, params) for method, params, _ in session.requests)
        self.assertIn("Host policy", params["thread/start"]["developerInstructions"])
        self.assertNotIn("Host policy", params["turn/start"]["input"][0]["text"])
        namespace = params["thread/start"]["dynamicTools"][0]
        self.assertEqual(namespace["name"], "host")
        self.assertEqual(namespace["tools"][0]["name"], codex._tool_alias("exec_command"))
        self.assertIn("Host tool: exec_command", namespace["tools"][0]["description"])
        self.assertIn('features.shell_tool=false', session.argv)

    def test_reserved_tool_names_round_trip_through_registration_history_and_calls(self):
        name = "mcp__ccd_directory__change_directory"
        alias = codex._tool_alias(name)
        fake = FakeCodexSession([])
        fake.script = [{"id": 9, "method": "item/tool/call", "params": {
            "namespace": "host", "tool": alias, "arguments": {"path": "/tmp"}, "callId": "c2"}}]
        history = [{"role": "assistant", "content": [{"type": "tool_use", "id": "c1", "name": name,
                    "input": {"path": "/previous"}}]},
                   {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "c1", "content": "done"}]}]
        events, session, _ = self._run(self._request(tools=[{"name": name}], history=history,
                                                   tool_choice={"type": "tool", "name": name}), fake)
        params = dict((method, params) for method, params, _ in session.requests)
        spec = params["thread/start"]["dynamicTools"][0]["tools"][0]
        self.assertEqual(spec["name"], alias)
        self.assertFalse(alias.startswith("mcp__"))
        self.assertLessEqual(len(alias), 64)
        self.assertIn(name, spec["description"])
        self.assertIn("host." + alias, params["thread/start"]["developerInstructions"])
        self.assertEqual(params["thread/inject_items"]["items"][0]["name"], alias)
        self.assertEqual(params["thread/inject_items"]["items"][1]["call_id"], "c1")
        self.assertEqual(events, [{"type": "tool_call", "id": "c2", "name": name, "input": {"path": "/tmp"}},
                                  {"type": "message_stop", "stop_reason": "tool_use"}])

    def test_reasoning_survives_the_tool_handoff(self):
        """The turn resumes with its own reasoning, in order, before the call.

        Every host-tool handoff is a fresh process, thread and prompt. Without
        this the model rejoins its own turn with no record of why it called the
        tool and re-derives the plan from the standing instructions each leg -
        observed as dozens of repeated inspection commands before a first edit.
        """
        fake = FakeCodexSession([])
        fake.script = [_completed_turn()]
        history = [
            {"role": "user", "content": "Fix the renderer"},
            {"role": "assistant", "content": [
                {"type": "thinking", "thinking": "Scheduler defers the commit; check the tree first."},
                {"type": "tool_use", "id": "c1", "name": "shell", "input": {"cmd": "git status"}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "c1", "content": "clean"}]},
        ]
        _, session, _ = self._run(self._request(tools=[{"name": "shell"}], history=history), fake)
        params = dict((method, params) for method, params, _ in session.requests)
        items = params["thread/inject_items"]["items"]
        self.assertEqual([item["type"] for item in items],
                         ["message", "reasoning", "function_call", "function_call_output"])
        self.assertEqual(items[1], {"type": "reasoning", "summary": [
            {"type": "summary_text", "text": "Scheduler defers the commit; check the tree first."}]})

    def test_reasoning_items_carry_the_summary_the_runtime_requires(self):
        """``summary`` is mandatory: a content-only item fails the whole turn.

        Probed against the installed runtime - ``{"type": "reasoning",
        "content": [...]}`` is refused with "items[0] is not a valid response
        item: missing field `summary`", which would break the handoff outright.
        """
        items = codex._history_items([
            {"role": "assistant", "content": [{"type": "thinking", "thinking": "why"}]}])
        self.assertEqual(len(items), 1)
        self.assertIn("summary", items[0])
        self.assertNotIn("content", items[0])
        self.assertEqual(items[0]["summary"][0]["type"], "summary_text")

    def test_unusable_reasoning_is_dropped_rather_than_invented(self):
        items = codex._history_items([
            {"role": "assistant", "content": [
                {"type": "redacted_thinking", "data": "opaque"},
                {"type": "thinking", "thinking": "   "},
                {"type": "thinking"}]},
            # A user turn never carries the assistant's reasoning.
            {"role": "user", "content": [{"type": "thinking", "thinking": "not mine"}]}])
        self.assertEqual(items, [])

    def test_signed_reasoning_from_another_provider_is_not_replayed(self):
        """Signed thinking came from a real Anthropic turn, not from this route.

        The bridge never signs the reasoning it emits for a CLI route, so a
        signature means a model switch mid-conversation put another vendor's
        reasoning in the transcript. Forwarding it to OpenAI would replay it as
        Codex's own - the replay ``protocol.py`` already refuses inbound.
        """
        items = codex._history_items([
            {"role": "assistant", "content": [
                {"type": "thinking", "thinking": "Anthropic reasoning", "signature": "sig"},
                {"type": "thinking", "thinking": "this route's own reasoning"},
                {"type": "tool_use", "id": "c1", "name": "shell", "input": {}}]}])
        self.assertEqual([item["type"] for item in items], ["reasoning", "function_call"])
        self.assertEqual(items[0]["summary"][0]["text"], "this route's own reasoning")

    def test_resumed_turn_is_not_prompted_as_a_fresh_task(self):
        fake = FakeCodexSession([])
        fake.script = [_completed_turn()]
        history = [{"role": "assistant", "content": [{"type": "tool_use", "id": "c1",
                                                      "name": "shell", "input": {}}]},
                   {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "c1",
                                                 "content": "clean"}]}]
        _, session, _ = self._run(self._request(tools=[{"name": "shell"}], history=history), fake)
        params = dict((method, params) for method, params, _ in session.requests)
        prompt = params["turn/start"]["input"][0]["text"]
        self.assertEqual(prompt, codex._RESUME_PROMPT)
        self.assertNotIn("Continue the user's task", prompt)
        # The instruction has to name the failure it prevents, not just "continue".
        self.assertIn("Resume the turn already in progress", prompt)
        for forbidden in ("do not repeat a command", "do not restate a plan"):
            self.assertIn(forbidden, prompt)

    def test_tool_aliases_are_stable_and_do_not_collide_with_host_alias_like_names(self):
        name = "mcp__ccd_directory__change_directory"
        alias = codex._tool_alias(name)
        self.assertEqual(alias, codex._tool_alias(name))
        names = [name, alias, "mcp__another__change_directory", "exec_command"]
        self.assertEqual(len(set(map(codex._tool_alias, names))), len(names))
        for order in (names, list(reversed(names))):
            request = self._request(tools=[{"name": item} for item in order])
            payload = codex._normalise_request(request)
            params = codex._thread_params(payload, mock.Mock(cwd="/tmp"))
            self.assertEqual([item["name"] for item in params["dynamicTools"][0]["tools"]],
                             [codex._tool_alias(item) for item in order])

    def test_unoffered_alias_cannot_escape_the_host_allowlist(self):
        fake = FakeCodexSession([])
        fake.script = [{"id": 9, "method": "item/tool/call", "params": {
            "namespace": "host", "tool": codex._tool_alias("mcp__unoffered__write"),
            "arguments": {}, "callId": "c"}}]
        events, _, _ = self._run(self._request(tools=[{"name": "read_file"}]), fake)
        self.assertEqual([item["type"] for item in events], ["error"])

    def test_invalid_runtime_request_is_non_retryable_and_never_starts_a_turn(self):
        for code in (-32600, -32601, -32602):
            fake = FakeCodexSession([])
            fake.responses["thread/start"] = {"error": {"code": code, "message": "invalid tool schema"}}
            events, session, _ = self._run(self._request(), fake)
            self.assertEqual(events[0]["http_status"], 400)
            self.assertEqual([item["type"] for item in events], ["error"])
            self.assertNotIn("turn/start", [method for method, _, _ in session.requests])

    def test_inherited_mcp_servers_are_explicitly_disabled(self):
        fake = FakeCodexSession([])
        fake.responses["config/read"] = {"result": {"config": {"mcp_servers": {
            "local-tools": {"enabled": True, "command": "server"},
            "remote-tools": {"enabled": True, "url": "https://example.test"}}}}}
        fake.script = [_completed_turn()]
        _, session, _ = self._run(self._request(), fake)
        params = next(params for method, params, _ in session.requests if method == "thread/start")
        self.assertEqual(params["config"]["mcp_servers"], {
            "local-tools": {"enabled": False}, "remote-tools": {"enabled": False}})

    def test_native_tools_and_interactive_requests_are_explicit_errors(self):
        for event in [_ev("item/started", {"item": {"type": "commandExecution"}}),
                      _ev("item/completed", {"item": {"type": "fileChange"}}),
                      {"id": 7, "method": "item/commandExecution/requestApproval", "params": {}}]:
            with self.subTest(event=event):
                fake = FakeCodexSession([])
                fake.script = [event, _completed_turn()]
                events, _, _ = self._run(self._request(), fake)
                self.assertEqual([e["type"] for e in events], ["error"])
                self.assertTrue(fake.closed)

    def test_unoffered_dynamic_tool_is_an_error(self):
        fake = FakeCodexSession([])
        fake.script = [{"id": 1, "method": "item/tool/call", "params": {
            "namespace": "host", "tool": "unoffered", "arguments": {}, "callId": "c"}}]
        events, _, _ = self._run(self._request(tools=[{"name": "read_file"}]), fake)
        self.assertEqual([e["type"] for e in events], ["error"])

    def test_deltas_for_other_turns_are_ignored(self):
        fake = FakeCodexSession([])
        foreign = _delta_ev("foreign")
        foreign["params"]["turnId"] = "other"
        fake.script = [foreign, _delta_ev("mine"), _completed_turn()]
        events, _, _ = self._run(self._request(), fake)
        self.assertEqual([e.get("text") for e in events if e["type"] == "text_delta"], ["mine"])


class RunTurnFallbackTests(unittest.TestCase):
    def setUp(self):
        # The fallback's config-only app-server preflight never uses a real
        # runtime in tests; spawner still represents just the exec process.
        self.config_session = FakeCodexSession([])
        patcher = mock.patch.object(codex, "StdioSession", return_value=self.config_session)
        patcher.start()
        self.addCleanup(patcher.stop)

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


class HandoffTelemetryTests(unittest.TestCase):
    """Measurement for the question the fix raises: is it still looping?"""

    def _call(self, name, inp, cid):
        return {"role": "assistant", "content": [{"type": "tool_use", "id": cid,
                                                  "name": name, "input": inp}]}

    def _result(self, cid):
        return {"role": "user", "content": [{"type": "tool_result", "tool_use_id": cid,
                                             "content": "clean"}]}

    def test_identical_repeated_call_is_the_loop_signal(self):
        history = []
        for n in range(4):
            history += [self._call("shell", {"cmd": "git status"}, f"c{n}"), self._result(f"c{n}")]
        stats = codex.handoff_telemetry(history)
        self.assertEqual(stats["calls"], 4)
        self.assertEqual(stats["legs"], 4)
        self.assertEqual(stats["distinct_calls"], 1)
        self.assertEqual(stats["repeats"], 3)
        self.assertEqual(stats["worst_call"], {"name": "shell", "count": 4})

    def test_same_tool_with_different_arguments_is_progress_not_a_repeat(self):
        history = [self._call("shell", {"cmd": "git status"}, "c1"), self._result("c1"),
                   self._call("shell", {"cmd": "sed -i s/a/b/ x"}, "c2"), self._result("c2")]
        stats = codex.handoff_telemetry(history)
        self.assertEqual((stats["repeats"], stats["distinct_calls"]), (0, 2))
        self.assertIsNone(stats["worst_call"])

    def test_argument_key_order_does_not_disguise_a_repeat(self):
        history = [self._call("shell", {"cmd": "ls", "cwd": "/tmp"}, "c1"), self._result("c1"),
                   self._call("shell", {"cwd": "/tmp", "cmd": "ls"}, "c2"), self._result("c2")]
        self.assertEqual(codex.handoff_telemetry(history)["repeats"], 1)

    def test_reasoning_count_distinguishes_a_working_fix_from_an_inert_one(self):
        """0 here means the client never echoed thinking back - the tests cannot see that."""
        history = [{"role": "assistant", "content": [{"type": "thinking", "thinking": "why"}]}]
        self.assertEqual(codex.handoff_telemetry(history, codex._history_items(history))["reasoning"], 1)
        self.assertEqual(codex.handoff_telemetry(history, [])["reasoning"], 0)

    def test_telemetry_tolerates_malformed_history(self):
        for junk in ([], None, ["not a dict"], [{"role": "user"}],
                     [{"role": "user", "content": "plain string"}],
                     [{"role": "assistant", "content": [None, {"type": "tool_use"}]}]):
            self.assertIsInstance(codex.handoff_telemetry(junk), dict)

    def test_record_writes_jsonl_only_when_asked_and_never_leaks_arguments(self):
        history = [self._call("shell", {"cmd": "git status", "token": "s3cr3t"}, "c1"),
                   self._result("c1")]
        stats = codex.handoff_telemetry(history)
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "turns.jsonl")
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop(codex._TURN_LOG_ENV, None)
                codex._record_handoff("gpt-5.6-sol", stats)
                self.assertFalse(os.path.exists(path))
                os.environ[codex._TURN_LOG_ENV] = path
                codex._record_handoff("gpt-5.6-sol", stats)
                codex._record_handoff("gpt-5.6-sol", stats)
            with open(path, encoding="utf-8") as handle:
                lines = handle.read().strip().splitlines()
        self.assertEqual(len(lines), 2)
        record = json.loads(lines[0])
        self.assertEqual(record["model"], "gpt-5.6-sol")
        self.assertEqual(record["calls"], 1)
        # Arguments can carry secrets and this file outlives the turn.
        self.assertNotIn("s3cr3t", lines[0])
        self.assertNotIn("git status", lines[0])

    def test_record_never_fails_a_turn_over_a_bad_sink(self):
        with mock.patch.dict(os.environ, {codex._TURN_LOG_ENV: "/nonexistent-dir/x/turns.jsonl"}):
            codex._record_handoff("gpt-5.6-sol", codex.handoff_telemetry([]))

    def test_run_turn_records_one_line_per_handoff(self):
        fake = FakeCodexSession([])
        fake.script = [_completed_turn()]
        history = [{"role": "assistant", "content": [
                        {"type": "thinking", "thinking": "tree is clean"},
                        {"type": "tool_use", "id": "c1", "name": "shell", "input": {"cmd": "git status"}}]},
                   {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "c1",
                                                 "content": "clean"}]}]
        request = {"model": "gpt-5.6-sol", "messages": [{"role": "user", "content": "hi"}],
                   "tools": [{"name": "shell"}], "history": history}
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "turns.jsonl")
            with mock.patch.dict(os.environ, {codex._TURN_LOG_ENV: path}):
                with mock.patch.object(codex, "StdioSession", side_effect=lambda argv, **kw: fake):
                    list(codex.run_turn(request, spawner="stub"))
            with open(path, encoding="utf-8") as handle:
                record = json.loads(handle.read().strip())
        self.assertEqual(record["calls"], 1)
        self.assertEqual(record["legs"], 1)
        self.assertEqual(record["reasoning"], 1)


if __name__ == "__main__":
    unittest.main()
