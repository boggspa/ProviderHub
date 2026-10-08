import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from chat_inspector import (git_changes, git_branches, switch_branch, create_branch,
                            create_worktree, switch_worktree)


class InspectorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "repo"
        self.root.mkdir()
        self.git("init", "-b", "main")
        self.git("config", "user.name", "Test")
        self.git("config", "user.email", "test@example.invalid")

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.root), *args], check=True,
                              capture_output=True).stdout

    def commit(self):
        self.git("add", "--all")
        self.git("commit", "-m", "fixture")

    def test_combined_diff_untracked_binary_and_subdirectory(self):
        (self.root / "file").write_text("one\ntwo\nthree\n")
        (self.root / "sub").mkdir()
        self.commit()
        (self.root / "file").write_text("one\nstaged\nthree\n")
        self.git("add", "file")
        (self.root / "file").write_text("one\nfinal\nthree\n")
        (self.root / "-odd\tname\n").write_text("untracked\n")
        (self.root / "binary").write_bytes(b"a\0b")
        rows = {row["path"]: row for row in git_changes(self.root / "sub")["files"]}
        self.assertIn("+final", rows["file"]["diff"])
        self.assertNotIn("+staged", rows["file"]["diff"])
        self.assertEqual((rows["file"]["added"], rows["file"]["deleted"]), (1, 1))
        self.assertEqual(rows["-odd\tname\n"]["added"], 1)
        self.assertTrue(rows["binary"]["binary"])

    def test_rename_hostile_filename_and_deletion(self):
        (self.root / "before\n").write_text("same\n" * 20)
        (self.root / "gone").write_text("gone\n")
        self.commit()
        self.git("mv", "--", "before\n", "-after\t")
        (self.root / "gone").unlink()
        rows = {row["path"]: row for row in git_changes(self.root)["files"]}
        self.assertEqual(rows["-after\t"]["oldPath"], "before\n")
        self.assertEqual(rows["-after\t"]["status"], "R")
        self.assertEqual(rows["gone"]["deleted"], 1)

    def test_unborn_detached_and_limits(self):
        (self.root / "new").write_text("hello\n")
        self.git("add", "new")
        self.assertEqual(git_changes(self.root)["files"][0]["added"], 1)
        self.assertEqual(git_branches(self.root)["current"], "main")
        self.commit()
        self.git("checkout", "--detach")
        self.assertIsNone(git_branches(self.root)["current"])
        (self.root / "huge").write_text("large\n" * 100000)
        result = git_changes(self.root)
        self.assertTrue(result["truncated"])
        self.assertLess(len(result["files"][0]["diff"]), 65000)

    def test_branch_and_worktree_success_and_checked_out_elsewhere(self):
        (self.root / "file").write_text("base\n")
        self.commit()
        self.assertEqual(create_branch(self.root, "feature")["current"], "feature")
        self.assertEqual(switch_branch(self.root, "main")["current"], "main")
        destination = self.root.parent / "tree\nwith space"
        result = create_worktree(self.root, "parallel", destination)
        self.assertTrue(any(tree["path"] == str(destination.resolve()) for tree in result["worktrees"]))
        self.assertEqual(switch_worktree(self.root, destination), str(destination.resolve()))
        with self.assertRaises(ValueError): switch_branch(self.root, "parallel")
        with self.assertRaises(ValueError): create_worktree(self.root, "another", destination)
        with self.assertRaises(ValueError): switch_worktree(self.root, self.root.parent)

    def test_dirty_rejections_preserve_index_and_content(self):
        (self.root / "file").write_text("base\n")
        self.commit()
        self.git("branch", "other")
        (self.root / "file").write_text("staged\n")
        self.git("add", "file")
        (self.root / "file").write_text("working\n")
        before = self.git("diff", "--cached")
        for operation in [lambda: switch_branch(self.root, "other"),
                          lambda: create_branch(self.root, "new"),
                          lambda: create_worktree(self.root, "new", self.root.parent / "new")]:
            with self.assertRaisesRegex(ValueError, "uncommitted"):
                operation()
        self.assertEqual(before, self.git("diff", "--cached"))
        self.assertEqual((self.root / "file").read_text(), "working\n")

    def test_untracked_refusals_invalid_names_and_no_hooks(self):
        (self.root / "file").write_text("base\n")
        self.commit()
        (self.root / "untracked").write_text("keep")
        with self.assertRaises(ValueError): create_branch(self.root, "new")
        (self.root / "untracked").unlink()
        for name in ["--orphan", "HEAD", "bad..name", "x\ny", "main"]:
            with self.assertRaises(ValueError): create_branch(self.root, name)
        hook = self.root / ".git/hooks/post-checkout"
        hook.write_text("#!/bin/sh\ntouch '" + str(self.root.parent / "hook-ran") + "'\n")
        hook.chmod(0o755)
        create_branch(self.root, "safe")
        self.assertFalse((self.root.parent / "hook-ran").exists())

    def test_external_diff_and_textconv_are_never_run(self):
        marker = self.root.parent / "external-ran"
        executable = self.root.parent / "external"
        executable.write_text("#!/bin/sh\ntouch '" + str(marker) + "'\n")
        executable.chmod(0o755)
        self.git("config", "diff.external", str(executable))
        self.git("config", "diff.custom.textconv", str(executable))
        (self.root / ".gitattributes").write_text("file diff=custom\n")
        (self.root / "file").write_text("before\n")
        self.commit()
        (self.root / "file").write_text("after\n")
        self.assertIn("+after", git_changes(self.root)["files"][0]["diff"])
        self.assertFalse(marker.exists())

    def test_rename_tab_counts_and_symlink(self):
        (self.root / "3\t4\tx").write_text("same\n" * 20)
        self.commit()
        self.git("mv", "--", "3\t4\tx", "8\t9\ty")
        row = git_changes(self.root)["files"][0]
        self.assertEqual((row["added"], row["deleted"]), (0, 0))
        (self.root / "link").symlink_to(self.root.parent / "missing")
        rows = {row["path"]: row for row in git_changes(self.root)["files"]}
        self.assertIn("missing", rows["link"]["diff"])

    def test_unborn_staged_then_deleted_is_empty_combined_diff(self):
        (self.root / "new").write_text("hello\n")
        self.git("add", "new")
        (self.root / "new").unlink()
        self.assertEqual(git_changes(self.root)["files"], [])


if __name__ == "__main__":
    unittest.main()
