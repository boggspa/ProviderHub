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
        self.sent = []
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

    def send(self, payload):
        self.sent.append(json.loads(payload))

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

    def test_native_image_viewer_is_disabled_on_both_transports(self):
        # A native imageView item aborts the turn (the runtime read the file
        # itself, outside host permissions), so the tool must not be offered.
        # features.view_image is a stable flag on 0.155.1 and 0.158.0-alpha.2.
        with mock.patch.object(codex, "runtime_binary", return_value="/fake/codex"):
            for transport, argv in (("app-server", codex.build_argv("gpt-5.6-sol")),
                                    ("exec", codex.build_exec_argv("gpt-5.6-sol"))):
                with self.subTest(transport=transport):
                    self.assertIn("features.view_image=false", argv)
                    self.assertEqual(argv[argv.index("features.view_image=false") - 1], "-c")

    def test_nested_goals_skill_search_and_skills_catalogue_are_off(self):
        # First-turn context: the host already offers goal tools, and the
        # nested runtime cannot read the SKILL.md files its catalogue lists.
        with mock.patch.object(codex, "runtime_binary", return_value="/fake/codex"):
            for transport, argv in (("app-server", codex.build_argv("gpt-5.6-sol")),
                                    ("exec", codex.build_exec_argv("gpt-5.6-sol"))):
                for setting in ("features.goals=false", "features.skill_search=false",
                                "skills.include_instructions=false"):
                    with self.subTest(transport=transport, setting=setting):
                        self.assertIn(setting, argv)
                        self.assertEqual(argv[argv.index(setting) - 1], "-c")

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
            config = base / ".codex" / "config.toml"
            config.parent.mkdir(parents=True, exist_ok=True)
            config.write_text('model_catalog_json = "/hub/codex-models.json"\n',
                              encoding="utf-8")
            with mock.patch.object(codex.Path, "home", return_value=base):
                self.assertIsNone(codex._openai_catalog_override())
            cache = base / ".codex" / "models_cache.json"
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

    def test_openai_catalog_override_needs_a_configured_catalog(self):
        # The shared cache holds whatever list the backend served the last
        # Codex client to refresh it. With nothing to neutralize, the runtime
        # must list its own rather than inherit another version's.
        with tempfile.TemporaryDirectory() as tmp:
            base = codex.Path(tmp)
            cache = base / ".codex" / "models_cache.json"
            cache.parent.mkdir()
            cache.write_text('{"client_version": "0.153.0", "models": [{"slug": "gpt-6-astra"}]}',
                             encoding="utf-8")
            config = base / ".codex" / "config.toml"
            override = ("model_catalog_json", str(cache))
            for text, expected in (
                    (None, None),
                    ('model = "gpt-6-astra"\n', None),
                    ('[profiles.work]\nmodel_catalog_json = "/work.json"\n', None),
                    ('profile = "work"\n[profiles.work]\nmodel_catalog_json = "/work.json"\n', override),
                    ('model_catalog_json = "/hub/codex-models.json"\n', override),
                    ('model_catalog_json = \n', override)):
                with self.subTest(config=text):
                    if text is None:
                        config.unlink(missing_ok=True)
                    else:
                        config.write_text(text, encoding="utf-8")
                    with mock.patch.object(codex.Path, "home", return_value=base), \
                            mock.patch.object(codex, "runtime_binary", return_value="/fake/codex"):
                        self.assertEqual(codex._openai_catalog_override(), expected)
                        argv = codex._app_server_argv()
                    self.assertEqual(any(part.startswith("model_catalog_json=") for part in argv),
                                     expected is not None)


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
            self.config_response = {"result": {"config": {}}}

        def request(self, method, params, timeout=None):
            self.requests.append((method, params, timeout))
            if method == "initialize":
                return {"result": {}}
            if method == "config/read":
                return self.config_response
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

    def test_native_context_survives_discovery_at_the_larger_maximum(self):
        fake = self._FetchSession([{"result": {"data": [
            {"model": "gpt-budget"}, {"model": "gpt-unknown", "displayName": "gpt-budget"}],
            "nextCursor": None}}])
        with tempfile.TemporaryDirectory() as tmp:
            base = codex.Path(tmp)
            cache = base / ".codex" / "models_cache.json"
            cache.parent.mkdir()
            cache.write_text(json.dumps({"models": [{"slug": "gpt-budget",
                "context_window": 272000, "max_context_window": 872000,
                "effective_context_window_percent": 95}]}))
            with mock.patch.object(codex.Path, "home", return_value=base), \
                    mock.patch.object(codex, "StdioSession", return_value=fake), \
                    mock.patch.object(codex, "runtime_binary", return_value="/fake/codex"):
                rows = codex.fetch_models(binary="/fake/codex", spawner="stub")
        self.assertEqual(rows[0]["context"], 872000)
        self.assertEqual(rows[0]["runtime_context"], 828400)
        self.assertEqual(rows[0]["context_kind"], "runtime_catalogue")
        self.assertEqual(rows[0]["context_evidence"], str(cache))
        self.assertNotIn("context", rows[1])

    def test_native_context_override_and_invalid_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = codex.Path(tmp) / "native-models.json"
            argv = ["codex", "-c", "model_catalog_json=" + json.dumps(str(cache)), "app-server"]
            valid = {"slug": "gpt-budget", "context_window": 272000,
                     "max_context_window": 872000, "effective_context_window_percent": 95}
            cache.write_text(json.dumps({"models": [valid]}))
            self.assertEqual(codex._catalogue_context(argv, 100000)["gpt-budget"]["runtime_context"], 95000)
            self.assertEqual(codex._catalogue_context(argv, True)["gpt-budget"]["runtime_context"], 828400)
            for ceiling in (None, True, 0, 100000, "872000"):
                with self.subTest(max_context_window=ceiling):
                    cache.write_text(json.dumps({"models": [{**valid, "max_context_window": ceiling}]}))
                    self.assertEqual(codex._catalogue_context(argv)["gpt-budget"]["runtime_context"], 258400)
            cache.write_text(json.dumps({"models": [valid]}))
            for field, values in (("context_window", (None, True, 0, -1, "272000")),
                                  ("effective_context_window_percent", (None, True, 0, 101, "95"))):
                for value in values:
                    with self.subTest(field=field, value=value):
                        cache.write_text(json.dumps({"models": [{**valid, field: value}]}))
                        self.assertEqual(codex._catalogue_context(argv), {})
            for text in ("{", "[]", '{"models": null}'):
                cache.write_text(text)
                self.assertEqual(codex._catalogue_context(argv), {})
            cache.unlink()
            self.assertEqual(codex._catalogue_context(argv), {})
            with mock.patch.object(codex.Path, "home", return_value=codex.Path(tmp)):
                self.assertEqual(codex._catalogue_context(["codex", "app-server"]), {})

    def test_turn_argv_runs_with_the_advertised_maximum_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = codex.Path(tmp) / ".codex" / "models_cache.json"
            cache.parent.mkdir()
            cache.write_text(json.dumps({"models": [
                {"slug": "gpt-6-astra", "context_window": 272000, "max_context_window": 872000},
                {"slug": "gpt-5.5", "context_window": 272000, "max_context_window": 272000}]}))
            with mock.patch.object(codex.Path, "home", return_value=codex.Path(tmp)), \
                    mock.patch.object(codex, "_openai_catalog_override", return_value=None), \
                    mock.patch.object(codex, "runtime_binary", return_value="/fake/codex"):
                self.assertIn("model_context_window=872000", codex.build_argv("gpt-6-astra"))
                self.assertIn("model_context_window=872000", codex.build_exec_argv("gpt-6-astra"))
                for argv in (codex.build_argv("gpt-5.5"), codex.build_exec_argv("gpt-5.5")):
                    self.assertFalse(any(arg.startswith("model_context_window=") for arg in argv))

    def test_listing_ignores_a_cache_written_by_another_client(self):
        # Seen live: a 0.153.0 app-server left running across an upgrade kept
        # rewriting the shared cache without gpt-6-sol, which the 0.155.1
        # runtime serves, and launch preparation blocked on the missing route.
        fake = self._FetchSession([{"result": {"data": [
            {"model": "gpt-6-astra"}, {"model": "gpt-6-sol"}], "nextCursor": None}}])
        with tempfile.TemporaryDirectory() as tmp:
            base = codex.Path(tmp)
            cache = base / ".codex" / "models_cache.json"
            cache.parent.mkdir()
            card = {"context_window": 272000, "effective_context_window_percent": 95}
            cache.write_text(json.dumps({"client_version": "0.153.0",
                                         "models": [{"slug": "gpt-6-astra", **card}]}))
            scripted = fake.request

            def refreshing(method, params, timeout=None):
                # Serving model/list re-caches the runtime's own list.
                if method == "model/list":
                    cache.write_text(json.dumps({"client_version": "0.155.1", "models": [
                        {"slug": "gpt-6-astra", **card}, {"slug": "gpt-6-sol", **card}]}))
                return scripted(method, params, timeout)

            fake.request = refreshing
            spawned = []
            with mock.patch.object(codex.Path, "home", return_value=base), \
                    mock.patch.object(codex, "StdioSession",
                                      side_effect=lambda argv, **kw: spawned.append(argv) or fake), \
                    mock.patch.object(codex, "runtime_binary", return_value="/fake/codex"):
                rows = codex.fetch_models(binary="/fake/codex", spawner="stub")
        self.assertFalse(any(part.startswith("model_catalog_json=") for part in spawned[0]))
        self.assertEqual([row["model"] for row in rows], ["gpt-6-astra", "gpt-6-sol"])
        self.assertEqual([row.get("runtime_context") for row in rows], [258400, 258400])

    def test_search_capability_comes_from_the_native_catalogue(self):
        fake = self._FetchSession([{"result": {"data": [{"model": "gpt-searches"}, {"model": "gpt-silent"}],
                                               "nextCursor": None}}])
        with tempfile.TemporaryDirectory() as tmp:
            base = codex.Path(tmp)
            cache = base / ".codex" / "models_cache.json"
            cache.parent.mkdir()
            cache.write_text(json.dumps({"models": [
                {"slug": "gpt-searches", "web_search_tool_type": "text_and_image"},
                {"slug": "gpt-silent"}]}))
            with mock.patch.object(codex.Path, "home", return_value=base), \
                    mock.patch.object(codex, "StdioSession", return_value=fake), \
                    mock.patch.object(codex, "runtime_binary", return_value="/fake/codex"):
                rows = codex.fetch_models(binary="/fake/codex", spawner="stub")
        self.assertIs(rows[0]["web_search"], True)
        self.assertNotIn("web_search", rows[1])

    def test_failed_config_probe_does_not_hide_routable_models(self):
        fake = self._FetchSession([{"result": {"data": [{"model": "gpt-budget"}]}}])
        fake.config_response = {"error": {"message": "config/read unavailable"}}
        with mock.patch.object(codex, "StdioSession", return_value=fake), \
                mock.patch.object(codex, "runtime_binary", return_value="/fake/codex"):
            rows = codex.fetch_models(binary="/fake/codex", spawner="stub")
        self.assertEqual([row["model"] for row in rows], ["gpt-budget"])
        self.assertNotIn("context", rows[0])


