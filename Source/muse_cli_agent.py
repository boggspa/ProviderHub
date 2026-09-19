"""Muse Code CLI as a hub MODEL ROUTE (local-only, default-off experiment).

Route 2 — CLI-as-transport. The vendor CLI owns and refreshes its own login.
This module NEVER reads, copies, or refreshes a credential file: it never
touches ``~/.muse``, the Keychain, or ``META_API_KEY``. There is no read-only
auth-status subcommand to ask, so :func:`auth_state` reports ``"unsupported"``
rather than guessing.

These routes are deliberately **pure text-in/text-out** so they behave like
model routes instead of delegatory headless agents. The desktop harness owns
the tool loop; if this route also ran tools, every tool call would be executed
twice against two different views of the world. The whole built-in tool set is
switched off with the flags Muse actually exposes (below), and no auto-approve
flag is ever constructed — ``_FORBIDDEN_FLAGS`` is asserted against inside
``build_argv`` so a future edit cannot introduce one quietly.

Transport: one-shot ``muse exec --json`` with NDJSON streaming. The prompt
travels via ``--prompt-file <temp file>`` — never as a positional argument and
never on stdin. This is the one place where evidence overrides the shared
Claude template: ``muse exec`` has no stdin-prompt mode, and ``--image`` is a
repeatable (variadic) flag that would swallow anything that follows it on the
command line. The conversation is stateless: history is rendered into the
prompt itself, so nothing depends on ``resume``/session ids, and
``--no-session-log`` keeps the CLI from writing a session the hub cannot
resume anyway.

``SYSTEM_PROMPT_TRANSPORT`` says where the harness system prompt goes, so the
hub can wire providers generically: ``"prompt"`` here, because ``muse exec``
exposes no system-prompt flag (verified against ``muse exec --help``).

Live evidence (Muse Code 1.3.0, ``1.3.0-R3401.1``, binary at ``~/.local/bin/muse``):

* ``muse exec --help`` — ``--json`` emits JSONL on stdout; ``--prompt-file
  <PATH>`` reads the prompt from a file; ``--reasoning-effort`` accepts exactly
  the full desktop ladder ``none|minimal|low|medium|high|xhigh|max|ultra``
  (default ``high``); safety flags include ``--disable-shell``,
  ``--disable-write``, ``--disable-web-tools``, ``--no-foreign-personal-context``,
  ``--approval-judge <off|on>``, ``--no-session-log``, ``--yolo``,
  ``--disable-approval``, ``--disable-sandbox``, ``--approval-mode``.
* ``muse exec --json --provider echo "prompt"`` completes rc 0 and emits MSP
  JSONL on stdout; diagnostics ("muse: workspace root ...") go to **stderr**.
  The final record is ``payload_type: "run.terminal.completed"`` with
  ``payload.kind == "run_terminal"``, ``payload.terminal == "completed"``,
  ``payload.text`` carrying the full answer and ``payload.reason`` null.
  Streaming text arrives as ``payload_type: "run.output.delta"``
  (``payload.kind == "run_output_delta"``, ``payload.text``).
* ``printf 'hi' | muse exec --json`` (no positional) prints ``missing prompt``
  and exits 2, and ``--prompt-file -`` errors ("failed to read --prompt-file -"):
  the prompt is neither stdin nor positional; ``--prompt-file <real file>`` is
  the only non-argv channel. Verified working with a real temp file.
* ``muse auth --help`` — only ``auth set``; ``muse auth status`` -> "expected
  ``auth set``" (rc 2); ``muse login --help`` — takes no arguments;
  ``muse logout`` only removes a credential. No read-only status subcommand.
* ``muse schema generate-json-schema`` (offline) — the serve-protocol schema
  documents the exact handshake and query shapes: ``initialize`` params require
  ``clientInfo`` (``name`` matching ``^[a-z0-9_]+$``, plus ``version``); the
  ``initialized`` client-to-server notification carries no params;
  ``model/list`` params accept only an optional ``sessionId``; and
  ``ModelListResult`` is ``{providerId, profileId, source, models[]}`` with each
  ``ModelCatalogEntry`` carrying ``modelId``, ``displayLabel``, ``contextLimit``,
  ``outputLimit``, ``description``, ``isDefault``, ``isActive``, ``cost``,
  ``releaseDate``. No cursor paging exists in v1.
* Live serve probe — ``muse serve --disable-shell --disable-write`` on stdio,
  driven with ``StdioSession``: ``initialize`` (clientInfo name
  ``provider_hub_muse_cli``) answered with ``serverInfo {name:"muse",
  version:"1.3.0"}`` and ``schema {version:1, fingerprint:<sha256>}``; after
  ``initialized``, ``model/list`` returned ``providerId:"meta"``,
  ``profileId:"tbh"``, ``source:"providerCatalog"`` and four visible rows:
  ``muse-spark-1.3``, ``muse-spark-1.3-contributor`` (default),
  ``muse-spark-1.2``, ``muse-spark-1.2-contributor`` — all ``contextLimit``
  1007997 / ``outputLimit`` 128000. ``muse export --help`` says reasoning is
  "verbatim encrypted".

Inferred (NOT verified live): the real ``meta`` provider's streaming text also
uses ``run.output.delta`` (confirmed only with ``echo``), and thinking/reasoning
is encrypted at rest, so there is no plaintext thinking-delta vocabulary — the
route therefore emits only ``text_delta`` and a terminal event. Stop reasons are
the ``run.terminal.*`` suffix: ``completed``/``failed``/``cancelled`` (matching
the schema's ``TurnTerminal`` enum).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Iterable, Iterator

from cli_session import CliSessionError, StdioSession, minimal_env, resolve_binary

try:  # Repo-native effort ladder; degrade to a local copy if unavailable.
    from effort_map import map_effort as _map_effort
except Exception:  # pragma: no cover - defensive import guard
    _map_effort = None


class MuseCliAgentError(RuntimeError):
    """The Muse Code CLI route could not be described, resolved, or driven."""


# ---------------------------------------------------------------------------
# Provider identity
# ---------------------------------------------------------------------------

PROVIDER_ID = "muse"
PROVIDER_NAME = "Muse (Muse Code CLI)"
TRANSPORT = "exec"
BINARY_NAMES = ("muse",)

# Where the harness system prompt is delivered. "flag" => build_argv emits it;
# "prompt" => the CLI has no such flag and it is folded into the stdin/prompt
# text. muse exec has no system-prompt flag, so the prompt is the only channel.
SYSTEM_PROMPT_TRANSPORT = "prompt"

# Real installs live outside the default PATH on this machine (~/.local/bin).
_EXTRA_BIN_DIRS = ("~/.local/bin", "/opt/homebrew/bin", "/usr/local/bin")

# ---------------------------------------------------------------------------
# Safety invariants
# ---------------------------------------------------------------------------

# Never constructed, never forwarded, asserted against in build_argv.
# ``--approval-mode`` is forbidden wholesale because its ``never`` value is an
# auto-approve escape hatch and the split-on-"=" assert must catch both the
# ``--approval-mode never`` and ``--approval-mode=never`` spellings.
_FORBIDDEN_FLAGS = frozenset({
    "--yolo",
    "--disable-approval",
    "--disable-sandbox",
    "--approval-mode",
})

# The read-only posture. These are the real tool-stripping flags muse exec
# exposes (verified with `muse exec --help` and one live echo run). Approval
# stays at its default (on-request, fail-closed in headless mode) and the LLM
# approval judge is switched off so no Prompt-bound call is silently approved.
READ_ONLY_FLAGS = (
    "--disable-shell",
    "--disable-write",
    "--disable-web-tools",
    "--no-foreign-personal-context",
    "--approval-judge", "off",
    "--no-session-log",
)

# Variadic/repeatable: run_turn appends ``--prompt-file <path>`` after
# build_argv's output, so a repeatable flag must never appear here or it would
# swallow the ``--prompt-file`` value.
_VARIADIC_TOOL_FLAGS = ("--image",)

# ---------------------------------------------------------------------------
# Models and effort
# ---------------------------------------------------------------------------

# muse exec --reasoning-effort accepts exactly the full desktop ladder
# (verified against `muse exec --help` and the closed enum in the exported
# MSP schema). No aliasing is needed: every canonical rank maps to itself.
MUSE_EFFORTS = ["none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"]
MUSE_EFFORT_ALIASES = {rank: rank for rank in MUSE_EFFORTS}

# LIVE-VERIFIED SEED, NOW A FALLBACK. catalogue() drives `muse serve` model/list
# over stdio (see below) and returns the installed CLI's own rows. These ids
# were captured from that live probe on Muse Code 1.3.0 (providerId "meta",
# profileId "tbh", source "providerCatalog") and are kept as the seed the hub
# falls back to when the serve probe is unavailable. Reasoning effort is a
# CLI-level flag accepting the full desktop ladder, so every model advertises
# it; the documented default is "high".
KNOWN_MODELS = (
    {"id": "muse-spark-1.3",
     "display_name": "muse-spark-1.3",
     "reasoning_levels": list(MUSE_EFFORTS),
     "default_effort": "high",
     "context": 1007997, "max_output": 128000},
    {"id": "muse-spark-1.3-contributor",
     "display_name": "muse-spark-1.3-contributor",
     "reasoning_levels": list(MUSE_EFFORTS),
     "default_effort": "high",
     "context": 1007997, "max_output": 128000,
     "description": "Your content, including inter-session messages, may be "
                    "used for product improvement."},
    {"id": "muse-spark-1.2",
     "display_name": "muse-spark-1.2",
     "reasoning_levels": list(MUSE_EFFORTS),
     "default_effort": "high",
     "context": 1007997, "max_output": 128000},
    {"id": "muse-spark-1.2-contributor",
     "display_name": "muse-spark-1.2-contributor",
     "reasoning_levels": list(MUSE_EFFORTS),
     "default_effort": "high",
     "context": 1007997, "max_output": 128000,
     "description": "Your content, including inter-session messages, may be "
                    "used for product improvement."},
)

# MSP stdio handshake identity. clientInfo.name must match ^[a-z0-9_]+$ (the
# schema's own ClientInfo pattern), so no hyphens or dots.
_CLIENT_NAME = "provider_hub_muse_cli"
_CLIENT_VERSION = "1.3.0"

# Documented default for `muse exec --reasoning-effort` (verified --help).
_MUSE_DEFAULT_EFFORT = "high"

# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

_MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/+\-]{0,199}\Z")
_VERSION = re.compile(r"(\d+(?:\.\d+)+(?:[.\-+][0-9A-Za-z.\-+]*)?)")
_ROLES = ("user", "assistant")
_MAX_STDERR_CHARS = 2000


def _validate_model(model: Any) -> str:
    if not isinstance(model, str) or not model.strip():
        raise MuseCliAgentError("a non-empty model id is required")
    candidate = model.strip()
    if not _MODEL_ID.match(candidate):
        raise MuseCliAgentError(f"refusing suspicious model id {model!r}")
    return candidate


def _validate_effort(effort: Any) -> str | None:
    """Normalize a harness effort rank onto one muse actually accepts."""
    if effort is None:
        return None
    if not isinstance(effort, str) or not effort.strip():
        raise MuseCliAgentError(f"refusing suspicious effort {effort!r}")
    requested = effort.strip()
    if _map_effort is not None:
        mapped = _map_effort(requested, MUSE_EFFORTS, MUSE_EFFORT_ALIASES)
        if mapped:
            return mapped
    alias = MUSE_EFFORT_ALIASES.get(requested.casefold())
    if alias:
        return alias
    raise MuseCliAgentError(
        f"effort {requested!r} is not servable by {PROVIDER_NAME}; "
        f"expected one of {', '.join(MUSE_EFFORTS)}"
    )


def _assert_safe(argv: Iterable[str]) -> list[str]:
    """Fail closed if an auto-approve flag ever reaches argv construction."""
    materialized = [str(item) for item in argv]
    offenders = [item for item in materialized
                 if item in _FORBIDDEN_FLAGS
                 or item.split("=", 1)[0] in _FORBIDDEN_FLAGS]
    if offenders:
        raise MuseCliAgentError(
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
    """Call an injected capture, adapting to whichever kwargs it accepts."""
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
    """Report the auth posture, which Muse Code does not expose read-only.

    Verified live: ``muse auth`` only offers ``auth set``, ``muse login`` is an
    interactive browser flow that takes no arguments, and ``muse logout`` only
    removes a credential. There is no ``auth status`` / ``login status`` /
    ``whoami`` to ask, so we report ``"unsupported"`` rather than guessing —
    this route is default-off and a wrong ``"missing"`` would look like a broken
    login.
    """
    binary = _resolve_binary()
    if not binary:
        return {"state": "missing",
                "detail": "the muse CLI is not installed or not on PATH"}
    return {"state": "unsupported",
            "detail": "Muse Code exposes no read-only auth-status subcommand; "
                      "the CLI owns and refreshes its own login"}


def _bounded_timeout(value, *, default, minimum=1.0):
    try:
        budget = float(value)
    except (TypeError, ValueError):
        return float(default)
    if budget != budget or budget in (float("inf"), float("-inf")):
        return float(default)
    return max(minimum, budget)


def _serve_argv(binary=None) -> list[str]:
    """argv for a `muse serve` stdio host, with the tool surface stripped."""
    if not binary:
        binary = _resolve_binary()
    if not binary:
        raise MuseCliAgentError("the muse CLI was not found on PATH")
    return [str(binary), "serve", "--disable-shell", "--disable-write"]


def _result(response, *, context):
    """Unwrap a JSON-RPC response, or raise with the server's own message.

    Tolerant of both conventions a ``request()`` implementation might use —
    the bare ``result`` payload or the whole envelope — because the session
    host is a sibling module.
    """
    if isinstance(response, dict):
        error = response.get("error")
        if isinstance(error, dict) and error:
            message = error.get("message") or json.dumps(error, sort_keys=True)[:300]
            code = error.get("code")
            suffix = f" (code {code})" if code is not None else ""
            raise MuseCliAgentError(
                f"{context}: the muse runtime reported an error{suffix}: {message}")
        if "result" in response:
            result = response["result"]
            return result if isinstance(result, dict) else {}
        return response
    raise MuseCliAgentError(f"{context}: the muse runtime returned a malformed response.")


def _handshake(session, *, timeout):
    """``initialize`` then ``initialized``, exactly as the MSP schema requires.

    Schema (verified offline via `muse schema generate-json-schema`): the
    ``initialize`` request params require ``clientInfo`` (with a ``name``
    matching ``^[a-z0-9_]+$`` and a ``version``), and the ``initialized``
    client-to-server notification carries no params. Nothing here starts a
    session or a turn — model discovery is a pure query.
    """
    response = session.request(
        "initialize",
        {"clientInfo": {"name": _CLIENT_NAME, "version": _CLIENT_VERSION}},
        timeout=max(1.0, timeout),
    )
    _result(response, context="initialize")
    session.notify("initialized", {})


def _row(raw) -> dict | None:
    """One live ``model/list`` row, normalized to the hub's CLI-row dialect.

    The wire shape (MSP ``ModelCatalogEntry``, tdd SS3.10) is camelCase:
    ``modelId`` (selectable id), ``displayLabel``, ``contextLimit``,
    ``outputLimit``, ``description``, ``isDefault``, ``isActive``, ``cost``,
    ``releaseDate``. The hub dialect consumed by ``cli_routes._hub_row`` is
    snake_case: ``id``, ``display_name``, ``reasoning_levels``, ``context``,
    ``max_output``, ``description``, ``default_effort``.
    """
    if not isinstance(raw, dict):
        return None
    model_id = raw.get("modelId")
    if not isinstance(model_id, str) or not model_id.strip():
        return None
    display = raw.get("displayLabel")
    if not isinstance(display, str) or not display.strip():
        display = model_id.strip()
    row = {
        "id": model_id.strip(),
        "display_name": display,
        # Reasoning effort is a CLI-level flag (full ladder), not per-model.
        "reasoning_levels": list(MUSE_EFFORTS),
        "default_effort": _MUSE_DEFAULT_EFFORT,
    }
    context = raw.get("contextLimit")
    if isinstance(context, int):
        row["context"] = context
    output = raw.get("outputLimit")
    if isinstance(output, int):
        row["max_output"] = output
    description = raw.get("description")
    if isinstance(description, str) and description.strip():
        row["description"] = description.strip()
    return row


def _parse_model_list(rows) -> list[dict]:
    """Normalize raw ``model/list`` rows, dropping anything without a model id."""
    parsed = []
    for raw in rows or []:
        entry = _row(raw)
        if entry is not None:
            parsed.append(entry)
    return parsed


def _fetch_models(*, binary=None, spawner=None, timeout=30) -> list[dict]:
    """Drive ``model/list`` over a `muse serve` stdio host.

    model/list is a snapshot query (the MSP schema's v1 has no catalog
    subscription and no cursor paging), so one request returns every visible
    row — ``ModelListResult`` is ``{providerId, profileId, source, models[]}``
    and ``ModelListParams`` accepts only an optional ``sessionId`` (omitted so
    no session is created).
    """
    budget = _bounded_timeout(timeout, default=30)
    argv = _serve_argv(binary=binary)
    stderr = tempfile.TemporaryFile(mode="w+", encoding="utf-8")
    session = None
    try:
        session = StdioSession(
            argv,
            env=minimal_env(),
            timeout=budget,
            spawner=spawner,
            stderr=stderr,
        )
        _handshake(session, timeout=budget)
        response = session.request("model/list", {}, timeout=max(1.0, budget))
        result = _result(response, context="model/list")
        models = result.get("models")
        if not isinstance(models, list):
            raise MuseCliAgentError("the muse runtime returned an invalid model catalogue")
        return _parse_model_list(models)
    finally:
        if session is not None:
            try:
                session.close()
            except Exception:
                pass
        try:
            stderr.close()
        except Exception:
            pass


def catalogue(*, capture=None, spawner=None, timeout=30) -> tuple[list[dict], list[str]]:
    """The installed Muse Code CLI's own model rows, plus human-readable notes.

    ``spawner`` is accepted in addition to the specified ``capture``/``timeout``
    because ``model/list`` is a JSON-RPC call over a long-lived stdio session,
    which the short-lived ``capture(argv, *, timeout) -> (rc, out, err)``
    contract cannot express. ``capture`` is still honoured as the execution
    signal: when either is injected, the real PATH is not walked for the
    availability check.

    Degrades rather than raising: an absent runtime, a wedged handshake, or a
    malformed catalogue returns ``([], [reason])`` so the hub's settings pane
    can say why the row is empty.
    """
    if capture is None and spawner is None:
        if _resolve_binary() is None:
            return [], ["the muse CLI is not installed or not on PATH"]
    try:
        rows = _fetch_models(spawner=spawner, timeout=timeout)
    except MuseCliAgentError as exc:
        return [], [str(exc)]
    except CliSessionError as exc:
        return [], [f"The muse serve session failed: {exc}"]
    except Exception as exc:  # noqa: BLE001 - a catalogue probe must never raise
        return [], [f"The muse catalogue could not be read: {exc.__class__.__name__}: {exc}"]
    if not rows:
        return rows, ["Muse Code advertised no models."]
    return rows, []


# ---------------------------------------------------------------------------
# argv construction
# ---------------------------------------------------------------------------

def build_argv(model, *, effort=None, system=None, stream=True) -> list[str]:
    """Full argv for one ``muse exec --json`` turn. argv[0] is the bare binary.

    ``run_turn`` replaces argv[0] with the resolved absolute path and appends
    ``--prompt-file <temp file>`` after this output; keeping the name here makes
    this function pure and testable on a machine with no CLI installed. The
    prompt is NOT part of argv — it travels via ``--prompt-file`` (muse exec
    reads no prompt from stdin, and its ``--image`` flag is repeatable, so a
    positional prompt would be swallowable). ``system`` is deliberately unused:
    with SYSTEM_PROMPT_TRANSPORT == "prompt" it is folded into the prompt text
    by run_turn, never into argv. ``stream`` is always effectively True — the
    only machine-readable mode is ``--json``.
    """
    validated_model = _validate_model(model)
    validated_effort = _validate_effort(effort)

    argv: list[str] = [BINARY_NAMES[0], "exec", "--json"]
    argv += list(READ_ONLY_FLAGS)
    argv += ["--model", validated_model]
    if validated_effort:
        argv += ["--reasoning-effort", validated_effort]

    # No variadic flag may appear: run_turn appends --prompt-file after this,
    # and a repeatable flag such as --image would swallow the path that follows.
    if any(item.split("=", 1)[0] in _VARIADIC_TOOL_FLAGS for item in argv):
        raise MuseCliAgentError(
            "internal error: a variadic flag must not appear in muse argv, "
            "because run_turn appends --prompt-file after it"
        )
    return _assert_safe(argv)


# ---------------------------------------------------------------------------
# Prompt rendering
# ---------------------------------------------------------------------------

_TRANSCRIPT_HEADER = (
    "You are being driven as a plain text completion model by an external "
    "harness. The transcript below is context only. You have no tools and "
    "cannot take actions; do not attempt to and do not describe attempting to. "
    "Reply with assistant text for the FINAL user turn only."
)
_TRANSCRIPT_FOOTER = 'Respond now to the final <turn role="user"> above.'


def _coerce_messages(messages: Any) -> list[dict]:
    if not isinstance(messages, (list, tuple)):
        raise MuseCliAgentError("request messages must be a list")
    normalized: list[dict] = []
    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            raise MuseCliAgentError(f"message {index} must be an object")
        role = message.get("role")
        if role not in _ROLES:
            raise MuseCliAgentError(
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
        raise MuseCliAgentError("request contains no user message to answer")
    return normalized


def render_prompt(messages, *, system=None) -> str:
    """Render the whole stateless conversation into one prompt text.

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


