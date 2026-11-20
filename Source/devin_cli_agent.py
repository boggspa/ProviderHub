"""Devin CLI as a hub MODEL ROUTE — Route 2, CLI-as-transport.

``devin acp`` is the Devin CLI's Agent Client Protocol server: JSON-RPC 2.0
over stdio, the same wire shape the installed ``claude``/``codex``/``muse``/
``grok``/``agy`` adapters already drive through
:class:`cli_session.StdioSession`. Like every Route-2 provider the CLI owns
and refreshes its own login (``devin auth login`` writes the credentials the
child reads); :func:`cli_session.minimal_env` hands the child an
deny-by-default environment, so no ``*_API_KEY`` variable — including
``WINDSURF_API_KEY``/``DEVIN_API_KEY`` — reaches it by accident. An env-key
lane is a deliberate follow-up, not an accident of ``os.environ``.

What makes this adapter different from its siblings is the peer: ``devin acp``
is a *stateful agent*, not a print-mode model endpoint. It owns a tool loop,
streams ``session/update`` notifications, and sends the client
``session/request_permission`` requests mid-turn. This v1 route is deliberately
the minimal honest slice:

* one ``devin acp --model <id>`` child per turn, in a fresh empty workspace
  (the agent's own tools have nothing to touch and every permission request
  is answered with a reject/cancel outcome — never an allow);
* the rendered transcript is the whole prompt, exactly like the other text
  adapters, and only ``agent_message_chunk``/``agent_thought_chunk`` updates
  become ``text_delta``/``thinking_delta`` events;
* no host-tool manifest is rendered (``cli_routes.plan_turn`` drops client
  tools for this provider), because Devin's agent would answer with its own
  native tool calls, not the host's call protocol — advertising tools the
  route cannot honour would produce silent non-compliance, not capability.

``session/new`` advertises ``mcpServers: []`` and fs capabilities are never
claimed, so nothing outside the transcript channel is exposed. ACP session
resumption, ``--agent-type`` profiles (summarizer/review carry the wrong
persona for a chat route), ``--cloud`` relay, and the env-key credential lane
are all documented follow-ups, not v1 scope.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Iterator

from cli_lifecycle import cleanup_after_exit
from cli_session import CliSessionError, StdioSession, minimal_env, resolve_binary
from cli_tool_call import TOOL_RESULTS_KEY, transcript_footer
from devin_models import catalogue_rows

PROVIDER_ID = "devin-cli"
PROVIDER_NAME = "Devin CLI"
TRANSPORT = "acp_stdio"
SYSTEM_PROMPT_TRANSPORT = "prompt"
BINARY_NAMES = ("devin",)
_EXTRA_BIN_DIRS = ("~/.local/bin",)

#: ``devin acp`` defaults to the CLI's configured model; the catalogue always
#: sends an explicit id, so this only matters for a hand-built request.
DEFAULT_MODEL = "swe-1.6-slow"

_CLIENT_NAME = "provider-hub"
_CLIENT_VERSION = "provider-hub-cli"
_ACP_PROTOCOL_VERSION = 1

_INITIALIZE_PARAMS = {
    "protocolVersion": _ACP_PROTOCOL_VERSION,
    # We service no fs/* or terminal/* methods — advertising them would invite
    # requests we can only answer -32601.
    "clientCapabilities": {"fs": {"readTextFile": False, "writeTextFile": False}},
    "clientInfo": {"name": _CLIENT_NAME, "version": _CLIENT_VERSION},
}

_PERMISSION_METHOD = "session/request_permission"
_METHOD_NOT_FOUND = -32601

_ROLES = ("user", "assistant")

#: Seed catalogue: one row per Devin model *family* — the fuzzy names
#: ``devin acp --model`` accepts alongside variant uids. Transcribed from the
#: Devin CLI's own listing (the same source TaskWraith's devinModelCatalog
#: records); ``devin models list --format json`` replaces these with the
#: account's live inventory whenever the CLI is signed in. The GLM-5.2 1M
#: family shares the ``glm-5.2`` slug and is not separately addressable by
#: slug, so it is folded into the one row.
KNOWN_MODELS = (
    {"id": "swe-2", "display_name": "SWE-2"},
    {"id": "swe-1.7", "display_name": "SWE-1.7"},
    {"id": "swe-1.7-lightning", "display_name": "SWE-1.7 Lightning"},
    {"id": "swe-1.6-slow", "display_name": "SWE-1.6 Slow"},
    {"id": "swe-1.6-fast", "display_name": "SWE-1.6 Fast"},
    {"id": "swe-1.6", "display_name": "SWE-1.6"},
    {"id": "adaptive", "display_name": "Adaptive"},
    {"id": "claude-fable-5.2", "display_name": "Claude Fable 5.2"},
    {"id": "claude-fable-5.1", "display_name": "Claude Fable 5.1"},
    {"id": "claude-opus-5.5", "display_name": "Claude Opus 5.5"},
    {"id": "claude-opus-5", "display_name": "Claude Opus 5"},
    {"id": "claude-sonnet-5.5", "display_name": "Claude Sonnet 5.5"},
    {"id": "claude-sonnet-5", "display_name": "Claude Sonnet 5"},
    {"id": "gpt-5.6-sol", "display_name": "GPT-5.6 Sol"},
    {"id": "gpt-5.6-terra", "display_name": "GPT-5.6 Terra"},
    {"id": "gpt-5.6-luna", "display_name": "GPT-5.6 Luna"},
    {"id": "gpt-5.3-codex", "display_name": "GPT-5.3-Codex"},
    {"id": "gemini-3.7-flash", "display_name": "Gemini 3.7 Flash"},
    {"id": "grok-4.6", "display_name": "Grok 4.6"},
    {"id": "grok-4.5", "display_name": "Grok 4.5"},
    {"id": "kimi-k3", "display_name": "Kimi K3"},
    {"id": "glm-5.3", "display_name": "GLM-5.3"},
    {"id": "glm-5.3-flash", "display_name": "GLM-5.3 Flash"},
    {"id": "glm-5.2", "display_name": "GLM-5.2"},
    {"id": "deepseek-v4-pro", "display_name": "DeepSeek V4 Pro"},
    {"id": "deepseek-v4-flash", "display_name": "DeepSeek V4 Flash"},
    {"id": "inkling", "display_name": "Inkling"},
    {"id": "nemotron-3-ultra", "display_name": "Nemotron 3 Ultra"},
)


class DevinCliAgentError(RuntimeError):
    """The Devin CLI route could not plan or complete a turn."""


#: Provider descriptor, shaped like the AntiGravity CLI-only entry in
#: provider_registry: there is no hosted endpoint to point at, the installed
#: ``devin`` binary fronting its own ``devin auth login`` session is the whole
#: transport, and routing fields stay inert placeholders so settings
#: round-trip. Registered into PROVIDERS by provider_registry once that file
#: is free; ``credential_account``/``credential_env`` stay None because Route
#: 2 never reads a credential — the CLI owns its login.
DESCRIPTOR = {
    "id": PROVIDER_ID,
    "name": PROVIDER_NAME,
    "protocol": "cli",
    "default_base_url": "",
    "default_region": "local",
    "regions": {"local": ""},
    "auth_header": {"name": "", "prefix": ""},
    "credential_account": None,
    "credential_env": None,
    "setup_url": "https://devin.ai/",
    "capabilities": {
        "streaming": True,
        # The route answers text/thinking only; client tools are dropped at
        # plan time and Devin's own tools are denied at the ACP seam.
        "tools": False,
        "thinking": True,
        "vision": False,
        "model_discovery": "cli",
        "reasoning_history": "not_applicable",
    },
}

OFFICIAL_PATHS = {""}


# ---------------------------------------------------------------------------
# Read-only probes
# ---------------------------------------------------------------------------


def _resolve_binary() -> str | None:
    """Resolve the devin binary from PATH and the installer's own directory."""
    extra = tuple(str(Path(item).expanduser()) for item in _EXTRA_BIN_DIRS)
    return resolve_binary(BINARY_NAMES, extra_dirs=extra)


