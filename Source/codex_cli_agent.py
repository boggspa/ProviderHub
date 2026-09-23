"""Codex as a hub model route: ChatGPT-subscription turns over the app-server.

This is the LOCAL-ONLY, opt-in-per-provider ``(CLI | API)`` experiment's CLI
side for Codex. It fronts an installed ``codex`` binary as an ordinary hub
model route, so a desktop harness thread can select ``codex/gpt-5.6-sol`` and
get a normal streaming turn backed by the user's ChatGPT subscription rather
than an API key.

Route 2: CLI-as-transport
-------------------------
We spawn the vendor's own runtime and let *it* own and refresh its login. This
module never reads, copies, refreshes, stats or parses a credential: not
``~/.codex/auth.json``, not the macOS Keychain, not ``CODEX_ACCESS_TOKEN``.
:func:`auth_state` asks ``codex login status`` and reports what it prints.

For Codex that is not merely tidy, it is the only viable design. The ChatGPT
OAuth refresh token *rotates on use*, so a second process holding a copy
revokes the first - TaskWraith's ``CodexOAuthCredentialLease`` calls copying
``auth.json`` "actively harmful" for exactly this reason. The decoded access
token also carries no inference scope (``openid, profile, email,
offline_access, api.connectors.read, api.connectors.invoke``), so a copied
token could not run a turn even if holding it were safe. Letting the Codex
runtime hold its own credential sidesteps both problems entirely.

Transport: ``thread/start`` -> ``turn/start`` -> delta notifications
-------------------------------------------------------------------
``codex exec --json`` was measured and rejected as the primary transport: a
trivial turn emits only five coarse NDJSON events (``thread.started``,
``item.completed``, ``turn.started``, ``item.completed``, ``turn.completed``),
so it cannot give token-level deltas. The app-server JSON-RPC protocol can, and
it is what this module uses. The methods and payload shapes below were
verified two ways against ``codex-cli 0.153.0``:

* against the runtime's own authoritative schema, via
  ``codex app-server generate-json-schema --out <dir>`` (definitions
  ``ThreadStartParams``, ``TurnStartParams``, ``AgentMessageDeltaNotification``,
  ``ReasoningTextDeltaNotification``, ``TurnCompletedNotification``); and
* live, by driving a real ``codex app-server`` over stdio under an isolated
  ``CODEX_HOME`` - handshake, ``model/list`` cursor paging, ``thread/start``,
  ``turn/start`` and the whole failure path were all observed on the wire.

Requests: ``initialize`` -> ``initialized`` -> ``thread/start`` ->
optional ``thread/inject_items`` -> ``turn/start``. Host instructions travel
as ``developerInstructions`` and host tools as namespaced ``dynamicTools``.
``item/tool/call`` ends the nested session with a host-visible tool handoff;
the next request restores the call and result as typed history items.
Notifications consumed:

``item/agentMessage/delta``    ``{"delta","itemId","threadId","turnId"}`` -> text_delta
``item/reasoning/textDelta``   ``{"delta","contentIndex",...}``          -> thinking_delta
``item/reasoning/summaryTextDelta`` ``{"delta","summaryIndex",...}``     -> thinking_delta
``item/completed``             ``{"item":{"type":"agentMessage","text"}}``-> fallback text
``thread/tokenUsage/updated``  ``{"tokenUsage":{"last":{...}},...}``      -> usage snapshot
``turn/completed``             ``{"turn":{"id","status","error"}}``       -> terminal
``error``                      ``{"error":{...},"willRetry",...}``        -> diagnostics

There is no ``turn/failed`` notification in 0.153.0: a failed turn arrives as
``turn/completed`` with ``turn.status == "failed"`` and a populated
``turn.error`` (``TurnStatus`` is ``completed|interrupted|failed|inProgress``).
``turn/failed`` is still tolerated because TaskWraith's older client handles it,
so a runtime upgrade cannot silently hang a thread.

The app-server runs against the user's real default ``CODEX_HOME``
(``~/.codex``): the child is spawned with no ``CODEX_HOME`` override, so the
CLI reads and refreshes the ChatGPT login it already owns at
``~/.codex/auth.json``. This module never opens that file. Safety still holds
because the real config is overridden on argv (see "The hub-loop hazard" and
"Read-only by construction") and the turn is a fresh, ephemeral, read-only
thread. The observed upstream URL is ``https://api.openai.com/v1/responses`` -
direct, never via the hub, and never a copied credential.

The coarse ``exec --json`` fallback
----------------------------------
:func:`run_turn_fallback` implements the documented coarse-grained fallback for
a runtime whose app-server turn streaming is unavailable: one ``text_delta``
per completed item rather than per token. It is opt-in and is not on the
default path.

The hub-loop hazard
-------------------
The hub writes a ``provider_hub`` ``model_provider`` into Codex configuration
pointing at ``http://127.0.0.1:<port>/v1``, and Codex Desktop points back at the
hub. A spawned runtime configured the same way would route its turn into the
hub, which would route it back into a spawned runtime - an unbounded recursion
burning the user's ChatGPT plan. The app-server argv therefore pins
``-c model_provider="openai"`` - the built-in ChatGPT provider - so the turn is
routed to ``https://api.openai.com/v1/responses`` even when the user's real
``~/.codex/config.toml`` has been pointed at the hub. The same override is
applied to the coarse ``exec`` fallback. When the real config's
``model_catalog_json`` has been pointed at the hub's catalog, the argv also
points it back at the CLI's own fetched OpenAI catalog so :func:`catalogue`
advertises models this route can actually run. Only then: every Codex client
on the machine rewrites that cache with the list the backend serves *its*
version, so with no configured catalog the runtime lists its own.

Nested process isolation and host permissions
---------------------------------------------
The nested sandbox does not describe host tool permissions. Workspace reads
and writes are forwarded to the host, which enforces its own settings. Native
shell, web, plugins, and multi-agent features are disabled; unexpected native
tool activity is an error, never silently discarded.

:data:`_FORBIDDEN_FLAGS` is asserted inside every argv builder, so a future
edit cannot introduce an approval bypass or a write-capable sandbox. Threads are
started ``sandbox="read-only"`` / ``approvalPolicy="never"`` / ``ephemeral``,
turns repeat ``approvalPolicy="never"`` and ``sandboxPolicy={"type":
"readOnly"}``, and the argv carries ``-c sandbox_mode="read-only"``,
``-c approval_policy="never"`` and ``-c model_provider="openai"``. A prompt
injected through message history therefore cannot make the runtime write or
route the turn back through the hub.
"""
from __future__ import annotations

import atexit
import hashlib
import json
import os
from collections.abc import Iterator
from pathlib import Path
import re
import select
import shutil
import subprocess
import tempfile
import time
import tomllib
import uuid

from cli_session import CliSessionError, StdioSession, minimal_env, resolve_binary
from codex_session_pool import SessionPool
from cli_lifecycle import cleanup_after_exit
from cli_tool_call import HOST_EXECUTION_NOTE, normalize_tools, validate_host_call
from cli_images import normalize_images, responses_content


PROVIDER_ID = "codex"
PROVIDER_NAME = "Codex (ChatGPT subscription)"
TRANSPORT = "app_server"
BINARY_NAMES = ("codex",)

#: The host policy uses a native instruction field, above user history.
SYSTEM_PROMPT_TRANSPORT = "developerInstructions"
HOST_TOOL_TRANSPORT = "dynamic"
NONBLOCKING_CLOSE = True
IMAGE_TRANSPORT = "native_history"

_NATIVE_TOOL_ITEMS = frozenset({
    "commandExecution", "fileChange", "mcpToolCall", "collabToolCall",
    "collabAgentToolCall", "webSearch", "imageView", "imageGeneration",
})

# Host tools live in their own namespace so a host's exec/apply_patch tool
# cannot collide with a built-in tool of the nested runtime.
_HOST_TOOL_NAMESPACE = "host"
_HOST_INSTRUCTIONS = HOST_EXECUTION_NOTE + (
    " Host tools are registered in the host namespace with bridge_ aliases. "
    "Each tool description identifies its original host name. Use the registered "
    "alias when calling it; the host still receives its original tool name. "
    "The bridge returns each call to the host and supplies its result in the "
    "next request's transcript. Use those results to continue the task."
)

#: The prompt that rejoins a turn after the host executed a tool call.
#: The transport is stateless per leg - a fresh process, thread and prompt for
#: every handoff - so this text is the only thing telling the model whether it
#: is starting work or resuming it. "Continue the user's task" read as the
#: former: the model re-oriented from scratch each leg, re-ran the checks its
#: own instructions open with, and re-announced the plan it had already given.
_RESUME_PROMPT = (
    "Resume the turn already in progress above. The host executed your tool call and "
    "its result is the last item in the conversation. You are mid-task, not starting: "
    "do not re-read context already shown above, do not repeat a command whose output "
    "is already above, and do not restate a plan or intention you have already given. "
    "Continue from where your own reasoning left off and take the next real step."
)

_TRANSPORT_CONFIG = (
    'features.shell_tool=false',
    # Current runtimes expose collaboration if any of these controls enables
    # it. The nested CLI must forward the host's tools, whose catalogue and
    # provider routing belong to the desktop, rather than spawn locally on
    # its pinned OpenAI provider. All three are needed on 0.155.0-alpha.9.2.
    'agents.enabled=false',
    'features.multi_agent=false',
    'features.multi_agent_v2=false',
    'features.apps=false',
    'features.plugins=false',
    'features.hooks=false',
    'web_search="disabled"',
)

#: The coarse transport :func:`run_turn_fallback` uses. Never the default.
FALLBACK_TRANSPORT = "exec_json"

