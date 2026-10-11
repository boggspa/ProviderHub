"""Real local Git pagination and bounded, asynchronous PR inspection."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import chat_git_overview as overview


class GitOverviewTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name) / "repo"
        self.root.mkdir()
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.name", "Fixture")
        self.git("config", "user.email", "fixture@example.invalid")

    def git(self, *args):
        return subprocess.check_output(["git", "-C", str(self.root), *args], stderr=subprocess.DEVNULL, text=True).strip()

    def commit(self, subject):
        self.git("commit", "--allow-empty", "-qm", subject)
        return self.git("rev-parse", "HEAD")

    def test_unpushed_pages_use_cached_upstream_and_preserve_index(self):
        self.commit("base")
        self.git("branch", "cached-upstream")
        self.git("branch", "--set-upstream-to=cached-upstream")
        hashes = [self.commit("change " + str(index)) for index in range(43)]
        (self.root / "staged").write_text("staged\n")
        self.git("add", "--", "staged")
        before = self.git("diff", "--cached")
        page = overview.git_overview(self.root)
        self.assertEqual(len(page["commits"]), 20)
        self.assertEqual(page["upstream"], "cached-upstream")
        commits = page["commits"]
        while page["nextCursor"]:
            page = overview.git_overview(self.root, page["nextCursor"])
            commits += page["commits"]
        self.assertEqual([row["hash"] for row in commits], list(reversed(hashes)))
        self.assertEqual(self.git("diff", "--cached"), before)
        self.assertFalse(page["truncated"])

    def test_cursor_rejects_changed_head_upstream_and_invalid_offsets(self):
        self.commit("base")
        self.git("branch", "cached-upstream")
        self.git("branch", "--set-upstream-to=cached-upstream")
        for index in range(22):
            self.commit("change " + str(index))
        page = overview.git_overview(self.root)
        cursor = page["nextCursor"]
        for offset in [-1, 0, 1, True, 200, "20"]:
            with self.subTest(offset=offset), self.assertRaises(ValueError):
                overview.git_overview(self.root, {**cursor, "offset": offset})
        self.git("branch", "-f", "cached-upstream", "HEAD~1")
        with self.assertRaisesRegex(ValueError, "history changed"):
            overview.git_overview(self.root, cursor)
        self.commit("new head")
        with self.assertRaisesRegex(ValueError, "history changed"):
            overview.git_overview(self.root, cursor)

    def test_unborn_detached_and_no_upstream_are_explicit(self):
        page = overview.git_overview(self.root)
        self.assertIsNone(page["head"])
        self.assertEqual(page["commits"], [])
        self.commit("local subject with\ttab")
        page = overview.git_overview(self.root)
        self.assertIsNone(page["upstream"])
        self.assertEqual(page["commits"][0]["subject"], "local subject with\ttab")
        self.git("checkout", "--detach")
        self.assertEqual(overview.git_overview(self.root)["branch"], "Detached HEAD")

    def test_commit_cursor_rejects_changed_repository_with_same_head(self):
        for index in range(21):
            self.commit("change " + str(index))
        cursor = overview.git_overview(self.root)["nextCursor"]
        self.git("remote", "add", "origin", "https://github.com/example/changed.git")
        with self.assertRaisesRegex(ValueError, "history changed"):
            overview.git_overview(self.root, cursor)
        cursor = overview.git_overview(self.root)["nextCursor"]
        with self.assertRaisesRegex(ValueError, "history changed"):
            overview.git_overview(self.root, {**cursor, "root": "/wrong-root"})

    def test_local_inspection_never_runs_external_diff_or_hook(self):
        self.commit("base")
        marker = self.root / "executed"
        script = self.root / "unsafe"
        script.write_text("#!/bin/sh\ntouch '" + str(marker) + "'\n")
        script.chmod(0o700)
        self.git("config", "diff.external", str(script))
        self.git("config", "core.fsmonitor", str(script))
        self.assertEqual(len(overview.git_overview(self.root)["commits"]), 1)
        self.assertFalse(marker.exists())

    def test_pull_request_page_is_bounded_and_anchored_to_repository(self):
        self.git("remote", "add", "origin", "git@github.com:example/project.git")
        payload = {"data": {"repository": {"pullRequests": {
            "nodes": [{"number": index, "title": "PR " + str(index),
                       "url": "https://github.com/example/project/pull/" + str(index),
                       "headRefName": "topic", "isDraft": index == 1, "author": {"login": "author"}}
                      for index in range(1, 22)],
            "pageInfo": {"hasNextPage": True, "endCursor": "cursor-1"}}}}}
        with patch.object(overview.shutil, "which", return_value="/test/gh"), patch.object(overview, "_gh", return_value=payload) as gh:
            first = overview.pull_requests(self.root)
            self.assertEqual(len(first["items"]), 20)
            self.assertTrue(first["items"][0]["draft"])
            second = overview.pull_requests(self.root, first["nextCursor"])
            self.assertIn("after=cursor-1", gh.call_args.args[0])
            self.assertEqual(second["repository"]["url"], "https://github.com/example/project")
            self.git("remote", "set-url", "origin", "https://github.com/example/other.git")
            with self.assertRaisesRegex(ValueError, "Repository changed"):
                overview.pull_requests(self.root, first["nextCursor"])

    def test_uses_branch_remote_and_rejects_unsafe_links(self):
        self.commit("base")
        self.git("remote", "add", "origin", "https://github.com/example/fork.git")
        self.git("remote", "add", "upstream", "https://github.com/example/project.git")
        self.git("config", "branch.main.remote", "upstream")
        payload = {"data": {"repository": {"pullRequests": {"nodes": [
            {"number": 1, "title": "unsafe", "url": "file:///tmp/unsafe"},
            {"number": 2, "title": "wrong host", "url": "https://elsewhere.invalid/pull/2"}],
            "pageInfo": {"hasNextPage": False, "endCursor": None}}}}}
        with patch.object(overview.shutil, "which", return_value="/test/gh"), patch.object(overview, "_gh", return_value=payload) as gh:
            result = overview.pull_requests(self.root)
            self.assertEqual(result["items"], [])
            self.assertEqual(result["repository"]["name"], "project")
            self.assertIn("name=project", gh.call_args.args[0])

    def test_missing_remote_does_not_launch_cli(self):
        with patch.object(overview, "_gh") as gh:
            result = overview.pull_requests(self.root)
        self.assertEqual(result["items"], [])
        self.assertIn("remote", result["notice"])
        gh.assert_not_called()

    def test_command_returns_before_slow_io_and_coalesces_per_kind(self):
        events = []
        entered = threading.Event()
        release = threading.Event()
        class Parent:
            chat = {"id": "chat", "workspace": str(self.root)}
            _mutex = threading.RLock()
            emit = staticmethod(events.append)
        parent = Parent()
        def slow(workspace, cursor):
            entered.set()
            release.wait(3)
            return {"branch": "main"}
        with patch.object(overview, "git_overview", side_effect=slow):
            started = time.monotonic()
            self.assertTrue(overview.handle(parent, {"command": "git_overview", "id": "chat", "request": "one"}))
            self.assertLess(time.monotonic() - started, 0.25)
            self.assertTrue(entered.wait(2))
            overview.handle(parent, {"command": "git_overview", "id": "chat", "request": "two"})
            self.assertIn("already running", events[-1]["notice"])
            release.set()
            deadline = time.monotonic() + 2
            while not any(event.get("overview") for event in events) and time.monotonic() < deadline:
                time.sleep(0.005)
        self.assertEqual(events[-1]["request"], "one")
        self.assertEqual(events[-1]["workspace"], str(self.root))
        self.assertEqual(parent._git_overview_pending, set())

    def test_cli_timeout_and_output_cap_stop_child(self):
        for code, expected in [("import time; time.sleep(10)", "timed out"),
                               ("import sys; sys.stdout.write('x' * 300000)", "limit")]:
            with self.subTest(expected=expected), self.assertRaisesRegex(ValueError, expected):
                overview._gh([sys.executable, "-c", code], timeout=0.15 if expected == "timed out" else 2)

    def test_worker_failure_echoes_request_and_releases_pending_slot(self):
        events = []
        class Parent:
            chat = {"id": "chat", "workspace": str(self.root)}
            _mutex = threading.RLock()
            emit = staticmethod(events.append)
        parent = Parent()
        with patch.object(overview, "pull_requests", side_effect=ValueError("Pull request lookup timed out.")):
            overview.handle(parent, {"command": "git_pull_requests", "id": "chat", "request": "one"})
            deadline = time.monotonic() + 2
            while not events and time.monotonic() < deadline:
                time.sleep(0.005)
        self.assertEqual(events[-1]["request"], "one")
        self.assertIn("timed out", events[-1]["notice"])
        self.assertEqual(parent._git_overview_pending, set())


if __name__ == "__main__":
    unittest.main()