def _default_capture(argv, *, timeout=30):
    """Run argv with the Route-2 minimal environment; (rc, out, err)."""
    try:
        result = subprocess.run(
            list(argv),
            capture_output=True,
            text=True,
            timeout=timeout,
            env=minimal_env(),
        )
        return result.returncode, result.stdout, result.stderr
    except subprocess.TimeoutExpired:
        return -1, "", f"Command timed out after {timeout}s"
    except Exception as exc:
        return -1, "", str(exc)


def _invoke(capture, argv, *, timeout=30) -> tuple[int | None, str, str]:
    """Call an injected capture (or the real subprocess runner) on full argv.

    ``capture`` is the adapter contract's read-only probe seam: a callable
    ``capture(argv, *, timeout) -> (rc, stdout, stderr)``. It receives the
    resolved binary as argv[0], exactly like the real spawn.
    """
    binary = _resolve_binary()
    if not binary:
        return (None, "", "devin CLI not found")
    runner = capture or _default_capture
    full_argv = [binary] + list(argv)
    try:
        rc, out, err = runner(full_argv, timeout=timeout)
    except TypeError:
        rc, out, err = runner(full_argv)
    return rc, out, err


def discover(*, capture=None) -> dict:
    """Report whether the CLI is installed and which version it claims."""
    binary = _resolve_binary()
    if not binary:
        return {"installed": False, "binary": None, "version": None}
    version = None
    try:
        rc, out, err = _invoke(capture, ["version"], timeout=30)
        if rc in (0, None):
            version = (out.strip().split("\n")[0] or err.strip().split("\n")[0]) or None
    except Exception:
        version = None
    return {"installed": True, "binary": binary, "version": version}


