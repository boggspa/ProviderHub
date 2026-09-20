"""Tests for cli_session: the Route-2 stdio session foundation.

No real CLI is ever spawned and nothing touches the network: a scriptable
``FakeProcess`` stands in for ``subprocess.Popen`` behind the session's
``spawner`` injection point, giving the tests real pipes for stdin/stdout so
the reader thread, the newline framing, and the EOF sentinel all exercise
their true code paths.
"""
from __future__ import annotations

import io
import itertools
import json
import os
import stat
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from cli_session import CliSessionError, StdioSession, minimal_env, resolve_binary


# ---------------------------------------------------------------------------
# Scriptable Popen replacement
# ---------------------------------------------------------------------------

class FakeProcess:
    """A controllable stand-in for a text-mode ``subprocess.Popen``.

    Stdin/stdout are genuine pipes, so the session's reader thread iterates
    the same way it would against a real child. When ``script`` is a list of
    ``(delay, line)`` tuples a player thread writes them to stdout and then
    closes it (the child "exits"); a tiny inter-line delay keeps ordering
    deterministic without any test-side sleep. When ``script`` is ``None`` no
    player runs at all: the test drives stdout directly and decides when EOF
    happens — that is the interactive mode used by the request/notify tests,
    where answering on the wire requires reading the request first.

    ``exit_on_stdin_eof`` models print-mode CLIs (claude, agy) that end their
    turn when stdin closes. ``stubborn`` models a wedged child that ignores
    SIGTERM and only dies on SIGKILL; in that mode ``wait`` raises
    ``TimeoutExpired`` immediately rather than blocking, so the close()
    ladder is exercised without burning wall clock.
    """

    def __init__(self, script=(), *, exit_on_stdin_eof=True, stubborn=False,
                 rc=0):
        self.exit_on_stdin_eof = exit_on_stdin_eof
        self.stubborn = stubborn
        self._rc = rc
        self.stdin_pipe = os.pipe()   # session writes [1], we read [0]
        self.stdout_pipe = os.pipe()  # we write [1], session reads [0]
        self.stdin = io.TextIOWrapper(io.BufferedWriter(
            io.FileIO(self.stdin_pipe[1], "w")), encoding="utf-8")
        self.stdout = io.TextIOWrapper(io.BufferedReader(
            io.FileIO(self.stdout_pipe[0], "r")), encoding="utf-8")
        self.stderr = None
        self.pid = os.getpid()
        self.returncode = None
        self.terminated = False
        self.killed = False
        self._cond = threading.Condition()
        self._stdin_closed = threading.Event()
        self._stdout_open = True
        # Everything the session writes to stdin is drained here (a real
        # child consumes its stdin) but kept, so tests can assert on the
        # exact bytes of a request after the fact.
        self.received = bytearray()
        self._received_cond = threading.Condition()

        def watch_stdin():
            # Drain the pipe so a chatty session never blocks on a full
            # buffer, and notice EOF so a polite child can exit. Bytes are
            # buffered rather than discarded so a test can read back the
            # request that prompted an answer.
            while True:
                chunk = os.read(self.stdin_pipe[0], 4096)
                if not chunk:
                    break
                with self._received_cond:
                    self.received.extend(chunk)
                    self._received_cond.notify_all()
            self._stdin_closed.set()
            if self.exit_on_stdin_eof:
                self.close_stdout(self._rc)

        threading.Thread(target=watch_stdin, daemon=True,
                         name="fake-watch-stdin").start()

        if script is not None:
            def play_script():
                for delay, line in script:
                    if delay:
                        time.sleep(delay)
                    try:
                        if not line.endswith("\n"):
                            line += "\n"
                        os.write(self.stdout_pipe[1], line.encode("utf-8"))
                    except OSError:
                        return  # session torn down mid-script
                self.close_stdout(self._rc)

            threading.Thread(target=play_script, daemon=True,
                             name="fake-play-script").start()

    # -- script helpers ------------------------------------------------

    def write_stdout(self, payload):
        """Deliver one line as the server side of the protocol."""
        if isinstance(payload, dict):
            payload = json.dumps(payload)
        if not str(payload).endswith("\n"):
            payload = str(payload) + "\n"
        os.write(self.stdout_pipe[1], payload.encode("utf-8"))

    def close_stdout(self, rc=0):
        """Close the write end: the session's reader sees EOF."""
        self._close_stdout_fd()
        self._finish(rc)

    def _close_stdout_fd(self):
        with self._cond:
            if self._stdout_open:
                self._stdout_open = False
                try:
                    os.close(self.stdout_pipe[1])
                except OSError:
                    pass

    def _finish(self, rc):
        with self._cond:
            if self.returncode is None:
                self.returncode = rc
                self._cond.notify_all()

    # -- subprocess.Popen surface ---------------------------------------

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        with self._cond:
            if self.returncode is not None:
                return self.returncode
            if self.stubborn and not self.killed:
                # Wedged: report "waited the whole timeout and it never
                # exited" without burning wall clock.
                raise subprocess.TimeoutExpired("fake", timeout)
            deadline = None if timeout is None \
                else time.monotonic() + max(0.0, timeout)
            while self.returncode is None:
                if deadline is None:
                    self._cond.wait(timeout=0.05)
                    continue
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired("fake", timeout)
                self._cond.wait(timeout=min(remaining, 0.05))
            return self.returncode

    def terminate(self):
        self.terminated = True
        if not self.stubborn:
            # A dying child closes its end of stdout: the reader's pipe read
            # returns EOF instead of staying parked forever.
            self._close_stdout_fd()
            self._finish(-15)

    def kill(self):
        self.killed = True
        self._close_stdout_fd()
        self._finish(-9)