def _translate(payload: Any, state: _TurnState) -> list[dict]:
    """Convert one muse exec JSONL record into zero or more route events."""
    if not isinstance(payload, dict):
        return []
    payload_type = str(payload.get("payload_type") or "")
    body = payload.get("payload")
    if not isinstance(body, dict):
        body = {}
    kind = str(body.get("kind") or "")

    # Terminal run event: run.terminal.completed / .failed / .cancelled.
    if kind == "run_terminal" or payload_type.startswith("run.terminal."):
        terminal = str(body.get("terminal") or "")
        if terminal:
            state.stop_reason = terminal
        text = body.get("text")
        if isinstance(text, str) and text:
            state.result_text = text
        if terminal in ("failed", "cancelled"):
            detail = body.get("reason") or text or terminal
            state.failure = f"muse reported {terminal}: {detail}"
        return []

    # Streaming text: run.output.delta (payload.text is the appended fragment).
    if kind == "run_output_delta" or payload_type == "run.output.delta":
        text = body.get("text")
        if isinstance(text, str) and text:
            state.emitted_text = True
            return [{"type": "text_delta", "text": text}]
        return []

    # Defensive only: muse encrypts reasoning at rest (`muse export --help`),
    # so no plaintext thinking vocabulary was observed. Should one appear, map
    # it rather than dropping it.
    if "think" in payload_type.casefold():
        text = body.get("text") or body.get("thinking") or body.get("delta")
        if isinstance(text, str) and text:
            return [{"type": "thinking_delta", "text": text}]
        return []

    # Everything else is bookkeeping, never route output. In particular,
    # task.lifecycle.failed is a background sub-task failure, not a run failure:
    # the echo provider emits one and still completes successfully.
    return []


