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


def refresh_accents(root: Path, settings: dict, *, active: bool) -> bool:
    """Refresh an opted-in installation atomically, without enabling a mod."""
    from bridge_core import atomic_json

    target = root / CATALOGUE_FILE
    if not target.is_file() or target.is_symlink():
        return False
    atomic_json(target, accent_catalogue(settings, active=active))
    return True
