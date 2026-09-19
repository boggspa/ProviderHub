"""AntiGravity (agy) CLI as a hub MODEL ROUTE (local-only, default-off).

Route 2 — CLI-as-transport. The agy CLI owns its Google OAuth login end to end.
This module NEVER reads, copies, or refreshes a credential file, and never
touches the Keychain. agy exposes no non-interactive auth verb at all
(``agy auth status`` exits 2 with ``unexpected argument "auth"``), so
``auth_state()`` reports ``"unknown"`` and spawns nothing rather than inventing
a probe.

These routes are deliberately **pure text-in/text-out** so they behave like
model routes rather than delegatory headless agents; the desktop harness owns
the tool loop and a second tool loop would double-execute. The posture is
``--sandbox`` plus ``--mode plan`` plus ``--disable-slash-commands``, and no
auto-approve flag is ever constructed.

Worth recording, because it shaped the error handling: that posture does not
stop agy from *attempting* a tool call. Observed live on agy 1.2.7 with
``--effort high`` — the model called ``run_command``, headless mode had nobody
to prompt, the call was auto-denied, and the run still ended
``{"status":"SUCCESS","response":""}`` with ``denied_actions`` populated. So
``SUCCESS`` alone is not evidence of output. The denial is the safety property
working (fail closed, nothing executed), but a silently empty completion is not
an acceptable turn result, so run_turn converts that exact combination into an
error event. agy's own stderr suggests re-running with an auto-approve flag to
fix it; that flag is in ``_FORBIDDEN_FLAGS`` and is never forwarded.

Transport: one-shot print mode with NDJSON streaming. Two agy specifics that
differ from the other print-mode CLIs:

* ``-p``/``--print``/``--prompt`` are the SAME string flag and it takes the
  prompt as its value, so ``agy -p --output-format ...`` makes agy eat
  ``--output-format`` as the prompt and then fail. build_argv therefore passes
  no print flag at all and the prompt travels on stdin, which agy documents as
  a first-class prompt source ("Prompts are read only from -p/--print,
  -i/--prompt-interactive, or stdin"). Verified rc=0.
* Stream events are keyed ``"event"``, not ``"type"``, and reasoning is never
  streamed as text — only counted as ``thinking_tokens`` in usage.

``SYSTEM_PROMPT_TRANSPORT`` is ``"prompt"``: agy has no system-prompt flag, so
the harness system text is folded into the stdin prompt by ``render_prompt``.
"""
from __future__ import annotations

import json
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


class AgyCliAgentError(RuntimeError):
    """The AntiGravity CLI route could not be described, resolved, or driven."""


# ---------------------------------------------------------------------------
# Provider identity
# ---------------------------------------------------------------------------

PROVIDER_ID = "antigravity"
PROVIDER_NAME = "AntiGravity (agy CLI)"
TRANSPORT = "print"
BINARY_NAMES = ("agy", "antigravity")

# agy exposes no system-prompt flag, so the harness system text is rendered
# into the stdin prompt instead of being passed on the command line.
SYSTEM_PROMPT_TRANSPORT = "prompt"

_EXTRA_BIN_DIRS = ("~/.local/bin", "/opt/homebrew/bin", "/usr/local/bin")

# ---------------------------------------------------------------------------
# Safety invariants
# ---------------------------------------------------------------------------

_FORBIDDEN_FLAGS = frozenset({
    "--dangerously-skip-permissions",
    "--allow-dangerously-skip-permissions",
    "--always-approve",
    "--dangerously-bypass-approvals-and-sandbox",
    "--approve-for-me",
})

# The read-only posture: sandboxed, plan mode, no slash-command/skill
# expansion, and an unbounded CLI-side print timeout so OUR timeout is the only
# one that fires (a CLI-side timeout would report its own confusing failure).
READ_ONLY_FLAGS = (
    "--sandbox",
    "--mode", "plan",
    "--disable-slash-commands",
    "--input-format", "text",
    "--print-timeout", "0s",
)

