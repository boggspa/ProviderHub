"""Release provenance must describe the runtime after native code is signed."""
import hashlib
import json
from pathlib import Path
import plistlib
import struct
import tempfile
import unittest
from unittest.mock import patch

from build_provenance import MANIFEST_NAME, _runtime_facts, verify_manifest
import package_macos


class PackageMacTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.app = self.root / "Provider Hub.app"
        resources = self.app / "Contents/Resources"
        worker = resources / "worker/agent.py"
        worker.parent.mkdir(parents=True)
        worker.write_bytes(b"print('worker')\n")
        self.python = resources / "python/bin/python3"
        self.python.parent.mkdir(parents=True)
        self.python.write_bytes(struct.pack("<4I", 0xFEEDFACF, 0, 0, 2) + b"unsigned")
        self.metadata = {"CFBundleShortVersionString": "0.5.5", "CFBundleVersion": "25"}
        (self.app / "Contents/Info.plist").write_bytes(plistlib.dumps(self.metadata))
        self.manifest_path = resources / MANIFEST_NAME
        self.manifest = {
            "source": {"revision": "a" * 40, "dirty": False, "verification": "git-head"},
            "worker": {"sha256": {"agent.py": hashlib.sha256(worker.read_bytes()).hexdigest()}},
            "application": {"version": "0.5.5", "build": "25"},
            "runtime": _runtime_facts(self.app),
        }
        self.manifest_path.write_text(json.dumps(self.manifest))
        self.arguments = [
            "package_macos.py", "--app", str(self.app), "--identity", "A" * 40,
            "--output", str(self.root / "release.zip"),
        ]

    def test_outer_signature_seals_the_post_signing_runtime_digest(self):
        signed = []
        sealed_manifest = None

        def run(arguments):
            nonlocal sealed_manifest
            if arguments[:2] == ["codesign", "--force"]:
                target = Path(arguments[-1])
                signed.append(target)
                if target == self.app:
                    # What the outer signature seals must already verify.
                    verify_manifest(self.app, require_clean=True)
                    sealed_manifest = self.manifest_path.read_bytes()
                else:
                    target.write_bytes(target.read_bytes() + b"-developer-id-signature")
            return ""

        with patch("sys.argv", self.arguments), patch.object(package_macos, "run", side_effect=run), \
                patch.object(package_macos, "archive") as archive:
            package_macos.main()
        self.assertEqual(signed, [self.python, self.app])
        final = verify_manifest(self.app, require_clean=True)
        self.assertNotEqual(final["runtime"]["sha256"], self.manifest["runtime"]["sha256"])
        self.assertEqual(final["source"], self.manifest["source"])
        self.assertEqual(self.manifest_path.read_bytes(), sealed_manifest)
        archive.assert_called_once()

    def test_packaging_rejects_preexisting_runtime_changes_before_signing(self):
        self.python.write_bytes(self.python.read_bytes() + b"-unexpected-edit")
        with patch("sys.argv", self.arguments), patch.object(package_macos, "run") as run, \
                patch.object(package_macos, "archive") as archive:
            with self.assertRaisesRegex(ValueError, "runtime"):
                package_macos.main()
        run.assert_not_called()
        archive.assert_not_called()

    def test_packaging_rejects_dirty_source_before_signing(self):
        self.manifest["source"]["dirty"] = True
        self.manifest_path.write_text(json.dumps(self.manifest))
        with patch("sys.argv", self.arguments), patch.object(package_macos, "run") as run:
            with self.assertRaisesRegex(ValueError, "clean source"):
                package_macos.main()
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
