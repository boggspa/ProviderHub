"""Grok CLI as a hub MODEL ROUTE (local-only, default-off experiment).

Route 2 — CLI-as-transport. The vendor CLI owns and refreshes its own login.
This module NEVER reads, copies, or refreshes a credential file, and never
touches the Keychain: ``auth_state()`` checks ``grok models`` and believes only
what that read-only subcommand reports (it lists models when authenticated).

These routes are deliberately **pure text-in/text-out** so they behave like
model routes instead of delegatory headless agents. The desktop harness owns
the tool loop; if this route also ran tools, every tool call would be executed
twice against two different views of the world. So the whole built-in tool set
is switched off with an explicit internal-tool removal list, verified against
``system/init.tools`` before host handoffs. An empty ``--tools ""`` alone is
insufficient in 1.0.34. Native-shaped model calls may be forwarded only after
that empty-registry check and only for tools actually offered by the host.

The registry is not only built-ins. By default the CLI also imports MCP
servers from Cursor's and Claude Code's configs (``[compat.cursor] mcps`` and
``[compat.claude] mcps``: ``~/.cursor/mcp.json``, ``~/.claude.json``), and
their tools land in the same ``init.tools`` list whenever a server handshake
wins the startup race against the turn start (observed on 1.0.41: the
taskwraith server connected 1 ms before ``turn_started`` and the guard
tripped, while every other run of the day passed). Hub turns therefore switch
both scans off for the child process alone, through the documented env cells
``GROK_CURSOR_MCPS_ENABLED=0`` and ``GROK_CLAUDE_MCPS_ENABLED=0`` (verified:
init reports ``mcp_servers: []`` and the session records no MCP events). The
user's own config is never edited. Should the registry still name tools (a
built-in added by a newer CLI, a native ``[mcp_servers]`` entry), the turn is
respawned once with those names appended to the removal list, names that
worked are remembered for the process lifetime, and a second failure reports
the names instead of a bare refusal.

Host tools ride grok's own ``use_tool`` function. Grok Build never offers a
non-built-in tool to its model directly: MCP tools too are reached only
through ``search_tool``/``use_tool`` (grok 1.0.41). So a turn with host tools
keeps ``use_tool`` alone out of the removal list, lists the host's tools with
their schemas in the system text (no ``search_tool`` round trip), and reads
each ``use_tool`` call - ``{"tool_name", "tool_input"}`` - off the stream as a
host call. ``--deny MCPTool`` stays, so the CLI refuses to dispatch it: the
call streams first, and the turn ends at that message before the refusal
reaches the model. Verified live on 1.0.41: init.tools ``["use_tool"]``, and
the model called ``use_tool`` with the right tool and the schema's own
argument names. This replaces the text envelope, which Grok followed but
which competed with its native tools. If init does not list ``use_tool`` the
turn is spawned again, invisibly, with the envelope manifest.

Live sessions (see cli_live_session). With typed history from the route, the
turn instead lets grok dispatch ``use_tool`` to a ``host`` MCP server that
forwards each call to the hub (cli_host_bridge) and waits, so the CLI waits
inside its own tool call and the next host request continues the same
process. The server is registered in the turn's private workspace
(``.grok/config.toml``, loaded headless with ``GROK_FOLDER_TRUST=0``, which
the workspace's contents make safe). Only that server is pre-approved
(``--allow MCPTool(host__*)`` in place of the blanket MCPTool deny; the other
denies stay). The model is shown each tool under its catalog key
``host__<tool>``. That is the one spelling grok's dispatcher resolves, so a
host name its catalog would skip (a second ``__``, say) is served under an
alias. Grok's MCP client sends no tool-call id, so the bridge pairs each
waiting call with the stream's by tool and arguments. Verified on 1.0.41:
two ``use_tool`` calls in one message reached the server together, and grok
returned their results to its model. Grok writes a message's end only after
its calls return, so a live leg hands off once the stream has gone quiet
with every call it read waiting in the bridge (``_LIVE_QUIET_SECONDS``). The
deferred end arrives on the next leg and belongs to a message already handed
over. A call that streams in after the handoff is handed over on its own.

One exception, only when the desktop asked for hosted web search: the turn
keeps web_search alone (``--tools web_search``, init.tools ``["web_search"]``).
On models whose cache entry sets ``supports_backend_search`` the search runs
on xAI's servers and arrives inline as a server_tool_use /
web_search_tool_result pair; web_fetch, which fetches locally, stays off.
Each search is relayed as a ``web_search`` event.

Verified live against grok 1.0.34 (3736acbc8658):
  - Binary at ~/.grok/bin/grok (self-updates on launch)
  - ``--help`` confirms: --no-auto-update, --single/-p, --output-format streaming-messages-json,
    --permission-mode plan, --no-subagents, --tools, --model, --reasoning-effort
    (aliases: --effort), --system-prompt-override (compat: --system-prompt)
  - ``grok models`` returns authenticated models list (2 models: grok-4.6, grok-4.5)
  - grok 1.0.40 lists grok-4.7 (default), grok-4.7-build-fast, grok-4.6, grok-4.5
  - ``--reasoning-effort`` accepts: low, medium, high, xhigh (no none, no ultra)
  - ``--tools ""`` alone leaves native tools enabled; the removal list and
    init.tools validation below are required
  - ``--output-format streaming-messages-json`` outputs NDJSON in Anthropic Messages API format
  - Prompt travels as positional argument to ``--single``, NOT on stdin
    (verified: ``--single`` with no positional aborts with "a value is required
    for '--single <PROMPT\u003e'"; stdin is ignored). argv is bounded by the
    kernel, so past ~256 KB (prompt plus system override together) the prompt
    travels in the workspace prompt file instead, the ACP JSON transport that
    screenshots already use (``--prompt-file``; verified on 1.0.41 with a
    text-only file).
  - Variadic: ``--tools`` must be last on command line

INVARIANT: Every argv this module builds carries ``--no-auto-update``. The
grok CLI self-updates on launch; this flag prevents the hub from accidentally
triggering a binary swap during capability checks or turns. This is stated
in the module docstring and asserted in ``build_argv``.

Transport: one-shot single-turn mode with streaming-messages-json NDJSON.
The conversation is stateless: history is rendered into the prompt itself.
SYSTEM_PROMPT_TRANSPORT is "flag" (``--system-prompt-override``).
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
from typing import Any, Iterator

from cli_session import CliSessionError, StdioSession, minimal_env, resolve_binary
from cli_lifecycle import cleanup_after_exit
from cli_host_bridge import HostBridgeError, HostCallBridge
from cli_host_mcp import SERVER_NAME, HostToolset, server_command
import cli_live_session as live_session
from cli_tool_call import (MAX_ENVELOPE_BYTES, TRANSCRIPT_HEADER, ToolCallError,
                           normalize_tools, render_tool_anchor, render_tool_manifest,
                           render_use_tool_manifest, validate_host_call)
from cli_images import prompt_content
from codex_session_pool import SessionPool, digest

try:
    from effort_map import map_effort as _map_effort
except Exception:  # pragma: no cover - defensive import guard
    _map_effort = None


class GrokCliAgentError(RuntimeError):
    """The Grok CLI route could not be described, resolved, or driven."""


# ---------------------------------------------------------------------------
# Provider identity
# ---------------------------------------------------------------------------

PROVIDER_ID = "grok"
PROVIDER_NAME = "Grok (Grok Build CLI)"
TRANSPORT = "print"
BINARY_NAMES = ("grok",)

# Where the harness system prompt is delivered.
# "flag" => build_argv emits it via --system-prompt-override
# "prompt" => the CLI has no such flag and it is folded into the stdin prompt.
SYSTEM_PROMPT_TRANSPORT = "flag"
IMAGE_TRANSPORT = "acp_json_file"
VERIFIED_IMAGE_MODELS = frozenset({"grok-4.6"})
#: Backend web search runs on xAI's servers and never fetches a page locally,
#: so web_search is the one tool a turn may enable (web_fetch stays off).
WEB_SEARCH = True
#: Host tools travel through grok's own ``use_tool`` dispatcher as native
#: structured calls; cli_routes renders no manifest and run_turn writes it.
HOST_TOOL_TRANSPORT = "use_tool"
_USE_TOOL = "use_tool"
#: grok's catalogue key for a tool of an MCP server named "host" (server__tool);
#: a model that spells a host tool that way still reaches it.
_HOST_KEY_PREFIX = f"{SERVER_NAME}__"
#: The surfaces on which host calls ride use_tool: refused by the CLI
#: ("use_tool"), or dispatched to the bridged host server ("live").
_USE_TOOL_MODES = frozenset({"use_tool", "live"})
#: What grok 1.0.41 admits as the tool half of a catalog key: letters, digits,
#: ``_`` and ``-``, with no leading underscore and no ``__`` inside.
_CATALOG_TOOL = re.compile(r"(?!_)(?!.*__)[A-Za-z0-9_-]+")
#: grok's catalog-key ceiling; the 64-character cap is for use_tool itself.
_CATALOG_KEY_LIMIT = 256
#: The route passes typed history so a host call can continue a live session.
LIVE_HOST_CALLS = True
#: How long a CLI may wait for the host's result before it is retired and the
#: next step replays the conversation into a fresh process instead.
LIVE_PENDING_TTL = 900
#: The bridged server's per-call ceiling (tool_timeout_sec), past LIVE_PENDING_TTL.
_LIVE_CALL_TIMEOUT_SEC = 3600
#: How long the CLI has, once its message ends, to be waiting on every call.
_LIVE_CONFIRM_SECONDS = 15.0
#: grok 1.0.41 dispatches each use_tool call as its block closes but writes the
#: message's end (message_delta, message_stop, snapshot) only once every call
#: has returned. So on a live session the handoff comes when every call read so
#: far is waiting in the bridge and the stream has been quiet this long.
_LIVE_QUIET_SECONDS = 0.75
#: A live child loads the workspace's server config headless and keeps host
#: results inline (grok spills MCP results past 20,000 bytes to a file).
_LIVE_ENV_EXTRA = {"GROK_FOLDER_TRUST": "0", "GROK_MAX_MCP_OUTPUT_BYTES": str(8 * 1024 * 1024)}

# Real installs live in ~/.grok/bin on this machine.
_EXTRA_BIN_DIRS = ("~/.grok/bin", "~/.local/bin", "/opt/homebrew/bin", "/usr/local/bin")

# ---------------------------------------------------------------------------
# Safety invariants
# ---------------------------------------------------------------------------

#: Largest prompt-plus-system byte length delivered on argv (the positional
#: --single value and --system-prompt-override). macOS ARG_MAX is ~1 MB; past
#: this margin the prompt moves into the workspace prompt file so a long host
#: conversation keeps working instead of being refused before spawn.
_MAX_PROMPT_ARGV_BYTES = 256 * 1024

# Never constructed, never forwarded, asserted against in build_argv.
_FORBIDDEN_FLAGS = frozenset({
    "--always-approve",
})

# Internal IDs differ from the names shown in init.tools. Empty --tools alone
# leaves the stock toolset enabled in Grok 1.0.34. Remove built-ins explicitly,
# including MCP dispatch and task tools, then verify init.tools is empty.
# 1.0.41 added sports_search (X data lookups); a built-in this list does not
# know yet is removed by name on the turn's one respawn (see run_turn).
_DISALLOWED_TOOLS = (
    "run_terminal_cmd", "run_terminal_command", "read_file", "search_replace",
    "list_dir", "grep", "kill_task", "get_task_output", "task", "Agent",
    "kill_command_or_subagent", "get_command_or_subagent_output", "spawn_subagent",
    "todo_write", "scheduler_create", "scheduler_delete", "scheduler_list",
    "monitor", "search_tool", "use_tool", "workflow", "enter_plan_mode",
    "exit_plan_mode", "ask_user_question", "send_feedback", "image_gen",
    "image_edit", "image_to_video", "reference_to_video", "write",
    "web_search", "web_fetch", "memory_search", "memory_get", "lsp",
    "sports_search",
)

# dontAsk denies approval prompts. Explicit deny rules also cover tools
# permitted by inherited config; the host owns all execution permissions.
READ_ONLY_FLAGS = (
    "--permission-mode", "dontAsk",
    "--no-subagents",
    "--disable-web-search",
    "--deny", "Bash", "--deny", "Read", "--deny", "Edit",
    "--deny", "Write", "--deny", "Grep", "--deny", "WebFetch", "--deny", "MCPTool",
    "--disallowed-tools", ",".join(_DISALLOWED_TOOLS),
)

# Variadic: must stay last in argv or it swallows every following flag.
_VARIADIC_TOOL_FLAGS = ("--tools", "--allow", "--allowedTools",
                        "--deny", "--disallow", "--disallowedTools",
                        "--disallowed-tools")

# Per-process compat cells (documented in the CLI's config reference; env wins
# over config.toml). They stop the child importing MCP servers from Cursor's
# and Claude Code's configs, whose tools would otherwise race into
# init.tools. Nothing here edits the user's ~/.grok/config.toml.
_CHILD_ENV = {
    "GROK_CURSOR_MCPS_ENABLED": "0",
    "GROK_CLAUDE_MCPS_ENABLED": "0",
}

# A tool name the CLI reported may join the removal list only in this shape:
# one argv element value, never something clap could read as a flag.
_REMOVAL_NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.:/-]{0,127}\Z")
_MAX_LEARNED_REMOVALS = 64

#: Names whose removal let a respawned turn pass the registry check. Every
#: later argv carries them, so a newer CLI's built-in costs one wasted spawn
#: per hub process rather than one per turn.
_LEARNED_REMOVALS: set[str] = set()

# ---------------------------------------------------------------------------
# Models and effort
# ---------------------------------------------------------------------------

# grok --reasoning-effort accepts exactly these (verified 1.0.34).
# The CLI rejects "none" and "ultra"; it accepts low, medium, high, xhigh.
GROK_EFFORTS = ["low", "medium", "high", "xhigh"]

# Map desktop effort ladder onto grok's supported reasoning efforts.
# Desktop: none, minimal, low, medium, high, xhigh, max, ultra
# Grok:    low, medium, high, xhigh (no none, no ultra)
# Strategy: none/minimal -> low, max/ultra -> xhigh, others pass through.
GROK_EFFORT_ALIASES = {
    "none": "low",
    "minimal": "low",
    "low": "low",
    "medium": "medium",
    "high": "high",
    "xhigh": "xhigh",
    "max": "xhigh",
    "ultra": "xhigh",
}

# Known models from ``grok models`` (1.0.40, 2026-09-22): grok-4.7 (default),
# grok-4.7-build-fast (Grok 4.7 Fast - Grok Build/Cursor only, not the public
# API), grok-4.6, grok-4.5. These are UNVERIFIED SEED for the hub's model
# picker; catalogue() returns nothing because grok has no read-only model list
# subcommand.
KNOWN_MODELS = ("grok-4.7", "grok-4.7-build-fast", "grok-4.6", "grok-4.5")
DEFAULT_MODEL = "grok-4.7"

_NO_MODEL_LIST_WARNING = (
    "grok has no non-interactive model list; catalogue is seeded from "
    "KNOWN_MODELS and live verification"
)

# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def _resolve_binary() -> str | None:
    """Resolve the grok binary from PATH and known extra directories."""
    # _EXTRA_BIN_DIRS holds "~/.grok/bin" and "~/.local/bin"; resolve_binary
    # compares real paths, so an unexpanded "~" entry can never match.
    extra = tuple(str(Path(item).expanduser()) for item in _EXTRA_BIN_DIRS)
    return resolve_binary(BINARY_NAMES, extra_dirs=extra)


def _invoke(capture, argv, *, timeout=30) -> tuple[int | None, str, str]:
    """Run a grok subcommand and return (returncode, stdout, stderr)."""
    binary = _resolve_binary()
    if not binary:
        return (None, "", "grok CLI not found")
    full_argv = [binary] + argv
    try:
        result = subprocess.run(
            full_argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=minimal_env(),
        )
        return (result.returncode, result.stdout, result.stderr)
    except subprocess.TimeoutExpired:
        return (-1, "", f"Command timed out after {timeout}s")
    except Exception as exc:
        return (-1, "", str(exc))


def discover(*, capture=None) -> dict:
    """Report whether the CLI is installed and which version it claims."""
    binary = _resolve_binary()
    if not binary:
        return {"installed": False, "binary": None, "version": None}
    version = None
    try:
        rc, out, err = _invoke(capture, ["--version"], timeout=30)
        if rc in (0, None):
            version = out.strip().split("\n")[0] if out.strip() else None
    except Exception:
        version = None
    return {"installed": True, "binary": binary, "version": version}


def auth_state(*, capture=None) -> dict:
    """Ask the CLI, and only the CLI, whether it is signed in.

    Uses ``grok models`` which returns the available models list when
    authenticated, or an error when not. Never reads ~/.grok or the Keychain.
    """
    binary = _resolve_binary()
    if not binary:
        return {"state": "missing",
                "detail": "the grok CLI is not installed or not on PATH"}
    try:
        rc, out, err = _invoke(capture, ["models"], timeout=30)
    except Exception as exc:
        return {"state": "unknown", "detail": f"models probe failed: {exc}"}

    if rc not in (0, None):
        detail = (err or out).strip().splitlines()
        err_msg = err.strip() if err.strip() else ""
        if "not logged in" in err_msg.casefold() or "login" in err_msg.casefold():
            return {"state": "missing",
                    "detail": "grok reports no active login; sign in with the grok CLI itself"}
        return {"state": "unknown",
                "detail": f"grok models exited {rc}" + (f": {detail[-1]}" if detail else "")}

    if "logged in" in out.casefold():
        detail = "signed in"
        for line in out.splitlines():
            if "logged in with" in line.casefold():
                detail = line.strip()
                break
        return {"state": "authenticated", "detail": detail}

    return {"state": "unknown",
            "detail": "grok models returned unexpected output"}


def catalogue(*, capture=None, timeout=30) -> tuple[list[dict], list[str]]:
    """No discovery subcommand exists: grok has no read-only model list.

    Returns an empty catalogue plus the reason, and never spawns anything.
    The hub should seed route names from KNOWN_MODELS.
    """
    return [], [_NO_MODEL_LIST_WARNING]


_MODELS_CACHE = Path("~/.grok/models_cache.json")


def search_models() -> frozenset[str]:
    """Models whose web searches run on xAI's servers, per the CLI's own cache.

    grok writes ~/.grok/models_cache.json from its models endpoint; it holds no
    credential (auth lives in auth.json, which this module never opens). A
    model with ``supports_backend_search`` reports each search inline, which
    run_turn relays. Any other model would call the client web_search tool
    instead, which this route does not relay, so it stays without search. No
    readable cache means no search.
    """
    try:
        with _MODELS_CACHE.expanduser().open(encoding="utf-8") as handle:
            cache = json.load(handle)
    except (OSError, ValueError):
        return frozenset()
    models = cache.get("models") if isinstance(cache, dict) else None
    if not isinstance(models, dict):
        return frozenset()
    return frozenset(identifier for identifier, entry in models.items()
                     if isinstance(entry, dict) and isinstance(entry.get("info"), dict)
                     and entry["info"].get("supports_backend_search") is True)


# ---------------------------------------------------------------------------
# argv construction
# ---------------------------------------------------------------------------


def _validate_model(model: str | None) -> str:
    """Validate and return the model identifier."""
    if model is None:
        return DEFAULT_MODEL
    if not isinstance(model, str):
        raise GrokCliAgentError(f"model must be a string, got {type(model).__name__}")
    stripped = model.strip()
    if not stripped:
        return DEFAULT_MODEL
    return stripped


def _validate_effort(effort: str | None) -> str | None:
    """Validate and map the effort level to a grok-supported value."""
    if effort is None:
        return None
    if not isinstance(effort, str):
        raise GrokCliAgentError(f"effort must be a string or None, got {type(effort).__name__}")
    normalized = GROK_EFFORT_ALIASES.get(effort)
    if normalized is None:
        raise GrokCliAgentError(
            f"unknown effort level {effort!r}; supported: {', '.join(GROK_EFFORTS)}"
        )
    if normalized not in GROK_EFFORTS:
        raise GrokCliAgentError(
            f"effort {normalized!r} is not supported by grok; supported: {', '.join(GROK_EFFORTS)}"
        )
    return normalized


def _assert_safe(argv: list[str]) -> list[str]:
    """Assert that argv does not contain any forbidden flag."""
    for arg in argv[1:]:
        if arg in _FORBIDDEN_FLAGS:
            raise GrokCliAgentError(
                f"internal error: forbidden flag {arg!r} found in argv"
            )
        for forbidden in _FORBIDDEN_FLAGS:
            if arg.startswith(forbidden):
                raise GrokCliAgentError(
                    f"internal error: argv contains {forbidden!r} prefix: {arg!r}"
                )
    return argv


def build_argv(model, *, effort=None, system=None, stream=True, search=False,
               extra_removals=(), host_tools=False, run_host_tools=False) -> list[str]:
    """Full argv for one print-mode turn. argv[0] is the bare binary name.

    ``run_turn`` replaces argv[0] with the resolved absolute path; keeping the
    name here makes this function pure and testable on a machine with no CLI
    installed.

    ``search`` keeps exactly one tool, web_search: it leaves the removal list
    and --tools, and --disable-web-search is dropped. web_search is read-only,
    so dontAsk runs it without a prompt; web_fetch stays removed and denied.

    ``extra_removals`` are further names for ``--disallowed-tools``: what the
    CLI's own init line reported still registered. Removal can only narrow,
    so they are appended as given, except a name that could not be a plain
    value (``_REMOVAL_NAME``) or web_search on a search turn.

    ``host_tools`` keeps ``use_tool``, the dispatcher host calls travel
    through. ``--deny MCPTool`` still refuses to run anything it names.
    ``run_host_tools`` (a live session) lets it reach the bridged ``host``
    server alone: ``--allow MCPTool(host__*)`` replaces that one deny, and
    the server forwards each call to the hub, running nothing itself.

    INVARIANT: Every argv carries ``--no-auto-update`` to prevent the CLI from
    self-updating during capability checks or turns.

    Note: The prompt is NOT part of argv — for grok, it is passed as the
    positional argument to ``--single`` by ``run_turn``, not included here.
    The system prompt travels as ``--system-prompt-override``.
    """
    validated_model = _validate_model(model)
    validated_effort = _validate_effort(effort)
    if run_host_tools and not host_tools:
        raise GrokCliAgentError("run_host_tools needs host_tools")

    argv: list[str] = [BINARY_NAMES[0]]

    # INVARIANT: --no-auto-update must be present on every invocation
    argv += ["--no-auto-update"]

    # Single-turn mode (value will be inserted by run_turn)
    argv += ["--single"]

    # Output format: streaming-messages-json for NDJSON Anthropic Messages API format
    if stream:
        argv += ["--output-format", "streaming-messages-json", "--include-partial-messages"]
    else:
        argv += ["--output-format", "json"]

    # Read-only posture
    flags = list(READ_ONLY_FLAGS)
    kept = ({"web_search"} if search else set()) | ({_USE_TOOL} if host_tools else set())
    removals = [tool for tool in _DISALLOWED_TOOLS if tool not in kept]
    for name in extra_removals:
        if isinstance(name, str) and _REMOVAL_NAME.fullmatch(name) and name not in removals \
                and name not in kept:
            removals.append(name)
    flags[flags.index("--disallowed-tools") + 1] = ",".join(removals)
    if search:
        flags.remove("--disable-web-search")
    if run_host_tools:
        at = flags.index("MCPTool")
        flags[at - 1:at + 1] = ["--allow", f"MCPTool({_HOST_KEY_PREFIX}*)"]
    argv += flags

    # Model selection
    argv += ["--model", validated_model]

    # Reasoning effort
    if validated_effort:
        argv += ["--reasoning-effort", validated_effort]

    # System prompt as flag (before variadic --tools)
    if system is not None:
        if not isinstance(system, str):
            raise GrokCliAgentError("system must be a string or None")
        system_text = system.strip()
        if system_text:
            if system_text.startswith("-"):
                argv += [f"--system-prompt-override={system_text}"]
            else:
                argv += ["--system-prompt-override", system_text]

    # Variadic flag: --tools must be last
    argv += ["--tools", "web_search" if search else ""]

    # Safety: verify variadic flag is at the end
    if argv[-2] not in _VARIADIC_TOOL_FLAGS:
        raise GrokCliAgentError(
            "internal error: a variadic flag must terminate argv, otherwise it "
            "swallows everything after it"
        )

    # INVARIANT: assert --no-auto-update is present
    if "--no-auto-update" not in argv:
        raise GrokCliAgentError(
            "internal error: --no-auto-update must be present in every argv"
        )

    return _assert_safe(argv)


# ---------------------------------------------------------------------------
# Prompt rendering
# ---------------------------------------------------------------------------

# Roles supported by the route
_ROLES = frozenset({"user", "assistant"})

_TRANSCRIPT_HEADER = TRANSCRIPT_HEADER
_TRANSCRIPT_FOOTER = 'Respond now to the final <turn role="user"> above.'

_MAX_STDERR_CHARS = 200


def _coerce_messages(messages: Any) -> list[dict]:
    """Normalize the messages list for rendering."""
    if not isinstance(messages, (list, tuple)):
        raise GrokCliAgentError("request messages must be a list")
    normalized: list[dict] = []
    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            raise GrokCliAgentError(f"message {index} must be an object")
        role = message.get("role")
        if role not in _ROLES:
            raise GrokCliAgentError(
                f"message {index} has unsupported role {role!r}; "
                f"expected one of {', '.join(_ROLES)}"
            )
        content = message.get("content")
        if content is None:
            content = ""
        if not isinstance(content, str):
            content = str(content)
        normalized.append({"role": role, "content": content})
    if not any(item["role"] == "user" for item in normalized):
        raise GrokCliAgentError("request contains no user message to answer")
    return normalized


def render_prompt(messages, *, system=None) -> str:
    """Render the whole stateless conversation into one prompt string.

    Order is system, then user/assistant turns in the order given.
    A single bare user turn with no system text is passed through verbatim.
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
    blocks.append(_TRANSCRIPT_FOOTER)
    return "\n\n".join(blocks) + "\n"