# ---------------------------------------------------------------------------
# Models and effort
# ---------------------------------------------------------------------------

# agy --effort accepts exactly these three (agy --help, verified 1.2.7).
AGY_EFFORTS = ["low", "medium", "high"]
AGY_EFFORT_ALIASES = {
    "none": "low",
    "minimal": "low",
    "low": "low",
    "medium": "medium",
    "high": "high",
    "xhigh": "high",
    "max": "high",
    "ultra": "high",
}

# `agy models` emits ids whose trailing -low/-medium/-high is an EFFORT RUNG of
# one underlying model, not three separate models. The ladder is folded onto
# every member of the group so gemini-3.8-flash-high reports
# ["low","medium","high"], while each real id stays individually addressable —
# no synthetic base id is ever invented, because only the real ids resolve.
_EFFORT_SUFFIXES = ("low", "medium", "high")
_EFFORT_RANK = {"low": 0, "medium": 1, "high": 2}

# ---------------------------------------------------------------------------
# Model family collapse: 14 agy rows -> 7 model families
# ---------------------------------------------------------------------------

# agy row id -> (family_id, canonical_display_name, provider_effort_rung)
# The family_id is what the hub picker shows; the native row id is what we
# pass to --model on the wire. provider_effort_rung is the agy-native rung
# string (low/medium/high).
_AGY_ROW_MAP: dict[str, tuple[str, str, str]] = {
    # Gemini 3.8 Flash: 3 rungs
    "gemini-3.8-flash-low":    ("gemini-3.8-flash", "Gemini 3.8 Flash", "low"),
    "gemini-3.8-flash-medium": ("gemini-3.8-flash", "Gemini 3.8 Flash", "medium"),
    "gemini-3.8-flash-high":   ("gemini-3.8-flash", "Gemini 3.8 Flash", "high"),
    # Gemini 3.7 Flash: 3 rungs
    "gemini-3.7-flash-low":    ("gemini-3.7-flash", "Gemini 3.7 Flash", "low"),
    "gemini-3.7-flash-medium": ("gemini-3.7-flash", "Gemini 3.7 Flash", "medium"),
    "gemini-3.7-flash-high":   ("gemini-3.7-flash", "Gemini 3.7 Flash", "high"),
    # Gemini 3.6 Flash: 3 rungs
    "gemini-3.6-flash-low":    ("gemini-3.6-flash", "Gemini 3.6 Flash", "low"),
    "gemini-3.6-flash-medium": ("gemini-3.6-flash", "Gemini 3.6 Flash", "medium"),
    "gemini-3.6-flash-high":   ("gemini-3.6-flash", "Gemini 3.6 Flash", "high"),
    # Gemini 3.1 Pro: 2 rungs (low, high) - note: no "medium" row exists
    "gemini-3.1-pro-low":  ("gemini-3.1-pro", "Gemini 3.1 Pro", "low"),
    "gemini-3.1-pro-high": ("gemini-3.1-pro", "Gemini 3.1 Pro", "high"),
    # Claude Sonnet 4.6: 1 rung (thinking)
    "claude-sonnet-4-6":     ("claude-sonnet-4.6", "Claude Sonnet 4.6", "thinking"),
    # Claude Opus 4.6: 1 rung
    "claude-opus-4-6-thinking": ("claude-opus-4.6", "Claude Opus 4.6", "thinking"),
    # GPT-OSS: 1 rung (medium)
    "gpt-oss-120b-medium":   ("gpt-oss-120b", "GPT-OSS 120B", "medium"),
}

# Map agy-native rung strings to canonical EFFORT_ORDER ranks.
# agy only accepts low/medium/high on --effort, so "thinking" and bare names
# must be translated.
_AGY_RUNG_TO_CANONICAL: dict[str, str] = {
    "low": "low",
    "medium": "medium",
    "high": "high",
    "thinking": "high",  # Claude families: thinking -> high
}