CLIENT_NAME = "provider-hub-codex-cli"
CLIENT_VERSION = "0.5.3"

#: Asserted by every argv builder. An approval bypass or a write-capable
#: sandbox must not be expressible in this module, even by accident in a later
#: edit. ``--dangerously-bypass-hook-trust`` is included beyond the four the
#: experiment names: it is the same class of escape hatch and exists in
#: 0.153.0's ``codex exec --help``.
_FORBIDDEN_FLAGS = frozenset({
    "--dangerously-bypass-approvals-and-sandbox",
    "--approve-for-me",
    "--dangerously-bypass-hook-trust",
    "danger-full-access",
    "workspace-write",
})

#: What "read-only" looks like on each transport's argv. The app-server takes
#: its sandbox per thread, so the argv expresses it as a TOML override; the
#: exec fallback takes it as ``-s read-only``.
READ_ONLY_ARGV_MARKERS = ("-s", "read-only")
READ_ONLY_CONFIG_OVERRIDES = ('sandbox_mode="read-only"', 'approval_policy="never"')

#: A route id must be a plain model slug. Validating it before it is embedded
#: in a ``-c key="value"`` override is what makes TOML injection - and with it
#: a smuggled ``sandbox_mode="danger-full-access"`` - unexpressible, rather
#: than relying on the forbidden-flag assert to notice afterwards.
_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:+/-]{0,127}$")
_EFFORT_RE = re.compile(r"^[A-Za-z0-9._-]{1,32}$")

#: Pin the built-in ChatGPT provider. The user's real ``~/.codex/config.toml``
#: may name a hub provider (``provider_hub`` -> ``http://127.0.0.1:<port>/v1``,
#: written by this repo's other product); inheriting that would route the turn
#: back into the hub and recurse. This is the argv-side guard that keeps the
#: turn direct to ``https://api.openai.com/v1/responses``.
_MODEL_PROVIDER_OVERRIDE = 'model_provider="openai"'

#: ``cli_auth_credentials_store="file"`` tells the CLI to read and refresh its
#: credentials from ``auth.json`` under its own ``CODEX_HOME``. Because the
#: child is spawned with no ``CODEX_HOME`` override, that is the user's real
#: ``~/.codex`` - exactly the login ``codex login`` wrote. The hub never opens
#: that file.
_CREDENTIALS_STORE_OVERRIDE = 'cli_auth_credentials_store="file"'

#: ``TurnStatus`` -> hub ``stop_reason``. ``interrupted`` is reported as itself
#: rather than folded into ``end_turn``: a cancelled turn is not a finished one
#: and the harness should be able to tell them apart.
_STOP_REASONS = {"completed": "end_turn", "interrupted": "interrupted"}

#: A turn's whole budget is split across its phases so no single phase can eat
#: it and leave nothing for the stream. Proportions, not absolutes.
_HANDSHAKE_SHARE = 0.15
_START_SHARE = 0.15

#: Cap on outer stream iterations. ``events()`` blocks for up to its timeout, so
#: this is a guard against an implementation that returns instantly and empty,
#: not a limit on turn length - the deadline does that.
_MAX_STREAM_LOOPS = 20000

#: Cursor pages tolerated from ``model/list`` before we call it a runaway.
_MAX_CATALOGUE_PAGES = 12

#: Env var that captures runtime stderr to a file in the disposable scratch dir
#: for a developer debugging a failed turn. Off by default: stderr goes to
#: DEVNULL and the scratch dir is deleted.
_DIAGNOSTICS_ENV = "PROVIDER_HUB_CODEX_CLI_DIAGNOSTICS"

#: Opt-in JSONL sink for per-handoff telemetry, set to a path. The workspace
#: diagnostics log is deleted with its workspace, so it cannot answer a
#: question asked across a whole task; this one outlives the turn on purpose.
_TURN_LOG_ENV = "PROVIDER_HUB_CODEX_CLI_TURN_LOG"

#: Never parsed as protocol. Codex writes noisy WARN/ERROR lines here -
#: ``codex_skills::interface: ignoring interface.icon_...``, websocket 401s -
#: and a protocol parser that read stderr would mis-frame on them.
_DEVNULL = subprocess.DEVNULL


class CodexCliAgentError(RuntimeError):
    """The Codex CLI route could not be resolved, started or completed."""

    def __init__(self, message, *, http_status=502):
        super().__init__(message)
        self.http_status = http_status


def _tool_alias(name):
    """Stable wire name, independent of tool order and Codex's reserved names.

    All tools are mapped so a host tool named like one of our aliases cannot
    collide with another tool. History uses the same mapping on every turn.
    """
    return "bridge_" + hashlib.sha256(name.encode("utf-8")).hexdigest()[:48]


# --------------------------------------------------------------------------
# argv construction
# --------------------------------------------------------------------------

def _assert_safe_argv(argv, *, context):
    """Refuse any argv carrying an approval bypass or a write-capable sandbox.

    Run on the *final* argv, after every interpolation, so it checks what will
    actually be executed rather than the template it was built from.
    """
    parts = [str(part) for part in argv]
    joined = " ".join(parts).lower()
    hits = sorted({flag for flag in _FORBIDDEN_FLAGS if flag.lower() in joined})
    if hits:
        raise CodexCliAgentError(
            f"Refusing to build a {context} argv containing {', '.join(hits)}. "
            "This route is read-only by construction.")
    return parts


def _toml_string(value):
    """Encode ``value`` as a TOML basic string.

    JSON's string grammar is a subset of TOML's basic strings, so ``json.dumps``
    is a correct encoder and quotes/backslashes/newlines cannot terminate the
    literal early.
    """
    return json.dumps(str(value))


def _checked_model(model):
    text = str(model or "").strip()
    if not _MODEL_RE.match(text):
        raise CodexCliAgentError(
            f"'{text[:80]}' is not a usable Codex model id. Expected a plain slug "
            "such as 'gpt-5.6-sol'.")
    return text


def _checked_effort(effort):
    """A validated reasoning effort, or None.

    The protocol types ``ReasoningEffort`` as any non-empty string, and the
    installed catalogue advertises ``low|medium|high|xhigh|max|ultra``, so the
    set is not hardcoded here: an unknown-but-well-formed effort is passed
    through and the runtime rejects it with a readable error instead of this
    module silently dropping the user's choice.
    """
    if effort is None:
        return None
    text = str(effort).strip()
    if not text:
        return None
    if not _EFFORT_RE.match(text):
        raise CodexCliAgentError(f"'{text[:40]}' is not a usable reasoning effort.")
    return text


def runtime_binary(binary=None):
    """The runtime to spawn: an explicit path, ``codex`` on PATH, or the app's.

    Mirrors ``codex_runtime.runtime_binary``'s desktop-app resolution
    (``<app>/Contents/Resources/codex``) as the fallback, but prefers PATH
    first: this route is CLI-backed, so the binary the user actually invokes is
    the one whose login they manage.
    """
    if binary is not None:
        path = Path(binary)
        if not path.is_file() or not os.access(path, os.X_OK):
            raise CodexCliAgentError(f"'{path}' is not an executable Codex runtime.")
        return str(path)
    resolved = resolve_binary(BINARY_NAMES)
    if resolved:
        return str(resolved)
    for candidate in _desktop_runtime_candidates():
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    raise CodexCliAgentError(
        "No Codex runtime was found. Install the Codex CLI, or the ChatGPT / "
        "Codex desktop app whose bundled runtime this route can use.")


def _desktop_runtime_candidates():
    """The desktop apps' bundled runtimes, in preference order."""
    roots = (Path("/Applications"), Path.home() / "Applications")
    return tuple(root / f"{name}.app/Contents/Resources/codex"
                 for root in roots for name in ("ChatGPT", "Codex"))


def runtime_signature(binary=None):
    """Stable hash of the runtime's identity.

    Same recipe as ``codex_runtime.runtime_signature`` - sha256 over
    ``[path, st_dev, st_ino, st_size, st_mtime_ns]`` - so the two modules agree
    on what "the same runtime" means. Any of those changing (an in-place
    upgrade, a replaced file) changes the signature.
    """
    path = Path(binary) if binary is not None else Path(runtime_binary())
    try:
        stat = path.stat()
    except OSError as exc:
        raise CodexCliAgentError(f"'{path}' could not be stat'd: {exc}") from exc
    payload = json.dumps([str(path), stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns])
    return hashlib.sha256(payload.encode()).hexdigest()


def _pinned_binary():
    """An env-pinned runtime path, for tests and for a non-standard install."""
    return os.environ.get("PROVIDER_HUB_CODEX_BINARY") or None


def _models_cache():
    """The runtime's own fetched catalog, in the real home the child reads."""
    return Path.home() / ".codex" / "models_cache.json"


def _configured_catalog():
    """Whether the real Codex config selects a ``model_catalog_json``.

    The hub writes one at the root for a desktop session; a user profile may
    carry its own. A config that cannot be read counts as configured, so the
    neutralization is only ever skipped when there is provably nothing to fight.
    """
    try:
        config = tomllib.loads((Path.home() / ".codex" / "config.toml").read_text(encoding="utf-8"))
    except FileNotFoundError:
        return False
    except (OSError, ValueError):
        return True
    if "model_catalog_json" in config:
        return True
    profile, profiles = config.get("profile"), config.get("profiles")
    table = profiles.get(profile) if isinstance(profile, str) and isinstance(profiles, dict) else None
    return isinstance(table, dict) and "model_catalog_json" in table