# ---------------------------------------------------------------------------
# Turn execution
# ---------------------------------------------------------------------------


class _TurnState:
    """Mutable accumulation for one streamed turn."""

    def __init__(self) -> None:
        self.emitted_text = False
        self.assistant_text: list[str] = []
        self.result_text: str | None = None
        self.stop_reason: str | None = None
        self.failure: str | None = None
        self.raw_lines: list[str] = []
        self.stderr_handle = None
        self.terminal = False
        self.messages = {}
        self.current_message = None
        self.native_tools_disabled = False
        # Names init.tools carried beyond the allowed set; None until init.
        self.native_tools_seen = None
        # True on the respawn, so a repeat failure says the removal was tried.
        self.retried = False
        self.host_tools = []
        self.host_handoff = False
        self.pending_calls = {}
        # Host calls read off this message, by id; handed over at its end.
        self.host_calls = {}
        # "use_tool" when host calls ride grok's dispatcher (refused by the
        # CLI), "live" when it dispatches them to the bridged host server,
        # "envelope" for the text fallback, None for a turn without host tools.
        self.mode = None
        # Catalog keys (host__<tool>) of a live session's bridged server.
        self.toolset = None
        # Host calls handed over on earlier legs of a live session, and the
        # messages that made them (grok ends a message only after its calls).
        self.handed = set()
        self.handed_messages = set()
        # init did not list use_tool: respawn with the envelope manifest.
        self.use_tool_missing = False
        self.backend_calls = set()
        self.search = False
        self.searches = {}
        self.pending_searches = {}

    def next_leg(self) -> None:
        """Start the next host request on a live session.

        What one request reports starts afresh; what the process has already
        shown (its registry, the messages and searches it has streamed) stays,
        so the snapshot of the handoff message, which grok sends after the
        message ends, repeats neither text, searches nor calls.
        """
        self.handed.update(identifier for identifier in self.host_calls if isinstance(identifier, str))
        if self.current_message is not None:
            # Still open: its end, and perhaps more of it, arrive on this leg.
            self.handed_messages.add(id(self.current_message))
        self.host_calls = {}
        self.pending_calls = {}
        self.pending_searches = {}
        self.host_handoff = False
        self.emitted_text = False
        self.assistant_text = []
        self.result_text = None
        self.stop_reason = None
        self.failure = None
        self.terminal = False
        self.raw_lines = self.raw_lines[-5:]

    def start_message(self, message):
        identifier = message.get("id") if isinstance(message, dict) else None
        current = {"id": identifier, "blocks": {}, "snapshot": False}
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
                for (index, kind), previous in active["blocks"].items():
                    block = content[index] if isinstance(index, int) and 0 <= index < len(content) else {}
                    text = block.get(kind) if isinstance(block, dict) else None
                    if isinstance(text, str) and not (previous.startswith(text) or text.startswith(previous)):
                        compatible = False
            # Older runtimes omit IDs. Associate only the first snapshot with
            # the active stream, never deduplicate prose across distinct turns.
            if active is not None and not active["snapshot"] and (
                    (identifier and active["id"] == identifier)
                    or (compatible and (not identifier or not active["id"]))):
                current = active
                if identifier:
                    current["id"] = identifier
            else:
                current = {"id": identifier, "blocks": {}, "snapshot": False}
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
                self.failure = "grok assistant snapshot conflicted with its streamed content."
                return None
            fragment = text[len(previous):]
            message["blocks"][key] = text
        else:
            fragment = text
            message["blocks"][key] = previous + text
        if kind == "text":
            self.emitted_text = True
        return {"type": kind + "_delta", "text": fragment}

    def host_call(self, call):
        arguments = call.get("input")
        # Grok also reports provider-side searches in the tool_use channel.
        # They are progress events, not executable host filesystem calls.
        if isinstance(arguments, dict) and arguments.get("backend") is True \
                and arguments.get("variant") in {"XSearch", "WebSearch"}:
            identifier = call.get("id")
            if identifier in self.backend_calls:
                return None
            self.backend_calls.add(identifier)
            return {"type": "thinking_delta", "text": f"Grok backend activity: {arguments['variant']}.\n"}
        if not self.native_tools_disabled:
            raise ToolCallError("grok attempted a native CLI tool before confirming tool isolation")
        identifier = call.get("id")
        if isinstance(identifier, str) and identifier in self.handed:
            return None
        if call.get("name") == _USE_TOOL and self.mode in _USE_TOOL_MODES:
            call = _unwrap_use_tool(call, self.host_tools, self.toolset)
        if isinstance(identifier, str) and identifier in self.host_calls:
            return None
        validate_host_call(call, self.host_tools)
        self.host_calls[identifier] = call
        return {"type": "tool_call", **call}

    def end_of_calls(self) -> None:
        """The message that made host calls is complete: hand them over."""
        if self.host_calls:
            self.host_handoff = True

    def search_started(self, block):
        """A backend search's server_tool_use, reported once per id.

        The partial stream opens the block with empty input and sends the
        query as one input_json_delta; the whole-message snapshot repeats it
        complete, so a later sighting only fills in a missing query.
        """
        identifier = block.get("id")
        arguments = block.get("input") if isinstance(block.get("input"), dict) else {}
        if not isinstance(identifier, str) or not identifier:
            return None
        search = self.searches.get(identifier)
        if search is not None:
            if arguments and not search["input"]:
                search["input"] = arguments
            return None
        self.searches[identifier] = {"input": arguments, "done": False}
        return {"type": "web_search", "status": "in_progress", "id": identifier}

    def search_finished(self, block):
        """The web_search_tool_result that ends a search, reported once.

        Its content is the hit list, or an error object for a failed search,
        which finishes with no results.
        """
        identifier = block.get("tool_use_id")
        if not isinstance(identifier, str) or not identifier:
            return []
        events = []
        if identifier not in self.searches:
            events.append(self.search_started({"id": identifier}))
        search = self.searches[identifier]
        if search["done"]:
            return events
        search["done"] = True
        content = block.get("content")
        results = [{"url": hit["url"], "title": hit["title"] if isinstance(hit.get("title"), str) else ""}
                   for hit in (content if isinstance(content, list) else [])
                   if isinstance(hit, dict) and hit.get("type") == "web_search_result"
                   and isinstance(hit.get("url"), str) and hit["url"].startswith(("https://", "http://"))]
        events.append({"type": "web_search", "status": "completed", "id": identifier,
                       "action": _search_action(search["input"]), "results": results[:20]})
        return events

    def fallback_text(self) -> str:
        if self.assistant_text:
            return "".join(self.assistant_text)
        return self.result_text or ""

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


