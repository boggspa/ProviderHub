"""Long-lived NDJSON-over-stdio sessions for vendor coding-agent CLIs.

Route 2 — CLI-as-transport. The hub fronts installed coding-agent CLIs
(``claude``, ``codex app-server``, ``muse``, ``grok``, ``agy``) as ordinary
local model routes, and the defining discipline of Route 2 is that **each CLI
owns and refreshes its own login**. Nothing in this module — or in any module
built on it — ever reads, copies, stats, or refreshes a credential file, and
neither does the environment we hand to the child: :func:`minimal_env` passes
through only a small operational allowlist and deliberately drops every
provider API-key variable (``OPENAI_API_KEY``, ``ANTHROPIC_API_KEY``,
``MISTRAL_API_KEY``, ``GEMINI_API_KEY``, ``XAI_API_KEY``, ``DEEPSEEK_API_KEY``,
…). If a key leaked into the child's environment the route would silently stop
being "the CLI's own login" and start being whichever credential happened to
be exported in the hub's process — the exact failure Route 2 exists to avoid.

This module is the shared foundation the CLI adapter modules
(``claude_cli_agent``, ``agy_cli_agent``, ``codex_cli_agent``) were coded
against. It generalises the session half of ``codex_runtime.py``: a background
reader thread turns the child's stdout into a thread-safe queue of parsed
JSON dicts (one per line) or raw strings (for the plain diagnostic lines these
CLIs do print), JSON-RPC 2.0 requests are matched to responses by id while
non-matching traffic is buffered — never dropped — for :meth:`events` to
drain, and teardown follows the delegated-then-escalated ladder:
close stdin, wait, terminate, wait, kill.

The failure-posture asymmetry is deliberate and load-bearing:

* ``request()`` raises :class:`CliSessionError` on timeout, EOF before the
  answer, or child exit — a request that never got answered is a broken
  session and the caller must know.
* ``events()`` treats a *quiet* timeout as routine: iteration simply ends and
  the caller may poll again (the Codex adapter loops ``events(timeout=5.0)``
  against a turn deadline). Only genuine breakage raises.
"""
from __future__ import annotations

import collections
import inspect
import itertools
import json
import os
import queue
import shutil
import subprocess
import threading
import time
from typing import Any, Callable, Iterable, Iterator, Mapping

__all__ = ["CliSessionError", "StdioSession", "minimal_env", "resolve_binary"]


class CliSessionError(RuntimeError):
    """The CLI session broke in a way the caller cannot recover from locally."""


# ---------------------------------------------------------------------------
# Environment discipline (Route 2)
# ---------------------------------------------------------------------------

# The complete set of names that may travel from the hub's environment into a
# vendor CLI's. Everything else — most importantly any *provider* variable —
# is dropped so the child can only ever use its own login. ``LC_*`` and
# ``XDG_*`` are prefix families: locale and freedesktop state are operational,
# not credential-bearing.
_ENV_EXACT = frozenset({
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "TMPDIR",
    "LANG", "LC_ALL", "TERM", "SSH_AUTH_SOCK", "__CF_USER_TEXT_ENCODING",
})
_ENV_PREFIXES = ("LC_", "XDG_")


def minimal_env(extra: Mapping[str, str] | None = None) -> dict:
    """The allowlist environment for spawning vendor CLIs.

    Only operational variables pass through; provider API-key variables are
    excluded by construction (the allowlist is a deny-by-default list, not a
    denylist of known key names, so a newly invented ``FOO_API_KEY`` cannot
    leak either). ``extra`` merges on top, which is how an adapter points a
    CLI at an isolated config home (e.g. ``{"CODEX_HOME": ...}``) without
    widening what else the child can see.
    """
    env = {
        key: value for key, value in os.environ.items()
        if key in _ENV_EXACT or key.startswith(_ENV_PREFIXES)
    }
    if extra:
        env.update({str(key): str(value) for key, value in extra.items()})
    return env


