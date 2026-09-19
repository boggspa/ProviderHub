"""Codex model catalogue projection: actual provider routes, no OpenAI aliases."""
from __future__ import annotations

import hashlib
import json

from hub_config import MODEL_VARIANT_FIELDS, SUBAGENT_POOL_SIZE, connection_signature, split_route
from catalogue import fits_desktop_baseline
from providers import PROVIDERS


BASE_INSTRUCTIONS = (
    "You are a coding assistant working with the user in a shared workspace. "
    "Use the available tools to inspect files, make requested changes, and verify results. "
    "Respect the user's instructions, workspace guidance and tool permissions. "
    "When an apply_patch tool is available, make file edits with it rather than rewriting files through the shell, "
    "so your changes are recorded for review. "
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
    # The native Ultra row's own wording. The Composer renders it verbatim
    # beside native rows, and it is the only place the picker says what Ultra
    # actually buys: the delegation runtime, not just more thinking.
    "ultra": "Maximum reasoning with automatic task delegation",
}
# Ranks below ultra that may head a model's advertised ladder, low to high.
# Used to synthesize an ultra slider position as an alias for a model's own
# top advertised rank, so the slider is available even when a provider does
# not name an ultra level natively.
_HIGH_END_RANKS = ("minimal", "low", "medium", "high", "xhigh", "max")

# A route whose provider runs its thinking always-on publishes no rank at all.
# An empty ladder gives Codex nowhere to stand - it persists effort "none" -
# and because the synthesized Ultra alias only ever aliases an advertised
# rank, the route could never reach Ultra and so never opt into multi-agent
# orchestration. One deliberate placeholder rank fixes both: "high" rather
# than a middle rank, because the model really is thinking at full depth and
# a lower label would misreport what the provider does. Nothing changes on
# the wire - these providers already drop every rank sent to them.
_FIXED_REASONING_PLACEHOLDER = "high"

# A route with no reasoning axis at all lands in the identical hole, and the
# rescue above never caught it because it asks for reasoning first. Measured
# against 26.908: a projected Mistral Large row - empty ladder, no default -
# makes Codex send `reasoning: {"effort": "medium"}`, a rank that row never
# advertised and cannot serve. It is standing on a value persisted from some
# other model, which is also what the composer chip shows.
#
# The rank published is the one Codex was already asking these rows for, so
# the composer reads the same as it does today and the row simply becomes
# entitled to it. "none" would be the literal truth - the model does no extra
# reasoning - but it spends a slider position and a line of chrome saying so,
# and the distinction buys the user nothing on a route that drops the rank
# either way. Marginally generous beats verbose here.
_NO_REASONING_PLACEHOLDER = "medium"

# Only providers that reach the shared chat path, where model_effort() drops a
# rank a non-reasoning model cannot use (verified: it returns None for every
# effort value before it validates anything). The native-Responses three
# police the rank themselves and refuse the turn rather than dropping it -
# openrouter's _drop_reasoning raises on anything but none - so their rows
# keep today's shape until that is worked through per provider.
# Mirrors responses_native.NATIVE_PROVIDERS; a test keeps the two in step
# rather than importing the gateway into the catalogue projection.
_NATIVE_RESPONSES_PROVIDERS = frozenset({"grok", "ollama", "openrouter"})


def _empty_ladder_placeholder(provider_id, entry):
    """The one rank to publish for a route that advertises none, or None."""
    if entry.get("reasoning") is True:
        return _FIXED_REASONING_PLACEHOLDER
    if provider_id not in _NATIVE_RESPONSES_PROVIDERS:
        return _NO_REASONING_PLACEHOLDER
    return None


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
    if not levels:
        placeholder = _empty_ladder_placeholder(provider_id, entry)
        if placeholder is not None:
            levels.append({
                "effort": placeholder,
                "description": _EFFORT_DESCRIPTIONS[placeholder],
            })
    return levels


def _ultra_level(entry, levels):
    """Synthesize an ultra slider position aliasing the model's top rank.

    Only added when the model advertises a reasoning ladder and does not
    already name ultra itself. The alias keeps the slider available across
    providers that cap at max/xhigh/high; the gateway maps ultra onto the
    provider's highest advertised rank at request time. The description is
    always the native Ultra text: the Composer shows it verbatim next to
    native rows, so a Hub-specific suffix would read as a different level.
    """
    if "ultra" in {row["effort"] for row in levels}:
        return None
    # Read the ladder we are about to publish, not the provider's raw
    # effort_modes: a fixed-reasoning route carries its placeholder rank
    # only in `levels`, and it is exactly that route that needs Ultra to
    # become reachable.
    if not [row for row in levels if row["effort"] in _HIGH_END_RANKS]:
        return None
    return {"effort": "ultra", "description": _EFFORT_DESCRIPTIONS["ultra"]}