def _iter_events(session: StdioSession, *, timeout=300) -> Iterator[tuple[str, Any]]:
    """Iterate over NDJSON events from the CLI.

    Reads through the session host's events() like the sibling adapters:
    parsed dicts arrive as dicts, anything else as one raw line per item.
    """
    for item in session.events(timeout=timeout):
        if isinstance(item, dict):
            yield ("parsed", item)
            continue
        line = str(item).strip()
        if not line:
            continue
        if line.startswith("\x1b["):
            continue
        if not line.startswith("{"):
            continue
        try:
            payload = json.loads(line)
            yield ("parsed", payload)
        except (json.JSONDecodeError, ValueError):
            yield ("raw", line)


def _translate(payload: Any, state: _TurnState) -> list[dict]:
    """Convert one NDJSON object from grok into zero or more route events.
    
    grok streaming-messages-json format produces:
    - system (subtype: init) - session initialization
    - assistant (message with content blocks) - assistant messages with thinking/text
    - result (subtype: success) - final result
    - end - end of stream with stopReason
    - error - error event
    - available_commands - tool list (ignored)
    - usage - token usage (ignored)
    """
    if not isinstance(payload, dict):
        return []

    kind = payload.get("type")

    if kind == "system" and payload.get("subtype") == "init":
        # A search turn may keep web_search (or lose it to the user's own
        # disable_web_search); any other registered tool fails the turn, and
        # is named so the cause (a new built-in, an MCP server) is visible.
        tools = payload.get("tools")
        if not isinstance(tools, list) or not all(isinstance(tool, str) for tool in tools):
            state.failure = "grok did not report its native tool registry; this runtime cannot safely forward host calls."
            return []
        allowed = ({"web_search"} if state.search else set()) \
            | ({_USE_TOOL} if state.mode in _USE_TOOL_MODES else set())
        # A live turn's own server can finish connecting before the turn
        # starts, and init then lists its catalog; grok still offers the
        # model use_tool alone (1.0.41 tool_definitions.json).
        extras = sorted(tool for tool in set(tools) - allowed
                        if state.toolset is None or state.toolset.host_name(tool) is None)
        if extras:
            state.native_tools_seen = extras
            names = ", ".join(extras)
            state.failure = (
                f"grok did not disable its native CLI tools ({names}) even after a respawn that "
                "removed them by name; this runtime cannot safely forward host calls."
                if state.retried else
                f"grok did not disable its native CLI tools ({names}); this runtime cannot safely forward host calls.")
        else:
            state.native_tools_disabled = True
            if state.mode in _USE_TOOL_MODES and _USE_TOOL not in tools:
                state.use_tool_missing = True
        return []

    if kind == "stream_event":
        event = payload.get("event") or {}
        if not isinstance(event, dict):
            return []
        event_type = event.get("type")
        if event_type == "message_start":
            state.start_message(event.get("message"))
        elif event_type == "content_block_start":
            block = event.get("content_block") or {}
            if not isinstance(block, dict):
                return []
            block_type = block.get("type")
            if block_type in {"text", "thinking"}:
                translated = state.content_event(event.get("index", 0), block_type,
                                                 block.get(block_type), snapshot=True)
                if translated:
                    yield translated
            elif state.search and block_type == "server_tool_use" and block.get("name") == "web_search":
                state.pending_searches[event.get("index", 0)] = {"id": block.get("id"), "json": ""}
                translated = state.search_started(block)
                if translated:
                    yield translated
            elif state.search and block_type == "web_search_tool_result":
                # The result block arrives whole; no delta type carries hits.
                if isinstance(block.get("content"), (list, dict)):
                    yield from state.search_finished(block)
            elif block_type in {"tool_use", "server_tool_use"}:
                if not state.native_tools_disabled:
                    state.failure = "grok attempted a native CLI tool; workspace actions must use host tools."
                    return []
                state.pending_calls[event.get("index", 0)] = {
                    "id": block.get("id"), "name": block.get("name"),
                    "input": block.get("input") or {}, "json": ""}
        elif event_type == "content_block_delta":
            delta = event.get("delta") or {}
            if delta.get("type") in {"text_delta", "thinking_delta"}:
                content_type = delta["type"].removesuffix("_delta")
                translated = state.content_event(event.get("index", 0), content_type,
                                                 delta.get(content_type))
                if translated:
                    yield translated
            elif delta.get("type") == "input_json_delta" and event.get("index", 0) in state.pending_searches:
                search = state.pending_searches[event.get("index", 0)]
                search["json"] += delta.get("partial_json") or ""
                if len(search["json"].encode("utf-8")) > MAX_ENVELOPE_BYTES:
                    raise ToolCallError("grok web search query exceeds the size limit")
            elif delta.get("type") == "input_json_delta":
                call = state.pending_calls.get(event.get("index", 0))
                if call is None:
                    raise ToolCallError("grok streamed arguments without a tool call")
                call["json"] += delta.get("partial_json") or ""
                if len(call["json"].encode("utf-8")) > MAX_ENVELOPE_BYTES:
                    raise ToolCallError("grok tool call exceeds the size limit")
        elif event_type == "content_block_stop" and event.get("index", 0) in state.pending_searches:
            search = state.pending_searches.pop(event.get("index", 0))
            try:
                arguments = json.loads(search["json"]) if search["json"] else None
            except ValueError:
                arguments = None
            if isinstance(arguments, dict) and isinstance(search["id"], str):
                state.search_started({"id": search["id"], "input": arguments})
        elif event_type == "content_block_stop":
            call = state.pending_calls.pop(event.get("index", 0), None)
            if call is not None:
                raw = call.pop("json")
                if raw:
                    try:
                        call["input"] = json.loads(raw)
                    except ValueError as exc:
                        raise ToolCallError("grok tool call has invalid JSON arguments") from exc
                event = state.host_call(call)
                if event:
                    yield event
        elif event_type == "message_delta":
            reason = (event.get("delta") or {}).get("stop_reason")
            if state.current_message is not None and id(state.current_message) in state.handed_messages \
                    and not state.host_calls:
                # The end of a message whose calls a previous leg handed over.
                return []
            if reason in {"cancelled", "canceled", "interrupted", "error", "failed"} \
                    or (reason == "tool_use" and not (state.backend_calls or state.searches or state.host_calls)):
                state.failure = f"grok reported {reason} before a host tool handoff."
            elif reason == "tool_use":
                # Every call in the message has closed; the CLI's refusal and
                # the whole-message snapshot come after this line (1.0.41).
                state.end_of_calls()
        return []

    if kind == "user" and state.host_calls:
        # The CLI is answering the calls itself; no message_delta arrived.
        state.end_of_calls()
        return []

    if kind == "assistant":
        message = payload.get("message")
        if not isinstance(message, dict):
            return []
        content = message.get("content")
        if not isinstance(content, list):
            return []
        current = state.snapshot_message(message)
        for index, block in enumerate(content):
            if not isinstance(block, dict):
                continue
            block_type = block.get("type")
            if state.search and block_type == "server_tool_use" and block.get("name") == "web_search":
                translated = state.search_started(block)
                if translated:
                    yield translated
                continue
            if state.search and block_type == "web_search_tool_result":
                yield from state.search_finished(block)
                continue
            if block_type in {"tool_use", "server_tool_use"}:
                if not state.native_tools_disabled:
                    state.failure = "grok attempted a native CLI tool; workspace actions must use host tools."
                    return []
                event = state.host_call({"id": block.get("id"), "name": block.get("name"),
                                         "input": block.get("input")})
                if event:
                    yield event
                continue
            if block_type in {"text", "thinking"}:
                translated = state.content_event(index, block_type, block.get(block_type),
                                                 message=current, snapshot=True)
                if translated:
                    yield translated

    elif kind in {"end", "result"} and state.host_calls:
        state.end_of_calls()

    elif kind == "end":
        state.terminal = True
        stop_reason = payload.get("stopReason") or payload.get("stop_reason")
        if isinstance(stop_reason, str) and stop_reason:
            state.stop_reason = stop_reason
        if stop_reason not in {"end_turn", "stop", "completed", "success", "max_tokens", "stop_sequence"}:
            state.failure = f"grok reported an unsuccessful terminal status: {stop_reason}."

    elif kind == "result":
        subtype = str(payload.get("subtype") or "")
        if payload.get("is_error") or subtype in {"failed", "cancelled", "canceled", "interrupted"} \
                or subtype.startswith("error"):
            state.failure = f"grok CLI result failed ({subtype or 'error'})."
            return []
        if subtype == "success":
            state.terminal = True
            state.stop_reason = "end_turn"
        result = payload.get("result")
        if isinstance(result, str) and result:
            state.result_text = result

    elif kind == "error":
        message = payload.get("message")
        if isinstance(message, str):
            state.failure = f"grok CLI error: {message}"

    return []