def _openai_catalog_override():
    """Neutralize a configured ``model_catalog_json`` with the CLI's own fetched catalog.

    The hub preview points ``model_catalog_json`` in ``~/.codex/config.toml`` at
    its own catalog; that would make :func:`catalogue` advertise hub models that
    the OpenAI provider cannot run. The CLI's own fetched catalog cache
    (``~/.codex/models_cache.json``) is the closest stable stand-in for the
    built-in catalog and lives in the real home the child already reads from.
    We only point at it when it parses as a non-empty catalog, so a missing or
    half-written cache cannot brick the app-server; in that case we leave the
    catalog alone (a machine that has never listed models has no hub override to
    fight).

    It is only a stand-in, so it is used only when a configured catalog needs
    neutralizing. Every Codex client on the machine rewrites that cache with the
    list the backend serves its own version: an app-server left running across
    an upgrade keeps overwriting it without models this runtime can serve (seen
    live: a 0.153.0 app-server dropped gpt-6-sol and gpt-6-luna from a 0.155.1
    route and blocked launch). With no configured catalog the runtime lists, and
    re-caches, its own.

    Returns ``("model_catalog_json", <path>)`` when a configured catalog needs
    neutralizing and the cache is usable, else None.
    """
    if not _configured_catalog():
        return None
    cache = _models_cache()
    try:
        if not cache.is_file():
            return None
        payload = json.loads(cache.read_text(encoding="utf-8"))
        if isinstance(payload, dict) and isinstance(payload.get("models"), list) and payload["models"]:
            return ("model_catalog_json", str(cache))
    except (OSError, ValueError):
        pass
    return None


def _app_server_argv(*, model=None, effort=None, binary=None):
    """The app-server argv, with model/effort as optional TOML overrides.

    Read-only is expressed three times over - as ``-c`` overrides here, as
    ``thread/start`` params, and as ``turn/start`` params - because each is
    enforced at a different layer and none of them trusts the others. The
    provider is also pinned to the built-in ChatGPT ``openai`` provider here so
    a hub-written ``model_provider`` in the real config cannot loop the turn
    back through the hub.
    """
    checked_model = _checked_model(model) if model is not None else None
    checked_effort = _checked_effort(effort)
    argv = [runtime_binary(binary or _pinned_binary()),
            "-c", _CREDENTIALS_STORE_OVERRIDE,
            "-c", _MODEL_PROVIDER_OVERRIDE,
            "-c", 'sandbox_mode="read-only"',
            "-c", 'approval_policy="never"']
    for setting in _TRANSPORT_CONFIG:
        argv += ["-c", setting]
    catalog = _openai_catalog_override()
    if catalog is not None:
        argv += ["-c", f"{catalog[0]}={_toml_string(catalog[1])}"]
    if checked_model is not None:
        argv += ["-c", f"model={_toml_string(checked_model)}"]
    if checked_effort is not None:
        argv += ["-c", f"model_reasoning_effort={_toml_string(checked_effort)}"]
    argv.append("app-server")
    return _assert_safe_argv(argv, context="app-server")


def build_argv(model, *, effort=None, system=None, stream=True):
    """The app-server argv: the primary, delta-streaming transport.

    ``system`` and ``stream`` are accepted so the hub can call every CLI
    adapter with the same signature. They are ignored here because the
    system prompt travels in thread/start developerInstructions, and the
    Codex app-server streams by design.
    """
    return _app_server_argv(model=model, effort=effort, binary=_pinned_binary())


def build_exec_argv(model, *, effort=None):
    """The ``exec --json`` argv: the documented coarse fallback transport.

    Carries ``-s read-only`` literally, plus ``--skip-git-repo-check`` (the
    scratch cwd is not a repository) and ``--ephemeral`` (no thread persisted
    into the real home). It also carries the same auth-store, provider and
    approval overrides as the app-server so the fallback stays read-only and
    never loops back through the hub. The prompt is appended by the caller and
    the finished argv is re-asserted, so the forbidden-flag check always sees
    what will actually be executed.
    """
    checked_model = _checked_model(model)
    checked_effort = _checked_effort(effort)
    argv = [runtime_binary(_pinned_binary()), "exec",
            "-c", _CREDENTIALS_STORE_OVERRIDE,
            "-c", _MODEL_PROVIDER_OVERRIDE,
            "-c", 'approval_policy="never"',
            "-s", "read-only",
            "--skip-git-repo-check",
            "--ephemeral",
            "--json",
            "-m", checked_model]
    for setting in _TRANSPORT_CONFIG:
        argv += ["-c", setting]
    if checked_effort is not None:
        argv += ["-c", f"model_reasoning_effort={_toml_string(checked_effort)}"]
    return _assert_safe_argv(argv, context="exec")


# --------------------------------------------------------------------------
# read-only capability probes
# --------------------------------------------------------------------------

def default_capture(argv, *, timeout=25):
    """The production capture: run one argv, bounded, no stdin, scrubbed env.

    Same contract as ``cli_auth_probe.default_capture`` - ``capture(argv, *,
    timeout) -> (returncode, stdout, stderr)`` - so one capture can be wired
    across every CLI adapter. ``stdin=DEVNULL`` is load-bearing: an inherited
    terminal is the difference between a probe and a session. ``check=False``
    because a non-zero status is an answer (a signed-out CLI says so by exiting
    1), not an exception.
    """
    binary = resolve_binary((str(argv[0]),)) or str(argv[0])
    completed = subprocess.run([binary, *[str(part) for part in argv[1:]]],
                               capture_output=True, text=True, timeout=timeout,
                               env=minimal_env(), stdin=_DEVNULL, check=False)
    return completed.returncode, completed.stdout, completed.stderr


def discover(*, capture=None) -> dict:
    """Whether a Codex runtime is installed, where, and what version.

    Never raises: a missing or wedged CLI is one row in a settings pane, and
    ``installed: False`` is the honest answer rather than an exception that
    takes the whole pane down.
    """
    runner = default_capture if capture is None else capture
    injected = capture is not None
    if injected:
        # An injected capture means the caller has taken responsibility for
        # running things; walking the real PATH would report the host's binary
        # instead of the facts we were handed.
        binary: str | None = BINARY_NAMES[0]
    else:
        try:
            binary = runtime_binary(_pinned_binary())
        except (CodexCliAgentError, OSError, ValueError):
            binary = None
    if binary is None:
        return {"installed": False, "binary": None, "version": None}
    version = None
    answered = False
    try:
        rc, stdout, _stderr = runner([binary, "--version"], timeout=25)
        answered = int(rc) == 0
        if answered:
            version = _parse_version(stdout)
    except subprocess.TimeoutExpired:
        answered = False
    except Exception:  # noqa: BLE001 - discovery must never raise at the hub
        answered = False
    if not answered:
        return {"installed": False, "binary": None, "version": None}
    return {"installed": True, "binary": str(binary), "version": version}


def _parse_version(stdout):
    """``codex-cli 0.153.0`` -> ``0.153.0``, tolerating other phrasings."""
    text = str(stdout or "").strip()
    if not text:
        return None
    match = re.search(r"(\d+\.\d+(?:\.\d+)?(?:[-+][0-9A-Za-z.-]+)?)", text)
    return match.group(1) if match else text.splitlines()[0][:60]


#: ``codex login status`` phrasings observed on 0.153.0.
_AUTHENTICATED_MARKERS = ("logged in", "signed in")
_MISSING_MARKERS = ("not logged in", "logged out", "not signed in", "signed out")


def auth_state(*, capture=None) -> dict:
    """Sign-in state, from ``codex login status`` and nothing else.

    Deliberately runs against the user's *real* ``~/.codex`` (via ``HOME`` in
    :func:`minimal_env`) with no ``CODEX_HOME`` override, so it reports the
    login the turn will actually use. Running it is a read: the CLI prints its
    state and exits 0 or 1.

    Following the repo's doctrine on markers, a status we cannot parse is
    *absence of evidence*, not evidence of absence: it is reported ``unknown``.
    Only a non-zero exit paired with a recognised signed-out phrasing - or a
    recognised signed-in line - is treated as an answer.
    """
    runner = default_capture if capture is None else capture
    try:
        binary = BINARY_NAMES[0] if capture is not None else runtime_binary()
    except (CodexCliAgentError, OSError, ValueError):
        return {"state": "unknown", "detail": "No Codex runtime was found to ask."}
    try:
        rc, stdout, stderr = runner([binary, "login", "status"], timeout=25)
    except subprocess.TimeoutExpired:
        return {"state": "unknown", "detail": "`codex login status` did not answer within 25s."}
    except Exception as exc:  # noqa: BLE001 - a probe must never raise at the hub
        return {"state": "unknown",
                "detail": f"`codex login status` could not be run: {exc.__class__.__name__}: {exc}"}
    detail = str(stdout or "").strip() or str(stderr or "").strip()
    lowered = detail.lower()
    if not lowered:
        return {"state": "unknown", "detail": f"`codex login status` printed nothing (rc={rc})."}
    # Order matters: "Not logged in" contains "logged in" as a substring, so
    # the negative markers are tested first.
    if any(marker in lowered for marker in _MISSING_MARKERS):
        return {"state": "missing", "detail": detail.splitlines()[0][:200]}
    if any(marker in lowered for marker in _AUTHENTICATED_MARKERS) and rc == 0:
        return {"state": "authenticated", "detail": detail.splitlines()[0][:200]}
    return {"state": "unknown",
            "detail": f"`codex login status` answered unrecognised text (rc={rc}): "
                      f"{detail.splitlines()[0][:160]}"}


# --------------------------------------------------------------------------
# catalogue
# --------------------------------------------------------------------------

