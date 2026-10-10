"""Workspace history survives chat deletion, restart, aliases and missing disks."""
from pathlib import Path
import json
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from chat_runtime import ChatService, ChatStore
from chat_workspaces import ChatWorkspaces
from test_chat_runtime import FakeTransport, model


class WorkspaceTests(unittest.TestCase):
    def test_remember_persists_only_changed_order(self):
        with tempfile.TemporaryDirectory() as root:
            history = ChatWorkspaces(root)
            with patch.object(history, "save", wraps=history.save) as save:
                history.remember(root)
                history.remember(root)
                self.assertEqual(save.call_count, 1)
                history.remember(str(Path(root) / "offline"), require_available=False)
                history.remember(root)
                self.assertEqual(save.call_count, 3)
            self.assertEqual(ChatWorkspaces(root).folders, history.folders)

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