class FakeSpawner:
    """Records the kwargs StdioSession offers, then returns a FakeProcess."""

    def __init__(self, process):
        self.process = process
        self.calls = []

    def __call__(self, argv, stdin=None, stdout=None, stderr=None,
                 env=None, cwd=None, text=False):
        self.calls.append({"argv": list(argv), "stdin": stdin, "stdout": stdout,
                           "stderr": stderr, "env": env, "cwd": cwd, "text": text})
        return self.process


def make_session(fake, **kwargs):
    kwargs.setdefault("timeout", 5.0)
    return StdioSession(["fake-cli", "--serve"], spawner=FakeSpawner(fake),
                        **kwargs)


def read_stdin_line(fake, timeout=2.0):
    """Read one newline-terminated line the session wrote to fake stdin."""
    data = bytearray()
    deadline = time.monotonic() + timeout
    with fake._received_cond:
        while True:
            newline = fake.received.find(b"\n")
            if newline >= 0:
                data.extend(fake.received[:newline + 1])
                del fake.received[:newline + 1]
                return bytes(data).decode("utf-8")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AssertionError(
                    f"timed out reading fake stdin, got {bytes(data)!r}")
            fake._received_cond.wait(timeout=remaining)


def wait_until(predicate, timeout=2.0):
    """Poll a condition that a background thread is expected to make true."""
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition never became true")
        time.sleep(0.01)


# ---------------------------------------------------------------------------
# minimal_env
# ---------------------------------------------------------------------------