def _raw_stdin(session):
    """The session's writable stdin, wherever the host happens to keep it."""
    for owner in (session, getattr(session, "_proc", None),
                  getattr(session, "process", None)):
        stdin = getattr(owner, "stdin", None)
        if stdin is not None and callable(getattr(stdin, "write", None)):
            return stdin
    return None


def _close_raw_stdin(session) -> None:
    """Signal EOF on stdin.

    muse exec reads no prompt from stdin (the prompt is in --prompt-file), so
    closing it is defensive: a child that happens to probe stdin sees EOF
    immediately instead of blocking.
    """
    stdin = _raw_stdin(session)
    close = getattr(stdin, "close", None) if stdin is not None else None
    if callable(close):
        try:
            close()
        except OSError:
            pass


def _iter_events(session, *, timeout) -> Iterator[tuple[str, Any]]:
    """Yield ("json", obj) or ("raw", line) for each stdout record.

    Tolerates a session host that yields either parsed dicts or raw strings, and
    never lets a non-JSON line abort the turn: those lines are buffered for error
    reporting instead.
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
            yield "raw", line.strip()


def _write_prompt_file(prompt: str) -> str:
    """Write the rendered prompt to a temp file and return its path.

    muse exec reads the prompt from ``--prompt-file <path>``; the file keeps the
    prompt text out of argv (so it is neither positional-swallowable nor visible
    in ``ps``). The caller unlinks the path after the turn.
    """
    handle = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", suffix=".txt", prefix="muse_prompt_",
        delete=False)
    try:
        payload = prompt if prompt.endswith("\n") else prompt + "\n"
        handle.write(payload)
        handle.flush()
    finally:
        handle.close()
    return handle.name


def _cleanup_prompt(path: str | None) -> None:
    if not path:
        return
    try:
        os.unlink(path)
    except OSError:
        pass


def _describe(exc: BaseException, state: _TurnState | None = None,
              timeout=None) -> str:
    text = str(exc).strip() or exc.__class__.__name__
    lowered = text.casefold()
    if isinstance(exc, subprocess.TimeoutExpired) or "timed out" in lowered \
            or "timeout" in lowered:
        limit = f" after {timeout}s" if timeout else ""
        text = f"the muse CLI did not finish{limit}"
    elif isinstance(exc, CliSessionError):
        text = f"muse CLI session error: {text}"
    else:
        text = f"muse CLI turn failed: {text}"
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
    prompt_path: str | None = None
    try:
        if not isinstance(request, dict):
            raise MuseCliAgentError("request must be an object")
        model = _validate_model(request.get("model"))
        messages = _coerce_messages(request.get("messages"))
        effort = _validate_effort(request.get("effort"))
        system = request.get("system")
        if system is not None and not isinstance(system, str):
            raise MuseCliAgentError("system must be a string or None")
        max_tokens = request.get("max_tokens")
        if max_tokens is not None and not isinstance(max_tokens, int):
            raise MuseCliAgentError("max_tokens must be an integer or None")
        if isinstance(max_tokens, int) and max_tokens <= 0:
            raise MuseCliAgentError("max_tokens must be positive")

        # The system prompt travels inside the prompt text (no system flag), so
        # it is folded here and not duplicated into argv.
        prompt = render_prompt(messages, system=system)
        argv = build_argv(model, effort=effort, system=None, stream=True)

        binary = _resolve_binary()
        if not binary:
            raise MuseCliAgentError(
                "the muse CLI was not found on PATH; install Muse Code or "
                "switch this route to API-key credentials"
            )
        argv[0] = binary

        # The prompt goes into a temp file (muse exec reads no prompt from
        # stdin); stderr goes to a temp file too, not a pipe nobody drains.
        prompt_path = _write_prompt_file(prompt)
        argv += ["--prompt-file", prompt_path]

        state.stderr_handle = tempfile.TemporaryFile(mode="w+", encoding="utf-8")
        session = StdioSession(
            argv,
            env=minimal_env(),
            timeout=float(timeout),
            spawner=spawner,
            stderr=state.stderr_handle,
        )
    except Exception as exc:
        _cleanup_prompt(prompt_path)
        yield {"type": "error", "message": _describe(exc, state, timeout)}
        return

    stop_reason = state.stop_reason or "end_turn"
    try:
        with session:
            _close_raw_stdin(session)
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
            detail = f"the muse CLI exited with code {returncode}"
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
                    yield {"type": "error", "message": "the muse CLI produced no output"}
                    return
            stop_reason = state.stop_reason or "end_turn"
    except Exception as exc:
        yield {"type": "error", "message": _describe(exc, state, timeout)}
        return
    finally:
        _cleanup_prompt(prompt_path)
        handle = state.stderr_handle
        if handle is not None:
            try:
                handle.close()
            except Exception:
                pass

    yield {"type": "message_stop", "stop_reason": stop_reason}