def _subagent_effort(levels):
    """The rank Codex should hand a sub-agent: this model's own top rank.

    This was hardcoded to "xhigh", which almost nothing here advertises -
    Mistral tops out at max, Kimi and DeepSeek at max, Ollama at max - so
    the catalogue told Codex to spawn children at a rank the same catalogue
    said the model did not support. Aim for the top and fall back down the
    model's real ladder (max, then xhigh, then high, ...) instead of naming
    a rank and hoping, so delegation never resolves to a pair the route
    would have to deny.
    """
    ranked = [row["effort"] for row in levels if row["effort"] in _HIGH_END_RANKS]
    return max(ranked, key=_HIGH_END_RANKS.index) if ranked else None


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


def _provider_suffix(provider_id, provider):
    # The historical qualifier each account/transport used when the friendly
    # name did not already identify it, including the account/transport when
    # Ollama presents an upstream brand.
    if provider_id == "ollama":
        return " · Ollama"
    if provider_id == "openrouter":
        return " · OpenRouter"
    return " · " + provider


def _composer_labels(rows):
    """Native-clean Composer labels, disambiguated only on collision.

    The Composer renders display_name verbatim next to native rows, so a
    unique friendly name stands alone ("K3", "DeepSeek V4 Pro"). The
    provider suffix appears only when two projected models would otherwise
    share a label, and the full route only when provider-qualified labels
    still collide. Routes are unique, so the fallback always terminates.
    """
    counts = {}
    for _, _, _, base in rows:
        key = base.casefold()
        counts[key] = counts.get(key, 0) + 1
    qualified = []
    for route, provider_id, provider, base in rows:
        if provider_id == "antigravity" and base.casefold().startswith("claude "):
            # These are Claude models served by another account/transport;
            # identify it even when a Legacy label prevents a name collision.
            label = base if provider.casefold() in base.casefold() else base + _provider_suffix(provider_id, provider)
            qualified.append((route, base, label))
        elif counts[base.casefold()] == 1:
            qualified.append((route, base, base))
        else:
            qualified.append((route, base, base + _provider_suffix(provider_id, provider)))
    counts = {}
    for _, _, label in qualified:
        key = label.casefold()
        counts[key] = counts.get(key, 0) + 1
    return [label if counts[label.casefold()] == 1 else base + " · " + route
            for route, base, label in qualified]


def apply_patch_qualified(settings, route):
    """Whether a route gets the JSON-wrapped apply_patch projection.

    The Codex tab's switch (codex_apply_patch_all) qualifies the whole
    catalogue except the codex_apply_patch_exclude routes; with the switch
    off only the routes listed in codex_apply_patch are qualified.
    """
    if route in (settings.get("codex_apply_patch_exclude") or []):
        return False
    if settings.get("codex_apply_patch_all") is True:
        return True
    return route in (settings.get("codex_apply_patch") or [])


