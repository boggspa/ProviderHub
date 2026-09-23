"""Provider model discovery and catalogue building."""
from __future__ import annotations
import copy
from datetime import datetime, timezone
import json
import re
import ssl
import urllib.error
import urllib.parse
import urllib.request
from provider_registry import (
    GATEWAY_USER_AGENT,
    PROVIDERS,
    ProviderError,
    _CEREBRAS_MODEL_METADATA,
    _CEREBRAS_PUBLIC_MODELS_URL,
    _DEEPSEEK_DOCS,
    _DEEPSEEK_MODEL_METADATA,
    _GROK_MODEL_METADATA,
    _KIMI_DOCS,
    _KIMI_MODELS,
    _MIMO_DOCS,
    _MIMO_MODELS,
    _MODEL_ID,
    _MUSE_COOKBOOK,
    _MUSE_MODEL_METADATA,
    _MUSE_NON_CHAT_MODELS,
    _MUSE_REASONING_DOCS,
    _auth_headers,
    _provider,
    _published_ollama_context,
    documented_context,
    validate_connection,
)
from devin_agent import catalogue as devin_catalogue
from effort_map import mistral_effort_modes, ollama_effort_modes
from fast_models import supports_fast_toggle
from gemini_provider import GeminiError, discover as gemini_discover
from openrouter_provider import OpenRouterError, discover as openrouter_discover
from qwen_provider import catalogue as qwen_catalogue


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: N802 - stdlib API
        return None


def _fetch_json(plan: dict) -> dict:
    body = plan.get("body")
    try:
        encoded = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
    except (TypeError, ValueError) as exc:
        raise ProviderError("Provider metadata request body must be JSON-serializable.") from exc
    request = urllib.request.Request(
        plan["url"], data=encoded, headers=plan["headers"], method=plan["method"],
    )
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        urllib.request.HTTPSHandler(context=ssl.create_default_context()),
        _NoRedirect(),
    )
    try:
        with opener.open(request, timeout=25) as response:
            raw = response.read(8 * 1024 * 1024 + 1)
    except urllib.error.HTTPError as exc:
        raise ProviderError(f"{plan['provider_name']} model discovery returned HTTP {exc.code}.") from exc
    except OSError as exc:
        raise ProviderError(f"Could not reach {plan['provider_name']} model discovery.") from exc
    if len(raw) > 8 * 1024 * 1024:
        raise ProviderError("Provider model discovery response is too large.")
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, ValueError) as exc:
        raise ProviderError("Provider model discovery returned invalid JSON.") from exc
    if not isinstance(value, dict):
        raise ProviderError("Provider model discovery did not return an object.")
    return value


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _catalogue_entry(
    identifier: str,
    *,
    display_name: str | None = None,
    canonical_id: str | None = None,
    context: int | None = None,
    aliases: list[str] | None = None,
    tools=None,
    vision=None,
    reasoning=None,
    effort_modes: list[str] | None = None,
    fast_mode: bool = False,
    source: str,
    evidence: str,
    **extra,
) -> dict:
    if not isinstance(identifier, str) or not _MODEL_ID.fullmatch(identifier):
        raise ProviderError("Provider model catalogue contains an invalid model ID.")
    if type(context) is not int or context <= 0:
        context = None
    raw_aliases = [identifier] + (aliases or [])
    unique_aliases = []
    for value in raw_aliases:
        if isinstance(value, str) and _MODEL_ID.fullmatch(value) and value not in unique_aliases:
            unique_aliases.append(value)
    result = {
        "id": identifier,
        "canonical_id": canonical_id or identifier,
        "display_name": display_name or identifier,
        "context": context,
        "aliases": unique_aliases,
        "tools": tools,
        "vision": vision,
        "reasoning": reasoning,
        "effort_modes": list(effort_modes or []),
        "fast_mode": bool(fast_mode),
        "inference_status": "advertised",
        "source": source,
        "evidence": evidence,
    }
    result.update(extra)
    return result