class MinimalEnvTests(unittest.TestCase):
    def test_allowlist_passes_operational_vars_only(self):
        with mock.patch.dict(os.environ, {
                "PATH": "/usr/bin:/bin", "HOME": "/tmp/fake-home",
                "USER": "tester", "LOGNAME": "tester", "SHELL": "/bin/zsh",
                "TMPDIR": "/tmp", "LANG": "en_GB.UTF-8", "LC_ALL": "C",
                "LC_MESSAGES": "en_GB", "TERM": "xterm-256color",
                "XDG_CONFIG_HOME": "/tmp/xdg", "SSH_AUTH_SOCK": "/tmp/ssh.sock",
                "__CF_USER_TEXT_ENCODING": "0x1F5:0x0:0x0",
                "OPENAI_API_KEY": "sk-live-openai",
                "ANTHROPIC_API_KEY": "sk-ant-live",
                "MISTRAL_API_KEY": "mistral-live",
                "GEMINI_API_KEY": "gemini-live", "XAI_API_KEY": "xai-live",
                "DEEPSEEK_API_KEY": "deepseek-live",
                "SUPER_SECRET_NEW_PROVIDER_API_KEY": "whatever",
                "RANDOM_OTHER_VAR": "noise",
                }, clear=True):
            env = minimal_env()
        # The deny-by-default allowlist must not leak any key, known or not.
        for key in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "MISTRAL_API_KEY",
                    "GEMINI_API_KEY", "XAI_API_KEY", "DEEPSEEK_API_KEY",
                    "SUPER_SECRET_NEW_PROVIDER_API_KEY", "RANDOM_OTHER_VAR"):
            self.assertNotIn(key, env)
        for key in ("PATH", "HOME", "USER", "LOGNAME", "SHELL", "TMPDIR",
                    "LANG", "LC_ALL", "TERM", "SSH_AUTH_SOCK",
                    "__CF_USER_TEXT_ENCODING", "LC_MESSAGES",
                    "XDG_CONFIG_HOME"):
            self.assertIn(key, env)

    def test_extra_merges_on_top(self):
        with mock.patch.dict(os.environ, {"PATH": "/usr/bin", "HOME": "/h"},
                             clear=True):
            env = minimal_env({"CODEX_HOME": "/tmp/codex-home",
                               "CLAUDE_CONFIG": "/tmp/claude"})
        self.assertEqual(env["CODEX_HOME"], "/tmp/codex-home")
        self.assertEqual(env["CLAUDE_CONFIG"], "/tmp/claude")
        # Merging must not pollute the hub's own environment.
        self.assertNotIn("CODEX_HOME", os.environ)

    def test_no_extra_returns_fresh_dict(self):
        with mock.patch.dict(os.environ, {"PATH": "/usr/bin"}, clear=True):
            first, second = minimal_env(), minimal_env()
        first["PATH"] = "clobbered"
        self.assertEqual(second["PATH"], "/usr/bin")


# ---------------------------------------------------------------------------
# resolve_binary
# ---------------------------------------------------------------------------

class ResolveBinaryTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)

    def _make_exe(self, directory, name):
        path = Path(directory) / name
        path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP
                   | stat.S_IXOTH)
        return path

    def test_path_lookup(self):
        self._make_exe(self.dir, "hub-fake-bin")
        with mock.patch.dict(os.environ, {"PATH": str(self.dir)}, clear=True):
            found = resolve_binary(("hub-fake-bin",))
        self.assertEqual(found, str(self.dir / "hub-fake-bin"))

    def test_extra_dirs_used_when_not_on_path(self):
        extra = self.dir / "extra"
        extra.mkdir()
        self._make_exe(extra, "hub-fake-bin2")
        with mock.patch.dict(os.environ, {"PATH": "/nonexistent"}, clear=True):
            found = resolve_binary(("hub-fake-bin2",),
                                   extra_dirs=(str(extra),))
        self.assertEqual(found, str(extra / "hub-fake-bin2"))

    def test_absolute_passthrough(self):
        exe = self._make_exe(self.dir, "hub-abs-bin")
        self.assertEqual(resolve_binary((str(exe),)), str(exe))

    def test_absolute_non_executable_falls_through(self):
        plain = self.dir / "hub-plain-file"
        plain.write_text("not executable", encoding="utf-8")
        self.assertIsNone(resolve_binary((str(plain),)))

    def test_none_when_nowhere(self):
        with mock.patch.dict(os.environ, {"PATH": "/nonexistent"}, clear=True):
            self.assertIsNone(
                resolve_binary(("hub-definitely-missing-bin",)))

    def test_first_resolvable_name_wins(self):
        self._make_exe(self.dir, "hub-second-bin")
        with mock.patch.dict(os.environ, {"PATH": str(self.dir)}, clear=True):
            found = resolve_binary(("hub-missing-bin", "hub-second-bin"))
        self.assertEqual(found, str(self.dir / "hub-second-bin"))