def _row(raw):
    """One ``model/list`` row, normalised so the documented fields always exist.

    Field names are the runtime's own camelCase - the same ones
    ``codex_runtime.py:102-121`` compares against - so the hub's generic wiring
    and the catalogue qualifier agree on the shape. ``project_codex`` is
    deliberately *not* used: it projects the hub's outbound catalogue and needs
    a full settings object plus provider inventory, whereas a CLI-backed route
    should advertise what the user's own Codex actually offers.
    """
    if not isinstance(raw, dict):
        return None
    model = raw.get("model") or raw.get("id")
    if not isinstance(model, str) or not model:
        return None
    efforts = []
    for entry in raw.get("supportedReasoningEfforts") or []:
        if isinstance(entry, dict) and entry.get("reasoningEffort"):
            efforts.append({"reasoningEffort": str(entry["reasoningEffort"]),
                            "description": str(entry.get("description") or "")})
        elif isinstance(entry, str) and entry:
            efforts.append({"reasoningEffort": entry, "description": ""})
    tiers = []
    for entry in raw.get("serviceTiers") or []:
        if isinstance(entry, dict) and entry.get("id"):
            tiers.append({"id": str(entry["id"]), "name": str(entry.get("name") or ""),
                          "description": str(entry.get("description") or "")})
    return {"model": model,
            "id": str(raw.get("id") or model),
            "displayName": str(raw.get("displayName") or model),
            "description": str(raw.get("description") or ""),
            "hidden": raw.get("hidden") is True,
            "isDefault": raw.get("isDefault") is True,
            "defaultReasoningEffort": raw.get("defaultReasoningEffort"),
            "supportedReasoningEfforts": efforts,
            "serviceTiers": tiers,
            "multiAgentVersion": raw.get("multiAgentVersion"),
            "inputModalities": [str(m) for m in (raw.get("inputModalities") or []) if isinstance(m, str)]}


def parse_model_list(rows):
    """Normalise raw ``model/list`` rows into catalogue rows, dropping junk."""
    parsed = []
    for raw in rows or []:
        entry = _row(raw)
        if entry is not None:
            parsed.append(entry)
    return parsed


def _catalogue_context(argv, configured_window=None):
    """Context budgets from the same native catalogue this runtime listed.

    model/list omits context metadata. Join its exact model IDs to the native
    catalogue selected on argv, or, with none selected, the runtime's own cache
    that the listing refreshed; never the Hub's projected rows or a model-name
    guess. max_context_window is an optional larger ceiling, not the active
    default. Preserve the runtime's reserved percentage in runtime_context.
    """
    path = _models_cache()
    for flag, value in zip(argv, argv[1:]):
        if flag == "-c" and value.startswith("model_catalog_json="):
            try:
                path = Path(json.loads(value.split("=", 1)[1]))
            except (ValueError, TypeError):
                return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    cards = payload.get("models") if isinstance(payload, dict) else None
    if not isinstance(cards, list):
        return {}
    result = {}
    for card in cards:
        if not isinstance(card, dict) or not isinstance(card.get("slug"), str):
            continue
        window = configured_window if type(configured_window) is int and configured_window > 0 else card.get("context_window")
        percent = card.get("effective_context_window_percent")
        if type(window) is not int or window <= 0 or type(percent) is not int or not 0 < percent <= 100:
            continue
        effective = window * percent // 100
        if effective > 0:
            result[card["slug"]] = {"context": window, "runtime_context": effective,
                                     "context_kind": "runtime_catalogue", "context_evidence": str(path)}
    return result


def _bounded_timeout(value, *, default, minimum=1.0):
    try:
        budget = float(value)
    except (TypeError, ValueError):
        return float(default)
    if budget != budget or budget in (float("inf"), float("-inf")):  # NaN / inf
        return float(default)
    return max(minimum, budget)


def _result(response, *, context):
    """Unwrap a JSON-RPC response, or raise with the server's own message.

    Tolerant of both conventions a ``request()`` implementation might use -
    returning the bare ``result`` payload, or the whole envelope - because the
    session host is a sibling module written in parallel.
    """
    if isinstance(response, dict):
        error = response.get("error")
        if isinstance(error, dict) and error:
            message = error.get("message") or json.dumps(error, sort_keys=True)[:300]
            code = error.get("code")
            suffix = f" (code {code})" if code is not None else ""
            raise CodexCliAgentError(f"{context}: the Codex runtime reported an error{suffix}: {message}",
                                    http_status=400 if code in {-32600, -32601, -32602} else 502)
        if "result" in response:
            result = response["result"]
            return result if isinstance(result, dict) else {}
        return response
    raise CodexCliAgentError(f"{context}: the Codex runtime returned a malformed response.")


def fetch_models(*, binary=None, spawner=None, timeout=30):
    """Drive ``model/list`` over the app-server, paging cursors to exhaustion.

    Cursor paging is real and was observed live: with ``limit=1`` the runtime
    returned ``nextCursor`` ``'1'``, ``'2'``, ... and finally ``null``.
    """
    budget = _bounded_timeout(timeout, default=30)
    workspace = CodexTurnWorkspace()
    session = None
    try:
        workspace.open()
        argv = _app_server_argv(binary=binary)
        session = _open_session(argv, workspace, spawner=spawner, timeout=budget)
        _handshake(session, timeout=budget * _HANDSHAKE_SHARE)
        config = None
        try:
            configured = _result(session.request("config/read", {"includeLayers": False},
                                                 timeout=max(1.0, budget * 0.5)), context="config/read")
            config = configured.get("config") or {}
        except (CodexCliAgentError, CliSessionError):
            # An optional capacity probe must not hide otherwise routable
            # models. Without resolved configuration, leave capacity unknown.
            pass
        rows, cursor = [], None
        for page in range(_MAX_CATALOGUE_PAGES):
            response = session.request("model/list",
                                       {"includeHidden": True, "limit": 1000, "cursor": cursor},
                                       timeout=max(1.0, budget * 0.5))
            result = _result(response, context="model/list")
            data = result.get("data")
            if not isinstance(data, list):
                raise CodexCliAgentError("The Codex runtime returned an invalid model catalogue.")
            rows.extend(data)
            cursor = result.get("nextCursor")
            if not cursor:
                break
        else:
            raise CodexCliAgentError("Codex returned too many catalogue pages.")
        # Joined after listing: with no catalog on argv, the listing is what
        # refreshes the runtime's own cache for its version.
        context = (_catalogue_context(argv, config.get("model_context_window"))
                   if isinstance(config, dict) else {})
        return [{**row, **context.get(row["model"], {})} for row in parse_model_list(rows)]
    finally:
        _teardown(session, workspace)


def catalogue(*, capture=None, spawner=None, timeout=30) -> tuple[list[dict], list[str]]:
    """The installed runtime's own model rows, plus human-readable notes.

    ``spawner`` is accepted in addition to the specified ``capture``/``timeout``
    because ``model/list`` is a JSON-RPC call over a long-lived stdio session,
    which the short-lived ``capture(argv, *, timeout) -> (rc, out, err)``
    contract cannot express. ``capture`` is still honoured for the binary
    label, mirroring ``cli_auth_probe._binary_label``: an injected capture
    means the caller owns execution, so the real PATH is not walked.

    Degrades rather than raising: an absent runtime or a wedged session returns
    ``([], [reason])`` so the hub's settings pane can say why the row is empty.
    """
    notes: list[str] = []
    if capture is None and spawner is None:
        try:
            runtime_binary()
        except (CodexCliAgentError, OSError, ValueError) as exc:
            return [], [f"No Codex runtime is available: {exc}"]
    try:
        rows = fetch_models(spawner=spawner, timeout=timeout)
    except CodexCliAgentError as exc:
        return [], [str(exc)]
    except CliSessionError as exc:
        return [], [f"The Codex app-server session failed: {exc}"]
    except Exception as exc:  # noqa: BLE001 - a catalogue probe must never raise
        return [], [f"The Codex catalogue could not be read: {exc.__class__.__name__}: {exc}"]
    if not rows:
        notes.append("The Codex runtime advertised no models.")
    visible = [row for row in rows if not row["hidden"]]
    if visible and len(visible) != len(rows):
        notes.append(f"{len(rows) - len(visible)} model(s) are hidden from the default picker.")
    return rows, notes


# --------------------------------------------------------------------------
# the disposable scratch cwd (the real CODEX_HOME is left untouched)
# --------------------------------------------------------------------------

class CodexTurnWorkspace:
    """A disposable scratch dir (cwd + optional diagnostics), created per turn.

    This is *not* a ``CODEX_HOME`` and it writes nothing into the user's real
    ``~/.codex``. The child is spawned with no ``CODEX_HOME`` override, so the
    CLI resolves its own real home and reads the ChatGPT login it owns there;
    this class supplies only the two things a turn genuinely needs per-process:
    a scratch working directory for the read-only thread, and - when
    diagnostics are on - a stderr log in the temp tree rather than in the real
    home. Cleanup is idempotent so it can be called from a ``finally`` and from
    a context-manager exit without racing.
    """

    def __init__(self, *, prefix="provider-hub-codex-cli-", diagnostics=None):
        self.prefix = prefix
        self.diagnostics = diagnostics
        self.base: Path | None = None
        self.cwd: Path | None = None
        self.diagnostics_path: Path | None = None
        self._diagnostics_handle = None

    def open(self) -> "CodexTurnWorkspace":
        base = Path(tempfile.mkdtemp(prefix=self.prefix))
        try:
            base.chmod(0o700)
            self.base = base
            self.cwd = base / "work"
            self.cwd.mkdir(mode=0o700)
            if self.diagnostics:
                self.diagnostics_path = base / "diagnostics.log"
            return self
        except BaseException:
            self.close()
            raise

    def env(self):
        """The child's environment: the shared minimal allowlist, no CODEX_HOME.

        Leaving ``CODEX_HOME`` unset is the point: the CLI then uses its real
        default ``~/.codex`` and reads the login it owns there.
        """
        if self.base is None:
            raise CodexCliAgentError("The Codex turn workspace was never created.")
        return minimal_env()

    def close(self):
        """Release the diagnostics handle, then delete the whole tree.

        Idempotent, and safe to call from both a ``finally`` and a
        context-manager exit. The handle must outlive the session because
        ``StdioSession`` may hold the file object rather than a dup'd descriptor.
        """
        handle, self._diagnostics_handle = self._diagnostics_handle, None
        if handle is not None:
            try:
                handle.close()
            except OSError:
                pass
        base, self.base = self.base, None
        self.cwd = self.diagnostics_path = None
        if base is None:
            return
        shutil.rmtree(base, ignore_errors=True)

    def __enter__(self):
        return self.open()

    def __exit__(self, exc_type, exc, traceback):
        self.close()
        return False


