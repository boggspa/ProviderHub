"""The launch selection the desktop apps actually receive.

A saved selection can outlive what a provider advertises: a model is sunset,
a key stops covering a route, a catalogue goes stale. Until now any such
route blocked the whole launch until the user edited the selection. This
module projects the saved selection onto what is servable right now, leaving
out routes nobody advertises and routes of providers whose preparation
failed, as long as at least one usable provider remains. The saved selection
is never rewritten, so a route that comes back is served again on the next
launch, and every omission is returned so the app can say what it left out.

The prepare step records its decision in ``launch-plan.json``. Activation and
the gateway process apply the same projection from that record, so the three
agree on the snapshot without repeating discovery.
"""
from __future__ import annotations

from pathlib import Path

from codex_catalogue import choices as codex_choices
from hub_config import SLOTS, _TIER_FALLBACKS, claude_routes, provider_label, split_route

PLAN_FILE = "launch-plan.json"


def advertised_routes(inventory: dict) -> set[str]:
    routes = set()
    for entry in inventory.get("models", []):
        routes.add(entry["id"])
        if entry.get("provider_id") == "claude":
            routes.update(entry.get("aliases") or [])
    return routes


def usable_providers(lifecycle: dict) -> tuple[list[str], dict[str, str]]:
    """Provider ids whose preparation succeeded, and the blocked ones' reasons."""
    usable, blocked = [], {}
    for provider_id, state in (lifecycle.get("providers") or {}).items():
        if state.get("status") == "blocked":
            error = state.get("error") or {}
            blocked[provider_id] = error.get("message") or "launch preparation failed"
        else:
            usable.append(provider_id)
    return usable, blocked


def require_launchable(lifecycle: dict) -> dict:
    """Raise the structured preparation error only when no provider is usable."""
    from catalogue_lifecycle import CataloguePreparationError
    usable, blocked = usable_providers(lifecycle)
    if blocked and not usable:
        raise CataloguePreparationError(lifecycle)
    return lifecycle


def _omission(settings: dict, surface: str, route: str, reason: str, **extra) -> dict:
    provider_id, model = split_route(route)
    name = provider_label(settings, provider_id)
    return {"surface": surface, "route": route, "provider_id": provider_id,
            "provider_name": name, "model": model, "label": f"{model} ({name})",
            "reason": reason, **extra}


def effective_launch_settings(settings: dict, inventory: dict, *, blocked=None) -> tuple[dict, list[dict]]:
    """Return (settings projection, omissions).

    ``blocked`` maps a provider id to the reason its preparation failed; its
    routes are left out alongside routes the inventory does not advertise.
    """
    blocked = dict(blocked or {})
    advertised = advertised_routes(inventory)

    def usable(route: str) -> bool:
        return route in advertised and split_route(route)[0] not in blocked

    def reason(route: str) -> str:
        provider_id, model = split_route(route)
        if provider_id in blocked:
            return blocked[provider_id]
        return f"{provider_label(settings, provider_id)} no longer advertises {model}"

    result = dict(settings)
    omissions: list[dict] = []

    if settings.get("claude_catalogue") is not None:
        kept = []
        for entry in settings["claude_catalogue"]:
            if usable(entry["route"]):
                kept.append(dict(entry))
            else:
                omissions.append(_omission(settings, "claude", entry["route"], reason(entry["route"]),
                                           tier=entry.get("tier")))
        # A tier keeps one default while it still has a row; the picker and
        # Claude Code's family requests start from that default.
        for tier in sorted({row["tier"] for row in kept}):
            members = [row for row in kept if row["tier"] == tier]
            if not any(row.get("tier_default") for row in members):
                members[0]["tier_default"] = True
        result["claude_catalogue"] = kept
    else:
        mappings = dict(settings.get("mappings") or {})
        slot_tier = {slot: tier for slot, _, tier, _ in SLOTS}
        for slot, route in list(mappings.items()):
            if usable(route):
                continue
            tier = slot_tier.get(slot)
            order = (tier, *_TIER_FALLBACKS.get(tier, ())) if tier else ()
            preferred = [other for want in order for other, _, other_tier, _ in SLOTS if other_tier == want]
            replacement = next((mappings[other] for other in preferred
                                if other in mappings and usable(mappings[other])), None)
            if replacement is None:
                replacement = next((candidate for candidate in mappings.values() if usable(candidate)), None)
            if replacement is None:
                continue  # nothing can stand in; servable_shortfall reports it
            mappings[slot] = replacement
            omissions.append(_omission(settings, "claude", route, reason(route),
                                       slot=slot, replacement=replacement))
        result["mappings"] = mappings

    if settings.get("codex_catalogue") is not None:
        kept = []
        for route in settings["codex_catalogue"]:
            if usable(route):
                kept.append(route)
            else:
                omissions.append(_omission(settings, "codex", route, reason(route)))
        result["codex_catalogue"] = kept

    model = settings.get("codex_model")
    if isinstance(model, str) and model and not usable(model):
        available = [choice["id"] for choice in codex_choices(result, inventory) if usable(choice["id"])]
        if available:
            result["codex_model"] = available[0]
            existing = next((item for item in omissions if item["surface"] == "codex" and item["route"] == model), None)
            if existing is not None:
                existing["replacement"] = available[0]
            else:
                omissions.append(_omission(settings, "codex", model, reason(model), replacement=available[0]))
    return result, omissions