def resolve_binary(names: Iterable[str], extra_dirs: Iterable[str] = ()) -> str | None:
    """Resolve the first available binary from ``names``.

    Each name is tried in order: an absolute path passes through when it
    exists and is executable (adapters build a concrete argv from settings
    that may already be a full path); otherwise ``shutil.which`` searches the
    current ``PATH``, then each of ``extra_dirs`` (already user-expanded
    absolute directories, e.g. an app's bundled Resources). ``None`` means
    nowhere — the caller turns that into a "route unavailable" degradation
    rather than an exception, because absence of a CLI is a configuration
    state, not a bug.
    """
    for name in names:
        candidate = str(name)
        if os.path.isabs(candidate):
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                return candidate
            continue
        found = shutil.which(candidate)
        if found:
            return found
        for directory in extra_dirs:
            if not directory:
                continue
            path = os.path.join(str(directory), candidate)
            if os.path.isfile(path) and os.access(path, os.X_OK):
                return path
    return None


# ---------------------------------------------------------------------------
# StdioSession
# ---------------------------------------------------------------------------

# Queue sentinel marking "stdout closed": every pending and future consumer
# must wind down promptly once this has been seen. A distinct object (not
# None) so a None payload can never masquerade as EOF.
_EOF = object()

# Marker for "no matching item" lookups inside request(); distinct from None
# because None could legitimately sit in a queue slot otherwise.
_MISSING = object()


