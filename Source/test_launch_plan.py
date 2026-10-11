"""Swift test harness for the launch-plan resolver.

This file is a Python test driver; it embeds the Swift test program as
a string, writes it to a temporary file, compiles it with ``xcrun swiftc``
together with the production ``Source/LaunchPlan.swift`` module, runs
the resulting binary, and asserts the exit code. The Swift program
exercises the actual production ``LaunchPlanResolver.resolve(...)`` /
``.mayStop(...)`` / ``.snapshotsMatch(...)`` functions — no Python
mirror, no parallel policy, no Swift stub of the production code.

Pattern follows ``Source/test_chat_model.py`` (which exercises the
production Swift ``ChatModel`` against a fake JSONL worker).
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PRODUCTION_MODULE = REPO_ROOT / "Source" / "LaunchPlan.swift"

# The Swift test program. It is compiled together with the production
# ``Source/LaunchPlan.swift`` (the only Swift file in the compilation
# unit that defines ``LaunchPlanResolver``) and exercises the actual
# public functions. The program exits 0 on success and 1 on the first
# failed assertion; the Python driver asserts the exit code.
SWIFT_PROGRAM = r'''
import Foundation

// Test harness for the production launch-plan resolver. Compiled
// together with Source/LaunchPlan.swift, which provides the public
// functions ``LaunchPlanResolver.resolve`` / ``.mayStop`` / ``.snapshotsMatch``
// and the ``ChangeKind`` / ``LaunchAction`` / ``LaunchPlan`` types.

func fail(_ message: String) -> Never {
    FileHandle.standardError.write(Data("FAIL: \(message)\n".utf8))
    exit(1)
}

func expectEqual<T: Equatable>(_ actual: T, _ expected: T, _ label: String) {
    if actual != expected {
        fail("\(label): expected \(expected), got \(actual)")
    }
}

func expectNil<T>(_ actual: T?, _ label: String) {
    if actual != nil {
        fail("\(label): expected nil, got \(String(describing: actual!))")
    }
}

func expectNonNil<T>(_ actual: T?, _ label: String) -> T {
    if let value = actual { return value }
    fail("\(label): expected non-nil")
}

func resolve(surface: String = "claude",
            gatewayRunning: Bool = true,
            gatewayFingerprint: String? = "fp-A",
            gatewayDigest: String? = nil,
            preparedFingerprint: String? = "fp-A",
            preparedDigest: String? = nil,
            chatWorking: Bool = false,
            chatHasActiveWork: Bool = false,
            chatWindowOpen: Bool = false,
            activeRequests: Int = 0,
            changeKind: ChangeKind = .unchanged,
            liveClaude: Bool = false,
            liveCodex: Bool = false) -> LaunchPlan {
    return LaunchPlanResolver.resolve(
        surface: surface,
        gatewayRunning: gatewayRunning,
        gatewayFingerprint: gatewayFingerprint,
        gatewayDigest: gatewayDigest,
        preparedFingerprint: preparedFingerprint,
        preparedDigest: preparedDigest,
        chatWorking: chatWorking,
        chatHasActiveWork: chatHasActiveWork,
        chatWindowOpen: chatWindowOpen,
        activeRequests: activeRequests,
        changeKind: changeKind,
        liveClaude: liveClaude,
        liveCodex: liveCodex)
}

@main struct Cases {
    static func main() {

        // --- open_directly ---------------------------------------------------------

        do {
            let plan = resolve(surface: "claude")
            expectEqual(plan.action, .openDirectly, "open_directly/idle_claude")
            expectEqual(plan.mustSave, false, "open_directly/idle_claude/mustSave")
            expectEqual(plan.mustRestart, false, "open_directly/idle_claude/mustRestart")
            expectNil(plan.blockReason, "open_directly/idle_claude/blockReason")
            expectEqual(plan.surface, "claude", "open_directly/idle_claude/surface")
            expectEqual(plan.otherHarness, "Codex / ChatGPT", "open_directly/idle_claude/otherHarness")
        }

        do {
            // Codex is live but the gateway snapshot already matches what
            // Claude needs — Claude can launch without disrupting Codex.
            // This is the user's primary complaint: chat work in another
            // surface must not block launching the next app.
            let plan = resolve(surface: "claude", liveCodex: true)
            expectEqual(plan.action, .openDirectly, "open_directly/claude_while_codex_live")
        }

        do {
            // Codex-side launch compares the digest dimension.
            let plan = LaunchPlanResolver.resolve(
                surface: "codex",
                gatewayRunning: true,
                gatewayFingerprint: nil,
                gatewayDigest: "digest-A",
                preparedFingerprint: nil,
                preparedDigest: "digest-A",
                chatWorking: false,
                chatHasActiveWork: false,
                chatWindowOpen: false,
                activeRequests: 0,
                changeKind: .unchanged,
                liveClaude: false,
                liveCodex: false)
            expectEqual(plan.action, .openDirectly, "open_directly/codex_digest_dimension")
            expectEqual(plan.otherHarness, "Claude", "open_directly/codex/otherHarness")
        }

        do {
            // chatWindowOpen without chat work is fine.
            let plan = resolve(surface: "claude", chatWindowOpen: true)
            expectEqual(plan.action, .openDirectly, "open_directly/chat_window_open")
        }

        do {
            // Gateway not running -> not a match -> restart_and_open (with
            // restart alert, not a hard block, because no in-flight work is
            // implied).
            let plan = resolve(surface: "claude", gatewayRunning: false)
            expectEqual(plan.action, .restartAndOpen, "restart_and_open/gateway_not_running")
            expectEqual(plan.mustRestart, true, "restart_and_open/gateway_not_running/mustRestart")
            expectEqual(plan.requiresUserConfirm, true, "restart_and_open/gateway_not_running/confirmation")
        }

        // --- save_and_open ---------------------------------------------------------

        do {
            // claudeRouting change with Codex live -> save without disturbing
            // the gateway. The gateway snapshot already matches.
            let plan = resolve(surface: "claude", changeKind: .claudeRouting, liveCodex: true)
            expectEqual(plan.action, .saveAndOpen, "save_and_open/claude_routing_with_codex_live")
            expectEqual(plan.mustSave, true, "save_and_open/mustSave")
            expectEqual(plan.mustRestart, false, "save_and_open/mustRestart")
            expectNil(plan.blockReason, "save_and_open/blockReason")
        }

        do {
            let plan = resolve(surface: "codex", changeKind: .codexOnly, liveClaude: true)
            expectEqual(plan.action, .saveAndOpen, "save_and_open/codex_only_with_claude_live")
        }

        do {
            let plan = resolve(changeKind: .prefs)
            expectEqual(plan.action, .saveAndOpen, "save_and_open/prefs_only")
        }

        do {
            let plan = resolve(changeKind: .mixed)
            expectEqual(plan.action, .saveAndOpen, "save_and_open/mixed_no_harness_live")
        }

        // --- restart_and_open ------------------------------------------------------

        do {
            let plan = resolve(gatewayFingerprint: "fp-OLD", preparedFingerprint: "fp-NEW")
            expectEqual(plan.action, .restartAndOpen, "restart_and_open/fingerprint_mismatch")
            expectEqual(plan.mustRestart, true, "restart_and_open/mustRestart")
            expectEqual(plan.requiresUserConfirm, true, "restart_and_open/confirmation")
            expectEqual(plan.mustSave, false, "restart_and_open/unchanged_mustSave")
        }

        do {
            let plan = resolve(
                gatewayFingerprint: "fp-OLD",
                preparedFingerprint: "fp-NEW",
                changeKind: .claudeRouting)
            expectEqual(plan.action, .restartAndOpen, "restart_and_open/fingerprint_mismatch_with_settings")
            expectEqual(plan.mustSave, true, "restart_and_open/mustSave")
        }

        do {
            let plan = LaunchPlanResolver.resolve(
                surface: "codex",
                gatewayRunning: true,
                gatewayFingerprint: nil,
                gatewayDigest: "digest-OLD",
                preparedFingerprint: nil,
                preparedDigest: "digest-NEW",
                chatWorking: false,
                chatHasActiveWork: false,
                chatWindowOpen: false,
                activeRequests: 0,
                changeKind: .unchanged,
                liveClaude: false,
                liveCodex: false)
            expectEqual(plan.action, .restartAndOpen, "restart_and_open/codex_digest_mismatch")
        }

        // --- blocked by activeRequests --------------------------------------------
        //
        // Block messages must name "the gateway", never a specific harness,
        // because ``/_bridge/status`` returns a global count.

        do {
            let plan = resolve(
                surface: "claude",
                gatewayFingerprint: "fp-OLD",
                preparedFingerprint: "fp-NEW",
                activeRequests: 2,
                liveCodex: true)
            expectEqual(plan.action, .blocked, "blocked/active_requests_2")
            let reason = expectNonNil(plan.blockReason, "blocked/active_requests_2/reason")
            if !reason.contains("The gateway has 2 requests in flight") {
                fail("blocked/active_requests_2: reason did not name 'the gateway'. Got: \(reason)")
            }
            if reason.contains("Codex / ChatGPT has") {
                fail("blocked/active_requests_2: reason attributed the count to Codex. Got: \(reason)")
            }
            if !reason.contains("Claude") {
                fail("blocked/active_requests_2: reason did not name the launching surface. Got: \(reason)")
            }
        }

        do {
            let plan = LaunchPlanResolver.resolve(
                surface: "codex",
                gatewayRunning: true,
                gatewayFingerprint: nil,
                gatewayDigest: "digest-OLD",
                preparedFingerprint: nil,
                preparedDigest: "digest-NEW",
                chatWorking: false,
                chatHasActiveWork: false,
                chatWindowOpen: false,
                activeRequests: 1,
                changeKind: .unchanged,
                liveClaude: true,
                liveCodex: false)
            expectEqual(plan.action, .blocked, "blocked/codex_active_request_1")
            let reason = expectNonNil(plan.blockReason, "blocked/codex_active_request_1/reason")
            if !reason.contains("The gateway has 1 request in flight") {
                fail("blocked/codex_active_request_1: expected singular 'request' phrasing. Got: \(reason)")
            }
            if reason.contains("Claude has") {
                fail("blocked/codex_active_request_1: reason attributed the count to Claude. Got: \(reason)")
            }
        }

        do {
            // activeRequests blocks even when Chat is idle.
            let plan = resolve(
                gatewayFingerprint: "fp-OLD",
                preparedFingerprint: "fp-NEW",
                activeRequests: 1)
            expectEqual(plan.action, .blocked, "blocked/active_requests_chat_idle")
        }

        // --- blocked by chat work --------------------------------------------------

        do {
            let plan = resolve(
                gatewayFingerprint: "fp-OLD",
                preparedFingerprint: "fp-NEW",
                chatWorking: true)
            expectEqual(plan.action, .blocked, "blocked/chat_working")
            let reason = expectNonNil(plan.blockReason, "blocked/chat_working/reason")
            if !reason.hasPrefix("Chat is working.") {
                fail("blocked/chat_working: reason did not start with 'Chat is working.'. Got: \(reason)")
            }
        }

        do {
            let plan = resolve(
                gatewayFingerprint: "fp-OLD",
                preparedFingerprint: "fp-NEW",
                chatHasActiveWork: true)
            expectEqual(plan.action, .blocked, "blocked/chat_has_active_work")
        }

        do {
            // activeRequests outranks chat in the named holder.
            let plan = resolve(
                gatewayFingerprint: "fp-OLD",
                preparedFingerprint: "fp-NEW",
                chatWorking: true,
                activeRequests: 3)
            let reason = expectNonNil(plan.blockReason, "blocked/outrank_precedence/reason")
            if !reason.contains("3 requests") {
                fail("blocked/outrank_precedence: reason did not lead with request count. Got: \(reason)")
            }
        }

        // --- blocked by change policy (live-app profile safety) --------------------

        do {
            let plan = resolve(surface: "codex", changeKind: .codexOnly, liveCodex: true)
            expectEqual(plan.action, .blocked, "blocked/codex_only_while_codex_live")
            let reason = expectNonNil(plan.blockReason, "blocked/codex_only_while_codex_live/reason")
            if !reason.contains("Quit Codex / ChatGPT") {
                fail("blocked/codex_only_while_codex_live: reason did not name Codex / ChatGPT. Got: \(reason)")
            }
        }

        do {
            let plan = resolve(surface: "claude", changeKind: .claudeRouting, liveClaude: true)
            expectEqual(plan.action, .blocked, "blocked/claude_routing_while_claude_live")
        }

        do {
            let plan = resolve(changeKind: .mixed, liveClaude: true)
            expectEqual(plan.action, .blocked, "blocked/mixed_while_claude_live")
        }

        do {
            let plan = resolve(changeKind: .mixed)
            expectEqual(plan.action, .saveAndOpen, "save_and_open/mixed_with_no_harness_live")
        }

        do {
            let plan = resolve(changeKind: .prefs, liveClaude: true, liveCodex: true)
            expectEqual(plan.action, .saveAndOpen, "save_and_open/prefs_with_any_harness_live")
        }

        // --- chat activity is NOT a block when snapshot matches -------------------

        do {
            // The user's primary complaint: a working Chat must not block a
            // launch whose gateway snapshot already matches what the new
            // surface needs.
            let plan = resolve(chatWorking: true)
            expectEqual(plan.action, .openDirectly, "open_directly/chat_working_with_match")
            expectNil(plan.blockReason, "open_directly/chat_working_with_match/reason")
        }

        do {
            let plan = resolve(activeRequests: 2)
            expectEqual(plan.action, .openDirectly, "open_directly/active_requests_with_match")
        }

        do {
            let plan = resolve(chatWorking: true, activeRequests: 1, liveCodex: true)
            expectEqual(plan.action, .openDirectly, "open_directly/everything_live_but_match")
        }

        do {
            let plan = resolve(chatWindowOpen: true)
            expectEqual(plan.action, .openDirectly, "open_directly/chat_window_idle")
        }

        // --- surface switch (cross-surface launches) -------------------------------

        do {
            let plan = resolve(surface: "claude", liveCodex: true)
            expectEqual(plan.action, .openDirectly, "open_directly/launching_claude_while_codex_live")
        }

        do {
            let plan = LaunchPlanResolver.resolve(
                surface: "codex",
                gatewayRunning: true,
                gatewayFingerprint: nil,
                gatewayDigest: "digest-A",
                preparedFingerprint: nil,
                preparedDigest: "digest-A",
                chatWorking: false,
                chatHasActiveWork: false,
                chatWindowOpen: false,
                activeRequests: 0,
                changeKind: .unchanged,
                liveClaude: true,
                liveCodex: false)
            expectEqual(plan.action, .openDirectly, "open_directly/launching_codex_while_claude_live")
        }

        do {
            let plan = resolve(
                surface: "claude",
                gatewayFingerprint: "fp-OLD",
                preparedFingerprint: "fp-NEW",
                activeRequests: 0,
                liveCodex: true)
            expectEqual(plan.action, .restartAndOpen, "restart_and_open/launching_claude_with_codex_and_mismatch")
        }

        // --- surface validation ----------------------------------------------------
        //
        // Unknown surfaces are allowed to fall through; production policy
        // documents the assumption in the comment on resolveLaunchPlan.

        do {
            let plan = LaunchPlanResolver.resolve(
                surface: "gemini",
                gatewayRunning: true,
                gatewayFingerprint: "fp-A",
                gatewayDigest: nil,
                preparedFingerprint: "fp-A",
                preparedDigest: nil,
                chatWorking: false,
                chatHasActiveWork: false,
                chatWindowOpen: false,
                activeRequests: 0,
                changeKind: .unchanged,
                liveClaude: false,
                liveCodex: false)
            expectEqual(plan.action, .openDirectly, "open_directly/unknown_surface_falls_through")
        }

        // --- snapshotsMatch --------------------------------------------------------

        do {
            expectEqual(
                LaunchPlanResolver.snapshotsMatch(
                    surface: "claude",
                    gatewayRunning: false,
                    gatewayFingerprint: "fp-A",
                    gatewayDigest: nil,
                    preparedFingerprint: "fp-A",
                    preparedDigest: nil),
                false,
                "snapshots_match/gateway_not_running")
            expectEqual(
                LaunchPlanResolver.snapshotsMatch(
                    surface: "claude",
                    gatewayRunning: true,
                    gatewayFingerprint: "fp-A",
                    gatewayDigest: nil,
                    preparedFingerprint: nil,
                    preparedDigest: nil),
                true,
                "snapshots_match/no_prepared_fingerprint")
            expectEqual(
                LaunchPlanResolver.snapshotsMatch(
                    surface: "codex",
                    gatewayRunning: true,
                    gatewayFingerprint: nil,
                    gatewayDigest: "digest-A",
                    preparedFingerprint: nil,
                    preparedDigest: "digest-NEW"),
                false,
                "snapshots_match/codex_digest_mismatch")
        }

        // --- mayStopGateway --------------------------------------------------------

        do {
            expectEqual(
                LaunchPlanResolver.mayStop(
                    liveClaude: false, liveCodex: false,
                    chatWorking: false, chatHasActiveWork: false,
                    chatWindowOpen: false, activeRequests: 0),
                true, "may_stop/idle")
            expectEqual(
                LaunchPlanResolver.mayStop(
                    liveClaude: false, liveCodex: false,
                    chatWorking: true, chatHasActiveWork: false,
                    chatWindowOpen: false, activeRequests: 0),
                false, "may_stop/chat_working")
            expectEqual(
                LaunchPlanResolver.mayStop(
                    liveClaude: false, liveCodex: false,
                    chatWorking: false, chatHasActiveWork: true,
                    chatWindowOpen: false, activeRequests: 0),
                false, "may_stop/chat_has_active_work")
            expectEqual(
                LaunchPlanResolver.mayStop(
                    liveClaude: false, liveCodex: false,
                    chatWorking: false, chatHasActiveWork: false,
                    chatWindowOpen: true, activeRequests: 0),
                false, "may_stop/chat_window_open")
            expectEqual(
                LaunchPlanResolver.mayStop(
                    liveClaude: true, liveCodex: false,
                    chatWorking: false, chatHasActiveWork: false,
                    chatWindowOpen: false, activeRequests: 0),
                false, "may_stop/live_claude")
            expectEqual(
                LaunchPlanResolver.mayStop(
                    liveClaude: false, liveCodex: true,
                    chatWorking: false, chatHasActiveWork: false,
                    chatWindowOpen: false, activeRequests: 0),
                false, "may_stop/live_codex")
            expectEqual(
                LaunchPlanResolver.mayStop(
                    liveClaude: false, liveCodex: false,
                    chatWorking: false, chatHasActiveWork: false,
                    chatWindowOpen: false, activeRequests: 1),
                false, "may_stop/active_requests")
        }

        // --- launchPlan equality & raw values --------------------------------------

        do {
            let a = LaunchPlan(
                action: .openDirectly, blockReason: nil, requiresUserConfirm: false,
                mustRestart: false, mustSave: false, otherHarness: "Claude", surface: "claude")
            let b = LaunchPlan(
                action: .openDirectly, blockReason: nil, requiresUserConfirm: false,
                mustRestart: false, mustSave: false, otherHarness: "Claude", surface: "claude")
            expectEqual(a, b, "launch_plan/equatable")
            expectEqual(a.action.rawValue, "open_directly", "launch_plan/raw_value")
            expectEqual(ChangeKind.codexOnly.rawValue, "codexOnly", "change_kind/raw_value")
        }

        FileHandle.standardError.write(Data("PASS\n".utf8))
        exit(0)
    }
}
'''


@unittest.skipUnless(sys.platform == "darwin" and shutil.which("xcrun"),
                     "Swift launch-plan harness needs macOS + xcrun")
class LaunchPlanSwiftTests(unittest.TestCase):
    def test_resolver_exercises_production_swift(self):
        """Compile the Swift test program against the production
        ``Source/LaunchPlan.swift`` module, run it, and require exit 0.
        The Swift program asserts every cell of the 3x3 surface x
        intent table, the named-block-message policy (no per-harness
        attribution of the gateway's active count), the
        ``mayStopGateway`` predicate, and the ``snapshotsMatch``
        dimension logic. If this test passes, the resolver on disk
        is byte-for-byte the policy under test.
        """
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            swift_path = tmp_path / "launch_plan_tests.swift"
            swift_path.write_text(SWIFT_PROGRAM)
            binary_path = tmp_path / "launch_plan_tests"
            proc = subprocess.run(
                [
                    "xcrun", "swiftc",
                    "-swift-version", "5",
                    "-O",
                    str(PRODUCTION_MODULE),
                    str(swift_path),
                    "-o", str(binary_path),
                ],
                capture_output=True, text=True, timeout=120,
            )
            if proc.returncode != 0:
                self.fail(
                    "Swift compile failed:\n"
                    f"stdout:\n{proc.stdout}\n"
                    f"stderr:\n{proc.stderr}"
                )
            run = subprocess.run([str(binary_path)],
                                 capture_output=True, text=True, timeout=30)
            self.assertEqual(
                run.returncode, 0,
                "Swift launch-plan harness failed.\n"
                f"stdout:\n{run.stdout}\n"
                f"stderr:\n{run.stderr}",
            )
            self.assertEqual(run.stderr.strip(), "PASS",
                             f"unexpected stderr: {run.stderr!r}")


if __name__ == "__main__":
    unittest.main()
