"""Claude Code CLI as a hub MODEL ROUTE (local-only, default-off experiment).

Route 2 — CLI-as-transport. The vendor CLI owns and refreshes its own login.
This module NEVER reads, copies, or refreshes a credential file, and never
touches the Keychain: ``auth_state()`` asks ``claude auth status`` and believes
only what that read-only subcommand reports.

These routes are deliberately **pure text-in/text-out** so they behave like
model routes instead of delegatory headless agents. The desktop harness owns
the tool loop; if this route also ran tools, every tool call would be executed
twice against two different views of the world. So the whole built-in tool set
is switched off, MCP servers are skipped, skills are disabled, the session runs
with ``--permission-mode default`` (not plan, which injects a plan-mode persona
that leaks to users), and anything that would have prompted for permission is
denied automatically rather than approved. Verified live against claude 2.1.276:
the ``system/init`` event reports ``tools: []`` and ``mcp_servers: []`` with
``--permission-mode default --permission-prompts none --tools ""``.

One exception, only when the desktop asked for hosted web search: the turn
keeps WebSearch alone (``--allowedTools WebSearch --tools WebSearch``). It runs
on Anthropic's search backend and fetches nothing locally; WebFetch, which
does, stays off. Each search is relayed as a ``web_search`` event.

Host tools travel as native tools. A turn that carries the desktop harness's
tools attaches them as a stdio MCP server (``cli_host_mcp``, server ``host``),
so the model sees ``mcp__host__<tool>`` in its real tool list and calls it the
way it was trained to, instead of through a text protocol it must be talked
into (Sonnet 5 under Codex ignored that protocol for native calls, 24 Sep
2026). A native call is handed to the host as a tool call and the turn ends at
that message: the CLI's fail-closed permission mode refuses to run it, and the
server executes nothing either way. Verified live on claude 2.1.280 with
Codex's 111 tools: init lists every ``mcp__host__`` tool, the model calls the
right one directly, and ``ENABLE_TOOL_SEARCH=false`` keeps them out of a
deferred tool-search step the turn could not reach. If ``system/init`` shows
the server did not attach, the turn is spawned again, invisibly, with the
older text manifest (``cli_tool_call``).

A native call for a host tool under its plain name (no ``mcp__host__``) is
forwarded too, but only when ``system/init`` has shown that the CLI's own
registry lacks the name: the CLI cannot have run it, so the host runs it
exactly once. A native call for a tool nobody provides becomes the route's
protocol correction rather than the CLI's "No such tool available".

Live sessions. Ending the CLI at every host call made each step a fresh
process fed the whole conversation as a transcript: the model's reasoning,
its search results and its native tool history were gone at every step. So a
turn whose request carries its typed history runs the ``host`` server with a
bridge (cli_host_bridge) instead. The host tools are pre-approved for that
server alone, and it forwards each call to the hub rather than refusing it,
so the CLI waits inside its own tool call. Once the bridge shows the CLI
waiting on exactly the calls its message made (same ids, tools and
arguments), the adapter hands them to the host and keeps the process in a
pool lease. When the host's next request continues from that handoff -
unchanged history, then that message, then one result per call and nothing
else - the results answer the waiting calls and the same process streams on.
Anything else replays the conversation into a fresh process as before: an
edited or compacted history, new user input, a CLI that has exited, a result
it cannot take, or a CLI that is not waiting on every call it made. Verified
live on claude 2.1.280 through the route with Codex's 111 tools and search
on: Sonnet 5 and Opus 5.5 each ran a three-step git task on one process (the
first step cold in 2.3-2.8 s, each continuation resumed in 0.7-1.3 s), and
the process exited when the turn ended.

Permission mode matrix tested live on claude 2.1.276:
 - ``plan``: tools=[], BUT injects plan-mode persona ("I'm in plan mode...")
 - ``default``: tools=[], NO persona, fail-closed via --permission-prompts none
 - ``manual``: tools=[], NO persona, BUT defaults to default mode (same as default)
 - ``dontAsk``: tools=[], NO persona, but less explicit about fail-closed
 - ``auto``/``acceptEdits``: auto-approve family, FORBIDDEN by design

No auto-approve flag is ever constructed. ``_FORBIDDEN_FLAGS`` is asserted
against inside ``build_argv`` so a future edit cannot introduce one quietly.

Transport: one-shot print mode with NDJSON streaming. The prompt always travels
on **stdin**, never as a positional argument — ``--tools`` is variadic and will
swallow anything that follows it, including the prompt. The conversation is
stateless: history is rendered into the prompt itself, so nothing depends on
``--continue``/``--resume``, and ``--no-session-persistence`` keeps the CLI from
writing a session the hub cannot resume anyway.

``SYSTEM_PROMPT_TRANSPORT`` says where the harness system prompt goes, so the
hub can wire providers generically: ``"flag"`` here (``--append-system-prompt``)
versus ``"prompt"`` for CLIs that expose no system-prompt flag.
"""
from __future__ import annotations

import atexit
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator

from cli_session import CliSessionError, StdioSession, minimal_env, resolve_binary
from cli_lifecycle import cleanup_after_exit
from cli_host_bridge import HostBridgeError, HostCallBridge
from cli_host_mcp import SERVER_NAME, HostToolset, server_command, tools_note
from cli_tool_call import (MAX_ENVELOPE_BYTES, TOOL_RESULTS_KEY, TRANSCRIPT_HEADER, ToolCallError, transcript_footer,
                           normalize_tools, render_tool_anchor, render_tool_manifest,
                           validate_host_call)
import cli_live_session as live_session
from cli_images import prompt_content
from codex_session_pool import SessionPool, digest
from fast_models import supports_fast_toggle
from model_names import CLAUDE_CLI_ALIASES, CLAUDE_MODEL_LABELS

try:  # Repo-native effort ladder; degrade to a local copy if unavailable.
    from effort_map import map_effort as _map_effort
except Exception:  # pragma: no cover - defensive import guard
    _map_effort = None


class ClaudeCliAgentError(RuntimeError):
    """The Claude Code CLI route could not be described, resolved, or driven."""


# ---------------------------------------------------------------------------
# Provider identity
# ---------------------------------------------------------------------------

PROVIDER_ID = "claude"
PROVIDER_NAME = "Claude (Claude Code CLI)"
TRANSPORT = "print"
BINARY_NAMES = ("claude",)

# Where the harness system prompt is delivered. "flag" => build_argv emits it;
# "prompt" => the CLI has no such flag and it is folded into the stdin prompt.
SYSTEM_PROMPT_TRANSPORT = "flag"
IMAGE_TRANSPORT = "stream_json"
VERIFIED_IMAGE_MODELS = frozenset({"sonnet", "claude-sonnet-5"})
#: Claude Code's WebSearch runs on Anthropic's search backend and never fetches
#: a page locally, so it is the one tool a turn may enable (WebFetch stays off).
WEB_SEARCH = True
#: Host tools reach the model as native MCP tools; cli_routes renders no text
#: manifest for this route and leaves the tool surface to run_turn.
HOST_TOOL_TRANSPORT = "mcp"
#: How Claude Code names a tool from the ``host`` MCP server to its model.
MCP_TOOL_PREFIX = f"mcp__{SERVER_NAME}__"
#: Child environment for a turn that attaches the host tools (claude 2.1.280):
#: connect the server before the first request instead of racing it, give up
#: on it after 20 s rather than waiting indefinitely, and list every host tool
#: directly, never behind a tool-search step this turn's tool set lacks.
_MCP_ENV = {"MCP_CONNECTION_NONBLOCKING": "false", "MCP_TIMEOUT": "20000",
            "ENABLE_TOOL_SEARCH": "false"}