# Reverse: canonical rank -> agy-native rung for each family.
# For families with fewer than 3 rungs, we map canonical ranks to the
# nearest available rung. This is used by build_argv to pick the native row.
_FAMILY_CANONICAL_TO_RUNG: dict[str, dict[str, str]] = {
    "gemini-3.8-flash": {"none": "low", "minimal": "low", "low": "low",
                         "medium": "medium", "high": "high",
                         "xhigh": "high", "max": "high", "ultra": "high"},
    "gemini-3.7-flash": {"none": "low", "minimal": "low", "low": "low",
                         "medium": "medium", "high": "high",
                         "xhigh": "high", "max": "high", "ultra": "high"},
    "gemini-3.6-flash": {"none": "low", "minimal": "low", "low": "low",
                         "medium": "medium", "high": "high",
                         "xhigh": "high", "max": "high", "ultra": "high"},
    # gemini-3.1-pro has only low and high (no medium)
    "gemini-3.1-pro": {"none": "low", "minimal": "low", "low": "low",
                        "medium": "high",  # medium -> high (nearest above)
                        "high": "high",
                        "xhigh": "high", "max": "high", "ultra": "high"},
    "claude-sonnet-4.6": {"none": "thinking", "minimal": "thinking",
                           "low": "thinking", "medium": "thinking",
                           "high": "thinking", "xhigh": "thinking",
                           "max": "thinking", "ultra": "thinking"},
    "claude-opus-4.6": {"none": "thinking", "minimal": "thinking",
                         "low": "thinking", "medium": "thinking",
                         "high": "thinking", "xhigh": "thinking",
                         "max": "thinking", "ultra": "thinking"},
    "gpt-oss-120b": {"none": "medium", "minimal": "medium", "low": "medium",
                      "medium": "medium", "high": "medium",
                      "xhigh": "medium", "max": "medium", "ultra": "medium"},
}

# Effort modes that each family advertises (canonical ranks).
_FAMILY_EFFORT_MODES: dict[str, list[str]] = {
    "gemini-3.8-flash": ["none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"],
    "gemini-3.7-flash": ["none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"],
    "gemini-3.6-flash": ["none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"],
    "gemini-3.1-pro": ["none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"],
    "claude-sonnet-4.6": ["none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"],
    "claude-opus-4.6": ["none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"],
    "gpt-oss-120b": ["none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"],
}

# Default effort for each family (canonical rank).
_FAMILY_DEFAULT_EFFORT: dict[str, str] = {
    "gemini-3.8-flash": "medium",
    "gemini-3.7-flash": "medium",
    "gemini-3.6-flash": "medium",
    "gemini-3.1-pro": "high",
    "claude-sonnet-4.6": "high",
    "claude-opus-4.6": "high",
    "gpt-oss-120b": "medium",
}

# Provider-native effort rungs exposed by each family (what agy accepts).
_FAMILY_PROVIDER_EFFORT_MODES: dict[str, list[str]] = {
    "gemini-3.8-flash": ["low", "medium", "high"],
    "gemini-3.7-flash": ["low", "medium", "high"],
    "gemini-3.6-flash": ["low", "medium", "high"],
    "gemini-3.1-pro": ["low", "high"],
    "claude-sonnet-4.6": ["thinking"],
    "claude-opus-4.6": ["thinking"],
    "gpt-oss-120b": ["medium"],
}


_NO_AUTH_VERB_DETAIL = (
    "agy exposes no non-interactive auth status verb; the CLI owns its Google "
    "login, so sign-in state cannot be probed read-only and is reported as "
    "unknown. A turn fails with the CLI's own message if it is not signed in."
)

# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

_MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/+\-]{0,199}\Z")
_VERSION = re.compile(r"(\d+(?:\.\d+)+(?:[.\-+][0-9A-Za-z.\-+]*)?)")
_ROLES = ("user", "assistant")
_MAX_STDERR_CHARS = 2000



