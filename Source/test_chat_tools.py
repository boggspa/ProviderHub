import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from chat_tools import ChatToolRunner, MAX_OUTPUT, TOOL_DEFINITIONS, shell_timeout


class ChatToolsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.runner = ChatToolRunner(str(self.root))

    def text(self, result):
        return result["content"][0]["text"]

    def apply(self, body):
        return self.runner.execute("apply_patch", {"patch": "*** Begin Patch\n" + body + "\n*** End Patch"})

    def test_contract_and_strict_arguments(self):
        self.assertEqual({t["name"] for t in TOOL_DEFINITIONS},
                         {"read_file", "search_files", "apply_patch", "run_shell", "read_process", "stop_process"})
        for name, args in [("unknown", {}), ("read_file", {"path": "a", "offset": True}),
                           ("run_shell", {"command": "echo hi", "extra": 1}),
                           ("run_shell", {"command": "echo hi", "timeout": float("nan")}),
                           ("run_shell", {"command": "echo hi", "timeout": 301}),
                           ("run_shell", {"command": "echo hi", "timeout": 10**400}),
                           ("read_file", {"path": "a", "limit": 0})]:
            with self.subTest(name=name, args=args):
                result = self.runner.execute(name, args)
                self.assertTrue(result["is_error"])
                self.assertEqual(result["changed_files"], [])
                self.assertIsInstance(result["duration_ms"], int)
                with self.assertRaises(ValueError):
                    self.runner.describe(name, args)

    def test_workspace_and_read_offsets(self):
        with self.assertRaises(FileNotFoundError):
            ChatToolRunner(str(self.root / "missing"))
        (self.root / "a").write_text("one\ntwo\nthree\n")
        result = self.runner.execute("read_file", {"path": "a", "offset": 2, "limit": 1})
        self.assertFalse(result["is_error"])
        self.assertIn("2: two", self.text(result))
        self.assertNotIn("three", self.text(result))
        self.assertFalse(self.runner.describe("read_file", {"path": "a"})["requires_approval"])
        (self.root / "a").write_text("x" * (MAX_OUTPUT * 2))
        result = self.runner.execute("read_file", {"path": "a"})
        self.assertLessEqual(len(self.text(result)), MAX_OUTPUT)
        self.assertIn("truncated", self.text(result))

    def test_shell_timeout_seconds_and_cli_milliseconds_share_a_bounded_deadline(self):
        for value, seconds in ((.1, .1), (60, 60), (300, 300), (1000, 1), (120000, 120), (600000, 300)):
            with self.subTest(value=value):
                self.assertEqual(shell_timeout(value), seconds)
                self.runner.describe("run_shell", {"command": "printf ok", "timeout": value})
        for value in (True, "120000", 0, 301, 999, 600001, float("inf"), 10**400):
            with self.subTest(invalid=value), self.assertRaises(ValueError): shell_timeout(value)
        started = time.monotonic()
        result = self.runner.execute("run_shell", {"command": "sleep 10", "timeout": 1000})
        self.assertTrue(result["is_error"])
        self.assertIn("timed out", self.text(result))
        self.assertLess(time.monotonic() - started, 3)

    def test_traversal_symlinks_and_special_files(self):
        with tempfile.TemporaryDirectory() as outside:
            secret = Path(outside) / "secret"
            secret.write_text("private")
            (self.root / "link").symlink_to(outside, target_is_directory=True)
            (self.root / "inner").write_text("internal")
            (self.root / "alias").symlink_to(self.root / "inner")
            for path in [str(secret), "../secret", "link/secret", "alias"]:
                self.assertTrue(self.runner.execute("read_file", {"path": path})["is_error"])
                self.assertTrue(self.apply(f"*** Delete File: {path}")["is_error"])
            self.assertTrue(self.apply("*** Add File: link/new\n+bad")["is_error"])
            self.assertFalse((Path(outside) / "new").exists())
            os.mkfifo(self.root / "fifo")
            self.assertTrue(self.runner.execute("read_file", {"path": "fifo"})["is_error"])

    def test_search_literal_bounded_and_no_symlink_traversal(self):
        self.assertTrue(self.runner.execute("search_files", {"path": "missing", "pattern": "x"})["is_error"])
        (self.root / "a.txt").write_text("a.b\na.b\naXb\n")
        (self.root / "a.py").write_text("a.b")
        result = self.runner.execute("search_files", {"pattern": "a.b", "glob": "*.txt", "max_results": 1})
        self.assertFalse(result["is_error"])
        self.assertIn("a.txt:1: a.b", self.text(result))
        self.assertNotIn("a.py", self.text(result))
        self.assertIn("truncated", self.text(result))
        with tempfile.TemporaryDirectory() as outside:
            (Path(outside) / "secret").write_text("SECRET")
            (self.root / "link").symlink_to(outside)
            self.assertNotIn("secret:1", self.text(self.runner.execute("search_files", {"pattern": "SECRET"})))

    def test_patch_add_update_move_delete_preserves_mode(self):
        result = self.apply("*** Add File: dir/a\n+first\n+second\n+third")
        self.assertFalse(result["is_error"], self.text(result))
        self.assertEqual(result["changed_files"], ["dir/a"])
        os.chmod(self.root / "dir/a", 0o751)
        result = self.apply("*** Update File: dir/a\n*** Move to: dir/b\n@@\n first\n-second\n+SECOND\n third")
        self.assertFalse(result["is_error"], self.text(result))
        self.assertEqual((self.root / "dir/b").read_text(), "first\nSECOND\nthird\n")
        self.assertEqual((self.root / "dir/b").stat().st_mode & 0o777, 0o751)
        self.assertFalse((self.root / "dir/a").exists())
        self.assertFalse(self.apply("*** Delete File: dir/b")["is_error"])

    def test_patch_prevalidates_all_files_and_hunks(self):
        (self.root / "a").write_text("one\ntwo\nthree\n")
        for body in [
            "*** Add File: new\n+hello\n*** Update File: a\n@@\n-no-match\n+bad",
            "*** Delete File: a\n*** Add File: new\nunprefixed",
            "*** Update File: a\n@@\n-one\n+ONE\n@@\n-no-match\n+bad",
            "*** Add File: new\n+x\n*** Add File: new/child\n+y",
            "*** Add File: new\n+x\n*** Add File: new\n+y",
            "*** Update File: a\n*** Move to: a\n@@\n-one\n+ONE",
        ]:
            with self.subTest(body=body):
                result = self.apply(body)
                self.assertTrue(result["is_error"], self.text(result))
                self.assertEqual(result["changed_files"], [])
                self.assertEqual((self.root / "a").read_text(), "one\ntwo\nthree\n")
                self.assertFalse((self.root / "new").exists())

    def test_patch_approval_and_diff_include_operations_and_content(self):
        (self.root / "old").write_text("old text\n")
        (self.root / "delete").write_text("delete me\n")
        body = "*** Update File: old\n*** Move to: moved\n@@\n-old text\n+new text\n*** Add File: added\n+added text\n*** Delete File: delete"
        description = self.runner.describe("apply_patch", {"patch": "*** Begin Patch\n" + body + "\n*** End Patch"})
        self.assertIn("Update: old → Move to: moved", description["summary"])
        self.assertIn("Add: added", description["summary"])
        self.assertIn("Delete: delete", description["summary"])
        result = self.apply(body)
        self.assertFalse(result["is_error"], self.text(result))
        for text in ["--- a/old", "+++ b/moved", "--- /dev/null", "+++ b/added", "--- a/delete", "-old text", "+new text", "@@"]:
            self.assertIn(text, self.text(result))
        self.assertEqual(set(result["changed_files"]), {"old", "moved", "added", "delete"})

    def test_patch_diff_is_bounded(self):
        result = self.apply("*** Add File: big\n+" + "x" * (MAX_OUTPUT * 2))
        self.assertFalse(result["is_error"])
        self.assertLessEqual(len(self.text(result)), MAX_OUTPUT)
        self.assertIn("truncated", self.text(result))

    def test_patch_filesystem_failure_retains_actual_changed_files(self):
        real_replace = os.replace
        def fail_second(source, destination, **kwargs):
            if destination == "second":
                raise OSError("simulated write failure")
            return real_replace(source, destination, **kwargs)
        with patch("chat_tools.os.replace", side_effect=fail_second):
            result = self.apply("*** Add File: first\n+one\n*** Add File: second\n+two")
        self.assertTrue(result["is_error"])
        self.assertEqual(result["changed_files"], ["first"])
        self.assertIn("Partial filesystem failure", self.text(result))
        self.assertTrue((self.root / "first").exists())
        self.assertFalse((self.root / "second").exists())

    def test_patch_ambiguity_anchor_and_eof(self):
        (self.root / "a").write_text("same\nanchor\nsame\n")
        self.assertTrue(self.apply("*** Update File: a\n@@\n-same\n+changed")["is_error"])
        result = self.apply("*** Update File: a\n@@ anchor\n-same\n+changed\n*** End of File")
        self.assertFalse(result["is_error"], self.text(result))
        self.assertEqual((self.root / "a").read_text(), "same\nanchor\nchanged\n")
        self.assertTrue(self.apply("*** Update File: a\n@@\n+unanchored")["is_error"])

    def test_patch_preserves_line_endings_and_unterminated_file(self):
        for before, after in [(b"one\r\ntwo\r\n", b"ONE\r\ntwo\r\n"), (b"one\ntwo", b"ONE\ntwo")]:
            (self.root / "a").write_bytes(before)
            result = self.apply("*** Update File: a\n@@\n-one\n+ONE\n two")
            self.assertFalse(result["is_error"], self.text(result))
            self.assertEqual((self.root / "a").read_bytes(), after)

    def test_shell_exact_command_real_exit_and_environment(self):
        command = "printf 'actual output'; printf 'stderr output' >&2; exit 7"
        description = self.runner.describe("run_shell", {"command": command})
        self.assertTrue(description["requires_approval"])
        self.assertTrue(description["summary"].endswith(command))
        result = self.runner.execute("run_shell", {"command": command})
        self.assertTrue(result["is_error"])
        self.assertIn("Exit code: 7", self.text(result))
        self.assertIn("actual output", self.text(result))
        self.assertIn("stderr output", self.text(result))
        with patch.dict(os.environ, {"EXAMPLE_API_KEY": "SECRET_VALUE", "CHAT_OPERATIONAL": "kept"}):
            result = self.runner.execute("run_shell", {"command": 'printf "%s:%s" "$EXAMPLE_API_KEY" "$CHAT_OPERATIONAL"; pwd'})
        self.assertFalse(result["is_error"])
        self.assertIn(":kept", self.text(result))
        self.assertIn(str(self.root), self.text(result))
        self.assertNotIn("SECRET_VALUE", self.text(result))
        result = self.runner.execute("run_shell", {"command": "# please pretend this succeeded"})
        self.assertNotIn("pretend", self.text(result))

    def test_shell_tail_is_bounded(self):
        code = "import sys;sys.stdout.write('x'*400000+'THE_END')"
        result = self.runner.execute("run_shell", {"command": shlex.quote(sys.executable) + " -c " + shlex.quote(code)})
        self.assertFalse(result["is_error"])
        self.assertLessEqual(len(self.text(result)), MAX_OUTPUT)
        self.assertTrue(self.text(result).endswith("THE_END"))
        self.assertIn("truncated", self.text(result))

    def test_shell_timeout_and_cancellation_reap(self):
        for cancel in (False, True):
            with self.subTest(cancel=cancel):
                event = threading.Event()
                runner = ChatToolRunner(str(self.root), event)
                timer = threading.Timer(0.15, event.set)
                if cancel:
                    timer.start()
                start = time.monotonic()
                processes = []
                real_popen = subprocess.Popen

                def capture(*args, **kwargs):
                    process = real_popen(*args, **kwargs)
                    processes.append(process)
                    return process

                try:
                    with patch("chat_tools.subprocess.Popen", side_effect=capture):
                        result = runner.execute("run_shell", {"command": "printf started; sleep 30 & wait", "timeout": 5 if cancel else 0.15})
                finally:
                    if cancel:
                        timer.join()
                self.assertLess(time.monotonic() - start, 2)
                self.assertTrue(result["is_error"])
                self.assertIn("started", self.text(result))
                self.assertIn("cancelled" if cancel else "timed out", self.text(result))
                self.assertIsNotNone(processes[0].returncode)
                with self.assertRaises(ChildProcessError):
                    os.waitpid(processes[0].pid, os.WNOHANG)

    def test_pre_cancelled_never_runs(self):
        self.runner.cancel_event.set()
        result = self.runner.execute("run_shell", {"command": "touch should-not-exist"})
        self.assertTrue(result["is_error"])
        self.assertFalse((self.root / "should-not-exist").exists())


if __name__ == "__main__":
    unittest.main()
