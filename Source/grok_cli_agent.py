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

Verified live against grok 1.0.34 (3736acbc8658):
  - Binary at ~/.grok/bin/grok (self-updates on launch)
  - ``--help`` confirms: --no-auto-update, --single/-p, --output-format streaming-messages-json,
    --permission-mode plan, --no-subagents, --tools, --model, --reasoning-effort
    (aliases: --effort), --system-prompt-override (compat: --system-prompt)
  - ``grok models`` returns authenticated models list (2 models: grok-4.6, grok-4.5)
  - ``--reasoning-effort`` accepts: low, medium, high, xhigh (no none, no ultra)
  - ``--tools ""`` alone leaves native tools enabled; the removal list and
    init.tools validation below are required
  - ``--output-format streaming-messages-json`` outputs NDJSON in Anthropic Messages API format
  - Prompt travels as positional argument to ``--single``, NOT on stdin
    (verified: ``--single`` with no positional aborts with "a value is required
    for '--single <PROMPT\u003e'"; stdin is ignored). Long prompts are therefore
    bounded by argv size; prompts above ~256 KB are refused before spawn.
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

import json
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Iterator

from cli_session import CliSessionError, StdioSession, minimal_env, resolve_binary
from cli_tool_call import (MAX_ENVELOPE_BYTES, TRANSCRIPT_HEADER, ToolCallError,
                           normalize_tools, validate_host_call)
from cli_images import prompt_content

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

# Real installs live in ~/.grok/bin on this machine.
_EXTRA_BIN_DIRS = ("~/.grok/bin", "~/.local/bin", "/opt/homebrew/bin", "/usr/local/bin")

# ---------------------------------------------------------------------------
# Safety invariants
# ---------------------------------------------------------------------------

#: Maximum prompt byte length delivered as a positional argument to --single.
#: macOS ARG_MAX is ~1 MB; we keep a large margin so the CLI never receives
#: an argv the kernel would refuse before it can be executed.
_MAX_PROMPT_ARGV_BYTES = 256 * 1024

# Never constructed, never forwarded, asserted against in build_argv.
_FORBIDDEN_FLAGS = frozenset({
    "--always-approve",
})

# Internal IDs differ from the names shown in init.tools. Empty --tools alone
# leaves the stock toolset enabled in Grok 1.0.34. Remove built-ins explicitly,
# including MCP dispatch and task tools, then verify init.tools is empty.
_DISALLOWED_TOOLS = (
    "run_terminal_cmd", "run_terminal_command", "read_file", "search_replace",
    "list_dir", "grep", "kill_task", "get_task_output", "task", "Agent",
    "kill_command_or_subagent", "get_command_or_subagent_output", "spawn_subagent",
    "todo_write", "scheduler_create", "scheduler_delete", "scheduler_list",
    "monitor", "search_tool", "use_tool", "workflow", "enter_plan_mode",
    "exit_plan_mode", "ask_user_question", "send_feedback", "image_gen",
    "image_edit", "image_to_video", "reference_to_video", "write",
    "web_search", "web_fetch", "memory_search", "memory_get", "lsp",
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

# Known models from live verification: grok-4.6 (default), grok-4.5.
# These are UNVERIFIED SEED for the hub's model picker; catalogue() returns
# nothing because grok has no read-only model list subcommand.
KNOWN_MODELS = ("grok-4.6", "grok-4.5")

_NO_MODEL_LIST_WARNING = (
    "grok has no non-interactive model list; catalogue is seeded from "
    "KNOWN_MODELS and live verification"
)

# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def _resolve_binary() -> str | None:
    """Resolve the grok binary from PATH and known extra directories."""
    return resolve_binary(BINARY_NAMES, extra_dirs=_EXTRA_BIN_DIRS)


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


# ---------------------------------------------------------------------------
# argv construction
# ---------------------------------------------------------------------------


def _validate_model(model: str | None) -> str:
    """Validate and return the model identifier."""
    if model is None:
        return "grok-4.6"
    if not isinstance(model, str):
        raise GrokCliAgentError(f"model must be a string, got {type(model).__name__}")
    stripped = model.strip()
    if not stripped:
        return "grok-4.6"
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


def build_argv(model, *, effort=None, system=None, stream=True) -> list[str]:
    """Full argv for one print-mode turn. argv[0] is the bare binary name.

    ``run_turn`` replaces argv[0] with the resolved absolute path; keeping the
    name here makes this function pure and testable on a machine with no CLI
    installed.

    INVARIANT: Every argv carries ``--no-auto-update`` to prevent the CLI from
    self-updating during capability checks or turns.

    Note: The prompt is NOT part of argv — for grok, it is passed as the
    positional argument to ``--single`` by ``run_turn``, not included here.
    The system prompt travels as ``--system-prompt-override``.
    """
    validated_model = _validate_model(model)
    validated_effort = _validate_effort(effort)

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
    argv += list(READ_ONLY_FLAGS)

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
    argv += ["--tools", ""]

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
        self.streamed_text = False
        self.streamed_thinking = False
        self.native_tools_disabled = False
        self.host_tools = []
        self.host_handoff = False
        self.pending_calls = {}
        self.backend_calls = set()

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
        validate_host_call(call, self.host_tools)
        self.host_handoff = True
        return {"type": "tool_call", **call}

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
        if payload.get("tools") != []:
            state.failure = "grok did not disable its native CLI tools; this runtime cannot safely forward host calls."
        else:
            state.native_tools_disabled = True
        return []

    if kind == "stream_event":
        event = payload.get("event") or {}
        if not isinstance(event, dict):
            return []
        event_type = event.get("type")
        if event_type == "message_start":
            state.streamed_text = state.streamed_thinking = False
        elif event_type == "content_block_start":
            block = event.get("content_block") or {}
            if block.get("type") in {"tool_use", "server_tool_use"}:
                if not state.native_tools_disabled:
                    state.failure = "grok attempted a native CLI tool; workspace actions must use host tools."
                    return []
                state.pending_calls[event.get("index", 0)] = {
                    "id": block.get("id"), "name": block.get("name"),
                    "input": block.get("input") or {}, "json": ""}
        elif event_type == "content_block_delta":
            delta = event.get("delta") or {}
            if delta.get("type") == "text_delta" and delta.get("text"):
                state.emitted_text = state.streamed_text = True
                yield {"type": "text_delta", "text": delta["text"]}
            elif delta.get("type") == "thinking_delta" and delta.get("thinking"):
                state.streamed_thinking = True
                yield {"type": "thinking_delta", "text": delta["thinking"]}
            elif delta.get("type") == "input_json_delta":
                call = state.pending_calls.get(event.get("index", 0))
                if call is None:
                    raise ToolCallError("grok streamed arguments without a tool call")
                call["json"] += delta.get("partial_json") or ""
                if len(call["json"].encode("utf-8")) > MAX_ENVELOPE_BYTES:
                    raise ToolCallError("grok tool call exceeds the size limit")
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
            if reason in {"cancelled", "canceled", "interrupted", "error", "failed"} \
                    or (reason == "tool_use" and not state.backend_calls):
                state.failure = f"grok reported {reason} before a host tool handoff."
        return []

    if kind == "assistant":
        message = payload.get("message")
        if not isinstance(message, dict):
            return []
        content = message.get("content")
        if not isinstance(content, list):
            return []
        for block in content:
            if not isinstance(block, dict):
                continue
            block_type = block.get("type")
            if block_type in {"tool_use", "server_tool_use"}:
                if not state.native_tools_disabled:
                    state.failure = "grok attempted a native CLI tool; workspace actions must use host tools."
                    return []
                event = state.host_call({"id": block.get("id"), "name": block.get("name"),
                                         "input": block.get("input")})
                if event:
                    yield event
                continue
            if block_type == "thinking":
                thinking = block.get("thinking")
                if isinstance(thinking, str) and thinking and not state.streamed_thinking:
                    yield {"type": "thinking_delta", "text": thinking}
            elif block_type == "text":
                text = block.get("text")
                if isinstance(text, str) and text and not state.streamed_text:
                    state.assistant_text.append(text)
                    state.emitted_text = True
                    yield {"type": "text_delta", "text": text}
        state.streamed_text = state.streamed_thinking = False

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


def run_turn(request, *, spawner=None, timeout=300) -> Iterator[dict]:
    """Stream one stateless turn as text_delta / thinking_delta / message_stop.

    Always terminates with exactly one message_stop or error event, and never
    raises: every failure mode (missing binary, bad request, CLI error result,
    non-zero exit, timeout, unparsable stream) degrades to an error event.
    """
    state = _TurnState()
    workspace_path = None
    try:
        if not isinstance(request, dict):
            raise GrokCliAgentError("request must be an object")
        model = _validate_model(request.get("model"))
        state.host_tools = normalize_tools(request.get("tools"))
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

        # Render the prompt
        prompt = render_prompt(messages)
        prompt_bytes = prompt.encode("utf-8")
        if not request.get("images") and len(prompt_bytes) > _MAX_PROMPT_ARGV_BYTES:
            raise GrokCliAgentError(
                f"prompt is too large for the grok CLI positional argument "
                f"({len(prompt_bytes)} bytes; maximum {_MAX_PROMPT_ARGV_BYTES} bytes)"
            )

        # Build argv - prompt goes as positional arg to --single
        argv = build_argv(model, effort=effort, system=None, stream=True)

        # Insert the prompt as the value for --single
        # argv: [grok, --no-auto-update, --single, --output-format, ...]
        # need: [grok, --no-auto-update, --single, PROMPT, --output-format, ...]
        try:
            single_idx = argv.index("--single")
        except ValueError:
            raise GrokCliAgentError("internal error: --single not found in argv")
        argv.insert(single_idx + 1, prompt)

        # Handle system prompt separately (we passed None to build_argv to avoid
        # duplication, but need to add it back if provided)
        if system is not None:
            system_text = system.strip()
            if system_text:
                # Find position before --tools (which is last)
                try:
                    tools_idx = argv.index("--tools")
                except ValueError:
                    tools_idx = len(argv)
                if system_text.startswith("-"):
                    argv.insert(tools_idx, f"--system-prompt-override={system_text}")
                else:
                    argv.insert(tools_idx, "--system-prompt-override")
                    argv.insert(tools_idx + 1, system_text)

        binary = _resolve_binary()
        if not binary:
            raise GrokCliAgentError(
                "the grok CLI was not found on PATH; install Grok or "
                "switch this route to API-key credentials"
            )
        argv[0] = binary

        # stderr goes to a temp file, not a pipe
        state.stderr_handle = tempfile.TemporaryFile(mode="w+", encoding="utf-8")
        workspace_path = tempfile.mkdtemp(prefix="grok_ws_")
        if request.get("images"):
            # Grok parses .json prompt files as ACP blocks. Keep screenshot
            # bytes out of argv and avoid the OS command-line size limit.
            prompt_path = Path(workspace_path) / "prompt.json"
            with prompt_path.open("x", encoding="utf-8") as handle:
                prompt_path.chmod(0o600)
                json.dump(prompt_content(prompt, request["images"], acp=True), handle, ensure_ascii=False)
            index = argv.index("--single")
            argv[index:index + 2] = ["--prompt-file", str(prompt_path)]
        session = StdioSession(
            argv,
            env=minimal_env(),
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
        return

    stop_reason = state.stop_reason or "end_turn"
    try:
        with session:
            # For grok --single, prompt is already in argv, not on stdin
            for kind, payload in _iter_events(session, timeout=timeout):
                if kind == "raw":
                    state.raw_lines.append(payload)
                    continue
                for event in _translate(payload, state):
                    yield event
                if state.failure:
                    yield {"type": "error", "message": state.failure}
                    return
                if state.host_handoff:
                    # With an empty native registry these are model requests
                    # for host tools. Stop before the CLI's own tool loop.
                    yield {"type": "message_stop", "stop_reason": "tool_use"}
                    return

        returncode = getattr(session, "returncode", None)
        if isinstance(returncode, int) and returncode != 0:
            detail = f"the grok CLI exited with code {returncode}"
            yield {"type": "error", "message": detail + state.diagnostics() + state.stderr_tail()}
            return
        elif state.failure:
            yield {"type": "error", "message": state.failure}
            return
        elif not state.terminal:
            yield {"type": "error", "message": "the grok CLI stream ended before completing the turn"}
            return
        else:
            if not state.emitted_text:
                fallback = state.fallback_text()
                if fallback:
                    state.emitted_text = True
                    yield {"type": "text_delta", "text": fallback}
                elif not state.raw_lines:
                    yield {"type": "error", "message": "the grok CLI produced no output"}
                    return
            stop_reason = state.stop_reason or "end_turn"
    except Exception as exc:
        yield {"type": "error", "message": _describe(exc, state, timeout)}
        return
    finally:
        if workspace_path is not None:
            shutil.rmtree(workspace_path, ignore_errors=True)
        handle = state.stderr_handle
        if handle is not None:
            try:
                handle.close()
            except Exception:
                pass

    yield {"type": "message_stop", "stop_reason": stop_reason}