def _resolve_native_row(family_id: str, effort: str | None) -> str:
    """Resolve (family_id, effort) to the native agy row id.
    
    family_id may be a family id or already a native row id.
    effort is a canonical rank. Returns the native row id to pass to --model.
    """
    # If it's already a native row id, just return it
    if family_id in _AGY_ROW_MAP:
        return family_id
    
    # It's a family id: look up the canonical->rung mapping
    rung_map = _FAMILY_CANONICAL_TO_RUNG.get(family_id, {})
    if effort and effort in rung_map:
        target_rung = rung_map[effort]
    else:
        # No effort specified or unknown: use default
        default_effort = _FAMILY_DEFAULT_EFFORT.get(family_id)
        if default_effort and default_effort in rung_map:
            target_rung = rung_map[default_effort]
        else:
            # Fall back to first available rung
            provider_modes = _FAMILY_PROVIDER_EFFORT_MODES.get(family_id, [])
            target_rung = provider_modes[0] if provider_modes else "high"
    
    # Find the native row id with this rung for this family
    for row_id, (fid, _, rung) in _AGY_ROW_MAP.items():
        if fid == family_id and rung == target_rung:
            return row_id
    
    # Fallback: return the first row for this family
    for row_id, (fid, _, _) in _AGY_ROW_MAP.items():
        if fid == family_id:
            return row_id
    
    # Last resort: return as-is
    return family_id


def _validate_model(model: Any) -> str:
    """Validate and resolve a model identifier.
    
    Accepts either a family id (e.g., "gemini-3.8-flash") or a native row id
    (e.g., "gemini-3.8-flash-high"). Family ids are resolved to their default
    native row for wire use.
    """
    if not isinstance(model, str) or not model.strip():
        raise AgyCliAgentError("a non-empty model id is required")
    candidate = model.strip()
    if not _MODEL_ID.match(candidate):
        raise AgyCliAgentError(f"refusing suspicious model id {model!r}")
    
    # If it's a known family id, resolve to its default native row
    if candidate in _FAMILY_DEFAULT_EFFORT:
        # Map family id to default native row
        default_rung = _FAMILY_CANONICAL_TO_RUNG.get(candidate, {}).get(
            _FAMILY_DEFAULT_EFFORT.get(candidate)
        )
        if default_rung:
            # Find the native row id that has this rung for this family
            for row_id, (family_id, _, rung) in _AGY_ROW_MAP.items():
                if family_id == candidate and rung == default_rung:
                    return row_id
    
    # If it's a known native row id, return it directly
    if candidate in _AGY_ROW_MAP:
        # Return the native row id as-is for wire use
        return candidate
    
    # Otherwise, accept it as-is (may be an unknown model)
    return candidate




def _validate_effort(effort: Any) -> str | None:
    """Normalize a harness effort rank onto one agy actually accepts."""
    if effort is None:
        return None
    if not isinstance(effort, str) or not effort.strip():
        raise AgyCliAgentError(f"refusing suspicious effort {effort!r}")
    requested = effort.strip()
    if _map_effort is not None:
        mapped = _map_effort(requested, AGY_EFFORTS, AGY_EFFORT_ALIASES)
        if mapped:
            return mapped
    alias = AGY_EFFORT_ALIASES.get(requested.casefold())
    if alias:
        return alias
    raise AgyCliAgentError(
        f"effort {requested!r} is not servable by {PROVIDER_NAME}; "
        f"expected one of {', '.join(AGY_EFFORTS)}"
    )


def _assert_safe(argv: Iterable[str]) -> list[str]:
    """Fail closed if an auto-approve flag ever reaches argv construction."""
    materialized = [str(item) for item in argv]
    offenders = [item for item in materialized
                 if item in _FORBIDDEN_FLAGS
                 or item.split("=", 1)[0] in _FORBIDDEN_FLAGS]
    if offenders:
        raise AgyCliAgentError(
            "refusing to build argv containing auto-approve flag(s): "
            + ", ".join(sorted(set(offenders)))
        )
    return materialized


