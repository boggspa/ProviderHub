"""Reversible, formatting-preserving configuration for the Codex desktop."""
from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).with_name("vendor")))
import tomlkit

from bridge_core import BridgeError, atomic_json, private_directory, read_json
from codex_catalogue import project_codex


PROVIDER_ID = "provider_hub"
ROOT_KEYS = (
    "model", "model_provider", "model_catalog_json", "model_context_window",
    "model_auto_compact_token_limit", "model_reasoning_effort", "model_reasoning_summary",
    "model_supports_reasoning_summaries", "model_verbosity", "service_tier", "web_search",
)


def codex_running():
    pattern = r"(ChatGPT|Codex)\.app/Contents/MacOS/(ChatGPT|Codex)( |$)"
    return subprocess.run(["/usr/bin/pgrep", "-f", pattern], stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL).returncode == 0


def installed_app():
    candidates = [Path("/Applications/ChatGPT.app"), Path("/Applications/Codex.app"),
                  Path.home() / "Applications/ChatGPT.app", Path.home() / "Applications/Codex.app"]
    return next((str(path) for path in candidates if path.is_dir()), None)


def digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


def atomic_text(path, text, *, mode=0o600):
    if path.is_symlink():
        raise BridgeError("Refusing to replace a symlinked Codex configuration.")
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


def parse(text):
    try:
        return tomlkit.parse(text)
    except Exception as exc:
        raise BridgeError("The Codex configuration is not valid TOML. It has not been changed.") from exc


def value_at(document, key):
    raw = document.unwrap()
    return {"present": key in raw, "value": raw.get(key)}


