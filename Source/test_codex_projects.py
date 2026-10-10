"""Desktop projects rooted at "/", read from disposable Codex homes only."""
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from codex_projects import GLOBAL_STATE, disk_root_projects


def write_state(home: Path, projects: dict) -> None:
    (home / GLOBAL_STATE).write_text(json.dumps({"local-projects": projects, "projectless-thread-ids": []}))


def write_database(home: Path, projects: dict, name: str = "state_5.sqlite") -> None:
    """projects: id -> (name, [roots in position order])."""
    connection = sqlite3.connect(home / name)
    connection.executescript(
        "CREATE TABLE projects (id TEXT PRIMARY KEY, name TEXT NOT NULL, metadata TEXT NOT NULL DEFAULT '{}',"
        " position INTEGER NOT NULL, created_at_ms INTEGER NOT NULL, updated_at_ms INTEGER NOT NULL);"
        "CREATE TABLE project_roots (project_id TEXT NOT NULL, position INTEGER NOT NULL, path TEXT NOT NULL,"
        " PRIMARY KEY (project_id, position));")
    for index, (identifier, (title, roots)) in enumerate(projects.items()):
        connection.execute("INSERT INTO projects (id, name, position, created_at_ms, updated_at_ms) VALUES (?, ?, ?, 0, 0)",
                           (identifier, title, index))
        # Positions need not start at zero; the lowest one is primary.
        for position, root in enumerate(roots, start=3):
            connection.execute("INSERT INTO project_roots VALUES (?, ?, ?)", (identifier, position, root))
    connection.commit()
    connection.close()


class DiskRootProjectTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.home = Path(self.directory.name)

    def tearDown(self):
        self.directory.cleanup()

    def test_both_records_are_read_and_the_same_project_is_named_once(self):
        write_state(self.home, {
            "63c6": {"id": "63c6", "name": "/Disk tidy", "rootPaths": ["/"]},
            "aa01": {"id": "aa01", "name": "Game", "rootPaths": ["/Users/tester/Game"]},
            "aa02": {"id": "aa02", "name": "  ", "rootPaths": ["//"]},
        })
        write_database(self.home, {
            "01a1": ("/Disk tidy", ["/"]),
            "01a2": ("Archive", ["/"]),
            "01a3": ("Game", ["/Users/tester/Game"]),
        })
        self.assertEqual(disk_root_projects(self.home), ["/Disk tidy", "Archive", "Untitled project"])

    def test_only_the_primary_folder_decides(self):
        # A chat starts in the primary folder; "/" as an extra source folder
        # leaves its chats a real working directory.
        write_state(self.home, {
            "b1": {"id": "b1", "name": "Extra root", "rootPaths": ["/Users/tester/Work", "/"]},
            "b2": {"id": "b2", "name": "Primary root", "rootPaths": ["/", "/Users/tester/Work"]},
            "b3": {"id": "b3", "name": "No folders", "rootPaths": []},
            "b4": {"id": "b4", "name": "Home", "rootPaths": ["~"]},
        })
        write_database(self.home, {
            "c1": ("Extra root", ["/Users/tester/Work", "/"]),
            "c2": ("Primary root", ["/", "/Users/tester/Work"]),
            "c3": ("Empty path", [""]),
        })
        self.assertEqual(disk_root_projects(self.home), ["Primary root"])

    def test_only_the_newest_database_counts(self):
        write_database(self.home, {"old": ("Old schema", ["/"])}, name="state_4.sqlite")
        write_database(self.home, {"new": ("Current", ["/Users/tester/Current"])}, name="state_12.sqlite")
        (self.home / "state_x.sqlite").write_text("not a database")
        self.assertEqual(disk_root_projects(self.home), [])

    def test_unreadable_sources_give_advice_from_the_other_or_nothing(self):
        self.assertEqual(disk_root_projects(self.home), [])
        self.assertEqual(disk_root_projects(self.home / "missing"), [])
        (self.home / GLOBAL_STATE).write_text("{ truncated")
        write_database(self.home, {"d1": ("Disk", ["/"])})
        self.assertEqual(disk_root_projects(self.home), ["Disk"])
        (self.home / "state_5.sqlite").unlink()
        sqlite3.connect(self.home / "state_5.sqlite").close()  # schema drift: no tables
        write_state(self.home, {"e1": {"id": "e1", "name": "Legacy", "rootPaths": ["/"]},
                                "e2": "not a project", "e3": {"name": "No roots"}})
        self.assertEqual(disk_root_projects(self.home), ["Legacy"])
        (self.home / GLOBAL_STATE).write_text(json.dumps(["not", "an", "object"]))
        self.assertEqual(disk_root_projects(self.home), [])

    def test_the_database_is_opened_read_only(self):
        write_database(self.home, {"f1": ("Disk", ["/"])})
        before = (self.home / "state_5.sqlite").read_bytes()
        self.assertEqual(disk_root_projects(self.home), ["Disk"])
        self.assertEqual((self.home / "state_5.sqlite").read_bytes(), before)
        self.assertEqual(sorted(path.name for path in self.home.iterdir()), ["state_5.sqlite"])

    def test_codex_home_defaults_from_the_environment(self):
        write_state(self.home, {"g1": {"id": "g1", "name": "From env", "rootPaths": ["/"]}})
        with mock.patch.dict(os.environ, {"CODEX_HOME": str(self.home)}):
            self.assertEqual(disk_root_projects(), ["From env"])

    def test_worker_command_reports_the_projects(self):
        write_state(self.home, {"h1": {"id": "h1", "name": "/Disk tidy", "rootPaths": ["/"]}})
        with tempfile.TemporaryDirectory() as state:
            environment = {key: value for key, value in os.environ.items() if not key.startswith("MISTRAL_BRIDGE_")}
            environment.update({"CODEX_HOME": str(self.home), "MISTRAL_BRIDGE_STATE_DIR": state})
            completed = subprocess.run([sys.executable, str(Path(__file__).with_name("gateway.py")), "codex-projects"],
                                       capture_output=True, text=True, env=environment, timeout=60)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout), {"ok": True, "codex_disk_root_projects": ["/Disk tidy"]})


if __name__ == "__main__":
    unittest.main()