# ---------------------------------------------------------------------------
# Capture plumbing (discover / catalogue)
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
# Model list parsing
# ---------------------------------------------------------------------------

def _collapse_models(raw_models: list[dict]) -> list[dict]:
    """Collapse 14 agy row ids into 7 model families for the hub picker.

    Each family card carries:
    - id: the family id (e.g., "gemini-3.8-flash")
    - display_name: canonical display name
    - effort_modes: full canonical EFFORT_ORDER ladder
    - default_effort: family default in canonical rank
    - provider_effort_modes: the underlying native rungs this family exposes
    - aliases: list of all native row ids that belong to this family
    - reasoning: True (all agy models support thinking)
    """
    # Build a mapping from family_id to all its row entries
    family_rows: dict[str, list[tuple[str, str]]] = {}  # family_id -> [(row_id, display)]
    for entry in raw_models:
        row_id = entry["id"]
        if row_id in _AGY_ROW_MAP:
            family_id, display_name, rung = _AGY_ROW_MAP[row_id]
            family_rows.setdefault(family_id, []).append((row_id, display_name))
        else:
            # Unknown row: pass through as its own family
            family_rows.setdefault(row_id, []).append((row_id, entry.get("display_name", row_id)))

    collapsed: list[dict] = []
    for family_id in sorted(family_rows.keys()):
        row_entries = family_rows[family_id]
        # Use the canonical display name from the first known row
        display_name = row_entries[0][1]
        # Collect all native row ids as aliases
        aliases = [row_id for row_id, _ in row_entries]

        collapsed.append({
            "id": family_id,
            "display_name": display_name,
            "effort_modes": list(_FAMILY_EFFORT_MODES.get(family_id, [])),
            "default_effort": _FAMILY_DEFAULT_EFFORT.get(family_id),
            "provider_effort_modes": list(_FAMILY_PROVIDER_EFFORT_MODES.get(family_id, [])),
            "aliases": aliases,
            "reasoning": True,
        })

    return collapsed




def _split_effort(model_id: str) -> tuple[str, str | None]:
    base, separator, tail = model_id.rpartition("-")
    if not separator or not base:
        return model_id, None
    rung = tail.casefold()
    return (base, rung) if rung in _EFFORT_SUFFIXES else (model_id, None)


def _parse_models(text: str) -> list[dict]:
    """Parse `agy models` TSV output into catalogue entries.

    Real output is a ``Fetching available models...`` banner (no tab), then
    ``id\\tDisplay Name`` rows, then a trailing blank line. Anything without a
    tab is skipped, which drops the banner, blank lines, and any plain-text
    diagnostic the CLI mixes in.
    """
    rows: list[tuple[str, str]] = []
    seen: set[str] = set()
    ladders: dict[str, set] = {}

    for line in (text or "").splitlines():
        if "\t" not in line:
            continue
        model_id, _, display = line.partition("\t")
        model_id = model_id.strip()
        if not model_id or model_id in seen:
            continue
        seen.add(model_id)
        rows.append((model_id, display.strip()))
        base, rung = _split_effort(model_id)
        if rung:
            ladders.setdefault(base, set()).add(rung)

    models: list[dict] = []
    for model_id, display in rows:
        base, rung = _split_effort(model_id)
        levels: list[str] = []
        if rung:
            levels = sorted(ladders.get(base) or {rung},
                            key=lambda item: _EFFORT_RANK.get(item, 99))
        models.append({
            "id": model_id,
            "display_name": display or model_id,
            "reasoning_levels": levels,
        })
    return models


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
        version = None
    return {"installed": True, "binary": binary, "version": version}