def _diagnostics_enabled():
    return bool(os.environ.get(_DIAGNOSTICS_ENV))


def _open_session(argv, workspace, *, spawner=None, timeout=120.0):
    """Start the app-server, with stderr kept out of the protocol channel."""
    stderr = _DEVNULL
    if workspace.diagnostics_path is not None:
        # Owned by the workspace, not by this function: it has to stay open for
        # as long as the session may write to it, and workspace.close() reaps it.
        workspace._diagnostics_handle = workspace.diagnostics_path.open("w", encoding="utf-8")
        stderr = workspace._diagnostics_handle
    try:
        return StdioSession(list(argv), env=workspace.env(), cwd=str(workspace.cwd),
                            timeout=timeout, spawner=spawner, stderr=stderr)
    except BaseException:
        workspace.close()
        raise


# --------------------------------------------------------------------------
# turn phases
# --------------------------------------------------------------------------

def _handshake(session, *, timeout):
    """``initialize`` then ``initialized``, exactly as ``codex_runtime.py`` does.

    ``experimentalApi`` is what unlocks the v2 thread/turn surface; without it
    the runtime advertises only the legacy methods.
    """
    response = session.request("initialize",
                              {"clientInfo": {"name": CLIENT_NAME, "version": CLIENT_VERSION},
                               "capabilities": {"experimentalApi": True}},
                              timeout=max(1.0, timeout))
    _result(response, context="initialize")
    session.notify("initialized", {})


def _thread_params(payload, workspace):
    params = {
        "cwd": str(workspace.cwd),
        "model": payload["model"],
        "sandbox": "read-only",
        "approvalPolicy": "never",
        "ephemeral": True,
        "developerInstructions": payload.get("system") or _HOST_INSTRUCTIONS,
    }
    if payload.get("tools"):
        params["dynamicTools"] = [{
            "type": "namespace", "name": _HOST_TOOL_NAMESPACE,
            "description": "Tools executed by the host application under its permissions.",
            "tools": [{"type": "function", "name": _tool_alias(tool["name"]),
                       "description": f"Host tool: {tool['name']}.\n{tool['description']}",
                       "inputSchema": tool["input_schema"]}
                      for tool in payload["tools"]],
        }]
    if payload["effort"] is not None:
        params["config"] = {"model_reasoning_effort": payload["effort"]}
    return params


def _inherited_mcp_overrides(session, *, timeout):
    # Config tables merge: mcp_servers={} does NOT clear inherited servers.
    # Ask the runtime for names only and disable each at thread scope. Never
    # copy credentials/configuration into the host request or diagnostics.
    configured = _result(session.request("config/read", {"includeLayers": False},
                                         timeout=max(1.0, timeout)), context="config/read")
    servers = (configured.get("config") or {}).get("mcp_servers") or {}
    return {name: {"enabled": False} for name in servers}


def _start_thread(session, payload, workspace, *, timeout, servers=None):
    params = _thread_params(payload, workspace)
    if servers is None:
        servers = _inherited_mcp_overrides(session, timeout=timeout)
    if servers:
        params.setdefault("config", {})["mcp_servers"] = servers
    response = session.request("thread/start", params,
                               timeout=max(1.0, timeout))
    result = _result(response, context="thread/start")
    thread = result.get("thread")
    thread_id = thread.get("id") if isinstance(thread, dict) else None
    if not isinstance(thread_id, str) or not thread_id:
        raise CodexCliAgentError("thread/start returned no thread id.")
    return thread_id


def _turn_params(payload, thread_id):
    """The ``turn/start`` request.

    The whole conversation is rendered into one text input because the route is
    stateless per turn: no ``thread/resume``, no reliance on a session the
    desktop app might have moved. ``sandboxPolicy`` uses the protocol's
    ``readOnly`` variant and ``networkAccess: false`` so the turn repeats the
    read-only posture the thread was started with.

    ``max_tokens`` has no counterpart in ``TurnStartParams`` (its fields are
    ``approvalPolicy, approvalsReviewer, clientUserMessageId, cwd, effort,
    input, model, outputSchema, personality, sandboxPolicy, serviceTier,
    serviceTierForTurn, summary, threadId, toolOutput, turnTrigger``), so it is
    accepted and deliberately not forwarded - the runtime owns its own budget.
    """
    params = {
        "threadId": thread_id,
        "input": [{"type": "text", "text": payload["prompt"], "text_elements": []}],
        "approvalPolicy": "never",
        "sandboxPolicy": {"type": "readOnly", "networkAccess": False},
    }
    if payload.get("images") and not payload.get("history"):
        for part in responses_content(payload["images"]):
            params["input"].append({"type": "image", "url": part["image_url"],
                                    **({"detail": part["detail"]} if "detail" in part else {})})
    if payload["model"] is not None:
        params["model"] = payload["model"]
    if payload["effort"] is not None:
        params["effort"] = payload["effort"]
    if payload.get("reasoning_summary") is not None:
        params["summary"] = payload["reasoning_summary"]
    if payload.get("service_tier") is not None:
        params["serviceTier"] = payload["service_tier"]
    return params


def _start_turn(session, payload, thread_id, *, timeout):
    response = session.request("turn/start", _turn_params(payload, thread_id),
                               timeout=max(1.0, timeout))
    result = _result(response, context="turn/start")
    turn = result.get("turn")
    turn_id = turn.get("id") if isinstance(turn, dict) else None
    return turn_id if isinstance(turn_id, str) and turn_id else None


_ROLE_RE = re.compile(r"[^a-z]")


def render_prompt(request):
    """Render a whole conversation into one deterministic text prompt.

    The app-server takes a flat ``input`` list, not an OpenAI-style messages
    array, and this route is stateless per turn - so the history is folded into
    a single transcript with role tags. Only the trailing user turn is the
    instruction; everything before it is context.
    """
    blocks = []
    messages = request.get("messages")
    if not isinstance(messages, list):
        messages = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = _ROLE_RE.sub("", str(message.get("role") or "user").lower()) or "user"
        content = message.get("content")
        if isinstance(content, list):
            content = "\n".join(str(part.get("text", "")) if isinstance(part, dict) else str(part)
                                for part in content)
        text = str(content or "").strip()
        if not text:
            continue
        blocks.append(f"<{role}>\n{text}\n</{role}>")
    if not blocks:
        raise CodexCliAgentError("The request carried no message to send.")
    return "\n\n".join(blocks)


def _normalise_request(request):
    """Validate and freeze one turn request into what the phases need."""
    if not isinstance(request, dict):
        raise CodexCliAgentError("A turn request must be a dict.")
    model = request.get("model")
    if not isinstance(model, str) or not model.strip():
        raise CodexCliAgentError("A turn request must name a model.")
    prompt = render_prompt(request)
    images = normalize_images(request.get("images", []))
    system = request.get("system")
    if system is not None and not isinstance(system, str):
        raise CodexCliAgentError("system must be a string or None")
    instructions = _HOST_INSTRUCTIONS + ("\n\n" + system.strip() if system else "")
    choice = request.get("tool_choice") or {}
    tools = normalize_tools(request.get("tools"))
    summary = request.get("reasoning_summary")
    thinking = request.get("thinking") or {}
    if summary is None and isinstance(thinking, dict) and thinking.get("display") == "summarized":
        summary = "auto"
    if summary is not None and summary not in {"auto", "concise", "detailed"}:
        raise CodexCliAgentError("Unsupported Codex reasoning summary mode.", http_status=400)
    tier = request.get("service_tier")
    if tier is not None:
        if not isinstance(tier, str) or tier not in {"auto", "default", "standard", "fast", "priority", "flex", "ultrafast"}:
            raise CodexCliAgentError("Unsupported Codex service tier.", http_status=400)
        tier = {"standard": "default", "priority": "fast"}.get(tier, tier)
    if choice.get("type") == "none":
        tools = []
    elif choice.get("type") in {"any", "required"}:
        instructions += "\nCall at least one host tool in this reply."
    elif choice.get("type") == "tool":
        name = choice.get("name")
        if name not in {tool["name"] for tool in tools}:
            raise CodexCliAgentError("The required host tool was not offered.", http_status=400)
        instructions += f"\nCall host.{_tool_alias(name)} (host tool {name}) in this reply."
    return {"model": _checked_model(model),
            "effort": _checked_effort(request.get("effort")),
            "prompt": prompt, "system": instructions, "tools": tools,
            "history": request.get("history"), "images": images, "reasoning_summary": summary,
            "service_tier": tier}