def auth_state(*, capture=None) -> dict:
    """Ask ``devin auth status`` — and only the CLI — whether it is signed in.

    Never reads ``~/.local/share/devin/credentials.toml`` or the Keychain:
    Route 2's discipline is that the CLI owns its login end to end.
    """
    binary = _resolve_binary()
    if not binary:
        return {"state": "missing",
                "detail": "the devin CLI is not installed or not on PATH"}
    try:
        rc, out, err = _invoke(capture, ["auth", "status"], timeout=30)
    except Exception as exc:
        return {"state": "unknown", "detail": f"auth probe failed: {exc}"}

    text = f"{out}\n{err}".casefold()
    if "not logged in" in text:
        return {"state": "missing",
                "detail": "devin reports no active login; sign in with the devin "
                          "CLI itself (`devin auth login`)"}
    if rc == 0:
        detail = next((line.strip() for line in out.splitlines() if line.strip()),
                      "signed in")
        return {"state": "authenticated", "detail": detail}
    tail = (err.strip() or out.strip().splitlines() or [""])[-1]
    return {"state": "unknown",
            "detail": f"devin auth status exited {rc}" + (f": {tail}" if tail else "")}


def catalogue(*, capture=None, timeout=30) -> tuple[list[dict], list[str]]:
    """The account's live model inventory via ``devin models list --format json``.

    Signed out, the subcommand exits non-zero with "Not logged in" — that is
    reported as a note, not an exception, and the hub falls back to
    KNOWN_MODELS seeds. An unrecognised JSON shape degrades the same way
    rather than emitting garbage rows.
    """
    binary = _resolve_binary()
    if not binary:
        return [], ["the devin CLI is not installed or not on PATH"]
    try:
        rc, out, err = _invoke(capture, ["models", "list", "--format", "json"],
                               timeout=timeout)
    except Exception as exc:
        return [], [f"devin models list probe failed: {exc}"]
    if rc != 0:
        text = f"{out}\n{err}".casefold()
        if "not logged in" in text or "auth login" in text:
            return [], ["devin is not signed in; run `devin auth login` "
                        "(or set WINDSURF_API_KEY) to list account models"]
        tail = (err.strip() or out.strip().splitlines() or [""])[-1]
        return [], [f"devin models list exited {rc}" + (f": {tail}" if tail else "")]
    try:
        payload = json.loads(out)
    except ValueError:
        return [], ["devin models list returned non-JSON output"]
    rows = catalogue_rows(payload)
    if not rows:
        return [], ["devin models list returned no recognisable model rows"]
    return rows[:100], []


# ---------------------------------------------------------------------------
# argv construction
# ---------------------------------------------------------------------------


def _validate_model(model: str | None) -> str:
    """Validate and return the model identifier for ``--model``.

    Devin accepts family slugs, aliases, partial names, and exact variant uids;
    the catalogue only ever emits ids, but a hand-built request may carry an
    alias. A leading dash is rejected so a model string can never become a
    flag.
    """
    if model is None:
        return DEFAULT_MODEL
    if not isinstance(model, str):
        raise DevinCliAgentError(f"model must be a string, got {type(model).__name__}")
    stripped = model.strip()
    if not stripped:
        return DEFAULT_MODEL
    if stripped.startswith("-"):
        raise DevinCliAgentError(f"model {model!r} is not a valid --model value")
    return stripped