class WebSearchRequestTests(unittest.TestCase):
    def test_request_becomes_thread_config_and_stays_cached_when_asked(self):
        self.assertIsNone(codex._checked_search(None))
        self.assertEqual(codex._checked_search({"context_size": "high", "allowed_domains": ["a.dev"], "live": True}),
                         {"web_search": "live", "tools": {"web_search": {"context_size": "high",
                                                                         "allowed_domains": ["a.dev"]}}})
        self.assertEqual(codex._checked_search({"context_size": None, "allowed_domains": [], "live": False}),
                         {"web_search": "cached"})
        for bad in ("yes", {"context_size": "huge"}, {"allowed_domains": "a.dev"}, {"allowed_domains": [""]}):
            with self.subTest(bad=bad), self.assertRaises(codex.CodexCliAgentError):
                codex._checked_search(bad)

    def test_runtime_actions_become_the_responses_spelling(self):
        def done(**item):
            return codex._search_event("item/completed", {"id": "ws", **item})
        self.assertEqual(done(query="q", action={"type": "search", "query": "q", "queries": ["q", "r"]})["action"],
                         {"type": "search", "query": "q", "queries": ["q", "r"]})
        self.assertEqual(done(action={"type": "findInPage", "url": "https://a.dev", "pattern": "x"})["action"],
                         {"type": "find_in_page", "url": "https://a.dev", "pattern": "x"})
        # A missing or newer action still names the query that ran.
        self.assertEqual(done(query="q", action={"type": "imageSearch"})["action"], {"type": "search", "query": "q"})
        self.assertEqual(done(query="", action=None)["action"], {"type": "other"})
        self.assertEqual(codex._search_event("item/started", {"id": "ws"}),
                         {"type": "web_search", "status": "in_progress", "id": "ws"})