def _history_items(messages):
    """Preserve role, reasoning and call/result identity in a stateless session."""
    items = []
    for message in messages:
        role = message.get("role")
        if role not in {"user", "assistant"}:
            continue
        content = message.get("content")
        blocks = [{"type": "text", "text": content}] if isinstance(content, str) else content or []
        for block in blocks:
            if not isinstance(block, dict):
                continue
            kind = block.get("type")
            if kind == "text" and block.get("text"):
                item = {"type": "message", "role": role, "content": [
                    {"type": "output_text" if role == "assistant" else "input_text",
                     "text": block["text"]}]}
                if role == "assistant":
                    phase = block.get("phase") or message.get("phase")
                    if phase not in {"commentary", "final_answer"}:
                        phase = ("commentary" if any(b.get("type") == "tool_use"
                                 for b in blocks if isinstance(b, dict)) else "final_answer")
                    item["phase"] = phase
                items.append(item)
            elif kind in {"thinking", "redacted_thinking"} and role == "assistant":
                # Summary-only reasoning acquires a local rs_ id in Codex, but
                # store:false cannot resolve it upstream. The Messages surface
                # carries readable text, not OpenAI's encrypted reasoning state.
                if block.get("signature"):
                    continue
                thought = block.get("thinking")
                if isinstance(thought, str) and thought.strip():
                    items.append({"type": "message", "role": "assistant",
                                  "phase": "commentary", "content": [{
                                      "type": "output_text",
                                      "text": "[Prior reasoning context]\n" + thought}]})
            elif kind == "tool_use":
                items.append({"type": "function_call", "call_id": block["id"],
                              "name": _tool_alias(block["name"]), "namespace": _HOST_TOOL_NAMESPACE,
                              "arguments": json.dumps(block.get("input") or {})})
            elif kind == "tool_result":
                result = block.get("content", "")
                if not isinstance(result, str):
                    result = responses_content(result)
                if block.get("is_error"):
                    result = ("Host tool error: " + result if isinstance(result, str) else
                              [{"type": "input_text", "text": "Host tool error:"}, *result])
                items.append({"type": "function_call_output", "call_id": block["tool_use_id"],
                              "output": result})
            elif kind in {"image", "input_image"}:
                items.append({"type": "message", "role": "user",
                              "content": responses_content([block])})
    return items


def _call_fingerprint(name, arguments):
    """A tool call's identity, without its payload.

    Arguments can carry file contents, paths and user text, and this record
    outlives the turn - so identity travels as a short digest and the raw
    input is never written. Key order is normalised so the same call made
    twice fingerprints the same both times.
    """
    try:
        rendered = json.dumps(arguments or {}, sort_keys=True, default=str)
    except (TypeError, ValueError):
        rendered = repr(arguments)
    return name, hashlib.sha256(rendered.encode("utf-8", "replace")).hexdigest()[:12]


def handoff_telemetry(messages, items=None):
    """What one rebuilt handoff looks like, for answering "is it still looping?".

    ``repeats`` is the loop signal: a host call whose name *and* arguments
    already appear earlier in the same conversation is work the model has
    already done and is doing again. ``reasoning`` is the control - it counts
    the reasoning context messages carried across, so a field log can tell a
    working fix from an inert one. If a client never echoes thinking back,
    reasoning stays 0 and the continuity fix is doing nothing, which is not
    something the tests can observe.
    """
    seen, repeats, calls, answered = {}, 0, 0, 0
    for message in messages or []:
        content = message.get("content") if isinstance(message, dict) else None
        for block in (content if isinstance(content, list) else []):
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                calls += 1
                key = _call_fingerprint(block.get("name"), block.get("input"))
                seen[key] = seen.get(key, 0) + 1
                repeats += seen[key] > 1
            elif block.get("type") == "tool_result":
                answered += 1
    worst = max(seen.items(), key=lambda pair: pair[1], default=None)
    return {
        "legs": answered,
        "calls": calls,
        "distinct_calls": len(seen),
        "repeats": repeats,
        "worst_call": ({"name": worst[0][0], "count": worst[1]}
                       if worst and worst[1] > 1 else None),
        "reasoning": sum(1 for item in items or []
                         if item.get("type") == "message" and item.get("role") == "assistant"
                         and item.get("phase") == "commentary"
                         and any(part.get("text", "").startswith("[Prior reasoning context]\n")
                                 for part in item.get("content", []) if isinstance(part, dict))),
    }


def _record_handoff(model, stats):
    """Append one telemetry line. Best effort: never fails a turn over a log."""
    path = os.environ.get(_TURN_LOG_ENV)
    if not path:
        return
    try:
        line = json.dumps({"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                           "model": model, **stats}, sort_keys=True)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except Exception:  # noqa: BLE001 - telemetry must never break the transport
        pass


def _delta(event):
    """The text of a delta notification, or None. Tolerates a bare string."""
    params = event.get("params")
    if isinstance(params, str):
        return params
    if not isinstance(params, dict):
        return None
    value = params.get("delta")
    if isinstance(value, str) and value:
        return value
    return None


def _same_turn(event, turn_id):
    """Whether a notification belongs to our turn.

    ``turn_id`` is None when ``turn/start`` answered without one, in which case
    correlation is impossible and every turn event is accepted - a single-turn,
    single-thread process has nothing else to confuse it with.
    """
    if turn_id is None:
        return True
    params = event.get("params")
    if not isinstance(params, dict):
        return True
    observed = params.get("turnId")
    if isinstance(observed, str) and observed:
        return observed == turn_id
    turn = params.get("turn")
    if isinstance(turn, dict) and isinstance(turn.get("id"), str) and turn["id"]:
        return turn["id"] == turn_id
    return True


def _failure_message(turn):
    error = turn.get("error") if isinstance(turn, dict) else None
    if isinstance(error, dict):
        message = error.get("message")
        if isinstance(message, str) and message.strip():
            return message.strip()
        return json.dumps(error, sort_keys=True)[:400]
    if isinstance(error, str) and error.strip():
        return error.strip()
    return None


def _usage_snapshot(params):
    """Normalize the latest request, never the thread's accumulated total.

    Codex inputTokens already includes cachedInputTokens; Anthropic usage
    counts the uncached and cached portions separately. Splitting here avoids
    counting the cache twice when the Responses bridge recombines them.
    """
    info = params.get("tokenUsage")
    last = info.get("last") if isinstance(info, dict) else None
    if not isinstance(last, dict):
        return None
    incoming, outgoing = last.get("inputTokens"), last.get("outputTokens")
    cached = last.get("cachedInputTokens", 0)
    if any(type(value) is not int or value < 0 for value in (incoming, outgoing, cached)) or cached > incoming:
        return None
    return {"type": "usage", "usage": {"input_tokens": incoming - cached,
            "cache_read_input_tokens": cached, "output_tokens": outgoing}}


def _stream_turn(session, *, turn_id, deadline, tools=None, thread_id=None, summary_only=False,
                 lease=None, timing=None):
    """Consume notifications until the turn terminates.

    Yields ``text_delta`` / ``thinking_delta`` as they arrive and always ends
    with exactly one terminal event: ``message_stop`` or ``error``.
    """
    fragments = lease.fragments if lease is not None else {}
    last_error = None
    host_names = {_tool_alias(tool["name"]): tool["name"] for tool in tools or []}

    def fragment(item_id, kind, index, text, *, complete=False):
        if summary_only and kind == "reasoning":
            return []
        key = (item_id, kind, index)
        previous = fragments.get(key, "")
        if complete:
            if text.startswith(previous):
                delta = text[len(previous):]
            elif item_id is None:
                # Legacy events without ids cannot identify a previous item.
                delta = text
            else:
                raise CodexCliAgentError("Codex completed an item with text inconsistent with its stream.")
            fragments[key] = text
        else:
            delta = text
            fragments[key] = previous + text
        if not delta:
            return []
        if kind == "text":
            return [{"type": "text_delta", "text": delta, "source_id": item_id}]
        return [{"type": "thinking_delta", "text": delta, "thinking_kind": kind,
                 "source_id": item_id, "part_index": index}]
    for _ in range(_MAX_STREAM_LOOPS):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            yield {"type": "error",
                   "message": "The Codex turn did not complete within its time budget."}
            return
        exited = getattr(session, "returncode", None)
        if exited is not None:
            yield {"type": "error",
                   "message": f"The Codex app-server exited (rc={exited}) mid-turn."}
            return
        try:
            events = session.events(timeout=min(remaining, 5.0))
        except CliSessionError as exc:
            yield {"type": "error", "message": f"The Codex session broke mid-turn: {exc}"}
            return
        saw_any = False
        try:
            for event in events:
                if not isinstance(event, dict):
                    continue
                saw_any = True
                method = event.get("method")
                if not _same_turn(event, turn_id):
                    continue
                params = event.get("params") or {}
                if not isinstance(params, dict):
                    params = {}
                if thread_id and params.get("threadId", thread_id) != thread_id:
                    continue
                if method == "thread/tokenUsage/updated":
                    usage = _usage_snapshot(params)
                    if usage is not None:
                        yield usage
                    continue
                if timing and (method in {"item/agentMessage/delta", "item/reasoning/textDelta",
                                          "item/reasoning/summaryTextDelta", "item/tool/call"} or
                               method == "item/completed" and
                               (params.get("item") or {}).get("type") in {"agentMessage", "reasoning"}):
                    timing.mark("first_model_event")
                if method == "item/tool/call":
                    if "id" not in event or params.get("namespace") != _HOST_TOOL_NAMESPACE:
                        yield {"type": "error", "message": "Codex requested an unregistered native tool."}
                        return
                    call = validate_host_call({"id": params.get("callId"),
                                               "name": host_names.get(params.get("tool")),
                                               "input": params.get("arguments")}, tools or [])
                    if lease is not None:
                        # A bridge-owned id is unique across tasks/processes,
                        # even when runtimes reuse local call identifiers.
                        call["id"] = "toolu_" + uuid.uuid4().hex
                        lease.pending = {"rpc_id": event["id"], "call": dict(call)}
                    yield {"type": "tool_call", **call}
                    yield {"type": "message_stop", "stop_reason": "tool_use"}
                    return
                if method in {"item/started", "item/completed"}:
                    item = params.get("item") or {}
                    if isinstance(item, dict) and item.get("type") in _NATIVE_TOOL_ITEMS:
                        yield {"type": "error", "message": "Codex attempted a native CLI tool "
                               f"({item['type']}); workspace actions must use host tools."}
                        return
                if "id" in event and method:
                    yield {"type": "error", "message": "Codex requested an unsupported "
                           f"interactive action ({method}); the host must handle it."}
                    return
                if method == "item/agentMessage/delta":
                    text = _delta(event)
                    if text:
                        yield from fragment(params.get("itemId"), "text", 0, text)
                elif method in ("item/reasoning/textDelta",
                                "item/reasoning/summaryTextDelta",
                                "item/reasoning/summaryPartAdded"):
                    text = _delta(event)
                    if text:
                        is_summary = method != "item/reasoning/textDelta"
                        index = params.get("summaryIndex" if is_summary else "contentIndex", 0)
                        yield from fragment(params.get("itemId"), "summary" if is_summary else "reasoning", index, text)
                elif method == "item/completed":
                    # The fallback for a runtime that completes an item without
                    # having streamed it: emit its text once, never twice.
                    params = event.get("params") or {}
                    item = params.get("item") if isinstance(params, dict) else None
                    if not isinstance(item, dict) or not _same_turn(event, turn_id):
                        continue
                    kind = item.get("type")
                    item_id = item.get("id") or params.get("itemId")
                    if kind == "agentMessage":
                        text = item.get("text")
                        if isinstance(text, str) and text.strip():
                            yield from fragment(item_id, "text", 0, text, complete=True)
                    elif kind == "reasoning":
                        for field, thought_kind in (("summary", "summary"), ("content", "reasoning")):
                            for index, text in enumerate(item.get(field) or []):
                                if isinstance(text, str) and text:
                                    yield from fragment(item_id, thought_kind, index, text, complete=True)
                elif method == "error":
                    params = event.get("params") if isinstance(event.get("params"), dict) else {}
                    # ``willRetry: true`` errors are the runtime's own reconnect
                    # attempts - five were observed before the terminal one.
                    # Reporting each as a failure would abort a turn Codex was
                    # still trying to finish, so only the last is kept, and the
                    # terminal ``turn/completed`` decides the outcome.
                    if not params.get("willRetry"):
                        error = params.get("error")
                        if isinstance(error, dict) and error.get("message"):
                            last_error = str(error["message"])
                elif method in ("turn/completed", "turn/failed"):
                    if not _same_turn(event, turn_id):
                        continue
                    params = event.get("params") if isinstance(event.get("params"), dict) else {}
                    turn = params.get("turn") if isinstance(params.get("turn"), dict) else {}
                    status = str(turn.get("status") or ("failed" if method == "turn/failed" else "completed"))
                    if method == "turn/failed" or status == "failed":
                        message = _failure_message(turn) or last_error or "The Codex turn failed."
                        yield {"type": "error", "message": message}
                        return
                    if status in {"interrupted", "cancelled", "canceled"}:
                        yield {"type": "error",
                               "message": last_error or "The Codex turn was interrupted."}
                        return
                    if status != "completed":
                        yield {"type": "error", "message": f"Unexpected Codex terminal status: {status}."}
                        return
                    yield {"type": "message_stop",
                           "stop_reason": _STOP_REASONS.get(status, "end_turn")}
                    return
        except CliSessionError as exc:
            yield {"type": "error", "message": f"The Codex session broke mid-turn: {exc}"}
            return
        if not saw_any and getattr(session, "returncode", None) is not None:
            yield {"type": "error",
                   "message": f"The Codex app-server exited (rc={session.returncode}) mid-turn."}
            return
    yield {"type": "error",
           "message": "The Codex turn stream did not terminate within its iteration budget."}


def _teardown(session, workspace):
    """The ``codex_runtime.py:125-140`` ladder, delegated then escalated.

    Runs on every exit path - normal completion, error, and a consumer that
    abandons the generator (``GeneratorExit``). Nothing here yields and nothing
    here raises: teardown failure must not mask the turn's own outcome, and a
    raise from ``finally`` during ``GeneratorExit`` is a RuntimeError.
    """
    if session is not None:
        try:
            session.close()
        except Exception:  # noqa: BLE001 - teardown must never mask the outcome
            pass
        # ``close()`` should have reaped the child. If a spawner implementation
        # left it running, escalate rather than orphan a runtime holding the
        # user's ChatGPT login.
        process = getattr(session, "process", None)
        if process is not None:
            for action in ("terminate", "kill"):
                try:
                    if process.poll() is None:
                        getattr(process, action)()
                        process.wait(timeout=3)
                except Exception:  # noqa: BLE001
                    pass
    if workspace is not None:
        try:
            cleanup_after_exit(session, workspace.close)
        except Exception:  # noqa: BLE001
            pass


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False).encode()).hexdigest()