def build_argv(model, *, effort=None, system=None, stream=True, **_ignored) -> list[str]:
    """Full argv for one ACP turn. argv[0] is the bare binary name.

    ``effort`` is accepted and ignored on purpose: Devin bakes the reasoning
    level into the variant uid, and this route's catalogue rows are family
    slugs that resolve to each family's own default variant. Effort folding
    (family + level -> exact uid) is a documented follow-up, not silently
    dropped behaviour — no caller can request an effort this route honours.
    ``system`` never reaches argv (SYSTEM_PROMPT_TRANSPORT is "prompt").
    """
    return ["devin", "acp", "--model", _validate_model(model)]


# ---------------------------------------------------------------------------
# Prompt rendering
# ---------------------------------------------------------------------------

_TRANSCRIPT_HEADER = (
    "You are answering through an external host application. "
    "The transcript below is conversation history. "
    "Respond to its final user turn with plain text."
)


def _coerce_messages(messages: Any) -> list[dict]:
    """Normalize the messages list for rendering."""
    if not isinstance(messages, (list, tuple)):
        raise DevinCliAgentError("request messages must be a list")
    normalized: list[dict] = []
    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            raise DevinCliAgentError(f"message {index} must be an object")
        role = message.get("role")
        if role not in _ROLES:
            raise DevinCliAgentError(
                f"message {index} has unsupported role {role!r}; "
                f"expected one of {', '.join(_ROLES)}"
            )
        content = message.get("content")
        if content is None:
            content = ""
        if not isinstance(content, str):
            content = str(content)
        normalized.append({"role": role, "content": content,
                           **({TOOL_RESULTS_KEY: True}
                              if message.get(TOOL_RESULTS_KEY) is True else {})})
    if not any(item["role"] == "user" for item in normalized):
        raise DevinCliAgentError("request contains no user message to answer")
    return normalized


def render_prompt(messages, *, system=None) -> str:
    """Render the whole stateless conversation into one prompt string.

    Order is system, then user/assistant turns in the order given — the same
    transcript convention the other text adapters use, because Devin's ACP
    session is per-turn here and carries no resumable history of its own.
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
# ACP turn execution
# ---------------------------------------------------------------------------


def _session_id(result: Any) -> str | None:
    """Extract the session id from a session/new result, tolerating shapes."""
    if not isinstance(result, dict):
        return None
    for key in ("sessionId", "session_id"):
        value = result.get(key)
        if isinstance(value, str) and value:
            return value
    session = result.get("session")
    if isinstance(session, dict):
        for key in ("id", "sessionId", "session_id"):
            value = session.get(key)
            if isinstance(value, str) and value:
                return value
    value = result.get("id")
    return value if isinstance(value, str) and value else None


def _content_text(update: dict) -> str | None:
    """Best-effort text out of an ACP content block or raw text field."""
    for value in (update.get("content"), update.get("text")):
        if isinstance(value, str) and value:
            return value
        if isinstance(value, dict):
            text = value.get("text")
            if isinstance(text, str) and text:
                return text
            inner = value.get("content")
            if isinstance(inner, list):
                parts = [block.get("text") for block in inner
                         if isinstance(block, dict) and isinstance(block.get("text"), str)]
                if parts:
                    return "".join(parts)
    return None


def _update_events(item: dict) -> Iterator[dict]:
    """One session/update notification -> adapter events (text/thinking only).

    Devin's own tool-call, plan, mode, and command updates are deliberately
    not surfaced: this route's contract is text in, text out, and the agent's
    native tools are already denied at the permission seam.
    """
    params = item.get("params")
    update = params.get("update") if isinstance(params, dict) else None
    if not isinstance(update, dict):
        return
    kind = str(update.get("sessionUpdate") or "")
    if "thought" in kind or "reasoning" in kind:
        text = _content_text(update)
        if text:
            yield {"type": "thinking_delta", "text": text}
    elif "agent_message" in kind or "assistant_message" in kind:
        text = _content_text(update)
        if text:
            yield {"type": "text_delta", "text": text}


def _reject_option(options: Any) -> str | None:
    """Pick a reject optionId, preferring the one-shot kind — never an allow."""
    entries = options if isinstance(options, list) else []
    kinds = []
    for entry in entries:
        if isinstance(entry, dict):
            option_id = entry.get("optionId")
            kind = entry.get("kind")
            if isinstance(option_id, str) and option_id:
                kinds.append((option_id, str(kind or "")))
    for wanted in ("reject_once",):
        for option_id, kind in kinds:
            if kind == wanted:
                return option_id
    for option_id, kind in kinds:
        if kind.startswith("reject"):
            return option_id
    return None


def _answer_inbound(session: StdioSession, item: dict) -> None:
    """Answer an agent->client JSON-RPC request; best-effort, never raises.

    ``session/request_permission`` is always denied: v1 grants the agent no
    side effects at all. Every other request method gets -32601 so the peer
    is never left waiting on an unanswered id.
    """
    rpc_id = item.get("id")
    method = item.get("method")
    try:
        if method == _PERMISSION_METHOD:
            params = item.get("params") if isinstance(item.get("params"), dict) else {}
            option_id = _reject_option(params.get("options"))
            outcome = ({"outcome": "selected", "optionId": option_id}
                       if option_id else {"outcome": "cancelled"})
            session.send(json.dumps({"jsonrpc": "2.0", "id": rpc_id,
                                     "result": {"outcome": outcome}}))
        else:
            session.send(json.dumps({"jsonrpc": "2.0", "id": rpc_id,
                                     "error": {"code": _METHOD_NOT_FOUND,
                                               "message": "Method not found"}}))
    except Exception:  # noqa: BLE001 - a dead pipe ends the turn via EOF anyway
        pass


def _map_stop_reason(value: Any) -> str:
    """ACP stopReason -> the hub's stop_reason vocabulary."""
    if value == "max_tokens":
        return "max_tokens"
    if value in {"max_turn_requests", "max_turns"}:
        return "max_tokens"
    if value == "stop_sequence":
        return "stop_sequence"
    # end_turn, refusal, cancelled, and anything a newer CLI invents all mean
    # "the turn is over"; refusal detail is not representable on this route.
    return "end_turn"