def _static_catalogue(provider_id: str) -> tuple[list[dict], list[str], str] | None:
    if provider_id == "qwen-token-plan":
        return qwen_catalogue()
    if provider_id == "kimi":
        models = [
            _catalogue_entry(
                model["id"],
                source="provider_documentation",
                evidence=_KIMI_DOCS,
                **{key: value for key, value in model.items() if key != "id"},
            )
            for model in _KIMI_MODELS
        ]
        return models, [
            "Kimi model availability depends on membership.",
            "K3 context is 262144 or 1048576 depending on membership, so no single context is claimed.",
            "Models are documented, not inference-tested by discovery.",
        ], _KIMI_DOCS
    if provider_id == "mimo":
        models = [
            _catalogue_entry(
                model["id"],
                source="provider_documentation",
                evidence=_MIMO_DOCS,
                **{key: value for key, value in model.items() if key != "id"},
            )
            for model in _MIMO_MODELS
        ]
        return models, [
            "Token Plan availability is documented, not inference-tested by discovery.",
        ], _MIMO_DOCS
    if provider_id == "devin":
        # Devin is a single session-based agent exposed as capability modes,
        # not a multi-model catalogue. Each mode is a routable "agent model"
        # entry so the existing model-catalogue projection can carry it.
        modes, warnings, docs = devin_catalogue()
        models = [
            _catalogue_entry(
                mode["id"],
                display_name=mode.get("display_name", mode["id"]),
                tools=mode.get("capabilities", {}).get("tools"),
                vision=mode.get("capabilities", {}).get("vision"),
                reasoning=mode.get("capabilities", {}).get("thinking"),
                source="provider_documentation",
                evidence=mode.get("evidence", docs),
                description=mode.get("description"),
            )
            for mode in modes
        ]
        return models, warnings, docs
    return None


def _discovery_plan(provider_id: str, connection: dict, api_key: str | None) -> dict:
    base = connection["base_url"]
    if provider_id in {"mistral", "cerebras"}:
        url = base + "/models"
    elif provider_id == "muse":
        url = base + "/v1/models"
    elif provider_id == "grok":
        url = base + "/language-models"
    elif provider_id == "claude":
        url = base + "/v1/models"
    elif provider_id == "codex":
        url = base + "/models"
    elif provider_id == "deepseek":
        url = "https://api.deepseek.com/models"
    elif provider_id == "ollama":
        url = base + "/api/tags"
    else:
        raise ProviderError("This provider uses its documented model catalogue.")
    headers = _auth_headers(provider_id, api_key, content_type=False)
    if provider_id == "deepseek":
        # The list route is on DeepSeek's OpenAI-compatible surface.  The
        # Anthropic route uses x-api-key; the list route uses bearer auth.
        headers.pop("x-api-key", None)
        headers.pop("anthropic-version", None)
        headers["Authorization"] = "Bearer " + api_key.strip()
    return {
        "method": "GET",
        "url": url,
        "headers": headers,
        "provider_name": PROVIDERS[provider_id]["name"],
    }


def _ollama_show_plan(connection: dict, api_key: str | None, model_id: str) -> dict:
    return {
        "method": "POST",
        "url": connection["base_url"] + "/api/show",
        "headers": _auth_headers("ollama", api_key, content_type=True),
        "body": {"model": model_id},
        "provider_name": PROVIDERS["ollama"]["name"],
    }


def _cerebras_public_plan() -> dict:
    return {
        "method": "GET",
        "url": _CEREBRAS_PUBLIC_MODELS_URL,
        "headers": {"Accept": "application/json", "User-Agent": GATEWAY_USER_AGENT},
        "provider_name": PROVIDERS["cerebras"]["name"],
    }


def _positive_integer(value):
    return value if type(value) is int and value > 0 else None


def _boolean(value):
    return value if type(value) is bool else None


def _first_positive(*values):
    return next((value for value in values if _positive_integer(value) is not None), None)