#: The route passes typed history so a host call can continue a live session.
LIVE_HOST_CALLS = True
#: How long a CLI may wait for the host's result before it is retired and the
#: next step replays the conversation into a fresh process instead.
LIVE_PENDING_TTL = 900
#: The bridge server's per-server call ceiling. claude 2.1.280 reads it as both
#: the hard and the idle limit of one call, and it sits well past
#: LIVE_PENDING_TTL, so the hub always retires a waiting CLI before the CLI
#: gives up on the call.
_LIVE_CALL_TIMEOUT_MS = 3_600_000
#: How long the CLI has, once its message ends, to be waiting on every call it
#: made. After that the calls are handed off without a live session.
_LIVE_CONFIRM_SECONDS = 15.0
#: A live child also keeps host results inline (Claude Code truncates MCP
#: output past 25,000 tokens by default) and never backgrounds a long call.
_LIVE_ENV = {**_MCP_ENV, "MAX_MCP_OUTPUT_TOKENS": "150000", "CLAUDE_CODE_MCP_AUTO_BACKGROUND_MS": "0"}

# Real installs live outside the default PATH on this machine (~/.local/bin).
_EXTRA_BIN_DIRS = ("~/.local/bin", "/opt/homebrew/bin", "/usr/local/bin")

# ---------------------------------------------------------------------------
# Safety invariants
# ---------------------------------------------------------------------------

# Never constructed, never forwarded, asserted against in build_argv.
_FORBIDDEN_FLAGS = frozenset({
    "--dangerously-skip-permissions",
    "--allow-dangerously-skip-permissions",
    "--always-approve",
    "--dangerously-bypass-approvals-and-sandbox",
    "--approve-for-me",
})

# The read-only posture. --permission-prompts none is the FAIL-CLOSED choice:
# anything that would have prompted is denied automatically, never approved.
READ_ONLY_FLAGS = (
    "--permission-mode", "default",
    "--permission-prompts", "none",
    "--strict-mcp-config",
    "--disable-slash-commands",
    "--no-session-persistence",
)

# Variadic: must stay last in argv or it swallows every following flag.
_VARIADIC_TOOL_FLAGS = ("--tools", "--allowedTools", "--allowed-tools",
                        "--disallowedTools", "--disallowed-tools")

# ---------------------------------------------------------------------------
# Models and effort
# ---------------------------------------------------------------------------

# claude --effort accepts exactly these (claude --help, verified 2.1.276).
CLAUDE_EFFORTS = ["low", "medium", "high", "xhigh", "max"]
CLAUDE_EFFORT_ALIASES = {
    "none": "low",
    "minimal": "low",
    "low": "low",
    "medium": "medium",
    "high": "high",
    "xhigh": "xhigh",
    "max": "max",
    "ultra": "max",
}

# CURATED SEED, NOT ACCOUNT DISCOVERY. Claude Code exposes no non-interactive
# model list. Publish explicit versions so a picker selection stays on that
# version and older releases can be labelled Legacy. Account availability and
# limits remain provider-managed; the CLI's error is surfaced at turn time.
KNOWN_MODELS = tuple(
    {"id": identifier, "display_name": label,
     "reasoning_levels": [] if identifier == "claude-haiku-4-5" else list(CLAUDE_EFFORTS),
     "web_search": True}
    for identifier, label in CLAUDE_MODEL_LABELS.items()
)

_NO_MODEL_LIST_WARNING = (
    "Claude Code exposes no non-interactive model list; routes must be named "
    "explicitly."
)

# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

_MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/+\-]{0,199}\Z")
_VERSION = re.compile(r"(\d+(?:\.\d+)+(?:[.\-+][0-9A-Za-z.\-+]*)?)")
_ROLES = ("user", "assistant")
_MAX_STDERR_CHARS = 2000


def _validate_model(model: Any) -> str:
    if not isinstance(model, str) or not model.strip():
        raise ClaudeCliAgentError("a non-empty model id is required")
    candidate = model.strip()
    if not _MODEL_ID.match(candidate):
        raise ClaudeCliAgentError(f"refusing suspicious model id {model!r}")
    return candidate


def _validate_effort(effort: Any) -> str | None:
    """Normalize a harness effort rank onto one claude actually accepts."""
    if effort is None:
        return None
    if not isinstance(effort, str) or not effort.strip():
        raise ClaudeCliAgentError(f"refusing suspicious effort {effort!r}")
    requested = effort.strip()
    if _map_effort is not None:
        mapped = _map_effort(requested, CLAUDE_EFFORTS, CLAUDE_EFFORT_ALIASES)
        if mapped:
            return mapped
    alias = CLAUDE_EFFORT_ALIASES.get(requested.casefold())
    if alias:
        return alias
    raise ClaudeCliAgentError(
        f"effort {requested!r} is not servable by {PROVIDER_NAME}; "
        f"expected one of {', '.join(CLAUDE_EFFORTS)}"
    )


def _assert_safe(argv: Iterable[str]) -> list[str]:
    """Fail closed if an auto-approve flag ever reaches argv construction."""
    materialized = [str(item) for item in argv]
    offenders = [item for item in materialized
                 if item in _FORBIDDEN_FLAGS
                 or item.split("=", 1)[0] in _FORBIDDEN_FLAGS]
    if offenders:
        raise ClaudeCliAgentError(
            "refusing to build argv containing auto-approve flag(s): "
            + ", ".join(sorted(set(offenders)))
        )
    return materialized


# ---------------------------------------------------------------------------
# Capture plumbing (discover / auth_state / catalogue)
# ---------------------------------------------------------------------------

def _account_env(config_dir) -> dict:
    """Point the CLI at one account's config folder; empty means its default login.

    Claude Code keys its Keychain item by this folder, so each account keeps
    its own login (and refreshes it itself) without touching the default one.
    """
    if not config_dir:
        return {}
    if not isinstance(config_dir, str) or not os.path.isabs(config_dir):
        raise ValueError("A Claude account's config folder must be an absolute path")
    return {"CLAUDE_CONFIG_DIR": config_dir}


def _default_capture(argv, *, timeout=30, stdin=None, env=None):
    """One-shot bounded subprocess using a minimal environment."""
    try:
        completed = subprocess.run(
            [str(item) for item in argv],
            capture_output=True,
            text=True,
            timeout=float(timeout),
            input=stdin,
            env=minimal_env(env),
        )
    except subprocess.TimeoutExpired as exc:
        raise CliSessionError(
            f"{Path(str(argv[0])).name} timed out after {timeout}s"
        ) from exc
    except OSError as exc:
        raise CliSessionError(
            f"could not run {Path(str(argv[0])).name}: {exc}"
        ) from exc
    return completed


def _coerce_result(result: Any) -> tuple[int | None, str, str]:
    """Normalize whatever a capture callable returned into (rc, out, err)."""
    if result is None:
        return None, "", ""
    if isinstance(result, dict):
        rc = result.get("returncode", result.get("rc", result.get("exit_code")))
        return (rc if isinstance(rc, int) else None,
                str(result.get("stdout") or ""),
                str(result.get("stderr") or ""))
    if isinstance(result, (tuple, list)):
        padded = list(result) + [None, "", ""]
        rc = padded[0]
        return (rc if isinstance(rc, int) else None,
                str(padded[1] or ""), str(padded[2] or ""))
    rc = getattr(result, "returncode", None)
    return (rc if isinstance(rc, int) else None,
            str(getattr(result, "stdout", "") or ""),
            str(getattr(result, "stderr", "") or ""))