def _rpc_error(response: Any, method: str) -> str | None:
    """A human-readable failure from a JSON-RPC response dict, or None."""
    if not isinstance(response, dict):
        return f"the devin CLI returned no response to '{method}'"
    error = response.get("error")
    if isinstance(error, dict):
        message = error.get("message")
        return f"the devin CLI rejected '{method}': {message or error}"
    if error is not None:
        return f"the devin CLI rejected '{method}': {error}"
    return None


def _plan(request: Any) -> dict:
    """Validate the request and render the ACP prompt content."""
    if not isinstance(request, dict):
        raise DevinCliAgentError("request must be an object")
    model = _validate_model(request.get("model"))
    messages = _coerce_messages(request.get("messages"))
    system = request.get("system")
    if system is not None and not isinstance(system, str):
        raise DevinCliAgentError("system must be a string or None")
    return {"model": model, "prompt": render_prompt(messages, system=system)}


def _drive_turn(session: StdioSession, plan: dict, cwd: str,
                *, timeout: float) -> Iterator[dict]:
    """Run the ACP handshake + one prompt, yielding adapter events.

    initialize -> session/new -> session/prompt, then a manual event loop:
    ``session.request`` cannot be used for the prompt itself because the agent
    sends ``session/request_permission`` *requests* mid-turn that must be
    answered while the prompt is still outstanding.
    """
    deadline = time.monotonic() + timeout
    try:
        remaining = max(0.01, deadline - time.monotonic())
        response = session.request("initialize", _INITIALIZE_PARAMS,
                                   timeout=min(60.0, remaining))
    except CliSessionError as exc:
        yield {"type": "error", "message": str(exc)}
        return
    failure = _rpc_error(response, "initialize")
    if failure:
        yield {"type": "error", "message": failure}
        return

    session.notify("initialized", {})
    try:
        remaining = max(0.01, deadline - time.monotonic())
        response = session.request("session/new",
                                   {"cwd": cwd, "mcpServers": []},
                                   timeout=min(60.0, remaining))
    except CliSessionError as exc:
        yield {"type": "error", "message": str(exc)}
        return
    failure = _rpc_error(response, "session/new")
    if failure:
        yield {"type": "error", "message": failure}
        return
    session_id = _session_id(response.get("result") if isinstance(response, dict) else None)
    if not session_id:
        yield {"type": "error",
               "message": "the devin CLI opened no ACP session (no sessionId in session/new)"}
        return

    # The prompt id continues the session's own request-id sequence so it can
    # never collide with the initialize/session/new ids request() already used.
    prompt_id = next(session._ids)
    try:
        session.send(json.dumps({
            "jsonrpc": "2.0", "id": prompt_id, "method": "session/prompt",
            "params": {"sessionId": session_id,
                       "prompt": [{"type": "text", "text": plan["prompt"]}]},
        }))
    except (OSError, ValueError, BrokenPipeError) as exc:
        yield {"type": "error", "message": f"could not write the ACP prompt: {exc}"}
        return

    stop_reason = None
    prompt_error = None
    while stop_reason is None and prompt_error is None:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or getattr(session, "_eof", False):
            break
        if session.returncode is not None:
            break
        for item in session.events(timeout=min(2.0, max(0.05, remaining))):
            if not isinstance(item, dict):
                continue  # raw diagnostic lines carry no protocol state
            method = item.get("method")
            if method is not None and "id" in item:
                _answer_inbound(session, item)
                continue
            if method == "session/update":
                yield from _update_events(item)
                continue
            if method is not None:
                continue  # notifications we do not model (mode, commands, usage)
            if item.get("id") == prompt_id:
                failure = _rpc_error(item, "session/prompt")
                if failure:
                    prompt_error = failure
                else:
                    result = item.get("result")
                    reason = result.get("stopReason") if isinstance(result, dict) else None
                    stop_reason = _map_stop_reason(reason)
                break
        if getattr(session, "_eof", False):
            break

    if prompt_error is not None:
        yield {"type": "error", "message": prompt_error}
        return
    if stop_reason is not None:
        yield {"type": "message_stop", "stop_reason": stop_reason}
        return
    if deadline - time.monotonic() <= 0:
        # Best-effort cancel before the teardown ladder kills the child.
        try:
            session.notify("session/cancel", {"sessionId": session_id})
        except Exception:  # noqa: BLE001
            pass
        yield {"type": "error",
               "message": f"the devin CLI did not finish within {timeout:.0f}s; "
                          "Provider Hub stopped it"}
        return
    yield {"type": "error",
           "message": "the devin acp session ended before completing the turn"}


