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

import json
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator

from cli_session import CliSessionError, StdioSession, minimal_env, resolve_binary
from cli_tool_call import TRANSCRIPT_HEADER
from cli_images import prompt_content

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
VERIFIED_IMAGE_MODELS = frozenset({"sonnet"})

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

# UNVERIFIED SEED, NOT DISCOVERY. Claude Code exposes no non-interactive model
# list, so catalogue() returns nothing and this tuple is the only seed the hub
# has. Entries come from `claude --help` (which names the aliases 'fable',
# 'opus', 'sonnet' and the full name 'claude-fable-5') and from one live run
# whose system/init event reported model "claude-sonnet-5". Nothing here was
# enumerated by the CLI, so treat every id as a hint a user may type, not as a
# promise that the account can serve it. An unusable id fails at turn time with
# the CLI's own error, which is surfaced as an error event.
KNOWN_MODELS = (
    {"id": "sonnet", "display_name": "Claude Sonnet (alias)",
     "reasoning_levels": list(CLAUDE_EFFORTS)},
    {"id": "opus", "display_name": "Claude Opus (alias)",
     "reasoning_levels": list(CLAUDE_EFFORTS)},
    {"id": "fable", "display_name": "Claude Fable (alias)",
     "reasoning_levels": list(CLAUDE_EFFORTS)},
    {"id": "claude-sonnet-5", "display_name": "Claude Sonnet 5",
     "reasoning_levels": list(CLAUDE_EFFORTS)},
    {"id": "claude-fable-5", "display_name": "Claude Fable 5",
     "reasoning_levels": list(CLAUDE_EFFORTS)},
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

def _default_capture(argv, *, timeout=30, stdin=None):
    """One-shot bounded subprocess using a minimal environment."""
    try:
        completed = subprocess.run(
            [str(item) for item in argv],
            capture_output=True,
            text=True,
            timeout=float(timeout),
            input=stdin,
            env=minimal_env(),
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


def _invoke(capture, argv, *, timeout=30, stdin=None) -> tuple[int | None, str, str]:
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
    except (TypeError, ValueError):
        kwargs = {"timeout": timeout}
        if stdin is not None:
            kwargs["stdin"] = stdin
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


def auth_state(*, capture=None) -> dict:
    """Ask the CLI, and only the CLI, whether it is signed in.

    Never reads ~/.claude, auth.json, or the Keychain. An unparseable answer or
    a failing subcommand is reported as "unknown" rather than guessed at: this
    route is default-off, and a wrong "missing" would look like a broken login.
    """
    binary = _resolve_binary()
    if not binary:
        return {"state": "missing",
                "detail": "the claude CLI is not installed or not on PATH"}
    try:
        rc, out, err = _invoke(capture, [binary, "auth", "status"], timeout=30)
    except Exception as exc:
        return {"state": "unknown", "detail": f"auth probe failed: {exc}"}
    if rc not in (0, None):
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
        if plan:
            detail += f" ({plan} subscription)"
        return {"state": "authenticated", "detail": detail}
    if logged_in is False:
        return {"state": "missing",
                "detail": "claude reports no active login; sign in with the "
                          "claude CLI itself"}
    return {"state": "unknown",
            "detail": "claude auth status did not report a loggedIn field"}


def catalogue(*, capture=None, timeout=30) -> tuple[list[dict], list[str]]:
    """No discovery is possible: Claude Code has no read-only model list.

    Returns an empty catalogue plus the reason, and never spawns anything. The
    hub should seed route names from KNOWN_MODELS, which is explicitly
    unverified rather than discovered.
    """
    return [], [_NO_MODEL_LIST_WARNING]


# ---------------------------------------------------------------------------
# argv construction
# ---------------------------------------------------------------------------

def build_argv(model, *, effort=None, system=None, stream=True) -> list[str]:
    """Full argv for one print-mode turn. argv[0] is the bare binary name.

    ``run_turn`` replaces argv[0] with the resolved absolute path; keeping the
    name here makes this function pure and testable on a machine with no CLI
    installed. The prompt is NOT part of argv — it always goes on stdin.
    """
    validated_model = _validate_model(model)
    validated_effort = _validate_effort(effort)

    argv: list[str] = [BINARY_NAMES[0], "-p",
                       "--output-format", "stream-json" if stream else "text"]
    if stream:
        # stream-json under -p requires --verbose; --include-partial-messages is
        # what turns whole-message events into token-level deltas.
        argv += ["--include-partial-messages", "--verbose"]
    argv += list(READ_ONLY_FLAGS)
    argv += ["--model", validated_model]
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
    # Variadic, so it must be the last thing on the command line.
    argv += ["--tools", ""]

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
_TRANSCRIPT_FOOTER = 'Respond now to the final <turn role="user"> above.'


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
        normalized.append({"role": role, "content": content})
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


def _text_from_content(content: Any) -> str:
    """Extract text from an Anthropic message content field."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "".join(parts)
    return ""


def _translate(payload: Any, state: _TurnState) -> list[dict]:
    """Convert one NDJSON object from claude into zero or more route events."""
    if not isinstance(payload, dict):
        return []
    kind = payload.get("type")

    if kind == "stream_event":
        event = payload.get("event")
        if not isinstance(event, dict):
            return []
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
            return []
        delta = event.get("delta")
        if not isinstance(delta, dict):
            return []
        delta_type = delta.get("type")
        if delta_type == "text_delta":
            text = delta.get("text")
            if isinstance(text, str) and text:
                state.emitted_text = True
                return [{"type": "text_delta", "text": text}]
            return []
        if delta_type == "thinking_delta":
            # Anthropic streams reasoning under delta.thinking; accept .text too
            # so a future field rename degrades instead of dropping reasoning.
            text = delta.get("thinking")
            if not isinstance(text, str):
                text = delta.get("text")
            if isinstance(text, str) and text:
                return [{"type": "thinking_delta", "text": text}]
            return []
        # input_json_delta and friends are tool traffic; tools are stripped, so
        # anything arriving here is ignored rather than surfaced as text.
        return []

    if kind == "assistant":
        message = payload.get("message")
        if isinstance(message, dict):
            text = _text_from_content(message.get("content"))
            if text:
                state.assistant_text.append(text)
        return []

    if kind == "result":
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
        return []

    if kind == "error":
        message = payload.get("message") or payload.get("error") or payload
        state.failure = f"claude reported an error: {message}"
        return []

    # system/init, system/status, rate_limit_event, replayed user messages:
    # informational, never route output.
    return []


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


def run_turn(request, *, spawner=None, timeout=300) -> Iterator[dict]:
    """Stream one stateless turn as text_delta / thinking_delta / message_stop.

    Always terminates with exactly one message_stop or error event, and never
    raises: every failure mode (missing binary, bad request, CLI error result,
    non-zero exit, timeout, unparsable stream) degrades to an error event.
    """
    state = _TurnState()
    try:
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

        # The system prompt travels as a flag, so it is not also rendered into
        # the prompt; duplicating it would double-charge and could conflict.
        prompt = render_prompt(messages)
        argv = build_argv(model, effort=effort, system=system, stream=True)
        if request.get("images"):
            argv[1:1] = ["--input-format", "stream-json"]
            prompt = json.dumps({"type": "user", "message": {"role": "user",
                "content": prompt_content(prompt, request["images"])}}, ensure_ascii=False)

        binary = _resolve_binary()
        if not binary:
            raise ClaudeCliAgentError(
                "the claude CLI was not found on PATH; install Claude Code or "
                "switch this route to API-key credentials"
            )
        argv[0] = binary

        # stderr goes to a temp file, not a pipe: a pipe nobody drains can fill
        # and deadlock the child, and the text is worth keeping for diagnosis.
        state.stderr_handle = tempfile.TemporaryFile(mode="w+", encoding="utf-8")
        session = StdioSession(
            argv,
            env=minimal_env(),
            timeout=float(timeout),
            spawner=spawner,
            stderr=state.stderr_handle,
        )
    except Exception as exc:
        yield {"type": "error", "message": _describe(exc, state, timeout)}
        return

    stop_reason = state.stop_reason or "end_turn"
    try:
        with session:
            _write_prompt(session, prompt)
            for kind, payload in _iter_events(session, timeout=timeout):
                if kind == "raw":
                    state.raw_lines.append(payload)
                    continue
                for event in _translate(payload, state):
                    yield event
                if state.failure:
                    yield {"type": "error", "message": state.failure}
                    return

        returncode = getattr(session, "returncode", None)
        if isinstance(returncode, int) and returncode != 0:
            yielded = state.emitted_text or bool(state.fallback_text())
            detail = f"the claude CLI exited with code {returncode}"
            if not yielded:
                yield {"type": "error",
                       "message": detail + state.diagnostics()
                                  + state.stderr_tail()}
                return
            # Text did arrive before the failure; report it as a stopped turn
            # rather than discarding a usable completion.
            stop_reason = state.stop_reason or "error"
        elif state.failure:
            yield {"type": "error", "message": state.failure}
            return
        else:
            if not state.emitted_text:
                fallback = state.fallback_text()
                if fallback:
                    state.emitted_text = True
                    yield {"type": "text_delta", "text": fallback}
                elif not state.raw_lines:
                    yield {"type": "error", "message": "the claude CLI produced no output"}
                    return
            stop_reason = state.stop_reason or "end_turn"
    except Exception as exc:
        yield {"type": "error", "message": _describe(exc, state, timeout)}
        return
    finally:
        handle = state.stderr_handle
        if handle is not None:
            try:
                handle.close()
            except Exception:
                pass

    yield {"type": "message_stop", "stop_reason": stop_reason}
