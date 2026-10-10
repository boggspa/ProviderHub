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
        if self.path.exists():
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or not isinstance(data.get("folders"), list):
                raise ValueError("Workspace history is damaged; its file has been preserved.")
            self.folders = list(dict.fromkeys(workspace_identity(item) for item in data["folders"]))
        # Migration retains saved folders even when their volume is unavailable.
        merged = list(dict.fromkeys([*self.folders, *(workspace_identity(item) for item in legacy)]))
        if merged != self.folders or not self.path.exists():
            self.folders = merged
            self.save()

    def remember(self, value, *, require_available=True):
        folder = available_workspace(value) if require_available else workspace_identity(value)
        folders = [folder, *(item for item in self.folders if item != folder)]
        if folders != self.folders:
            self.folders = folders
            self.save()
        return folder

    def save(self):
        temporary = self.path.parent / ("." + uuid.uuid4().hex + ".tmp")
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump({"version": 1, "folders": self.folders}, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)