def auth_state(*, capture=None) -> dict:
    """Always ``"unknown"``: agy has no read-only auth verb to ask.

    ``capture`` is accepted for API symmetry with the other CLI routes and is
    deliberately never used — running ``agy auth status`` only produces
    ``Error: unexpected argument "auth"`` (exit 2), and treating that as
    "missing" would report a working Google login as signed out. Nothing here
    reads a credential file or the Keychain.
    """
    binary = _resolve_binary()
    if not binary:
        return {"state": "missing",
                "detail": "the agy CLI is not installed or not on PATH"}
    return {"state": "unknown", "detail": _NO_AUTH_VERB_DETAIL}


def catalogue(*, capture=None, timeout=30) -> tuple[list[dict], list[str]]:
    """Discover models from `agy models`, folding effort rungs into ladders."""
    binary = _resolve_binary()
    if not binary:
        return [], [f"{PROVIDER_NAME} is not installed; no models could be "
                    f"listed."]
    try:
        rc, out, err = _invoke(capture, [binary, "models"], timeout=timeout)
    except Exception as exc:
        return [], [f"agy models failed: {exc}"]
    if rc not in (0, None):
        detail = (err or out).strip().splitlines()
        return [], ["agy models exited "
                    f"{rc}" + (f": {detail[-1]}" if detail else "")]
    models = _parse_models(out)
    raw_models = _parse_models(out)
    warnings: list[str] = []
    if not raw_models:
        warnings.append(
            "agy models produced no tab-separated rows; the CLI may be signed "
            "out or its output format may have changed."
        )
        return [], warnings
    collapsed = _collapse_models(raw_models)
    return collapsed, warnings

# ---------------------------------------------------------------------------
# argv construction
# ---------------------------------------------------------------------------