def _invoke(capture, argv, *, timeout=30, stdin=None, env=None) -> tuple[int | None, str, str]:
    """Call an injected capture, adapting to whichever kwargs it accepts.

    ``capture`` is supplied by the hub; its exact signature is not part of this
    module's contract, so kwargs are passed only when the callable declares
    them (or accepts **kwargs). Everything is normalized by _coerce_result.
    """
    runner = capture or _default_capture
    materialized = [str(item) for item in argv]
    kwargs: dict[str, Any] = {}
    try:
        import inspect

        signature = inspect.signature(runner)
        accepts_var_kw = any(
            p.kind is inspect.Parameter.VAR_KEYWORD
            for p in signature.parameters.values()
        )
        declared = set(signature.parameters)
        if accepts_var_kw or "timeout" in declared:
            kwargs["timeout"] = timeout
        if stdin is not None:
            for name in ("stdin", "input", "prompt"):
                if accepts_var_kw or name in declared:
                    kwargs[name] = stdin
                    break
        if env and (accepts_var_kw or "env" in declared):
            kwargs["env"] = env
    except (TypeError, ValueError):
        kwargs = {"timeout": timeout}
        if stdin is not None:
            kwargs["stdin"] = stdin
        if env:
            kwargs["env"] = env
    return _coerce_result(runner(materialized, **kwargs))


def _resolve_binary() -> str | None:
    extra = tuple(str(Path(item).expanduser()) for item in _EXTRA_BIN_DIRS)
    return resolve_binary(BINARY_NAMES, extra_dirs=extra)


def _parse_version(text: str) -> str | None:
    match = _VERSION.search(text or "")
    return match.group(1) if match else None


# ---------------------------------------------------------------------------
# Public read-only surface
# ---------------------------------------------------------------------------

def discover(*, capture=None) -> dict:
    """Report whether the CLI is installed and which version it claims."""
    binary = _resolve_binary()
    if not binary:
        return {"installed": False, "binary": None, "version": None}
    version = None
    try:
        rc, out, err = _invoke(capture, [binary, "--version"], timeout=30)
        if rc in (0, None):
            version = _parse_version(out) or _parse_version(err)
    except Exception:
        # A binary that cannot report a version is still installed; the hub
        # shows it as such and the failure surfaces at turn time instead.
        version = None
    return {"installed": True, "binary": binary, "version": version}


def auth_state(*, capture=None, config_dir=None) -> dict:
    """Ask the CLI, and only the CLI, whether it is signed in.

    Never reads ~/.claude, auth.json, or the Keychain. An unparseable answer or
    a failing subcommand is reported as "unknown" rather than guessed at: this
    route is default-off, and a wrong "missing" would look like a broken login.
    ``config_dir`` asks about that account's folder instead of the default.
    """
    binary = _resolve_binary()
    if not binary:
        return {"state": "missing",
                "detail": "the claude CLI is not installed or not on PATH"}
    try:
        rc, out, err = _invoke(capture, [binary, "auth", "status"], timeout=30,
                               env=_account_env(config_dir))
    except Exception as exc:
        return {"state": "unknown", "detail": f"auth probe failed: {exc}"}
    if rc not in (0, None):
        # A signed-out CLI exits 1 but still prints its status object
        # (seen on 2.1.281 with an unused CLAUDE_CONFIG_DIR): that is an answer.
        try:
            if json.loads(out).get("loggedIn") is False:
                return {"state": "missing",
                        "detail": "claude reports no active login; sign in with the "
                                  "claude CLI itself"}
        except (ValueError, TypeError, AttributeError):
            pass
        detail = (err or out).strip().splitlines()
        return {"state": "unknown",
                "detail": f"claude auth status exited {rc}"
                          + (f": {detail[-1]}" if detail else "")}
    try:
        payload = json.loads(out)
    except (ValueError, TypeError):
        return {"state": "unknown",
                "detail": "claude auth status returned unparseable output"}
    if not isinstance(payload, dict):
        return {"state": "unknown",
                "detail": "claude auth status returned a non-object payload"}
    logged_in = payload.get("loggedIn")
    if logged_in is True:
        method = str(payload.get("authMethod") or "unknown")
        plan = payload.get("subscriptionType")
        detail = f"signed in via {method}"
        email = payload.get("email")
        if isinstance(email, str) and email:
            detail += f" as {email}"
        if plan:
            detail += f" ({plan} subscription)"
        return {"state": "authenticated", "detail": detail}
    if logged_in is False:
        return {"state": "missing",
                "detail": "claude reports no active login; sign in with the "
                          "claude CLI itself"}
    return {"state": "unknown",
            "detail": "claude auth status did not report a loggedIn field"}


def catalogue(*, capture=None, timeout=30, config_dir=None) -> tuple[list[dict], list[str]]:
    """No discovery is possible: Claude Code has no read-only model list.

    Returns an empty catalogue plus the reason, and never spawns anything. The
    hub should seed route names from KNOWN_MODELS, which is explicitly
    unverified rather than discovered.
    """
    return [], [_NO_MODEL_LIST_WARNING]


# ---------------------------------------------------------------------------
# argv construction
# ---------------------------------------------------------------------------

def build_argv(model, *, effort=None, system=None, stream=True, search=False,
               fast_mode=None, mcp_config=None, run_host_tools=False) -> list[str]:
    """Full argv for one print-mode turn. argv[0] is the bare binary name.

    ``run_turn`` replaces argv[0] with the resolved absolute path; keeping the
    name here makes this function pure and testable on a machine with no CLI
    installed. The prompt is NOT part of argv — it always goes on stdin.

    ``search`` leaves exactly one tool, WebSearch, and pre-approves it: the
    fail-closed ``--permission-prompts none`` would otherwise deny every call.

    ``mcp_config`` (a JSON string) attaches the host tools' MCP server. Under
    ``--strict-mcp-config`` it is the only server; its tools are listed to the
    model but never pre-approved, so the CLI refuses to run them itself.
    ``run_host_tools`` pre-approves that one server for a live session, whose
    server forwards each call to the hub (cli_host_bridge) and runs nothing.
    """
    if run_host_tools and mcp_config is None:
        raise ClaudeCliAgentError("run_host_tools needs the host tools' MCP server")
    validated_model = _validate_model(model)
    # Saved short routes display a specific version in the hub catalogue.
    # Pin the CLI request to that same version instead of letting its moving
    # alias select a different model behind the displayed name.
    validated_model = CLAUDE_CLI_ALIASES.get(validated_model, validated_model)
    validated_effort = _validate_effort(effort)
    if fast_mode is not None and type(fast_mode) is not bool:
        raise ClaudeCliAgentError("fast_mode must be true, false, or omitted")
    if fast_mode is True and not supports_fast_toggle("claude", validated_model):
        raise ClaudeCliAgentError("Claude Fast mode is unavailable for this model")

    argv: list[str] = [BINARY_NAMES[0], "-p",
                       "--output-format", "stream-json" if stream else "text"]
    if stream:
        # stream-json under -p requires --verbose; --include-partial-messages is
        # what turns whole-message events into token-level deltas.
        argv += ["--include-partial-messages", "--verbose"]
    argv += list(READ_ONLY_FLAGS)
    argv += ["--model", validated_model]
    if fast_mode is not None:
        # Claude Code documents this session-local setting for -p. It leaves
        # the user's ~/.claude/settings.json untouched.
        argv += ["--settings", json.dumps({"fastMode": fast_mode}, separators=(",", ":"))]
    if validated_effort:
        argv += ["--effort", validated_effort]
    if system is not None:
        if not isinstance(system, str):
            raise ClaudeCliAgentError("system must be a string or None")
        if system.strip():
            # Attached (=) form when the text could be mistaken for a flag.
            if system.startswith("-"):
                argv += [f"--append-system-prompt={system}"]
            else:
                argv += ["--append-system-prompt", system]
    if mcp_config is not None:
        if not isinstance(mcp_config, str) or not mcp_config.startswith("{"):
            raise ClaudeCliAgentError("mcp_config must be a JSON object string")
        # Variadic too, so a following flag must end it: the tools flags do.
        argv += ["--mcp-config", mcp_config]
    # Variadic, so it must be the last thing on the command line.
    allowed = (["WebSearch"] if search else []) + ([f"mcp__{SERVER_NAME}"] if run_host_tools else [])
    if allowed:
        argv += ["--allowedTools", ",".join(allowed)]
    argv += ["--tools", "WebSearch" if search else ""]

    if argv[-2] not in _VARIADIC_TOOL_FLAGS:
        raise ClaudeCliAgentError(
            "internal error: a variadic flag must terminate argv, otherwise it "
            "swallows everything after it"
        )
    return _assert_safe(argv)


