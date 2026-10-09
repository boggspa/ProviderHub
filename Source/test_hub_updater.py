"""Release validation, staged installation and the real native restart flow."""
from contextlib import ExitStack
import hashlib
import io
import json
from pathlib import Path
import plistlib
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
import warnings
from unittest.mock import patch
import zipfile

import hub_updater as updater

SOURCE = Path(__file__).parent


def release(version="0.5.6", build="63"):
    tag = f"v{version}-build{build}"
    name = f"ProviderHub-{version}-build{build}-abcdef1-notarized.zip"
    return {"tag_name": tag, "draft": False, "prerelease": False, "assets": [{
        "name": name, "state": "uploaded", "size": 4, "digest": "sha256:" + hashlib.sha256(b"test").hexdigest(),
        "browser_download_url": f"https://github.com/{updater.REPOSITORY}/releases/download/{tag}/{name}"}]}


def app_metadata(path, build="62"):
    (path / "Contents").mkdir(parents=True)
    (path / "Contents/Info.plist").write_bytes(plistlib.dumps({
        "CFBundleIdentifier": updater.BUNDLE_ID, "CFBundleExecutable": "MistralBridge",
        "CFBundleShortVersionString": "0.5.6", "CFBundleVersion": build, "LSMinimumSystemVersion": "14.0"}))


class ReleaseTests(unittest.TestCase):
    def test_same_marketing_version_newer_build_and_numeric_order(self):
        self.assertEqual(updater.release_candidate(release(build="100"), "0.5.6", "99")["build"], "100")
        self.assertIsNone(updater.release_candidate(release(), "0.5.6", "63"))
        self.assertIsNone(updater.release_candidate(release(), "0.5.6", "64"))
        self.assertIsNotNone(updater.release_candidate(release("0.5.10", "1"), "0.5.9", "99"))
        self.assertIsNone(updater.release_candidate(release("0.5.5", "1000"), "0.5.6", "62"))

    def test_drafts_prereleases_missing_ambiguous_and_submission_assets(self):
        cases = [None, {}, {**release(), "draft": True}, {**release(), "prerelease": True},
                 {**release(), "tag_name": "v0.5.7-beta-build63"}, {**release(), "assets": []}]
        ambiguous = release(); ambiguous["assets"] *= 2; cases.append(ambiguous)
        submission = release(); submission["assets"][0]["name"] = "ProviderHub-abcdef1-submission.zip"; cases.append(submission)
        for value in cases:
            with self.subTest(value=value):
                self.assertIsNone(updater.release_candidate(value, "0.5.6", "62"))

    def test_requires_official_https_asset_digest_and_bounded_size(self):
        for field, values in {
            "browser_download_url": ["http://github.com/x", "https://evil.test/x", "https://github.com@evil.test/x",
                "https://github.com/boggspa/Other/releases/download/v0.5.6-build63/x", release()["assets"][0]["browser_download_url"] + "?x=1"],
            "digest": [None, "", "sha1:" + "a" * 40, "sha256:" + "a" * 63],
            "size": [True, -1, 0, updater.MAX_DOWNLOAD + 1, "4"],
        }.items():
            for value in values:
                with self.subTest(field=field, value=value):
                    data = release(); data["assets"][0][field] = value
                    with self.assertRaises((ValueError, TypeError)):
                        updater.release_candidate(data, "0.5.6", "62")

    def test_invalid_version_never_guesses(self):
        for version, build in [("latest", "63"), ("0.5.6", "x"), ("0.5", "63")]:
            with self.subTest(version=version, build=build), self.assertRaises(ValueError):
                updater.version_key(version, build)

    def test_download_checks_all_bytes_and_never_accepts_mismatch(self):
        class Response(io.BytesIO):
            def geturl(self): return "https://release-assets.githubusercontent.com/asset"
        candidate = updater.release_candidate(release(), "0.5.6", "62")
        with tempfile.TemporaryDirectory() as directory:
            for index, payload in enumerate([b"test", b"evil", b"tes", b"test-extra"]):
                destination = Path(directory) / str(index)
                with patch.object(updater.urllib.request, "urlopen", return_value=Response(payload)):
                    if index == 0:
                        updater.download(candidate, destination)
                        self.assertEqual(destination.read_bytes(), b"test")
                    else:
                        with self.assertRaises(ValueError): updater.download(candidate, destination)


