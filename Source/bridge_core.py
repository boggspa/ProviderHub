"""Provider Hub: shared configuration, credentials, and Claude profile transactions.

No transcript files are read or modified. Credentials never appear in JSON output.
Requires Python 3.11+. An existing Vibe installation is one supported runtime source.
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
import uuid

from model_names import friendly_model_name, label_catalog
from catalogue import build_catalogue, read_observations, route_specs
from cli_routes import cli_credential_mode, discover_via_cli
from hub_config import (CLAUDE_TIER_MODELS, SLOTS, claude_catalogue_rows, claude_routes, connection_signature,
                        defaults as hub_defaults, normalize as normalize_hub_settings,
                        project_catalogue, provider_presentations, qualify, split_route)
from providers import PROVIDERS, discover

PROFILE_ID = str(uuid.UUID(os.environ.get("MISTRAL_BRIDGE_PROFILE_ID", "8a93d471-d0f9-428c-b203-48fce46277bc")))
# Rows the hub adds to Claude Code's modelPicker carry this description
# prefix so they can be told apart from the user's own rows on restore.
HUB_ROW_PREFIX = "Provider Hub"


class BridgeError(Exception):
    pass


def state_root() -> Path:
    return Path(os.environ.get("MISTRAL_BRIDGE_STATE_DIR", str(Path.home() / "Library/Application Support/Mistral Bridge")))


def keychain_service() -> str:
    return os.environ.get("MISTRAL_BRIDGE_KEYCHAIN_SERVICE", "com.mistralbridge.local")


def app_display_name() -> str:
    return os.environ.get("MISTRAL_BRIDGE_DISPLAY_NAME", "Mistral Bridge")


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


def vibe_credentials() -> tuple[str, str]:
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


def credentials(settings: dict, provider_id="mistral") -> tuple[str, str]:
    if provider_id not in PROVIDERS:
        raise BridgeError("Unknown inference provider.")
    provider = PROVIDERS[provider_id]
    connection = settings["providers"][provider_id]
    mode = connection["credential_mode"]
    if provider_id == "ollama":
        return "", "Local Ollama daemon"
    if mode == "cli":
        # No secret to resolve, and none may be.  The installed CLI owns this
        # login and refreshes it; a second holder of the same rotating refresh
        # token would revoke the first.  The label keeps the (key, source)
        # contract the callers expect while the key stays empty.
        return "", f"{provider['name']} CLI login"
    if provider_id == "mistral" and mode == "vibe":
        return vibe_credentials()
    if mode == "environment":
        name = provider.get("credential_env")
        if name and os.environ.get(name):
            return os.environ[name], name
        raise BridgeError(f"{name or 'The provider credential'} is not set in this app's environment.")
    key = keychain_read(keychain_service(), provider["credential_account"])
    if key:
        return key, "macOS Keychain"
    raise BridgeError(f"Add the {provider['name']} API key in Providers to use this connection.")


def default_settings() -> dict:
    try:
        model = vibe_settings()["active_model"]
    except BridgeError:
        model = "mistral-vibe-cli-latest"
    return hub_defaults(SLOTS, model, int(os.environ.get("MISTRAL_BRIDGE_DEFAULT_PORT", "11436")))


def validate_settings(value: dict) -> dict:
    try:
        model = vibe_settings()["active_model"]
    except BridgeError:
        model = "mistral-vibe-cli-latest"
    try:
        return normalize_hub_settings(value, SLOTS, model, int(os.environ.get("MISTRAL_BRIDGE_DEFAULT_PORT", "11436")))
    except ValueError as exc:
        raise BridgeError(str(exc)) from exc


def load_settings(root: Path | None = None) -> dict:
    root = root or state_root()
    return validate_settings(read_json(root / "settings.json", default_settings()))


def private_token(root: Path, name: str) -> str:
    if name not in {"gateway-token", "reasoning-signing-key", "responses-encryption-key"}:
        raise BridgeError("Unknown private token purpose.")
    private_directory(root)
    path = root / name
    if path.is_symlink():
        raise BridgeError("Private token path must not be a symbolic link.")
    try:
        value = path.read_text().strip()
        if len(value) < 32:
            raise BridgeError("A private gateway token is invalid; restore it before continuing.")
        return value
    except FileNotFoundError:
        token = secrets.token_urlsafe(36)
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            return private_token(root, name)
        with os.fdopen(fd, "w") as stream:
            stream.write(token)
        return token


def gateway_token(root: Path) -> str:
    return private_token(root, "gateway-token")


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
    observations = read_observations(root)
    # Old events were bare Mistral IDs. Keep them historical, without assigning
    # them to a different provider that happens to use the same model string.
    observations = {qualify(*split_route(identifier)): value for identifier, value in observations.items()}
    models, summaries = [], {}
    for provider_id in PROVIDERS:
        path = root / "catalogues" / (provider_id + ".json")
        inventory = read_json(path)
        imported_legacy = False
        if not inventory and provider_id == "mistral" and settings["providers"][provider_id]["credential_mode"] == "vibe" and settings["providers"][provider_id].get("credential_revision", 0) == 0:
            legacy = read_json(root / "catalog.json")
            if legacy.get("schema_version") == 2:
                if legacy.get("raw"):
                    try:
                        vibe = vibe_settings()
                    except BridgeError:
                        vibe = {}
                    bare_settings = {"mappings": {slot: split_route(route)[1] for slot, route in settings["mappings"].items() if split_route(route)[0] == "mistral"}}
                    inventory = build_catalogue(legacy["raw"], bare_settings, vibe)
                    # Rebuilding a legacy projection must not make old metadata
                    # appear freshly fetched on every read.
                    inventory["fetched_at"] = legacy.get("fetched_at")
                else:
                    inventory = legacy
                inventory["source"] = "imported-provider-metadata"
                imported_legacy = True
        expected = connection_signature(provider_id, settings["providers"][provider_id])
        if inventory and not imported_legacy and inventory.get("connection_signature") != expected:
            summaries[provider_id] = {"needs_refresh": True, "warnings": ["Connection changed; refresh this provider's catalogue."]}
            continue
        if not inventory:
            summaries[provider_id] = {"needs_refresh": True}
            continue
        projected = project_catalogue(provider_id, inventory, settings, observations)
        models.extend(projected)
        summaries[provider_id] = {"model_count": len(projected), "source": inventory.get("source", "provider"),
                                  "fetched_at": inventory.get("fetched_at"), "warnings": inventory.get("warnings", [])}
    return {"schema_version": 3, "models": models, "providers": summaries,
            "model_count": len(models), "needs_refresh": not bool(models)}


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
    identifiers = set(claude_routes(settings).values()) | {entry["id"] for entry in entries} | set(vibe.get("configured_models", []))
    return label_catalog(identifiers, entries, vibe)


def claude_running() -> bool:
    try:
        result = subprocess.run(["pgrep", "-f", "/Claude.app/Contents/MacOS/Claude"], capture_output=True, timeout=5)
        if result.returncode == 0:
            return True
        elif result.returncode == 1:
            return False
    except OSError:
        pass
    try:
        result = subprocess.run(["/bin/ps", "-axo", "comm="], capture_output=True, timeout=5, text=True)
        return any(line.strip().endswith("/Claude.app/Contents/MacOS/Claude") for line in result.stdout.splitlines())
    except OSError:
        return False


class ClaudeProfile:
    """Small write-ahead transaction over profile pointers, never session data."""

    def __init__(self, root: Path, application_support: Path | None = None, claude_home: Path | None = None):
        self.root = root
        self.support = application_support or Path.home() / "Library/Application Support"
        self.normal = self.support / "Claude/claude_desktop_config.json"
        self.third_party = self.support / "Claude-3p/claude_desktop_config.json"
        self.meta = self.support / "Claude-3p/configLibrary/_meta.json"
        self.profile = self.meta.parent / (PROFILE_ID + ".json")
        # The managed Claude Code inside Claude Desktop reads the user's own
        # Claude Code settings; catalogue rows are taught to it there.
        self.code_settings = (claude_home or Path.home() / ".claude") / "settings.json"
        self.journal = root / "profile-transaction.json"

    def active(self) -> bool:
        return read_json(self.meta).get("appliedId") == PROFILE_ID

    def prepare(self, settings: dict, token: str) -> dict:
        features = settings.get("claude_features") or {}
        profile = {
            "inferenceProvider": "gateway", "inferenceCredentialKind": "static",
            "inferenceGatewayBaseUrl": f"http://127.0.0.1:{settings['port']}",
            "inferenceGatewayApiKey": token, "inferenceGatewayAuthScheme": "bearer",
            "deploymentDisplayName": app_display_name(), "chatTabEnabled": True,
            "modelDiscoveryEnabled": True, "disableDeploymentModeChooser": True,
            # Claude preserves explicit existing context choices; this asks new
            # selections to prefer a truthful advertised 1M variant when one
            # exists, without fabricating a separate fixed-context model.
            "modelPrefer1mContext": True,
            "disableEssentialTelemetry": True, "disableNonessentialTelemetry": True,
            "autoModeEnabled": settings.get("auto_mode", False),
            # Chat Completions has no hosted WebSearch tool; local WebFetch and
            # all normal coding tools retain the desktop's permission handling.
            "disabledBuiltinTools": ["WebSearch"],
        }
        # Opt-in profile features. Each is a local capability whose inference
        # still runs through this gateway. Keys are added only when enabled:
        # Claude's config health check flags any key its build does not
        # recognise, even an explicit false (2.110.x does not recognise
        # builtinBrowserEnabled), and all of these default off in its schema.
        for setting, field in (("dictation", "dictationEnabled"),
                               ("builtin_browser", "builtinBrowserEnabled"),
                               ("scheduled_tasks", "scheduledTasksEnabled"),
                               ("cowork_tab", "coworkTabEnabled")):
            if features.get(setting) is True:
                profile[field] = True
        # claudeInChromeEnabled is deliberately never written: Claude reports
        # it is delivered only by the Anthropic-hosted control plane and
        # cannot be set from a local profile, so its presence alone triggers
        # the "configuration may need attention" banner.
        return profile

    @staticmethod
    def hub_row(row) -> bool:
        return isinstance(row, dict) and str(row.get("description", "")).startswith(HUB_ROW_PREFIX)

    def code_settings_operation(self, settings: dict):
        """Journal operation teaching Claude Code the catalogue ids, or (None, why not).

        Claude Code treats an id it does not know as an unknown model: no
        effort ladder, an assumed context window and a notice in every
        session. Its documented remedy is a modelPicker row whose behavesAs
        names a model it knows, so each catalogue row gets one that points at
        its tier's Claude model. enableWorkflows is added on request because
        the Ultracode level needs dynamic workflows.
        """
        rows = claude_catalogue_rows(settings) if settings.get("claude_code_settings", True) else []
        workflows = settings.get("claude_workflows") is True
        if not rows and not workflows:
            return None, None
        try:
            current = read_json(self.code_settings)
        except BridgeError as exc:
            return None, str(exc)
        changes, hub_ids = {}, []
        if rows:
            picker = current.get("modelPicker")
            picker = dict(picker) if isinstance(picker, dict) else {}
            options = picker.get("options")
            kept = [row for row in options if not self.hub_row(row)] if isinstance(options, list) else []
            labels = settings.get("_display_names") or {}
            added = []
            for row in rows:
                target = CLAUDE_TIER_MODELS[row["tier"]]
                hub_ids.append(row["id"])
                added.append({"model": row["id"], "label": labels.get(row["route"], row["route"]),
                              "description": f"{HUB_ROW_PREFIX} · {row['route']} · behaves as {target}",
                              "behavesAs": target})
            picker["options"] = kept + added
            changes["modelPicker"] = picker
        if workflows:
            changes["enableWorkflows"] = True
        return {"path": str(self.code_settings), "existed": self.code_settings.exists(), "changes": changes,
                "previous": {k: {"present": k in current, "value": current.get(k)} for k in changes},
                "hub_rows": hub_ids}, None

    def activate(self, settings: dict, token: str, *, require_closed: bool = True) -> dict:
        if require_closed and claude_running():
            raise BridgeError("Claude is already open. Finish your current work and quit Claude, then launch it here.")
        if self.journal.exists():
            raise BridgeError("A previous profile transaction needs restoration before launching.")
        private_directory(self.root)
        profile = self.prepare(settings, token)
        meta = read_json(self.meta)
        entries = [e for e in meta.get("entries", []) if not isinstance(e, dict) or e.get("id") != PROFILE_ID]
        entries.append({"id": PROFILE_ID, "name": app_display_name()})
        targets = [(self.profile, profile), (self.meta, {"appliedId": PROFILE_ID, "entries": entries, "hybridPointer": None}),
                   (self.third_party, {"deploymentMode": "3p"}), (self.normal, {"deploymentMode": "3p"})]
        operations = []
        for path, changes in targets:
            before = read_json(path)
            operations.append({"path": str(path), "existed": path.exists(), "changes": changes,
                               "previous": {k: {"present": k in before, "value": before.get(k)} for k in changes}})
        code_operation, code_note = self.code_settings_operation(settings)
        if code_operation:
            operations.append(code_operation)
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
        result = {"active": True, "profile_id": PROFILE_ID}
        if code_operation:
            result["claude_code_settings"] = "written"
        elif code_note:
            result["claude_code_settings"] = "skipped: " + code_note
        return result

    def restore_code_settings(self, data: dict, op: dict) -> bool:
        """Undo the Claude Code settings operation, leaving the user's rows alone."""
        changed = False
        hub_ids = set(op.get("hub_rows", []))
        for key, applied in op["changes"].items():
            previous = op["previous"][key]
            current = data.get(key)
            if key == "modelPicker" and current != applied and isinstance(current, dict):
                # Edited since activation: drop only the rows the hub added.
                options = current.get("options")
                if isinstance(options, list):
                    kept = [row for row in options
                            if not self.hub_row(row) and not (isinstance(row, dict) and row.get("model") in hub_ids)]
                    if kept != options:
                        picker = {**current, "options": kept}
                        if not kept and not previous["present"] and set(picker) <= {"options"}:
                            data.pop(key, None)
                        else:
                            data[key] = picker
                        changed = True
                continue
            if current != applied or key not in data:
                continue
            if previous["present"]:
                data[key] = previous["value"]
            else:
                data.pop(key, None)
            changed = True
        return changed

    def restore(self, *, require_closed: bool = True, rollback: bool = False) -> dict:
        if require_closed and claude_running():
            raise BridgeError("Quit Claude before restoring its previous setup.")
        if not self.journal.exists():
            return {"restored": False, "message": "No saved profile change needs restoration."}
        journal = read_json(self.journal)
        allowed = {str(p) for p in (self.normal, self.third_party, self.meta, self.profile, self.code_settings)}
        if journal.get("version") != 1 or any(op.get("path") not in allowed for op in journal.get("operations", [])):
            raise BridgeError("The profile recovery journal is invalid; no Claude files were changed.")
        # If another manager selected a different profile, preserve its choices.
        external_switch = read_json(self.meta).get("appliedId") != PROFILE_ID and not rollback
        skipped = []
        for op in reversed(journal["operations"]):
            path = Path(op["path"])
            if path == self.code_settings:
                try:
                    data = read_json(path)
                except BridgeError:
                    skipped.append(path.name + ":unreadable")
                    continue
                if self.restore_code_settings(data, op):
                    if not data and not op["existed"]:
                        path.unlink(missing_ok=True)
                    else:
                        atomic_json(path, data)
                continue
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
    states = {}
    for provider_id in PROVIDERS:
        try:
            _, source = credentials(settings, provider_id)
            states[provider_id] = {"credential_found": True, "credential_source": source}
        except (BridgeError, subprocess.TimeoutExpired) as exc:
            states[provider_id] = {"credential_found": False, "credential_source": "Setup needed", "credential_error": str(exc)}
    result.update(states["mistral"])
    result["provider_states"] = states
    result["provider_definitions"] = provider_presentations(settings)
    result["profile_id"] = PROFILE_ID
    cached = cached_catalogue(settings, root)
    result["catalog"] = cached.get("models", [])
    result["catalog_summary"] = {k: v for k, v in cached.items() if k not in {"models", "raw"}}
    result["friendly_names"] = model_labels(settings, result["catalog"], result.get("vibe", {}))
    return result