# ---------------------------------------------------------------------------
# Prompt rendering
# ---------------------------------------------------------------------------

_TRANSCRIPT_HEADER = TRANSCRIPT_HEADER


def _coerce_messages(messages: Any) -> list[dict]:
    if not isinstance(messages, (list, tuple)):
        raise ClaudeCliAgentError("request messages must be a list")
    normalized: list[dict] = []
    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            raise ClaudeCliAgentError(f"message {index} must be an object")
        role = message.get("role")
        if role not in _ROLES:
            raise ClaudeCliAgentError(
                f"message {index} has unsupported role {role!r}; "
                f"expected one of {', '.join(_ROLES)}"
            )
        content = message.get("content")
        if content is None:
            content = ""
        if not isinstance(content, str):
            content = str(content)
        normalized.append({"role": role, "content": content,
                           **({TOOL_RESULTS_KEY: True} if message.get(TOOL_RESULTS_KEY) is True else {})})
    if not any(item["role"] == "user" for item in normalized):
        raise ClaudeCliAgentError("request contains no user message to answer")
    return normalized


def render_prompt(messages, *, system=None) -> str:
    """Render the whole stateless conversation into one stdin prompt.

    Order is system, then user/assistant turns in the order given. A single
    bare user turn with no system text is passed through verbatim so the common
    case pays no framing overhead.
    """
    normalized = _coerce_messages(messages)
    system_text = system.strip() if isinstance(system, str) else ""
    if len(normalized) == 1 and not system_text:
        only = normalized[0]
        if only["role"] == "user":
            return only["content"]

    blocks = [_TRANSCRIPT_HEADER]
    if system_text:
        blocks.append(f"<system>\n{system_text}\n</system>")
    for message in normalized:
        blocks.append(
            f'<turn role="{message["role"]}">\n{message["content"]}\n</turn>'
        )
    blocks.append(transcript_footer(normalized))
    return "\n\n".join(blocks) + "\n"


# ---------------------------------------------------------------------------
# Turn execution
# ---------------------------------------------------------------------------

class _TurnState:
    """Mutable accumulation for one streamed turn."""

    def __init__(self) -> None:
        self.emitted_text = False
        self.result_text: str | None = None
        self.stop_reason: str | None = None
        self.failure: str | None = None
        self.raw_lines: list[str] = []
        self.stderr_handle = None
        self.terminal = False
        self.messages = {}
        self.current_message = None
        self.last_text_message = None
        self.search = False
        self.host_tools: list[dict] = []
        self.host_names: frozenset[str] = frozenset()
        # The host tools served over MCP on this attempt; None without them.
        self.toolset: HostToolset | None = None
        # system/init showed the MCP server's tools missing: respawn with the
        # text manifest before anything reaches the client.
        self.mcp_unavailable = False
        # The CLI's own tool registry from system/init; None until reported.
        self.registry: frozenset[str] | None = None
        self.pending_calls: dict = {}
        self.host_calls: dict = {}
        self.stray_calls: list[str] = []
        self.host_handoff = False
        # Host calls handed over on earlier legs of a live session.
        self.handed: set = set()

    def next_leg(self) -> None:
        """Start the next host request on a live session.

        What one request reports starts afresh; what the process has already
        shown (its registry, its tool set, the messages it has streamed) stays,
        so a late snapshot of an earlier message repeats neither text nor calls.
        """
        self.handed.update(identifier for identifier in self.host_calls if isinstance(identifier, str))
        self.host_calls = {}
        self.pending_calls = {}
        self.stray_calls = []
        self.host_handoff = False
        self.emitted_text = False
        self.result_text = None
        self.stop_reason = None
        self.failure = None
        self.terminal = False
        self.current_message = None
        self.last_text_message = None
        self.raw_lines = self.raw_lines[-5:]

    def host_tool_for(self, name) -> str | None:
        """The host tool a native tool_use stands for, when handing it over is safe.

        An ``mcp__host__`` tool is one this turn attached: the CLI never runs
        it (not pre-approved, and the server executes nothing). A plain host
        name is safe only once init has shown the CLI's registry lacks it, so
        nothing can have executed. Anything else stays with the CLI.
        """
        if self.toolset is not None:
            host = self.toolset.host_name(name)
            if host is not None:
                return host
        if isinstance(name, str) and name in self.host_names \
                and self.registry is not None and name not in self.registry:
            return name
        return None

    def attach_check(self) -> None:
        """At init: every attached host tool must be in the model's tool list."""
        if self.toolset is not None and not (self.registry is not None
                                             and self.toolset.model_names <= self.registry):
            self.mcp_unavailable = True

    def begin_native_call(self, index, block) -> None:
        name = block.get("name")
        host = self.host_tool_for(name)
        if host is not None:
            arguments = block.get("input")
            self.pending_calls[index] = {"id": block.get("id"), "name": host,
                                         "input": arguments if isinstance(arguments, dict) else {},
                                         "json": ""}
        elif self.host_names and self.registry is not None and name not in self.registry:
            # Neither the host's nor the CLI's: it would come back "No such
            # tool available", which models read as having no tools at all.
            self.stray_calls.append(str(name))

    def finish_native_call(self, call) -> list[dict]:
        """One forwarded host call, validated and reported once per id."""
        identifier = call.get("id")
        if isinstance(identifier, str) and (identifier in self.host_calls or identifier in self.handed):
            return []
        raw = call.pop("json", "")
        if raw:
            try:
                call["input"] = json.loads(raw)
            except ValueError as exc:
                raise ToolCallError("claude tool call has invalid JSON arguments") from exc
        validate_host_call(call, self.host_tools)
        self.host_calls[identifier] = call
        return [{"type": "tool_call", "id": identifier, "name": call["name"], "input": call["input"]}]

    def end_of_calls(self) -> None:
        """The assistant message that made native calls is complete."""
        if self.host_calls:
            self.host_handoff = True
        elif self.stray_calls:
            raise ToolCallError(
                f"claude called {', '.join(sorted(set(self.stray_calls)))} as a native tool, which neither "
                "the host nor the CLI provides")

    def start_message(self, message):
        identifier = message.get("id") if isinstance(message, dict) else None
        current = {"id": identifier, "blocks": {}, "snapshot": False, "active_block": None}
        if isinstance(identifier, str) and identifier:
            current = self.messages.setdefault(identifier, current)
        self.current_message = current
        return current

    def snapshot_message(self, message):
        identifier = message.get("id")
        current = self.messages.get(identifier) if isinstance(identifier, str) else None
        if current is None:
            active = self.current_message
            compatible = True
            if active is not None:
                content = message.get("content") or []
                if isinstance(content, str):
                    content = [{"type": "text", "text": content}]
                for (index, kind), previous in active["blocks"].items():
                    block = content[index] if isinstance(index, int) and 0 <= index < len(content) else {}
                    text = block.get(kind) if isinstance(block, dict) else None
                    if isinstance(text, str) and not (previous.startswith(text) or text.startswith(previous)):
                        compatible = False
            if active is not None and not active["snapshot"] and (
                    (identifier and active["id"] == identifier)
                    or (compatible and (not identifier or not active["id"]))):
                current = active
                if identifier:
                    current["id"] = identifier
            else:
                current = {"id": identifier, "blocks": {}, "snapshot": False, "active_block": None}
            if isinstance(identifier, str) and identifier:
                self.messages[identifier] = current
        current["snapshot"] = True
        return current

    def content_event(self, index, kind, text, *, message=None, snapshot=False):
        if not isinstance(text, str) or not text:
            return None
        if message is None:
            message = self.current_message
            if message is None:
                message = self.start_message({})
        key = (index, kind)
        previous = message["blocks"].get(key, "")
        if snapshot:
            if previous.startswith(text):
                return None
            if not text.startswith(previous):
                self.failure = "claude assistant snapshot conflicted with its streamed content."
                return None
            fragment = text[len(previous):]
            message["blocks"][key] = text
        else:
            message["active_block"] = key
            fragment = text
            message["blocks"][key] = previous + text
        if kind == "text":
            self.emitted_text = True
            self.last_text_message = message
        return {"type": kind + "_delta", "text": fragment}

    def result_suffix(self):
        """The result is the final answer, potentially missing from snapshots."""
        text = self.result_text or ""
        message = self.last_text_message
        if not text or message is None:
            return text
        parts = [value for (index, kind), value in sorted(message["blocks"].items()) if kind == "text"]
        # Claude Code 2.1.276 projects the last text block into result.result;
        # it need not equal the concatenation of every block in the message.
        if text in parts:
            return ""
        previous = "".join(parts)
        if previous.startswith(text):
            return ""
        return text[len(previous):] if text.startswith(previous) else text

    def stderr_tail(self) -> str:
        handle = self.stderr_handle
        if handle is None:
            return ""
        try:
            handle.seek(0)
            text = handle.read()
        except Exception:
            return ""
        if isinstance(text, bytes):
            text = text.decode("utf-8", "replace")
        text = (text or "").strip()
        if not text:
            return ""
        return " | stderr: " + text[-_MAX_STDERR_CHARS:]

    def diagnostics(self) -> str:
        tail = "".join(self.raw_lines[-5:]).strip()
        if tail:
            return " | cli said: " + tail[-_MAX_STDERR_CHARS:]
        return ""