def run_turn(request, *, spawner=None, timeout=300) -> Iterator[dict]:
    """Stream one turn as text_delta / thinking_delta / message_stop / error.

    Always terminates with exactly one message_stop or error event, and never
    raises: every failure mode (missing binary, bad request, ACP error,
    child exit, timeout) degrades to an error event.
    """
    workspace_path: str | None = None
    stderr_handle = None
    session = None
    try:
        plan = _plan(request)
        binary = _resolve_binary()
        if not binary:
            raise DevinCliAgentError(
                "the devin CLI was not found on PATH; install the Devin CLI "
                "(`curl -fsSL https://cli.devin.ai/install.sh | bash`) and sign "
                "in with `devin auth login`, or switch this route to API-key "
                "credentials"
            )
        # A fresh empty directory per turn: the agent's own tools (which this
        # route cannot supervise) have no workspace to touch, and session/new
        # still gets a real cwd the CLI requires.
        workspace_path = tempfile.mkdtemp(prefix="devin_cli_")
        stderr_handle = tempfile.TemporaryFile(mode="w+", encoding="utf-8")
        argv = build_argv(plan["model"])
        argv[0] = binary
        session = StdioSession(
            argv,
            env=minimal_env(),
            cwd=workspace_path,
            timeout=float(timeout),
            spawner=spawner,
            stderr=stderr_handle,
        )
    except Exception as exc:  # noqa: BLE001 - every setup failure is an event
        if workspace_path:
            shutil.rmtree(workspace_path, ignore_errors=True)
        if stderr_handle is not None:
            stderr_handle.close()
        yield {"type": "error", "message": str(exc)}
        return

    def release_files():
        shutil.rmtree(workspace_path, ignore_errors=True)
        if stderr_handle is not None:
            stderr_handle.close()

    try:
        with session:
            yield from _drive_turn(session, plan, workspace_path,
                                   timeout=float(timeout))
    except Exception as exc:  # noqa: BLE001
        yield {"type": "error", "message": str(exc)}
    finally:
        cleanup_after_exit(session, release_files)