def discover_provider(settings, provider_id, root=None):
    root = root or state_root()
    if provider_id not in PROVIDERS:
        raise BridgeError("Unknown inference provider.")
    try:
        key, source = credentials(settings, provider_id)
    except BridgeError:
        if PROVIDERS[provider_id].get("capabilities", {}).get("model_discovery") != "documentation":
            raise
        key, source = "", "Public provider documentation; credential not configured"
    if cli_credential_mode(settings, provider_id):
        # The CLI owns this login. Its own catalogue report is the inventory:
        # no HTTP endpoint is contacted and no key is resolved for this mode.
        inventory = discover_via_cli(provider_id)
    else:
        try:
            inventory = discover(provider_id, settings["providers"][provider_id], key)
        except ValueError as exc:
            raise BridgeError(str(exc).replace(key, "[redacted]") if key else str(exc)) from exc
    inventory["connection_signature"] = connection_signature(provider_id, settings["providers"][provider_id])
    inventory["fetched_at"] = time_now_iso()
    atomic_json(root / "catalogues" / (provider_id + ".json"), inventory)
    return source


def bootstrap_metadata(root: Path):
    """Seed a preview with non-secret Mistral catalogue data only."""
    source = os.environ.get("MISTRAL_BRIDGE_SEED_CATALOG")
    target = root / "catalog.json"
    if not source or target.exists():
        return
    seed = read_json(Path(source))
    if seed.get("schema_version") == 2 and isinstance(seed.get("raw"), dict):
        private_directory(root)
        atomic_json(target, seed)


def time_now_iso():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()
