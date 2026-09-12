"""Canonical model inventory, provider limits, and observed route availability."""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path

from model_names import friendly_model_name


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def retired(card, today=None):
    if card.get("archived") is True:
        return True
    value = card.get("deprecation")
    if not isinstance(value, str) or not value:
        return False
    try:
        date = datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    except ValueError:
        return False
    return date <= (today or datetime.now(timezone.utc).date())


def read_observations(root: Path):
    result = {}
    for name in ("activity.previous.jsonl", "activity.jsonl"):
        path = root / name
        if not path.exists():
            continue
        for line in path.read_text().splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            model = event.get("model")
            if not model:
                continue
            observation = result.setdefault(model, {})
            if event.get("event") == "completed":
                observation.update(last_success=event.get("time"), status="responded")
            elif event.get("event") == "error":
                status = event.get("status")
                if status == 429:
                    observation.update(status="quota_limited", last_error=event.get("time"))
                elif event.get("model_unavailable"):
                    observation.update(status="unavailable", last_error=event.get("time"))
    return result


def build_catalogue(raw, settings, vibe=None, observations=None):
    vibe = vibe or {}
    observations = observations or {}
    groups = {}
    chat_count = retired_count = 0
    for card in raw.get("data", []):
        caps = card.get("capabilities") or {}
        if not caps.get("completion_chat"):
            continue
        chat_count += 1
        if retired(card):
            retired_count += 1
            continue
        context = card.get("max_context_length")
        if type(context) is not int or context <= 0:
            continue
        canonical = card.get("name") or card["id"]
        # Actual context/capability variants stay separate, even when their
        # canonical family name happens to be the same.
        group_key = (canonical, context, bool(caps.get("reasoning")), bool(caps.get("vision")), bool(caps.get("function_calling")))
        groups.setdefault(group_key, []).append(card)
    models = []
    configured = list(settings["mappings"].values())
    for (canonical, context, reasoning, vision, tools), cards in groups.items():
        ids = [card["id"] for card in cards]
        candidates = ([vibe.get("active_model")] + configured +
                      [identifier for identifier in ids if observations.get(identifier, {}).get("status") == "responded"] +
                      [canonical] + ids)
        selected = next(identifier for identifier in candidates if identifier in ids)
        label = friendly_model_name(canonical)
        if vibe.get("active_model") in ids and vibe.get("active_display_name"):
            label = vibe["active_display_name"]
        selected_card = next(card for card in cards if card["id"] == selected)
        observation = observations.get(selected, {})
        models.append({
            "id": selected, "canonical_id": canonical, "display_name": label,
            "context": context, "aliases": sorted(ids), "tools": tools,
            "vision": vision, "reasoning": reasoning,
            # These are the actual levels used by the installed Vibe Mistral
            # backend. Do not invent separate models for UI effort positions.
            "effort_modes": ["none", "high"] if reasoning else [],
            "fast_mode": False,
            "inference_status": observation.get("status", "advertised"),
            "last_success": observation.get("last_success"),
            "deprecation": selected_card.get("deprecation"),
            "billing_model_name": selected_card.get("billing_model_name"),
        })
    counts = Counter(model["display_name"] for model in models)
    for model in models:
        if counts[model["display_name"]] > 1:
            model["display_name"] += f" · {model['context']:,} tokens"
    return {"schema_version": 2, "fetched_at": now_iso(), "models": sorted(models, key=lambda model: model["display_name"]),
            "advertised_ids": chat_count, "retired_ids": retired_count,
            "aliases_grouped": max(0, chat_count - retired_count - len(models)), "raw": raw}


def route_specs(catalogue):
    specs = {}
    for model in catalogue.get("models", []):
        for identifier in model.get("aliases", [model["id"]]):
            specs[identifier] = model
    return specs


def status_label(model):
    return {"advertised": "Advertised · not tested", "responded": "Previously responded", "quota_limited": "Quota or rate limited",
            "unavailable": "Rejected by provider"}.get(model.get("inference_status"), "Not tested")