def _history_blocks(history):
    """Ignore message grouping, but retain every model-visible block in order."""
    blocks = []
    for message in history or []:
        content = message.get("content") or []
        if isinstance(content, str):
            content = [{"type": "text", "text": content}]
        for block in content:
            blocks.append((message.get("role"), block))
    return blocks


def _continuation(lease, payload):
    """Match a unique host call, unchanged history, and all subsequent input.

    Changed instructions/tools/effort use another pool key. Edited or compacted
    history falls back to native history injection in a fresh thread. A pending
    RPC is consumed only by an exact call/result match.
    """
    pending = lease.pending
    if not pending or "prefix" not in pending:
        return None
    blocks = _history_blocks(payload.get("history"))
    count = pending["prefix_count"]
    if len(blocks) <= count or _digest(blocks[:count]) != pending["prefix"]:
        return None
    call = pending["call"]
    tail = blocks[count:]
    matches = [(index, block) for index, (role, block) in enumerate(tail)
               if role == "assistant" and block.get("type") == "tool_use"]
    if len(matches) != 1:
        return None
    call_index, observed = matches[0]
    if any(observed.get(key) != call.get(key) for key in ("id", "name", "input")):
        return None
    result = None
    steering = []
    for index, (role, block) in enumerate(tail):
        if role == "assistant":
            if index > call_index:
                return None
            continue
        if role != "user":
            return None
        if block.get("type") == "tool_result":
            if index <= call_index or result is not None or block.get("tool_use_id") != call["id"]:
                return None
            result = block
        elif block.get("type") in {"text", "image", "input_image"}:
            steering.append(block)
        else:
            return None
    return (result, steering) if result is not None else None


def _resume_host_call(lease, result, steering, *, timeout):
    # Queue new user input before unblocking the model's tool wait. If steering
    # is refused, fail this leg instead of silently dropping the user's input.
    if steering:
        inputs = []
        for part in responses_content(steering):
            if part["type"] == "input_text":
                inputs.append({"type": "text", "text": part["text"], "text_elements": []})
            else:
                inputs.append({"type": "image", "url": part["image_url"]})
        _result(lease.session.request("turn/steer", {
            "threadId": lease.thread_id, "expectedTurnId": lease.turn_id,
            "input": inputs}, timeout=timeout), context="turn/steer")
    content = []
    for part in responses_content(result.get("content", "")):
        if part["type"] == "input_text":
            content.append({"type": "inputText", "text": part["text"]})
        else:
            content.append({"type": "inputImage", "imageUrl": part["image_url"]})
    lease.session.send(json.dumps({"id": lease.pending["rpc_id"], "result": {
        "contentItems": content, "success": not bool(result.get("is_error"))}}))
    lease.pending = None


def _dispose_lease(lease):
    _teardown(lease.session, lease.workspace)


_POOL = SessionPool(_dispose_lease, capacity=8)
atexit.register(_POOL.close)


