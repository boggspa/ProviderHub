"""Codex model catalogue projection: actual provider routes, no OpenAI aliases."""
from __future__ import annotations

import hashlib
import json

from hub_config import MODEL_VARIANT_FIELDS, connection_signature, split_route
from catalogue import fits_desktop_baseline
from providers import PROVIDERS


BASE_INSTRUCTIONS = (
    "You are a coding assistant working with the user in a shared workspace. "
    "Use the available tools to inspect files, make requested changes, and verify results. "
    "Respect the user's instructions, workspace guidance and tool permissions. "
    "State what you changed and what you verified."
)

# ChatGPT's compact Power control becomes this effort slider (and the advanced
# Effort menu) once a custom catalogue model is selected. Only ranks the
# installed app-server accepts are published; extra GPT-only Power bundles
# and per-rank duplicate models are not invented here.
_CHAT_EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra")
_EFFORT_DESCRIPTIONS = {
    "none": "No extra reasoning",
    "minimal": "Minimal reasoning",
    "low": "Low reasoning",
    "medium": "Medium reasoning",
    "high": "High reasoning",
    "xhigh": "Extra-high reasoning",
    "max": "Maximum reasoning",
    "ultra": "Ultra reasoning",
}
# Ranks below ultra that may head a model's advertised ladder, low to high.
# Used to synthesize an ultra slider position as an alias for a model's own
# top advertised rank, so the slider is available even when a provider does
# not name an ultra level natively.
_HIGH_END_RANKS = ("minimal", "low", "medium", "high", "xhigh", "max")


def _reasoning_levels(provider_id, entry):
    levels = []
    seen = set()
    for effort in entry.get("effort_modes") or []:
        if effort not in _CHAT_EFFORTS or effort in seen:
            continue
        seen.add(effort)
        levels.append({
            "effort": effort,
            "description": _EFFORT_DESCRIPTIONS.get(effort, effort.title() + " reasoning"),
        })
    return levels


def _ultra_level(entry, levels):
    """Synthesize an ultra slider position aliasing the model's top rank.

    Only added when the model advertises a reasoning ladder and does not
    already name ultra itself. The alias keeps the slider available across
    providers that cap at max/xhigh/high; the gateway maps ultra onto the
    provider's highest advertised rank at request time.
    """
    if "ultra" in {row["effort"] for row in levels}:
        return None
    advertised = [rank for rank in entry.get("effort_modes") or [] if rank in _HIGH_END_RANKS]
    if not advertised:
        return None
    top = max(advertised, key=_HIGH_END_RANKS.index)
    if top == "max":
        return {"effort": "ultra", "description": _EFFORT_DESCRIPTIONS["ultra"]}
    return {"effort": "ultra", "description": "Ultra reasoning · maps to " + top}


def _default_reasoning_level(provider_id, entry, efforts):
    if entry.get("default_effort") in efforts:
        return entry["default_effort"]
    if provider_id == "gemini":
        return None
    # The synthesized ultra level aliases the model's top advertised rank; it
    # should not become the default. Pick the highest non-ultra advertised rank.
    advertised = [e for e in efforts if e != "ultra"]
    if "high" in advertised:
        return "high"
    return advertised[-1] if advertised else None


def _service_tiers(provider_id, entry):
    # Fast is the ChatGPT Speed/Fast control, keyed off serviceTiers whose id
    # or name maps to fast/priority. Advertise it only for a documented
    # same-model Fast tier; never as extra catalogue rows.
    if provider_id == "ollama" or entry.get("fast_mode") is not True:
        return []
    if provider_id == "grok":
        return [{"id": "priority", "name": "Fast · xAI Priority",
                 "description": "Higher scheduling priority at xAI's premium token rates."}]
    return [{"id": "fast", "name": "Fast",
             "description": "Same-model Fast processing advertised by this provider."}]


def _codex_description(provider_id, provider, context, service_tiers):
    if provider_id == "ollama":
        text = provider + " via Ollama. Runtime context is managed by the daemon."
    elif provider_id == "grok":
        text = ("xAI API billing; Fast requests premium Priority processing."
                if service_tiers else "xAI API billing.")
    else:
        text = PROVIDERS[provider_id]["name"] + " model connection."
    if context is None:
        text += " Exact context is not reported; compact manually when needed."
    elif fits_desktop_baseline(context) is False:
        text += " Context is below the desktop baseline; long sessions may be rejected."
    return text


