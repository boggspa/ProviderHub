"""Mistral Bridge: local configuration, credentials, and Claude profile transactions.

No transcript files are read or modified. Credentials never appear in JSON output.
Requires Python 3.11+, supplied by the user's existing Vibe installation.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import ssl
import subprocess
import tempfile
import tomllib
import urllib.error
import urllib.request

from model_names import friendly_model_name, label_catalog
from catalogue import build_catalogue, read_observations, route_specs

PROFILE_ID = "8a93d471-d0f9-428c-b203-48fce46277bc"
SLOTS = [
    ("claude-fable-5", "Fable 5", "fable", True),
    ("claude-opus-5", "Opus 5", "opus", True),
    ("claude-sonnet-5", "Sonnet 5", "sonnet", True),
    ("claude-haiku-4-5", "Haiku 4.5", "haiku", True),
    ("claude-sonnet-4-6", "Sonnet 4.6", "sonnet", False),
]


class BridgeError(Exception):
    pass


def state_root() -> Path:
    return Path(os.environ.get("MISTRAL_BRIDGE_STATE_DIR", str(Path.home() / "Library/Application Support/Mistral Bridge")))


def private_directory(path: Path) -> None:
    if path.is_symlink():
        raise BridgeError("The configuration directory must not be a symbolic link.")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)


def read_json(path: Path, default=None):
    if path.is_symlink():
        raise BridgeError("Refusing to follow a configuration file symlink.")
    if not path.exists():
        return {} if default is None else default
    try:
        value = json.loads(path.read_text())
    except (ValueError, OSError) as exc:
        raise BridgeError(f"Cannot read {path.name}; it has not been changed.") from exc
    if not isinstance(value, dict):
        raise BridgeError(f"Expected a JSON object in {path.name}.")
    return value


def atomic_json(path: Path, value: dict) -> None:
    if path.is_symlink():
        raise BridgeError("Refusing to overwrite a configuration file symlink.")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temp = tempfile.mkstemp(prefix=".mistral-bridge-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, indent=2, ensure_ascii=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def vibe_settings() -> dict:
    root = Path(os.environ.get("VIBE_HOME", str(Path.home() / ".vibe")))
    raw = {}
    path = root / "config.toml"
    if path.exists():
        try:
            raw = tomllib.loads(path.read_text())
        except (ValueError, OSError) as exc:
            raise BridgeError("Vibe config.toml could not be read.") from exc
    # Vibe's installed defaults are read as data, without instantiating its agent
    # or invoking configuration migrations, login, telemetry, or a model call.
    defaults = {"alias": "mistral-medium-3.5", "name": "mistral-vibe-cli-latest", "provider": "mistral", "display_name": ""}
    try:
        import ast
        import importlib.util
        spec = importlib.util.find_spec("vibe")
        if spec and spec.submodule_search_locations:
            source = Path(next(iter(spec.submodule_search_locations))) / "core/config/vibe_schema.py"
            tree = ast.parse(source.read_text())
            for node in tree.body:
                if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "DEFAULT_ACTIVE_MODEL_CONFIG" for t in node.targets):
                    for kw in node.value.keywords:
                        if kw.arg in defaults and isinstance(kw.value, ast.Constant):
                            defaults[kw.arg] = kw.value.value
    except (ImportError, OSError, ValueError, AttributeError, SyntaxError):
        pass
    models = [dict(defaults)]
    for item in raw.get("models", []):
        if not isinstance(item, dict):
            continue
        if item.get("alias") == defaults["alias"]:
            models[0].update(item)
        elif item.get("name"):
            models.append(item)
    active_alias = raw.get("active_model") or defaults["alias"]
    active = next((m for m in models if m.get("alias") == active_alias or m.get("name") == active_alias), None)
    if not active:
        active = {"name": active_alias, "alias": active_alias, "provider": "mistral"}
    provider = next((p for p in raw.get("providers", []) if p.get("name") == active.get("provider")), {})
    endpoint = provider.get("api_base", "https://api.mistral.ai/v1")
    if endpoint.rstrip("/") != "https://api.mistral.ai/v1":
        raise BridgeError("Vibe is using a custom provider. This prototype supports api.mistral.ai; select a Mistral provider in Vibe first.")
    return {
        "active_alias": active_alias,
        "active_model": active["name"],
        "active_display_name": active.get("display_name") or friendly_model_name(active_alias),
        "key_name": provider.get("api_key_env_var", "MISTRAL_API_KEY"),
        "vibe_home": str(root),
        "configured_models": sorted({m["name"] for m in models if m.get("provider", "mistral") == "mistral"}),
    }


def keychain_read(service: str, account: str) -> str | None:
    # Captured privately; never forward this subprocess's stdout/stderr to logs.
    result = subprocess.run(["/usr/bin/security", "find-generic-password", "-s", service, "-a", account, "-w"], capture_output=True, timeout=25)
    return result.stdout.decode().strip() if result.returncode == 0 else None


def credentials(settings: dict) -> tuple[str, str]:
    if settings.get("credential_mode", "vibe") == "separate":
        key = keychain_read("com.mistralbridge.local", "MISTRAL_API_KEY")
        if key:
            return key, "Bridge Keychain"
        raise BridgeError("Save a Mistral API key in Connection settings, or select Vibe credentials.")
    info = vibe_settings()
    name = info["key_name"]
    if os.environ.get(name):
        return os.environ[name], "Vibe environment"
    env_file = Path(info["vibe_home"]) / ".env"
    if env_file.is_file():
        # python-dotenv is already supplied by Vibe. Never source a shell file.
        try:
            from dotenv import dotenv_values
            key = dotenv_values(env_file).get(name)
        except ImportError:
            key = None
            for line in env_file.read_text().splitlines():
                key_name, sep, value = line.partition("=")
                if sep and key_name.strip() == name:
                    key = value.strip().strip("\"'")
                    break
        if key:
            return key, "Vibe .env"
    for service in ("ai.mistral.vibe", "vibe"):
        key = keychain_read(service, name)
        if key:
            return key, "Vibe Keychain"
    raise BridgeError("No Vibe credential was found. Sign in through Vibe, then click Reconnect.")


def default_settings() -> dict:
    try:
        model = vibe_settings()["active_model"]
    except BridgeError:
        model = "mistral-vibe-cli-latest"
    return {"port": 11436, "credential_mode": "vibe",
            "mappings": {slot[0]: model for slot in SLOTS}, "auto_stop": True, "auto_mode": False}


def validate_settings(value: dict) -> dict:
    result = default_settings()
    for key in result:
        if key in value:
            result[key] = value[key]
    if type(result["port"]) is not int or not 1024 <= result["port"] <= 65535:
        raise BridgeError("Choose a port between 1024 and 65535.")
    if result["credential_mode"] not in {"vibe", "separate"}:
        raise BridgeError("Choose Vibe credentials or a separate API key.")
    mappings = result["mappings"]
    if not isinstance(mappings, dict):
        raise BridgeError("Invalid model mappings.")
    import re
    for slot, _, _, _ in SLOTS:
        model = mappings.get(slot, "").strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,199}", model):
            raise BridgeError("Enter a valid Mistral API model ID for every slot.")
        mappings[slot] = model
    result["mappings"] = {s[0]: mappings[s[0]] for s in SLOTS}
    result["auto_stop"] = bool(result["auto_stop"])
    result["auto_mode"] = bool(result["auto_mode"])
    return result


def load_settings(root: Path | None = None) -> dict:
    root = root or state_root()
    return validate_settings(read_json(root / "settings.json", default_settings()))


def gateway_token(root: Path) -> str:
    private_directory(root)
    path = root / "gateway-token"
    if path.is_symlink():
        raise BridgeError("Gateway token path must not be a symbolic link.")
    try:
        return path.read_text().strip()
    except FileNotFoundError:
        token = secrets.token_urlsafe(36)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as stream:
            stream.write(token)
        return token


def ssl_context():
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


def catalog(key: str) -> dict:
    req = urllib.request.Request("https://api.mistral.ai/v1/models", headers={"Authorization": "Bearer " + key, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, context=ssl_context(), timeout=25) as response:
            data = json.load(response)
    except urllib.error.HTTPError as exc:
        raise BridgeError(f"Mistral model discovery returned HTTP {exc.code}.") from exc
    except (OSError, ValueError) as exc:
        raise BridgeError("Could not reach Mistral model discovery.") from exc
    return data


def cached_catalogue(settings, root=None):
    root = root or state_root()
    cached = read_json(root / "catalog.json")
    if cached.get("schema_version") != 2:
        return {"models": [], "needs_refresh": True}
    if cached.get("raw"):
        try:
            vibe = vibe_settings()
        except BridgeError:
            vibe = {}
        result = build_catalogue(cached["raw"], settings, vibe, read_observations(root))
        result["fetched_at"] = cached.get("fetched_at")
        return result
    return cached


def attach_model_specs(settings, root=None):
    inventory = cached_catalogue(settings, root)
    return {**settings, "_model_specs": route_specs(inventory),
            "_display_names": {identifier: entry["display_name"] for identifier, entry in route_specs(inventory).items()}}, inventory


def model_labels(settings: dict, entries=(), vibe=None):
    if entries and all("aliases" in entry and "display_name" in entry for entry in entries):
        return {identifier: entry["display_name"] for entry in entries for identifier in entry["aliases"]}
    if vibe is None:
        try:
            vibe = vibe_settings()
        except BridgeError:
            vibe = {}
    identifiers = set(settings["mappings"].values()) | {entry["id"] for entry in entries} | set(vibe.get("configured_models", []))
    return label_catalog(identifiers, entries, vibe)


def claude_running() -> bool:
    result = subprocess.run(["/bin/ps", "-axo", "comm="], capture_output=True, timeout=5, text=True)
    return any(line.strip().endswith("/Claude.app/Contents/MacOS/Claude") for line in result.stdout.splitlines())


class ClaudeProfile:
    """Small write-ahead transaction over profile pointers, never session data."""

    def __init__(self, root: Path, application_support: Path | None = None):
        self.root = root
        self.support = application_support or Path.home() / "Library/Application Support"
        self.normal = self.support / "Claude/claude_desktop_config.json"
        self.third_party = self.support / "Claude-3p/claude_desktop_config.json"
        self.meta = self.support / "Claude-3p/configLibrary/_meta.json"
        self.profile = self.meta.parent / (PROFILE_ID + ".json")
        self.journal = root / "profile-transaction.json"

    def active(self) -> bool:
        return read_json(self.meta).get("appliedId") == PROFILE_ID

    def prepare(self, settings: dict, token: str) -> dict:
        return {
            "inferenceProvider": "gateway", "inferenceCredentialKind": "static",
            "inferenceGatewayBaseUrl": f"http://127.0.0.1:{settings['port']}",
            "inferenceGatewayApiKey": token, "inferenceGatewayAuthScheme": "bearer",
            "deploymentDisplayName": "Mistral Bridge", "chatTabEnabled": True,
            "modelDiscoveryEnabled": True, "disableDeploymentModeChooser": True,
            "disableEssentialTelemetry": True, "disableNonessentialTelemetry": True,
            "autoModeEnabled": settings.get("auto_mode", False),
            # Chat Completions has no hosted WebSearch tool; local WebFetch and
            # all normal coding tools retain the desktop's permission handling.
            "disabledBuiltinTools": ["WebSearch"],
        }

    def activate(self, settings: dict, token: str, *, require_closed: bool = True) -> dict:
        if require_closed and claude_running():
            raise BridgeError("Claude is already open. Finish your current work and quit Claude, then launch it here.")
        if self.journal.exists():
            raise BridgeError("A previous profile transaction needs restoration before launching.")
        private_directory(self.root)
        profile = self.prepare(settings, token)
        meta = read_json(self.meta)
        entries = [e for e in meta.get("entries", []) if not isinstance(e, dict) or e.get("id") != PROFILE_ID]
        entries.append({"id": PROFILE_ID, "name": "Mistral Bridge"})
        targets = [(self.profile, profile), (self.meta, {"appliedId": PROFILE_ID, "entries": entries, "hybridPointer": None}),
                   (self.third_party, {"deploymentMode": "3p"}), (self.normal, {"deploymentMode": "3p"})]
        operations = []
        for path, changes in targets:
            before = read_json(path)
            operations.append({"path": str(path), "existed": path.exists(), "changes": changes,
                               "previous": {k: {"present": k in before, "value": before.get(k)} for k in changes}})
        atomic_json(self.journal, {"version": 1, "operations": operations})
        try:
            for op in operations:
                path = Path(op["path"])
                data = read_json(path)
                data.update(op["changes"])
                atomic_json(path, data)
        except Exception:
            self.restore(require_closed=False, rollback=True)
            raise
        return {"active": True, "profile_id": PROFILE_ID}

    def restore(self, *, require_closed: bool = True, rollback: bool = False) -> dict:
        if require_closed and claude_running():
            raise BridgeError("Quit Claude before restoring its previous setup.")
        if not self.journal.exists():
            return {"restored": False, "message": "No saved profile change needs restoration."}
        journal = read_json(self.journal)
        allowed = {str(p) for p in (self.normal, self.third_party, self.meta, self.profile)}
        if journal.get("version") != 1 or any(op.get("path") not in allowed for op in journal.get("operations", [])):
            raise BridgeError("The profile recovery journal is invalid; no Claude files were changed.")
        # If another manager selected a different profile, preserve its choices.
        external_switch = read_json(self.meta).get("appliedId") != PROFILE_ID and not rollback
        skipped = []
        for op in reversed(journal["operations"]):
            path = Path(op["path"])
            data = read_json(path)
            changed = False
            for key, applied in op["changes"].items():
                if path == self.meta and key == "entries" and isinstance(data.get(key), list):
                    previous_entries = op["previous"][key]["value"] or []
                    ours_before = [e for e in previous_entries if isinstance(e, dict) and e.get("id") == PROFILE_ID]
                    cleaned = [e for e in data[key] if not isinstance(e, dict) or e.get("id") != PROFILE_ID] + ours_before
                    if data[key] != applied or external_switch:
                        if cleaned != data[key]:
                            data[key] = cleaned
                            changed = True
                        continue
                if external_switch and path != self.profile:
                    skipped.append(path.name + ":" + key)
                    continue
                if data.get(key) != applied or key not in data:
                    skipped.append(path.name + ":" + key)
                    continue
                previous = op["previous"][key]
                if previous["present"]:
                    data[key] = previous["value"]
                else:
                    data.pop(key, None)
                changed = True
            if changed:
                if not data and not op["existed"]:
                    path.unlink(missing_ok=True)
                else:
                    atomic_json(path, data)
        self.journal.unlink()
        return {"restored": True, "preserved_external_changes": len(skipped)}


def inspect_state(root: Path) -> dict:
    settings = load_settings(root)
    result = {"settings": settings, "claude_installed": Path("/Applications/Claude.app").exists(),
              "claude_running": claude_running(), "recovery_needed": (root / "profile-transaction.json").exists(),
              "profile_active": ClaudeProfile(root).active()}
    try:
        result["vibe"] = vibe_settings()
    except BridgeError as exc:
        result["vibe_error"] = str(exc)
    try:
        _, source = credentials(settings)
        result.update(credential_found=True, credential_source=source)
    except (BridgeError, subprocess.TimeoutExpired) as exc:
        result.update(credential_found=False, credential_source="Not connected", credential_error=str(exc))
    cached = cached_catalogue(settings, root)
    result["catalog"] = cached.get("models", [])
    result["catalog_summary"] = {k: v for k, v in cached.items() if k not in {"models", "raw"}}
    result["friendly_names"] = model_labels(settings, result["catalog"], result.get("vibe", {}))
    return result
