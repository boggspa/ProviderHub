"""Project Hub routes into the Antigravity gateway's Gemini model list."""
from __future__ import annotations

import base64
import hashlib
import json

from catalogue import fits_desktop_baseline
from effort_map import display_effort_modes
from hub_config import MODEL_VARIANT_FIELDS, connection_signature, qualify, split_route
from providers import PROVIDERS


#: The Hub's own handle on the Antigravity surface. Antigravity keys its
#: custom models by the model id, so this only labels the projection.
PROVIDER_ID = "provider_hub"
PROVIDER_NAME = "Provider Hub"

#: The output cap Antigravity assumes when a model does not advertise one.
#: Its own Gemini rows run to 65,535 output tokens, and a route that says
#: nothing gets the same allowance.
DEFAULT_MAX_OUTPUT = 65536

#: The window assumed for a route that advertises no context limit. The
#: agent needs a number to plan against; an unknown window is not a licence
#: to refuse the route.
DEFAULT_CONTEXT = 200000


def slug_for(route: str) -> str:
    """The flat model id Antigravity sends for a Hub route."""
    provider_id, model = split_route(route)
    canonical = qualify(provider_id, model)
    return "ph-" + base64.urlsafe_b64encode(canonical.encode()).decode().rstrip("=")


def route_for_slug(slug: str) -> str | None:
    """The Hub route a flat model id names, or None when it names none."""
    if not isinstance(slug, str) or not slug.startswith("ph-") or len(slug) > 512:
        return None
    try:
        encoded = slug[3:]
        raw = base64.b64decode(encoded + "=" * (-len(encoded) % 4), altchars=b"-_", validate=True).decode()
    except (ValueError, UnicodeError):
        return None
    provider_id, separator, model = raw.partition("/")
    if not separator or not provider_id or not model:
        return None
    if provider_id not in PROVIDERS:
        return None
    try:
        route = qualify(provider_id, model)
        return route if slug_for(route) == slug else None
    except ValueError:
        return None


def _context(entry: dict) -> int:
    """The window the route advertises, or a safe default."""
    for field in ("runtime_context", "context"):
        value = entry.get(field)
        if type(value) is int and value > 0:
            return value
    options = [value for value in (entry.get("context_options") or [])
               if type(value) is int and value > 0]
    return max(options) if options else DEFAULT_CONTEXT


