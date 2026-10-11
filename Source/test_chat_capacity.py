"""Expanded default capacity exercised through production admission and cleanup."""
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
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
        with self.assertRaises(ValueError): chat_execution.settings({"processes": 9})

    @fixtures.NEEDS_WAITID
    def test_concurrent_launches_enforce_sixty_four_global_and_eight_per_chat(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = ProcessRegistry()
            workspace = str(Path(directory).resolve())
            owners = [fixtures.Conversation(f"capacity-{i}") for i in range(10)]
            accepted, rejected, pids = [], [], []
            def launch(owner):
                try:
                    registry.start(owner, workspace, "exec sleep 30", dict(os.environ), owner.cancel)
                    return True
                except ValueError as exc:
                    return str(exc)
            try:
                with patch.object(chat_processes, "FIRST_LOOK", 0):
                    # Ten chats race for the same global budget. All use real
                    # isolated process groups, but no shell/vendor workloads.
                    with ThreadPoolExecutor(max_workers=16) as executor:
                        outcomes = list(executor.map(launch, [owner for owner in owners for _ in range(8)]))
                    accepted = [value for value in outcomes if value is True]
                    rejected = [value for value in outcomes if value is not True]
                    self.assertEqual(len(accepted), 64)
                    self.assertEqual(len(rejected), 16)
                    self.assertTrue(all("64 background processes" in error for error in rejected), rejected)
                    rows = {owner.chat["id"]: registry.snapshot(owner.chat["id"]) for owner in owners}
                    pids = [row["pid"] for values in rows.values() for row in values]
                    self.assertEqual(len(set(pids)), 64)
                    full = next(owner for owner in owners if len(rows[owner.chat["id"]]) == 8)
                    with self.assertRaisesRegex(ValueError, "already has 8"):
                        registry.start(full, workspace, "exec sleep 30", dict(os.environ), full.cancel)
                    free = fixtures.Conversation("replacement")
                    with self.assertRaisesRegex(ValueError, "64 background processes"):
                        registry.start(free, workspace, "exec sleep 30", dict(os.environ), free.cancel)
                    stopped = registry.snapshot(full.chat["id"])[0]
                    registry.stop_by_user(full.chat["id"], stopped["id"])
                    self.assertTrue(fixtures.wait_for(lambda: registry.inspect(full.chat["id"], stopped["id"])["status"] == "stopped"))
                    registry.start(free, workspace, "exec sleep 30", dict(os.environ), free.cancel)
                    pids.append(registry.snapshot("replacement")[0]["pid"])
                    self.assertEqual(sum(row["status"] == "running" for owner in owners + [free] for row in registry.snapshot(owner.chat["id"])), 64)
            finally:
                # Also cleans up launches when an earlier assertion fails.
                pids = list(set(pids + [process.pid for record in registry._chats.values() for process in record["processes"]]))
                registry.shutdown(timeout=2)
                self.assertTrue(fixtures.wait_for(lambda: all(fixtures.gone(pid) for pid in pids)), "capacity test left a process running")
            with self.assertRaisesRegex(ValueError, "closing"):
                launch_after_shutdown = fixtures.Conversation("after-shutdown")
                registry.start(launch_after_shutdown, workspace, "true", dict(os.environ), launch_after_shutdown.cancel)


if __name__ == "__main__": unittest.main()