#: _attempt_turn's signal that init lacked use_tool before any event reached
#: the client, so run_turn may fall back to the text envelope.
_USE_TOOL_MISSING = "use_tool_missing"


def _interrupt(session) -> None:
    """SIGTERM the child at a host handoff; anything it does next is discarded."""
    terminate = getattr(getattr(session, "process", None), "terminate", None)
    if callable(terminate):
        try:
            terminate()
        except Exception:  # noqa: BLE001 - teardown must never raise
            pass


def _unwrap_use_tool(call, host_tools, toolset=None) -> dict:
    """The host call inside a ``use_tool`` dispatch: ``{tool_name, tool_input}``.

    grok also documents file-based arguments for large MCP inputs; a host call
    has no file the host could read, so anything but inline ``tool_input`` is
    a protocol error for the route's one correction, never a guessed call.
    On a live session ``toolset`` maps the catalog key the model was shown
    (``host__<tool>``, perhaps an alias) back to the host tool.
    """
    arguments = call.get("input")
    if not isinstance(arguments, dict) or not isinstance(arguments.get("tool_name"), str):
        raise ToolCallError("use_tool call has no tool_name")
    if set(arguments) - {"tool_name", "tool_input"}:
        raise ToolCallError("use_tool arguments must be inline in tool_input")
    name = arguments["tool_name"]
    offered = {tool["name"] for tool in host_tools}
    if toolset is not None and toolset.host_name(name) is not None:
        name = toolset.host_name(name)
    elif name not in offered and name.startswith(_HOST_KEY_PREFIX) \
            and name[len(_HOST_KEY_PREFIX):] in offered:
        name = name[len(_HOST_KEY_PREFIX):]
    tool_input = arguments.get("tool_input", {})
    if isinstance(tool_input, str):
        try:
            tool_input = json.loads(tool_input)
        except ValueError as exc:
            raise ToolCallError("use_tool tool_input is not valid JSON") from exc
    if not isinstance(tool_input, dict):
        raise ToolCallError("use_tool tool_input must be an object")
    return {"id": call.get("id"), "name": name, "input": tool_input}


