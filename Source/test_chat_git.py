from pathlib import Path
import os
import subprocess
import tempfile
import unittest

from chat_git import git_status


class ChatGitTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"; self.repo.mkdir()
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.name", "Fixture")
        self.git("config", "user.email", "fixture@example.invalid")
    def git(self, *args):
        return subprocess.check_output(["git", "-C", str(self.repo), *args], stderr=subprocess.DEVNULL, text=True)
    def commit(self, text="one\ntwo\n"):
        (self.repo / "file.txt").write_text(text)
        self.git("add", "--", "file.txt"); self.git("commit", "-qm", "fixture")

    def test_changes_include_index_worktree_and_untracked_without_double_counting(self):
        self.commit()
        (self.repo / "file.txt").write_text("one\nnew\nthree\n")
        self.git("add", "--", "file.txt")
        (self.repo / "file.txt").write_text("one\nnew\nthree\nfour\n")
        (self.repo / "new.txt").write_text("untracked\n")
        result = git_status(self.repo)
        self.assertEqual((result["files"], result["added"], result["deleted"]), (2, 3, 1))
        self.assertEqual(result["ahead"], 1)
        self.assertIsNone(result["behind"]); self.assertIsNone(result["upstream"])

    def test_unborn_repo_rename_binary_and_non_repo(self):
        self.assertEqual(git_status(self.repo)["ahead"], 0)
        self.assertIsNone(git_status(self.root))
        self.commit()
        self.git("mv", "file.txt", "renamed.txt")
        (self.repo / "binary").write_bytes(b"\x00\xff")
        result = git_status(self.repo)
        self.assertEqual(result["files"], 2)
        self.assertEqual((result["added"], result["deleted"]), (0, 0))

    def test_ahead_behind_compare_cached_upstream_without_network(self):
        self.commit()
        self.git("branch", "upstream")
        self.git("branch", "--set-upstream-to=upstream", "main")
        self.commit("local\n")
        result = git_status(self.repo)
        self.assertEqual((result["ahead"], result["behind"]), (1, 0))
        self.git("switch", "upstream"); self.commit("remote\n"); self.git("switch", "main")
        result = git_status(self.repo)
        self.assertEqual((result["ahead"], result["behind"]), (1, 1))

    def test_git_status_does_not_run_external_diff(self):
        self.commit(); (self.repo / "file.txt").write_text("changed\n")
        script = self.root / "untrusted-diff"; marker = self.root / "executed"
        script.write_text(f"#!/bin/sh\ntouch '{marker}'\n"); script.chmod(0o700)
        self.git("config", "diff.external", str(script))
        self.assertIsNotNone(git_status(self.repo))
        self.assertFalse(marker.exists())


if __name__ == "__main__": unittest.main()