class CodexProfile:
    def __init__(self, root, *, config_home=None, running=codex_running, python=None):
        self.root = Path(root)
        self.config_home = Path(config_home) if config_home is not None else Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
        self.config = self.config_home / "config.toml"
        self.catalogue = self.root / "codex-models.json"
        self.journal = self.root / "codex-transaction.json"
        self.backup = self.root / "codex-config-before.toml"
        self.running = running
        self.python = python or sys.executable

    def read(self):
        if not self.config_home.is_absolute() or self.config_home.is_symlink() or self.config.is_symlink():
            raise BridgeError("Codex configuration must use a direct absolute path for managed switching.")
        return self.config.read_text() if self.config.exists() else ""

    def status(self):
        active = False
        if self.journal.exists():
            document = parse(self.read())
            active = document.get("model_provider") == PROVIDER_ID
        return {"codex_running": self.running(), "codex_profile_active": active,
                "codex_recovery_needed": self.journal.exists(), "codex_app_path": installed_app()}

    def provider(self, port):
        return {"name": "Provider Hub", "base_url": f"http://127.0.0.1:{port}/v1",
                "wire_api": "responses", "supports_websockets": False,
                "supports_standalone_web_search": False,
                "auth": {"command": self.python,
                         "args": ["-I", "-B", str(Path(__file__).with_name("codex_token.py")), str(self.root)],
                         "timeout_ms": 5000, "refresh_interval_ms": 300000}}

    def activate(self, settings, inventory):
        if self.running():
            raise BridgeError("Quit Codex / ChatGPT before switching its model provider.")
        private_directory(self.root)
        with (self.root / "codex-config.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if self.journal.exists():
                raise BridgeError("Restore the previous Codex configuration before starting a new switch.")
            catalog = project_codex(settings, inventory)
            selected = settings.get("codex_model")
            if selected not in {model["slug"] for model in catalog["models"]}:
                raise BridgeError("Choose an available model from the Codex catalogue.")
            original = self.read()
            document = parse(original)
            before = document.unwrap()
            if PROVIDER_ID in before.get("model_providers", {}):
                raise BridgeError("A provider_hub provider already exists outside this launch transaction. It has not been overwritten.")
            original_values = {key: value_at(document, key) for key in ROOT_KEYS}
            try:
                json.dumps(original_values)
            except (TypeError, ValueError) as exc:
                raise BridgeError("Codex model settings contain unsupported values and were not changed.") from exc
            applied = {"model": selected, "model_provider": PROVIDER_ID, "model_catalog_json": str(self.catalogue),
                       "web_search": "disabled"}
            for key in ROOT_KEYS:
                if key in applied:
                    document[key] = applied[key]
                elif key in document:
                    del document[key]
            if "model_providers" not in document:
                document["model_providers"] = tomlkit.table()
            owned_provider = self.provider(settings["port"])
            document["model_providers"][PROVIDER_ID] = owned_provider
            updated = tomlkit.dumps(document)
            after = parse(updated).unwrap()
            expected = copy.deepcopy(before)
            for key in ROOT_KEYS:
                expected.pop(key, None)
            expected.update(applied)
            expected.setdefault("model_providers", {})[PROVIDER_ID] = owned_provider
            if after != expected:
                raise BridgeError("Codex configuration verification failed; no configuration was changed.")
            mode = self.config.stat().st_mode & 0o777 if self.config.exists() else 0o600
            atomic_text(self.backup, original)
            atomic_json(self.catalogue, {"models": catalog["models"]})
            journal = {"version": 1, "config_path": str(self.config), "existed": self.config.exists(),
                       "mode": mode, "before": original_values,
                       "providers_existed": "model_providers" in before,
                       "applied": {key: value_at(document, key) for key in ROOT_KEYS},
                       "provider": owned_provider, "before_digest": digest(original),
                       "applied_digest": digest(updated)}
            atomic_json(self.journal, journal)
            if self.running() or self.read() != original:
                self.journal.unlink()
                raise BridgeError("Codex configuration changed during preparation. No settings were overwritten; retry after closing the app.")
            atomic_text(self.config, updated, mode=mode)
            return {"codex_profile_active": True, "codex_recovery_needed": True,
                    "model_count": len(catalog["models"])}

    def restore(self):
        if self.running():
            raise BridgeError("Quit Codex / ChatGPT before restoring its previous configuration.")
        if not self.journal.exists():
            return {"restored": False, "preserved_external_changes": 0}
        with (self.root / "codex-config.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            journal = read_json(self.journal)
            if (journal.get("version") != 1 or journal.get("config_path") != str(self.config)
                    or set(journal.get("before", {})) != set(ROOT_KEYS)
                    or set(journal.get("applied", {})) != set(ROOT_KEYS)
                    or not isinstance(journal.get("provider"), dict)):
                raise BridgeError("The Codex recovery journal is invalid. No settings were changed.")
            original = self.backup.read_text()
            if digest(original) != journal.get("before_digest"):
                raise BridgeError("The Codex configuration backup could not be verified.")
            current = self.read()
            if digest(current) == journal["applied_digest"]:
                updated = original
                preserved = 0
            elif digest(current) == journal["before_digest"]:
                # Recovery after a failure before the configuration write.
                updated = current
                preserved = 0
            else:
                document = parse(current)
                preserved = 0
                provider_changed = document.get("model_provider") != PROVIDER_ID
                selected_changed = value_at(document, "model") != journal["applied"]["model"]
                # A picker change to another model in our own catalogue is a
                # managed selection. An unrelated new selection is not ours.
                known = {model["slug"] for model in read_json(self.catalogue).get("models", [])}
                if not provider_changed and selected_changed and document.get("model") not in known:
                    raise BridgeError("Codex now selects an external model under the hub provider. Its edits were preserved; select another provider or a hub model before restoring.")
                for key in ROOT_KEYS:
                    if (provider_changed and key != "web_search") or (value_at(document, key) != journal["applied"][key] and key != "model"):
                        preserved += 1
                        continue
                    saved = journal["before"][key]
                    if saved["present"]:
                        document[key] = saved["value"]
                    elif key in document:
                        del document[key]
                providers = document.get("model_providers", {})
                if PROVIDER_ID in providers:
                    if providers[PROVIDER_ID].unwrap() == journal["provider"] and document.get("model_provider") != PROVIDER_ID:
                        del providers[PROVIDER_ID]
                        if not providers and not journal["providers_existed"]:
                            del document["model_providers"]
                    else:
                        preserved += 1
                updated = tomlkit.dumps(document)
                parse(updated)
            if self.running() or self.read() != current:
                raise BridgeError("Codex configuration changed during restoration. Its current settings were preserved; retry after closing the app.")
            if not journal["existed"] and updated == "":
                if self.config.exists():
                    self.config.unlink()
            else:
                atomic_text(self.config, updated, mode=journal["mode"])
            self.journal.unlink()
            return {"restored": True, "preserved_external_changes": preserved}
