"""Tests for the launch-plan resolver.

These cover the 3×3 surface × intent table the user complained about, plus
the named-block-message policy the resolver promises. Every assertion is
pure — no I/O, no globals, no shared state — so the table can be regenerated
or extended without touching production code.
"""
from __future__ import annotations

import unittest

from launch_plan import (BLOCKED, CHANGE_CLAUDE_ROUTING, CHANGE_CODEX_ONLY,
                         CHANGE_MIXED, CHANGE_PREFS, CHANGE_UNCHANGED, CLAUDE,
                         CODEX, OPEN_DIRECTLY, RESTART_AND_OPEN, SAVE_AND_OPEN,
                         LaunchPlan, may_stop_gateway, resolve_launch_plan)


def base(**overrides):
    """A default state where launching should succeed without a restart."""
    state = dict(
        surface=CLAUDE,
        gateway_running=True,
        gateway_fingerprint="fp-A",
        gateway_digest=None,
        prepared_fingerprint="fp-A",
        prepared_digest=None,
        chat_working=False,
        chat_window_open=False,
        chat_has_active_work=False,
        active_requests=0,
        change_kind=CHANGE_UNCHANGED,
        live_claude=False,
        live_codex=False,
    )
    state.update(overrides)
    return state


class OpenDirectlyTests(unittest.TestCase):
    """The gateway is already running with the right snapshot."""

    def test_no_settings_changed_no_other_harness_opens_directly(self):
        plan = resolve_launch_plan(**base())
        self.assertEqual(plan.action, OPEN_DIRECTLY)
        self.assertFalse(plan.must_save)
        self.assertFalse(plan.must_restart)
        self.assertFalse(plan.requires_user_confirm)
        self.assertIsNone(plan.block_reason)

    def test_chat_window_open_does_not_block_when_snapshot_matches(self):
        # The user can have a Chat window open but idle; the launch only
        # cares whether the gateway snapshot matches.
        plan = resolve_launch_plan(**base(chat_window_open=True))
        self.assertEqual(plan.action, OPEN_DIRECTLY)

    def test_other_desktop_harness_running_does_not_block(self):
        # Codex is live but the gateway snapshot already matches what
        # Claude needs — Claude can launch without disrupting Codex.
        plan = resolve_launch_plan(**base(
            surface=CLAUDE,
            live_codex=True,
        ))
        self.assertEqual(plan.action, OPEN_DIRECTLY)

    def test_codex_launch_uses_digest_dimension(self):
        plan = resolve_launch_plan(**base(
            surface=CODEX,
            gateway_digest="digest-X",
            prepared_digest="digest-X",
            prepared_fingerprint=None,
            gateway_fingerprint=None,
        ))
        self.assertEqual(plan.action, OPEN_DIRECTLY)

    def test_gateway_not_running_is_not_a_match(self):
        # First launch — no gateway yet — the launcher must start it.
        plan = resolve_launch_plan(**base(gateway_running=False))
        self.assertEqual(plan.action, RESTART_AND_OPEN)
        self.assertTrue(plan.must_restart)
        self.assertTrue(plan.requires_user_confirm)


class SaveAndOpenTests(unittest.TestCase):
    """Settings changed; gateway snapshot already matches the prepared plan."""

    def test_claude_routing_change_with_codex_live_saves_without_restart(self):
        plan = resolve_launch_plan(**base(
            surface=CLAUDE,
            change_kind=CHANGE_CLAUDE_ROUTING,
            live_codex=True,
        ))
        self.assertEqual(plan.action, SAVE_AND_OPEN)
        self.assertTrue(plan.must_save)
        self.assertFalse(plan.must_restart)
        self.assertIsNone(plan.block_reason)

    def test_prefs_change_is_save_and_open(self):
        plan = resolve_launch_plan(**base(change_kind=CHANGE_PREFS))
        self.assertEqual(plan.action, SAVE_AND_OPEN)
        self.assertTrue(plan.must_save)

    def test_codex_only_change_with_claude_live_saves_without_restart(self):
        plan = resolve_launch_plan(**base(
            surface=CODEX,
            change_kind=CHANGE_CODEX_ONLY,
            live_claude=True,
        ))
        self.assertEqual(plan.action, SAVE_AND_OPEN)

    def test_mixed_change_with_no_harness_live_saves_without_restart(self):
        plan = resolve_launch_plan(**base(change_kind=CHANGE_MIXED))
        self.assertEqual(plan.action, SAVE_AND_OPEN)