def _translate(payload: Any, state: _TurnState) -> list[dict]:
    """Convert one NDJSON object from claude into zero or more route events."""
    if not isinstance(payload, dict):
        return []
    kind = payload.get("type")

    if kind == "system" and payload.get("subtype") == "init":
        tools = payload.get("tools")
        if isinstance(tools, list) and all(isinstance(tool, str) for tool in tools):
            state.registry = frozenset(tools)
        state.attach_check()
        return []

    if kind == "stream_event":
        event = payload.get("event")
        if not isinstance(event, dict):
            return []
        if event.get("type") == "message_start":
            state.start_message(event.get("message"))
            return []
        if event.get("type") == "content_block_start":
            block = event.get("content_block") or {}
            if isinstance(block, dict) and block.get("type") in {"text", "thinking"}:
                block_type = block["type"]
                current = state.current_message
                if current is None:
                    current = state.start_message({})
                current["active_block"] = (event.get("index", 0), block_type)
                translated = state.content_event(event.get("index", 0), block_type,
                                                 block.get(block_type), snapshot=True)
                return [translated] if translated else []
            if isinstance(block, dict) and block.get("type") == "tool_use":
                if state.search and block.get("name") == "WebSearch":
                    return [{"type": "web_search", "status": "in_progress", "id": block.get("id")}]
                state.begin_native_call(event.get("index", 0), block)
            return []
        if event.get("type") == "content_block_stop":
            call = state.pending_calls.pop(event.get("index", 0), None)
            return state.finish_native_call(call) if call is not None else []
        if event.get("type") != "content_block_delta":
            # message_delta carries a stop reason too; record it as a fallback
            # so a stream truncated before `result` still reports why it ended
            # instead of silently defaulting to "end_turn".
            if event.get("type") == "message_delta":
                delta = event.get("delta")
                if isinstance(delta, dict):
                    reason = delta.get("stop_reason")
                    if isinstance(reason, str) and reason:
                        state.stop_reason = reason
                    if reason == "tool_use":
                        state.end_of_calls()
            return []
        delta = event.get("delta")
        if not isinstance(delta, dict):
            return []
        delta_type = delta.get("type")
        if delta_type == "text_delta":
            text = delta.get("text")
            translated = state.content_event(event.get("index", 0), "text", text)
            return [translated] if translated else []
        if delta_type == "thinking_delta":
            # Anthropic streams reasoning under delta.thinking; accept .text too
            # so a future field rename degrades instead of dropping reasoning.
            text = delta.get("thinking")
            if not isinstance(text, str):
                text = delta.get("text")
            translated = state.content_event(event.get("index", 0), "thinking", text)
            return [translated] if translated else []
        if delta_type == "input_json_delta":
            call = state.pending_calls.get(event.get("index", 0))
            if call is not None:
                call["json"] += delta.get("partial_json") or ""
                if len(call["json"].encode("utf-8")) > MAX_ENVELOPE_BYTES:
                    raise ToolCallError("claude tool call exceeds the size limit")
        # Other tool traffic (WebSearch arguments) is never surfaced as text.
        return []

    if kind == "assistant":
        message = payload.get("message")
        if isinstance(message, dict):
            current = state.snapshot_message(message)
            content = message.get("content") or []
            if isinstance(content, str):
                content = [{"type": "text", "text": content}]
            events = []
            for index, block in enumerate(content):
                host = state.host_tool_for(block.get("name")) \
                    if isinstance(block, dict) and block.get("type") == "tool_use" else None
                if host is not None:
                    # A snapshot holds only complete blocks, so it can carry a
                    # call whose stream events never arrived.
                    events.extend(state.finish_native_call({
                        "id": block.get("id"), "name": host, "input": block.get("input")}))
                    continue
                if isinstance(block, dict) and block.get("type") in {"text", "thinking"}:
                    block_type = block["type"]
                    # Claude Code emits a one-block assistant snapshot before
                    # content_block_stop, with the same message id but no
                    # stream index. Its local index 0 may be stream block 1+
                    # (e.g. text following thinking). Full snapshots still
                    # use their own indices; distinct blocks remain distinct.
                    active = current.get("active_block")
                    if len(content) == 1 and active and active[1] == block_type:
                        index = active[0]
                    translated = state.content_event(index, block_type, block.get(block_type),
                                                     message=current, snapshot=True)
                    if translated:
                        events.append(translated)
            return events
        return []

    if kind == "result":
        if state.host_calls or state.stray_calls:
            state.end_of_calls()
            return []
        subtype = str(payload.get("subtype") or "")
        stop_reason = payload.get("stop_reason")
        if isinstance(stop_reason, str) and stop_reason:
            state.stop_reason = stop_reason
        result_text = payload.get("result")
        if isinstance(result_text, str) and result_text:
            state.result_text = result_text
        if payload.get("is_error") is True or subtype.startswith("error"):
            detail = result_text or subtype or "unknown error"
            state.failure = f"claude reported {subtype or 'an error'}: {detail}"
        elif subtype == "success":
            state.terminal = True
        else:
            state.failure = f"claude reported an unsuccessful terminal status: {subtype or 'missing'}"
        return []

    if kind == "error":
        message = payload.get("message") or payload.get("error") or payload
        state.failure = f"claude reported an error: {message}"
        return []

    if kind == "user" and (state.host_calls or state.stray_calls):
        # The CLI is answering the calls itself ("No such tool available").
        # Without a message_delta to end on, this is the last safe stop.
        state.end_of_calls()
        return []

    if kind == "user" and state.search:
        return _search_result(payload)

    # system/init, system/status, rate_limit_event, replayed user messages:
    # informational, never route output.
    return []