# ---------------------------------------------------------------------------
# StdioSession lifecycle and spawning
# ---------------------------------------------------------------------------

class SessionLifecycleTests(unittest.TestCase):
    def test_spawner_receives_framed_argv_and_stderr_default(self):
        fake = FakeProcess(script=None)
        session = make_session(fake)
        try:
            call = session._spawn.calls[0]
            self.assertEqual(call["argv"], ["fake-cli", "--serve"])
            self.assertEqual(call["stdin"], subprocess.PIPE)
            self.assertEqual(call["stdout"], subprocess.PIPE)
            self.assertEqual(call["stderr"], subprocess.DEVNULL)
            self.assertTrue(call["text"])
            self.assertIsNone(call["env"])
            self.assertIsNone(call["cwd"])
        finally:
            session.close()

    def test_spawner_adapts_to_partial_signature(self):
        fake = FakeProcess(script=None)
        seen = {}

        def partial_spawner(argv, stdin=None, stdout=None):
            seen.update(argv=list(argv), stdin=stdin, stdout=stdout)
            return fake

        session = StdioSession(["fake-cli"], spawner=partial_spawner,
                               env={"A": "1"}, timeout=5.0)
        try:
            self.assertEqual(seen["argv"], ["fake-cli"])
            self.assertEqual(seen["stdin"], subprocess.PIPE)
            self.assertEqual(seen["stdout"], subprocess.PIPE)
            self.assertNotIn("env", seen)  # undeclared kwarg not offered
        finally:
            session.close()

    def test_env_and_cwd_forwarded(self):
        fake = FakeProcess(script=None)
        session = make_session(fake, env={"PATH": "/x"}, cwd="/tmp")
        try:
            call = session._spawn.calls[0]
            self.assertEqual(call["env"], {"PATH": "/x"})
            self.assertEqual(call["cwd"], "/tmp")
        finally:
            session.close()

    def test_stderr_file_object_passes_through(self):
        fake = FakeProcess(script=None)
        with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as handle:
            session = make_session(fake, stderr=handle)
            try:
                self.assertIs(session._spawn.calls[0]["stderr"], handle)
            finally:
                session.close()

    def test_returncode_tracks_poll(self):
        fake = FakeProcess(script=None)
        session = make_session(fake)
        try:
            self.assertIsNone(session.returncode)
            fake._finish(7)
            self.assertEqual(session.returncode, 7)
        finally:
            session.close()

    def test_context_manager_returns_self_and_closes(self):
        fake = FakeProcess()
        with make_session(fake) as session:
            self.assertIs(session.stdin, fake.stdin)
        # close() returns once the child is reaped; the stdin watch thread
        # may legitimately still be tearing down.
        wait_until(lambda: fake._stdin_closed.is_set())
        self.assertFalse(fake.terminated)
        self.assertFalse(fake.killed)
        # Second close is a no-op, not an error.
        session.close()


# ---------------------------------------------------------------------------
# send / notify framing
# ---------------------------------------------------------------------------

class SendFramingTests(unittest.TestCase):
    def test_send_appends_newline_when_missing(self):
        fake = FakeProcess(script=None)
        session = make_session(fake)
        try:
            session.send("hello")
            self.assertEqual(read_stdin_line(fake), "hello\n")
            session.send("already framed\n")
            self.assertEqual(read_stdin_line(fake), "already framed\n")
        finally:
            session.close()

    def test_notify_writes_jsonrpc_line(self):
        fake = FakeProcess(script=None)
        session = make_session(fake)
        try:
            session.notify("initialized", {})
            line = json.loads(read_stdin_line(fake))
            self.assertEqual(line, {"jsonrpc": "2.0",
                                    "method": "initialized", "params": {}})
            session.notify("notifications/initialized", None)
            line = json.loads(read_stdin_line(fake))
            self.assertEqual(line["method"], "notifications/initialized")
            self.assertEqual(line["params"], {})
        finally:
            session.close()