class RestartAndOpenTests(unittest.TestCase):
    """The gateway must restart to load the prepared plan; no in-flight work."""

    def test_fingerprint_mismatch_no_in_flight_work_prompts_restart(self):
        plan = resolve_launch_plan(**base(
            prepared_fingerprint="fp-NEW",
            gateway_fingerprint="fp-OLD",
        ))
        self.assertEqual(plan.action, RESTART_AND_OPEN)
        self.assertTrue(plan.must_restart)
        self.assertTrue(plan.requires_user_confirm)
        # Unchanged settings — no save needed; the restart alone picks up
        # the prepared selection. (A separate test covers save+restart.)
        self.assertFalse(plan.must_save)

    def test_fingerprint_mismatch_with_settings_changed_also_prompts_restart(self):
        plan = resolve_launch_plan(**base(
            change_kind=CHANGE_CLAUDE_ROUTING,
            prepared_fingerprint="fp-NEW",
            gateway_fingerprint="fp-OLD",
        ))
        self.assertEqual(plan.action, RESTART_AND_OPEN)
        self.assertTrue(plan.must_restart)
        self.assertTrue(plan.must_save)

    def test_codex_digest_mismatch_prompts_restart(self):
        plan = resolve_launch_plan(**base(
            surface=CODEX,
            prepared_digest="digest-NEW",
            gateway_digest="digest-OLD",
        ))
        self.assertEqual(plan.action, RESTART_AND_OPEN)

    def test_no_other_harness_keeps_a_calm_alert_text(self):
        plan = resolve_launch_plan(**base(
            prepared_fingerprint="fp-NEW",
            gateway_fingerprint="fp-OLD",
        ))
        # The notes field carries the orchestrator's UI string.
        self.assertTrue(any("Restart" in note for note in plan.notes))

    def test_other_harness_connected_adds_a_reconnect_note(self):
        plan = resolve_launch_plan(**base(
            prepared_fingerprint="fp-NEW",
            gateway_fingerprint="fp-OLD",
            live_codex=True,
        ))
        self.assertTrue(any("reconnect" in note.lower() for note in plan.notes))


class BlockedByActiveRequestsTests(unittest.TestCase):
    """In-flight requests block the gateway restart with a named message."""

    def test_codex_running_blocks_claude_with_named_count(self):
        plan = resolve_launch_plan(**base(
            surface=CLAUDE,
            live_codex=True,
            active_requests=2,
            prepared_fingerprint="fp-NEW",
            gateway_fingerprint="fp-OLD",
        ))
        self.assertEqual(plan.action, BLOCKED)
        self.assertIsNotNone(plan.block_reason)
        self.assertIn("Codex / ChatGPT", plan.block_reason)
        self.assertIn("2 requests", plan.block_reason)
        self.assertIn("Claude", plan.block_reason)

    def test_claude_running_blocks_codex_with_named_count(self):
        plan = resolve_launch_plan(**base(
            surface=CODEX,
            live_claude=True,
            active_requests=1,
            prepared_digest="digest-NEW",
            gateway_digest="digest-OLD",
        ))
        self.assertEqual(plan.action, BLOCKED)
        self.assertIn("1 request in flight", plan.block_reason)
        self.assertIn("Codex", plan.block_reason)

    def test_active_requests_block_even_when_chat_idle(self):
        plan = resolve_launch_plan(**base(
            active_requests=1,
            prepared_fingerprint="fp-NEW",
            gateway_fingerprint="fp-OLD",
        ))
        self.assertEqual(plan.action, BLOCKED)
        self.assertIsNotNone(plan.block_reason)


