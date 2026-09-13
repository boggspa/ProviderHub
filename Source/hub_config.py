"""Provider-aware settings and catalogue projection for the desktop hub.

Presentation is deliberately downstream of provider/credential selection.
This module never reads or serializes an API key.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re

from providers import PROVIDERS, provider_defaults, validate_connection
from branding import resolve_presentation, validate_overrides
from model_names import PINNED_LABELS, friendly_model_name

MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/+\-]{0,299}\Z")
MAX_CREDENTIAL_REVISION = 9_007_199_254_740_991

# These fields can change whether a route is admissible or how a request is
# planned.  Rows coalesce only when every value is exactly equal.  Descriptive
# and provenance fields remain on the deterministic representative row.
MODEL_VARIANT_FIELDS = (
    "context", "context_options", "context_kind", "runtime_context",
    "max_input", "max_output", "advertised_context", "advertised_max_output",
    "tools", "vision", "reasoning", "effort_modes", "default_effort", "fast_mode", "speed_tier",
    "streaming", "tool_choice", "parallel_tool_calls", "service_tiers",
    "reasoning_history", "complete_tool_cycles", "capabilities", "billing_model_name",
    "upstream_model_id", "routing_endpoints", "routing_ignore", "routing_all_tags", "effort_control", "reasoning_mandatory",
    "version", "base_model_id", "provider_effort_modes",
)


def _default_credential_mode(provider_id: str) -> str:
    if provider_id == "mistral":
        return "vibe"
    if provider_id == "ollama":
        return "none"
    return "keychain"


def _credential_mode(provider_id: str, value) -> str:
    allowed = {"none"} if provider_id == "ollama" else {"keychain", "environment"}
    if provider_id == "mistral":
        allowed.add("vibe")
    if value not in allowed:
        raise ValueError(f"Unsupported credential source for {PROVIDERS[provider_id]['name']}.")
    return value


def _credential_revision(value) -> int:
    if (type(value) is not int or value < 0
            or value > MAX_CREDENTIAL_REVISION):
        raise ValueError("Provider credential revision must be a non-negative safe integer.")
    return value


def qualify(provider_id: str, model_id: str) -> str:
    if provider_id not in PROVIDERS:
        raise ValueError("Unknown inference provider.")
    if not isinstance(model_id, str) or not MODEL_ID.fullmatch(model_id):
        raise ValueError("Enter an exact model ID without whitespace.")
    return provider_id + "/" + model_id


def split_route(value: str) -> tuple[str, str]:
    if not isinstance(value, str):
        raise ValueError("Model route must be text.")
    value = value.strip()
    first, separator, remainder = value.partition("/")
    if separator and first in PROVIDERS:
        qualify(first, remainder)
        return first, remainder
    # The v0.2 settings format contained bare Mistral model IDs.
    qualify("mistral", value)
    return "mistral", value


def connection_signature(provider_id: str, connection: dict) -> str:
    normalized = validate_connection(provider_id, connection)
    mode = _credential_mode(
        provider_id, connection.get("credential_mode", _default_credential_mode(provider_id)))
    revision = _credential_revision(connection.get("credential_revision", 0))
    value = json.dumps(
        [provider_id, normalized, mode, revision], sort_keys=True, separators=(",", ":")
    ).encode()
    return hashlib.sha256(value).hexdigest()[:20]


def defaults(slots, vibe_model: str, port: int = 11436) -> dict:
    connections = provider_defaults()
    for provider_id, connection in connections.items():
        connection["credential_mode"] = _default_credential_mode(provider_id)
        # The UI increments this non-secret generation after replacing or
        # removing a provider credential.  It lets account-scoped catalogues
        # fail stale without hashing or persisting any secret material.
        connection["credential_revision"] = 0
    return {"schema_version": 3, "port": port,
            "mappings": {slot[0]: qualify("mistral", vibe_model) for slot in slots},
            "providers": connections, "branding_overrides": {},
            "auto_stop": True, "auto_mode": False, "codex_model": None,
            "codex_catalogue": None}


def normalize(value: dict, slots, vibe_model: str, port: int = 11436) -> dict:
    if not isinstance(value, dict):
        raise ValueError("Settings must be a JSON object.")
    schema_version = value.get("schema_version", 0)
    if type(schema_version) is not int or schema_version < 0:
        raise ValueError("Settings schema_version must be a non-negative integer.")
    if schema_version > 3:
        raise ValueError("These settings belong to a newer version of Provider Hub.")
    legacy_mode = value.get("credential_mode")
    if "credential_mode" in value and legacy_mode not in {"vibe", "separate"}:
        raise ValueError("Choose Vibe credentials or a separate API key.")
    result = defaults(slots, vibe_model, port)
    requested_port = value.get("port", result["port"])
    if type(requested_port) is not int or not 1024 <= requested_port <= 65535:
        raise ValueError("Choose a port between 1024 and 65535.")
    result["port"] = requested_port
    mappings = value.get("mappings", result["mappings"])
    if not isinstance(mappings, dict):
        raise ValueError("Model mappings must be an object.")
    for slot, *_ in slots:
        if slot not in mappings:
            raise ValueError("Choose a model for every Claude slot.")
        result["mappings"][slot] = qualify(*split_route(mappings[slot]))
    configured = value.get("providers", {})
    if not isinstance(configured, dict):
        raise ValueError("Provider connections must be an object.")
    if set(configured) - set(PROVIDERS):
        raise ValueError("The settings contain an unknown provider connection.")
    for provider_id, baseline in result["providers"].items():
        requested = configured.get(provider_id, {})
        if not isinstance(requested, dict):
            raise ValueError("Provider connection must be an object.")
        if any(key in requested for key in ("api_key", "apiKey", "token", "password", "headers")):
            raise ValueError("API keys belong in Keychain, not the settings file.")
        merged = {**baseline, **requested}
        if "region" in requested and "base_url" not in requested:
            merged.pop("base_url", None)
        connection = validate_connection(provider_id, merged)
        mode = requested.get("credential_mode", baseline["credential_mode"])
        if provider_id == "mistral" and "credential_mode" in value and not requested:
            mode = "vibe" if legacy_mode == "vibe" else "keychain"
        connection["credential_mode"] = _credential_mode(provider_id, mode)
        connection["credential_revision"] = _credential_revision(
            requested.get("credential_revision", baseline["credential_revision"]))
        result["providers"][provider_id] = connection
    result["branding_overrides"] = validate_overrides(value.get("branding_overrides", {}))
    if value.get("codex_model") is not None:
        result["codex_model"] = qualify(*split_route(value["codex_model"]))
    catalogue = value.get("codex_catalogue")
    if catalogue is not None:
        if not isinstance(catalogue, list) or not catalogue:
            raise ValueError("The Codex catalogue selection must be a non-empty list of model routes.")
        selected = []
        for route in catalogue:
            qualified = qualify(*split_route(route))
            if qualified not in selected:
                selected.append(qualified)
        result["codex_catalogue"] = selected
        if result["codex_model"] is not None and result["codex_model"] not in selected:
            raise ValueError("Choose the Codex default model from the catalogue selection.")
    for key, fallback in (("auto_stop", True), ("auto_mode", False)):
        requested = value.get(key, fallback)
        if type(requested) is not bool:
            raise ValueError(f"{key} must be true or false.")
        result[key] = requested
    return result


def _model_variant_key(item: dict) -> tuple[str, str]:
    canonical = item.get("canonical_id")
    if not isinstance(canonical, str) or not canonical:
        canonical = item["id"]
    try:
        variant = json.dumps(
            [[field, item.get(field)] for field in MODEL_VARIANT_FIELDS],
            sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("Provider catalogue capability metadata must be JSON data.") from exc
    return canonical, variant


def _catalogue_groups(provider_id: str, inventory: dict) -> list[dict]:
    """Return deterministic model groups with cross-group aliases removed."""
    if provider_id not in PROVIDERS:
        raise ValueError("Unknown inference provider.")
    claimed_provider = inventory.get("provider_id")
    if claimed_provider not in (None, provider_id):
        raise ValueError("Provider catalogue identity does not match its connection.")
    models = inventory.get("models", [])
    if not isinstance(models, list):
        raise ValueError("Provider catalogue models must be an array.")

    grouped = {}
    for index, source in enumerate(models):
        if not isinstance(source, dict):
            raise ValueError(f"Provider catalogue model {index} must be an object.")
        item = copy.deepcopy(source)
        identifier = item.get("id")
        try:
            qualify(provider_id, identifier)
        except ValueError as exc:
            raise ValueError(f"Provider catalogue model {index} has an invalid id.") from exc
        entry_provider = item.get("provider_id")
        if entry_provider not in (None, provider_id):
            raise ValueError("Provider catalogue model identity does not match its connection.")
        aliases = item.get("aliases") or []
        if not isinstance(aliases, list):
            raise ValueError(f"Provider catalogue model {identifier} aliases must be an array.")
        raw_ids = {identifier}
        for alias in aliases:
            try:
                qualify(provider_id, alias)
            except ValueError as exc:
                raise ValueError(
                    f"Provider catalogue model {identifier} has an invalid alias.") from exc
            raw_ids.add(alias)
        key = _model_variant_key(item)
        grouped.setdefault(key, []).append((item, raw_ids))

    raw_owners = {}
    alias_claims = {}
    for key, entries in grouped.items():
        for item, raw_ids in entries:
            raw_owners.setdefault(item["id"], set()).add(key)
            for identifier in raw_ids:
                alias_claims.setdefault(identifier, set()).add(key)
    conflicting_raw = sorted(
        identifier for identifier, owners in raw_owners.items() if len(owners) > 1)
    if conflicting_raw:
        raise ValueError(
            f"Provider catalogue gives model id {conflicting_raw[0]!r} conflicting specifications.")

    output = []
    for key, entries in sorted(grouped.items(), key=lambda pair: pair[0]):
        entries = sorted(
            entries,
            key=lambda pair: (
                pair[0]["id"],
                json.dumps(pair[0], sort_keys=True, separators=(",", ":"), ensure_ascii=False),
            ),
        )
        raw_ids = {item["id"] for item, _ in entries}
        all_ids = set().union(*(identifiers for _, identifiers in entries))
        kept = []
        dropped = set()
        for identifier in sorted(all_ids):
            claimants = alias_claims.get(identifier, {key})
            owners = raw_owners.get(identifier, set())
            if len(claimants) == 1 or key in owners:
                kept.append(identifier)
            else:
                dropped.add(identifier)
        for item, _ in entries:
            removed = item.get("ambiguous_aliases_removed") or []
            if isinstance(removed, list):
                dropped.update(value for value in removed if isinstance(value, str))
        output.append({
            "key": key,
            "entries": entries,
            "raw_ids": raw_ids,
            "kept": kept,
            "dropped": sorted(dropped),
        })
    return output


def project_catalogue(provider_id: str, inventory: dict, settings: dict, observations=None) -> list[dict]:
    """Qualify raw provider IDs once and attach validated presentation data."""
    observations = observations or {}
    output = []
    selected = [split_route(route)[1] for route in settings["mappings"].values() if split_route(route)[0] == provider_id]
    for group in _catalogue_groups(provider_id, inventory):
        canonical = group["key"][0]
        preferred = next((identifier for identifier in selected if identifier in group["kept"]), None)
        if preferred is None:
            preferred = canonical if canonical in group["raw_ids"] else min(group["raw_ids"])
        source, _ = next(
            ((item, identifiers) for item, identifiers in group["entries"]
             if item["id"] == preferred),
            next(((item, identifiers) for item, identifiers in group["entries"]
                  if preferred in identifiers), group["entries"][0]),
        )
        item = copy.deepcopy(source)
        route = qualify(provider_id, preferred)
        overrides = settings.get("branding_overrides", {})
        labels = overrides.get(provider_id, {}).get("modelLabels", {})
        override_label = labels.get(preferred) or labels.get(route)
        advertised = item.get("display_name")
        canonical = item.get("canonical_id") or preferred
        # A moving Mistral alias can still advertise a resolved, versioned
        # billing-model identifier. Use it for presentation, never for routing.
        label_identifier = canonical
        billing_model = item.get("billing_model_name")
        if (provider_id == "mistral" and canonical not in PINNED_LABELS
                and isinstance(billing_model, str) and MODEL_ID.fullmatch(billing_model)
                and re.search(r"(?:^|[-_.])\d", billing_model)):
            label_identifier = billing_model
        # Repeated raw IDs are not human labels and must not suppress known
        # product names such as Mistral Small 4 or Large 3.
        friendly = advertised if advertised and advertised not in {item["id"], canonical, label_identifier} else friendly_model_name(label_identifier)
        presentation = resolve_presentation(
            provider_id, preferred, overrides, override_label or friendly)
        item.update(id=route, model_id=preferred, provider_id=provider_id,
                    aliases=[route] + [qualify(provider_id, identifier)
                                       for identifier in group["kept"] if identifier != preferred],
                    display_name=presentation.get("modelLabel", item.get("display_name", preferred)),
                    presentation=presentation,
                    discovery_source=inventory.get("source", item.get("source", "provider")))
        if group["dropped"]:
            item["ambiguous_aliases_removed"] = group["dropped"]
        else:
            item.pop("ambiguous_aliases_removed", None)
        observation = observations.get(route, {})
        if observation:
            item["inference_status"] = observation.get("status", "advertised")
            item["last_success"] = observation.get("last_success")
        else:
            item.setdefault("inference_status", "advertised")
        output.append(item)
    return sorted(output, key=lambda item: (item["display_name"].casefold(), item["id"]))


def provider_presentations(settings: dict) -> list[dict]:
    return [{**descriptor, "id": provider_id,
             "presentation": resolve_presentation(provider_id, overrides=settings.get("branding_overrides"))}
            for provider_id, descriptor in PROVIDERS.items()]