# ---------------------------------------------------------------------------
# request()
# ---------------------------------------------------------------------------

class RequestTests(unittest.TestCase):
    def test_server_call_with_same_id_is_not_a_client_response(self):
        fake = FakeProcess(script=None)
        session = make_session(fake)
        native = {"id": 1, "method": "item/tool/call", "params": {"callId": "host-call"}}
        response = {"id": 1, "result": {"turnId": "turn-1"}}
        try:
            fake.write_stdout(native)
            fake.write_stdout(response)
            self.assertEqual(session.request("turn/steer", {}, timeout=1), response)
            events = session.events(timeout=.01)
            self.assertEqual(next(events), native)
            # A caller can send another RPC while an event iterator is paused.
            # The pending-queue lock must not remain held across that yield.
            fake.write_stdout({"id": 2, "result": {}})
            done = threading.Event()
            results = []
            def request_again():
                try:
                    results.append(session.request("turn/steer", {}, timeout=1))
                finally:
                    done.set()
            worker = threading.Thread(target=request_again, daemon=True)
            worker.start()
            try:
                self.assertTrue(done.wait(2), "paused event iterator retained the pending lock")
                self.assertEqual(results, [{"id": 2, "result": {}}])
            finally:
                events.close()
                worker.join(2)
        finally:
            session.close()

    def _read_request(self, fake, method):
        """Read one request line from stdin; return its id after asserting."""
        line = json.loads(read_stdin_line(fake))
        self.assertEqual(line["jsonrpc"], "2.0")
        self.assertEqual(line["method"], method)
        self.assertIsInstance(line["id"], int)
        return line["id"]

    def test_request_frames_and_matches_id(self):
        fake = FakeProcess(script=None)
        session = make_session(fake)
        try:
            responses = []
            waiter = threading.Thread(
                target=lambda: responses.append(
                    session.request("thread/start", {"cwd": "/t"},
                                    timeout=2.0)),
                daemon=True)
            waiter.start()
            identifier = self._read_request(fake, "thread/start")
            fake.write_stdout({"jsonrpc": "2.0", "id": identifier,
                               "result": {"thread": {"id": "th1"}}})
            waiter.join(timeout=2.0)
            self.assertFalse(waiter.is_alive())
            self.assertEqual(responses[0]["id"], identifier)
            self.assertEqual(responses[0]["result"]["thread"]["id"], "th1")
        finally:
            session.close()

    def test_request_returns_full_response_dict(self):
        fake = FakeProcess(script=None)
        session = make_session(fake)
        try:
            waiter = threading.Thread(
                target=lambda: responses.append(
                    session.request("initialize", {"clientInfo": {}},
                                    timeout=2.0)),
                daemon=True)
            responses = []
            waiter.start()
            identifier = self._read_request(fake, "initialize")
            fake.write_stdout({"jsonrpc": "2.0", "id": identifier,
                               "result": {"capabilities": {"x": 1}}})
            waiter.join(timeout=2.0)
            self.assertEqual(responses[0]["result"], {"capabilities": {"x": 1}})
            self.assertEqual(responses[0]["id"], identifier)
        finally:
            session.close()

    def test_request_ids_increment(self):
        fake = FakeProcess(script=None)
        session = make_session(fake)
        try:
            for expected_id in (1, 2):
                waiter = threading.Thread(
                    target=lambda: session.request("model/list", {},
                                                   timeout=2.0),
                    daemon=True)
                waiter.start()
                self.assertEqual(self._read_request(fake, "model/list"),
                                 expected_id)
                fake.write_stdout({"jsonrpc": "2.0", "id": expected_id,
                                   "result": {}})
                waiter.join(timeout=2.0)
        finally:
            session.close()

    def test_interleaved_notification_buffered_for_events(self):
        fake = FakeProcess(script=None)
        session = make_session(fake)
        try:
            waiter = threading.Thread(
                target=lambda: responses.append(
                    session.request("thread/start", {}, timeout=2.0)),
                daemon=True)
            responses = []
            waiter.start()
            identifier = self._read_request(fake, "thread/start")
            # The server speaks a notification BEFORE answering: request()
            # must consume it without dropping it.
            fake.write_stdout({"jsonrpc": "2.0", "method": "turn/started",
                               "params": {"turn": {"id": "t1"}}})
            fake.write_stdout({"jsonrpc": "2.0", "id": identifier,
                               "result": {"thread": {"id": "th1"}}})
            waiter.join(timeout=2.0)
            self.assertEqual(responses[0]["result"]["thread"]["id"], "th1")
            drained = list(session.events(timeout=0.2))
            self.assertEqual(len(drained), 1)
            self.assertEqual(drained[0]["method"], "turn/started")
        finally:
            session.close()

    def test_raw_line_during_request_is_buffered(self):
        fake = FakeProcess(script=None)
        session = make_session(fake)
        try:
            waiter = threading.Thread(
                target=lambda: session.request("initialize", {}, timeout=2.0),
                daemon=True)
            waiter.start()
            identifier = self._read_request(fake, "initialize")
            fake.write_stdout("plain diagnostic on stdout")
            fake.write_stdout({"jsonrpc": "2.0", "id": identifier,
                               "result": {}})
            waiter.join(timeout=2.0)
            drained = list(session.events(timeout=0.2))
            self.assertEqual(drained, ["plain diagnostic on stdout"])
        finally:
            session.close()

    def test_request_raises_on_quiet_timeout(self):
        fake = FakeProcess(script=None)
        session = make_session(fake)
        try:
            with self.assertRaises(CliSessionError) as ctx:
                session.request("model/list", {}, timeout=0.1)
            self.assertIn("model/list", str(ctx.exception))
            self.assertIn("timed out", str(ctx.exception).lower())
        finally:
            session.close()

    def test_request_raises_on_eof(self):
        fake = FakeProcess(script=None)
        session = make_session(fake)
        try:
            fake.close_stdout()
            # The reader thread exits as soon as it has queued the EOF
            # sentinel; session._eof is only set once a consumer dequeues it.
            wait_until(lambda: not session._reader.is_alive())
            with self.assertRaises(CliSessionError) as ctx:
                session.request("initialize", {}, timeout=1.0)
            self.assertIn("closed", str(ctx.exception).lower())
        finally:
            session.close()

    def test_response_buffered_by_events_is_found_by_request(self):
        # Symmetry defence: a response an events() slice pulled into the
        # pending deque must still satisfy a later request() with the same
        # id. Adapters never interleave like this, but losing a response
        # would be a protocol violation, so the lookup is idempotent.
        fake = FakeProcess(script=None)
        session = make_session(fake)
        try:
            fake.write_stdout({"jsonrpc": "2.0", "id": 41,
                               "result": {"late": True}})
            wait_until(lambda: session._queue.qsize() > 0)
            session._ids = itertools.count(41)  # collide with the buffered id
            response = session.request("thread/start", {}, timeout=1.0)
            self.assertEqual(response["result"], {"late": True})
        finally:
            session.close()


