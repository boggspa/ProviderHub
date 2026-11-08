"""Missing packaged modules must fail the build rather than Chat startup."""
from pathlib import Path
import sys
import tempfile
import unittest

from check_worker_imports import check


class WorkerBundleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.worker = Path(self.temp.name)
        for name in ("gateway", "chat_runtime", "chat_sessions", "chat_host"):
            (self.worker / (name + ".py")).write_text("")

    def test_missing_transitive_import_is_reported(self):
        (self.worker / "chat_runtime.py").write_text("import missing_chat_startup_dependency\n")
        with self.assertRaisesRegex(ValueError, "missing_chat_startup_dependency"):
            check(self.worker, sys.executable)

    def test_complete_bundle_imports_without_running_main_or_writing_caches(self):
        (self.worker / "chat_runtime.py").write_text(
            "import bundled_dependency\nif __name__ == '__main__': raise RuntimeError('started services')\n")
        (self.worker / "bundled_dependency.py").write_text("VALUE = 1\n")
        check(self.worker, sys.executable)
        self.assertFalse((self.worker / "__pycache__").exists())


if __name__ == "__main__":
    unittest.main()