def servable_shortfall(settings: dict, inventory: dict, *, blocked=None, surface: str, saved: dict | None = None) -> list[str]:
    """Routes the projection still relies on that cannot be served.

    Empty means the surface can launch. Non-empty means nothing could stand
    in for these routes, which is the one case that still has to block.
    ``saved`` is the unprojected selection, used to name what was selected
    when the projection emptied a curated Claude catalogue entirely.
    """
    blocked = dict(blocked or {})
    advertised = advertised_routes(inventory)
    if surface == "claude":
        if settings.get("claude_catalogue") is not None and not settings["claude_catalogue"]:
            selected = (saved or {}).get("claude_catalogue") or []
            return sorted({row["route"] for row in selected}) or ["(no Claude models left)"]
        routes = set(claude_routes(settings).values())
    else:
        model = settings.get("codex_model")
        routes = {model} if isinstance(model, str) and model else set()
    return sorted(route for route in routes
                  if route not in advertised or split_route(route)[0] in blocked)


def read_plan(root) -> dict:
    from bridge_core import read_json
    try:
        plan = read_json(Path(root) / PLAN_FILE)
    except Exception:
        return {}
    return plan if isinstance(plan, dict) else {}


def write_plan(root, surface: str, lifecycle: dict, omissions: list[dict], checked_at: str) -> dict:
    """Record this surface's decision; a provider that prepared fine clears
    its earlier block on either surface so a fixed key is not held against it."""
    from bridge_core import atomic_json, private_directory
    usable, blocked = usable_providers(lifecycle)
    plan = read_plan(root)
    for state in plan.values():
        if isinstance(state, dict):
            for provider_id in usable:
                (state.get("blocked") or {}).pop(provider_id, None)
    plan[surface] = {"blocked": blocked, "checked_at": checked_at,
                     "omitted_routes": sorted({item["route"] for item in omissions})}
    private_directory(Path(root))
    atomic_json(Path(root) / PLAN_FILE, plan)
    return plan


def plan_blocked(root) -> dict[str, str]:
    blocked: dict[str, str] = {}
    for state in read_plan(root).values():
        if isinstance(state, dict):
            blocked.update({k: v for k, v in (state.get("blocked") or {}).items() if isinstance(v, str)})
    return blocked


def apply_plan(settings: dict, inventory: dict, root) -> tuple[dict, list[dict]]:
    """The projection activation and the gateway use, from the recorded plan."""
    return effective_launch_settings(settings, inventory, blocked=plan_blocked(root))