class StdioSession:
    """One long-lived child process speaking newline-delimited JSON over stdio.

    A daemon reader thread owns the child's stdout: line-by-line, each line
    is parsed as JSON and the resulting dict (or the raw string when the line
    is not JSON — these CLIs interleave plain diagnostics with the protocol)
    is put on a thread-safe queue, followed by an EOF sentinel when stdout
    closes. Consumers never touch the pipe directly, so a slow reader can
    never deadlock the child the way an undrained pipe would.

    ``spawner`` exists so tests can inject a scriptable ``Popen``
    replacement; it is invoked with ``(list(argv), stdin=PIPE, stdout=PIPE,
    stderr=..., env=..., cwd=..., text=True)``, trimmed via ``inspect`` to
    whichever subset of those keyword arguments it actually declares.
    """

    def __init__(self, argv: list[str], *, env: Mapping[str, str] | None = None,
                 cwd: str | None = None, timeout: float = 120.0,
                 spawner: Callable[..., Any] | None = None,
                 stderr: Any = None):
        self.argv = [str(item) for item in argv]
        self.env = dict(env) if env is not None else None
        self.cwd = cwd
        # Per-operation default budget for request()/events() when the caller
        # does not pass one. Adapters pass the turn budget here so a request
        # without an explicit timeout still cannot hang the harness forever.
        self.timeout = float(timeout)
        # stderr may be None (discarded), a text file object the caller keeps
        # open for diagnostics, or subprocess.DEVNULL. It is never a PIPE:
        # nobody would drain it and a chatty CLI would eventually block.
        if stderr is None:
            stderr = subprocess.DEVNULL
        self._spawn = spawner or subprocess.Popen
        self._queue: queue.Queue = queue.Queue()
        # Responses that arrived while waiting for a different id, plus raw
        # lines consumed by request(): buffered for events(), never dropped,
        # because server notifications carry state the adapters act on.
        self._pending: collections.deque = collections.deque()
        self._pending_lock = threading.Lock()
        self._ids = itertools.count(1)
        self._eof = False
        self._closed = False
        self.process = self._launch(stderr)
        # Exposed because print-mode CLIs (claude, agy) duck-type the raw
        # writer: they write the prompt as plain text and signal EOF by
        # closing stdin, which their --input-format text mode requires.
        self.stdin = self.process.stdin
        self._reader = threading.Thread(
            target=self._drain_stdout,
            name=f"cli-session-reader-{os.path.basename(self.argv[0]) if self.argv else 'child'}",
            daemon=True,
        )
        self._reader.start()

    # -- lifecycle -----------------------------------------------------

    def _launch(self, stderr: Any):
        """Spawn via the (possibly injected) spawner, adapting its signature."""
        offered = {
            "stdin": subprocess.PIPE,
            "stdout": subprocess.PIPE,
            "stderr": stderr,
            "env": self.env,
            "cwd": self.cwd,
            "text": True,
        }
        try:
            parameters = inspect.signature(self._spawn).parameters
        except (TypeError, ValueError):
            # Builtin or exotic callable with no introspectable signature:
            # offer everything and let it reject what it does not take.
            kwargs = dict(offered)
        else:
            accepts_any = any(p.kind is inspect.Parameter.VAR_KEYWORD
                              for p in parameters.values())
            if accepts_any:
                kwargs = dict(offered)
            else:
                kwargs = {name: value for name, value in offered.items()
                          if name in parameters}
        return self._spawn(list(self.argv), **kwargs)

    def _drain_stdout(self) -> None:
        """Reader thread body: stdout lines -> queue, then the EOF sentinel."""
        try:
            stream = self.process.stdout
            if stream is not None:
                for line in stream:
                    self._queue.put(self._parse_line(line))
        except (OSError, ValueError):
            # The pipe broke or closed under us (usually teardown racing the
            # reader); the EOF sentinel below still fires so consumers wind
            # down instead of waiting out their full budget.
            pass
        finally:
            self._queue.put(_EOF)

    @staticmethod
    def _parse_line(line: Any) -> Any:
        """One stdout line -> dict when it is a JSON object, else raw string."""
        if isinstance(line, (bytes, bytearray)):
            line = bytes(line).decode("utf-8", "replace")
        text = str(line)
        try:
            parsed = json.loads(text)
        except ValueError:
            # Not protocol traffic: keep the raw line (newline stripped) so
            # adapters can buffer it for diagnostics instead of losing it.
            return text.rstrip("\n")
        # Only objects participate in the protocol; a bare JSON scalar is
        # treated like a diagnostic line rather than an event dict.
        return parsed if isinstance(parsed, dict) else text.rstrip("\n")

    def __enter__(self) -> "StdioSession":
        return self

    def __exit__(self, exc_type, exc, traceback) -> bool:
        self.close()
        return False

    @property
    def returncode(self) -> int | None:
        """Poll-based child state: ``None`` while running, int after exit."""
        try:
            return self.process.poll()
        except Exception:  # noqa: BLE001 - a probe must not raise
            return None

    # -- writing --------------------------------------------------------

    def send(self, payload: str) -> None:
        """Write one line of text to the child's stdin, flushing immediately.

        The newline is appended only when missing so callers that pre-frame
        their payload are not double-terminated — and so print-mode adapters
        can send a bare prompt and close stdin as the turn-ending signal.
        """
        if not payload.endswith("\n"):
            payload += "\n"
        self.stdin.write(payload)
        self.stdin.flush()

    def notify(self, method: str, params: Any) -> None:
        """One JSON-RPC 2.0 notification line (no id: never answered)."""
        self.send(json.dumps({"jsonrpc": "2.0", "method": method,
                              "params": params if params is not None else {}}))

    # -- reading ---------------------------------------------------------

    def _pop_pending_match(self, identifier: int) -> Any:
        """Pull a buffered response with this id out of the pending deque.

        Called first by request(): a previous events() slice may already have
        fished the answer out of the queue, and re-deriving it from the wire
        is impossible. Returns _MISSING when the deque holds no match; all
        other items (notifications, raw lines) stay queued, untouched.
        """
        with self._pending_lock:
            for index, item in enumerate(self._pending):
                if isinstance(item, dict) and item.get("id") == identifier:
                    del self._pending[index]
                    return item
        return _MISSING

    def _enqueue_pending(self, item: Any) -> None:
        with self._pending_lock:
            self._pending.append(item)

    def request(self, method: str, params: Any, timeout: float | None = None) -> dict:
        """Send one JSON-RPC 2.0 request and wait for the id-matched response.

        Returns the full response dict; callers unwrap "result"/"error"
        themselves because each adapter has its own error vocabulary.

        Raises :class:`CliSessionError` on timeout, on EOF/child exit before
        the answer, or after the child dies mid-wait — an unanswered request
        means the session cannot honour the protocol any more. Non-matching
        dicts (server notifications) and raw lines are buffered for
        :meth:`events`, never dropped.
        """
        budget = self.timeout if timeout is None else float(timeout)
        identifier = next(self._ids)
        self.send(json.dumps({"jsonrpc": "2.0", "id": identifier,
                              "method": method, "params": params if params is not None else {}}))
        deadline = time.monotonic() + budget
        while True:
            buffered = self._pop_pending_match(identifier)
            if buffered is not _MISSING:
                return buffered
            if self._eof:
                raise CliSessionError(
                    f"the CLI session closed before answering '{method}' "
                    f"(child rc={self.returncode})")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CliSessionError(
                    f"timed out after {budget:.1f}s waiting for '{method}' "
                    f"(id {identifier}) to answer")
            try:
                item = self._queue.get(timeout=max(0.01, remaining))
            except queue.Empty:
                continue  # re-check the deadline, EOF and the child
            if item is _EOF:
                self._eof = True
                raise CliSessionError(
                    f"the CLI session closed before answering '{method}' "
                    f"(child rc={self.returncode})")
            if isinstance(item, dict) and item.get("id") == identifier:
                return item
            self._enqueue_pending(item)

    def events(self, timeout: float | None = None) -> Iterator[Any]:
        """Yield queued items — dicts for JSON lines, raw strings otherwise.

        Drains the pending deque first (responses buffered during a
        request() plus everything events() did not previously consume), then
        pulls fresh items from the reader queue.

        A *quiet* timeout ends the iteration without raising: the Codex
        adapter polls ``events(timeout=5.0)`` in a loop until its turn
        deadline, and an empty slice there is the routine "nothing happened
        in the last five seconds", not an error. EOF ends this and all future
        iterations promptly. :class:`CliSessionError` is reserved for genuine
        breakage; a dead child surfaces as EOF, not an exception here.
        """
        budget = self.timeout if timeout is None else float(timeout)
        deadline = time.monotonic() + budget
        while True:
            with self._pending_lock:
                if self._pending:
                    yield self._pending.popleft()
                    continue
            if self._eof:
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            try:
                item = self._queue.get(timeout=max(0.01, remaining))
            except queue.Empty:
                return
            if item is _EOF:
                self._eof = True
                return
            yield item

    # -- teardown ---------------------------------------------------------

    def close(self) -> None:
        """Idempotent delegated-then-escalated teardown; never raises.

        The ``codex_runtime.py`` ladder: closing stdin is the polite signal
        (print-mode CLIs end their turn on EOF); if the child has not exited
        within three seconds it gets SIGTERM, then SIGKILL, waiting three
        seconds between rungs. Every step is best-effort — teardown must not
        mask the turn's own outcome when it runs from a ``finally``.
        """
        if self._closed:
            return
        self._closed = True
        process = self.process
        try:
            if self.stdin is not None:
                try:
                    self.stdin.close()
                except OSError:
                    pass
        except Exception:  # noqa: BLE001 - teardown must never raise
            pass
        try:
            process.wait(timeout=3)
        except Exception:  # noqa: BLE001 - TimeoutExpired or a fake's error
            try:
                process.terminate()
            except Exception:  # noqa: BLE001
                pass
            try:
                process.wait(timeout=3)
            except Exception:  # noqa: BLE001
                pass
                try:
                    process.kill()
                except Exception:  # noqa: BLE001
                    pass
                try:
                    process.wait(timeout=3)
                except Exception:  # noqa: BLE001
                    pass
        # Whether the child exited politely or had to be shot, the reader
        # thread (and the stdout handle behind it) is reaped on every path.
        try:
            self._reader.join(timeout=1)
        except Exception:  # noqa: BLE001
            pass
        if self._reader.is_alive():
            # The reader is still parked on a live pipe (a wedged child that
            # survived SIGKILL is gone, but its stdout can still be held open
            # by a grandchild). Closing the wrapper now would block on the
            # buffer lock the reader holds mid-read, deadlocking teardown —
            # so the handle is deliberately leaked instead. The child is
            # already reaped; the daemon thread keeps the object alive.
            return
        try:
            if process.stdout is not None:
                process.stdout.close()
        except Exception:  # noqa: BLE001
            pass