def project_codex(settings, inventory):
    models = []
    excluded = []
    seen = set()
    entries = inventory.get("models", [])
    curated = settings.get("codex_catalogue")
    if curated is not None:
        by_route = {entry["id"]: entry for entry in entries}
        picked = set()
        selected = []
        for route in curated:
            if route in picked:
                continue
            picked.add(route)
            entry = by_route.get(route)
            if entry is None:
                excluded.append({"id": route, "reason": "Selected for Codex but not advertised by the current provider catalogues. Refresh its provider or remove it from the Codex catalogue."})
                continue
            if entry.get("tools") is False:
                excluded.append({"id": route, "reason": "The model does not support coding tools."})
                continue
            selected.append(entry)
        entries = selected
    for entry in entries:
        route = entry["id"]
        provider_id, _ = split_route(route)
        if provider_id not in PROVIDERS:
            continue
        context = entry.get("runtime_context") or entry.get("context")
        if type(context) is not int or context <= 0:
            context = None
        if entry.get("tools") is False:
            excluded.append({"id": route, "reason": "The model does not support coding tools."})
            continue
        if route in seen:
            continue
        seen.add(route)
        provider = entry.get("presentation", {}).get("displayProvider") or provider_id.title()
        name = entry.get("display_name") or route
        # Include the account/transport when Ollama presents an upstream brand.
        label = name + (" · Ollama" if provider_id == "ollama" else " · OpenRouter" if provider_id == "openrouter"
                        else ("" if name.casefold().startswith(provider.casefold()) else " · " + provider))
        levels = _reasoning_levels(provider_id, entry)
        ultra = _ultra_level(entry, levels)
        if ultra is not None:
            levels = list(levels) + [ultra]
        efforts = [row["effort"] for row in levels]
        service_tiers = _service_tiers(provider_id, entry)
        models.append({
            "slug": route,
            "display_name": label,
            "description": _codex_description(provider_id, provider, context, service_tiers),
            "default_reasoning_level": _default_reasoning_level(provider_id, entry, efforts),
            "supported_reasoning_levels": levels,
            "shell_type": "default",
            "visibility": "list",
            "supported_in_api": True,
            "priority": 0,
            "additional_speed_tiers": [],
            "service_tiers": service_tiers,
            "availability_nux": None,
            "upgrade": None,
            "base_instructions": BASE_INSTRUCTIONS,
            "model_messages": None,
            "supports_reasoning_summaries": False,
            "default_reasoning_summary": "none",
            "support_verbosity": False,
            "default_verbosity": None,
            # This installed Codex catalogue accepts only "freeform" or null.
            # Native Grok/Ollama qualification uses shell/function tools.
            "apply_patch_tool_type": None,
            "web_search_tool_type": "text",
            "truncation_policy": {"mode": "bytes", "limit": 10000},
            "supports_parallel_tool_calls": entry.get("parallel_tool_calls") is True,
            "supports_image_detail_original": False,
            "context_window": context,
            "max_context_window": context,
            "fits_desktop_baseline": fits_desktop_baseline(context),
            "auto_compact_token_limit": int(min(context, entry.get("max_input") or context) * .85) if context is not None else None,
            "effective_context_window_percent": 100,
            "experimental_supported_tools": [],
            "input_modalities": ["text", "image"] if entry.get("vision") is True else ["text"],
            "supports_search_tool": False,
            # Multi-agent capability: advertise the native multi_agent runtime
            # the installed Codex engine supports for reasoning-capable models.
            # ultra/v2 let the desktop slider opt into autonomous sub-agent
            # orchestration; the gateway still maps ultra onto the provider's
            # highest advertised reasoning rank at request time.
            "multi_agent_version": "v2" if (entry.get("reasoning") is True and efforts) else None,
            "multi_agent_reasoning_effort": "xhigh" if (entry.get("reasoning") is True and efforts) else None,
        })
    models.sort(key=lambda model: (model["display_name"].casefold(), model["slug"]))
    selected = settings.get("codex_model")
    for index, model in enumerate(models):
        model["priority"] = 0 if model["slug"] == selected else index + 1
    return {"models": models, "excluded": excluded}


def catalogue_digest(settings, inventory):
    projected = project_codex(settings, inventory)
    # The selected default affects picker ordering, not runtime routing. Both
    # desktop harnesses can share an existing snapshot when only it changes.
    for model in projected["models"]:
        model.pop("priority", None)
    material = {"catalogue": projected, "planning": {
        entry["id"]: {key: entry.get(key) for key in MODEL_VARIANT_FIELDS}
        for entry in inventory.get("models", [])
    }, "accounts": {
        provider: connection_signature(provider, settings["providers"][provider])
        for provider in sorted(PROVIDERS)
    }}
    return hashlib.sha256(json.dumps(material, sort_keys=True).encode()).hexdigest()


def choices(settings, inventory):
    return [{"id": model["slug"], "name": model["display_name"], "context": model["context_window"],
             "description": model["description"],
             "fits_desktop_baseline": model["fits_desktop_baseline"]}
            for model in project_codex(settings, inventory)["models"]]


def launch_settings(settings):
    route = settings.get("codex_model")
    if not isinstance(route, str) or not route:
        raise ValueError("Choose a Codex model first.")
    if split_route(route)[0] not in PROVIDERS:
        raise ValueError("Choose a configured provider model for Codex.")
    mappings = {"Codex": route}
    # Launch preparation must freshness-check every provider that feeds the
    # prepared catalogue, not only the default model's provider.
    for index, catalogue_route in enumerate(settings.get("codex_catalogue") or []):
        mappings[f"codex-catalog-{index}"] = catalogue_route
    return {**settings, "mappings": mappings}
