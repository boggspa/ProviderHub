import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import codex_cli_agent
import codex_runtime
from bridge_core import BridgeError


NESTED = "Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex"
LEGACY = "Contents/Resources/codex"


class BundledRuntimePathTests(unittest.TestCase):
    """ChatGPT 26.924 moved the runtime; both layouts must still resolve."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.app = Path(temp.name) / "ChatGPT.app"

    def install(self, relative, executable=True):
        path = self.app / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("#!/bin/sh\n")
        os.chmod(path, 0o755 if executable else 0o644)
        return path

    def resolve(self):
        with patch.object(codex_runtime, "installed_app", return_value=str(self.app)):
            return codex_runtime.runtime_binary()

    def test_nested_codex_cli_layout(self):
        path = self.install(NESTED)
        self.assertEqual(self.resolve(), path)

    def test_legacy_layout(self):
        path = self.install(LEGACY)
        self.assertEqual(self.resolve(), path)

    def test_nested_wins_when_both_exist(self):
        self.install(LEGACY)
        path = self.install(NESTED)
        self.assertEqual(self.resolve(), path)

    def test_non_executable_or_missing_is_refused(self):
        with self.assertRaisesRegex(BridgeError, "no usable Codex runtime"):
            self.resolve()
        self.install(NESTED, executable=False)
        with self.assertRaisesRegex(BridgeError, "no usable Codex runtime"):
            self.resolve()

    def test_cli_route_falls_back_to_the_nested_desktop_runtime(self):
        home = self.app.parent
        path = self.install(NESTED)
        with patch.object(codex_cli_agent, "resolve_binary", return_value=None), \
                patch.object(codex_cli_agent.Path, "home", return_value=home), \
                patch.object(codex_cli_agent, "_desktop_runtime_candidates",
                             wraps=codex_cli_agent._desktop_runtime_candidates):
            candidates = codex_cli_agent._desktop_runtime_candidates()
            self.assertIn(home / "Applications/ChatGPT.app" / NESTED, candidates)
            self.assertLess(candidates.index(Path("/Applications/ChatGPT.app") / NESTED),
                            candidates.index(Path("/Applications/ChatGPT.app") / LEGACY))
        self.assertTrue(path.is_file())


if __name__ == "__main__":
    unittest.main()