def _search_action(arguments: dict) -> dict:
    """The Responses action for a backend search's server_tool_use input.

    grok documents only ``input.query``; a page visit, if one ever arrives
    here, carries its URL instead.
    """
    query = arguments.get("query")
    if isinstance(query, str) and query.strip():
        return {"type": "search", "query": query}
    url = arguments.get("url")
    if isinstance(url, str) and url.startswith(("https://", "http://")):
        return {"type": "open_page", "url": url}
    return {"type": "other"}


def search_enabled(value) -> bool:
    """Whether the desktop's hosted search can run here without widening it.

    grok searches live and takes domain limits only from its config file, so
    a cached-only or domain-limited request runs without search rather than
    with a broader one.
    """
    return isinstance(value, dict) and value.get("live") is not False and not value.get("allowed_domains")


def _describe(exc: BaseException, state: _TurnState | None = None,
              timeout=None) -> str:
    text = str(exc).strip() or exc.__class__.__name__
    lowered = text.casefold()
    if isinstance(exc, subprocess.TimeoutExpired) or "timed out" in lowered \
            or "timeout" in lowered:
        limit = f" after {timeout}s" if timeout else ""
        text = f"the grok CLI did not finish{limit}"
    elif isinstance(exc, CliSessionError):
        text = f"grok CLI session error: {text}"
    else:
        text = f"grok CLI turn failed: {text}"
    extra = ""
    if state is not None:
        extra = state.diagnostics() + state.stderr_tail()
    return text + extra


