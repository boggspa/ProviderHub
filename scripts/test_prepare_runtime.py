"""Tests for the runtime neutralizer that gates the release build.

`scripts/prepare_runtime.py` fails the build when a personal path survives or
the pruned runtime cannot start, so its decision rules need their own tests:
a gate that silently stops detecting leaks is worse than no gate.

Run with: uv run python scripts/test_prepare_runtime.py
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import prepare_runtime as pr


class LeakScanTests(unittest.TestCase):
    """The gate that fails the build."""

    def _tree(self, files):
        tmp = tempfile.mkdtemp(prefix="leakscan-")
        root = Path(tmp)
        for rel, data in files.items():
            path = root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        return root

    def test_personal_path_in_text_is_detected(self):
        root = self._tree({"lib/x.py": b'prefix = "/Users/someone/build/cpython"'})
        self.assertEqual(pr.leak_scan(root), ["lib/x.py"])

    def test_personal_path_in_a_binary_is_detected(self):
        # The real case: a Mach-O LC_ID_DYLIB install name, not text.
        root = self._tree({"lib/libpython3.13.dylib": b"\xcf\xfa\xed\xfe/Users/me/build/lib/x.dylib"})
        self.assertEqual(pr.leak_scan(root), ["lib/libpython3.13.dylib"])

    def test_upstream_ci_path_is_permitted(self):
        # /Users/runner/... is a GitHub Actions path inside third-party wheels.
        root = self._tree({"lib/y.so": b"built at /Users/runner/work/cffi/cffi/src/c"})
        self.assertEqual(pr.leak_scan(root), [])

    def test_clean_tree_passes(self):
        root = self._tree({"lib/z.py": b'prefix = "/install"'})
        self.assertEqual(pr.leak_scan(root), [])


class RewriteTextTests(unittest.TestCase):
    def test_recorded_prefix_is_replaced(self):
        prefix = "/Users/builder/work/py/cpython-3.13.13-macos-aarch64-none"
        out = pr.rewrite_text(f'"prefix": "{prefix}",\n"LIBDEST": "{prefix}/lib",\n', prefix)
        self.assertNotIn("builder", out)
        self.assertEqual(out, '"prefix": "/install",\n"LIBDEST": "/install/lib",\n')

    def test_per_user_tmpdir_is_replaced(self):
        out = pr.rewrite_text('abs_srcdir = "/private/var/folders/1r/xy/T/tmpgucoaz3y/src"', "")
        self.assertEqual(out, 'abs_srcdir = "/install/src"')

    def test_upstream_runner_path_is_untouched(self):
        text = 'built = "/Users/runner/work/cpython/build"'
        self.assertEqual(pr.rewrite_text(text, ""), text)

    def test_unrelated_absolute_paths_are_untouched(self):
        text = 'x = "/opt/homebrew/bin"\ny = "/usr/lib/libSystem.B.dylib"\n'
        self.assertEqual(pr.rewrite_text(text, ""), text)

    def test_relative_prefix_is_ignored(self):
        # A prefix that is not absolute must not be substituted blindly.
        self.assertEqual(pr.rewrite_text("keep prefix as-is", "prefix"), "keep prefix as-is")


class PrunePatternTests(unittest.TestCase):
    def test_dead_subtrees_match(self):
        for rel in ("include", "share", "lib/pkgconfig", "lib/itcl4.3.5",
                    "lib/tcl9.0", "lib/tk9.0", "lib/thread3.0.4",
                    "lib/python3.13/config-3.13-darwin", "lib/python3.13/ensurepip",
                    "lib/python3.13/idlelib", "lib/python3.13/venv",
                    "lib/python3.13/tkinter", "lib/python3.13/site-packages/pip",
                    "lib/python3.13/site-packages/pip-26.1.dist-info",
                    "lib/python3.13/site-packages/bin"):
            with self.subTest(rel=rel):
                self.assertTrue(pr._matches(rel, pr.PRUNE_DIR_PATTERNS))

    def test_required_subtrees_do_not_match(self):
        for rel in ("bin", "lib", "lib/python3.13", "lib/python3.13/encodings",
                    "lib/python3.13/lib-dynload", "lib/python3.13/unittest",
                    "lib/python3.13/site-packages",
                    "lib/python3.13/site-packages/cryptography",
                    "lib/python3.13/site-packages/cryptography-50.0.0.dist-info",
                    "lib/python3.13/site-packages/cffi"):
            with self.subTest(rel=rel):
                self.assertFalse(pr._matches(rel, pr.PRUNE_DIR_PATTERNS))

    def test_dead_binaries_match(self):
        for rel in ("lib/libpython3.13.dylib", "lib/libtcl9.0.dylib",
                    "lib/libtcl9tk9.0.dylib",
                    "lib/python3.13/lib-dynload/_tkinter.cpython-313-darwin.so"):
            with self.subTest(rel=rel):
                self.assertTrue(pr._matches(rel, pr.PRUNE_FILE_PATTERNS))

    def test_required_binaries_are_kept(self):
        for rel in ("lib/python3.13/lib-dynload/_ssl.cpython-313-darwin.so",
                    "lib/python3.13/lib-dynload/_socket.cpython-313-darwin.so",
                    "lib/python3.13/site-packages/_cffi_backend.cpython-313-darwin.so",
                    "lib/python3.13/site-packages/cryptography/hazmat/bindings/_rust.abi3.so"):
            with self.subTest(rel=rel):
                self.assertFalse(pr._matches(rel, pr.PRUNE_FILE_PATTERNS))


class BinKeepTests(unittest.TestCase):
    def test_interpreters_are_kept(self):
        for name in ("python", "python3", "python3.13", "python3.14"):
            with self.subTest(name=name):
                self.assertTrue(pr.BIN_KEEP_RE.match(name))

    def test_development_tools_are_dropped(self):
        for name in ("pip", "pip3", "pip3.13", "idle3", "idle3.13", "pydoc3",
                     "pydoc3.13", "python3-config", "python3.13-config", "2to3"):
            with self.subTest(name=name):
                self.assertFalse(pr.BIN_KEEP_RE.match(name))


class PruneEndToEndTests(unittest.TestCase):
    """prune() against a synthetic runtime shaped like the real one."""

    def test_prunes_dead_subtrees_and_keeps_the_interpreter(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "python"
            keep = ["bin/python3.13", "lib/python3.13/os.py",
                    "lib/python3.13/encodings/utf_8.py",
                    "lib/python3.13/lib-dynload/_ssl.cpython-313-darwin.so",
                    "lib/python3.13/unittest/case.py",
                    "lib/python3.13/site-packages/_cffi_backend.cpython-313-darwin.so",
                    "lib/python3.13/site-packages/cryptography/fernet.py",
                    "lib/python3.13/site-packages/cffi/api.py"]
            drop = ["include/python3.13/Python.h", "share/man/man1/python3.1",
                    "lib/pkgconfig/python-3.13.pc", "lib/libpython3.13.dylib",
                    "lib/tcl9.0/init.tcl", "lib/python3.13/config-3.13-darwin/Makefile",
                    "lib/python3.13/ensurepip/__init__.py",
                    "lib/python3.13/idlelib/__init__.py",
                    "lib/python3.13/venv/__init__.py",
                    "lib/python3.13/tkinter/__init__.py",
                    "lib/python3.13/lib-dynload/_tkinter.cpython-313-darwin.so",
                    "lib/python3.13/site-packages/pip/__init__.py",
                    "lib/python3.13/site-packages/bin/cffi-gen-src",
                    "lib/python3.13/encodings/__pycache__/utf_8.cpython-313.pyc",
                    "bin/pip3.13", "bin/idle3", "bin/python3.13-config"]
            for rel in keep + drop:
                path = root / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("x")
            (root / "bin" / "python3").symlink_to("python3.13")

            pr.prune(root)

            for rel in keep:
                self.assertTrue((root / rel).exists(), f"wrongly pruned {rel}")
            for rel in drop:
                self.assertFalse((root / rel).exists(), f"not pruned {rel}")
            self.assertTrue((root / "bin" / "python3").is_symlink(),
                            "the bin/python3 symlink findPython() relies on was lost")
            self.assertEqual(sorted(p.name for p in (root / "bin").iterdir()),
                             ["python3", "python3.13"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
