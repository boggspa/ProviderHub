"""Vibe model catalogue projection: Hub routes as Vibe ``[[models]]`` entries.

Vibe keeps its own model list in ``$VIBE_HOME/config.toml``: every entry names
a provider, the model id to send upstream, and the alias the picker shows.
The desktop app reads that file through the bundled app-server, so writing it
is how the Hub offers its own catalogue to Vibe - the same idea as the Codex
catalogue file, in Vibe's own config dialect.

The model id Vibe sends is the Hub's route (``provider/model``). The gateway
resolves that string straight back to the route it advertised, so no second
mapping table has to stay in step with the catalogue.

The wire is Anthropic Messages. Vibe's generic Anthropic adapter sends the
effort rank in ``output_config.effort`` and thinking-off as
``thinking: {"type": "disabled"}`` - the two spellings the gateway's Messages
surface already reads for Claude Desktop. The base URL therefore carries no
``/v1`` suffix: the adapter appends ``/v1/messages`` itself.
"""
from __future__ import annotations

import hashlib
import json

from catalogue import fits_desktop_baseline
from effort_map import display_effort_modes
from hub_config import MODEL_VARIANT_FIELDS, connection_signature, qualify, split_route
from providers import PROVIDERS

#: The Vibe provider entry the Hub owns. Vibe addresses providers by name, so
#: this is the only handle Vibe has on the Hub's gateway.
PROVIDER_ID = "provider_hub"
PROVIDER_NAME = "Provider Hub"

#: Vibe's config schema accepts exactly these thinking levels, and no others.
VIBE_THINKING_LEVELS = ("off", "low", "medium", "high", "max")

#: Hub rank -> Vibe level. Monotone, so a rank the Hub counts as more
#: thinking never lands on a lower Vibe level. ``xhigh`` and ``ultra`` fold
#: onto ``high``/``max`` because Vibe has no rung between them.
VIBE_THINKING = {
    "none": "off", "minimal": "off", "low": "low", "medium": "medium",
    "high": "high", "xhigh": "high", "max": "max", "ultra": "max",
}

#: Vibe samples at 0.2 unless told otherwise. Every model the Hub routes
#: expects the vendor default, and the reasoning models the Hub serves are
#: documented to want 1.0, so the projection asks for it explicitly.
VIBE_TEMPERATURE = 1.0


def thinking_levels(spec: dict) -> list[str]:
    """Vibe's five levels, as many as the route actually advertises.

    A route with no reasoning axis offers ``off`` only: Vibe sends the
    level on every request, and offering a rung the provider cannot serve
    would turn a working route into a rejected one.
    """
    levels: list[str] = []
    for rank in display_effort_modes(spec):
        level = VIBE_THINKING.get(rank)
        if level and level not in levels:
            levels.append(level)
    return levels or ["off"]


def default_thinking(spec: dict, levels: list[str]) -> str:
    """The level the route starts on.

    The model's own advertised default wins; otherwise the highest rank that
    is not the synthesized top one, matching the Codex projection's rule so
    both surfaces start a route at the same depth.
    """
    advertised = spec.get("default_effort")
    level = VIBE_THINKING.get(advertised) if isinstance(advertised, str) else None
    if level and level in levels:
        return level
    if "high" in levels:
        return "high"
    return levels[-1]


def _context(entry: dict) -> int | None:
    """The window the route advertises, or None when nobody described it."""
    for field in ("runtime_context", "context"):
        value = entry.get(field)
        if type(value) is int and value > 0:
            return value
    options = [value for value in (entry.get("context_options") or [])
               if type(value) is int and value > 0]
    return max(options) if options else None


def _label(entry: dict) -> str:
    """The picker label: the model's name, qualified by its provider.

    Two providers can advertise the same model name, so the provider is
    always named - unless it is already in the label.
    """
    provider = (entry.get("presentation") or {}).get("displayProvider") or ""
    name = entry.get("display_name") or entry["id"]
    if provider and provider.casefold() not in name.casefold():
        return f"{name} · {provider}"
    return name