class RunTurnTests(unittest.TestCase):
    def setUp(self):
        # build_argv resolves a real runtime before the fake session is ever
        # consulted. Pin it, as RunTurnFallbackTests and the pool tests do:
        # on a machine with neither the Codex CLI nor a desktop app (CI)
        # every turn would otherwise end in "No Codex runtime was found"
        # before any scripted event is read.
        patcher = mock.patch.object(codex, "runtime_binary", return_value="/fake/codex")
        patcher.start()
        self.addCleanup(patcher.stop)

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

    def test_requested_search_runs_on_the_thread_and_streams_as_searches(self):
        # Verified live on 0.155.1: a thread-scoped web_search="live" turns on
        # OpenAI's hosted search although the argv keeps it disabled.
        def item(method, value):
            return _ev(method, {"item": value, "threadId": "t1", "turnId": "t1"})
        fake = FakeCodexSession([])
        fake.script = [
            item("item/started", {"type": "webSearch", "id": "ws1", "query": "", "action": None}),
            item("item/completed", {"type": "webSearch", "id": "ws1", "query": "https://example.com/a",
                                    "action": {"type": "openPage", "url": "https://example.com/a"},
                                    "results": [{"type": "text_result", "url": "https://example.com/a", "title": "A"},
                                                {"type": "text_result", "url": "file:///etc/hosts", "title": "no"}]}),
            _delta_ev("found it"), _completed_turn()]
        search = {"context_size": "low", "allowed_domains": ["example.com"], "live": True}
        events, session, _ = self._run(self._request(web_search=search, effort="high"), fake)
        self.assertEqual([e["type"] for e in events], ["web_search", "web_search", "text_delta", "message_stop"])
        self.assertEqual(events[0], {"type": "web_search", "status": "in_progress", "id": "ws1"})
        self.assertEqual(events[1]["action"], {"type": "open_page", "url": "https://example.com/a"})
        self.assertEqual(events[1]["results"], [{"url": "https://example.com/a", "title": "A"}])
        thread = next(params for method, params, _ in session.requests if method == "thread/start")
        self.assertEqual(thread["config"]["web_search"], "live")
        self.assertEqual(thread["config"]["tools"],
                         {"web_search": {"context_size": "low", "allowed_domains": ["example.com"]}})
        self.assertEqual(thread["config"]["model_reasoning_effort"], "high")
        self.assertIn('web_search="disabled"', session.argv)

    def test_unrequested_search_is_still_refused_as_native_tool_activity(self):
        fake = FakeCodexSession([])
        fake.script = [_ev("item/started", {"item": {"type": "webSearch", "id": "ws1", "query": ""},
                                            "threadId": "t1", "turnId": "t1"}), _completed_turn()]
        events, session, _ = self._run(self._request(), fake)
        self.assertEqual(events[-1]["type"], "error")
        self.assertIn("webSearch", events[-1]["message"])
        thread = next(params for method, params, _ in session.requests if method == "thread/start")
        self.assertNotIn("web_search", thread.get("config") or {})

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

    def test_usage_uses_latest_request_and_splits_cached_input(self):
        def usage(thread="t1", turn="t1", **counts):
            return _ev("thread/tokenUsage/updated", {"threadId": thread, "turnId": turn,
                "tokenUsage": {"total": {"inputTokens": 9000000, "outputTokens": 500000},
                               "last": counts, "modelContextWindow": 258400}})
        fake = FakeCodexSession([])
        fake.script = [_delta_ev("hello"),
            usage(inputTokens=10000, cachedInputTokens=8000, outputTokens=100),
            usage(thread="another-thread", inputTokens=999, outputTokens=999),
            usage(turn="another-turn", inputTokens=999, outputTokens=999),
            usage(inputTokens=True, outputTokens=10),
            usage(inputTokens=100, cachedInputTokens=101, outputTokens=10),
            usage(inputTokens=8000, cachedInputTokens=6000, outputTokens=50),
            _completed_turn()]
        events, _, _ = self._run(self._request(), fake)
        self.assertEqual([event["usage"] for event in events if event["type"] == "usage"], [
            {"input_tokens": 2000, "cache_read_input_tokens": 8000, "output_tokens": 100},
            {"input_tokens": 2000, "cache_read_input_tokens": 6000, "output_tokens": 50}])
        self.assertEqual(events[-1]["type"], "message_stop")

    def test_missing_or_invalid_usage_is_unknown_but_real_zero_is_preserved(self):
        for last in (None, {}, {"inputTokens": -1, "outputTokens": 1},
                     {"inputTokens": 1, "outputTokens": "2"},
                     {"inputTokens": 1, "outputTokens": 1, "cachedInputTokens": False}):
            with self.subTest(last=last):
                self.assertIsNone(codex._usage_snapshot({"tokenUsage": {"last": last}}))
        self.assertEqual(codex._usage_snapshot({"tokenUsage": {"last": {
            "inputTokens": 0, "outputTokens": 0}}})["usage"],
            {"input_tokens": 0, "cache_read_input_tokens": 0, "output_tokens": 0})

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
        for control in ('agents.enabled=false', 'features.multi_agent=false', 'features.multi_agent_v2=false'):
            self.assertIn(control, session.argv)

    def test_cross_provider_spawn_uses_host_alias_and_preserves_arguments(self):
        name = "spawn_agent"
        arguments = {"task_name": "review", "message": "Review the patch.",
                     "model": "gemini/gemini-3.8-flash", "fork_turns": "none", "reasoning_effort": "high"}
        fake = FakeCodexSession([])
        fake.script = [{"id": 9, "method": "item/tool/call", "params": {
            "namespace": "host", "tool": codex._tool_alias(name), "arguments": arguments, "callId": "spawn1"}}]
        events, session, _ = self._run(self._request(tools=[{
            "name": name, "description": "Host delegation catalogue", "input_schema": {"type": "object"}}]), fake)
        self.assertEqual(events, [{"type": "tool_call", "id": "spawn1", "name": name, "input": arguments},
                                  {"type": "message_stop", "stop_reason": "tool_use"}])
        params = dict((method, params) for method, params, _ in session.requests)
        dynamic = params["thread/start"]["dynamicTools"][0]["tools"][0]
        self.assertIn("Host delegation catalogue", dynamic["description"])

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
                         ["message", "message", "function_call", "function_call_output"])
        self.assertEqual(items[1], {"type": "message", "role": "assistant",
            "phase": "commentary", "content": [{"type": "output_text",
            "text": "[Prior reasoning context]\nScheduler defers the commit; check the tree first."}]})

    def test_plain_thinking_never_becomes_an_unresolvable_reasoning_item(self):
        items = codex._history_items([
            {"role": "assistant", "content": [{"type": "thinking", "thinking": "why"}]}])
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["type"], "message")
        self.assertEqual(items[0]["phase"], "commentary")
        self.assertNotIn("id", items[0])
        self.assertNotIn("encrypted_content", items[0])
        self.assertIn("why", items[0]["content"][0]["text"])

    def test_assistant_phases_survive_and_legacy_tool_preambles_are_commentary(self):
        items = codex._history_items([
            {"role": "assistant", "content": [
                {"type": "text", "text": "Checking"},
                {"type": "tool_use", "id": "c1", "name": "shell", "input": {}}]},
            {"role": "assistant", "phase": "commentary", "content": "Still working"},
            {"role": "assistant", "content": [
                {"type": "text", "text": "Progress", "phase": "commentary"},
                {"type": "text", "text": "Answer", "phase": "final_answer"}]},
            {"role": "assistant", "content": "Legacy answer"}])
        messages = [item for item in items if item["type"] == "message"]
        self.assertEqual([item["phase"] for item in messages],
                         ["commentary", "commentary", "commentary", "final_answer", "final_answer"])

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
        self.assertEqual([item["type"] for item in items], ["message", "function_call"])
        self.assertEqual(items[0]["content"][0]["text"],
                         "[Prior reasoning context]\nthis route's own reasoning")

    def test_long_screenshot_history_keeps_the_newest_images_instead_of_failing(self):
        # A seat that views a frame every few steps passes the CLI image budget.
        # The route drops the oldest pixels, keeps every turn's text and tool
        # identity, marks each elision, and the turn still runs.
        import cli_routes
        from cli_images import MAX_IMAGES
        pixel = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")
        image = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": pixel}}
        messages = [{"role": "user", "content": "Play on."}]
        total = MAX_IMAGES + 5
        for index in range(total):
            messages.append({"role": "assistant", "content": [
                {"type": "tool_use", "id": f"s{index}", "name": "view_image", "input": {}}]})
            messages.append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": f"s{index}",
                                                          "content": [{"type": "text", "text": f"frame {index}"}, image]}]})
        body = cli_routes.plan_turn("codex", "gpt-6-sol", {"messages": messages, "tools": [
            {"name": "view_image", "input_schema": {"type": "object"}}]}, {}, wanted_output=64)["body"]
        fake = FakeCodexSession([])
        fake.script = [_completed_turn()]
        events, session, _ = self._run(body, fake)
        self.assertEqual(events[-1]["type"], "message_stop")
        items = next(params for method, params, _ in session.requests if method == "thread/inject_items")["items"]
        outputs = [item["output"] for item in items if item["type"] == "function_call_output"]
        self.assertEqual(len(outputs), total)
        with_image = [index for index, output in enumerate(outputs)
                      if any(part.get("type") == "input_image" for part in output)]
        self.assertEqual(with_image, list(range(5, total)))
        for output in outputs[:5]:
            self.assertTrue(any("omitted this earlier image" in part.get("text", "") for part in output))
        self.assertTrue(all(any(f"frame {index}" == part.get("text") for part in output)
                            for index, output in enumerate(outputs)))

    def test_rejoin_prompt_names_a_tool_result_only_when_one_is_last(self):
        call = {"role": "assistant", "content": [{"type": "tool_use", "id": "c1", "name": "shell", "input": {}}]}
        result = {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "c1", "content": "clean"}]}
        goal = {"role": "user", "content": '<codex_internal_context source="goal"> Continue working toward '
                                           "the active thread goal. </codex_internal_context>"}
        cases = [("tool result", [call, result], codex._RESUME_PROMPT),
                 ("user message", [call, result, {"role": "user", "content": "Now heal at the Pokemon Center."}],
                  codex._NEW_INPUT_PROMPT),
                 ("goal continuation", [call, result, {"role": "assistant", "content": "Paused at the gym."}, goal],
                  codex._NEW_INPUT_PROMPT),
                 ("steer beside a result", [call, {"role": "user", "content": [
                     result["content"][0], {"type": "text", "text": "Use the stairs instead."}]}],
                  codex._NEW_INPUT_PROMPT),
                 ("trailing empty text", [call, {"role": "user", "content": [
                     result["content"][0], {"type": "text", "text": "  "}]}], codex._RESUME_PROMPT)]
        for label, history, expected in cases:
            with self.subTest(label):
                fake = FakeCodexSession([])
                fake.script = [_completed_turn()]
                _, session, _ = self._run(self._request(tools=[{"name": "shell"}], history=history), fake)
                prompt = next(params for method, params, _ in session.requests
                              if method == "turn/start")["input"][0]["text"]
                self.assertEqual(prompt, expected)
        self.assertIn("new instruction has arrived", codex._NEW_INPUT_PROMPT)
        self.assertNotIn("tool call", codex._NEW_INPUT_PROMPT)

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
        # imageView stays fail-closed if a runtime ever ignores
        # features.view_image: the read already bypassed host permissions.
        for event in [_ev("item/started", {"item": {"type": "commandExecution"}}),
                      _ev("item/completed", {"item": {"type": "fileChange"}}),
                      _ev("item/started", {"item": {"type": "imageView", "id": "iv", "path": "frame.png"}}),
                      _ev("item/completed", {"item": {"type": "imageGeneration", "id": "ig"}}),
                      {"id": 7, "method": "item/commandExecution/requestApproval", "params": {}}]:
            with self.subTest(event=event):
                fake = FakeCodexSession([])
                fake.script = [event, _completed_turn()]
                events, _, _ = self._run(self._request(), fake)
                self.assertEqual([e["type"] for e in events], ["error"])
                self.assertTrue(fake.closed)

    def test_native_image_generation_streams_and_keeps_the_turn_alive(self):
        fake = FakeCodexSession([])
        fake.script = [
            _ev("item/started", {"item": {"type": "imageGeneration", "id": "ig1", "status": "in_progress", "result": ""}}),
            _ev("item/completed", {"item": {"type": "imageGeneration", "id": "ig1", "status": "completed",
                                          "result": "PNG-RESULT", "revisedPrompt": "A red bird"}}),
            _delta_ev("Here is the image."), _completed_turn()]
        events, session, _ = self._run(self._request(native_image_generation=True), fake)
        self.assertEqual([event["type"] for event in events],
                         ["image_generation", "image_generation", "text_delta", "message_stop"])
        self.assertEqual(events[1]["result"], "PNG-RESULT")
        self.assertEqual(events[1]["revised_prompt"], "A red bird")
        self.assertEqual(events[-1]["stop_reason"], "end_turn")
        thread = next(params for method, params, _ in session.requests if method == "thread/start")
        self.assertTrue(thread["config"]["features"]["image_generation"])
        self.assertIn("features.image_generation=false", session.argv)
        self.assertIn("features.shell_tool=false", session.argv)

    def test_failed_native_image_can_recover_with_a_host_tool_in_the_same_turn(self):
        tool = {"name": "imagegen", "input_schema": {"type": "object"}}
        fake = FakeCodexSession([])
        fake.script = [
            _ev("item/completed", {"item": {"type": "imageGeneration", "id": "ig1", "status": "failed", "result": "",
                                          "failure": {"type": "usageLimitExceeded", "limitId": "images"}}}),
            {"id": 7, "method": "item/tool/call", "params": {"namespace": "host", "callId": "host-image",
                "tool": codex._tool_alias("imagegen"), "arguments": {"prompt": "A red bird"}}}]
        events, _, _ = self._run(self._request(native_image_generation=True, tools=[tool]), fake)
        self.assertEqual([event["type"] for event in events], ["image_generation", "tool_call", "message_stop"])
        self.assertEqual(events[0]["status"], "failed")
        self.assertEqual(events[1]["name"], "imagegen")
        self.assertEqual(events[-1]["stop_reason"], "tool_use")

    def test_native_image_generation_respects_tool_choice_none(self):
        fake = FakeCodexSession([])
        fake.script = [_completed_turn()]
        _, session, _ = self._run(self._request(native_image_generation=True, tool_choice={"type": "none"}), fake)
        thread = next(params for method, params, _ in session.requests if method == "thread/start")
        self.assertFalse(thread.get("config", {}).get("features", {}).get("image_generation", False))

    def test_native_image_override_preserves_the_entire_transport_feature_policy(self):
        # The installed runtime replaces this table, losing argv features.*
        # values omitted here. Merely checking the argv or image flag missed
        # the shell re-enablement behind commandExecution terminal errors.
        import tomllib
        argv_features = {name: value for setting in codex._TRANSPORT_CONFIG
                         for name, value in tomllib.loads(setting).get("features", {}).items()}
        params = codex._thread_params({"model": "gpt-6-luna", "effort": "low", "tools": [],
                                      "native_image_generation": True}, type("Workspace", (), {"cwd": "/tmp/probe"})())
        self.assertEqual(params["config"]["features"], {**argv_features, "image_generation": True})
        for name in ("shell_tool", "multi_agent", "multi_agent_v2", "apps", "plugins", "hooks",
                     "view_image", "goals", "skill_search"):
            self.assertFalse(params["config"]["features"][name], name)
        self.assertEqual(params["sandbox"], "read-only")
        self.assertEqual(params["approvalPolicy"], "never")

    def test_new_transport_restrictions_are_carried_into_image_threads(self):
        from types import SimpleNamespace
        with mock.patch.object(codex, "_TRANSPORT_CONFIG", (*codex._TRANSPORT_CONFIG,
                                                         "features.future_native_tool=false")):
            params = codex._thread_params({"model": "gpt-6-luna", "effort": None, "tools": [],
                                          "native_image_generation": True}, SimpleNamespace(cwd="/tmp/probe"))
        self.assertFalse(params["config"]["features"]["future_native_tool"])

    def test_native_image_completion_normalizes_runtime_status_without_hiding_failure(self):
        for status, result, failure, expected in [
                ("succeeded", "PNG", None, "completed"),
                ("completed", "PNG", {"type": "usageLimitExceeded"}, "failed"),
                ("cancelled", "PARTIAL", None, "failed"),
                ("incomplete", "PARTIAL", None, "incomplete"),
                ("completed", "", None, "failed")]:
            with self.subTest(status=status, failure=failure):
                fake = FakeCodexSession([])
                fake.script = [_ev("item/completed", {"item": {"type": "imageGeneration", "id": "ig1",
                    "status": status, "result": result, "failure": failure}}), _completed_turn()]
                events, _, _ = self._run(self._request(native_image_generation=True), fake)
                self.assertEqual(events[0]["status"], expected)
                self.assertEqual(events[-1]["type"], "message_stop")

    def test_invalid_host_call_is_returned_to_model_for_correction_without_reconnecting(self):
        tool = {"name": "imagegen", "input_schema": {"type": "object"}}
        for name, arguments in [("imagegen", {}), (codex._tool_alias("imagegen"), "not an object")]:
            with self.subTest(name=name):
                fake = FakeCodexSession([])
                fake.script = [
                    {"id": 6, "method": "item/tool/call", "params": {"namespace": "host", "callId": "bad",
                        "tool": name, "arguments": arguments}},
                    {"id": 7, "method": "item/tool/call", "params": {"namespace": "host", "callId": "fixed",
                        "tool": codex._tool_alias("imagegen"), "arguments": {"prompt": "A red bird"}}}]
                events, session, _ = self._run(self._request(tools=[tool]), fake)
                self.assertEqual([event["type"] for event in events], ["tool_call", "message_stop"])
                self.assertEqual(events[0]["id"], "fixed")
                self.assertEqual(len(session.sent), 1)
                self.assertEqual(session.sent[0]["id"], 6)
                self.assertFalse(session.sent[0]["result"]["success"])
                self.assertIn("No tool was executed", session.sent[0]["result"]["contentItems"][0]["text"])

    def test_invalid_host_call_recovery_is_bounded(self):
        fake = FakeCodexSession([])
        fake.script = [{"id": number, "method": "item/tool/call", "params": {
            "namespace": "host", "callId": "bad", "tool": "unknown", "arguments": {}}}
            for number in range(10)]
        events, session, _ = self._run(self._request(), fake)
        self.assertEqual(len(session.sent), 3)
        self.assertEqual([event["type"] for event in events], ["error"])
        self.assertEqual(events[0]["http_status"], 400)

    def test_image_viewing_is_steered_to_the_host_view_image_alias(self):
        # The nested runtime has no image viewer of its own, so a model that
        # wants to look at a downloaded frame.png must reach the host's tool.
        viewer = {"name": "view_image", "description": "View a local image.",
                  "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}}}
        other = {"name": "exec_command", "input_schema": {"type": "object"}}
        cases = [({"tools": [other, viewer]}, True),
                 ({"tools": [other]}, False),
                 ({"tools": [other, viewer], "tool_choice": {"type": "none"}}, False)]
        for extra, expected in cases:
            with self.subTest(extra=extra):
                fake = FakeCodexSession([])
                fake.script = [_completed_turn()]
                _, session, _ = self._run(self._request(**extra), fake)
                self.assertIn("features.view_image=false", session.argv)
                thread = next(params for method, params, _ in session.requests if method == "thread/start")
                hint = f"call host.{codex._tool_alias('view_image')} (host tool view_image)"
                if expected:
                    self.assertIn(hint, thread["developerInstructions"])
                else:
                    self.assertNotIn(codex._tool_alias("view_image"), thread["developerInstructions"])
                    self.assertNotIn("no native image viewer", thread["developerInstructions"])

    def test_goal_tools_are_named_to_the_model_by_their_host_aliases(self):
        # Goals keep an autonomous seat working. The nested runtime's own goal
        # tools are off (its thread is ephemeral), so a Codex model that wants
        # create_goal must be told which host alias it is.
        goals = [{"name": name, "input_schema": {"type": "object"}}
                 for name in ("create_goal", "update_goal", "get_goal")]
        other = {"name": "exec_command", "input_schema": {"type": "object"}}
        cases = [({"tools": [other, *goals]}, ("create_goal", "update_goal", "get_goal")),
                 ({"tools": [other, goals[1]]}, ("update_goal",)),
                 ({"tools": [other]}, ()),
                 ({"tools": [other, *goals], "tool_choice": {"type": "none"}}, ())]
        for extra, named in cases:
            with self.subTest(extra=[tool["name"] for tool in extra["tools"]], choice=extra.get("tool_choice")):
                fake = FakeCodexSession([])
                fake.script = [_completed_turn()]
                _, session, _ = self._run(self._request(**extra), fake)
                thread = next(params for method, params, _ in session.requests if method == "thread/start")
                instructions = thread["developerInstructions"]
                for name in ("create_goal", "update_goal", "get_goal"):
                    hint = f"call host.{codex._tool_alias(name)} for {name}"
                    if name in named:
                        self.assertIn(hint, instructions)
                    else:
                        self.assertNotIn(hint, instructions)
                self.assertEqual("Thread goals are host tools here" in instructions, bool(named))

    def test_host_goal_call_reaches_the_desktop_under_its_own_name(self):
        tool = {"name": "create_goal", "input_schema": {"type": "object"}}
        fake = FakeCodexSession([])
        fake.script = [{"id": 5, "method": "item/tool/call", "params": {
            "namespace": "host", "tool": codex._tool_alias("create_goal"),
            "arguments": {"objective": "Beat the Violet City gym"}, "callId": "g1"}}]
        events, session, _ = self._run(self._request(tools=[tool]), fake)
        self.assertEqual(events, [{"type": "tool_call", "id": "g1", "name": "create_goal",
                                   "input": {"objective": "Beat the Violet City gym"}},
                                  {"type": "message_stop", "stop_reason": "tool_use"}])
        self.assertIn("features.goals=false", session.argv)
        thread = next(params for method, params, _ in session.requests if method == "thread/start")
        self.assertIs(thread["ephemeral"], True)

    def test_host_view_image_call_carries_the_picture_back_to_the_model(self):
        # The host alias is the working path: its call is handed to the host
        # and its image result returns as a native function_call_output image.
        png = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")
        viewer = {"name": "view_image", "input_schema": {"type": "object"}}
        fake = FakeCodexSession([])
        fake.script = [{"id": 4, "method": "item/tool/call", "params": {
            "namespace": "host", "tool": codex._tool_alias("view_image"),
            "arguments": {"path": "frame.png"}, "callId": "v1"}}]
        events, _, _ = self._run(self._request(tools=[viewer]), fake)
        self.assertEqual(events, [{"type": "tool_call", "id": "v1", "name": "view_image",
                                   "input": {"path": "frame.png"}},
                                  {"type": "message_stop", "stop_reason": "tool_use"}])
        items = codex._history_items([
            {"role": "assistant", "content": [{"type": "tool_use", "id": "v1", "name": "view_image",
                                               "input": {"path": "frame.png"}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "v1", "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": png}}]}]}])
        self.assertEqual(items[1]["type"], "function_call_output")
        self.assertEqual(items[1]["output"][0]["type"], "input_image")
        self.assertTrue(items[1]["output"][0]["image_url"].startswith("data:image/png;base64,"))

    def test_tool_aliases_are_short_stable_and_distinct(self):
        names = [f"mcp__codex_apps__sites._tool_{n}" for n in range(5000)] + ["exec_command", "view_image"]
        aliases = [codex._tool_alias(name) for name in names]
        self.assertEqual(len(set(aliases)), len(names))
        for alias in aliases[:50] + aliases[-2:]:
            self.assertRegex(alias, r"^bridge_[0-9a-f]{16}$")
        self.assertEqual(codex._tool_alias("exec_command"), aliases[-2])

    def test_host_execution_note_is_sent_once(self):
        import cli_routes
        from cli_tool_call import HOST_EXECUTION_NOTE
        tools = [{"name": "exec_command", "input_schema": {"type": "object"}}]
        for system in (None, "Desktop policy"):
            with self.subTest(system=system):
                payload = {"messages": [{"role": "user", "content": "hi"}], "tools": tools,
                           **({"system": system} if system else {})}
                body = cli_routes.plan_turn("codex", "gpt-6-sol", payload, {}, wanted_output=64)["body"]
                instructions = codex._normalise_request(body)["system"]
                self.assertEqual(instructions.count(HOST_EXECUTION_NOTE), 1)
                self.assertTrue(instructions.startswith(codex._HOST_INSTRUCTIONS))
                if system:
                    self.assertIn("\n\nDesktop policy", instructions)

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
                with mock.patch.object(codex, "runtime_binary", return_value="/fake/codex"), \
                        mock.patch.object(codex, "StdioSession", side_effect=lambda argv, **kw: fake):
                    list(codex.run_turn(request, spawner="stub"))
            with open(path, encoding="utf-8") as handle:
                record = json.loads(handle.read().strip())
        self.assertEqual(record["calls"], 1)
        self.assertEqual(record["legs"], 1)
        self.assertEqual(record["reasoning"], 1)


if __name__ == "__main__":
    unittest.main()
