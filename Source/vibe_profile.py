"""Reversible, formatting-preserving configuration for the Vibe desktop app.

Vibe reads its model list from ``$VIBE_HOME/config.toml``: one ``[[providers]]``
entry per endpoint and one ``[[models]]`` entry per model the picker offers.
The Hub writes the gateway provider and its own model rows there, journals
what it replaced, and puts the user's configuration back when Vibe quits.

Three things make this different from the Codex switch:

* Vibe owns the file. It rewrites ``active_model``, its migrations and its
  settings on its own, so the Hub's rows are added to the user's file rather
  than replacing it, and restore removes only the rows the Hub added.
* The rows are identified by their provider. Every ``[[models]]`` entry whose
  provider is ``provider_hub`` is ours, so a restore can tell our rows from
  the user's without keeping a second list.
* The credential never reaches the file. Vibe reads the gateway token from an
  environment variable, which the launcher puts in the app's environment.
"""
from __future__ import annotations

import copy
import fcntl
import os
from pathlib import Path
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).with_name("vendor")))
import tomlkit
from tomlkit.items import AoT, Table

from bridge_core import BridgeError, atomic_json, private_directory, read_json
from vibe_catalogue import PROVIDER_ID, PROVIDER_NAME, project_vibe

#: The environment variable Vibe reads the gateway token from. The launcher
#: puts it in the app's environment, so the token never reaches a file.
TOKEN_ENV = "PROVIDER_HUB_VIBE_TOKEN"

#: Vibe's provider entries name the endpoint; the adapter appends
#: ``/v1/messages`` to it, so the base URL carries no ``/v1`` suffix.
API_STYLE = "anthropic"


def vibe_running() -> bool:
    """Whether the Vibe desktop app is running."""
    pattern = r"/Vibe\.app/Contents/MacOS/Vibe( |$)"
    return subprocess.run(["/usr/bin/pgrep", "-f", pattern], stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL).returncode == 0


def installed_app() -> str | None:
    """The installed Vibe app, or None."""
    candidates = [Path("/Applications/Vibe.app"), Path.home() / "Applications/Vibe.app"]
    return next((str(path) for path in candidates if path.is_dir()), None)


def vibe_home() -> Path:
    """The Vibe home the desktop app and the CLI share."""
    return Path(os.environ.get("VIBE_HOME", str(Path.home() / ".vibe")))


def atomic_text(path: Path, text: str, *, mode: int = 0o600) -> None:
    """Replace a file's contents in one step, never leaving it half-written."""
    if path.is_symlink():
        raise BridgeError("Refusing to replace a symlinked Vibe configuration.")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(prefix=".provider-hub-", dir=path.parent)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def _tables(document, key: str) -> AoT | None:
    """The array of tables under ``key``, when the file has one."""
    value = document.get(key)
    return value if isinstance(value, AoT) else None


def _provider_table(settings: dict) -> Table:
    """The provider entry that points Vibe at the Hub's gateway."""
    table = tomlkit.table()
    table["name"] = PROVIDER_ID
    table["api_base"] = f"http://127.0.0.1:{settings['port']}"
    table["api_key_env_var"] = TOKEN_ENV
    table["backend"] = "generic"
    table["api_style"] = API_STYLE
    table["emits_finish_reason"] = True
    return table


def _model_table(row: dict) -> Table:
    """One model row, in the spelling Vibe's schema expects."""
    table = tomlkit.table()
    table["name"] = row["name"]
    table["provider"] = row["provider"]
    table["alias"] = row["alias"]
    if row.get("display_name"):
        table["display_name"] = row["display_name"]
    table["thinking"] = row["thinking"]
    table["thinking_levels"] = list(row["thinking_levels"])
    table["supports_images"] = bool(row["supports_images"])
    table["temperature"] = float(row["temperature"])
    if row.get("max_context_length"):
        table["max_context_length"] = int(row["max_context_length"])
    if row.get("max_output_tokens"):
        table["max_output_tokens"] = int(row["max_output_tokens"])
    return table