# ---------------------------------------------------------------------------
# events()
# ---------------------------------------------------------------------------

class EventsTests(unittest.TestCase):
    def test_events_yields_dicts_and_raw_strings(self):
        fake = FakeProcess(script=[
            (0.0, json.dumps({"jsonrpc": "2.0", "method": "turn/started",
                              "params": {}})),
            (0.02, "a plain diagnostic line"),
        ], exit_on_stdin_eof=False)
        session = make_session(fake)
        try:
            items = list(session.events(timeout=2.0))
            self.assertEqual(items[0], {"jsonrpc": "2.0",
                                        "method": "turn/started",
                                        "params": {}})
            self.assertEqual(items[1], "a plain diagnostic line")
        finally:
            session.close()

    def test_quiet_timeout_ends_iteration_without_raising(self):
        fake = FakeProcess(script=None)
        session = make_session(fake)
        try:
            started = time.monotonic()
            self.assertEqual(list(session.events(timeout=0.1)), [])
            # Must end promptly at the quiet timeout, not at the full budget.
            self.assertLess(time.monotonic() - started, 0.5)
        finally:
            session.close()

    def test_eof_ends_iteration_and_all_future_iterations(self):
        fake = FakeProcess(script=[
            (0.0, json.dumps({"jsonrpc": "2.0", "id": 1, "result": {}})),
        ], exit_on_stdin_eof=False)
        session = make_session(fake)
        try:
            items = list(session.events(timeout=2.0))
            self.assertEqual(len(items), 1)
            self.assertTrue(session._eof)
            # Future slices end promptly even with a generous budget.
            started = time.monotonic()
            self.assertEqual(list(session.events(timeout=5.0)), [])
            self.assertLess(time.monotonic() - started, 0.5)
        finally:
            session.close()

    def test_repeated_slices_yield_new_items(self):
        fake = FakeProcess(script=None)
        session = make_session(fake)
        try:
            fake.write_stdout({"jsonrpc": "2.0", "method": "one",
                               "params": {}})
            first = list(session.events(timeout=2.0))
            self.assertEqual([item.get("method") for item in first
                              if isinstance(item, dict)], ["one"])
            # Quiet slice between arrivals: routine, not an error.
            self.assertEqual(list(session.events(timeout=0.1)), [])
            fake.write_stdout({"jsonrpc": "2.0", "method": "two",
                               "params": {}})
            second = list(session.events(timeout=2.0))
            self.assertEqual([item.get("method") for item in second
                              if isinstance(item, dict)], ["two"])
        finally:
            session.close()