def _merge_objects(base: dict, override: dict) -> dict:
    result = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(result.get(key), dict) and isinstance(value, dict):
            result[key] = _merge_objects(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def _ollama_declared_context(show: dict) -> int | None:
    direct = _positive_integer(show.get("context_length"))
    if direct is not None:
        return direct
    info = show.get("model_info")
    if not isinstance(info, dict):
        return None
    direct = _positive_integer(info.get("context_length"))
    if direct is not None:
        return direct
    architecture = info.get("general.architecture")
    if isinstance(architecture, str) and architecture:
        direct = _positive_integer(info.get(architecture + ".context_length"))
        if direct is not None:
            return direct
    # Older daemons may omit general.architecture.  Accept a single top-level
    # architecture context key, but never guess between multiple candidates
    # such as a text model and a vision encoder.
    candidates = {
        value for key, value in info.items()
        if isinstance(key, str) and key.endswith(".context_length")
        and key.count(".") == 1 and _positive_integer(value) is not None
    }
    return next(iter(candidates)) if len(candidates) == 1 else None


def _ollama_models(raw: dict, connection: dict, api_key: str | None, fetch) -> tuple[list[dict], list[str]]:
    cards = raw.get("models")
    if not isinstance(cards, list):
        raise ProviderError("Provider model discovery response has no model list.")
    models = []
    warnings = []
    for card in cards:
        if not isinstance(card, dict):
            continue
        identifier = card.get("model")
        if not isinstance(identifier, str) or not _MODEL_ID.fullmatch(identifier):
            continue
        show_plan = _ollama_show_plan(connection, api_key, identifier)
        try:
            show = fetch(copy.deepcopy(show_plan))
        except ProviderError:
            show = None
        if not isinstance(show, dict):
            show = {}
            warnings.append(f"Ollama did not return details for {identifier}; its limits and capabilities remain unknown.")
        raw_capabilities = show.get("capabilities")
        capabilities = None
        if isinstance(raw_capabilities, list) and all(isinstance(value, str) for value in raw_capabilities):
            capabilities = set(raw_capabilities)
        if capabilities is not None and "completion" not in capabilities:
            warnings.append(f"Ollama model {identifier} is not advertised for completion and was omitted.")
            continue
        context = _ollama_declared_context(show)
        per_model_warnings = []
        if context is None:
            per_model_warnings.append("The model's declared maximum context is not reported by /api/show.")
        per_model_warnings.append(
            "The daemon's effective runtime context is not reported by /api/show and may be lower than the model maximum."
        )
        details = show.get("details") if isinstance(show.get("details"), dict) else card.get("details")
        context, published_evidence = _published_ollama_context(identifier, context)
        models.append(_catalogue_entry(
            identifier,
            display_name=card.get("name") if isinstance(card.get("name"), str) else identifier,
            context=context,
            tools=("tools" in capabilities) if capabilities is not None else None,
            vision=("vision" in capabilities) if capabilities is not None else None,
            reasoning=("thinking" in capabilities) if capabilities is not None else None,
            effort_modes=ollama_effort_modes(identifier, ("thinking" in capabilities) if capabilities is not None else None),
            fast_mode=False,
            source="ollama_daemon",
            evidence=published_evidence or show_plan["url"],
            details=copy.deepcopy(details) if isinstance(details, dict) else {},
            context_kind=("publisher_documented" if published_evidence
                          else "model_declared_maximum" if context is not None else "unknown"),
            runtime_context=None,
            capability_source="ollama_api_show" if capabilities is not None else "unknown",
            warnings=per_model_warnings,
        ))
    return sorted(models, key=lambda model: (model["display_name"].casefold(), model["id"])), warnings


def _cerebras_entry(card: dict, public_card: dict | None, evidence: str) -> dict:
    public_card = public_card or {}
    merged = _merge_objects(public_card, card)
    identifier = merged["id"]
    metadata = _CEREBRAS_MODEL_METADATA.get(identifier, {})
    account_limits = card.get("limits") if isinstance(card.get("limits"), dict) else {}
    public_limits = public_card.get("limits") if isinstance(public_card.get("limits"), dict) else {}
    reported_context = _first_positive(
        account_limits.get("max_context_length"),
        card.get("max_context_length"),
        card.get("context_length"),
        card.get("max_context_window"),
    )
    public_context = _first_positive(
        public_limits.get("max_context_length"),
        public_card.get("max_context_length"),
        public_card.get("context_length"),
        public_card.get("max_context_window"),
    )
    reported_output = _first_positive(
        account_limits.get("max_completion_tokens"),
        card.get("max_completion_tokens"),
        card.get("max_output_length"),
        card.get("max_output_tokens"),
    )
    public_output = _first_positive(
        public_limits.get("max_completion_tokens"),
        public_card.get("max_completion_tokens"),
        public_card.get("max_output_length"),
        public_card.get("max_output_tokens"),
    )
    context = reported_context or _positive_integer(metadata.get("context")) or public_context
    max_output = reported_output or _positive_integer(metadata.get("max_output")) or public_output
    account_capabilities = card.get("capabilities") if isinstance(card.get("capabilities"), dict) else {}
    public_capabilities = public_card.get("capabilities") if isinstance(public_card.get("capabilities"), dict) else {}
    tools = _boolean(account_capabilities.get("tools"))
    if tools is None:
        tools = _boolean(account_capabilities.get("function_calling"))
    if tools is None:
        tools = _boolean(public_capabilities.get("tools"))
    if tools is None:
        tools = _boolean(public_capabilities.get("function_calling"))
    if tools is None:
        tools = _boolean(metadata.get("tools"))
    vision = _boolean(account_capabilities.get("vision"))
    if vision is None:
        vision = _boolean(metadata.get("vision"))
    if vision is None:
        vision = _boolean(public_capabilities.get("vision"))
    reasoning = _boolean(account_capabilities.get("reasoning"))
    if reasoning is None:
        reasoning = _boolean(public_capabilities.get("reasoning"))
    if reasoning is None:
        reasoning = _boolean(metadata.get("reasoning"))
    effort_modes = merged.get("effort_modes")
    if not isinstance(effort_modes, list) or not all(isinstance(mode, str) for mode in effort_modes):
        effort_modes = metadata.get("effort_modes", [])
    source_evidence = (
        _CEREBRAS_PUBLIC_MODELS_URL if public_card
        else metadata.get("evidence", evidence)
    )
    return _catalogue_entry(
        identifier,
        display_name=merged.get("name") if isinstance(merged.get("name"), str) else identifier,
        context=context,
        # Only while the account itself has told us nothing: a reported window
        # is this key's actual window and settles the question. Omitted rather
        # than nulled, so a route with one known window carries no key at all.
        **({"context_options": list(metadata["context_options"])}
           if reported_context is None and metadata.get("context_options") else {}),
        tools=tools,
        vision=vision,
        reasoning=reasoning,
        effort_modes=effort_modes,
        fast_mode=False,
        source="provider_api",
        evidence=source_evidence,
        max_output=max_output,
        context_kind=("provider_reported" if reported_context is not None else
                      "verified_documentation" if _positive_integer(metadata.get("context")) is not None else
                      "provider_reported" if public_context is not None else "unknown"),
        reasoning_history=("gateway_signed_replay" if reasoning is True else
                           "not_required" if reasoning is False else "unknown"),
        complete_tool_cycles=(True if reasoning is not None else None),
        streaming=_boolean(account_capabilities.get("streaming"))
                  if _boolean(account_capabilities.get("streaming")) is not None
                  else _boolean(public_capabilities.get("streaming")),
        tool_choice=_boolean(account_capabilities.get("tool_choice"))
                    if _boolean(account_capabilities.get("tool_choice")) is not None
                    else _boolean(public_capabilities.get("tool_choice")),
        parallel_tool_calls=(_boolean(account_capabilities.get("parallel_tool_calls"))
                             if _boolean(account_capabilities.get("parallel_tool_calls")) is not None
                             else _boolean(public_capabilities.get("parallel_tool_calls"))),
        preview=_boolean(merged.get("preview")),
        deprecated=_boolean(merged.get("deprecated")),
        description=merged.get("description") if isinstance(merged.get("description"), str) else None,
    )


def _grok_entry(card: dict, supplement: dict, evidence: str) -> dict:
    identifier = card["id"]
    metadata = _GROK_MODEL_METADATA.get(identifier, {})
    # The language list defines membership; /models only adds metadata for the
    # same ID. It must never introduce image/video models into this picker.
    merged = {**supplement, **card}
    capabilities = merged.get("capabilities")
    capabilities = capabilities if isinstance(capabilities, dict) else {}

    def capability(name):
        reported = _boolean(capabilities.get(name))
        return reported if reported is not None else _boolean(metadata.get(name))

    context = _first_positive(card.get("context_length"), card.get("max_context_length"),
                              supplement.get("context_length"), supplement.get("max_context_length"))
    inputs = card.get("input_modalities")
    vision = ("image" in inputs) if isinstance(inputs, list) else capability("vision")
    efforts = capabilities.get("effort_modes", card.get("effort_modes"))
    if not isinstance(efforts, list) or not all(isinstance(value, str) for value in efforts):
        efforts = metadata.get("effort_modes", [])
    reasoning = capability("reasoning")
    if reasoning is None and efforts:
        reasoning = True
    return _catalogue_entry(
        identifier,
        context=context or metadata.get("context"),
        aliases=card.get("aliases") if isinstance(card.get("aliases"), list) else [],
        tools=capability("tools"), vision=vision, reasoning=reasoning,
        effort_modes=efforts,
        # xAI documents Priority on both text inference endpoints. Actual
        # service tier is returned by the API; requesting it is not a grant.
        fast_mode=True,
        source="provider_api", evidence=evidence,
        metadata_evidence=metadata.get("metadata_evidence"),
        context_kind=("provider_reported" if context else
                      "verified_documentation" if metadata.get("context") else "unknown"),
        max_output=_first_positive(merged.get("max_output_tokens"), merged.get("max_completion_tokens")),
        streaming=True,
        reasoning_history="not_required",
    )


def _muse_entry(card: dict, evidence: str) -> dict:
    identifier = card["id"]
    metadata = _MUSE_MODEL_METADATA.get(identifier, {})
    limits = card.get("limits") if isinstance(card.get("limits"), dict) else {}
    reported_context = _first_positive(
        limits.get("max_context_length"),
        card.get("max_context_length"),
        card.get("context_length"),
        card.get("max_context_window"),
    )
    reported_output = _first_positive(
        limits.get("max_completion_tokens"),
        card.get("max_completion_tokens"),
        card.get("max_output_length"),
        card.get("max_output_tokens"),
    )
    capabilities = card.get("capabilities") if isinstance(card.get("capabilities"), dict) else {}

    def capability(*names):
        for name in names:
            value = _boolean(capabilities.get(name))
            if value is not None:
                return value
        for name in names:
            value = _boolean(metadata.get(name))
            if value is not None:
                return value
        return None

    effort_modes = None
    for field in ("effort_modes", "supported_efforts", "reasoning_efforts"):
        value = card.get(field)
        if isinstance(value, list) and all(isinstance(mode, str) for mode in value):
            effort_modes = list(value)
            break
    if effort_modes is None:
        effort_modes = list(metadata.get("effort_modes", []))
    display_name = card.get("display_name") or card.get("name") or metadata.get("display_name")
    if not isinstance(display_name, str) or not display_name:
        display_name = identifier
    aliases = card.get("aliases") if isinstance(card.get("aliases"), list) else []
    context = reported_context or _positive_integer(metadata.get("context"))
    max_output = reported_output or _positive_integer(metadata.get("max_output"))
    reasoning = capability("reasoning", "thinking")
    tools = capability("tools", "function_calling", "tool_use")
    return _catalogue_entry(
        identifier,
        canonical_id=identifier,
        display_name=display_name,
        context=context,
        aliases=aliases,
        tools=tools,
        vision=capability("vision"),
        reasoning=reasoning,
        effort_modes=effort_modes,
        fast_mode=False,
        source="provider_api",
        evidence=evidence,
        metadata_evidence=metadata.get("metadata_evidence"),
        max_output=max_output,
        context_kind=("provider_reported" if reported_context is not None else
                      "verified_documentation" if context is not None else "unknown"),
        streaming=capability("streaming"),
        parallel_tool_calls=capability("parallel_tool_calls"),
        tool_choice_modes=(card.get("tool_choice_modes")
                           if isinstance(card.get("tool_choice_modes"), list)
                           else list(metadata.get("tool_choice_modes", []))),
        reasoning_history=("native" if reasoning is True else
                           "not_required" if reasoning is False else "unknown"),
        complete_tool_cycles=(True if tools is True else None),
        adaptive_thinking=(_boolean(card.get("adaptive_thinking"))
                           if _boolean(card.get("adaptive_thinking")) is not None
                           else _boolean(metadata.get("adaptive_thinking"))),
        preview=_boolean(card.get("preview")),
        deprecated=_boolean(card.get("deprecated")),
        description=card.get("description") if isinstance(card.get("description"), str) else None,
    )


def _coalesce_mistral_models(models: list[dict]) -> list[dict]:
    """Group equivalent cards and remove aliases with ambiguous specifications."""
    groups = {}
    for model in models:
        key = (
            model.get("canonical_id"),
            model.get("context"),
            model.get("tools"),
            model.get("vision"),
            model.get("reasoning"),
            tuple(model.get("effort_modes") or []),
            model.get("billing_model_name"),
        )
        groups.setdefault(key, []).append(model)

    raw_owners = {}
    alias_claims = {}
    for key, entries in groups.items():
        for entry in entries:
            raw_owners.setdefault(entry["id"], set()).add(key)
            for identifier in entry.get("aliases") or [entry["id"]]:
                alias_claims.setdefault(identifier, set()).add(key)
    duplicate_raw = sorted(identifier for identifier, owners in raw_owners.items() if len(owners) > 1)
    if duplicate_raw:
        raise ProviderError("Mistral advertised one model ID with conflicting specifications.")

    output = []
    for key, entries in groups.items():
        entries = sorted(entries, key=lambda entry: entry["id"])
        raw_ids = {entry["id"] for entry in entries}
        all_aliases = {
            identifier for entry in entries for identifier in (entry.get("aliases") or [entry["id"]])
        } | raw_ids
        kept = []
        dropped = []
        for identifier in sorted(all_aliases):
            claimants = alias_claims.get(identifier, {key})
            raw_owner = next(iter(raw_owners.get(identifier, ())), None)
            if len(claimants) == 1 or raw_owner == key:
                kept.append(identifier)
            else:
                dropped.append(identifier)
        primary = key[0] if isinstance(key[0], str) and key[0] in raw_ids else min(raw_ids)
        selected = next((entry for entry in entries if entry["id"] == primary), entries[0])
        merged = copy.deepcopy(selected)
        merged["id"] = primary
        merged["aliases"] = [primary] + [identifier for identifier in kept if identifier != primary]
        if dropped:
            merged["ambiguous_aliases_removed"] = dropped
        output.append(merged)
    return sorted(output, key=lambda model: (model["display_name"].casefold(), model["id"]))


def _models_from_api(provider_id: str, raw: dict, evidence: str, *, enriched=None) -> list[dict]:
    cards = raw.get("data")
    if not isinstance(cards, list):
        raise ProviderError("Provider model discovery response has no model list.")
    models = []
    for card in cards:
        if not isinstance(card, dict):
            continue
        identifier = card.get("id")
        if not isinstance(identifier, str) or not _MODEL_ID.fullmatch(identifier):
            continue
        if provider_id == "mistral":
            capabilities = card.get("capabilities") if isinstance(card.get("capabilities"), dict) else {}
            retired = card.get("archived") is True
            deprecation = card.get("deprecation")
            if isinstance(deprecation, str) and deprecation:
                try:
                    retired = retired or datetime.fromisoformat(deprecation.replace("Z", "+00:00")).date() <= datetime.now(timezone.utc).date()
                except ValueError:
                    pass
            if not capabilities.get("completion_chat") or retired:
                continue
            canonical = card.get("name") or card.get("root") or identifier
            if not isinstance(canonical, str) or not canonical:
                canonical = identifier
            models.append(_catalogue_entry(
                identifier,
                display_name=card.get("name") if isinstance(card.get("name"), str) else identifier,
                canonical_id=canonical,
                context=card.get("max_context_length"),
                aliases=card.get("aliases") if isinstance(card.get("aliases"), list) else [],
                tools=capabilities.get("function_calling") if type(capabilities.get("function_calling")) is bool else None,
                vision=capabilities.get("vision") if type(capabilities.get("vision")) is bool else None,
                reasoning=capabilities.get("reasoning") if type(capabilities.get("reasoning")) is bool else None,
                effort_modes=mistral_effort_modes(identifier, capabilities.get("reasoning")),
                fast_mode=False,
                source="provider_api",
                evidence=evidence,
                deprecation=card.get("deprecation"),
                billing_model_name=(card.get("billing_model_name")
                                    if isinstance(card.get("billing_model_name"), str) else None),
            ))
        elif provider_id == "deepseek":
            metadata = _DEEPSEEK_MODEL_METADATA.get(identifier, {})
            reported_context = _first_positive(card.get("context_length"), card.get("max_context_length"))
            models.append(_catalogue_entry(
                identifier,
                context=reported_context or metadata.get("context"),
                context_kind=("provider_reported" if reported_context else
                              "verified_documentation" if metadata.get("context") else "unknown"),
                context_evidence=metadata.get("context_evidence"),
                tools=metadata.get("tools"),
                vision=metadata.get("vision"),
                reasoning=metadata.get("reasoning"),
                effort_modes=metadata.get("effort_modes", []),
                fast_mode=False,
                source="provider_api",
                evidence=_DEEPSEEK_DOCS if metadata else evidence,
                advertised_context="1M" if metadata else None,
                advertised_max_output="384K" if metadata else None,
            ))
        elif provider_id == "cerebras":
            public_card = (enriched or {}).get(identifier)
            models.append(_cerebras_entry(card, public_card, evidence))
        elif provider_id == "grok":
            capabilities = card.get("capabilities") if isinstance(card.get("capabilities"), dict) else {}
            output = card.get("output_modalities")
            if (card.get("deprecated") is True or card.get("archived") is True
                    or capabilities.get("chat_completions") is False
                    or (isinstance(output, list) and "text" not in output)):
                continue
            models.append(_grok_entry(card, (enriched or {}).get(identifier, {}), evidence))
        elif provider_id == "muse":
            capabilities = card.get("capabilities") if isinstance(card.get("capabilities"), dict) else {}
            output = card.get("output_modalities")
            if (identifier in _MUSE_NON_CHAT_MODELS
                    or card.get("deprecated") is True or card.get("archived") is True
                    or (isinstance(output, list) and "text" not in output)):
                continue
            if any(capabilities.get(field) is False for field in (
                "completion_chat", "chat_completion", "chat_completions", "messages",
            )):
                continue
            models.append(_muse_entry(card, evidence))
        elif provider_id == "claude":
            # Enrich exact IDs when the list omits context. Capabilities and
            # account availability are still determined independently.
            if card.get("type") not in (None, "model"):
                continue
            context, context_evidence = documented_context(provider_id, identifier)
            reported_context = _first_positive(card.get("context_window"), card.get("max_context_length"))
            models.append(_catalogue_entry(
                identifier,
                context=reported_context or context,
                context_kind=("provider_reported" if reported_context else
                              "verified_documentation" if context else "unknown"),
                context_evidence=context_evidence,
                display_name=card.get("display_name") if isinstance(card.get("display_name"), str) else identifier,
                fast_mode=supports_fast_toggle(provider_id, identifier),
                source="provider_api",
                evidence=evidence,
            ))
        elif provider_id == "codex":
            # OpenAI's list route mixes chat models with audio, image,
            # realtime and embedding families; keep only chat-capable ids.
            if not _CODEX_CHAT_MODEL.fullmatch(identifier):
                continue
            models.append(_catalogue_entry(
                identifier,
                fast_mode=supports_fast_toggle(provider_id, identifier),
                source="provider_api",
                evidence=evidence,
            ))
    if provider_id == "mistral":
        return _coalesce_mistral_models(models)
    return sorted(models, key=lambda model: (model["display_name"].casefold(), model["id"]))


# OpenAI's /v1/models mixes chat ids with audio, image, realtime, embedding
# and legacy-completion families. Chat routes accept only these shapes.
_CODEX_CHAT_MODEL = re.compile(
 r"(?!(?:.*(?:realtime|audio|image|tts|whisper|embedding|moderation|transcribe|instruct)))(?:gpt-(?:3\.5|4|5|6)|o[1-9]|chatgpt-)[A-Za-z0-9.:-]*\Z")


def discover(provider_id: str, connection: dict | None, api_key: str | None, *, transport=None) -> dict:
    """Return a normalized, non-inference-tested provider model catalogue.

    ``transport`` is an optional callable receiving a plain metadata request
    plan and returning its decoded JSON object.  Plans may be GET requests or
    Ollama ``POST /api/show`` requests with a JSON ``body``.  Tests and callers
    with an existing HTTP stack can inject it; otherwise a bounded
    standard-library fetch is used.
    """
    _provider(provider_id)
    normalized = validate_connection(provider_id, connection)
    if provider_id == "gemini":
        try:
            return gemini_discover(normalized, api_key, transport=transport or _fetch_json)
        except GeminiError as exc:
            raise ProviderError(str(exc)) from exc
    if provider_id == "openrouter":
        try:
            return openrouter_discover(normalized, api_key, transport=transport or _fetch_json)
        except OpenRouterError as exc:
            raise ProviderError(str(exc)) from exc
    static = _static_catalogue(provider_id)
    if static is not None:
        models, warnings, evidence = static
        return {
            "provider_id": provider_id,
            "fetched_at": _now_iso(),
            "models": models,
            "source": "provider_documentation",
            "evidence": evidence,
            "warnings": warnings,
        }
    plan = _discovery_plan(provider_id, normalized, api_key)
    fetch = transport or _fetch_json
    raw = fetch(copy.deepcopy(plan))
    if not isinstance(raw, dict):
        raise ProviderError("Provider model discovery did not return an object.")
    warnings = ["Catalogue entries are advertised by the provider and have not been inference-tested."]
    if provider_id == "ollama":
        models, detail_warnings = _ollama_models(raw, normalized, api_key, fetch)
        warnings.extend(detail_warnings)
        warnings.append(
            "Ollama context values are model-declared maxima from /api/show; the daemon's effective runtime allocation remains provider-managed."
        )
    else:
        enriched = None
        if provider_id == "grok":
            if not isinstance(raw.get("models"), list):
                raise ProviderError("Grok language-model discovery response has no model list.")
            raw = {"data": raw["models"]}
            detail_plan = {**plan, "url": normalized["base_url"] + "/models"}
            try:
                details = fetch(copy.deepcopy(detail_plan))
                if not isinstance(details, dict) or not isinstance(details.get("data"), list):
                    raise ProviderError("Grok context metadata response has no model list.")
                enriched = {card["id"]: card for card in details["data"]
                            if isinstance(card, dict) and isinstance(card.get("id"), str)}
            except ProviderError:
                warnings.append("Grok context metadata was unavailable; exact known-model documentation is used where available.")
        if provider_id == "cerebras":
            public_plan = _cerebras_public_plan()
            try:
                public_raw = fetch(copy.deepcopy(public_plan))
            except ProviderError:
                public_raw = None
                warnings.append("Cerebras public model metadata was unavailable; only account-list and verified fixed-ID metadata were used.")
            if isinstance(public_raw, dict) and isinstance(public_raw.get("data"), list):
                enriched = {
                    card["id"]: card for card in public_raw["data"]
                    if isinstance(card, dict) and isinstance(card.get("id"), str)
                    and _MODEL_ID.fullmatch(card["id"])
                }
            elif public_raw is not None:
                warnings.append("Cerebras public model metadata was malformed and was ignored.")
        models = _models_from_api(provider_id, raw, plan["url"], enriched=enriched)
    if provider_id == "deepseek":
        warnings.append("DeepSeek publishes 1M and 384K labels without exact integer values; numeric context and output limits remain provider-managed.")
    if provider_id == "ollama" and any(model.get("context") is None for model in models):
        warnings.append("At least one Ollama model did not report an exact declared context limit.")
    if provider_id == "cerebras" and any(model.get("context") is None for model in models):
        warnings.append("At least one Cerebras model did not report an exact context limit; it remains provider-managed.")
    if provider_id == "cerebras":
        warnings.append(
            "Reasoning tool cycles require gateway-signed assistant reasoning replay through the Cerebras adapter."
        )
    if provider_id == "muse":
        if any(model.get("metadata_evidence") for model in models):
            warnings.append(
                "Exact Muse Spark 1.3 context and output limits were enriched from Meta's Model API cookbook "
                f"({_MUSE_COOKBOOK}); effort ranks follow Meta's current first-party reasoning documentation "
                f"({_MUSE_REASONING_DOCS}) unless the account list supplies effort_modes."
            )
        if any(model.get("context") is None for model in models):
            warnings.append(
                "At least one Meta Model API model did not report an exact context limit; it remains provider-managed."
            )
    if provider_id == "grok":
        warnings.append("Grok uses xAI API billing. Fast requests Priority processing at a premium token price; the actual tier is recorded when returned.")
        if any(model.get("context") is None for model in models):
            warnings.append("At least one Grok model has no exact reported context limit; it remains provider-managed.")
    if provider_id in {"claude", "codex"}:
        warnings.append(
            "This list route reports model identifiers only; context sizes and capabilities remain provider-managed."
        )
    if provider_id == "mistral":
        removed = sorted({
            alias for model in models for alias in model.get("ambiguous_aliases_removed", [])
        })
        if removed:
            warnings.append(
                "Mistral aliases with conflicting context or capability specifications were omitted: "
                + ", ".join(removed)
            )
    return {
        "provider_id": provider_id,
        "fetched_at": _now_iso(),
        "models": models,
        "source": "provider_api" if provider_id != "ollama" else "ollama_daemon",
        "evidence": plan["url"],
        "warnings": warnings,
    }
