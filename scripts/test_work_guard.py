"""Unit tests for work_guard.py.

Run with the uv CPython 3.13 runtime (the repo convention), or directly:
    python3 -m pytest scripts/test_work_guard.py -q
    python3 -m unittest scripts.test_work_guard

These tests are offline: they synthesize temp git repos and markers, and
never touch the real checkout or any remote.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Import the module under test by path (scripts/ is not a package).
_SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRIPTS_DIR))
import work_guard as wg  # noqa: E402


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


class TestMarkerFilenames(unittest.TestCase):
    def test_human_marker(self):
        self.assertTrue(wg.is_human_claim_marker_name(".WORK-IN-PROGRESS-fix-gateway.md"))
        self.assertTrue(wg.is_human_claim_marker_name("SHIP-HOLD-1.0.md"))
        self.assertTrue(wg.is_human_claim_marker_name("SESSION-IN-PROGRESS-test.md"))

    def test_runtime_marker_excluded_from_human(self):
        self.assertFalse(wg.is_human_claim_marker_name(
            ".WORK-IN-PROGRESS-taskwraith-runtime-seat-"
            + "a" * 64 + ".md"))

    def test_contribution_marker_excluded_from_human(self):
        self.assertFalse(wg.is_human_claim_marker_name(
            ".WORK-IN-PROGRESS-taskwraith-contribution-" + "b" * 64 + ".md"))

    def test_runtime_recognition(self):
        self.assertTrue(wg.is_runtime_marker_name(
            ".WORK-IN-PROGRESS-taskwraith-runtime-foo-" + "a" * 64 + ".md"))
        self.assertTrue(wg.is_runtime_marker_name(
            ".WORK-IN-PROGRESS-taskwraith-contribution-" + "b" * 64 + ".md"))

    def test_non_marker(self):
        self.assertFalse(wg.is_marker_file("README.md"))
        self.assertFalse(wg.is_marker_file(".gitignore"))


class TestClaimMatching(unittest.TestCase):
    def test_exact_file(self):
        m = wg._claim_to_matcher("Source/gateway.py")
        self.assertTrue(m("Source/gateway.py"))
        self.assertFalse(m("Source/gateway2.py"))

    def test_directory_with_slash(self):
        m = wg._claim_to_matcher("Source/")
        self.assertTrue(m("Source/gateway.py"))
        self.assertTrue(m("Source"))
        self.assertFalse(m("tests/gateway.py"))

    def test_bare_directory_no_slash(self):
        m = wg._claim_to_matcher("Source")
        self.assertTrue(m("Source/gateway.py"))
        self.assertTrue(m("Source"))

    def test_glob(self):
        m = wg._claim_to_matcher("Source/test_*.py")
        self.assertTrue(m("Source/test_bridge.py"))
        self.assertFalse(m("Source/gateway.py"))


class TestParseMarker(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="wg-test-")
        self.marker_path = Path(self.tmpdir, ".WORK-IN-PROGRESS-test.md")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _write_marker(self, frontmatter: str, body: str = ""):
        self.marker_path.write_text(f"---\n{frontmatter}\n---\n{body}\n")

    def test_parses_frontmatter_fields(self):
        now = datetime.now(timezone.utc)
        started = now - timedelta(minutes=5)
        self._write_marker(
            f"agent: claude\n"
            f"pid: 12345\n"
            f"started: {_iso(started)}\n"
            f"expires: {_iso(now + timedelta(minutes=10))}\n"
            f"paths:\n"
            f"  - Source/gateway.py\n"
            f"  - Source/protocol.py\n"
        )
        m = wg.parse_marker(self.tmpdir, self.marker_path.name)
        self.assertIsNotNone(m)
        self.assertEqual(m["agent"], "claude")
        self.assertEqual(m["pid"], 12345)
        self.assertIn("Source/gateway.py", m["paths"])
        self.assertIn("Source/protocol.py", m["paths"])
        self.assertIsNotNone(m["expiresMs"])

    def test_body_text_is_not_a_claim(self):
        """A `lockOwnerId:` line in the prose body must not be parsed as a
        real field. This is the body-text masquerade guard."""
        self._write_marker(
            "agent: claude\nexpires: 2099-01-01T00:00:00Z\npaths:\n  - Source/x.py\n",
            "This marker mentions lockOwnerId: attacker-owned in prose.",
        )
        m = wg.parse_marker(self.tmpdir, self.marker_path.name)
        self.assertIsNone(m["lockOwnerId"])

    def test_json_quoted_dialect(self):
        self._write_marker(
            'agent: "claude"\n'
            'expires: "2099-01-01T00:00:00Z"\n'
            'paths:\n  - "Source/gateway.py"\n'
        )
        m = wg.parse_marker(self.tmpdir, self.marker_path.name)
        self.assertEqual(m["agent"], "claude")
        self.assertEqual(m["expires"], "2099-01-01T00:00:00Z")
        self.assertIn("Source/gateway.py", m["paths"])

    def test_no_frontmatter_parses_empty(self):
        self.marker_path.write_text("just prose, no delimiters\n")
        m = wg.parse_marker(self.tmpdir, self.marker_path.name)
        # _extract_frontmatter returns "" so parse_marker returns None
        self.assertIsNone(m)

    def test_contribution_marker_has_no_pid(self):
        self._write_marker(
            "agent: taskwraith-contribution\n"
            "lockOwnerId: 4947b033-b17c-4d3e-a83e-d7440aeab5d5\n"
            "expires: 2099-01-01T00:00:00Z\n"
            "paths:\n  - Source/gateway.py\n"
        )
        m = wg.parse_marker(self.tmpdir, self.marker_path.name)
        self.assertIsNone(m["pid"])
        self.assertIsNotNone(m["lockOwnerId"])


class TestLiveness(unittest.TestCase):
    def setUp(self):
        self.now = datetime.now(timezone.utc).timestamp() * 1000
        self.started_ms = self.now - 5 * 60 * 1000  # 5 min ago
        self.expires_ms = self.now + 10 * 60 * 1000  # 10 min ahead

    def _marker(self, **overrides):
        base = {
            "file": ".WORK-IN-PROGRESS-test.md",
            "pid": None,
            "started": None,
            "expires": None,
            "expiresMs": self.expires_ms,
            "lockOwnerId": None,
            "matchers": [],
            "paths": [],
            "workspaceWide": False,
            "derived": False,
        }
        base.update(overrides)
        return base

    def test_live_pid_and_not_expired(self):
        m = self._marker(pid=999999)  # unlikely to exist
        s = wg.liveness(m, {}, self.now)
        # pid 999999 is probably dead, but we test the logic path:
        # if alive and not expired → live
        # Since we can't guarantee pid liveness, test with expires only:
        self.assertFalse(s["expired"])

    def test_expired_pid_dead(self):
        m = self._marker(pid=1, expiresMs=self.now - 1000)  # pid 1 likely alive (init)
        s = wg.liveness(m, {}, self.now)
        self.assertTrue(s["expired"])

    def test_lease_cap_clamps_to_20m(self):
        """A lease of 73 years is clamped to 20 minutes from started."""
        started_str = _iso(datetime.now(timezone.utc) - timedelta(minutes=25))
        m = self._marker(
            started=started_str,
            expiresMs=self.now + 73 * 365 * 24 * 3600 * 1000,  # 73 years
        )
        s = wg.liveness(m, {}, self.now)
        self.assertTrue(s["expired"])  # 25m > 20m cap → expired

    def test_lease_cap_not_voided(self):
        """A 73-year lease still protects for its first 20 minutes."""
        started_str = _iso(datetime.now(timezone.utc) - timedelta(minutes=5))
        m = self._marker(
            started=started_str,
            expiresMs=self.now + 73 * 365 * 24 * 3600 * 1000,
            pid=1,  # alive
        )
        s = wg.liveness(m, {}, self.now)
        self.assertFalse(s["expired"])  # 5m < 20m cap → still live

    def test_owner_id_held(self):
        m = self._marker(
            lockOwnerId="4947b033-b17c-4d3e-a83e-d7440aeab5d5",
            expiresMs=self.now + 10 * 60 * 1000,
        )
        s = wg.liveness(m, {}, self.now)
        self.assertTrue(s["ownerHeld"])
        self.assertTrue(s["live"])

    def test_heartbeat_fresh(self):
        side = {"schemaVersion": 2, "markers": {
            ".WORK-IN-PROGRESS-test.md": {"lastSeen": self.now - 30 * 1000}
        }}
        m = self._marker()
        s = wg.liveness(m, side, self.now)
        self.assertTrue(s["heartbeatFresh"])
        self.assertTrue(s["live"])

    def test_unreadable_expires_decays(self):
        m = self._marker(expiresMs=None, lockOwnerId=None)
        s = wg.liveness(m, {}, self.now)
        self.assertTrue(s["expired"])
        self.assertFalse(s["live"])


class TestLivenessExpiryGate(unittest.TestCase):
    """An elapsed lease must defeat every other liveness signal.

    Regression cover for the immortality bug. `liveness` used to OR a fresh
    heartbeat in without consulting `expired`, and because `advance_heartbeats`
    refreshes `lastSeen` from a live pid on every tick, a claim whose owner was
    merely still running could never decay. `work_guard status` then printed
    `LIVE ... expires PASSED` indefinitely, contradicting `.githooks/pre-commit`
    — which steps past an expired claim — so the tool reported a lane held that
    the hook would not enforce, and four landed claims stayed unadoptable.
    """

    UUID = "4947b033-b17c-4d3e-a83e-d7440aeab5d5"
    SHA = "a" * 64
    RUNTIME = f".WORK-IN-PROGRESS-taskwraith-runtime-seat1-{SHA}.md"
    CONTRIBUTION = f".WORK-IN-PROGRESS-taskwraith-contribution-{SHA}.md"

    def setUp(self):
        self.now = datetime.now(timezone.utc).timestamp() * 1000
        self.past = self.now - 60 * 1000
        self.future = self.now + 10 * 60 * 1000

    def _marker(self, **overrides):
        base = {
            "file": ".WORK-IN-PROGRESS-test.md",
            "pid": None,
            "started": None,
            "expires": None,
            "expiresMs": self.future,
            "lockOwnerId": None,
            "matchers": [],
            "paths": [],
            "workspaceWide": False,
            "derived": False,
        }
        base.update(overrides)
        return base

    def _fresh_heartbeat(self, filename=".WORK-IN-PROGRESS-test.md"):
        return {"schemaVersion": 2,
                "markers": {filename: {"lastSeen": self.now - 30 * 1000}}}

    # ── the bug itself: expiry must win ─────────────────────────────────
    def test_alive_pid_past_expiry_is_decayed(self):
        s = wg.liveness(self._marker(pid=1, expiresMs=self.past), {}, self.now)
        self.assertTrue(s["alive"], "pid 1 exists; EPERM still means alive")
        self.assertTrue(s["expired"])
        self.assertFalse(s["live"], "an alive pid past expiry is still decayed")

    def test_fresh_heartbeat_cannot_outvote_an_elapsed_lease(self):
        side = self._fresh_heartbeat()
        s = wg.liveness(self._marker(pid=1, expiresMs=self.past), side, self.now)
        self.assertTrue(s["heartbeatFresh"])
        self.assertTrue(s["expired"])
        self.assertFalse(s["live"], "a heartbeat refreshes a claim, it never renews it")

    def test_missing_lease_decays_even_with_a_fresh_heartbeat(self):
        side = self._fresh_heartbeat()
        s = wg.liveness(self._marker(pid=1, expiresMs=None), side, self.now)
        self.assertTrue(s["expired"])
        self.assertFalse(s["live"])

    # ── what the heartbeat is still for ─────────────────────────────────
    def test_fresh_heartbeat_holds_a_quiet_session_inside_its_lease(self):
        """No pid to probe: the heartbeat is the only live signal, and must
        keep a session that is thinking rather than writing from decaying."""
        side = self._fresh_heartbeat()
        s = wg.liveness(self._marker(pid=None, expiresMs=self.future), side, self.now)
        self.assertTrue(s["heartbeatFresh"])
        self.assertTrue(s["live"])

    def test_alive_pid_inside_its_lease_is_live_without_any_heartbeat(self):
        """The safety invariant: never DECAYED while the hook would block."""
        s = wg.liveness(self._marker(pid=1, expiresMs=self.future), {}, self.now)
        self.assertTrue(s["live"])

    # ── owner-id identity ───────────────────────────────────────────────
    def test_opaque_seat_owner_id_holds_a_manual_claim(self):
        s = wg.liveness(self._marker(lockOwnerId=self.UUID, expiresMs=self.future),
                        {}, self.now)
        self.assertTrue(s["ownerHeld"])
        self.assertTrue(s["live"])

    def test_opaque_seat_owner_id_does_not_hold_a_claim_past_its_lease(self):
        s = wg.liveness(self._marker(lockOwnerId=self.UUID, expiresMs=self.past),
                        {}, self.now)
        self.assertFalse(s["ownerHeld"])
        self.assertFalse(s["live"])

    def test_human_readable_owner_id_blocks_without_being_opaque(self):
        """`MySeatName` identifies no seat, so it is not `ownerHeld`; but the
        hook still blocks other sessions on it, so this tool must not report
        the lane decayed and invite the next agent to harvest it."""
        s = wg.liveness(self._marker(lockOwnerId="MySeatName", expiresMs=self.future),
                        {}, self.now)
        self.assertFalse(s["ownerHeld"])
        self.assertTrue(s["live"])

    # ── runtime projections fail closed, classified by FILENAME ─────────
    def test_runtime_projection_fails_closed_past_expiry(self):
        m = self._marker(file=self.RUNTIME, pid=1, expiresMs=self.past,
                         lockOwnerId=self.UUID)
        s = wg.liveness(m, {}, self.now)
        self.assertEqual(s["classification"], "runtime")
        self.assertTrue(s["expired"])
        self.assertTrue(s["live"], "durable authority owns cleanup, not the lease")

    def test_runtime_projection_fails_closed_with_a_dead_pid_and_no_lease(self):
        m = self._marker(file=self.RUNTIME, pid=999999, expiresMs=None)
        s = wg.liveness(m, {}, self.now)
        self.assertEqual(s["classification"], "runtime")
        self.assertTrue(s["live"], "a dead projected leader pid is a recovery block")

    def test_runtime_classification_ignores_the_derived_frontmatter_field(self):
        """The hook recognises a projection by filename; a writer that omits
        `derived: true` must not demote it to manual-claim rules."""
        m = self._marker(file=self.RUNTIME, derived=False, expiresMs=self.past)
        s = wg.liveness(m, {}, self.now)
        self.assertEqual(s["classification"], "runtime")
        self.assertTrue(s["live"])

    # ── contribution projections are expires-based ──────────────────────
    def test_contribution_projection_is_held_inside_its_lease(self):
        m = self._marker(file=self.CONTRIBUTION, expiresMs=self.future,
                         lockOwnerId=self.UUID)
        s = wg.liveness(m, {}, self.now)
        self.assertEqual(s["classification"], "contribution")
        self.assertTrue(s["live"])

    def test_contribution_projection_decays_past_its_lease(self):
        m = self._marker(file=self.CONTRIBUTION, expiresMs=self.past,
                         lockOwnerId=self.UUID)
        s = wg.liveness(m, {}, self.now)
        self.assertEqual(s["classification"], "contribution")
        self.assertFalse(s["live"])

    def test_contribution_projection_without_an_owner_id_claims_nothing(self):
        m = self._marker(file=self.CONTRIBUTION, expiresMs=self.future,
                         lockOwnerId=None)
        s = wg.liveness(m, {}, self.now)
        self.assertEqual(s["classification"], "contribution")
        self.assertFalse(s["live"])

    def test_manual_marker_is_classified_manual(self):
        s = wg.liveness(self._marker(pid=1), {}, self.now)
        self.assertEqual(s["classification"], "manual")


class TestSnapshotRoundTrip(unittest.TestCase):
    """End-to-end: create a temp repo, snapshot, recover a file."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="wg-snap-")
        self.repo = Path(self.tmpdir, "repo")
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=self.repo, check=True)
        subprocess.run(["git", "config", "user.name", "work-guard test"], cwd=self.repo, check=True)
        subprocess.run(["git", "config", "user.email", "test@invalid"], cwd=self.repo, check=True)
        subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=self.repo, check=True)
        # baseline commit
        Path(self.repo, "Source").mkdir()
        Path(self.repo, "Source", "gateway.py").write_text("baseline\n")
        subprocess.run(["git", "add", "Source/gateway.py"], cwd=self.repo, check=True)
        subprocess.run(["git", "commit", "-qm", "baseline"], cwd=self.repo, check=True)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_snapshot_captures_uncommitted_work(self):
        Path(self.repo, "Source", "gateway.py").write_text("modified but uncommitted\n")
        Path(self.repo, "Source", "new_file.py").write_text("untracked\n")
        snap = wg.take_snapshot(str(self.repo), "test snapshot")
        self.assertTrue(snap["ok"], f"snapshot failed: {snap.get('reason')}")
        ref = snap["ref"]
        self.assertIn("refs/wip/", ref)
        # Recover the uncommitted content from the snapshot.
        result = subprocess.run(
            ["git", "show", f"{ref}:Source/gateway.py"],
            cwd=self.repo, capture_output=True, text=True,
        )
        self.assertEqual(result.stdout, "modified but uncommitted\n")
        # Recover the untracked file too.
        result2 = subprocess.run(
            ["git", "show", f"{ref}:Source/new_file.py"],
            cwd=self.repo, capture_output=True, text=True,
        )
        self.assertEqual(result2.stdout, "untracked\n")

    def test_snapshot_does_not_touch_index(self):
        """The shared index must remain byte-identical after a snapshot."""
        index_path = Path(self.repo, ".git", "index")
        Path(self.repo, "Source", "gateway.py").write_text("dirty\n")
        subprocess.run(["git", "add", "Source/gateway.py"], cwd=self.repo, check=True)
        before = index_path.read_bytes()
        wg.take_snapshot(str(self.repo), "test")
        after = index_path.read_bytes()
        self.assertEqual(before, after, "shared index was modified by snapshot")

    def test_identical_tree_skips_snapshot(self):
        Path(self.repo, "Source", "gateway.py").write_text("dirty\n")
        snap1 = wg.take_snapshot(str(self.repo), "first")
        self.assertTrue(snap1["ok"])
        snap2 = wg.take_snapshot(str(self.repo), "second")
        self.assertTrue(snap2.get("skipped"), "identical tree should be skipped")