def _search_result(payload: dict) -> list[dict]:
    """A finished WebSearch, from the tool result the CLI hands back to Claude.

    ``tool_use_result`` carries the query and each result's title and URL; the
    tool_result block carries the id that pairs it with the search it ends.
    """
    found = payload.get("tool_use_result")
    content = (payload.get("message") or {}).get("content")
    ids = [block.get("tool_use_id") for block in content
           if isinstance(block, dict) and block.get("type") == "tool_result"] if isinstance(content, list) else []
    if not isinstance(found, dict) or not isinstance(found.get("query"), str) or not ids:
        return []
    results = []
    for entry in found.get("results") or []:
        for item in (entry.get("content") if isinstance(entry, dict) else None) or []:
            if isinstance(item, dict) and isinstance(item.get("url"), str) \
                    and item["url"].startswith(("https://", "http://")):
                results.append({"url": item["url"],
                                "title": item["title"] if isinstance(item.get("title"), str) else ""})
    return [{"type": "web_search", "status": "completed", "id": ids[0],
             "action": {"type": "search", "query": found["query"]}, "results": results[:20]}]


def search_enabled(value) -> bool:
    """Whether the desktop's hosted search can run here without widening it.

    WebSearch is always live and takes domain filters only as the model's own
    per-call choice, so a cached-only or domain-limited request runs without
    search rather than with a broader one.
    """
    return isinstance(value, dict) and value.get("live") is not False and not value.get("allowed_domains")


def _write_prompt(session, prompt: str) -> None:
    """Write RAW prompt text, then signal EOF.

    Print mode with --input-format text reads the prompt as plain stdin text and
    only ends the turn at EOF, so both the write and the close are load-bearing.
    A JSON-oriented .send() is the wrong shape here; it is used only as a last
    resort for a session host that exposes no raw writer.
    """
    payload = prompt if prompt.endswith("\n") else prompt + "\n"
    for name in ("send_text", "write_text", "write"):
        writer = getattr(session, name, None)
        if callable(writer):
            writer(payload)
            break
    else:
        _write_raw_stdin(session, payload)
    for name in ("close_stdin", "end_input", "close_write"):
        closer = getattr(session, name, None)
        if callable(closer):
            closer()
            return
    _close_raw_stdin(session)


def _raw_stdin(session):
    """The session's writable stdin, wherever the host happens to keep it."""
    for owner in (session, getattr(session, "_proc", None),
                  getattr(session, "process", None)):
        stdin = getattr(owner, "stdin", None)
        if stdin is not None and callable(getattr(stdin, "write", None)):
            return stdin
    return None


def _write_raw_stdin(session, payload: str) -> None:
    """Write raw text straight to stdin, bypassing any JSON framing.

    Falls back to .send() only when no raw handle is reachable at all, so a
    session host exposing just a JSON writer still gets the prompt instead of
    failing the turn outright.
    """
    stdin = _raw_stdin(session)
    if stdin is None:
        session.send(payload)
        return
    try:
        stdin.write(payload)
    except TypeError:
        stdin.write(payload.encode("utf-8"))
    flush = getattr(stdin, "flush", None)
    if callable(flush):
        flush()


def _close_raw_stdin(session) -> None:
    """Signal EOF, without which print mode waits for more input forever."""
    stdin = _raw_stdin(session)
    close = getattr(stdin, "close", None) if stdin is not None else None
    if callable(close):
        try:
            close()
        except OSError:
            pass


def _iter_events(session, *, timeout) -> Iterator[tuple[str, Any]]:
    """Yield ("json", obj) or ("raw", line) for each stdout line.

    Tolerates a session host that yields either parsed dicts or raw strings, and
    never lets a non-JSON line (the CLIs do print plain diagnostics) abort the
    turn: those lines are buffered for error reporting instead.
    """
    for item in session.events(timeout=timeout):
        if isinstance(item, dict):
            yield "json", item
            continue
        if isinstance(item, (bytes, bytearray)):
            item = bytes(item).decode("utf-8", "replace")
        line = str(item).rstrip("\n")
        if not line.strip():
            continue
        try:
            yield "json", json.loads(line)
        except ValueError:
            state_lines = line.strip()
            yield "raw", state_lines


def _interrupt(session) -> None:
    """SIGTERM the child at a host handoff; anything it does next is discarded."""
    terminate = getattr(getattr(session, "process", None), "terminate", None)
    if callable(terminate):
        try:
            terminate()
        except Exception:  # noqa: BLE001 - teardown must never raise
            pass


def _describe(exc: BaseException, state: _TurnState | None = None,
              timeout=None) -> str:
    text = str(exc).strip() or exc.__class__.__name__
    lowered = text.casefold()
    if isinstance(exc, subprocess.TimeoutExpired) or "timed out" in lowered \
            or "timeout" in lowered:
        limit = f" after {timeout}s" if timeout else ""
        text = f"the claude CLI did not finish{limit}"
    elif isinstance(exc, CliSessionError):
        text = f"claude CLI session error: {text}"
    else:
        text = f"claude CLI turn failed: {text}"
    extra = ""
    if state is not None:
        extra = state.diagnostics() + state.stderr_tail()
    return text + extra


