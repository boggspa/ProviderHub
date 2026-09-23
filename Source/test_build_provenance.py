"""Release identity must describe the shipped files, including archive builds."""
import hashlib
import json
from pathlib import Path
import plistlib
import shutil
import subprocess
import tempfile
import unittest

from build_provenance import create_manifest, verify_manifest


class BuildProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.repository = self.root / "repository"
        self.source = self.repository / "Source"
        self.source.mkdir(parents=True)
        for name, content in {
            "gateway.py": "print('current worker')\n",
            "App.swift": "import AppKit\n",
            "build.sh": "#!/bin/bash\n",
            "build_provenance.py": "# provenance builder\n",
            "test_unrelated.py": "# existing test\n",
            "vendor/library.py": "VALUE = 1\n",
            "provider-logos/icon.txt": "logo\n",
        }.items():
            path = self.source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        self.git("init", "-q")
        self.git("add", "--", "Source")
        self.git("-c", "user.name=Build Test", "-c", "user.email=build@example.invalid",
                 "commit", "-qm", "Fixture")
        self.revision = self.git("rev-parse", "HEAD").decode().strip()
        self.app = self.root / "Provider Hub.app"
        self.worker = self.app / "Contents/Resources/worker"
        self.worker.mkdir(parents=True)
        for relative in ("gateway.py", "vendor/library.py", "provider-logos/icon.txt"):
            target = self.worker / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(self.source / relative, target)
        self.plist = self.app / "Contents/Info.plist"
        self.plist.write_bytes(plistlib.dumps({"CFBundleShortVersionString": "0.5.5",
                                              "CFBundleVersion": "17"}))

    def git(self, *args):
        return subprocess.check_output(["git", "-C", str(self.repository), *args],
                                       stderr=subprocess.DEVNULL)

    def build(self, source=None, **kwargs):
        source = source or self.source
        return create_manifest(source, self.app, [source / "App.swift"], **kwargs)

    def add_runtime(self, files, symlinks=()):
        """Create a stand-in bundled runtime under Contents/Resources/python."""
        runtime = self.app / "Contents/Resources/python"
        for relative, content in files.items():
            path = runtime / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content if isinstance(content, bytes) else content.encode())
        for relative, target in symlinks:
            path = runtime / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.symlink_to(target)
        return runtime

    def test_build_without_a_runtime_records_it_as_absent(self):
        manifest = self.build()
        self.assertEqual(manifest["runtime"], {"present": False})
        self.assertEqual(verify_manifest(self.app, revision=self.revision,
                                         require_clean=True), manifest)

    def test_bundled_runtime_is_hashed_and_symlinks_are_recorded(self):
        self.add_runtime({"bin/python3.13": b"\xcf\xfa\xed\xfe",
                          "lib/python3.13/os.py": "x = 1\n"},
                         symlinks=[("bin/python3", "python3.13")])
        manifest = self.build()
        runtime = manifest["runtime"]
        self.assertTrue(runtime["present"])
        # Two regular files plus the recorded bin/python3 -> python3.13 symlink.
        self.assertEqual(runtime["entry_count"], 3)
        self.assertEqual(runtime["personal_path_files"], [])
        self.assertEqual(len(runtime["sha256"]), 64)
        self.assertEqual(runtime["bytes"], len(b"\xcf\xfa\xed\xfe") + len(b"x = 1\n"))
        self.assertNotIn(str(self.root), json.dumps(manifest))
        self.assertEqual(verify_manifest(self.app, revision=self.revision,
                                         require_clean=True), manifest)

    def test_runtime_personal_path_is_recorded_not_hidden(self):
        self.add_runtime({"bin/python3.13": b"x", "bin/python3": b"y",
                          "lib/python3.13/_sysconfigdata.py":
                              b'prefix = "/Users/someone/build/cpython"'})
        manifest = self.build()
        self.assertEqual(manifest["runtime"]["personal_path_files"],
                         ["lib/python3.13/_sysconfigdata.py"])

    def test_upstream_ci_paths_are_not_reported_as_personal(self):
        self.add_runtime({"bin/python3.13": b"x", "bin/python3": b"y",
                          "lib/site.so": b"built at /Users/runner/work/cffi/src"})
        self.assertEqual(self.build()["runtime"]["personal_path_files"], [])

    def test_verification_rejects_a_modified_runtime(self):
        self.add_runtime({"bin/python3.13": b"x", "bin/python3": b"y",
                          "lib/python3.13/os.py": "a\n"})
        self.build()
        (self.app / "Contents/Resources/python/lib/python3.13/os.py").write_text("b\n")
        with self.assertRaises(ValueError) as caught:
            verify_manifest(self.app, revision=self.revision)
        self.assertIn("runtime", str(caught.exception))

    def test_verification_rejects_a_removed_runtime_file(self):
        runtime = self.add_runtime({"bin/python3.13": b"x", "bin/python3": b"y",
                                    "lib/python3.13/os.py": "a\n"})
        self.build()
        (runtime / "lib/python3.13/os.py").unlink()
        with self.assertRaises(ValueError):
            verify_manifest(self.app, revision=self.revision)

    def test_manifests_from_before_runtime_hashing_still_verify(self):
        self.add_runtime({"bin/python3.13": b"x", "bin/python3": b"y"})
        self.build()
        path = self.app / "Contents/Resources/build-manifest.json"
        legacy = json.loads(path.read_text())
        legacy.pop("runtime")
        path.write_text(json.dumps(legacy, indent=2, sort_keys=True) + "\n")
        verified = verify_manifest(self.app, revision=self.revision, require_clean=True)
        self.assertNotIn("runtime", verified)
        self.assertEqual(verified["source"]["revision"], self.revision)

    def test_clean_manifest_records_real_source_and_worker_hashes_without_host_paths(self):
        manifest = self.build()
        self.assertEqual(manifest["source"]["revision"], self.revision)
        self.assertFalse(manifest["source"]["dirty"])
        self.assertEqual(manifest["source"]["changed_inputs"], [])
        self.assertEqual(manifest["application"], {"version": "0.5.5", "build": "17"})
        self.assertRegex(manifest["built_at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assertEqual(manifest["source"]["sha256"]["Source/App.swift"],
                         hashlib.sha256((self.source / "App.swift").read_bytes()).hexdigest())
        self.assertEqual(manifest["worker"]["sha256"]["gateway.py"],
                         manifest["source"]["sha256"]["Source/gateway.py"])
        self.assertNotIn(str(self.root), json.dumps(manifest))
        self.assertEqual(verify_manifest(self.app, revision=self.revision, require_clean=True), manifest)

    def test_only_packaged_build_inputs_affect_dirty_status(self):
        (self.source / "test_unrelated.py").write_text("# changed unrelated test\n")
        (self.repository / "notes.md").write_text("Unrelated local notes\n")
        self.assertFalse(self.build()["source"]["dirty"])
        (self.source / "gateway.py").write_text("print('changed worker')\n")
        shutil.copy2(self.source / "gateway.py", self.worker / "gateway.py")
        manifest = self.build()
        self.assertTrue(manifest["source"]["dirty"])
        self.assertEqual(manifest["source"]["changed_inputs"], ["Source/gateway.py"])

    def test_export_is_compared_to_declared_git_tree_including_deleted_assets(self):
        exported = self.root / "export/Source"
        shutil.copytree(self.source, exported)
        manifest = self.build(exported, revision=self.revision, repository=self.repository)
        self.assertEqual(manifest["source"]["verification"], "git-tree")
        self.assertFalse(manifest["source"]["dirty"])
        (exported / "vendor/library.py").unlink()
        (self.worker / "vendor/library.py").unlink()
        manifest = self.build(exported, revision=self.revision, repository=self.repository)
        self.assertTrue(manifest["source"]["dirty"])
        self.assertEqual(manifest["source"]["changed_inputs"], ["Source/vendor/library.py"])

    def test_export_without_reference_repository_does_not_claim_to_be_clean(self):
        exported = self.root / "export/Source"
        shutil.copytree(self.source, exported)
        manifest = self.build(exported, revision=self.revision)
        self.assertEqual(manifest["source"]["verification"], "unverified-export")
        self.assertIsNone(manifest["source"]["dirty"])
        with self.assertRaisesRegex(ValueError, "requires PROVIDER_HUB_SOURCE_REVISION"):
            self.build(exported)

    def test_stale_or_mismatched_packaged_worker_is_rejected_before_manifest_creation(self):
        (self.worker / "removed_module.py").write_text("# stale build output\n")
        with self.assertRaisesRegex(ValueError, "does not match source: removed_module.py"):
            self.build()
        (self.worker / "removed_module.py").unlink()
        (self.worker / "gateway.py").write_text("# previous worker\n")
        with self.assertRaisesRegex(ValueError, "does not match source: gateway.py"):
            self.build()

    def test_verification_rejects_modified_added_and_missing_worker_files(self):
        self.build()
        original = (self.worker / "gateway.py").read_bytes()
        for change in ("modified", "added", "missing"):
            with self.subTest(change=change):
                if change == "modified":
                    (self.worker / "gateway.py").write_text("# modified\n")
                elif change == "added":
                    (self.worker / "extra.py").write_text("# extra\n")
                else:
                    (self.worker / "gateway.py").unlink()
                with self.assertRaisesRegex(ValueError, "do not match the build manifest"):
                    verify_manifest(self.app)
                (self.worker / "gateway.py").write_bytes(original)
                (self.worker / "extra.py").unlink(missing_ok=True)

    def test_verification_rejects_wrong_revision_or_version(self):
        self.build()
        with self.assertRaisesRegex(ValueError, "different source revision"):
            verify_manifest(self.app, revision="0" * 40)
        self.plist.write_bytes(plistlib.dumps({"CFBundleShortVersionString": "0.5.4",
                                              "CFBundleVersion": "16"}))
        with self.assertRaisesRegex(ValueError, "version does not match"):
            verify_manifest(self.app)

    def test_declared_revision_requires_a_full_commit_hash(self):
        with self.assertRaisesRegex(ValueError, "full Git commit hash"):
            self.build(revision="main")

    def test_release_verification_rejects_dirty_build_inputs_despite_matching_revision(self):
        (self.source / "App.swift").write_text("import SwiftUI\n")
        self.build()
        verify_manifest(self.app, revision=self.revision)
        with self.assertRaisesRegex(ValueError, "requires clean source verified against a Git tree"):
            verify_manifest(self.app, revision=self.revision, require_clean=True)

    def test_release_verification_rejects_unverified_export_despite_matching_revision(self):
        exported = self.root / "export/Source"
        shutil.copytree(self.source, exported)
        self.build(exported, revision=self.revision)
        verify_manifest(self.app, revision=self.revision)
        with self.assertRaisesRegex(ValueError, "requires clean source verified against a Git tree"):
            verify_manifest(self.app, revision=self.revision, require_clean=True)
        self.build(exported, revision=self.revision, repository=self.repository)
        verify_manifest(self.app, revision=self.revision, require_clean=True)


if __name__ == "__main__":
    unittest.main()