class ArchiveTests(unittest.TestCase):
    def archive(self, entries):
        data = io.BytesIO()
        with zipfile.ZipFile(data, "w") as output:
            output.writestr(updater.APP_NAME + "/Contents/Info.plist", b"plist")
            for name, payload, symlink in entries:
                info = zipfile.ZipInfo(name); info.create_system = 3
                info.external_attr = (stat.S_IFLNK | 0o777 if symlink else stat.S_IFREG | 0o644) << 16
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", UserWarning)
                    output.writestr(info, payload)
        data.seek(0)
        return data

    def test_runtime_symlinks_and_regular_paths(self):
        updater.inspect_archive(self.archive([
            (updater.APP_NAME + "/Contents/Resources/python/bin/python3", "python3.13", True),
            (updater.APP_NAME + "/Contents/Resources/python/bin/python3.13", b"binary", False)]))

    def test_traversal_absolute_backslash_duplicate_and_wrong_bundle(self):
        names = ["../escaped", "/tmp/escaped", updater.APP_NAME + "/../../escaped", updater.APP_NAME + "/x/../escaped",
                 updater.APP_NAME + "//x", updater.APP_NAME + "/./x", updater.APP_NAME + "/x\\y", "Other.app/Contents/x",
                 updater.APP_NAME + "/Contents/Info.plist"]
        for name in names:
            with self.subTest(name=name), self.assertRaises(ValueError):
                updater.inspect_archive(self.archive([(name, "x", False)]))

    def test_escaping_chained_and_written_through_symlinks(self):
        root = updater.APP_NAME + "/Contents"
        cases = [[(root + "/link", "/tmp", True)], [(root + "/link", "../../outside", True)],
                 [(root + "/link", "../Contents", True), (root + "/link/write", "x", False)],
                 [(root + "/first", "second", True), (root + "/second", "Info.plist", True)],
                 [(root + "/link", "", True)]]
        for entries in cases:
            with self.subTest(entries=entries), self.assertRaises(ValueError):
                updater.inspect_archive(self.archive(entries))

    def test_size_and_entry_budgets(self):
        with patch.object(updater, "MAX_EXPANDED", 3), self.assertRaises(ValueError):
            updater.inspect_archive(self.archive([]))

    def test_macos_case_and_unicode_aliases_cannot_bypass_symlink_checks(self):
        root = updater.APP_NAME + "/Contents"
        for entries in [
            [(root + "/info.plist", "duplicate", False)],
            [(root + "/é", "x", False), (root + "/e\u0301", "y", False)],
            [(root + "/link", "Info.plist", True), (root + "/LINK/file", "x", False)],
            [(root + "/first", "SECOND", True), (root + "/second", "Info.plist", True)],
        ]:
            with self.subTest(entries=entries), self.assertRaises(ValueError):
                updater.inspect_archive(self.archive(entries))


class StagingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.target = self.root / "Provider Hub's app.app"; app_metadata(self.target)
        self.cache = self.root / "cache"; self.cache.mkdir()
        self.candidate = updater.release_candidate(release(), "0.5.6", "62")

    def test_verification_requires_matching_identity_version_and_notarization(self):
        app = self.root / "new.app"; app_metadata(app, "63")
        with patch.object(updater.platform, "mac_ver", return_value=("14.6.0", (), "")):
            with patch.object(updater, "run", return_value="source=Notarized Developer ID") as run:
                updater.verify_app(app, "0.5.6", "63")
                self.assertIn(updater.REQUIREMENT, run.call_args_list[0].args[-2])
            with patch.object(updater, "run", return_value="source=Developer ID"), self.assertRaises(ValueError):
                updater.verify_app(app, "0.5.6", "63")
            with patch.object(updater, "run", side_effect=ValueError("wrong signer")), self.assertRaises(ValueError):
                updater.verify_app(app, "0.5.6", "63")
            with patch.object(updater, "run") as run, self.assertRaises(ValueError):
                updater.verify_app(app, "0.5.6", "64")
            run.assert_not_called()
        with patch.object(updater.platform, "mac_ver", return_value=("13.0", (), "")), self.assertRaises(ValueError):
            updater.verify_app(app, "0.5.6", "63")

    def stage_patches(self, stack, failure=None):
        def downloaded(candidate, destination): destination.write_bytes(b"archive")
        def extracted(*args):
            app_metadata(Path(args[-1]) / updater.APP_NAME, "63")
            return ""
        stack.enter_context(patch.object(updater, "download", side_effect=downloaded))
        stack.enter_context(patch.object(updater, "inspect_archive"))
        stack.enter_context(patch.object(updater, "run", side_effect=extracted))
        stack.enter_context(patch.object(updater, "verify_app", side_effect=failure))

    def test_stage_keeps_running_app_untouched_and_persists_ready_update(self):
        before = (self.target / "Contents/Info.plist").read_bytes()
        with ExitStack() as stack:
            self.stage_patches(stack)
            value = updater.stage(self.candidate, self.cache, self.target)
            self.assertEqual(updater.pending(self.cache, self.target, "0.5.6", "62"), value)
            prepared = updater.prepare(self.cache, self.target, "0.5.6", "62")
            self.assertEqual(Path(prepared["script"]).read_bytes(), (SOURCE / "update_install.sh").read_bytes())
            self.assertEqual(prepared["installedBuild"], "62")
        self.assertEqual((self.target / "Contents/Info.plist").read_bytes(), before)
        self.assertTrue(Path(value["path"]).is_dir())

    def test_failed_signature_leaves_no_pending_record_or_staging_folder(self):
        with ExitStack() as stack:
            self.stage_patches(stack, ValueError("signature rejected"))
            with self.assertRaises(ValueError): updater.stage(self.candidate, self.cache, self.target)
        self.assertFalse((self.cache / "pending.json").exists())
        self.assertEqual(list(self.root.glob(".provider-hub-update-*")), [])
        self.assertTrue(self.target.exists())

    def test_pending_rejects_other_target_arbitrary_paths_and_symlinks(self):
        with ExitStack() as stack:
            self.stage_patches(stack)
            value = updater.stage(self.candidate, self.cache, self.target)
        for replacement in [{"target": str(self.root / "Other.app")}, {"path": str(self.root / updater.APP_NAME)}]:
            (self.cache / "pending.json").write_text(json.dumps({**value, **replacement}))
            with self.assertRaises(ValueError): updater.pending(self.cache, self.target, "0.5.6", "62")

    def test_consumed_update_clears_record_and_external_replacement_blocks_prepare(self):
        with ExitStack() as stack:
            self.stage_patches(stack)
            updater.stage(self.candidate, self.cache, self.target)
            info = self.target / "Contents/Info.plist"
            data = plistlib.loads(info.read_bytes()); data["CFBundleVersion"] = "64"; info.write_bytes(plistlib.dumps(data))
            with self.assertRaisesRegex(ValueError, "outside the updater"):
                updater.prepare(self.cache, self.target, "0.5.6", "62")
            self.assertIsNone(updater.pending(self.cache, self.target, "0.5.6", "64"))
            self.assertFalse((self.cache / "pending.json").exists())


