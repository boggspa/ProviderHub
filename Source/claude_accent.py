"""Credential-free presentation data for the official Claude Code mod API.

Only an explicitly installed mod opts a Hub state directory in. The mod
matches its session's gateway URL and exact catalogue id before drawing.
"""
from __future__ import annotations

import re
from pathlib import Path

from branding import resolve_presentation
from hub_config import claude_routes, split_route
from model_names import friendly_model_name

CATALOGUE_FILE = "claude-accents.json"
PLUGIN_ID = "provider-hub-accents@provider-hub-local"


def accent_catalogue(settings: dict, *, active: bool) -> dict:
    """Project only display fields; never serialize settings or credentials."""
    models = {}
    labels = settings.get("_display_names") or {}
    for identifier, route in claude_routes(settings).items():
        provider, model = split_route(route)
        presentation = resolve_presentation(
            provider, model, settings.get("branding_overrides"),
            supplied_label=labels.get(route) or friendly_model_name(model),
        )
        label = re.sub(r"[\x00-\x1f\x7f]", " ", presentation["modelLabel"])[:160].strip()
        models[identifier] = {
            "accent": presentation["accent"],
            "modelLabel": label,
        }
    return {"schema": 1, "active": active,
            "gatewayUrl": f"http://127.0.0.1:{settings['port']}", "models": models}


def refresh_accents(root: Path, settings: dict | None, *, active: bool) -> bool:
    """Refresh an opted-in installation atomically, without enabling a mod."""
    from bridge_core import atomic_json

    target = root / CATALOGUE_FILE
    if not target.is_file() or target.is_symlink():
        return False
    data = (accent_catalogue(settings, active=True) if active else
            {"schema": 1, "active": False, "gatewayUrl": "", "models": {}})
    atomic_json(target, data)
    return True


def profile_accents(root: Path, settings: dict | None, *, active: bool) -> str | None:
    """Presentation failure must not block launching or restoring a profile."""
    from bridge_core import BridgeError

    try:
        if refresh_accents(root, settings, active=active):
            return "refreshed" if active else "inactive"
    except (OSError, ValueError, BridgeError):
        return "skipped: could not refresh the accent catalogue"
    return None


def main() -> int:
    """Install explicitly, or refresh a prototype used by an older Hub build."""
    import argparse
    import json
    import os
    import plistlib
    import shutil
    import subprocess

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("install", "refresh"))
    parser.add_argument("--app", type=Path, required=True,
                        help="Provider Hub app whose profile and state directory to use")
    parser.add_argument("--claude", type=Path, default=Path.home() / ".local/bin/claude")
    args = parser.parse_args()
    info = plistlib.loads((args.app / "Contents/Info.plist").read_bytes())
    state_name, profile_id = info["BridgeStateName"], info["BridgeProfileID"]
    if not isinstance(state_name, str) or Path(state_name).name != state_name or state_name in {".", ".."}:
        parser.error("App has an invalid BridgeStateName")
    root = Path.home() / "Library/Application Support" / state_name
    os.environ["MISTRAL_BRIDGE_PROFILE_ID"] = profile_id
    os.environ["MISTRAL_BRIDGE_DEFAULT_PORT"] = str(info["BridgeDefaultPort"])
    # Import after selecting the app; bridge_core binds PROFILE_ID at import.
    from bridge_core import ClaudeProfile, atomic_json, attach_model_specs, load_settings, private_directory

    if args.command == "install":
        version = subprocess.check_output([str(args.claude), "--version"], text=True)
        match = re.search(r"(\d+)\.(\d+)\.(\d+)", version)
        if not match or tuple(map(int, match.groups())) < (2, 1, 287):
            parser.error("Update the standalone Claude Code CLI to 2.1.287 or later first")
        source = Path(__file__).with_name("claude-mods")
        subprocess.run([str(args.claude), "plugin", "validate", str(source), "--strict"], check=True)
        subprocess.run([str(args.claude), "plugin", "validate", str(source / "provider-hub-accents"), "--strict"], check=True)
        # Keep the local marketplace outside an app bundle or checkout so
        # replacing a build or moving the repository cannot break the install.
        private_directory(root)
        marketplace = root / "claude-mods"
        if marketplace.is_symlink():
            parser.error("The managed marketplace must not be a symbolic link")
        marketplaces = json.loads(subprocess.check_output(
            [str(args.claude), "plugin", "marketplace", "list", "--json"], text=True))
        existing = next((row for row in marketplaces if row.get("name") == "provider-hub-local"), None)
        if existing and (existing.get("source") != "directory" or existing.get("path") != str(marketplace)):
            parser.error("provider-hub-local already belongs to another source; no marketplace was changed")
        shutil.copytree(source, marketplace, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("tests", "types", "tsconfig.json", "__pycache__"))
        if existing:
            subprocess.run([str(args.claude), "plugin", "marketplace", "update", "provider-hub-local"], check=True)
        else:
            subprocess.run([str(args.claude), "plugin", "marketplace", "add", str(marketplace)], check=True)
        installed = json.loads(subprocess.check_output([str(args.claude), "plugin", "list", "--json"], text=True))
        if any(row.get("id") == PLUGIN_ID and row.get("scope") == "user" for row in installed):
            subprocess.run([str(args.claude), "plugin", "update", PLUGIN_ID], check=True)
        else:
            subprocess.run([str(args.claude), "plugin", "install", PLUGIN_ID, "--scope", "user",
                            "--config", "cataloguePath=" + str(root / CATALOGUE_FILE)], check=True)
        subprocess.run([str(args.claude), "plugin", "configure", PLUGIN_ID, "--values-stdin"],
                       input=json.dumps({"cataloguePath": str(root / CATALOGUE_FILE)}), text=True, check=True)

    target = root / CATALOGUE_FILE
    if target.is_symlink():
        parser.error("The accent catalogue must not be a symbolic link")
    if args.command == "refresh" and not target.is_file():
        parser.error("Install the mod before refreshing its catalogue")
    settings, _ = attach_model_specs(load_settings(root), root)
    data = accent_catalogue(settings, active=ClaudeProfile(root).active())
    atomic_json(target, data)
    print(f"Accent catalogue: {len(data['models'])} exact model IDs; "
          f"profile {'active' if data['active'] else 'inactive'}")
    print(f"Saved {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
