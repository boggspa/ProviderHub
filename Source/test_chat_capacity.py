"""Expanded default capacity exercised through production admission and cleanup."""
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import chat_execution
import chat_processes
from chat_processes import ProcessRegistry
import test_chat_processes as fixtures


class CapacityTests(unittest.TestCase):
    def test_new_team_default_and_saved_process_limit(self):
        self.assertEqual(chat_execution.settings()["processes"], 8)
        self.assertEqual(chat_execution.settings({"processes": 3})["processes"], 3)
        self.assertEqual(chat_execution.settings({"processes": 16})["processes"], 16)
        for invalid in (0, 17, True):
            with self.subTest(processes=invalid), self.assertRaises(ValueError):
                chat_execution.settings({"processes": invalid})

    @fixtures.NEEDS_WAITID
    def test_full_capacity_with_256_file_descriptors(self):
        import resource
        before = resource.getrlimit(resource.RLIMIT_NOFILE)
        if before[1] != resource.RLIM_INFINITY and before[1] < 256:
            self.skipTest("hard file-descriptor limit is below 256")
        # Limit a fresh interpreter, never the test runner or neighboring tests.
        # Reserve ordinary transport/file headroom in addition to the live pipes.
        program = """
from contextlib import ExitStack
import os
import resource
import sys
import unittest
from test_chat_capacity import CapacityTests

try:
    resource.setrlimit(resource.RLIMIT_NOFILE, (256, 256))
except (OSError, ValueError) as exc:
    print(f"Cannot set a 256-descriptor soft limit: {exc}", file=sys.stderr)
    sys.exit(77)
with ExitStack() as opened:
    for _ in range(32):
        opened.enter_context(open(os.devnull, "rb"))
    suite = unittest.TestSuite([
        CapacityTests("test_concurrent_launches_enforce_128_global_and_sixteen_per_chat")
    ])
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
"""
        result = subprocess.run([sys.executable, "-c", program],
                                cwd=Path(__file__).resolve().parent,
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(resource.getrlimit(resource.RLIMIT_NOFILE), before)
        if result.returncode == 77:
            self.skipTest(result.stderr.strip())
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    @fixtures.NEEDS_WAITID
    def test_concurrent_launches_enforce_128_global_and_sixteen_per_chat(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = ProcessRegistry()
            workspace = str(Path(directory).resolve())
            owners = [fixtures.Conversation(f"capacity-{i}") for i in range(10)]
            accepted, rejected, pids = [], [], []
            def launch(owner):
                try:
                    registry.start(owner, workspace, "echo ready; exec sleep 30", dict(os.environ), owner.cancel)
                    return True
                except ValueError as exc:
                    return str(exc)
            try:
                with patch.object(chat_processes, "FIRST_LOOK", 0):
                    # Ten chats race for the same global budget using real
                    # isolated process groups that report readiness, then sleep.
                    with ThreadPoolExecutor(max_workers=16) as executor:
                        outcomes = list(executor.map(launch, [owner for owner in owners for _ in range(16)]))
                    accepted = [value for value in outcomes if value is True]
                    rejected = [value for value in outcomes if value is not True]
                    self.assertEqual(len(accepted), 128)
                    self.assertEqual(len(rejected), 32)
                    self.assertTrue(all("128 background processes" in error for error in rejected), rejected)
                    self.assertTrue(fixtures.wait_for(lambda: all(
                        "ready" in row["output"]
                        for owner in owners for row in registry.snapshot(owner.chat["id"])
                    )), "not every admitted process could read its output")
                    rows = {owner.chat["id"]: registry.snapshot(owner.chat["id"]) for owner in owners}
                    pids = [row["pid"] for values in rows.values() for row in values]
                    self.assertEqual(len(set(pids)), 128)
                    full = next(owner for owner in owners if len(rows[owner.chat["id"]]) == 16)
                    with self.assertRaisesRegex(ValueError, "already has 16"):
                        registry.start(full, workspace, "exec sleep 30", dict(os.environ), full.cancel)
                    free = fixtures.Conversation("replacement")
                    with self.assertRaisesRegex(ValueError, "128 background processes"):
                        registry.start(free, workspace, "exec sleep 30", dict(os.environ), free.cancel)
                    stopped = registry.snapshot(full.chat["id"])[0]
                    registry.stop_by_user(full.chat["id"], stopped["id"])
                    self.assertTrue(fixtures.wait_for(lambda: registry.inspect(full.chat["id"], stopped["id"])["status"] == "stopped"))
                    registry.start(free, workspace, "exec sleep 30", dict(os.environ), free.cancel)
                    pids.append(registry.snapshot("replacement")[0]["pid"])
                    self.assertEqual(sum(row["status"] == "running" for owner in owners + [free] for row in registry.snapshot(owner.chat["id"])), 128)
            finally:
                # Also cleans up launches when an earlier assertion fails.
                pids = list(set(pids + [process.pid for record in registry._chats.values() for process in record["processes"]]))
                registry.shutdown(timeout=2)
                self.assertTrue(fixtures.wait_for(lambda: all(fixtures.gone(pid) for pid in pids)), "capacity test left a process running")
            with self.assertRaisesRegex(ValueError, "closing"):
                launch_after_shutdown = fixtures.Conversation("after-shutdown")
                registry.start(launch_after_shutdown, workspace, "true", dict(os.environ), launch_after_shutdown.cancel)


if __name__ == "__main__": unittest.main()