class VibeProfile:
    """A write-ahead transaction over Vibe's own configuration file."""

    def __init__(self, root, *, vibe_home_dir=None, running=vibe_running):
        self.root = Path(root)
        self.home = Path(vibe_home_dir) if vibe_home_dir is not None else vibe_home()
        self.config = self.home / "config.toml"
        self.journal = self.root / "vibe-transaction.json"
        self.running = running

    # MARK: - state

    def read(self) -> str:
        if not self.config.is_absolute() or self.config.is_symlink():
            raise BridgeError("Vibe configuration must use a direct absolute path for managed switching.")
        return self.config.read_text() if self.config.exists() else ""

    def document(self):
        text = self.read()
        try:
            return tomlkit.parse(text)
        except Exception as exc:
            raise BridgeError("The Vibe configuration is not valid TOML. It has not been changed.") from exc

    def active(self) -> bool:
        """Whether the Hub's provider is in the configuration."""
        try:
            providers = _tables(self.document(), "providers")
        except BridgeError:
            return False
        if providers is None:
            return False
        return any(table.get("name") == PROVIDER_ID for table in providers)

    def status(self) -> dict:
        return {"vibe_running": self.running(), "vibe_profile_active": self.active(),
                "vibe_recovery_needed": self.journal.exists(),
                "vibe_app_path": installed_app(), "vibe_home": str(self.home)}

    # MARK: - the transaction

    def activate(self, settings: dict, inventory: dict) -> dict:
        """Add the Hub's provider and model rows to Vibe's configuration."""
        if self.running():
            raise BridgeError("Quit Vibe before switching its model provider.")
        private_directory(self.root)
        with (self.root / "vibe-config.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if self.journal.exists():
                raise BridgeError("Restore the previous Vibe configuration before starting a new switch.")
            projection = project_vibe(settings, inventory)
            selected = settings.get("vibe_model")
            rows = [row for row in projection["models"] if row["route"] == selected]
            if not rows:
                raise BridgeError("Choose an available model from the Vibe catalogue.")
            document = self.document()
            existed = self.config.exists()
            created: list[str] = []

            # Our own rows from an earlier run come out first; they are
            # journalled so a restore can put them back.
            removed_provider = None
            providers = _tables(document, "providers")
            if providers is not None:
                for index in range(len(providers) - 1, -1, -1):
                    if providers[index].get("name") == PROVIDER_ID:
                        removed_provider = copy.deepcopy(providers[index])
                        del providers[index]
            removed_models = []
            models = _tables(document, "models")
            if models is not None:
                for index in range(len(models) - 1, -1, -1):
                    if models[index].get("provider") == PROVIDER_ID:
                        removed_models.append(copy.deepcopy(models[index]))
                        del models[index]

            if providers is None:
                providers = tomlkit.aot()
                document["providers"] = providers
                created.append("providers")
            providers.append(_provider_table(settings))
            if models is None:
                models = tomlkit.aot()
                document["models"] = models
                created.append("models")
            for row in projection["models"]:
                models.append(_model_table(row))

            previous_active = {"present": "active_model" in document,
                               "value": document.get("active_model")}
            document["active_model"] = selected

            journal = {"version": 1,
                       "config": {"path": str(self.config), "existed": existed},
                       "provider": removed_provider.unwrap() if removed_provider is not None else None,
                       "models": [table.unwrap() for table in removed_models],
                       "created": created,
                       "active_model": previous_active,
                       "aliases": [row["route"] for row in projection["models"]]}
            atomic_json(self.journal, journal)
            atomic_text(self.config, tomlkit.dumps(document))
            return {"vibe_models": [row["route"] for row in projection["models"]],
                    "vibe_model": selected, "vibe_home": str(self.home),
                    "vibe_excluded": projection["excluded"]}

    def restore(self) -> dict:
        """Remove the Hub's rows and put back what they replaced."""
        if not self.journal.exists():
            return {"restored": False, "vibe_recovery_needed": False}
        journal = read_json(self.journal)
        try:
            document = self.document()
        except BridgeError:
            document = tomlkit.parse("")
        preserved = 0
        providers = _tables(document, "providers")
        if providers is not None:
            for index in range(len(providers) - 1, -1, -1):
                if providers[index].get("name") == PROVIDER_ID:
                    del providers[index]
            previous = journal.get("provider")
            if previous:
                table = tomlkit.table()
                for key, value in previous.items():
                    table[key] = value
                providers.append(table)
        models = _tables(document, "models")
        if models is not None:
            for index in range(len(models) - 1, -1, -1):
                if models[index].get("provider") == PROVIDER_ID:
                    del models[index]
            for previous in journal.get("models") or []:
                table = tomlkit.table()
                for key, value in previous.items():
                    table[key] = value
                models.append(table)
        # The user's own model choice is restored unless they changed it
        # while the Hub was active.
        active = journal.get("active_model") or {}
        current = document.get("active_model")
        if active.get("present"):
            if current in (journal.get("aliases") or []):
                document["active_model"] = active.get("value")
        else:
            if current in (journal.get("aliases") or []):
                del document["active_model"]
        if current is not None and current not in (journal.get("aliases") or []):
            preserved += 1
        # A key the Hub created and the user has nothing left in goes away
        # with it, so an untouched file is left exactly as it was found.
        for key in journal.get("created") or []:
            tables = _tables(document, key)
            if tables is not None and not len(tables):
                del document[key]
        existed = (journal.get("config") or {}).get("existed")
        if not existed and not len(document):
            if self.config.exists():
                self.config.unlink()
        else:
            atomic_text(self.config, tomlkit.dumps(document))
        self.journal.unlink(missing_ok=True)
        return {"restored": True, "preserved_external_changes": preserved,
                "vibe_recovery_needed": False}
