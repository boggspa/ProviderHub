"""Prepare Antigravity's documented environment-based gateway connection.

The app inherits these variables for its language server. No Antigravity
configuration file or Google account credential is modified.
"""
from __future__ import annotations

from pathlib import Path
import subprocess

from antigravity_catalogue import project_antigravity, slug_for
from bridge_core import BridgeError, atomic_json, read_json


def antigravity_running() -> bool:
    return subprocess.run(["/usr/bin/pgrep", "-f", r"/Antigravity\.app/Contents/MacOS/Antigravity( |$)"],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0


def installed_app() -> str | None:
    candidates = [Path("/Applications/Antigravity.app"), Path.home() / "Applications/Antigravity.app"]
    return next((str(path) for path in candidates if path.is_dir()), None)


class AntigravityProfile:
    def __init__(self, root, *, running=antigravity_running):
        self.root = Path(root)
        self.running = running
        self.session = self.root / "antigravity-session.json"

    def owned_process_running(self) -> bool:
        if not self.session.exists():
            return False
        try:
            pid = read_json(self.session).get("pid")
            if type(pid) is not int or pid <= 0:
                return False
            result = subprocess.run(["/bin/ps", "-p", str(pid), "-o", "command="],
                                    capture_output=True, text=True, timeout=3)
            return result.returncode == 0 and "antigravity" in result.stdout.lower()
        except (OSError, ValueError, AttributeError, subprocess.SubprocessError):
            return False

    def status(self) -> dict:
        return {"antigravity_app_path": installed_app(), "antigravity_running": self.running(),
                "antigravity_profile_active": self.owned_process_running(),
                "antigravity_recovery_needed": self.session.exists()}

    def activate(self, settings: dict, inventory: dict) -> dict:
        if self.running():
            raise BridgeError("Quit Antigravity before launching it through Provider Hub.")
        projected = project_antigravity(settings, inventory)
        rows = projected["models"]
        if not rows:
            raise BridgeError("No Provider Hub model is available for Antigravity right now.")
        selected = settings.get("antigravity_model")
        if selected not in {row["route"] for row in rows}:
            raise BridgeError("Choose an available Antigravity default model.")
        return {"antigravity_models": [row["model_name"] for row in rows],
                "antigravity_model": slug_for(selected), "antigravity_route": selected,
                "antigravity_base_url": f"http://127.0.0.1:{settings['port']}",
                "antigravity_excluded": projected["excluded"]}

    def restore(self) -> dict:
        if self.owned_process_running():
            raise BridgeError("Quit the Provider Hub Antigravity session before disconnecting it.")
        existed = self.session.exists()
        self.session.unlink(missing_ok=True)
        return {"restored": existed, "antigravity_recovery_needed": False}

    def register(self, pid: int) -> dict:
        if type(pid) is not int or pid <= 0:
            raise BridgeError("Antigravity did not provide a valid process identifier.")
        atomic_json(self.session, {"pid": pid})
        if not self.owned_process_running():
            self.session.unlink(missing_ok=True)
            raise BridgeError("Could not verify the launched Antigravity process.")
        return self.status()