def _output_cap(entry: dict, context: int) -> int:
    """The output allowance for a route, kept below its window.

    Antigravity subtracts the output allowance from the context window before
    it plans a turn, and refuses the route when the remainder is negative. A
    small-window route would therefore be unusable against the default
    allowance, so the allowance is capped at a quarter of the window.
    """
    advertised = entry.get("max_output")
    cap = advertised if type(advertised) is int and advertised > 0 else DEFAULT_MAX_OUTPUT
    return max(1024, min(cap, max(1024, context // 4)))


def _label(entry: dict) -> str:
    """The picker label: the model's name, qualified by its provider."""
    provider = (entry.get("presentation") or {}).get("displayProvider") or ""
    name = entry.get("display_name") or entry["id"]
    if provider and provider.casefold() not in name.casefold():
        return f"{name} · {provider}"
    return name


def _description(entry: dict, context: int) -> str:
    """The row's description line."""
    if type(context) is int and context > 0:
        return f"{context:,} token context · {entry['id']}"
    return f"{entry['id']} · provider-managed context"


def _features(entry: dict) -> dict:
    """The capabilities the agent should assume for a route."""
    reasoning = entry.get("reasoning") is True
    return {
        "supportsImages": entry.get("vision") is True,
        "supportsVideo": False,
        "supportsPdf": False,
        "supportsAudio": False,
        "supportsToolCalls": entry.get("tools") is not False,
        "supportsThinking": reasoning,
        "zeroShotCapable": True,
    }


def project_antigravity(settings: dict, inventory: dict) -> dict:
    """The Antigravity model entries for the catalogue, and what was left out.

    A saved selection narrows the list the way the Codex and Vibe catalogues
    do; an absent one serves every compatible route. Routes nobody advertises
    are reported rather than dropped silently.
    """
    models: list[dict] = []
    excluded: list[dict] = []
    seen: set[str] = set()
    entries = list(inventory.get("models", []))
    curated = settings.get("antigravity_catalogue")
    if curated is not None:
        by_route = {entry["id"]: entry for entry in entries}
        selected = []
        for route in curated:
            entry = by_route.get(route)
            if entry is None:
                excluded.append({"id": route, "reason": "Selected for Antigravity but not advertised by the current provider catalogues. Refresh its provider or remove it from the Antigravity catalogue."})
                continue
            selected.append(entry)
        entries = selected
    for entry in entries:
        route = entry["id"]
        provider_id, _ = split_route(route)
        if provider_id not in PROVIDERS:
            continue
        if PROVIDERS[provider_id].get("protocol", "chat_completions") == "agent_session":
            # Agent modes are sessions, not chat models; Antigravity sends a
            # completion, so they have no meaning on this surface.
            excluded.append({"id": route, "reason": "The provider offers agent sessions rather than chat models."})
            continue
        if entry.get("tools") is False:
            excluded.append({"id": route, "reason": "The model does not support coding tools."})
            continue
        if route in seen:
            continue
        seen.add(route)
        context = _context(entry)
        output = _output_cap(entry, context)
        slug = slug_for(route)
        models.append({
            "route": route,
            # What Antigravity sends upstream. The gateway turns it back into
            # the provider and model it advertised.
            "model_name": slug,
            "display_name": _label(entry),
            "description": _description(entry, context),
            "context": context,
            "max_output_tokens": output,
            "reasoning": entry.get("reasoning") is True,
            "effort_modes": list(display_effort_modes(entry)),
            "features": _features(entry),
            "fits_desktop_baseline": fits_desktop_baseline(context),
        })
    models.sort(key=lambda row: (row["display_name"].casefold(), row["route"]))
    return {"models": models, "excluded": excluded}


def choices(settings: dict, inventory: dict) -> list[dict]:
    """The routes the picker offers, in the shape the app reads."""
    return [{"id": row["route"], "name": row["display_name"], "context": row["context"],
             "description": row["description"],
             "fits_desktop_baseline": row["fits_desktop_baseline"]}
            for row in project_antigravity(settings, inventory)["models"]]


def catalogue_digest(settings: dict, inventory: dict) -> str:
    """A fingerprint of what this surface would serve.

    The gateway publishes it while it runs and activation checks it, so a
    catalogue that changed after preparation cannot be handed to Antigravity
    by a gateway still serving the older one.
    """
    projected = project_antigravity(settings, inventory)
    material = {"catalogue": projected, "planning": {
        entry["id"]: {key: entry.get(key) for key in MODEL_VARIANT_FIELDS}
        for entry in inventory.get("models", [])
    }, "accounts": {
        provider: connection_signature(provider, (settings.get("providers") or {})[provider])
        for provider in sorted(PROVIDERS)
        if provider in (settings.get("providers") or {})
    }}
    return hashlib.sha256(json.dumps(material, sort_keys=True).encode()).hexdigest()


def launch_settings(settings: dict, inventory: dict | None = None) -> dict:
    """The settings a launch preparation freshness-checks.

    Every provider that feeds the prepared catalogue is checked, not only the
    default model's provider, so a route that went stale is caught before
    Antigravity is launched with it.
    """
    route = settings.get("antigravity_model")
    if not isinstance(route, str) or not route:
        raise ValueError("Choose an Antigravity model first.")
    provider_id, separator, model = route.partition("/")
    if not separator or provider_id not in PROVIDERS:
        raise ValueError("Choose a configured provider model for Antigravity.")
    route = qualify(provider_id, model)
    mappings = {"Antigravity": route}
    routes = settings.get("antigravity_catalogue")
    if routes is None and inventory is not None:
        routes = [row["route"] for row in project_antigravity(settings, inventory)["models"]]
    for index, catalogue_route in enumerate(routes or []):
        mappings[f"antigravity-catalog-{index}"] = catalogue_route
    return {**settings, "mappings": mappings}