def _plan_turn(request) -> dict:
    """Validate the request and render everything a spawn needs. No I/O.

    The prompt and the system override both travel on argv. Past the cap
    the prompt moves into the workspace prompt file instead (the screenshot
    transport), so a long host conversation never reaches the kernel's limit.
    """
    if not isinstance(request, dict):
        raise GrokCliAgentError("request must be an object")
    model = _validate_model(request.get("model"))
    host_tools = normalize_tools(request.get("tools"))
    messages = _coerce_messages(request.get("messages"))
    effort = _validate_effort(request.get("effort"))
    system = request.get("system")
    if system is not None and not isinstance(system, str):
        raise GrokCliAgentError("system must be a string or None")
    max_tokens = request.get("max_tokens")
    if max_tokens is not None and not isinstance(max_tokens, int):
        raise GrokCliAgentError("max_tokens must be an integer or None")
    if isinstance(max_tokens, int) and max_tokens <= 0:
        raise GrokCliAgentError("max_tokens must be positive")
    tool_choice = request.get("tool_choice")
    history = request.get("history")
    return {
        "model": model,
        "host_tools": host_tools,
        "tool_choice": tool_choice if isinstance(tool_choice, dict) else None,
        "effort": effort,
        "system": system.strip() if isinstance(system, str) else "",
        "messages": messages,
        "images": request.get("images") or [],
        "search": search_enabled(request.get("web_search")),
        "host_tool_schema": request.get("host_tool_schema"),
        # A live session's host tools under the catalog keys grok dispatches.
        "toolset": HostToolset(host_tools, prefix=_HOST_KEY_PREFIX, limit=_CATALOG_KEY_LIMIT,
                               pattern=_CATALOG_TOOL) if host_tools else None,
        # Typed Messages history, when the route passes it: live sessions match
        # a host request to the CLI waiting on its calls with it.
        "history": history if isinstance(history, list) else None,
        "timing": request.get("_cli_timing"),
    }


def _modes(plan) -> tuple:
    """The host tool surfaces to try, in order, for one stateless turn."""
    if plan["host_tools"] and not plan["host_tool_schema"]:
        return ("use_tool", "envelope")
    return (None,)


def _surface(plan, mode) -> dict:
    """``plan`` rendered for one surface: system text, prompt and its transport.

    ``use_tool`` lists the host tools for grok's own dispatcher and ``live``
    lists them under the catalog keys it resolves; ``envelope`` is the text
    fallback plus its anchor on the final user turn, which the route's parser
    still reads.
    """
    system, messages = plan["system"], plan["messages"]
    tools, choice = plan["host_tools"], plan["tool_choice"]
    if mode in _USE_TOOL_MODES:
        names = plan["toolset"].model_name if mode == "live" else None
        system = render_use_tool_manifest(tools, choice, names=names) + ("\n\n" + system if system else "")
    elif mode == "envelope":
        system = render_tool_manifest(tools, choice) + ("\n\n" + system if system else "")
        anchor = render_tool_anchor(tools)
        messages = [dict(message) for message in messages]
        for message in reversed(messages):
            if message["role"] == "user":
                message["content"] = (message["content"] + "\n\n" + anchor) if message["content"] else anchor
                break
    prompt = render_prompt(messages)
    argv_bytes = len(prompt.encode("utf-8")) + len(system.encode("utf-8"))
    return {**plan, "mode": mode, "system": system, "prompt": prompt,
            "prompt_file": bool(plan["images"]) or argv_bytes > _MAX_PROMPT_ARGV_BYTES}