class TestEvaluateOrphans(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="wg-eval-")
        self.repo = Path(self.tmpdir, "repo")
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=self.repo, check=True)
        subprocess.run(["git", "config", "user.name", "test"], cwd=self.repo, check=True)
        subprocess.run(["git", "config", "user.email", "test@invalid"], cwd=self.repo, check=True)
        Path(self.repo, "Source").mkdir()
        Path(self.repo, "Source", "x.py").write_text("baseline\n")
        subprocess.run(["git", "add", "."], cwd=self.repo, check=True)
        subprocess.run(["git", "commit", "-qm", "init"], cwd=self.repo, check=True)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_dirty_file_with_no_marker_is_orphan(self):
        Path(self.repo, "Source", "x.py").write_text("dirty\n")
        now = time.time() * 1000
        result = wg.evaluate(str(self.repo), now)
        self.assertEqual(len(result["orphans"]), 1)
        self.assertEqual(result["orphans"][0]["path"], "Source/x.py")

    def test_dirty_file_covered_by_live_marker_not_orphan(self):
        Path(self.repo, "Source", "x.py").write_text("dirty\n")
        # Create a live marker covering this path.
        now = time.time() * 1000
        started = datetime.now(timezone.utc) - timedelta(minutes=2)
        expires = datetime.now(timezone.utc) + timedelta(minutes=10)
        marker_file = Path(self.repo, ".WORK-IN-PROGRESS-test.md")
        marker_file.write_text(
            f"---\nagent: test\npid: {os.getpid()}\n"
            f"started: {_iso(started)}\nexpires: {_iso(expires)}\n"
            f"paths:\n  - Source/x.py\n---\n"
        )
        result = wg.evaluate(str(self.repo), now)
        self.assertEqual(len(result["orphans"]), 0,
                         "dirty path covered by a live claim should not be orphaned")


if __name__ == "__main__":
    unittest.main()