class BlockedByChatWorkTests(unittest.TestCase):
    """A running Chat turn blocks the gateway restart with a named message."""

    def test_chat_working_blocks_with_named_message(self):
        plan = resolve_launch_plan(**base(
            chat_working=True,
            prepared_fingerprint="fp-NEW",
            gateway_fingerprint="fp-OLD",
        ))
        self.assertEqual(plan.action, BLOCKED)
        self.assertEqual(plan.block_reason,
                         "Chat is working. Stop the Chat turn, then launch again so the "
                         "gateway can load the Claude selection.")

    def test_chat_has_active_work_also_blocks(self):
        # The resolver accepts either of the two Chat signals — the Swift
        # side passes whichever it has at the call site.
        plan = resolve_launch_plan(**base(
            chat_has_active_work=True,
            prepared_fingerprint="fp-NEW",
            gateway_fingerprint="fp-OLD",
        ))
        self.assertEqual(plan.action, BLOCKED)
        self.assertIn("Chat is working", plan.block_reason)

    def test_active_requests_outrank_chat_message(self):
        # Both signals true; the request count is the named holder because
        # that is what blocks the restart first.
        plan = resolve_launch_plan(**base(
            chat_working=True,
            active_requests=3,
            prepared_fingerprint="fp-NEW",
            gateway_fingerprint="fp-OLD",
        ))
        self.assertIn("3 requests", plan.block_reason)


class BlockedByChangePolicyTests(unittest.TestCase):
    """Settings-vs-live-harness refusals remain hard blocks."""

    def test_codex_only_change_while_codex_live_blocks(self):
        plan = resolve_launch_plan(**base(
            surface=CODEX,
            change_kind=CHANGE_CODEX_ONLY,
            live_codex=True,
        ))
        self.assertEqual(plan.action, BLOCKED)
        self.assertIn("Quit Codex", plan.block_reason)

    def test_claude_routing_change_while_claude_live_blocks(self):
        plan = resolve_launch_plan(**base(
            surface=CLAUDE,
            change_kind=CHANGE_CLAUDE_ROUTING,
            live_claude=True,
        ))
        self.assertEqual(plan.action, BLOCKED)
        self.assertIn("Quit Claude", plan.block_reason)

    def test_mixed_change_while_any_harness_live_blocks(self):
        plan = resolve_launch_plan(**base(
            change_kind=CHANGE_MIXED,
            live_claude=True,
        ))
        self.assertEqual(plan.action, BLOCKED)

    def test_mixed_change_with_no_harness_live_does_not_block(self):
        plan = resolve_launch_plan(**base(change_kind=CHANGE_MIXED))
        self.assertNotEqual(plan.action, BLOCKED)

    def test_prefs_change_with_any_harness_live_does_not_block(self):
        plan = resolve_launch_plan(**base(
            change_kind=CHANGE_PREFS,
            live_claude=True,
            live_codex=True,
        ))
        self.assertNotEqual(plan.action, BLOCKED)


class NoRestartWhenSnapshotMatches(unittest.TestCase):
    """The user's primary complaint: a working Chat must not block a launch
    whose gateway snapshot already matches what the new surface needs."""

    def test_chat_working_with_matching_snapshot_is_open_directly(self):
        plan = resolve_launch_plan(**base(chat_working=True))
        self.assertEqual(plan.action, OPEN_DIRECTLY)
        self.assertIsNone(plan.block_reason)

    def test_active_requests_with_matching_snapshot_is_open_directly(self):
        plan = resolve_launch_plan(**base(active_requests=2))
        self.assertEqual(plan.action, OPEN_DIRECTLY)

    def test_other_harness_running_with_matching_snapshot_is_open_directly(self):
        plan = resolve_launch_plan(**base(
            live_codex=True,
            chat_working=True,
            active_requests=1,
        ))
        self.assertEqual(plan.action, OPEN_DIRECTLY)

    def test_chat_window_open_idle_with_matching_snapshot_is_open_directly(self):
        plan = resolve_launch_plan(**base(chat_window_open=True))
        self.assertEqual(plan.action, OPEN_DIRECTLY)