def build_argv(model, *, effort=None, system=None, stream=True) -> list[str]:
    """Full argv for one print-mode turn. argv[0] is the bare binary name.

    No ``-p``/``--print``/``--prompt`` flag is emitted: on agy that flag takes
    the prompt as its VALUE, so emitting it would make agy swallow the next
    flag as the prompt. Omitting it puts agy in print mode reading the prompt
    from stdin, which is where run_turn always writes it.

    ``system`` is accepted for API symmetry and intentionally emits nothing —
    agy has no system-prompt flag. Callers must fold the system text into the
    prompt via ``render_prompt(messages, system=...)``; run_turn does.
    """
    # Resolve the model to a native row id, using effort if provided
    native_row = _resolve_native_row(model, effort)
    validated_model = _validate_model(native_row)
    validated_effort = _validate_effort(effort)
    if system is not None and not isinstance(system, str):
        raise AgyCliAgentError(
            "system must be a string or None; note that agy has no "
            "system-prompt flag, so it is rendered into the stdin prompt"
        )

    argv: list[str] = [BINARY_NAMES[0]]
    argv += ["--output-format", "stream-json" if stream else "text"]
    argv += list(READ_ONLY_FLAGS)
    argv += ["--model", validated_model]
    if validated_effort:
        argv += ["--effort", validated_effort]
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
        raise AgyCliAgentError("request messages must be a list")
    normalized: list[dict] = []
    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            raise AgyCliAgentError(f"message {index} must be an object")
        role = message.get("role")
        if role not in _ROLES:
            raise AgyCliAgentError(
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
        raise AgyCliAgentError("request contains no user message to answer")
    return normalized


def render_prompt(messages, *, system=None) -> str:
    """Render the whole stateless conversation into one stdin prompt.

    Order is system, then user/assistant turns in the order given. A single
    bare user turn with no system text is passed through verbatim so the common
    case pays no framing overhead. agy needs the system text here because it has
    no system-prompt flag (SYSTEM_PROMPT_TRANSPORT == "prompt").
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
        self.result_text: str | None = None
        self.stop_reason: str | None = None
        self.failure: str | None = None
        self.saw_tool_step = False
        self.tool_error: str | None = None
        self.denied_actions: list = []
        self.raw_lines: list[str] = []
        self.stderr_handle = None

    def fallback_text(self) -> str:
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

    def denial_detail(self) -> str:
        names = sorted({
            str((action or {}).get("display_name")
                or (action or {}).get("action") or "action")
            for action in self.denied_actions
            if isinstance(action, dict)
        })
        label = ", ".join(names) if names else "a tool call"
        detail = f"agy produced no output because {label} was denied under the "
        detail += ("read-only posture (--sandbox --mode plan). This route "
                   "strips tools by design and never auto-approves; rephrase "
                   "the request as plain text.")
        if self.tool_error:
            detail += " | cli: " + self.tool_error[:_MAX_STDERR_CHARS]
        return detail


def _translate(payload: Any, state: _TurnState) -> list[dict]:
    """Convert one NDJSON object from agy into zero or more route events."""
    if not isinstance(payload, dict):
        return []
    kind = payload.get("event")

    if kind == "step_update":
        step = payload.get("step_update")
        if not isinstance(step, dict):
            return []
        step_type = step.get("step_type")
        if step_type == "tool":
            # Tools are stripped by policy, so tool steps never become route
            # text. They are recorded because a denied tool call that leaves
            # the turn empty must be reported as a failure, not a blank reply.
            state.saw_tool_step = True
            if str(step.get("state") or "").upper() == "ERROR":
                info = step.get("tool_info")
                error = info.get("error") if isinstance(info, dict) else None
                message = error.get("message") if isinstance(error, dict) else None
                if isinstance(message, str) and message:
                    state.tool_error = message
            return []
        if step_type != "agent_response":
            return []
        events: list[dict] = []
        # Deltas arrive on both ACTIVE and DONE step_update rows, so state is
        # not filtered on; concatenating every text_delta reproduces
        # result.response exactly (verified live).
        text = step.get("text_delta")
        if isinstance(text, str) and text:
            state.emitted_text = True
            events.append({"type": "text_delta", "text": text})
        thinking = step.get("thinking_delta")
        if isinstance(thinking, str) and thinking:
            events.append({"type": "thinking_delta", "text": thinking})
        return events

    if kind == "result":
        result = payload.get("result")
        if not isinstance(result, dict):
            return []
        status = str(result.get("status") or "").upper()
        response = result.get("response")
        if isinstance(response, str) and response:
            state.result_text = response
        denied = result.get("denied_actions")
        if isinstance(denied, list) and denied:
            state.denied_actions = denied
        error = result.get("error")
        if status == "ERROR" or (isinstance(error, str) and error):
            state.failure = ("agy reported "
                             f"{status or 'an error'}: "
                             f"{error or response or 'no detail'}")
        elif status and status != "SUCCESS":
            state.stop_reason = status.casefold()
        else:
            state.stop_reason = "end_turn"
        return []

    # "init" and anything unrecognized: informational, never route output.
    return []


def _write_prompt(session, prompt: str) -> None:
    """Write RAW prompt text, then signal EOF.

    agy reads a plain-text prompt from stdin under --input-format text and ends
    the turn at EOF, so both the write and the close are load-bearing. A
    JSON-oriented .send() is the wrong shape here and is used only as a last
    resort for a session host that exposes no raw writer.
    """
    payload = prompt if prompt.endswith("\n") else prompt + "\n"
    for name in ("send_text", "write_text", "write"):
        writer = getattr(session, name, None)
        if callable(writer):
            writer(payload)
            break
    else:
        session.send(prompt)
    for name in ("close_stdin", "end_input", "close_write"):
        closer = getattr(session, name, None)
        if callable(closer):
            closer()
            return
    stdin = getattr(session, "stdin", None)
    close = getattr(stdin, "close", None)
    if callable(close):
        try:
            close()
        except OSError:
            pass


def _iter_events(session, *, timeout) -> Iterator[tuple[str, Any]]:
    """Yield ("json", obj) or ("raw", line) for each stdout line.

    Tolerates a session host that yields either parsed dicts or raw strings, and
    never lets a non-JSON line abort the turn: agy does print plain diagnostics
    (for example its tool-denial explanation), which are buffered for error
    reporting instead of being mistaken for events.
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


def _describe(exc: BaseException, state: _TurnState | None = None,
              timeout=None) -> str:
    text = str(exc).strip() or exc.__class__.__name__
    lowered = text.casefold()
    if isinstance(exc, subprocess.TimeoutExpired) or "timed out" in lowered \
            or "timeout" in lowered:
        limit = f" after {timeout}s" if timeout else ""
        text = f"the agy CLI did not finish{limit}"
    elif isinstance(exc, CliSessionError):
        text = f"agy CLI session error: {text}"
    else:
        text = f"agy CLI turn failed: {text}"
    extra = ""
    if state is not None:
        extra = state.diagnostics() + state.stderr_tail()
    return text + extra


def run_turn(request, *, spawner=None, timeout=300) -> Iterator[dict]:
    """Stream one stateless turn as text_delta / thinking_delta / message_stop.

    Always terminates with exactly one message_stop or error event, and never
    raises: every failure mode (missing binary, bad request, ERROR result, a
    denied tool call that left the turn empty, non-zero exit, timeout,
    unparsable stream) degrades to an error event.
    """
    state = _TurnState()
    try:
        if not isinstance(request, dict):
            raise AgyCliAgentError("request must be an object")
        model = _validate_model(request.get("model"))
        messages = _coerce_messages(request.get("messages"))
        effort = _validate_effort(request.get("effort"))
        system = request.get("system")
        if system is not None and not isinstance(system, str):
            raise AgyCliAgentError("system must be a string or None")
        max_tokens = request.get("max_tokens")
        if max_tokens is not None and not isinstance(max_tokens, int):
            raise AgyCliAgentError("max_tokens must be an integer or None")
        if isinstance(max_tokens, int) and max_tokens <= 0:
            raise AgyCliAgentError("max_tokens must be positive")

        # agy has no system-prompt flag, so the system text is folded into the
        # prompt here and deliberately NOT passed to build_argv.
        prompt = render_prompt(messages, system=system)
        argv = build_argv(model, effort=effort, system=None, stream=True)

        binary = _resolve_binary()
        if not binary:
            raise AgyCliAgentError(
                "the agy CLI was not found on PATH; install AntiGravity or "
                "switch this route to API-key credentials"
            )
        argv[0] = binary

        # stderr goes to a temp file, not a pipe: a pipe nobody drains can fill
        # and deadlock the child, and agy's diagnostics there are worth keeping.
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
        if state.failure:
            yield {"type": "error", "message": state.failure}
            return
        if not state.emitted_text and not state.fallback_text():
            # Empty stream with clean exit and no tool activity -> no output
            if not state.denied_actions and not state.tool_error and not state.saw_tool_step:
                yield {"type": "error", "message": "the agy CLI produced no output"}
                return
            # SUCCESS with an empty response is what agy reports after a
            # headless tool call is auto-denied. Nothing executed, which is the
            # posture working, but an empty turn is not a usable completion.
            if state.denied_actions or state.tool_error or state.saw_tool_step:
                yield {"type": "error", "message": state.denial_detail()}
                return
        if isinstance(returncode, int) and returncode != 0:
            detail = f"the agy CLI exited with code {returncode}"
            if not state.emitted_text and not state.fallback_text():
                yield {"type": "error",
                       "message": detail + state.diagnostics()
                                  + state.stderr_tail()}
                return
            stop_reason = state.stop_reason or "error"
        else:
            if not state.emitted_text:
                fallback = state.fallback_text()
                if fallback:
                    state.emitted_text = True
                    yield {"type": "text_delta", "text": fallback}
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
