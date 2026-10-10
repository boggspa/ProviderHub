"""Workspace history survives chat deletion, restart, aliases and missing disks."""
from pathlib import Path
import json
import subprocess
import sys
import tempfile
import unittest

from chat_runtime import ChatService, ChatStore, needs_approval
from chat_tools import ChatToolRunner
from chat_workspaces import ChatWorkspaces
from test_chat_runtime import FakeTransport, model


class AttachedFolderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.folder = self.root / "project"
        self.folder.mkdir()
        self.secondary = self.root / "docs"
        self.secondary.mkdir()
        self.store = ChatStore(self.root, lock=False)
        self.events = []
        self.transport = FakeTransport([])
        self.service = ChatService(self.store, self.transport, self.events.append)
        self.service.models = [model()]
        self.service.create(model()["id"], str(self.folder))

    def catalogues(self):
        return [event for event in self.events if event["event"] == "catalogue"]

    def test_attach_persists_version_two_and_survives_restart(self):
        self.service.handle({"command": "attach_workspace", "workspace": str(self.folder), "folder": str(self.secondary)})
        data = json.loads(self.service.workspaces.path.read_text(encoding="utf-8"))
        self.assertEqual(data["version"], 2)
        self.assertEqual(data["projects"], {str(self.folder): [str(self.secondary)]})
        restarted = ChatService(ChatStore(self.root, lock=False), FakeTransport([]), self.events.append)
        self.assertEqual(restarted.workspaces.projects, {str(self.folder): [str(self.secondary)]})
        self.assertEqual(restarted.workspaces.secondaries(str(self.folder)), [str(self.secondary)])

    def test_version_one_file_loads_without_projects(self):
        self.service.workspaces.path.write_text(json.dumps({"version": 1, "folders": [str(self.folder)]}), encoding="utf-8")
        migrated = ChatWorkspaces(self.store.root)
        self.assertEqual(migrated.folders, [str(self.folder)])
        self.assertEqual(migrated.projects, {})
        migrated.save()
        self.assertEqual(json.loads(migrated.path.read_text(encoding="utf-8"))["version"], 2)

    def test_attach_and_detach_commands_publish_projects(self):
        self.events.clear()
        self.service.handle({"command": "attach_workspace", "workspace": str(self.folder), "folder": str(self.secondary)})
        self.assertEqual(self.catalogues()[-1]["projects"], {str(self.folder): [str(self.secondary)]})
        self.service.handle({"command": "detach_workspace", "workspace": str(self.folder), "folder": str(self.secondary)})
        self.assertEqual(self.catalogues()[-1]["projects"], {})
        self.assertEqual(self.service.workspaces.projects, {})

    def test_demotion_after_last_disconnect(self):
        other = self.root / "assets"
        other.mkdir()
        self.service.handle({"command": "attach_workspace", "workspace": str(self.folder), "folder": str(self.secondary)})
        self.service.handle({"command": "attach_workspace", "workspace": str(self.folder), "folder": str(other)})
        self.assertEqual(self.service.workspaces.secondaries(str(self.folder)), [str(self.secondary), str(other)])
        self.service.handle({"command": "detach_workspace", "workspace": str(self.folder), "folder": str(self.secondary)})
        self.assertEqual(list(self.service.workspaces.projects), [str(self.folder)])
        self.service.handle({"command": "detach_workspace", "workspace": str(self.folder), "folder": str(other)})
        self.assertEqual(self.service.workspaces.projects, {})

    def test_attach_requires_saved_workspace_and_available_folder(self):
        unsaved = self.root / "unsaved"
        unsaved.mkdir()
        with self.assertRaisesRegex(ValueError, "saved workspace"):
            self.service.handle({"command": "attach_workspace", "workspace": str(unsaved), "folder": str(self.secondary)})
        missing = self.root / "missing"
        with self.assertRaisesRegex(ValueError, "unavailable"):
            self.service.handle({"command": "attach_workspace", "workspace": str(self.folder), "folder": str(missing)})
        self.assertEqual(self.service.workspaces.projects, {})

    def test_attach_rejects_aliases_duplicates_conflicts_and_cycles(self):
        self.service.handle({"command": "attach_workspace", "workspace": str(self.folder), "folder": str(self.secondary)})
        alias = self.root / "alias"
        alias.symlink_to(self.secondary, target_is_directory=True)
        # The same folder through an alias adds nothing and stays unique.
        self.service.handle({"command": "attach_workspace", "workspace": str(self.folder), "folder": str(alias)})
        self.assertEqual(self.service.workspaces.secondaries(str(self.folder)), [str(self.secondary)])
        primary_alias = self.root / "project-alias"
        primary_alias.symlink_to(self.folder, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "itself"):
            self.service.handle({"command": "attach_workspace", "workspace": str(self.folder), "folder": str(primary_alias)})
        # A folder that is itself a project cannot be absorbed.
        third = self.root / "third"
        third.mkdir()
        assets = self.root / "assets2"
        assets.mkdir()
        self.service.workspaces.remember(str(third))
        self.service.handle({"command": "attach_workspace", "workspace": str(third), "folder": str(assets)})
        with self.assertRaisesRegex(ValueError, "project with its own folders"):
            self.service.handle({"command": "attach_workspace", "workspace": str(self.folder), "folder": str(third)})
        # A folder already attached elsewhere cannot be attached twice.
        fourth = self.root / "fourth"
        fourth.mkdir()
        self.service.workspaces.remember(str(fourth))
        with self.assertRaisesRegex(ValueError, "already attached"):
            self.service.handle({"command": "attach_workspace", "workspace": str(fourth), "folder": str(assets)})
        self.assertEqual(self.service.workspaces.secondaries(str(fourth)), [])
        with self.assertRaisesRegex(ValueError, "not attached"):
            self.service.handle({"command": "detach_workspace", "workspace": str(self.folder), "folder": str(assets)})

    def test_attached_folders_reach_the_system_prompt(self):
        from test_chat_runtime import response
        self.service.handle({"command": "attach_workspace", "workspace": str(self.folder), "folder": str(self.secondary)})
        self.service.transport.responses.append(response())
        self.service.handle({"command": "send", "id": self.service.chat["id"], "text": "work"})
        self.service.thread.join(3)
        self.assertFalse(self.service.busy, "worker did not complete")
        self.assertIn("Attached folders:", self.service.transport.requests[-1]["system"])
        self.assertIn(str(self.secondary), self.service.transport.requests[-1]["system"])

    def test_workspace_without_attachments_keeps_plain_prompt(self):
        from test_chat_runtime import response
        self.service.transport.responses.append(response())
        self.service.handle({"command": "send", "id": self.service.chat["id"], "text": "work"})
        self.service.thread.join(3)
        self.assertNotIn("Attached folders:", self.service.transport.requests[-1]["system"])


class SecondaryRootToolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.primary = self.root / "primary"
        self.primary.mkdir()
        self.secondary = self.root / "secondary"
        self.secondary.mkdir()
        (self.primary / "main.py").write_text("print('primary')\n", encoding="utf-8")
        (self.secondary / "lib.py").write_text("VALUE = 42\nprint(VALUE)\n", encoding="utf-8")

    def runner(self, roots=()):
        return ChatToolRunner(str(self.primary), roots=[str(path) for path in roots])

    def test_read_and_search_reach_secondary_roots_only_by_absolute_path(self):
        runner = self.runner([self.secondary])
        read = runner.execute("read_file", {"path": str(self.secondary / "lib.py")})
        self.assertFalse(read["is_error"], read["content"][0]["text"])
        self.assertIn("VALUE = 42", read["content"][0]["text"])
        relative = runner.execute("read_file", {"path": "lib.py"})
        self.assertTrue(relative["is_error"], "relative paths must stay in the primary workspace")
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "secret.txt").write_text("nope", encoding="utf-8")
        denied = runner.execute("read_file", {"path": str(outside / "secret.txt")})
        self.assertTrue(denied["is_error"] and "outside workspace" in denied["content"][0]["text"])
        found = runner.execute("search_files", {"pattern": "VALUE", "path": str(self.secondary)})
        self.assertFalse(found["is_error"], found["content"][0]["text"])
        self.assertIn("lib.py:1", found["content"][0]["text"])

    def test_patch_add_and_update_in_secondary_root(self):
        runner = self.runner([self.secondary])
        added = runner.execute("apply_patch", {"patch":
            f"*** Begin Patch\n*** Add File: {self.secondary / 'new.py'}\n+hello = 1\n*** End Patch"})
        self.assertFalse(added["is_error"], added["content"][0]["text"])
        self.assertEqual((self.secondary / "new.py").read_text(encoding="utf-8"), "hello = 1\n")
        updated = runner.execute("apply_patch", {"patch":
            f"*** Begin Patch\n*** Update File: {self.secondary / 'lib.py'}\n@@\n-VALUE = 42\n+VALUE = 43\n*** End Patch"})
        self.assertFalse(updated["is_error"], updated["content"][0]["text"])
        self.assertIn("VALUE = 43", (self.secondary / "lib.py").read_text(encoding="utf-8"))
        denied = runner.execute("apply_patch", {"patch":
            f"*** Begin Patch\n*** Add File: {self.root / 'outside' / 'x.py'}\n+x = 1\n*** End Patch"})
        self.assertTrue(denied["is_error"])

    def test_symlink_and_traversal_guards_still_apply_in_secondary_roots(self):
        # Absolute paths resolve aliases (like workspace_identity does); a
        # symlink that escapes every root is still refused, as is traversal.
        outside = self.root / "outside.txt"
        outside.write_text("secret", encoding="utf-8")
        link = self.secondary / "link.py"
        link.symlink_to(outside)
        runner = self.runner([self.secondary])
        denied = runner.execute("read_file", {"path": str(link)})
        self.assertTrue(denied["is_error"] and "outside workspace" in denied["content"][0]["text"])

    def test_missing_secondary_volume_is_skipped_not_fatal(self):
        missing = self.root / "gone"
        runner = self.runner([missing])
        self.assertEqual(runner.extra_roots, ())
        read = runner.execute("read_file", {"path": str(self.primary / "main.py")})
        self.assertFalse(read["is_error"], read["content"][0]["text"])

    def test_nested_roots_resolve_to_the_most_specific(self):
        nested = self.secondary / "nested"
        nested.mkdir()
        (nested / "deep.txt").write_text("deep", encoding="utf-8")
        runner = self.runner([self.secondary, nested])
        read = runner.execute("read_file", {"path": str(nested / "deep.txt")})
        self.assertFalse(read["is_error"], read["content"][0]["text"])

    def test_accept_edits_preauthorization_stays_inside_the_git_worktree(self):
        import subprocess as sp
        sp.run(["git", "init", "-q", str(self.primary)], check=True)
        patch = f"*** Begin Patch\n*** Add File: {self.secondary / 'x.py'}\n+x = 1\n*** End Patch"
        # A patch landing outside the selected Git repository still asks.
        self.assertTrue(needs_approval("accept_edits", "apply_patch", {"patch": patch}, str(self.primary)))
        inside = "*** Begin Patch\n*** Add File: main2.py\n+x = 1\n*** End Patch"
        self.assertFalse(needs_approval("accept_edits", "apply_patch", {"patch": inside}, str(self.primary)))


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.folder = self.root / "project"
        self.folder.mkdir()
        self.store = ChatStore(self.root, lock=False)
        self.events = []
        self.transport = FakeTransport([])
        self.service = ChatService(self.store, self.transport, self.events.append)
        self.service.models = [model()]

    def test_configure_publishes_immediately_and_restart_preserves(self):
        self.service.create(model()["id"], str(self.root))
        self.events.clear()
        self.service.handle({"command": "configure", "workspace": str(self.folder)})
        catalogues = [event for event in self.events if event["event"] == "catalogue"]
        self.assertEqual(catalogues[-1]["folders"][0], str(self.folder.resolve()))
        restarted = ChatService(ChatStore(self.root, lock=False), self.transport, self.events.append)
        self.assertEqual(restarted.workspaces.folders[0], str(self.folder.resolve()))
        self.assertEqual(len(restarted.store.headers()), 1)

    def test_delete_last_chat_keeps_folder_and_clears_selection(self):
        self.service.create(model()["id"], str(self.folder))
        self.service.handle({"command": "delete", "id": self.service.chat["id"]})
        self.assertEqual(self.store.headers(), [])
        self.assertIsNone(self.service.chat)
        self.assertEqual(self.events[-1]["event"], "selected")
        self.assertIsNone(self.events[-1]["id"])
        restarted = ChatService(ChatStore(self.root, lock=False), self.transport, self.events.append)
        restarted.initialize()
        self.assertEqual(restarted.chat["workspace"], str(self.folder.resolve()))

    def test_alias_deduplication_and_same_name_are_distinct(self):
        alias = self.root / "alias"
        alias.symlink_to(self.folder, target_is_directory=True)
        other = self.root / "other" / "project"
        other.mkdir(parents=True)
        for path in [self.folder, other, alias]:
            self.service.create(model()["id"], str(path))
        self.assertEqual(self.service.workspaces.folders, [str(self.folder.resolve()), str(other.resolve())])
        summaries = self.service.summaries()
        self.assertEqual([row["workspace"] for row in summaries].count(str(self.folder.resolve())), 2)
        self.assertEqual(len({row["id"] for row in summaries}), 3)

    def test_choosing_folder_without_chat_and_default_new_chat(self):
        self.service.handle({"command": "configure", "workspace": str(self.folder)})
        self.assertEqual(self.service.chat["workspace"], str(self.folder.resolve()))
        self.service.handle({"command": "delete", "id": self.service.chat["id"]})
        self.service.handle({"command": "create", "choice": model()["id"]})
        self.assertEqual(self.service.chat["workspace"], str(self.folder.resolve()))

    def test_missing_folder_is_retained_and_never_created(self):
        self.service.create(model()["id"], str(self.folder))
        self.folder.rmdir()
        restarted = ChatService(ChatStore(self.root, lock=False), self.transport, self.events.append)
        restarted.models = [model()]
        with self.assertRaisesRegex(ValueError, "Workspace is unavailable"):
            restarted.handle({"command": "create", "choice": model()["id"], "workspace": str(self.folder)})
        self.assertIn(str(self.folder.resolve()), restarted.workspaces.folders)
        self.assertFalse(self.folder.exists())

    def test_offline_restart_still_publishes_folders(self):
        self.service.create(model()["id"], str(self.folder))
        def offline():
            raise OSError("offline")
        self.transport.catalogue = offline
        self.events.clear()
        restarted = ChatService(ChatStore(self.root, lock=False), self.transport, self.events.append)
        restarted.initialize()
        self.assertEqual(self.events[0]["folders"], [str(self.folder.resolve())])
        self.assertEqual(self.events[-1]["event"], "ready")

    def test_saved_chat_on_disconnected_volume_can_still_be_read(self):
        self.service.create(model()["id"], str(self.folder))
        identifier = self.service.chat["id"]
        self.folder.rmdir()
        self.service.chat = None
        self.service.handle({"command": "select", "id": identifier})
        self.assertEqual(self.service.chat["id"], identifier)
        self.assertEqual(self.events[-1]["event"], "selected")
        self.assertIn(str(self.folder.resolve()), self.service.workspaces.folders)
        self.assertFalse(self.folder.exists())

    def test_legacy_migration_retains_missing_folders_and_history_is_private(self):
        self.service.create(model()["id"], str(self.folder))
        self.service.workspaces.path.unlink()
        self.folder.rmdir()
        migrated = ChatWorkspaces(self.store.root, [str(self.folder)])
        self.assertEqual(migrated.folders, [str(self.folder.resolve())])
        self.assertEqual(migrated.path.stat().st_mode & 0o777, 0o600)
        self.assertFalse((self.root / "settings.json").exists())

    def test_fresh_worker_process_reads_history_after_all_chats_deleted(self):
        self.service.create(model()["id"], str(self.folder))
        self.service.handle({"command": "delete", "id": self.service.chat["id"]})
        program = """
import json, sys
from chat_runtime import ChatService, ChatStore
from test_chat_runtime import FakeTransport
events = []
service = ChatService(ChatStore(sys.argv[1]), FakeTransport([]), events.append)
service.initialize()
print(json.dumps({'folders': service.workspaces.folders, 'workspace': service.chat['workspace']}))
"""
        result = subprocess.run([sys.executable, "-c", program, str(self.root)],
                                cwd=Path(__file__).parent, capture_output=True, text=True, check=True)
        data = json.loads(result.stdout)
        self.assertEqual(data["folders"], [str(self.folder.resolve())])
        self.assertEqual(data["workspace"], str(self.folder.resolve()))


if __name__ == "__main__":
    unittest.main()
