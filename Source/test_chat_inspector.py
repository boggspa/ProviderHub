import os
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from chat_inspector import (git_changes, git_branches, switch_branch, create_branch,
                            create_worktree, switch_worktree, decode_branch)


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

    def claim(self, text, name=".WORK-IN-PROGRESS-peer.md"):
        (self.root / ".git/info/exclude").write_text(".WORK-IN-PROGRESS*\n.work-guard/\n")
        marker = self.root / name
        marker.write_text("---\n" + text + "\n---\nDo not remove this claim.\n")
        self.assertEqual(self.git("status", "--porcelain"), b"")
        return marker

    def test_live_claim_blocks_every_branch_mutation_even_with_clean_files(self):
        (self.root / "file").write_text("untouched\n"); self.commit()
        self.git("branch", "other")
        now = datetime.now(timezone.utc)
        marker = self.claim(f"pid: {os.getpid()}\nstarted: {now.isoformat()}\nexpires: {(now + timedelta(minutes=10)).isoformat()}\npaths:\n  - file")
        before = self.git("rev-parse", "HEAD")
        destination = self.root.parent / "peer-tree"
        for action in [lambda: switch_branch(self.root, "other"), lambda: create_branch(self.root, "new"),
                       lambda: create_worktree(self.root, "new-tree", destination)]:
            with self.assertRaisesRegex(ValueError, "Active work claim"): action()
        self.assertEqual(self.git("rev-parse", "HEAD"), before)
        self.assertEqual(self.git("branch", "--show-current").strip(), b"main")
        self.assertTrue(marker.exists()); self.assertFalse(destination.exists())
        self.assertEqual((self.root / "file").read_text(), "untouched\n")

    def test_expired_manual_claim_decays_but_runtime_projection_stays_blocked(self):
        (self.root / "file").write_text("base\n"); self.commit()
        now = datetime.now(timezone.utc)
        started = now - timedelta(minutes=21)
        marker = self.claim(f"pid: {os.getpid()}\nstarted: {started.isoformat()}\nexpires: {(now + timedelta(hours=2)).isoformat()}")
        self.assertEqual(create_branch(self.root, "manual-expired")["current"], "manual-expired")
        self.assertTrue(marker.exists())
        runtime = self.claim("pid: 99999999\nexpires: 2000-01-01T00:00:00Z", ".WORK-IN-PROGRESS-taskwraith-runtime-incomplete.md")
        with self.assertRaisesRegex(ValueError, "Active work claim"): switch_branch(self.root, "main")
        self.assertTrue(runtime.exists())

    def test_owner_only_claim_heartbeat_and_declared_worktree_scope(self):
        (self.root / "file").write_text("base\n"); self.commit()
        now = datetime.now(timezone.utc)
        lease = f"started: '{now.isoformat()}'\nexpires: '{(now + timedelta(minutes=10)).isoformat()}'"
        marker = self.claim(lease + '\nlockOwnerId: "owner-from-host"')
        with self.assertRaisesRegex(ValueError, "Active work claim"): create_branch(self.root, "new")
        marker.write_text("---\n" + lease + "\npid: 99999999\n---\n")
        heartbeat = self.root / ".work-guard/heartbeat.json"
        heartbeat.parent.mkdir()
        heartbeat.write_text(json.dumps({"schemaVersion": 2, "markers": {marker.name: {"lastSeen": now.timestamp() * 1000}}}))
        with self.assertRaisesRegex(ValueError, "Active work claim"): create_branch(self.root, "new")
        marker.write_text("---\n" + lease + f"\npid: {os.getpid()}\nworktree: {self.root.parent / 'different'}\n---\n")
        self.assertEqual(create_branch(self.root, "new")["current"], "new")

    def test_non_utf8_paths_stay_lossless_for_git_and_valid_for_json(self):
        seen = []
        raw_names = [b"bad-\xff", b"bad-\\xff"]
        def git(root, *args, **kwargs):
            if "--raw" in args:
                return b"".join(b":100644 100644 a b M\0" + path + b"\0" for path in raw_names), False, 0
            if args[0] == "ls-files": return b"", False, 0
            if "--numstat" in args: return b"1\t1\t" + os.fsencode(args[-1]) + b"\0", False, 0
            if "--unified=3" in args:
                seen.append(os.fsencode(args[-1]))
                return b"@@ -1 +1 @@\n-before\n+after\n", False, 0
            return b"", False, 0
        with patch("chat_inspector._root", return_value=str(self.root)), patch("chat_inspector._git", side_effect=git):
            result = git_changes(self.root)
        self.assertEqual(seen, raw_names)
        wire = json.dumps(result, ensure_ascii=False).encode("utf-8")
        self.assertEqual(len(json.loads(wire)["files"]), 2)
        self.assertEqual(len({row["path"] for row in result["files"]}), 2)
        self.assertTrue(all(row["added"] == row["deleted"] == 1 for row in result["files"]))

    def test_packed_non_utf8_ref_can_be_listed_and_switched_by_exact_bytes(self):
        (self.root / "file").write_text("base\n"); self.commit()
        revision = self.git("rev-parse", "HEAD").strip()
        raw = b"topic-\xff"
        (self.root / ".git/packed-refs").write_bytes(revision + b" refs/heads/" + raw + b"\n")
        listing = git_branches(self.root)
        encoded = next(row for row in listing["branches"] if row["nameBytes"] == raw.hex())
        self.assertEqual(encoded["name"], "topic-\\xff")
        json.dumps(listing, ensure_ascii=False).encode("utf-8")
        result = switch_branch(self.root, decode_branch(encoded["nameBytes"]))
        self.assertEqual(result["current"], "topic-\\xff")
        self.assertEqual(self.git("symbolic-ref", "--short", "HEAD").strip(), raw)
        self.assertEqual(switch_branch(self.root, "main")["current"], "main")
        for token in [None, "", "0", "not-hex", "00"]:
            if token == "00":
                with self.assertRaises(ValueError): switch_branch(self.root, decode_branch(token))
            else:
                with self.assertRaises(ValueError): decode_branch(token)

    def test_unrepresentable_worktree_does_not_break_usable_entries(self):
        root = os.fsencode(str(self.root))
        def complete(workspace, *args):
            if args[0] == "worktree": return b"worktree " + root + b"\0branch refs/heads/main\0\0worktree /bad-\xff\0branch refs/heads/topic-\xff\0\0"
            if args[0] == "for-each-ref": return b"refs/heads/main\nrefs/heads/topic-\xff\n"
            raise AssertionError(args)
        with patch("chat_inspector._root", return_value=str(self.root)), patch("chat_inspector._complete", side_effect=complete), patch("chat_inspector._git", return_value=(b"main\n", False, 0)):
            result = git_branches(self.root)
        wire = json.dumps(result, ensure_ascii=False).encode("utf-8")
        self.assertEqual(len(json.loads(wire)["worktrees"]), 2)
        self.assertTrue(result["worktrees"][0]["selectable"])
        self.assertFalse(result["worktrees"][1]["selectable"])
        self.assertEqual(bytes.fromhex(result["worktrees"][1]["pathBytes"]), b"/bad-\xff")

    def test_linked_worktree_honors_claim_at_primary_checkout(self):
        (self.root / "file").write_text("base\n"); self.commit()
        self.git("branch", "other")
        linked = self.root.parent / "linked"
        create_worktree(self.root, "linked", linked)
        now = datetime.now(timezone.utc)
        lease = f"pid: {os.getpid()}\nstarted: {now.isoformat()}\nexpires: {(now + timedelta(minutes=10)).isoformat()}"
        marker = self.claim(lease + f"\nworktree: {linked}")
        self.assertEqual(subprocess.check_output(["git", "-C", str(linked), "status", "--porcelain"]), b"")
        for action in [lambda: switch_branch(linked, "other"), lambda: create_branch(linked, "new"),
                       lambda: create_worktree(linked, "nested", self.root.parent / "nested")]:
            with self.assertRaisesRegex(ValueError, "Active work claim"): action()
        self.assertEqual(git_branches(linked)["current"], "linked")
        # A promise for the primary checkout alone must not block unrelated
        # work in an isolated linked checkout.
        marker.write_text("---\n" + lease + "\n---\n")
        self.assertEqual(create_branch(linked, "independent")["current"], "independent")
        self.assertTrue(marker.exists())
        self.assertEqual(git_branches(self.root)["current"], "main")


if __name__ == "__main__":
    unittest.main()
