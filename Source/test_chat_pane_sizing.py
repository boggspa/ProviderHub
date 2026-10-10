"""Execute the production Swift sizing rules without opening an app."""
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

CASES = r'''
import Foundation
@main struct Cases {
    static func main() {
        func layout(_ window: CGFloat, _ rail: CGFloat = 190, _ inspector: CGFloat = 0,
                    _ visible: Bool = true, _ preferred: Bool = true) -> ChatPaneSizing.Layout {
            ChatPaneSizing.layout(window: window, railPreferred: preferred,
                inspectorVisible: visible, rail: rail, inspector: inspector)
        }
        precondition(layout(1600).rail == 190 && layout(1600).inspector == 360)
        precondition(layout(1600, 999, 999).rail == 360)
        precondition(layout(1600, 999, 999).inspector == 600)
        precondition(layout(1600, 1, 1).rail == 150 && layout(1600, 1, 1).inspector == 260)
        precondition(layout(720, 360, 600).transcript == 420)
        precondition(layout(720, 360, 600).rail == 0)
        precondition(layout(900, 360, 600, false).rail == 360)
        precondition(layout(859, 190, 0, false).rail == 0)
        precondition(layout(860, 190, 0, false).rail == 190)
        precondition(layout(1600, 190, 0, true, false).rail == 0)
        precondition(ChatPaneSizing.inspectorDefault(720) == 260)
        precondition(ChatPaneSizing.inspectorDefault(900) == 315)
        for window in stride(from: 720, through: 2400, by: 7) {
            for inspector in [CGFloat(0), 260, 360, 600, 999] {
                let sizes = layout(CGFloat(window), 360, inspector)
                precondition(sizes.transcript >= 420)
                precondition(sizes.inspector >= 260 && sizes.inspector <= 600)
                precondition(sizes.rail == 0 || (sizes.rail >= 150 && sizes.rail <= 360))
                let total = sizes.transcript + sizes.rail + sizes.inspector + 8 + (sizes.rail > 0 ? 8 : 0)
                precondition(abs(total - CGFloat(window)) < 0.001)
            }
        }
        print("pane sizing cases passed")
    }
}
'''

@unittest.skipUnless(sys.platform == "darwin" and shutil.which("xcrun"), "native Swift requires macOS")
class PaneSizingTests(unittest.TestCase):
    def test_production_sizing_bounds_defaults_and_resize(self):
        source = Path(__file__).parent
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cases = root / "Cases.swift"
            cases.write_text(CASES)
            binary = root / "cases"
            result = subprocess.run(["xcrun", "swiftc", "-parse-as-library", "-module-cache-path",
                str(root / "cache"), str(source / "ChatPaneSizing.swift"), str(cases), "-o", str(binary)],
                capture_output=True, text=True, timeout=120)
            self.assertEqual(result.returncode, 0, result.stderr)
            result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("pane sizing cases passed", result.stdout)