def run_turn(request, *, spawner=None, timeout=300, pool=None) -> Iterator[dict]:
    """One streaming turn on the user's ChatGPT subscription.

    Yields only ``text_delta``, ``thinking_delta``, ``message_stop`` and
    ``error``, and always terminates with ``message_stop`` or ``error``: every
    failure - absent runtime, refused argv, handshake timeout, JSON-RPC error,
    host exit, malformed NDJSON, deadline - is converted into an ``error`` event
    rather than escaping, because the caller is a harness thread drawing a
    transcript, not something that can recover from an exception mid-stream.
    """
    workspace = None
    session = None
    lease = None
    healthy = False
    timing = request.get("_cli_timing") if isinstance(request, dict) else None
    # Custom spawners remain one-shot unless explicitly given a test pool.
    owner = pool if pool is not None else (_POOL if spawner is None else None)
    try:
        budget = _bounded_timeout(timeout, default=300)
        deadline = time.monotonic() + budget
        payload = _normalise_request(request)
        argv = build_argv(payload["model"], effort=payload["effort"])
        mode = "cold"
        if owner is not None:
            key = _digest({"argv": argv, "env": minimal_env(), "system": payload["system"],
                           "tools": payload["tools"], "summary": payload["reasoning_summary"],
                           "tier": payload["service_tier"]})
            blocks = _history_blocks(payload.get("history"))
            continuation_key = (_digest([key, blocks]) if any(
                role == "user" and block.get("type") == "tool_result" and
                str(block.get("tool_use_id", "")).startswith("toolu_")
                for role, block in blocks) else None)
            lease, mode = owner.acquire(key, match=lambda entry: _continuation(entry, payload) is not None,
                                        timeout=max(0.01, deadline - time.monotonic()),
                                        request_key=continuation_key)
            session, workspace = lease.session, lease.workspace
        if timing:
            timing.mark("pool_ready")
            timing.label(reuse=mode)
        if session is None:
            if timing:
                timing.mark("startup_started")
            workspace = CodexTurnWorkspace(diagnostics=_diagnostics_enabled()).open()
            if lease is not None:
                lease.workspace = workspace
            session = _open_session(argv, workspace, spawner=spawner, timeout=budget)
            if lease is not None:
                lease.session = session
            if timing:
                timing.mark("startup_complete")
            _handshake(session, timeout=min(budget * _HANDSHAKE_SHARE, max(0.01, deadline - time.monotonic())))
            if lease is not None:
                lease.config = _inherited_mcp_overrides(session, timeout=max(0.01, deadline - time.monotonic()))
            if timing:
                timing.mark("initialization_complete")
        if timing:
            timing.label(cli_pid=getattr(getattr(session, "process", None), "pid", None))
        if mode == "resumed":
            result, steering = _continuation(lease, payload)
            _resume_host_call(lease, result, steering, timeout=max(0.01, deadline - time.monotonic()))
            thread_id, turn_id = lease.thread_id, lease.turn_id
        else:
            if lease is not None and lease.thread_id:
                _result(session.request("thread/unsubscribe", {"threadId": lease.thread_id},
                                        timeout=max(0.01, deadline - time.monotonic())),
                        context="thread/unsubscribe")
                lease.fragments.clear()
            thread_id = _start_thread(session, payload, workspace,
                                      timeout=max(0.01, deadline - time.monotonic()),
                                      servers=lease.config if lease is not None else None)
        if mode != "resumed" and payload.get("history"):
            history = payload["history"]
            # Historical calls, results and reasoning must be actual protocol
            # items. A tool result in user-message text makes some models repeat
            # the original call forever while waiting for its native result, and
            # reasoning dropped on the floor makes them re-plan from scratch on
            # every leg instead of resuming the turn they are already in.
            items = _history_items(history)
            _record_handoff(payload["model"], handoff_telemetry(history, items))
            response = session.request("thread/inject_items", {"threadId": thread_id, "items": items},
                                       timeout=max(1.0, deadline - time.monotonic()))
            _result(response, context="thread/inject_items")
            payload["prompt"] = _RESUME_PROMPT
        if mode != "resumed":
            turn_id = _start_turn(session, payload, thread_id,
                                  timeout=max(0.01, deadline - time.monotonic()))
        if lease is not None:
            lease.thread_id, lease.turn_id = thread_id, turn_id
        if timing:
            timing.mark("model_ready")
        for event in _stream_turn(session, turn_id=turn_id, deadline=deadline,
                                  tools=payload["tools"], thread_id=thread_id,
                                  summary_only=payload["reasoning_summary"] is not None,
                                  lease=lease, timing=timing):
            if event.get("type") == "message_stop":
                healthy = True
                if lease is not None and lease.pending:
                    blocks = _history_blocks(payload.get("history"))
                    lease.pending.update(prefix=_digest(blocks), prefix_count=len(blocks))
            yield event
    except CodexCliAgentError as exc:
        yield {"type": "error", "message": str(exc), "http_status": exc.http_status}
    except CliSessionError as exc:
        yield {"type": "error", "message": f"The Codex app-server session failed: {exc}"}
    except subprocess.TimeoutExpired:
        yield {"type": "error",
               "message": "The Codex app-server did not answer within its time budget."}
    except Exception as exc:  # noqa: BLE001 - a turn must never raise at the harness
        yield {"type": "error",
               "message": f"The Codex turn could not be run: {exc.__class__.__name__}: {exc}"}
    finally:
        if lease is not None:
            if timing:
                timing.label(cleanup_kind="lease_release", session_retained=bool(
                    healthy and timing.delivered))
            owner.release(lease, healthy=healthy and (timing is None or timing.delivered))
        else:
            _teardown(session, workspace)


# --------------------------------------------------------------------------
# the coarse exec --json fallback (opt-in, not the default path)
# --------------------------------------------------------------------------

def _exec_events(process, *, deadline):
    """Parse ``codex exec --json`` NDJSON, tolerating malformed lines.

    Measured on 0.153.0: a trivial turn emits exactly five events -
    ``thread.started``, ``item.completed``, ``turn.started``,
    ``item.completed``, ``turn.completed``. There is no token-level channel, so
    this transport yields one ``text_delta`` per completed item.

    The deadline is enforced while waiting for stdout as well as between
    lines, so a silent or wedged child cannot block the harness past its
    budget.
    """
    stdout = process.stdout
    while True:
        remaining = max(0.0, deadline - time.monotonic())
        if remaining <= 0.0:
            yield {"type": "error",
                   "message": "The Codex exec turn did not complete within its time budget."}
            return
        readable, _, _ = select.select([stdout], [], [], remaining)
        if not readable:
            yield {"type": "error",
                   "message": "The Codex exec turn did not complete within its time budget."}
            return
        line = stdout.readline()
        if not line:
            break
        if time.monotonic() > deadline:
            yield {"type": "error",
                   "message": "The Codex exec turn did not complete within its time budget."}
            return
        text = line.strip() if isinstance(line, str) else str(line).strip()
        if not text:
            continue
        try:
            event = json.loads(text)
        except ValueError:
            # Codex mixes human-readable progress into the same stream; a line
            # that is not JSON is skipped, never fatal.
            continue
        if not isinstance(event, dict):
            continue
        kind = event.get("type") or event.get("msg")
        item = event.get("item") if isinstance(event.get("item"), dict) else {}
        if kind in ("item.completed", "item_completed"):
            item_kind = item.get("item_type") or item.get("type")
            if item_kind in {"command_execution", "file_change", "mcp_tool_call", "web_search"} \
                    or item_kind in _NATIVE_TOOL_ITEMS:
                yield {"type": "error", "message": "Codex exec attempted a native tool; use the host tool transport."}
                return
            if item_kind in {"agent_message", "assistant", "agentMessage"}:
                text_value = item.get("text") or item.get("content")
                if isinstance(text_value, str) and text_value.strip():
                    yield {"type": "text_delta", "text": text_value}
        elif kind in ("turn.completed", "turn_completed", "task_complete"):
            if event.get("status", "completed") != "completed":
                yield {"type": "error", "message": "The Codex exec turn did not complete successfully."}
                return
            yield {"type": "message_stop", "stop_reason": "end_turn"}
            return
        elif kind in ("error", "turn.failed"):
            message = event.get("message") or event.get("error") or "The Codex exec turn failed."
            yield {"type": "error", "message": str(message)}
            return
    yield {"type": "error", "message": "The Codex exec stream ended before the turn completed."}


def run_turn_fallback(request, *, spawner=None, timeout=300) -> Iterator[dict]:
    """The documented coarse fallback: ``codex exec --json``, one delta per item.

    Use only when app-server turn streaming is unavailable on the installed
    runtime. It cannot stream tokens - see :func:`_exec_events` - so a long
    answer appears all at once.
    """
    workspace = None
    process = None
    config_session = None
    try:
        budget = _bounded_timeout(timeout, default=300)
        deadline = time.monotonic() + budget
        payload = _normalise_request(request)
        if payload["tools"]:
            raise CodexCliAgentError("Codex host tools require the app-server transport; exec cannot forward them.")
        if payload["images"]:
            raise CodexCliAgentError("Codex image inputs require the app-server transport.")
        workspace = CodexTurnWorkspace(diagnostics=_diagnostics_enabled()).open()
        # exec has no per-thread config request. Read config without starting
        # a model turn, then explicitly disable inherited MCP servers on argv.
        config_session = _open_session(_app_server_argv(), workspace, timeout=budget)
        _handshake(config_session, timeout=budget * _HANDSHAKE_SHARE)
        servers = _inherited_mcp_overrides(config_session, timeout=budget * _START_SHARE)
        _teardown(config_session, None)
        config_session = None
        # stdin is DEVNULL, so the prompt travels as the positional argument
        # rather than as piped input. The forbidden-flag assert is re-run on the
        # constructed flags only; the user prompt must not be able to trip the
        # substring scan (e.g. a prompt containing "workspace-write").
        flag_argv = build_exec_argv(payload["model"], effort=payload["effort"])
        if servers:
            disabled = ",".join(f"{_toml_string(name)}={{enabled=false}}" for name in servers)
            flag_argv += ["-c", "mcp_servers={" + disabled + "}"]
        _assert_safe_argv(flag_argv, context="exec")
        argv = [*flag_argv, "-c", f"developer_instructions={_toml_string(payload['system'])}",
                payload["prompt"]]
        spawn = spawner or subprocess.Popen
        process = spawn(list(argv), stdin=_DEVNULL, stdout=subprocess.PIPE,
                        stderr=_DEVNULL, text=True, env=workspace.env(), cwd=str(workspace.cwd))
        yield from _exec_events(process, deadline=deadline)
    except CodexCliAgentError as exc:
        yield {"type": "error", "message": str(exc)}
    except Exception as exc:  # noqa: BLE001 - a turn must never raise at the harness
        yield {"type": "error",
               "message": f"The Codex exec turn could not be run: {exc.__class__.__name__}: {exc}"}
    finally:
        if process is not None:
            for action in ("terminate", "kill"):
                try:
                    if process.poll() is None:
                        getattr(process, action)()
                        process.wait(timeout=3)
                except Exception:  # noqa: BLE001
                    pass
        _teardown(config_session, workspace)
