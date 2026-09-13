"""Codex model catalogue projection: actual provider routes, no OpenAI aliases."""
from __future__ import annotations

import hashlib
import json

from hub_config import MODEL_VARIANT_FIELDS, connection_signature, split_route
from providers import PROVIDERS


BASE_INSTRUCTIONS = (
    "You are a coding assistant working with the user in a shared workspace. "
    "Use the available tools to inspect files, make requested changes, and verify results. "
    "Respect the user's instructions, workspace guidance and tool permissions. "
    "State what you changed and what you verified."
)


def project_codex(settings, inventory):
    models = []
    excluded = []
    seen = set()
    for entry in inventory.get("models", []):
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
        efforts = list(entry.get("effort_modes") or []) if provider_id != "ollama" else []
        service_tiers = ([{"id": "priority", "name": "Fast · xAI Priority",
                          "description": "Higher scheduling priority at xAI's premium token rates."}]
                         if provider_id == "grok" else [])
        models.append({
            "slug": route,
            "display_name": label,
            "description": (provider + " via Ollama. Runtime context is managed by the daemon." if provider_id == "ollama" else
                            "xAI API billing; Fast requests premium Priority processing." if provider_id == "grok" else
                            PROVIDERS[provider_id]["name"] + " model connection.") + (" Exact context is not reported; compact manually when needed." if context is None else ""),
            "default_reasoning_level": (entry["default_effort"] if entry.get("default_effort") in efforts
                                        else "high" if "high" in efforts else None),
            "supported_reasoning_levels": [{"effort": effort, "description": effort.title() + " reasoning"} for effort in efforts],
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
            "auto_compact_token_limit": int(min(context, entry.get("max_input") or context) * .85) if context is not None else None,
            "effective_context_window_percent": 100,
            "experimental_supported_tools": [],
            "input_modalities": ["text", "image"] if entry.get("vision") is True else ["text"],
            "supports_search_tool": False,
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
             "description": model["description"]} for model in project_codex(settings, inventory)["models"]]


def launch_settings(settings):
    route = settings.get("codex_model")
    if not isinstance(route, str) or not route:
        raise ValueError("Choose a Codex model first.")
    if split_route(route)[0] not in PROVIDERS:
        raise ValueError("Choose a configured provider model for Codex.")
    return {**settings, "mappings": {"Codex": route}}