def _plan_turn(request) -> dict:
    """Validate the request and gather everything the attempts share. No I/O."""
    if not isinstance(request, dict):
        raise ClaudeCliAgentError("request must be an object")
    model = _validate_model(request.get("model"))
    messages = _coerce_messages(request.get("messages"))
    effort = _validate_effort(request.get("effort"))
    system = request.get("system")
    if system is not None and not isinstance(system, str):
        raise ClaudeCliAgentError("system must be a string or None")
    max_tokens = request.get("max_tokens")
    if max_tokens is not None and not isinstance(max_tokens, int):
        raise ClaudeCliAgentError("max_tokens must be an integer or None")
    if isinstance(max_tokens, int) and max_tokens <= 0:
        raise ClaudeCliAgentError("max_tokens must be positive")
    host_tools = normalize_tools(request.get("tools"))
    tool_choice = request.get("tool_choice")
    history = request.get("history")
    try:
        account_env = _account_env(request.get("config_dir"))
    except ValueError as exc:
        raise ClaudeCliAgentError(str(exc)) from exc
    return {
        "account_env": account_env,
        "model": model, "messages": messages, "effort": effort, "system": system,
        "search": search_enabled(request.get("web_search")),
        "fast_mode": request.get("fast_mode"), "images": request.get("images") or [],
        "host_tools": host_tools,
        "tool_choice": tool_choice if isinstance(tool_choice, dict) else None,
        "toolset": HostToolset(host_tools, prefix=MCP_TOOL_PREFIX) if host_tools else None,
        # Typed Messages history, when the route passes it: live sessions match
        # a host request to the CLI waiting on its calls with it.
        "history": history if isinstance(history, list) else None,
        "timing": request.get("_cli_timing"),
    }


def _surface(plan, mode):
    """The system text and messages for one attempt's host tool surface.

    ``mcp`` explains the attached native tools; ``envelope`` is the fallback
    text manifest plus its anchor on the final user turn, which the route's
    parser still understands; ``None`` is a turn without host tools.
    """
    system, messages = plan["system"] or "", plan["messages"]
    if mode == "mcp":
        system = tools_note(plan["toolset"], plan["tool_choice"]) + ("\n\n" + system if system else "")
    elif mode == "envelope":
        system = render_tool_manifest(plan["host_tools"], plan["tool_choice"]) + ("\n\n" + system if system else "")
        anchor = render_tool_anchor(plan["host_tools"])
        messages = [dict(message) for message in messages]
        for message in reversed(messages):
            if message["role"] == "user":
                message["content"] = (message["content"] + "\n\n" + anchor) if message["content"] else anchor
                break
    return system or None, messages


def _new_state(plan, mode) -> _TurnState:
    state = _TurnState()
    state.search = plan["search"]
    state.host_tools = plan["host_tools"]
    state.host_names = frozenset(tool["name"] for tool in plan["host_tools"])
    state.toolset = plan["toolset"] if mode == "mcp" else None
    return state


def _turn_argv(plan, mode, *, mcp_config=None, run_host_tools=False) -> tuple[list[str], str]:
    """argv (with the resolved binary) and the stdin prompt for one spawn of ``plan``."""
    system, messages = _surface(plan, mode)
    # The system prompt travels as a flag, so it is not also rendered into
    # the prompt; duplicating it would double-charge and could conflict.
    prompt = render_prompt(messages)
    argv = build_argv(plan["model"], effort=plan["effort"], system=system, stream=True,
                      search=plan["search"], fast_mode=plan["fast_mode"], mcp_config=mcp_config,
                      run_host_tools=run_host_tools)
    if plan["images"]:
        argv[1:1] = ["--input-format", "stream-json"]
        prompt = json.dumps({"type": "user", "message": {"role": "user",
            "content": prompt_content(prompt, plan["images"])}}, ensure_ascii=False)
    binary = _resolve_binary()
    if not binary:
        raise ClaudeCliAgentError(
            "the claude CLI was not found on PATH; install Claude Code or "
            "switch this route to API-key credentials"
        )
    argv[0] = binary
    return argv, prompt


def run_turn(request, *, spawner=None, timeout=300, pool=None) -> Iterator[dict]:
    """Stream one host request as text_delta / thinking_delta / message_stop.

    Always terminates with exactly one message_stop or error event, and never
    raises: every failure mode (missing binary, bad request, CLI error result,
    non-zero exit, timeout, unparsable stream) degrades to an error event.

    A turn with host tools attaches them over MCP; when init shows they did
    not attach, it is spawned once more with the text manifest before any
    event has reached the caller. With typed history and a session pool (the
    module's own unless a test ``spawner`` is given) it runs as a live
    session, and a request that continues a waiting CLI resumes it; whenever
    no live session can serve the request, it runs statelessly as before.
    """
    try:
        plan = _plan_turn(request)
    except Exception as exc:
        yield {"type": "error", "message": _describe(exc, None, timeout)}
        return
    started = time.monotonic()
    modes = ("mcp", "envelope") if plan["toolset"] else (None,)
    owner = pool if pool is not None else (_POOL if spawner is None else None)
    if owner is not None and plan["toolset"] and plan["history"] is not None:
        outcome = yield from _live_turn(plan, owner, spawner=spawner, timeout=timeout)
        if outcome == "mcp_unavailable":
            modes = ("envelope",)
        elif outcome != "stateless":
            return
    for mode in modes:
        state = _new_state(plan, mode)
        remaining = max(0.01, timeout - (time.monotonic() - started))
        outcome = yield from _attempt_turn(plan, mode, state, spawner=spawner, timeout=remaining)
        if outcome != "mcp_unavailable":
            return


def _attempt_turn(plan, mode, state, *, spawner, timeout):
    """One spawn of the CLI that ends with the turn or at its first host handoff.

    Returns "mcp_unavailable" when init showed the host tools missing before
    any event was yielded; otherwise ends with exactly one message_stop or
    error event and returns None.
    """
    workspace = None
    session = None

    def release():
        if state.stderr_handle is not None:
            state.stderr_handle.close()
        if workspace is not None:
            shutil.rmtree(workspace, ignore_errors=True)

    try:
        mcp_config = None
        if mode == "mcp":
            workspace = tempfile.mkdtemp(prefix="claude_host_")
            command = server_command(plan["toolset"].write(workspace))
            mcp_config = json.dumps({"mcpServers": {SERVER_NAME: {
                "type": "stdio", "command": command[0], "args": command[1:]}}}, separators=(",", ":"))
        argv, prompt = _turn_argv(plan, mode, mcp_config=mcp_config)
        # stderr goes to a temp file, not a pipe: a pipe nobody drains can fill
        # and deadlock the child, and the text is worth keeping for diagnosis.
        state.stderr_handle = tempfile.TemporaryFile(mode="w+", encoding="utf-8")
        session = StdioSession(
            argv,
            env=minimal_env({**(_MCP_ENV if mode == "mcp" else {}), **plan["account_env"]}),
            timeout=float(timeout),
            spawner=spawner,
            stderr=state.stderr_handle,
        )
    except Exception as exc:
        message = _describe(exc, state, timeout)
        release()
        yield {"type": "error", "message": message}
        return None

    outcome = None
    try:
        with session:
            try:
                _write_prompt(session, prompt)
            except Exception as exc:
                yield {"type": "error", "message": _describe(exc, state, timeout)}
                return None
            outcome = yield from _stream(session, state, timeout=timeout)
    finally:
        # The MCP server reads its tools file while the CLI runs, so the
        # workspace goes only after the child has exited.
        cleanup_after_exit(session, release)
    return outcome


