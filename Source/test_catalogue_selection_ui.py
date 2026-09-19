"""Exercise the actual Swift catalogue migration without starting the Hub."""
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


@unittest.skipUnless(shutil.which("xcrun"), "Swift UI migration tests require the macOS toolchain")
class CatalogueSelectionUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory(prefix="hub-catalogue-selection-")
        cls.addClassCleanup(cls.directory.cleanup)
        root = Path(cls.directory.name)
        harness = root / "main.swift"
        harness.write_text('''import Foundation
struct Input: Decodable {
    var mappings: [String: String]
    var options: [String: MappingOptions]
}
let input = try JSONDecoder().decode(Input.self, from: FileHandle.standardInput.readDataToEndOfFile())
let entries = seedClaudeCatalogue(mappings: input.mappings, options: input.options)
FileHandle.standardOutput.write(try JSONEncoder().encode(entries))
''')
        cls.executable = root / "catalogue-selection"
        subprocess.run(
            ["xcrun", "swiftc", "-swift-version", "5",
             "-module-cache-path", str(root / "ModuleCache"),
             str(Path(__file__).with_name("CatalogueSelection.swift")),
             str(harness), "-o", str(cls.executable)],
            check=True, capture_output=True, text=True, timeout=120,
        )

    def seed(self, mappings, thresholds=None):
        options = {
            slot: {"omit_system": False, "omit_tools": False, "compact_limit": threshold}
            for slot, threshold in (thresholds or {}).items()
        }
        result = subprocess.run(
            [str(self.executable)],
            input=json.dumps({"mappings": mappings, "options": options}),
            check=True, capture_output=True, text=True, timeout=10,
        )
        return json.loads(result.stdout)

    def test_preserves_distinct_selections_in_slot_order_with_their_thresholds(self):
        rows = self.seed({
            "claude-sonnet-4-6": "mistral/small",
            "claude-haiku-4-5": "kimi/fast",
            "claude-opus-5": "kimi/k3",
            "claude-sonnet-5": "mistral/medium",
            "claude-fable-5": "mistral/large",
        }, {"claude-opus-5": 120_000, "claude-sonnet-4-6": 60_000})
        self.assertEqual(rows, [
            {"route": "mistral/large", "tier": "fable", "tier_default": True},
            {"route": "kimi/k3", "tier": "opus", "tier_default": True, "compact_limit": 120_000},
            {"route": "mistral/medium", "tier": "sonnet", "tier_default": True},
            {"route": "kimi/fast", "tier": "haiku", "tier_default": True},
            {"route": "mistral/small", "tier": "sonnet", "tier_default": False, "compact_limit": 60_000},
        ])

    def test_duplicate_route_keeps_first_tier_and_smallest_explicit_threshold(self):
        rows = self.seed({
            "claude-fable-5": "mistral/shared",
            "claude-opus-5": "mistral/shared",
            "claude-sonnet-5": "mistral/shared",
            "claude-haiku-4-5": "mistral/shared",
        }, {
            "claude-opus-5": 120_000,
            "claude-sonnet-5": 80_000,
            "claude-haiku-4-5": 100_000,
        })
        self.assertEqual(rows, [{
            "route": "mistral/shared", "tier": "fable",
            "tier_default": True, "compact_limit": 80_000,
        }])

    def test_deduplicated_sonnet_slot_does_not_consume_another_rows_default(self):
        rows = self.seed({
            "claude-fable-5": "mistral/shared",
            "claude-sonnet-5": "mistral/shared",
            "claude-sonnet-4-6": "mistral/sonnet",
        })
        self.assertEqual(rows, [
            {"route": "mistral/shared", "tier": "fable", "tier_default": True},
            {"route": "mistral/sonnet", "tier": "sonnet", "tier_default": True},
        ])

    def test_empty_or_unrecognised_slots_do_not_create_catalogue_entries(self):
        self.assertEqual(self.seed({}), [])
        self.assertEqual(self.seed({"claude-fable-5": "", "future-slot": "mistral/large"}), [])


@unittest.skipUnless(shutil.which("xcrun"), "Swift save classification requires the macOS toolchain")
class SettingsChangeClassificationTests(unittest.TestCase):
    def test_combined_preferences_keep_codex_session_restrictions(self):
        # Execute the production property with its actual RouteSettings value
        # type. This catches a benign preference masking a Codex mutation.
        source = Path(__file__).with_name("MistralBridge.swift").read_text()
        classifier = source[source.index("    enum ChangeKind {"):source.index("    var routeOptions:")]
        slot_source = source[source.index("let slots:"):source.index("/// Family tier")]
        harness_source = "import Foundation\n" + slot_source + "\nstruct Probe {\n"
        harness_source += "var settings = RouteSettings()\nvar savedSettings = RouteSettings()\n" + classifier + "}\n"
        harness_source += '''
var result: [String] = []
var unchanged = Probe()
result.append(String(describing: unchanged.changeKind))
var prefs = Probe()
prefs.settings.auto_stop = false
result.append(String(describing: prefs.changeKind))
var codex = Probe()
codex.settings.codex_model = "mistral/new-model"
result.append(String(describing: codex.changeKind))
codex.settings.auto_stop = false
result.append(String(describing: codex.changeKind))
codex.settings.claude_features.dictation = true
result.append(String(describing: codex.changeKind))
var routing = Probe()
routing.settings.mappings["claude-opus-5"] = "kimi/k3"
result.append(String(describing: routing.changeKind))
routing.settings.codex_goal_budget = true
result.append(String(describing: routing.changeKind))
FileHandle.standardOutput.write(try JSONEncoder().encode(result))
'''
        with tempfile.TemporaryDirectory(prefix="hub-settings-classification-") as tmp:
            root = Path(tmp)
            harness = root / "main.swift"
            harness.write_text(harness_source)
            executable = root / "classify"
            subprocess.run([
                "xcrun", "swiftc", "-swift-version", "5", "-module-cache-path", str(root / "ModuleCache"),
                str(Path(__file__).with_name("CatalogueSelection.swift")),
                str(Path(__file__).with_name("HubModels.swift")), str(harness), "-o", str(executable),
            ], check=True, capture_output=True, text=True, timeout=120)
            result = subprocess.run([str(executable)], check=True, capture_output=True, text=True, timeout=10)
        self.assertEqual(json.loads(result.stdout), [
            "unchanged", "prefs", "codexOnly", "codexOnly", "codexOnly", "claudeRouting", "mixed",
        ])


if __name__ == "__main__":
    unittest.main()
