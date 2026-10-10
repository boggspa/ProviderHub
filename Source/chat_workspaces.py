"""Chat-only workspace history; never writes desktop provider settings."""
import json
import os
from pathlib import Path
import uuid


def workspace_identity(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Choose a workspace folder.")
    return str(Path(value).expanduser().resolve())


def available_workspace(value):
    folder = workspace_identity(value)
    if not Path(folder).is_dir():
        raise ValueError(f"Workspace is unavailable: {folder}. Reconnect its drive or choose another folder.")
    if not os.access(folder, os.R_OK | os.X_OK):
        raise ValueError(f"Cannot access workspace: {folder}. Check its permissions or choose another folder.")
    return folder


class ChatWorkspaces:
    def __init__(self, root, legacy=()):
        self.path = Path(root) / "workspaces.json"
        if self.path.is_symlink():
            raise ValueError("Workspace history must not be a symbolic link.")
        self.folders = []
        self.projects = {}
        if self.path.exists():
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or not isinstance(data.get("folders"), list):
                raise ValueError("Workspace history is damaged; its file has been preserved.")
            self.folders = list(dict.fromkeys(workspace_identity(item) for item in data["folders"]))
            # Version 2 adds projects: {primaryPath: [secondaryPath, ...]}.
            # Older files have no projects key and keep loading unchanged.
            raw_projects = data.get("projects", {})
            if isinstance(raw_projects, dict):
                self.projects = self._normalized_projects(raw_projects)
        # Migration retains saved folders even when their volume is unavailable.
        merged = list(dict.fromkeys([*self.folders, *(workspace_identity(item) for item in legacy)]))
        if merged != self.folders or not self.path.exists():
            self.folders = merged
            self.save()

    @staticmethod
    def _normalized_projects(raw):
        projects = {}
        for primary, secondaries in raw.items():
            if not isinstance(secondaries, list):
                continue
            primary_id = workspace_identity(primary)
            attached = list(dict.fromkeys(
                workspace_identity(item) for item in secondaries
                if isinstance(item, str) and workspace_identity(item) != primary_id))
            if attached:
                projects[primary_id] = attached
        return projects

    def secondaries(self, primary):
        """Secondary roots attached to a workspace, primary identity resolved."""
        return self.projects.get(workspace_identity(primary), [])[:]

    def attach(self, primary, secondary):
        """Attach an additional folder to a saved workspace."""
        primary_id = workspace_identity(primary)
        secondary_id = workspace_identity(secondary)
        if primary_id not in self.folders:
            raise ValueError("Choose a saved workspace before attaching a folder.")
        if secondary_id == primary_id:
            raise ValueError("A workspace cannot attach itself.")
        if self.projects.get(secondary_id):
            raise ValueError("That folder is a project with its own folders; disconnect them first.")
        attached = self.projects.get(primary_id, [])
        if secondary_id in attached:
            return secondary_id
        if any(secondary_id in others for primary, others in self.projects.items() if primary != primary_id):
            raise ValueError("That folder is already attached to another workspace.")
        available_workspace(secondary_id)
        self.projects[primary_id] = [*attached, secondary_id]
        self.save()
        return secondary_id

    def detach(self, primary, secondary):
        """Disconnect a secondary folder; an emptied project is demoted."""
        primary_id = workspace_identity(primary)
        secondary_id = workspace_identity(secondary)
        attached = self.projects.get(primary_id, [])
        if secondary_id not in attached:
            raise ValueError("That folder is not attached to this workspace.")
        attached.remove(secondary_id)
        if not attached:
            self.projects.pop(primary_id, None)
        self.save()

    def remember(self, value, *, require_available=True):
        folder = available_workspace(value) if require_available else workspace_identity(value)
        self.folders = [folder, *(item for item in self.folders if item != folder)]
        self.save()
        return folder

    def save(self):
        temporary = self.path.parent / ("." + uuid.uuid4().hex + ".tmp")
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump({"version": 2, "folders": self.folders, "projects": self.projects}, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)