@unittest.skipUnless(sys.platform == "darwin", "Installer uses macOS commands")
class InstallerTests(unittest.TestCase):
    def exercise(self, live_owner=False, fail_open=False, fail_swap=False, wrong_current=False, bad_signature=False):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "Hub's $(touch injected).app"; app_metadata(target)
            folder = root / ".provider-hub-update-test"; folder.mkdir()
            candidate = folder / updater.APP_NAME; app_metadata(candidate, "63")
            opened = root / "opened"
            open_command = root / "open"
            open_command.write_text("#!/bin/sh\n" + (f"[ -e '{opened}' ] || {{ touch '{opened}'; exit 1; }}\n" if fail_open else "") + f"touch '{opened}'\n")
            open_command.chmod(0o700)
            verification = root / "verify"; verification.write_text("#!/bin/sh\nexit " + ("1" if bad_signature else "0") + "\n"); verification.chmod(0o700)
            script = (SOURCE / "update_install.sh").read_text().replace("/usr/bin/open", str(open_command))
            script = script.replace("/usr/bin/codesign", str(verification)).replace("/usr/sbin/spctl", str(verification))
            if fail_swap:
                move = root / "move"
                move.write_text('#!/bin/sh\ncase "$1" in *"Provider Hub.app") exit 1;; esac\nexec /bin/mv "$@"\n'); move.chmod(0o700)
                script = script.replace("/bin/mv", str(move))
            path = root / "installer.sh"; path.write_text(script)
            owner = subprocess.Popen(["/bin/sleep", "30"]) if live_owner else None
            try:
                if wrong_current:
                    data = plistlib.loads((target / "Contents/Info.plist").read_bytes()); data["CFBundleVersion"] = "64"
                    (target / "Contents/Info.plist").write_bytes(plistlib.dumps(data))
                pid = owner.pid if owner else "99999999"
                process = subprocess.Popen(["/bin/sh", str(path), str(pid), str(target), str(candidate), "0.5.6", "62"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                if owner:
                    time.sleep(0.15)
                    self.assertIsNone(process.poll())
                    self.assertTrue(candidate.exists())
                    self.assertEqual(plistlib.loads((target / "Contents/Info.plist").read_bytes())["CFBundleVersion"], "62")
                    # Other protocol tests install signal handlers; spawned
                    # fixtures can inherit ignored SIGTERM. This owned sleeper
                    # has no work to drain, so end it deterministically.
                    owner.kill(); owner.wait(timeout=5)
                stdout, stderr = process.communicate(timeout=10)
                failed = fail_open or fail_swap or wrong_current or bad_signature
                self.assertEqual(process.returncode != 0, failed, stderr.decode())
                expected = "64" if wrong_current else "62" if failed else "63"
                self.assertEqual(plistlib.loads((target / "Contents/Info.plist").read_bytes())["CFBundleVersion"], expected)
                self.assertFalse((root / "injected").exists())
                if not failed: self.assertTrue((folder / "previous.app").exists())
            finally:
                if owner and owner.poll() is None: owner.kill(); owner.wait()
                if process.poll() is None: process.kill(); process.wait()

    def test_waits_for_host_exit_then_swaps_and_relaunches(self): self.exercise(live_owner=True)
    def test_failed_swap_rolls_back(self): self.exercise(fail_swap=True)
    def test_failed_launch_rolls_back(self): self.exercise(fail_open=True)
    def test_external_replacement_is_preserved(self): self.exercise(wrong_current=True)
    def test_changed_signature_is_rejected_before_swap(self): self.exercise(bad_signature=True)


@unittest.skipUnless(sys.platform == "darwin" and shutil.which("xcrun"), "Native updater tests need macOS")
class NativeUpdaterTests(unittest.TestCase):
    def test_restart_state_machine(self):
        stubs = '''import AppKit
import SwiftUI
struct WorkerError: LocalizedError { var message: String; var errorDescription: String? { message } }
enum HubTheme { enum Semantic { static let ink = Color.primary }; enum Accent { static let brand = Color.orange } }
'''
        cases = r'''
import AppKit
@main struct Cases {
    @MainActor static func main() async throws {
        func check(_ value: Bool, _ message: String) { if !value { fatalError(message) } }
        for bits in 0..<16 {
            let reason = HubUpdateRestartPolicy.blocker(desktopOpen: bits & 1 != 0, activeWork: bits & 2 != 0,
                unsavedSettings: bits & 4 != 0, drafts: bits & 8 != 0)
            check((reason == nil) == (bits == 0), "Restart ignored an active session or unsaved work")
        }
        let update = HubUpdateInfo(version: "0.5.6", build: "63", tag: "v0.5.6-build63")
        var blocked = true, restarts = 0, fail = false, beginWorkOnDownload = false, finishWorkOnDownload = false
        let updater = HubUpdater(execute: { action, tag in
            if action == "check" { return update }
            if fail { throw WorkerError(message: "Download rejected") }
            if action == "stage" {
                check(tag == update.tag, "Stage changed the advertised release")
                if beginWorkOnDownload { blocked = true }
                if finishWorkOnDownload { blocked = false }
            }
            return update
        }, restart: { restarts += 1 }, blocker: { blocked ? "Work is active" : nil })
        await updater.check(); check(updater.label == "Update" && updater.visible, "Available update not advertised")
        fail = true; await updater.activate()
        check(!updater.working && updater.ready == nil && updater.available != nil && restarts == 0, "Failed update lost retry or restarted")
        fail = false; updater.error = nil; await updater.activate()
        check(updater.label == "Restart" && updater.ready != nil && restarts == 0, "Active work was restarted")
        blocked = false; await updater.check()
        check(restarts == 0, "Update restarted as soon as work ended without a user click")
        fail = true; await updater.activate()
        check(restarts == 0 && updater.ready != nil && !updater.working, "Preparation failure restarted or lost staged update")
        fail = false; await updater.activate()
        check(restarts == 1 && updater.restartRequested, "Explicit safe restart did not proceed")
        updater.deferRestart("New work started")
        check(!updater.restartRequested && updater.ready != nil, "Shutdown race lost pending update")

        blocked = false; beginWorkOnDownload = true
        let late = HubUpdater(execute: { action, _ in
            if action == "stage" { blocked = true }; return update
        }, restart: { restarts += 1 }, blocker: { blocked ? "Working" : nil })
        await late.check(); await late.activate()
        check(restarts == 1 && late.ready != nil, "Work started during download was interrupted")
        blocked = true
        let ended = HubUpdater(execute: { action, _ in
            if action == "stage" { blocked = false }; return update
        }, restart: { restarts += 1 }, blocker: { blocked ? "Working" : nil })
        await ended.check(); await ended.activate()
        check(restarts == 1, "Busy-at-click update auto-restarted after work ended")
        let idle = HubUpdater(execute: { _, _ in update }, restart: { restarts += 1 }, blocker: { nil })
        await idle.check(); await idle.activate()
        check(restarts == 2 && idle.restartRequested, "Idle update did not auto-restart")
        let offline = HubUpdater(execute: { _, _ in throw WorkerError(message: "Offline") })
        await offline.check(); check(!offline.visible && offline.error == nil, "Offline check disturbed launch")
        print("Native updater state checks passed")
    }
}
'''
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "Stubs.swift").write_text(stubs); (root / "Cases.swift").write_text(cases)
            binary = root / "checks"
            result = subprocess.run(["xcrun", "swiftc", "-swift-version", "5", "-parse-as-library",
                "-module-cache-path", str(root / "cache"), str(root / "Stubs.swift"), str(SOURCE / "HubUpdater.swift"),
                str(root / "Cases.swift"), "-framework", "AppKit", "-framework", "SwiftUI", "-o", str(binary)],
                capture_output=True, text=True, timeout=120)
            self.assertEqual(result.returncode, 0, result.stderr)
            result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Native updater state checks passed", result.stdout)


if __name__ == "__main__": unittest.main()