def _stream(session, state, *, timeout, on_handoff=None):
    """Stream the CLI's output as route events until the turn, or this leg of it, ends.

    Returns "mcp_unavailable" when init showed the host tools missing before
    any event was yielded, and "pending" when ``on_handoff`` kept the CLI
    waiting inside its host calls; otherwise None. Every return but
    "mcp_unavailable" follows exactly one message_stop or error event.
    """
    produced = False
    try:
        for kind, payload in _iter_events(session, timeout=timeout):
            if kind == "raw":
                state.raw_lines.append(payload)
                continue
            for event in _translate(payload, state):
                produced = True
                yield event
            if state.mcp_unavailable:
                _interrupt(session)
                if produced:
                    yield {"type": "error", "message": "the host tools could not be attached to the "
                           "claude CLI" + state.diagnostics() + state.stderr_tail()}
                    return None
                # init is the stream's first line, so the respawn is
                # invisible to the client.
                return "mcp_unavailable"
            if state.failure:
                yield {"type": "error", "message": state.failure}
                return None
            if state.host_handoff:
                if on_handoff is not None and on_handoff():
                    # The CLI waits in the bridge for the host's results.
                    yield {"type": "message_stop", "stop_reason": "tool_use"}
                    return "pending"
                # Native calls for host tools the CLI must not run. Stop
                # before its refusal reaches the model; its stdin is already
                # at EOF, so only a signal ends it now.
                _interrupt(session)
                yield {"type": "message_stop", "stop_reason": "tool_use"}
                return None
            if state.terminal:
                returncode = getattr(session, "returncode", None)
                if isinstance(returncode, int) and returncode != 0:
                    yield {"type": "error", "message": f"the claude CLI exited with code {returncode}"}
                    return None
                fallback = state.result_suffix()
                if fallback:
                    state.emitted_text = True
                    yield {"type": "text_delta", "text": fallback}
                if not state.emitted_text:
                    yield {"type": "error", "message": "the claude CLI produced no output"}
                    return None
                yield {"type": "message_stop", "stop_reason": state.stop_reason or "end_turn"}
                return None

        returncode = getattr(session, "returncode", None)
        if isinstance(returncode, int) and returncode != 0:
            detail = f"the claude CLI exited with code {returncode}"
            yield {"type": "error", "message": detail + state.diagnostics() + state.stderr_tail()}
            return None
        if state.failure:
            yield {"type": "error", "message": state.failure}
            return None
        if not state.terminal:
            detail = ("the claude CLI stream ended before completing the turn"
                      if state.emitted_text or state.raw_lines else "the claude CLI produced no output")
            yield {"type": "error", "message": detail}
            return None
        fallback = state.result_suffix()
        if fallback:
            state.emitted_text = True
            yield {"type": "text_delta", "text": fallback}
        if not state.emitted_text:
            yield {"type": "error", "message": "the claude CLI produced no output"}
            return None
        stop_reason = state.stop_reason or "end_turn"
    except ToolCallError as exc:
        # The route's protocol correction keys on this code; nothing ran.
        yield {"type": "error", "code": "invalid_cli_tool_call",
               "message": f"Invalid CLI host tool call: {exc}."}
        return None
    except Exception as exc:
        yield {"type": "error", "message": _describe(exc, state, timeout)}
        return None
    yield {"type": "message_stop", "stop_reason": stop_reason}
    return None


# ---------------------------------------------------------------------------
# Live sessions
# ---------------------------------------------------------------------------

_dispose_live = live_session.dispose
_mcp_result = live_session.mcp_result
_continuation = live_session.continuation


#: Each waiting CLI is a whole Claude Code process; past four, the oldest is
#: retired and its task's next step replays instead.
_POOL = SessionPool(_dispose_live, capacity=4, idle_ttl=0, pending_ttl=LIVE_PENDING_TTL, label="Claude")
atexit.register(_POOL.close)


def _pool_key(plan) -> str:
    """What a waiting CLI must share with a request to continue it: argv and system text."""
    system, _ = _surface(plan, "mcp")
    return digest({"account": plan["account_env"], "model": plan["model"], "effort": plan["effort"], "fast_mode": plan["fast_mode"],
                   "search": plan["search"], "system": system, "tools": plan["host_tools"],
                   "tool_choice": plan["tool_choice"]})


def _spawn_live(plan, state, *, spawner, timeout) -> live_session.LiveSession:
    """Start the CLI with its host tools bridged and pre-approved, and send the prompt."""
    workspace = tempfile.mkdtemp(prefix="claude_host_")
    bridge = None
    try:
        toolset = plan["toolset"]
        bridge = HostCallBridge(workspace, lambda served: toolset.host_name(MCP_TOOL_PREFIX + served))
        command = server_command(toolset.write(workspace, bridge=bridge))
        mcp_config = json.dumps({"mcpServers": {SERVER_NAME: {
            "type": "stdio", "command": command[0], "args": command[1:],
            "timeout": _LIVE_CALL_TIMEOUT_MS}}}, separators=(",", ":"))
        argv, prompt = _turn_argv(plan, "mcp", mcp_config=mcp_config, run_host_tools=True)
        state.stderr_handle = tempfile.TemporaryFile(mode="w+", encoding="utf-8")
        session = StdioSession(argv, env=minimal_env({**_LIVE_ENV, **plan["account_env"]}), timeout=float(timeout),
                               spawner=spawner, stderr=state.stderr_handle)
    except BaseException:
        if bridge is not None:
            bridge.close()
        if state.stderr_handle is not None:
            state.stderr_handle.close()
            state.stderr_handle = None
        shutil.rmtree(workspace, ignore_errors=True)
        raise
    live = live_session.LiveSession(session, workspace, bridge, state, interrupt=_interrupt)
    try:
        _write_prompt(session, prompt)
    except BaseException:
        live.close()
        raise
    return live


def _live_turn(plan, owner, *, spawner, timeout):
    """One host request on a live session, resumed from its waiting calls or started cold.

    Returns "stateless" when no live session can serve the request and
    "mcp_unavailable" when the host tools did not attach - in both cases
    before any event was yielded. Otherwise it ends with exactly one
    message_stop or error event and returns None or "pending".
    """
    deadline = time.monotonic() + timeout
    timing = plan["timing"]
    history = plan["history"]
    key = _pool_key(plan)
    try:
        lease, reuse = owner.acquire(key, match=lambda entry: _continuation(entry, history) is not None,
                                     timeout=max(0.01, deadline - time.monotonic()))
    except (RuntimeError, TimeoutError):
        # A full pool or a continuation still being released: this request
        # replays the conversation instead of waiting on either.
        return "stateless"
    except Exception as exc:  # the client cancelled while the lease was awaited
        yield {"type": "error", "message": _describe(exc, None, timeout)}
        return None
    try:
        live = None
        if reuse == "resumed":
            results = _continuation(lease, history)
            lease.pending = None
            live = lease.session
            if results is None or not live.resume(results):
                # Nothing has reached the client: retire the CLI and replay.
                return "stateless"
        if timing:
            timing.label(reuse="resumed" if live is not None else "cold")
        if live is None:
            state = _new_state(plan, "mcp")
            try:
                live = _spawn_live(plan, state, spawner=spawner, timeout=max(0.01, deadline - time.monotonic()))
            except HostBridgeError:
                return "stateless"
            except Exception as exc:
                yield {"type": "error", "message": _describe(exc, state, timeout)}
                return None
            lease.session = live
        return (yield from _stream(live.session, live.state, timeout=max(0.01, deadline - time.monotonic()),
                                   on_handoff=lambda: live_session.hold(
                                       live, lease, history, deadline, events=_iter_events,
                                       translate=_translate, seconds=_LIVE_CONFIRM_SECONDS)))
    finally:
        # Only a handoff the client has received keeps the CLI waiting. Any
        # other ending closes it now, as a stateless turn would, rather than
        # leaving it to the pool's reaper.
        keep = bool(lease.pending) and (timing is None or timing.delivered)
        if not keep and lease.session is not None:
            lease.session.close()
        owner.release(lease, healthy=keep)