# ---------------------------------------------------------------------------
# close() teardown ladder
# ---------------------------------------------------------------------------

class CloseLadderTests(unittest.TestCase):
    def test_close_is_idempotent_and_polite_child_needs_no_signals(self):
        fake = FakeProcess()  # exits when stdin closes
        session = make_session(fake)
        session.close()
        wait_until(lambda: fake._stdin_closed.is_set())
        self.assertFalse(fake.terminated)
        self.assertFalse(fake.killed)
        session.close()  # second call must be a no-op
        self.assertFalse(fake.terminated)
        self.assertFalse(fake.killed)

    def test_close_skips_terminate_when_child_already_exited(self):
        fake = FakeProcess(script=None)
        session = make_session(fake)
        fake._finish(3)
        session.close()
        self.assertEqual(fake.returncode, 3)
        self.assertFalse(fake.terminated)
        self.assertFalse(fake.killed)

    def test_wedged_child_escalates_terminate_then_kill(self):
        fake = FakeProcess(script=None, exit_on_stdin_eof=False, stubborn=True)
        session = make_session(fake)
        started = time.monotonic()
        session.close()
        self.assertTrue(fake.terminated)
        self.assertTrue(fake.killed)
        self.assertEqual(fake.returncode, -9)
        # The ladder must not have burned the real 3s+3s timeouts.
        self.assertLess(time.monotonic() - started, 1.0)
        session.close()  # still idempotent afterwards

    def test_close_never_raises_on_broken_child(self):
        fake = FakeProcess(script=None)
        session = make_session(fake)

        def explode(*args, **kwargs):
            raise OSError("everything is broken")

        fake.wait = explode
        fake.terminate = explode
        fake.kill = explode
        session.close()  # must swallow all of it
        session.close()


if __name__ == "__main__":
    unittest.main()