def project_codex(settings, inventory):
    models = []
    label_rows = []
    excluded = []
    seen = set()
    entries = inventory.get("models", [])
    curated = settings.get("codex_catalogue")
    if curated is not None:
        by_route = {entry["id"]: entry for entry in entries}
        # A saved short Claude ID can now share a single versioned row. The
        # default's route stays stable; a duplicate alias must not add a row.
        for entry in entries:
            if entry.get("provider_id") == "claude":
                for alias in entry.get("aliases") or []:
                    by_route.setdefault(alias, entry)
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
            # A route whose window follows the account carries its candidates
            # rather than a single number, and this projection used to drop
            # them - leaving context_window null, which is the one value the
            # Codex composer cannot draw a context ring from. Its only input
            # is this field; there is no fallback and no per-request channel.
            # Resolved upwards, as the Anthropic projection already does via
            # protocol._effective_context, so both surfaces tell the route the
            # same window. The cost is on a smaller membership, where the ring
            # under-reports how full the window is.
            options = [value for value in (entry.get("context_options") or [])
                       if type(value) is int and value > 0]
            context = max(options) if options else None
        if entry.get("tools") is False:
            excluded.append({"id": route, "reason": "The model does not support coding tools."})
            continue
        if route in seen:
            continue
        seen.add(route)
        provider = entry.get("presentation", {}).get("displayProvider") or provider_id.title()
        name = entry.get("display_name") or route
        label_rows.append((route, provider_id, provider, name))
        levels = _reasoning_levels(provider_id, entry)
        ultra = _ultra_level(entry, levels)
        if ultra is not None:
            levels = list(levels) + [ultra]
        efforts = [row["effort"] for row in levels]
        service_tiers = _service_tiers(provider_id, entry)
        row = {
            "slug": route,
            "display_name": name,
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
            # Codex resolves an omitted include_* flag to false. The skills are
            # still listed either way, but without these the model never gets
            # the progressive-disclosure how-to (read SKILL.md before acting,
            # resolve relative paths against it) or the plugin briefing, so a
            # projected route reads a thinner prompt than the native rows for
            # the same installed plugins.
            "include_skills_usage_instructions": True,
            "include_plugin_usage_instructions": True,
            "include_apps_usage_instructions": True,
            "supports_reasoning_summaries": (
                provider_id == "codex" and entry.get("reasoning") is True
                and (settings.get("providers", {}).get(provider_id) or {}).get("credential_mode") == "cli"
            ),
            "default_reasoning_summary": "none",
            "support_verbosity": False,
            "default_verbosity": None,
            # This installed Codex catalogue accepts only "freeform" or null.
            # Routes stay on shell/function tools until they are qualified
            # for the JSON-wrapped apply_patch projection, either by the
            # Codex tab's switch (codex_apply_patch_all, minus
            # codex_apply_patch_exclude) or route by route (codex_apply_patch).
            # Advertising freeform makes Codex core offer apply_patch, which
            # feeds the TurnDiffTracker behind the close-out diff card.
            "apply_patch_tool_type": "freeform" if apply_patch_qualified(settings, route) else None,
            # Only advertise search where the route can actually serve it.
            # web_search is a hosted tool: the model's own server runs it, and
            # nothing behind this gateway is OpenAI, so it is honoured only
            # where the provider has search of its own for the route layer to
            # translate into. Offered anywhere else, Codex sends a tool the
            # request cannot carry and the whole turn fails rather than the
            # search quietly going missing.
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
            # node_repl - the runtime behind Computer Use and Browser Use - is
            # wired per install rather than per provider, so a projected route
            # is handed the js tools like any native row. Ask for the strict
            # auto-review the installed catalogue's own rows carry, so model
            # written JavaScript is reviewed before it runs; the desktop
            # honours the flag when its node_repl model check is enabled.
            "node_repl_auto_review_required": True,
            # Multi-agent capability: advertise the native multi_agent runtime
            # the installed Codex engine supports for reasoning-capable models.
            # ultra/v2 let the desktop slider opt into autonomous sub-agent
            # orchestration; the gateway still maps ultra onto the provider's
            # highest advertised reasoning rank at request time.
            # A published ladder is the whole requirement. This used to ask for
            # a reasoning flag as well, which quietly withheld the runtime from
            # every plain instruct route - and withheld it for no reason, since
            # the collaboration tools and the multi-agent briefing arrive from
            # the desktop's own features.multi_agent_v2 either way (measured:
            # a row with a null multi_agent_version still gets both). All the
            # flag decided was whether the route could advertise the Ultra
            # position that selects them, so a route that could already
            # delegate had no way to say so.
            "multi_agent_version": "v2" if efforts else None,
            "multi_agent_reasoning_effort": _subagent_effort(levels) if efforts else None,
        }
        # Absence is the only way to say "no search" here. The field takes
        # "text" or "text_and_image" and nothing else - not null, not "none",
        # not an empty string - and one unreadable value makes Codex discard
        # the whole catalogue file and fall back to its own models, so the
        # switch reads as the hub failing to load rather than as a route
        # declining a tool. apply_patch_tool_type above does accept null; this
        # one does not, and the asymmetry is the runtime's, not a choice here.
        if not entry.get("web_search"):
            del row["web_search_tool_type"]
        models.append(row)
    for model, label in zip(models, _composer_labels(label_rows)):
        model["display_name"] = label
    models.sort(key=lambda model: (model["display_name"].casefold(), model["slug"]))
    selected = settings.get("codex_model")
    # priority is not only picker order: Codex offers its top SUBAGENT_POOL_SIZE
    # rows as sub-agent model overrides, so this ordering decides which routes a
    # thread may delegate to. Ranked routes take the head of the list in rank
    # order; ties and gaps resolve by display name so the result is stable
    # rather than rejected. With no ranks set the order is as it always was -
    # the selected default first, then alphabetical.
    ranks = settings.get("codex_subagent_rank") or {}
    ordered = sorted(models, key=lambda model: (
        ranks.get(model["slug"], SUBAGENT_POOL_SIZE + 1),
        model["slug"] != selected,
        model["display_name"].casefold(),
        model["slug"],
    ))
    for index, model in enumerate(ordered):
        model["priority"] = index
    return {"models": models, "excluded": excluded}


def catalogue_digest(settings, inventory):
    projected = project_codex(settings, inventory)
    # priority used to be dropped here, on the grounds that the selected
    # default only moved rows around in the picker and both desktop harnesses
    # could share a snapshot when nothing else changed. It is load-bearing now:
    # Codex offers its top SUBAGENT_POOL_SIZE rows as sub-agent targets, so the
    # ordering decides what a thread may delegate to. Reusing a snapshot across
    # a change to it would leave the pool naming routes the settings no longer
    # choose, which costs a cheap rewrite to avoid.
    material = {"catalogue": projected, "planning": {
        entry["id"]: {key: entry.get(key) for key in MODEL_VARIANT_FIELDS}
        for entry in inventory.get("models", [])
    }, "accounts": {
        provider: connection_signature(provider, settings["providers"][provider])
        for provider in sorted(PROVIDERS)
    }, "gateway": {
        # Settings the gateway snapshots at startup that change what it serves
        # without changing the catalogue. codex_apply_patch_all needs no entry
        # here because project_codex already reads it, so the projection above
        # moves with it; these leave the picker byte-identical and would
        # otherwise let a flip reuse a snapshot still serving the old answer.
        "codex_goal_budget": settings.get("codex_goal_budget") is True,
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