def _turn_argv(plan, workspace_path, *, extra_removals=()) -> list[str]:
    """argv for one spawn of ``plan`` inside ``workspace_path`` (bare binary name).

    A prompt file is written only when the plan needs one: Grok parses .json
    prompt files as ACP blocks, which keeps screenshot bytes and oversized
    transcripts out of argv.
    """
    argv = build_argv(plan["model"], effort=plan["effort"], system=None, stream=True,
                      search=plan["search"], extra_removals=extra_removals,
                      host_tools=plan.get("mode") in _USE_TOOL_MODES,
                      run_host_tools=plan.get("mode") == "live")
    try:
        single = argv.index("--single")
    except ValueError:
        raise GrokCliAgentError("internal error: --single not found in argv")
    if plan["prompt_file"]:
        prompt_path = Path(workspace_path) / "prompt.json"
        with prompt_path.open("x", encoding="utf-8") as handle:
            prompt_path.chmod(0o600)
            json.dump(prompt_content(plan["prompt"], plan["images"], acp=True), handle,
                      ensure_ascii=False)
        argv[single:single + 1] = ["--prompt-file", str(prompt_path)]
    else:
        argv.insert(single + 1, plan["prompt"])
    if plan["host_tool_schema"]:
        tools_idx = argv.index("--tools")
        argv[tools_idx:tools_idx] = ["--json-schema", json.dumps(plan["host_tool_schema"]),
                                     "--max-turns", "1"]
    # System prompt as flag, before the variadic --tools that ends argv.
    if plan["system"]:
        tools_idx = argv.index("--tools")
        if plan["system"].startswith("-"):
            argv.insert(tools_idx, f"--system-prompt-override={plan['system']}")
        else:
            argv[tools_idx:tools_idx] = ["--system-prompt-override", plan["system"]]
    return argv


def _new_state(plan, mode, *, retried=False) -> _TurnState:
    state = _TurnState()
    state.host_tools = plan["host_tools"]
    state.search = plan["search"]
    state.mode = mode
    state.retried = retried
    state.toolset = plan["toolset"] if mode == "live" else None
    return state


def _attempt_turn(plan, state, *, spawner, timeout, extra_removals, retry_allowed):
    """One spawn of the CLI that ends with the turn or at its first host handoff.

    Returns the tool names init reported still registered when the registry
    check failed before any event reached the caller and a respawn is
    allowed; ``run_turn`` then spawns again with them removed. Otherwise the
    turn ends here, with exactly one message_stop or error event.
    """
    workspace_path = None
    try:
        binary = _resolve_binary()
        if not binary:
            raise GrokCliAgentError(
                "the grok CLI was not found on PATH; install Grok or "
                "switch this route to API-key credentials"
            )
        # stderr goes to a temp file, not a pipe
        state.stderr_handle = tempfile.TemporaryFile(mode="w+", encoding="utf-8")
        workspace_path = tempfile.mkdtemp(prefix="grok_ws_")
        argv = _turn_argv(plan, workspace_path, extra_removals=extra_removals)
        argv[0] = binary
        session = StdioSession(
            argv,
            env=minimal_env(_CHILD_ENV),
            cwd=workspace_path,
            timeout=float(timeout),
            spawner=spawner,
            stderr=state.stderr_handle,
        )
    except Exception as exc:
        message = _describe(exc, state, timeout)
        if state.stderr_handle is not None:
            state.stderr_handle.close()
        if workspace_path is not None:
            shutil.rmtree(workspace_path, ignore_errors=True)
        yield {"type": "error", "message": message}
        return None

    def release_files():
        if workspace_path is not None:
            shutil.rmtree(workspace_path, ignore_errors=True)
        if state.stderr_handle is not None:
            state.stderr_handle.close()

    try:
        with session:
            # For grok --single, prompt is already in argv, not on stdin
            return (yield from _stream(session, state, timeout=timeout, retry_allowed=retry_allowed))
    finally:
        cleanup_after_exit(session, release_files)


def _paced_events(session, *, timeout, idle):
    """``_iter_events``, plus ("idle", None) after each ``idle`` seconds with nothing to read."""
    deadline = time.monotonic() + timeout
    while True:
        started = time.monotonic()
        window = min(idle, deadline - started)
        if window <= 0:
            return
        quiet = True
        for item in _iter_events(session, timeout=window):
            quiet = False
            yield item
        if quiet:
            if time.monotonic() - started < window / 2:
                return  # the stream ended rather than went quiet
            yield ("idle", None)


def _stream(session, state, *, timeout, retry_allowed, on_handoff=None, ready=None):
    """Stream the CLI's output as route events until the turn, or this leg of it, ends.

    Returns, before any event was yielded: the names init reported still
    registered (a frozenset) when a respawn is allowed, or ``_USE_TOOL_MISSING``.
    Otherwise it ends with exactly one message_stop or error event and returns
    "pending" when ``on_handoff`` kept the CLI waiting inside its host calls,
    or None. ``ready`` (a live session's) says whether every host call read
    so far is waiting in the bridge; once it is and the stream has gone quiet,
    the calls are handed over without waiting for the message's end.
    """
    stop_reason = state.stop_reason or "end_turn"
    produced = False
    events = (_paced_events(session, timeout=timeout, idle=_LIVE_QUIET_SECONDS) if ready is not None
              else _iter_events(session, timeout=timeout))
    try:
        for kind, payload in events:
            if kind == "idle":
                if not (state.host_calls and not state.pending_calls and not state.host_handoff
                        and ready()):
                    continue
                state.host_handoff = True
            elif kind == "raw":
                state.raw_lines.append(payload)
                continue
            else:
                for event in _translate(payload, state):
                    produced = True
                    yield event
            if state.native_tools_seen and retry_allowed and not produced:
                # init is the stream's first line, so nothing has reached
                # the client: the respawn is invisible to it.
                return frozenset(state.native_tools_seen)
            if state.use_tool_missing and not produced:
                return _USE_TOOL_MISSING
            if state.failure:
                yield {"type": "error", "message": state.failure}
                return None
            if state.host_handoff:
                if on_handoff is not None and on_handoff():
                    # The CLI waits in the bridge for the host's results.
                    yield {"type": "message_stop", "stop_reason": "tool_use"}
                    return "pending"
                # With an empty native registry these are model requests
                # for host tools. Stop before the CLI's own tool loop.
                _interrupt(session)
                yield {"type": "message_stop", "stop_reason": "tool_use"}
                return None
            if state.terminal:
                returncode = getattr(session, "returncode", None)
                if isinstance(returncode, int) and returncode != 0:
                    yield {"type": "error", "message": f"the grok CLI exited with code {returncode}"}
                    return None
                if not state.emitted_text:
                    fallback = state.fallback_text()
                    if fallback:
                        state.emitted_text = True
                        yield {"type": "text_delta", "text": fallback}
                    else:
                        yield {"type": "error", "message": "the grok CLI produced no output"}
                        return None
                yield {"type": "message_stop", "stop_reason": state.stop_reason or "end_turn"}
                return None

        returncode = getattr(session, "returncode", None)
        if isinstance(returncode, int) and returncode != 0:
            detail = f"the grok CLI exited with code {returncode}"
            yield {"type": "error", "message": detail + state.diagnostics() + state.stderr_tail()}
            return None
        elif state.failure:
            yield {"type": "error", "message": state.failure}
            return None
        elif not state.terminal:
            yield {"type": "error", "message": "the grok CLI stream ended before completing the turn"}
            return None
        else:
            if not state.emitted_text:
                fallback = state.fallback_text()
                if fallback:
                    state.emitted_text = True
                    yield {"type": "text_delta", "text": fallback}
                elif not state.raw_lines:
                    yield {"type": "error", "message": "the grok CLI produced no output"}
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