class SurfaceSwitchTests(unittest.TestCase):
    """Cross-surface launches (Claude while Codex is live, vice versa)."""

    def test_launching_claude_with_codex_live_matching_snapshot(self):
        plan = resolve_launch_plan(**base(
            surface=CLAUDE,
            live_codex=True,
        ))
        self.assertEqual(plan.action, OPEN_DIRECTLY)

    def test_launching_codex_with_claude_live_matching_snapshot(self):
        plan = resolve_launch_plan(**base(
            surface=CODEX,
            gateway_digest="digest-A",
            prepared_digest="digest-A",
            gateway_fingerprint=None,
            prepared_fingerprint=None,
            live_claude=True,
        ))
        self.assertEqual(plan.action, OPEN_DIRECTLY)

    def test_launching_claude_with_codex_running_and_mismatch(self):
        plan = resolve_launch_plan(**base(
            surface=CLAUDE,
            live_codex=True,
            prepared_fingerprint="fp-NEW",
            gateway_fingerprint="fp-OLD",
            active_requests=0,
        ))
        self.assertEqual(plan.action, RESTART_AND_OPEN)


class MayStopGatewayTests(unittest.TestCase):
    """Auto-stop / restore paths share the same Chat check the launch path uses."""

    def test_idle_state_may_stop(self):
        self.assertTrue(may_stop_gateway(
            live_claude=False, live_codex=False,
            chat_working=False, chat_window_open=False,
            chat_has_active_work=False, active_requests=0,
        ))

    def test_chat_working_blocks_stop(self):
        self.assertFalse(may_stop_gateway(
            live_claude=False, live_codex=False,
            chat_working=True, chat_window_open=False,
            chat_has_active_work=False, active_requests=0,
        ))

    def test_chat_has_active_work_blocks_stop(self):
        # The explicit ChatModel signal the user asked the resolver to honour.
        self.assertFalse(may_stop_gateway(
            live_claude=False, live_codex=False,
            chat_working=False, chat_window_open=False,
            chat_has_active_work=True, active_requests=0,
        ))

    def test_chat_window_open_blocks_stop(self):
        self.assertFalse(may_stop_gateway(
            live_claude=False, live_codex=False,
            chat_working=False, chat_window_open=True,
            chat_has_active_work=False, active_requests=0,
        ))

    def test_live_claude_blocks_stop(self):
        self.assertFalse(may_stop_gateway(
            live_claude=True, live_codex=False,
            chat_working=False, chat_window_open=False,
            chat_has_active_work=False, active_requests=0,
        ))

    def test_live_codex_blocks_stop(self):
        self.assertFalse(may_stop_gateway(
            live_claude=False, live_codex=True,
            chat_working=False, chat_window_open=False,
            chat_has_active_work=False, active_requests=0,
        ))

    def test_active_requests_blocks_stop(self):
        self.assertFalse(may_stop_gateway(
            live_claude=False, live_codex=False,
            chat_working=False, chat_window_open=False,
            chat_has_active_work=False, active_requests=1,
        ))


class SurfaceValidationTests(unittest.TestCase):
    def test_unknown_surface_raises(self):
        with self.assertRaises(ValueError):
            resolve_launch_plan(**base(surface="gemini"))


class DataclassTests(unittest.TestCase):
    def test_plan_is_hashable(self):
        plan = resolve_launch_plan(**base())
        self.assertEqual(plan, resolve_launch_plan(**base()))
        # The frozen dataclass should hash deterministically.
        self.assertEqual(hash(plan), hash(resolve_launch_plan(**base())))


if __name__ == "__main__":
    unittest.main()