def _description(entry: dict, context: int | None) -> str:
    if type(context) is int and context > 0:
        return f"{context:,} token context · {entry['id']}"
    return f"{entry['id']} · provider-managed context"



def project_vibe(settings: dict, inventory: dict) -> dict:
    """The Vibe ``[[models]]`` rows for the catalogue, and what was left out.

    A saved selection narrows the list the way the Codex catalogue does; an
    absent one serves every compatible route. Routes nobody advertises are
    reported rather than dropped silently.
    """
    models: list[dict] = []
    excluded: list[dict] = []
    seen: set[str] = set()
    entries = list(inventory.get("models", []))
    curated = settings.get("vibe_catalogue")
    if curated is not None:
        by_route = {entry["id"]: entry for entry in entries}
        selected = []
        for route in curated:
            entry = by_route.get(route)
            if entry is None:
                excluded.append({"id": route, "reason": "Selected for Vibe but not advertised by the current provider catalogues. Refresh its provider or remove it from the Vibe catalogue."})
                continue
            selected.append(entry)
        entries = selected
    for entry in entries:
        route = entry["id"]
        provider_id, _ = split_route(route)
        if provider_id not in PROVIDERS:
            continue
        if PROVIDERS[provider_id].get("protocol", "chat_completions") == "agent_session":
            # Agent modes are sessions, not chat models; Vibe sends a
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
        levels = thinking_levels(entry)
        row = {
            "route": route,
            # What Vibe sends upstream. The gateway resolves the route back
            # to the provider and model it advertised.
            "name": route,
            "alias": route,
            "provider": PROVIDER_ID,
            "display_name": _label(entry),
            "thinking": default_thinking(entry, levels),
            "thinking_levels": levels,
            "supports_images": entry.get("vision") is True,
            "temperature": VIBE_TEMPERATURE,
            "description": _description(entry, context),
            "context": context,
            "fits_desktop_baseline": fits_desktop_baseline(context),
        }
        output = entry.get("max_output")
        if type(output) is int and output > 0:
            row["max_output_tokens"] = output
        models.append(row)
    models.sort(key=lambda row: (row["display_name"].casefold(), row["route"]))
    return {"models": models, "excluded": excluded}


def choices(settings: dict, inventory: dict) -> list[dict]:
    """The routes the picker offers, in the shape the app reads."""
    return [{"id": row["route"], "name": row["display_name"], "context": row["context"],
             "description": row["description"],
             "fits_desktop_baseline": row["fits_desktop_baseline"]}
            for row in project_vibe(settings, inventory)["models"]]


def catalogue_digest(settings: dict, inventory: dict) -> str:
    """A fingerprint of what this surface would serve.

    The gateway publishes it while it runs and activation checks it, so a
    catalogue that changed after preparation cannot be handed to Vibe by a
    gateway still serving the older one.
    """
    projected = project_vibe(settings, inventory)
    material = {"catalogue": projected, "planning": {
        entry["id"]: {key: entry.get(key) for key in MODEL_VARIANT_FIELDS}
        for entry in inventory.get("models", [])
    }, "accounts": {
        provider: connection_signature(provider, (settings.get("providers") or {})[provider])
        for provider in sorted(PROVIDERS)
        if provider in (settings.get("providers") or {})
    }}
    return hashlib.sha256(json.dumps(material, sort_keys=True).encode()).hexdigest()


def launch_settings(settings: dict) -> dict:
    """The settings a launch preparation freshness-checks.

    Every provider that feeds the prepared catalogue is checked, not only the
    default model's provider, so a route that went stale is caught before
    Vibe is launched with it.
    """
    route = settings.get("vibe_model")
    if not isinstance(route, str) or not route:
        raise ValueError("Choose a Vibe model first.")
    route = qualify(*split_route(route))
    if split_route(route)[0] not in PROVIDERS:
        raise ValueError("Choose a configured provider model for Vibe.")
    mappings = {"Vibe": route}
    for index, catalogue_route in enumerate(settings.get("vibe_catalogue") or []):
        mappings[f"vibe-catalog-{index}"] = catalogue_route
    return {**settings, "mappings": mappings}