def run_turn(request, *, spawner=None, timeout=300, pool=None) -> Iterator[dict]:
    """Stream one host request as text_delta / thinking_delta / message_stop.

    Always terminates with exactly one message_stop or error event, and never
    raises: every failure mode (missing binary, bad request, CLI error result,
    non-zero exit, timeout, unparsable stream) degrades to an error event.

    When the CLI's init line still names registered tools, the turn is
    spawned a second time with those names appended to the removal list
    before the client sees anything. A removal that worked is remembered in
    ``_LEARNED_REMOVALS`` so later turns spawn correctly first time.

    A turn with host tools offers them through ``use_tool``; if init does not
    list that dispatcher, the turn is spawned again with the text envelope,
    also before anything reaches the client. With typed history and a session
    pool (the module's own unless a test ``spawner`` is given) it runs as a
    live session, and a request that continues a waiting CLI resumes it;
    whenever no live session can serve the request, it runs statelessly.
    """
    try:
        plan = _plan_turn(request)
    except Exception as exc:
        yield {"type": "error", "message": _describe(exc, None, timeout)}
        return
    modes = _modes(plan)
    owner = pool if pool is not None else (_POOL if spawner is None else None)
    if owner is not None and modes[0] == "use_tool" and plan["history"] is not None:
        outcome = yield from _live_turn(plan, owner, spawner=spawner, timeout=timeout)
        if outcome == _USE_TOOL_MISSING:
            modes = ("envelope",)
        elif outcome != "stateless":
            return
    for mode in modes:
        surface = _surface(plan, mode)
        removals = frozenset(_LEARNED_REMOVALS)
        for attempt in (1, 2):
            state = _new_state(plan, mode, retried=attempt == 2)
            outcome = yield from _attempt_turn(surface, state, spawner=spawner, timeout=timeout,
                                               extra_removals=sorted(removals), retry_allowed=attempt == 1)
            if attempt == 2 and state.native_tools_disabled and len(_LEARNED_REMOVALS) < _MAX_LEARNED_REMOVALS:
                _LEARNED_REMOVALS.update(name for name in removals if _REMOVAL_NAME.fullmatch(name))
            if outcome == _USE_TOOL_MISSING:
                break
            if not outcome:
                return
            removals = removals | outcome
        else:
            return


# ---------------------------------------------------------------------------
# Live sessions
# ---------------------------------------------------------------------------

#: Each waiting CLI is a whole Grok process; past four, the oldest is retired
#: and its task's next step replays instead.
_POOL = SessionPool(live_session.dispose, capacity=4, idle_ttl=0, pending_ttl=LIVE_PENDING_TTL, label="Grok")
atexit.register(_POOL.close)


def _pool_key(plan) -> str:
    """What a waiting CLI must share with a request to continue it: argv and system text."""
    return digest({"model": plan["model"], "effort": plan["effort"], "search": plan["search"],
                   "system": _surface(plan, "live")["system"], "tools": plan["host_tools"],
                   "tool_choice": plan["tool_choice"]})


def _write_server_config(workspace_path, command) -> None:
    """Register the bridged host server for this workspace alone (grok reads cwd/.grok)."""
    directory = Path(workspace_path) / ".grok"
    directory.mkdir(mode=0o700)
    # JSON strings and arrays are valid TOML basic strings and arrays.
    body = (f"[mcp_servers.{SERVER_NAME}]\n"
            f"command = {json.dumps(command[0])}\n"
            f"args = {json.dumps(command[1:])}\n"
            "startup_timeout_sec = 20\n"
            f"tool_timeout_sec = {_LIVE_CALL_TIMEOUT_SEC}\n")
    descriptor = os.open(directory / "config.toml", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(body)


def _spawn_live(surface, state, *, spawner, timeout, extra_removals) -> live_session.LiveSession:
    """Start the CLI in a workspace whose host server is bridged to the hub."""
    binary = _resolve_binary()
    if not binary:
        raise GrokCliAgentError(
            "the grok CLI was not found on PATH; install Grok or "
            "switch this route to API-key credentials"
        )
    workspace_path = tempfile.mkdtemp(prefix="grok_ws_")
    bridge = None
    try:
        toolset = surface["toolset"]
        bridge = HostCallBridge(workspace_path, lambda served: toolset.host_name(_HOST_KEY_PREFIX + served))
        _write_server_config(workspace_path, server_command(toolset.write(workspace_path, bridge=bridge)))
        argv = _turn_argv(surface, workspace_path, extra_removals=extra_removals)
        argv[0] = binary
        state.stderr_handle = tempfile.TemporaryFile(mode="w+", encoding="utf-8")
        session = StdioSession(argv, env=minimal_env({**_CHILD_ENV, **_LIVE_ENV_EXTRA}), cwd=workspace_path,
                               timeout=float(timeout), spawner=spawner, stderr=state.stderr_handle)
    except BaseException:
        if bridge is not None:
            bridge.close()
        if state.stderr_handle is not None:
            state.stderr_handle.close()
            state.stderr_handle = None
        shutil.rmtree(workspace_path, ignore_errors=True)
        raise
    return live_session.LiveSession(session, workspace_path, bridge, state, interrupt=_interrupt)


def _live_turn(plan, owner, *, spawner, timeout):
    """One host request on a live session, resumed from its waiting calls or started cold.

    Returns "stateless" when no live session can serve the request and
    ``_USE_TOOL_MISSING`` when init lacked the dispatcher - in both cases
    before any event was yielded. Otherwise it ends with exactly one
    message_stop or error event and returns None or "pending".
    """
    deadline = time.monotonic() + timeout
    timing = plan["timing"]
    history = plan["history"]
    key = _pool_key(plan)
    try:
        # Grok's MCP client is not shown to pass images to the model, so a
        # screenshot result replays through the verified image transport.
        lease, reuse = owner.acquire(
            key, match=lambda entry: live_session.continuation(entry, history, images=False) is not None,
            timeout=max(0.01, deadline - time.monotonic()))
    except (RuntimeError, TimeoutError):
        return "stateless"
    except Exception as exc:  # the client cancelled while the lease was awaited
        yield {"type": "error", "message": _describe(exc, None, timeout)}
        return None

    def on_handoff(live):
        return lambda: live_session.hold(live, lease, history, deadline, events=_iter_events,
                                         translate=_translate, seconds=_LIVE_CONFIRM_SECONDS)

    def ready(live):
        return lambda: live.bridge.check(live.state.host_calls) == "ready"

    try:
        if reuse == "resumed":
            results = live_session.continuation(lease, history, images=False)
            lease.pending = None
            live = lease.session
            if results is None or not live.resume(results):
                return "stateless"
            if timing:
                timing.label(reuse="resumed")
            return (yield from _stream(live.session, live.state, timeout=max(0.01, deadline - time.monotonic()),
                                       retry_allowed=False, on_handoff=on_handoff(live), ready=ready(live)))
        if timing:
            timing.label(reuse="cold")
        surface = _surface(plan, "live")
        removals = frozenset(_LEARNED_REMOVALS)
        for attempt in (1, 2):
            state = _new_state(plan, "live", retried=attempt == 2)
            try:
                live = _spawn_live(surface, state, spawner=spawner, timeout=max(0.01, deadline - time.monotonic()),
                                   extra_removals=sorted(removals))
            except HostBridgeError:
                return "stateless"
            except Exception as exc:
                yield {"type": "error", "message": _describe(exc, state, timeout)}
                return None
            lease.session = live
            outcome = yield from _stream(live.session, state, timeout=max(0.01, deadline - time.monotonic()),
                                         retry_allowed=attempt == 1, on_handoff=on_handoff(live),
                                         ready=ready(live))
            if attempt == 2 and state.native_tools_disabled and len(_LEARNED_REMOVALS) < _MAX_LEARNED_REMOVALS:
                _LEARNED_REMOVALS.update(name for name in removals if _REMOVAL_NAME.fullmatch(name))
            if not isinstance(outcome, frozenset):
                return outcome
            # init named tools the removal list missed: this process goes, and
            # the respawn, still before any event, removes them by name.
            live.close()
            lease.session = None
            removals = removals | outcome
        return None
    finally:
        # Only a handoff the client has received keeps the CLI waiting. Any
        # other ending closes it now, as a stateless turn would, rather than
        # leaving it to the pool's reaper.
        keep = bool(lease.pending) and (timing is None or timing.delivered)
        if not keep and lease.session is not None:
            lease.session.close()
        owner.release(lease, healthy=keep)
