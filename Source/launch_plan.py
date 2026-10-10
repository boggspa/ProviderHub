"""Launch-plan resolver — a pure function the Hub asks before launching Claude
or Codex / ChatGPT.

The Hub used to refuse launching another desktop app while a run was active
in the Chat, Codex or Claude surface because every launcher routed through
``save()`` and ``ensureGatewaySnapshot()`` paths that block on ``chatWorking``
and ``activeRequests``. The block is correct when a launch genuinely needs to
restart the gateway or rewrite a live app's profile — those gates protect
in-flight work and the live app's configuration. It is wrong when the
gateway is already running with a snapshot that matches what the new launch
needs: there is no in-flight gateway traffic to protect, and Claude's 3P
profile isolation only matters on Claude's own activate.

``resolve_launch_plan`` is the policy the launcher follows: it takes the
state the Swift orchestrator already knows and returns one of four actions
the orchestrator then executes. The function is pure — no I/O, no globals —
so a unit test exercises every cell of the 3×3 surface × intent table.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


# The action names the Swift orchestrator dispatches on. Keeping them as
# strings (not an Enum) makes the IPC payload self-describing.
OPEN_DIRECTLY = "open_directly"            # gateway already matches; just activate + open
SAVE_AND_OPEN = "save_and_open"            # settings need saving; gateway already matches
RESTART_AND_OPEN = "restart_and_open"      # gateway snapshot mismatch; restart required (user confirms)
BLOCKED = "blocked"                        # launch cannot proceed; surface ``block_reason`` to the user


# Change-kind mirrors the Swift ``ChangeKind`` enum in MistralBridge.swift.
# We accept the string form so the resolver stays decoupled from the Swift
# enum's identity and the IPC payload stays JSON-shaped.
CHANGE_UNCHANGED = "unchanged"
CHANGE_PREFS = "prefs"
CHANGE_CODEX_ONLY = "codexOnly"
CHANGE_CLAUDE_ROUTING = "claudeRouting"
CHANGE_MIXED = "mixed"


# The two surfaces the Hub launches. The resolver is shared between them; the
# ``surface`` argument tells it which fingerprint/digest dimension to compare
# and which side's live-state rules apply.
CLAUDE = "claude"
CODEX = "codex"


@dataclass(frozen=True)
class LaunchPlan:
    """The Hub's decision for one launch attempt.

    ``action`` is the verb the orchestrator performs; ``block_reason`` is a
    named message that names what is holding the launch (the user complained
    that "I cannot launch" with no explanation was unhelpful); the rest are
    flags the orchestrator consumes in the order they appear.
    """
    action: str
    block_reason: Optional[str] = None
    requires_user_confirm: bool = False
    must_restart: bool = False
    must_save: bool = False
    # ``notes`` carries UI copy; it is intentionally excluded from equality
    # so two plans with the same action and reason still hash to the same
    # value (used in the resolver's own test assertions and any future
    # downstream memoisation).
    notes: list[str] = field(default_factory=list, compare=False, hash=False)


def _has_other_harness(live_claude: bool, live_codex: bool, surface: str) -> bool:
    """True when a desktop harness *other* than the one being launched is live.

    Used to name the blocker precisely ("Codex has 2 requests in flight")
    rather than generically ("the gateway is busy").
    """
    if surface == CLAUDE:
        return live_codex
    return live_claude


def _other_harness_label(surface: str) -> str:
    """A user-facing name for the other harness; the only label the resolver
    needs to produce is short and unambiguous."""
    return "Codex / ChatGPT" if surface == CLAUDE else "Claude"


def _block_reason(active_requests: int, chat_working: bool, surface: str) -> str:
    """The single named message that names what is holding the restart."""
    if active_requests > 0:
        other = _other_harness_label(surface)
        noun = "request" if active_requests == 1 else "requests"
        return (
            f"{other} has {active_requests} {noun} in flight. "
            f"Wait for them to finish, then launch again so the gateway can load "
            f"the {surface.title()} selection."
        )
    if chat_working:
        return (
            "Chat is working. Stop the Chat turn, then launch again so the "
            f"gateway can load the {surface.title()} selection."
        )
    return "The gateway needs to restart before this launch can continue."


def _snapshots_match(
    *,
    surface: str,
    gateway_running: bool,
    gateway_fingerprint: Optional[str],
    gateway_digest: Optional[str],
    prepared_fingerprint: Optional[str],
    prepared_digest: Optional[str],
) -> bool:
    """True when the live gateway already carries the snapshot the launch needs.

    The Swift side calls ``gatewayMatches(fingerprint: digest:)`` against
    ``/_bridge/status``; this function is the same comparison the orchestrator
    can do before any I/O, given the prepared values and the last known
    gateway state. ``None`` on either side means "do not compare this
    dimension"; both dims matching (or both unconstrained) means the
    snapshots are equivalent for the launch.
    """
    if not gateway_running:
        # A not-yet-started gateway has no snapshot to match. Treat as a
        # non-match so the launcher starts it cleanly.
        return False
    if surface == CLAUDE:
        if prepared_fingerprint is None:
            # The launch did not produce a fingerprint expectation — only
            # the digest side of the gateway applies, but Claude has no
            # digest. Treat the absence as a match so the orchestrator
            # activates without a restart.
            return True
        return gateway_fingerprint == prepared_fingerprint
    # surface == CODEX
    if prepared_digest is None:
        return True
    return gateway_digest == prepared_digest


def _change_blocks_launch(
    change_kind: str,
    *,
    live_claude: bool,
    live_codex: bool,
    surface: str,
) -> Optional[str]:
    """The settings-vs-live-harness rules from ``save()`` that still apply.

    These are the only blocks the resolver can authoritatively raise on the
    launcher's behalf; the Swift ``save()`` carries the rest of the policy
    (catalogue validation, key rotation). Returning a string is a hard
    refusal; returning ``None`` means the orchestrator may call ``save()``
    and rely on its own diagnostics.
    """
    if change_kind == CHANGE_UNCHANGED:
        return None
    if change_kind == CHANGE_PREFS:
        # Prefs-only never touches a live harness's profile; Swift handles
        # the write with ``saveScoped``.
        return None
    if change_kind == CHANGE_CODEX_ONLY:
        if surface == CODEX and live_codex:
            return (
                "Quit Codex / ChatGPT before changing its model catalogue or default."
            )
        # Editing Codex's catalogue while only Claude is live is fine.
        return None
    if change_kind == CHANGE_CLAUDE_ROUTING:
        if surface == CLAUDE and live_claude:
            return "Quit Claude before changing its provider settings."
        return None
    # CHANGE_MIXED: any live harness owns at least one route being changed.
    if live_claude or live_codex:
        return "Quit the desktop sessions using this gateway before changing provider settings."
    return None


def resolve_launch_plan(
    *,
    surface: str,
    gateway_running: bool,
    gateway_fingerprint: Optional[str],
    gateway_digest: Optional[str],
    prepared_fingerprint: Optional[str],
    prepared_digest: Optional[str],
    chat_working: bool,
    chat_window_open: bool,
    chat_has_active_work: bool,
    active_requests: int,
    change_kind: str,
    live_claude: bool,
    live_codex: bool,
) -> LaunchPlan:
    """Resolve the action the orchestrator takes for one launch attempt.

    Parameters mirror the Swift-side state the orchestrator already knows,
    plus the two values the worker returns from ``prepare-launch`` /
    ``codex-prepare``. The function never reads or writes disk; the
    orchestrator does the I/O around it.

    The decision tree in plain English:

    1. Settings say we are rewriting a live app's profile (``_change_blocks_launch``).
       Refuse with the named reason; no launch.
    2. Settings need saving (``change_kind != unchanged``) and the live
       gateway snapshot already matches the prepared plan. Save scoped and
       open the app — no gateway restart, no chat block.
    3. Gateway is already running and its snapshot already matches the
       prepared plan. Activate and open; no save, no restart, no chat block.
    4. Gateway needs to restart to load the prepared plan. Block only on
       in-flight requests (named) and on chat work (named); otherwise
       confirm via the existing restart alert and proceed.
    """
    if surface not in {CLAUDE, CODEX}:
        raise ValueError(f"Unknown launch surface: {surface!r}")

    notes: list[str] = []

    refusal = _change_blocks_launch(
        change_kind,
        live_claude=live_claude,
        live_codex=live_codex,
        surface=surface,
    )
    if refusal:
        return LaunchPlan(action=BLOCKED, block_reason=refusal)

    must_save = change_kind != CHANGE_UNCHANGED
    snapshots_match = _snapshots_match(
        surface=surface,
        gateway_running=gateway_running,
        gateway_fingerprint=gateway_fingerprint,
        gateway_digest=gateway_digest,
        prepared_fingerprint=prepared_fingerprint,
        prepared_digest=prepared_digest,
    )

    if snapshots_match:
        if must_save:
            notes.append("Save settings without stopping the gateway; gateway snapshot already matches.")
            return LaunchPlan(
                action=SAVE_AND_OPEN,
                must_save=True,
                must_restart=False,
                notes=notes,
            )
        notes.append("Gateway snapshot already matches; no restart needed.")
        return LaunchPlan(action=OPEN_DIRECTLY, notes=notes)

    # Gateway needs a restart to load the prepared plan. From here on, the
    # block messages name the holder so the user can see what is in the way.
    if active_requests > 0:
        return LaunchPlan(
            action=BLOCKED,
            block_reason=_block_reason(active_requests, chat_working, surface),
            must_save=must_save,
            must_restart=True,
            notes=notes,
        )
    if chat_working or chat_has_active_work:
        return LaunchPlan(
            action=BLOCKED,
            block_reason=_block_reason(active_requests, chat_working or chat_has_active_work, surface),
            must_save=must_save,
            must_restart=True,
            notes=notes,
        )

    # No in-flight work to protect; surface the existing restart alert and
    # let the user choose.
    other = _has_other_harness(live_claude, live_codex, surface)
    notes.append(
        "Restart the gateway so the launch loads its prepared selection."
        + (" Other harness is connected and may reconnect." if other else "")
    )
    return LaunchPlan(
        action=RESTART_AND_OPEN,
        requires_user_confirm=True,
        must_restart=True,
        must_save=must_save,
        notes=notes,
    )


# Convenience predicate used by restore() / auto_stop paths: the gateway may
# be torn down only when no surface (desktop harness or chat) is keeping it
# alive. ``chatWindowOpen`` is included for symmetry with the Swift
# ``anyOwnedHarnessRunning`` predicate; ``chatWorking`` and
# ``chat_has_active_work`` are kept as separate inputs so the caller can
# pass the most accurate signal it has at the call site.
def may_stop_gateway(
    *,
    live_claude: bool,
    live_codex: bool,
    chat_working: bool,
    chat_window_open: bool,
    chat_has_active_work: bool,
    active_requests: int,
) -> bool:
    """True when no surface is keeping the gateway alive.

    Mirrors the Swift ``!anyOwnedHarnessRunning && activeRequests == 0``
    predicate, but with the explicit ``chat_has_active_work`` check the user
    asked for. The Swift side can call this through ``command("may-stop-gateway")``
    so the policy lives in one place.
    """
    any_harness = live_claude or live_codex or chat_window_open or chat_working or chat_has_active_work
    return not any_harness and active_requests == 0
