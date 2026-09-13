"""Curated OpenRouter discovery with context-specific endpoint routing."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import copy
from datetime import datetime, timezone
import hashlib
import hmac
import json
import re
from urllib.parse import quote

BASE_URL = "https://openrouter.ai/api"
MODELS_URL = BASE_URL + "/v1/models"
DOCS = "https://openrouter.ai/docs/guides/routing/provider-selection"
DESCRIPTOR = {
    "id": "openrouter", "name": "OpenRouter", "protocol": "anthropic",
    "default_base_url": BASE_URL, "default_region": "global", "regions": {"global": BASE_URL},
    "auth_header": {"name": "Authorization", "prefix": "Bearer "},
    "credential_account": "OPENROUTER_API_KEY", "credential_env": "OPENROUTER_API_KEY",
    "setup_url": "https://openrouter.ai/settings/keys",
    "capabilities": {"streaming": True, "tools": True, "thinking": True, "vision": "model_dependent",
                     "model_discovery": "api", "reasoning_history": "native"},
}
OFFICIAL_PATHS = {"", "/api", "/api/v1", "/api/v1/messages", "/api/v1/responses",
                  "/api/v1/chat/completions", "/api/v1/models"}
# Selection/labels from TaskWraith's PiOpenRouterModelRegistration.ts, 2026-09-13.
# Membership, capabilities and limits always come from current OpenRouter data.
CURATED = {
    "z-ai/glm-5.2": "GLM 5.2", "poolside/laguna-s-2.1": "Laguna S 2.1",
    "nvidia/nemotron-3-ultra-550b-a55b:free": "Nemotron 3 Ultra · Free",
    "cohere/north-mini-code:free": "North Mini Code · Free",
    "minimax/minimax-m3:free": "MiniMax M3 · Free",
    "thinkingmachines/inkling:free": "Inkling · Free",
    "thinkingmachines/inkling-small:free": "Inkling Small · Free",
    "inception/mercury-2.5-preview": "Mercury 2.5 Preview",
    "inception/mercury-2.5": "Mercury 2.5", "tencent/hy4-preview": "Hy4 Preview",
    "nex-agi/nex-n2.5-mini:free": "Nex N2.5 Mini · Free",
    "nex-agi/nex-n2.5-pro:free": "Nex N2.5 Pro · Free",
    "sakana/fugu-max": "Fugu Max", "sakana/fugu-ultra-v2": "Fugu Ultra v2",
}
EFFORT_ORDER = ["none", "minimal", "low", "medium", "high", "xhigh", "max"]
TAG = re.compile(r"[a-z0-9][a-z0-9._/-]{0,159}\Z", re.I)


class OpenRouterError(ValueError):
    pass


def integer(value):
    return value if type(value) is int and value > 0 else None


def _efforts(card, endpoints):
    metadata = card.get("reasoning") if isinstance(card.get("reasoning"), dict) else {}
    parameters = {p for p in card.get("supported_parameters", []) if isinstance(p, str)}
    capable = bool(parameters & {"reasoning", "reasoning_effort", "include_reasoning"})
    if not capable:
        return False, [], "none", None, None
    declared = metadata.get("supported_efforts")
    mandatory = metadata.get("mandatory")
    has_levels = "reasoning_effort" in parameters and isinstance(declared, list) and bool(declared)
    if endpoints:
        has_levels = has_levels and any("reasoning_effort" in endpoint["parameters"] for endpoint in endpoints)
    efforts = [effort for effort in EFFORT_ORDER if has_levels and effort in declared and effort != "none"]
    if not has_levels:
        efforts = ["high"]  # The documented enabled switch, not a named level.
    if mandatory is not True and (mandatory is False or metadata.get("default_enabled") is False
                                   or isinstance(declared, list) and "none" in declared):
        efforts.insert(0, "none")
    default = metadata.get("default_effort")
    if default not in efforts:
        default = "none" if metadata.get("default_enabled") is False and "none" in efforts else None
    return True, efforts, "levels" if has_levels else "toggle", default, mandatory


def _entries(card, details):
    identifier = card["id"]
    architecture = card.get("architecture") if isinstance(card.get("architecture"), dict) else {}
    parameters = card.get("supported_parameters") if isinstance(card.get("supported_parameters"), list) else []
    if ("text" not in architecture.get("output_modalities", [])
            or "tools" not in parameters
            or card.get("deprecated") is True or card.get("archived") is True):
        return []
    groups = {}
    all_tags = set()
    data = details.get("data") if isinstance(details, dict) else None
    if isinstance(data, dict) and data.get("id", identifier) != identifier:
        return []
    endpoints = data.get("endpoints") if isinstance(data, dict) else None
    for endpoint in endpoints if isinstance(endpoints, list) else []:
        if not isinstance(endpoint, dict) or endpoint.get("model_id", identifier) != identifier:
            continue
        tag = endpoint.get("tag")
        params = endpoint.get("supported_parameters")
        if isinstance(tag, str) and TAG.fullmatch(tag):
            all_tags.add(tag)
        if (not isinstance(tag, str) or not TAG.fullmatch(tag) or not isinstance(params, list)
                or "tools" not in params or endpoint.get("status", 0) != 0
                or any(part == tier or part.startswith(tier + "-") for part in tag.split("/")[1:]
                       for tier in ("fast", "flex", "priority"))):
            continue
        context = integer(endpoint.get("context_length"))
        row = {"tag": tag, "parameters": sorted({p for p in params if isinstance(p, str)}),
               "max_output": integer(endpoint.get("max_completion_tokens")),
               "max_input": integer(endpoint.get("max_prompt_tokens")),
               "tool_choice": endpoint.get("supports_tool_choice") if isinstance(endpoint.get("supports_tool_choice"), dict) else {}}
        groups.setdefault(context, []).append(row)
    claims = {}
    for context, rows in groups.items():
        for row in rows:
            claims.setdefault(row["tag"], set()).add(context)
    groups = {context: [row for row in rows if len(claims[row["tag"]]) == 1] for context, rows in groups.items()}
    groups = {context: rows for context, rows in groups.items() if rows}
    if not groups:
        if isinstance(endpoints, list):
            return []  # No current tool-capable standard endpoint.
        groups[None] = []  # Discovery failure is not an invented context limit.
    contexts = sorted(groups, key=lambda value: value or 0, reverse=True)
    entries = []
    for index, context in enumerate(contexts):
        rows = sorted(groups[context], key=lambda row: row["tag"])
        route_id = identifier if index == 0 else identifier + "/context-" + str(context or "unknown")
        capable, efforts, control, default, mandatory = _efforts(card, rows)
        caps = [row["max_output"] for row in rows]
        input_caps = [row["max_input"] for row in rows]
        name = CURATED[identifier]
        if len(contexts) > 1:
            name += " · " + (f"{context:,} context" if context else "unreported context")
        entries.append({
            "id": route_id, "canonical_id": identifier, "aliases": [route_id],
            "upstream_model_id": identifier, "display_name": name,
            "context": context, "max_input": min(input_caps) if input_caps and all(input_caps) else None,
            "max_output": min(caps) if caps and all(caps) else None,
            "context_kind": "provider_endpoint_reported" if context else "unknown",
            "tools": True, "vision": "image" in architecture.get("input_modalities", []),
            "reasoning": capable, "effort_modes": efforts, "effort_control": control,
            "default_effort": default, "reasoning_mandatory": mandatory,
            "parallel_tool_calls": bool(rows) and all("parallel_tool_calls" in row["parameters"] for row in rows),
            "fast_mode": False, "streaming": True, "reasoning_history": "native", "complete_tool_cycles": True,
            "routing_endpoints": rows,
            "routing_all_tags": sorted(all_tags),
            "routing_ignore": sorted(tag for tag in all_tags - {row["tag"] for row in rows}
                                     if any("/" not in row["tag"] and tag.startswith(row["tag"] + "/") for row in rows)),
            "source": "provider_api", "evidence": MODELS_URL,
            "metadata_evidence": MODELS_URL + "/" + quote(identifier, safe="/:") + "/endpoints",
            "inference_status": "advertised",
        })
    return entries


def discover(connection, api_key, *, transport):
    if not isinstance(api_key, str) or not api_key.strip() or any(c in api_key for c in "\r\n"):
        raise OpenRouterError("Add an OpenRouter API key to load this catalogue.")
    headers = {"Authorization": "Bearer " + api_key.strip(), "Accept": "application/json", "User-Agent": "ProviderHub/0.5"}
    def fetch(url):
        return transport({"url": url, "headers": headers, "method": "GET", "provider_name": "OpenRouter"})
    raw = fetch(MODELS_URL)
    if not isinstance(raw, dict) or not isinstance(raw.get("data"), list):
        raise OpenRouterError("OpenRouter discovery returned an invalid model list.")
    cards = {card["id"]: card for card in raw["data"] if isinstance(card, dict)
             and isinstance(card.get("id"), str) and card["id"] in CURATED}
    def detail(identifier):
        try:
            return identifier, fetch(MODELS_URL + "/" + quote(identifier, safe="/:") + "/endpoints")
        except (ValueError, OSError):
            return identifier, None
    with ThreadPoolExecutor(max_workers=4) as pool:
        details = dict(pool.map(detail, sorted(cards)))
    models = [entry for identifier in sorted(cards) for entry in _entries(cards[identifier], details[identifier])]
    missing = sorted(set(CURATED) - set(cards))
    warnings = ["Curated from TaskWraith's Pi roster, refreshed from OpenRouter. Listings are not inference tests.",
                "Context variants route only to matching tool-capable endpoints; provider account restrictions still apply."]
    if missing:
        warnings.append("Not currently in the OpenRouter catalogue: " + ", ".join(missing) + ".")
    if any(value is None for value in details.values()):
        warnings.append("Some endpoint metadata was unavailable. Those models retain unreported limits until discovery succeeds.")
    return {"provider_id": "openrouter", "models": models, "source": "provider_api", "evidence": MODELS_URL,
            "fetched_at": datetime.now(timezone.utc).isoformat(), "warnings": warnings}


def normalized_effort(requested, spec):
    if requested is None:
        return None
    if not isinstance(requested, str):
        raise OpenRouterError("Reasoning effort must be text.")
    requested = {"ultra": "max"}.get(requested, requested)
    levels = spec.get("effort_modes") or []
    if requested == "none" and "none" not in levels:
        raise OpenRouterError("This OpenRouter model does not advertise a reasoning-off control.")
    if requested not in EFFORT_ORDER or not levels:
        raise OpenRouterError("This OpenRouter model does not advertise that reasoning control.")
    if requested in levels:
        return requested
    higher = [value for value in levels if value != "none" and EFFORT_ORDER.index(value) >= EFFORT_ORDER.index(requested)]
    return higher[0] if higher else levels[-1]


def normalize_messages(body, model_id, spec):
    output = body.get("output_config") or {}
    requested = output.get("effort")
    effort = normalized_effort(requested, spec)
    thinking = body.get("thinking")
    if thinking and thinking.get("type") == "disabled" and "none" not in (spec.get("effort_modes") or []):
        raise OpenRouterError("This OpenRouter model does not advertise a reasoning-off control.")
    compatibility = {}
    if effort is not None:
        target = "disabled" if effort == "none" else "enabled"
        if thinking and thinking.get("type") not in {target, "adaptive"}:
            raise OpenRouterError("OpenRouter received conflicting thinking and effort controls.")
        body["thinking"] = {**(thinking or {}), "type": target}
        if "budget_tokens" in body["thinking"]:
            body["thinking"].pop("budget_tokens")
            compatibility["thinking_budget"] = "replaced_by_effort"
        if effort == "none" or spec.get("effort_control") == "toggle":
            output.pop("effort", None)
        else:
            output["effort"] = effort
        if output:
            body["output_config"] = output
        else:
            body.pop("output_config", None)
        if requested != effort:
            compatibility["reasoning_effort"] = f"{requested}_normalized_to_{effort}"
    return compatibility


def _first_user_content(history):
    if isinstance(history, str):
        content = history
    elif isinstance(history, list):
        first = next((item for item in history if isinstance(item, dict) and item.get("role") == "user"), {})
        content = first.get("content", [])
    else:
        content = []
    if isinstance(content, str):
        return [["text", content]]
    return [["text", part.get("text", "")] if isinstance(part, dict) and part.get("type") in {"text", "input_text"}
            else part for part in content] if isinstance(content, list) else content


def _routing(body, spec, responses):
    rows = spec.get("routing_endpoints") or []
    if not rows:
        return {"require_parameters": True}
    required = {"tools"} if body.get("tools") else set()
    for parameter in ("temperature", "top_p", "top_k", "top_logprobs"):
        if body.get(parameter) is not None:
            required.add(parameter)
    if body.get("stop_sequences"):
        required.add("stop")
    output_format = ((body.get("text") or {}).get("format") if responses
                     else (body.get("output_config") or {}).get("format"))
    if isinstance(output_format, dict) and output_format.get("type") in {"json_schema", "json_object"}:
        required.add("structured_outputs" if output_format["type"] == "json_schema" else "response_format")
    reasoning = (body.get("reasoning") if responses else body.get("output_config")) or {}
    if reasoning.get("effort") is not None and spec.get("effort_control") == "levels":
        required.add("reasoning_effort")
    choice = body.get("tool_choice")
    if choice is not None and not isinstance(choice, (str, dict)):
        raise OpenRouterError("tool_choice must be text or an object.")
    kind = choice.get("type") if isinstance(choice, dict) else choice
    if kind is not None and not isinstance(kind, str):
        raise OpenRouterError("tool_choice.type must be text.")
    forced = {"any": "required", "required": "required", "tool": "function", "function": "function", "none": "none"}.get(kind)
    if forced:
        required.add("tool_choice")
    explicit_switch = "enabled" in reasoning if responses else bool(body.get("thinking"))
    candidates = [row for row in rows if required <= set(row["parameters"])
                  and (not explicit_switch or bool({"reasoning", "reasoning_effort"} & set(row["parameters"])))
                  and (not forced or row.get("tool_choice", {}).get(forced) is not False)]
    if not candidates:
        raise OpenRouterError("No endpoint for this context choice advertises the requested tool or reasoning controls.")
    # Auto is the API default. Omit an explicit default where host metadata
    # does not advertise a selector, but preserve a parallel-disable request.
    if kind == "auto" and not any("tool_choice" in row["parameters"] for row in candidates):
        if not isinstance(choice, dict) or not choice.get("disable_parallel_tool_use"):
            body.pop("tool_choice", None)
    allowed = {row["tag"] for row in candidates}
    all_tags = set(spec.get("routing_all_tags") or []) | {row["tag"] for row in rows}
    ignored = {tag for tag in all_tags - allowed if any("/" not in base and tag.startswith(base + "/") for base in allowed)}
    # Check semantic controls above. Blanket require_parameters also checks
    # structural/default fields that valid native endpoints may not enumerate
    # (notably Fugu max_tokens and Codex's parallel_tool_calls:false).
    return {"require_parameters": False, "only": sorted(allowed), "ignore": sorted(ignored)}


def finalize(body, spec, api_key, *, responses=False):
    upstream = spec.get("upstream_model_id")
    if upstream not in CURATED:
        raise OpenRouterError("Choose an OpenRouter model from the curated catalogue.")
    if (body.get("speed") not in {None, "standard", "normal"}
            or body.get("service_tier") not in {None, "auto", "default", "standard"}):
        raise OpenRouterError("This OpenRouter connection does not expose a qualified Fast service tier.")
    for field in ("models", "fallbacks", "route", "provider"):
        if body.get(field):
            raise OpenRouterError("OpenRouter model and endpoint selection is managed by the Provider Hub catalogue.")
    if responses:
        if body.get("previous_response_id") or body.get("store"):
            raise OpenRouterError("This OpenRouter profile uses full history with store:false.")
        reasoning = body.get("reasoning")
        if reasoning is not None:
            if not isinstance(reasoning, dict):
                raise OpenRouterError("reasoning must be an object.")
            effort = normalized_effort(reasoning.get("effort"), spec)
            enabled = reasoning.get("enabled")
            if "enabled" in reasoning and type(enabled) is not bool:
                raise OpenRouterError("reasoning.enabled must be true or false.")
            if enabled is False and "none" not in (spec.get("effort_modes") or []):
                raise OpenRouterError("This OpenRouter model does not advertise a reasoning-off control.")
            if (enabled is False and effort not in (None, "none")) or (enabled is True and effort == "none"):
                raise OpenRouterError("OpenRouter received conflicting reasoning enabled and effort controls.")
            if effort == "none":
                reasoning.pop("effort", None)
                reasoning["enabled"] = False
            elif effort is not None and spec.get("effort_control") == "toggle":
                reasoning.pop("effort", None)
                reasoning["enabled"] = True
            elif effort is not None:
                reasoning["effort"] = effort
    body["model"] = upstream
    body.pop("speed", None)
    body.pop("service_tier", None)
    body["provider"] = _routing(body, spec, responses)
    # Sticky routing is only an opaque grouping key; requests remain full-history.
    history = body.get("input") if responses else body.get("messages")
    material = json.dumps([upstream, spec.get("context"), _first_user_content(history)], sort_keys=True, ensure_ascii=False).encode()
    body["session_id"] = "provider-hub-" + hmac.new(api_key.encode(), material, hashlib.sha256).hexdigest()
    return body
