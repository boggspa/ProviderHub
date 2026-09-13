"""Gemini API-key adapter for Provider Hub.

The inference surface is Google's official OpenAI-compatible Chat Completions
endpoint.  Gemini thought signatures live in provider-specific ``extra_content``
fields on Chat messages and tool calls.  Claude cannot represent those fields
directly, so this module carries them in a gateway-authenticated Anthropic
``redacted_thinking`` block.  The existing Responses bridge encrypts that block
again when presenting it to Codex.

No SDK, OAuth credential, CLI session, or Antigravity state is used here.
"""
from __future__ import annotations

import base64
import copy
from datetime import datetime, timezone
import hashlib
import hmac
import json
import math
import re
import secrets
import urllib.parse


class GeminiError(ValueError):
    """A Gemini catalogue, request, response, or replay envelope is invalid."""


PROVIDER_ID = "gemini"
PROVIDER_NAME = "Gemini API"
BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai"
MODELS_URL = "https://generativelanguage.googleapis.com/v1beta/models"
THINKING_DOCS = "https://ai.google.dev/gemini-api/docs/thinking"
FUNCTION_CALLING_DOCS = "https://ai.google.dev/gemini-api/docs/function-calling"
THOUGHT_SIGNATURE_DOCS = "https://ai.google.dev/gemini-api/docs/generate-content/thought-signatures"
OPENAI_DOCS = "https://ai.google.dev/gemini-api/docs/openai"
CLIENT_HEADER = "provider-hub-oai/0.4.0"
ENVELOPE_PREFIX = "ph_gemini_v1."


DESCRIPTOR = {
    "id": PROVIDER_ID,
    "name": PROVIDER_NAME,
    "protocol": "chat_completions",
    "default_base_url": BASE_URL,
    "default_region": "global",
    "regions": {"global": BASE_URL},
    "auth_header": {"name": "Authorization", "prefix": "Bearer "},
    "credential_account": "GEMINI_API_KEY",
    "credential_env": "GEMINI_API_KEY",
    "setup_url": "https://aistudio.google.com/app/apikey",
    "capabilities": {
        "streaming": True,
        "tools": True,
        "thinking": "model_dependent",
        "vision": "model_dependent",
        "model_discovery": "api",
        "reasoning_history": "gateway_signed_replay",
    },
}

OFFICIAL_PATHS = {
    "",
    "/v1beta/openai",
    "/v1beta/openai/chat/completions",
}

_MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/+\-]{0,199}\Z")
_FUNCTION_NAME = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
_LOCAL_CREDENTIAL_FIELDS = {
    "api_key", "api-key", "x-api-key", "authorization", "gateway_token", "gateway-token",
}
_MAX_SIGNATURE = 4 * 1024 * 1024
_MAX_DISCOVERY_PAGES = 16

# This is an explicit intersection with the current function-calling support
# table.  Unknown generateContent models remain absent rather than being
# labelled tool-capable from their name.  Account membership and exact token
# limits still come live from models.list.
_MODEL_EFFORTS = {
    "gemini-3.8-flash": ("low", "medium", "high"),
    "gemini-3.7-flash": ("low", "medium", "high"),
    "gemini-3.6-flash": ("minimal", "low", "medium", "high"),
    "gemini-3.5-flash": ("minimal", "low", "medium", "high"),
    "gemini-3.5-flash-lite": ("minimal", "low", "medium", "high"),
    # The compatibility API maps minimal to low for 3.1 Pro.
    "gemini-3.1-pro-preview": ("minimal", "low", "medium", "high"),
    "gemini-3.1-flash-lite": ("minimal", "low", "medium", "high"),
    "gemini-3-flash-preview": ("minimal", "low", "medium", "high"),
    # The compatibility API maps minimal to a 1,024-token budget for 2.5.
    "gemini-2.5-pro": ("minimal", "low", "medium", "high"),
    "gemini-2.5-flash": ("none", "minimal", "low", "medium", "high"),
    "gemini-2.5-flash-lite": ("none", "minimal", "low", "medium", "high"),
}

_UNDERLYING_EFFORTS = {
    "gemini-3.8-flash": ("low", "medium", "high"),
    "gemini-3.7-flash": ("low", "medium", "high"),
    "gemini-3.6-flash": ("minimal", "low", "medium", "high"),
    "gemini-3.5-flash": ("minimal", "low", "medium", "high"),
    "gemini-3.5-flash-lite": ("minimal", "low", "medium", "high"),
    "gemini-3.1-pro-preview": ("low", "medium", "high"),
    "gemini-3.1-flash-lite": ("minimal", "low", "medium", "high"),
    "gemini-3-flash-preview": ("minimal", "low", "medium", "high"),
    "gemini-2.5-pro": ("low", "medium", "high"),
    "gemini-2.5-flash": ("none", "low", "medium", "high"),
    "gemini-2.5-flash-lite": ("none", "low", "medium", "high"),
}


def _positive_integer(value):
    return value if type(value) is int and value > 0 else None


def _boolean(value):
    return value if type(value) is bool else None


def _valid_key(api_key: str | None) -> str:
    if not isinstance(api_key, str) or not api_key.strip():
        raise GeminiError("A Gemini API key is required.")
    value = api_key.strip()
    if "\r" in value or "\n" in value:
        raise GeminiError("Gemini API key contains invalid characters.")
    return value


def _valid_model(value: str) -> str:
    if not isinstance(value, str) or not _MODEL_ID.fullmatch(value):
        raise GeminiError("Gemini model ID is invalid.")
    return value


def validate_connection(connection: dict | None) -> dict:
    """Accept only Google's global API-key compatibility endpoint."""
    if connection is None:
        connection = {}
    if not isinstance(connection, dict):
        raise GeminiError("Gemini connection must be an object.")
    if any(str(key).lower() in _LOCAL_CREDENTIAL_FIELDS for key in connection):
        raise GeminiError("Gemini credentials cannot be stored in connection settings.")
    region = connection.get("region", "global")
    if region != "global":
        raise GeminiError("Gemini API supports only its global endpoint here.")
    value = connection.get("base_url") or BASE_URL
    if not isinstance(value, str) or not value.strip():
        raise GeminiError("Gemini base URL must be a non-empty URL.")
    try:
        parsed = urllib.parse.urlsplit(value.strip())
        port = parsed.port
    except ValueError as exc:
        raise GeminiError("Gemini base URL has an invalid port.") from exc
    if parsed.username is not None or parsed.password is not None:
        raise GeminiError("Gemini base URL cannot contain credentials.")
    if parsed.query or parsed.fragment:
        raise GeminiError("Gemini base URL cannot contain a query or fragment.")
    if parsed.scheme.lower() != "https" or port not in (None, 443):
        raise GeminiError("Gemini requires HTTPS on Google's official endpoint.")
    if (parsed.hostname or "").lower().rstrip(".") != "generativelanguage.googleapis.com":
        raise GeminiError("Gemini base URL must use Google's official Generative Language domain.")
    if parsed.path.rstrip("/") not in OFFICIAL_PATHS:
        raise GeminiError("Gemini base URL has an unsupported path.")
    return {"region": "global", "base_url": BASE_URL}


def _metadata_headers(api_key: str) -> dict:
    return {
        "Accept": "application/json",
        "User-Agent": "ProviderHub/0.4",
        "x-goog-api-client": CLIENT_HEADER,
        "x-goog-api-key": api_key,
    }


def _inference_headers(api_key: str) -> dict:
    return {
        "Accept": "application/json",
        "Authorization": "Bearer " + api_key,
        "Content-Type": "application/json",
        "User-Agent": "ProviderHub/0.4",
        "x-goog-api-client": CLIENT_HEADER,
    }


def _page_plan(api_key: str, page_token: str | None = None) -> dict:
    query = {"pageSize": "1000"}
    if page_token:
        query["pageToken"] = page_token
    return {
        "method": "GET",
        "url": MODELS_URL + "?" + urllib.parse.urlencode(query),
        "headers": _metadata_headers(api_key),
        "provider_name": PROVIDER_NAME,
    }


def _capability_model_id(identifier: str, reported_base) -> str | None:
    """Resolve only an exact documented tool-model key.

    ``baseModelId`` is optional and some preview rows report a shorter family
    name.  The exact resource ID is therefore the sole fallback; prefixes and
    suffix heuristics would incorrectly admit image, audio, and other media
    variants that do not share the coding-tool contract.
    """
    if isinstance(reported_base, str) and reported_base in _MODEL_EFFORTS:
        return reported_base
    if identifier in _MODEL_EFFORTS:
        return identifier
    return None


def _catalogue_entry(card: dict) -> dict | None:
    name = card.get("name")
    if not isinstance(name, str) or not name.startswith("models/"):
        return None
    identifier = name.removeprefix("models/")
    if not _MODEL_ID.fullmatch(identifier):
        return None
    methods = card.get("supportedGenerationMethods")
    if not isinstance(methods, list) or not any(
        isinstance(method, str) and method.casefold() == "generatecontent"
        for method in methods
    ):
        return None
    reported_base = card.get("baseModelId")
    capability_model = _capability_model_id(identifier, reported_base)
    if capability_model is None:
        return None
    context = _positive_integer(card.get("inputTokenLimit"))
    max_output = _positive_integer(card.get("outputTokenLimit"))
    if context is None or max_output is None:
        return None
    display_name = card.get("displayName")
    if not isinstance(display_name, str) or not display_name.strip():
        display_name = identifier
    thinking = _boolean(card.get("thinking"))
    effort_modes = list(_MODEL_EFFORTS[capability_model]) if thinking is True else []
    description = card.get("description")
    base_model = (
        reported_base
        if isinstance(reported_base, str) and _MODEL_ID.fullmatch(reported_base)
        else None
    )
    return {
        "id": identifier,
        "canonical_id": base_model or identifier,
        "display_name": display_name,
        "context": context,
        "max_input": context,
        "max_output": max_output,
        "aliases": [identifier],
        "tools": True,
        "vision": True,
        "reasoning": thinking,
        "effort_modes": effort_modes,
        "provider_effort_modes": list(_UNDERLYING_EFFORTS[capability_model]) if thinking is True else [],
        "fast_mode": False,
        "inference_status": "advertised",
        "source": "provider_api",
        "evidence": MODELS_URL,
        "capability_evidence": FUNCTION_CALLING_DOCS,
        "thinking_evidence": THINKING_DOCS,
        "context_kind": "provider_reported_input_limit",
        "reasoning_history": "gateway_signed_replay" if thinking is True else "not_required_or_unknown",
        "complete_tool_cycles": True,
        "streaming": True,
        "parallel_tool_calls": True,
        "base_model_id": base_model,
        "version": card.get("version") if isinstance(card.get("version"), str) else None,
        "description": description if isinstance(description, str) else None,
    }


def discover(connection: dict | None, api_key: str | None, *, transport) -> dict:
    """List account-visible, documented function-calling models and exact limits.

    ``transport`` receives a metadata request plan and returns its decoded JSON
    object.  Pagination is bounded and API keys never appear in URLs or output.
    """
    validate_connection(connection)
    key = _valid_key(api_key)
    if not callable(transport):
        raise GeminiError("Gemini discovery requires a metadata transport.")
    cards = []
    seen_tokens = set()
    page_token = None
    for _ in range(_MAX_DISCOVERY_PAGES):
        raw = transport(copy.deepcopy(_page_plan(key, page_token)))
        if not isinstance(raw, dict) or not isinstance(raw.get("models"), list):
            raise GeminiError("Gemini model discovery response has no model list.")
        cards.extend(raw["models"])
        next_token = raw.get("nextPageToken")
        if next_token in (None, ""):
            break
        if not isinstance(next_token, str) or len(next_token) > 8192 or next_token in seen_tokens:
            raise GeminiError("Gemini model discovery returned an invalid pagination token.")
        seen_tokens.add(next_token)
        page_token = next_token
    else:
        raise GeminiError("Gemini model discovery exceeded its pagination limit.")

    models_by_id = {}
    omitted_non_tool = 0
    omitted_limits = 0
    for card in cards:
        if not isinstance(card, dict):
            omitted_non_tool += 1
            continue
        entry = _catalogue_entry(card)
        if entry is None:
            name = card.get("name")
            identifier = name.removeprefix("models/") if isinstance(name, str) and name.startswith("models/") else ""
            methods = card.get("supportedGenerationMethods")
            candidate = (
                bool(identifier)
                and _MODEL_ID.fullmatch(identifier) is not None
                and _capability_model_id(identifier, card.get("baseModelId")) is not None
                and isinstance(methods, list)
                and any(isinstance(method, str) and method.casefold() == "generatecontent" for method in methods)
            )
            if candidate and (
                _positive_integer(card.get("inputTokenLimit")) is None
                or _positive_integer(card.get("outputTokenLimit")) is None
            ):
                omitted_limits += 1
            else:
                omitted_non_tool += 1
            continue
        previous = models_by_id.get(entry["id"])
        if previous is not None and previous != entry:
            raise GeminiError("Gemini advertised one model ID with conflicting metadata.")
        models_by_id[entry["id"]] = entry
    models = sorted(models_by_id.values(), key=lambda item: (item["display_name"].casefold(), item["id"]))
    warnings = [
        "Catalogue membership and numeric limits come from the account-scoped Gemini models API and have not been inference-tested.",
        "Only models in Google's documented function-calling table are included; unknown generateContent models are omitted until their tool contract is documented.",
        "Claude Fast mode is not offered because this adapter has no documented same-model Fast control.",
    ]
    if omitted_non_tool:
        warnings.append(f"Omitted {omitted_non_tool} non-generation or not-documented-as-tool-capable model entries.")
    if omitted_limits:
        warnings.append(f"Omitted {omitted_limits} tool-capable model entries without exact API-reported input and output limits.")
    if any(model["reasoning"] is None for model in models):
        warnings.append("At least one model did not report its thinking capability; no effort choices are offered for it.")
    return {
        "provider_id": PROVIDER_ID,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "models": models,
        "source": "provider_api",
        "evidence": MODELS_URL,
        "warnings": warnings,
    }


def _canonical_json(value) -> bytes:
    try:
        return json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise GeminiError("Gemini assistant history must be canonical JSON data.") from exc


def _key_bytes(replay_key) -> bytes:
    if isinstance(replay_key, str):
        value = replay_key.encode()
    elif isinstance(replay_key, bytes):
        value = replay_key
    else:
        raise GeminiError("Gemini replay key must be text or bytes.")
    if not value:
        raise GeminiError("Gemini replay key cannot be empty.")
    return hmac.new(value, b"Provider Hub/Gemini thought signatures/v1", hashlib.sha256).digest()


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def _b64decode(value: str) -> bytes:
    if not isinstance(value, str) or not value or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise GeminiError("Gemini reasoning envelope is malformed.")
    try:
        return base64.b64decode(
            value + "=" * (-len(value) % 4), altchars=b"-_", validate=True,
        )
    except Exception as exc:
        raise GeminiError("Gemini reasoning envelope is malformed.") from exc


def _signature(value, label: str, *, optional=True) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > _MAX_SIGNATURE:
        raise GeminiError(f"{label} must be bounded non-empty text.")
    return value


def _google_signature(value, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise GeminiError(f"{label} extra_content must be an object.")
    google = value.get("google")
    if google is None:
        return None
    if not isinstance(google, dict):
        raise GeminiError(f"{label} Google extra_content must be an object.")
    return _signature(google.get("thought_signature"), f"{label} thought signature")


def _visible_blocks(content) -> list[dict]:
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if not isinstance(content, list) or not all(isinstance(block, dict) for block in content):
        raise GeminiError("Assistant content must be text or an array of blocks.")
    return copy.deepcopy(content)


def _tool_count(blocks: list[dict]) -> int:
    return sum(block.get("type") == "tool_use" for block in blocks)


def _envelope_mac(payload: dict, visible: list[dict], upstream_model: str, scope: str, replay_key) -> bytes:
    if not isinstance(scope, str) or not scope:
        raise GeminiError("Gemini replay scope must be non-empty text.")
    context = {
        "model": _valid_model(upstream_model),
        "scope": scope,
        "visible_sha256": hashlib.sha256(_canonical_json(visible)).hexdigest(),
        "payload": payload,
    }
    return hmac.new(_key_bytes(replay_key), _canonical_json(context), hashlib.sha256).digest()


def seal_replay(
    message_signature: str | None,
    tool_signatures: list[str | None],
    visible: list[dict],
    upstream_model: str,
    scope: str,
    replay_key,
    *,
    force=False,
) -> dict | None:
    """Create one opaque Claude block containing positional Google signatures."""
    message_signature = _signature(message_signature, "Gemini message thought signature")
    if not isinstance(tool_signatures, list):
        raise GeminiError("Gemini tool thought signatures must be an array.")
    normalized_tools = [
        _signature(value, "Gemini tool thought signature") for value in tool_signatures
    ]
    if len(normalized_tools) != _tool_count(visible):
        raise GeminiError("Gemini tool thought signatures do not match the assistant tool calls.")
    if message_signature is None and not any(normalized_tools) and not force:
        return None
    payload = {"message": message_signature, "tools": normalized_tools}
    encoded = _b64encode(_canonical_json(payload))
    mac = _b64encode(_envelope_mac(payload, visible, upstream_model, scope, replay_key))
    return {"type": "redacted_thinking", "data": ENVELOPE_PREFIX + encoded + "." + mac}


def _open_replay(block: dict, visible: list[dict], upstream_model: str, scope: str, replay_key) -> dict:
    if block.get("type") != "redacted_thinking":
        raise GeminiError("Gemini reasoning replay requires an opaque redacted thinking block.")
    data = block.get("data")
    if not isinstance(data, str) or not data.startswith(ENVELOPE_PREFIX):
        raise GeminiError("A foreign reasoning block cannot be replayed as Gemini thought context.")
    try:
        encoded, supplied_mac = data[len(ENVELOPE_PREFIX):].split(".", 1)
        payload = json.loads(_b64decode(encoded))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GeminiError("Gemini reasoning envelope is malformed.") from exc
    if not isinstance(payload, dict) or set(payload) != {"message", "tools"}:
        raise GeminiError("Gemini reasoning envelope is malformed.")
    message_signature = _signature(payload.get("message"), "Gemini message thought signature")
    tools = payload.get("tools")
    if not isinstance(tools, list) or len(tools) != _tool_count(visible):
        raise GeminiError("Gemini reasoning envelope does not match its assistant tool calls.")
    tool_signatures = [_signature(value, "Gemini tool thought signature") for value in tools]
    normalized = {"message": message_signature, "tools": tool_signatures}
    expected = _b64encode(_envelope_mac(normalized, visible, upstream_model, scope, replay_key))
    if not hmac.compare_digest(supplied_mac, expected):
        raise GeminiError("Gemini reasoning signature does not match this model, account, or assistant output.")
    return normalized


def validate_messages(messages, upstream_model: str, scope: str, replay_key) -> dict[int, dict]:
    """Authenticate Gemini replay blocks without mutating client history."""
    _valid_model(upstream_model)
    _key_bytes(replay_key)
    if not isinstance(messages, list):
        raise GeminiError("Messages must be an array.")
    result = {}
    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            raise GeminiError("Every message must be an object.")
        if message.get("role") != "assistant":
            continue
        blocks = _visible_blocks(message.get("content", ""))
        reasoning = [block for block in blocks if block.get("type") in {"thinking", "redacted_thinking"}]
        visible = [block for block in blocks if block.get("type") not in {"thinking", "redacted_thinking"}]
        has_tools = _tool_count(visible) > 0
        if len(reasoning) > 1:
            raise GeminiError("A Gemini assistant message may contain only one replay envelope.")
        if not reasoning:
            if has_tools:
                raise GeminiError("Gemini tool history is missing its authenticated thought-signature envelope.")
            continue
        if blocks[0] is not reasoning[0]:
            raise GeminiError("Gemini reasoning replay must be the first assistant content block.")
        result[index] = _open_replay(reasoning[0], visible, upstream_model, scope, replay_key)
    return result


def _normalize_effort(payload: dict, model_spec: dict, upstream_model: str) -> tuple[str | None, dict]:
    thinking = payload.get("thinking") or {}
    output = payload.get("output_config") or {}
    if not isinstance(thinking, dict) or not isinstance(output, dict):
        raise GeminiError("Thinking and output_config must be objects.")
    thinking_type = thinking.get("type")
    if thinking_type not in (None, "enabled", "disabled", "adaptive"):
        raise GeminiError("Unsupported Gemini thinking mode.")
    requested = output.get("effort")
    if requested is not None and not isinstance(requested, str):
        raise GeminiError("Gemini reasoning effort must be text.")
    if thinking_type == "disabled":
        if requested not in (None, "none"):
            raise GeminiError("Gemini received conflicting thinking and effort controls.")
        requested = "none"
    elif thinking_type in {"enabled", "adaptive"} and requested == "none":
        raise GeminiError("Gemini received conflicting thinking and effort controls.")
    reasoning = model_spec.get("reasoning")
    if thinking_type in {"enabled", "adaptive"} and reasoning is not True:
        if reasoning is False:
            raise GeminiError("The selected Gemini model does not support thinking.")
        raise GeminiError("Gemini thinking support is unknown for this model; use its default setting.")
    if requested is None:
        return None, {}
    if requested != "none" and reasoning is False:
        raise GeminiError("The selected Gemini model does not support reasoning effort.")
    supported = model_spec.get("effort_modes")
    if not isinstance(supported, list) or not all(isinstance(value, str) for value in supported):
        raise GeminiError("Gemini effort metadata is malformed; refresh the model catalogue.")
    if not supported:
        raise GeminiError("Gemini effort support is unknown for this model; use its default setting.")
    normalized = {"xhigh": "high", "max": "high", "ultra": "high"}.get(requested, requested)
    if normalized not in supported:
        if normalized == "none":
            raise GeminiError("Thinking cannot be disabled on this Gemini model.")
        raise GeminiError(f"Gemini model does not support reasoning effort {requested!r}.")
    controls = {}
    if normalized != requested:
        controls["reasoning_effort"] = f"{requested}_normalized_to_{normalized}"
    return normalized, controls


def _attach_replays(body_messages: list[dict], source_messages: list[dict], replays: dict[int, dict]) -> None:
    output_assistants = iter(message for message in body_messages if message.get("role") == "assistant")
    for source_index, source in enumerate(source_messages):
        if not isinstance(source, dict) or source.get("role") != "assistant":
            continue
        blocks = _visible_blocks(source.get("content", ""))
        visible = [block for block in blocks if block.get("type") not in {"thinking", "redacted_thinking"}]
        if not visible:
            if source_index in replays:
                raise GeminiError("Gemini reasoning replay has no visible assistant output to bind.")
            continue
        try:
            target = next(output_assistants)
        except StopIteration as exc:
            raise GeminiError("Gemini assistant history could not be translated safely.") from exc
        replay = replays.get(source_index)
        if replay is None:
            continue
        if replay["message"] is not None:
            target["extra_content"] = {"google": {"thought_signature": replay["message"]}}
        calls = target.get("tool_calls") or []
        if len(calls) != len(replay["tools"]):
            raise GeminiError("Gemini replay signatures no longer match translated tool calls.")
        for call, signature in zip(calls, replay["tools"]):
            if signature is not None:
                call["extra_content"] = {"google": {"thought_signature": signature}}


def _normalize_openai_messages(messages: list[dict]) -> None:
    call_names = {}
    for message in messages:
        for call in message.get("tool_calls") or []:
            function = call.get("function") if isinstance(call, dict) else None
            if isinstance(function, dict) and isinstance(call.get("id"), str) and isinstance(function.get("name"), str):
                call_names[call["id"]] = function["name"]
        if message.get("role") == "tool":
            name = call_names.get(message.get("tool_call_id"))
            if name:
                message["name"] = name
        content = message.get("content")
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") == "image_url" and isinstance(part.get("image_url"), str):
                    part["image_url"] = {"url": part["image_url"]}


def _estimated_input_tokens(payload: dict) -> int:
    images = 0

    def without_image_data(value):
        nonlocal images
        if isinstance(value, dict):
            if value.get("type") == "redacted_thinking":
                # This is transport metadata, not text tokens. Its encrypted
                # byte length cannot be used to estimate Google's input usage.
                return {"type": "redacted_thinking"}
            if value.get("type") == "image":
                images += 1
                return {"type": "image"}
            return {key: without_image_data(item) for key, item in value.items()}
        if isinstance(value, list):
            return [without_image_data(item) for item in value]
        return value

    subset = without_image_data({
        key: payload[key] for key in ("system", "messages", "tools") if key in payload
    })
    try:
        size = len(json.dumps(subset, ensure_ascii=False).encode())
    except (TypeError, ValueError) as exc:
        raise GeminiError("Gemini request content must be JSON-serializable.") from exc
    return max(1, math.ceil(size / 3) + images * 4096)


def prepare_request(
    connection: dict | None,
    api_key: str | None,
    anthropic_payload: dict,
    upstream_model: str,
    model_spec: dict | None,
    scope: str,
    replay_key,
    *,
    translate_chat=None,
) -> dict:
    """Build one authenticated Gemini Chat Completions request plan."""
    normalized = validate_connection(connection)
    key = _valid_key(api_key)
    upstream_model = _valid_model(upstream_model)
    if not isinstance(anthropic_payload, dict):
        raise GeminiError("Anthropic request payload must be an object.")
    source_messages = anthropic_payload.get("messages")
    replays = validate_messages(source_messages, upstream_model, scope, replay_key)
    spec = copy.deepcopy(model_spec) if isinstance(model_spec, dict) else {}
    max_input = spec.get("max_input", spec.get("context"))
    if type(max_input) is int and max_input > 0 and _estimated_input_tokens(anthropic_payload) >= max_input:
        raise GeminiError(f"Request exceeds the model's reported {max_input}-token input limit.")
    # Google reports independent input and output limits. The shared Chat
    # translator's context field is a combined-window clamp, so leave that
    # field out here and retain max_output as the exact output ceiling.
    translation_spec = {**spec, "context": None}
    if translate_chat is None:
        # Delayed import avoids a providers -> gemini_provider import cycle.
        from providers import _translate_chat_payload as translate_chat
    try:
        body, name_map = translate_chat(
            PROVIDER_ID, anthropic_payload, upstream_model, translation_spec,
        )
    except GeminiError:
        raise
    except Exception as exc:
        # ProviderError remains a ValueError at this seam, but present a stable
        # Gemini adapter error to gateway callers and unit tests.
        raise GeminiError(str(exc)) from exc
    _attach_replays(body["messages"], source_messages, replays)
    _normalize_openai_messages(body["messages"])
    effort, controls = _normalize_effort(anthropic_payload, spec, upstream_model)
    if effort is not None:
        body["reasoning_effort"] = effort
    if anthropic_payload.get("speed") == "fast" or anthropic_payload.get("service_tier") in {"fast", "priority"}:
        raise GeminiError("Claude Fast mode is not available for the Gemini API adapter.")
    tier = anthropic_payload.get("service_tier")
    if tier not in (None, "", "auto", "default", "standard"):
        raise GeminiError("Gemini API adapter does not expose the requested service tier.")
    if body.get("stream"):
        body["stream_options"] = {"include_usage": True}
    return {
        "url": normalized["base_url"] + "/chat/completions",
        "headers": _inference_headers(key),
        "body": body,
        "protocol": "chat_completions",
        "tool_name_map": name_map,
        "compatibility": {
            "complete_tool_cycles": True,
            "reasoning_history": "gateway_signed_replay",
            "verified_reasoning_messages": len(replays),
            **controls,
        },
    }


def _visible_text(content) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        output = []
        for part in content:
            if not isinstance(part, dict) or part.get("type") != "text" or not isinstance(part.get("text"), str):
                raise GeminiError("Gemini returned an unsupported content format.")
            output.append(part["text"])
        return "".join(output)
    raise GeminiError("Gemini returned an unsupported content format.")


def usage_counts(usage) -> dict:
    usage = usage if isinstance(usage, dict) else {}
    prompt = usage.get("prompt_tokens", 0)
    completion = usage.get("completion_tokens", 0)
    total = usage.get("total_tokens")
    prompt = prompt if type(prompt) is int and prompt >= 0 else 0
    completion = completion if type(completion) is int and completion >= 0 else 0
    # Gemini total_tokens includes hidden thought tokens that completion_tokens
    # may exclude.  Anthropic output_tokens and Responses accounting must retain
    # that consumed output context.
    generated = total - prompt if type(total) is int and total >= prompt + completion else completion
    return {"input_tokens": prompt, "output_tokens": generated}


def _stop_reason(reason, has_tools: bool) -> str:
    if reason in {"length", "max_tokens"}:
        return "max_tokens"
    return "tool_use" if has_tools else "end_turn"


def _strict_tool_signature(upstream_model: str, model_spec: dict | None) -> bool:
    base = (model_spec or {}).get("base_model_id")
    if not isinstance(base, str):
        base = upstream_model
    return base.startswith("gemini-3")


def _parse_tool(call: dict, names: dict) -> tuple[dict, str | None]:
    if not isinstance(call, dict):
        raise GeminiError("Gemini returned a malformed tool call.")
    identifier = call.get("id")
    function = call.get("function")
    if not isinstance(identifier, str) or not identifier or not isinstance(function, dict):
        raise GeminiError("Gemini returned an incomplete tool call.")
    name = function.get("name")
    if not isinstance(name, str) or not name:
        raise GeminiError("Gemini returned a tool call without a name.")
    arguments = function.get("arguments", "{}")
    try:
        value = json.loads(arguments) if isinstance(arguments, str) else copy.deepcopy(arguments)
    except (TypeError, ValueError) as exc:
        raise GeminiError("Gemini returned invalid tool arguments.") from exc
    if not isinstance(value, dict):
        raise GeminiError("Gemini tool arguments must decode to an object.")
    block = {"type": "tool_use", "id": identifier, "name": names.get(name, name), "input": value}
    return block, _google_signature(call.get("extra_content"), "Gemini tool call")


def translate_response(
    response: dict,
    requested_model: str,
    tool_name_map: dict,
    upstream_model: str,
    scope: str,
    replay_key,
    *,
    model_spec: dict | None = None,
) -> dict:
    """Translate a complete Gemini Chat response into Anthropic Messages."""
    try:
        choices = response["choices"]
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise GeminiError("Gemini returned no completion choice.")
        choice = choices[0]
        message = choice["message"]
        if not isinstance(message, dict):
            raise GeminiError("Gemini returned a malformed assistant message.")
    except (KeyError, TypeError) as exc:
        raise GeminiError("Gemini returned a malformed response.") from exc
    visible = []
    text = _visible_text(message.get("content"))
    if text:
        visible.append({"type": "text", "text": text})
    calls = message.get("tool_calls") or []
    if not isinstance(calls, list):
        raise GeminiError("Gemini returned malformed tool calls.")
    tool_signatures = []
    tool_ids = set()
    for call in calls:
        block, signature = _parse_tool(call, tool_name_map)
        if block["id"] in tool_ids:
            raise GeminiError("Gemini returned duplicate tool call IDs.")
        tool_ids.add(block["id"])
        visible.append(block)
        tool_signatures.append(signature)
    message_signature = _google_signature(message.get("extra_content"), "Gemini assistant message")
    if calls and _strict_tool_signature(upstream_model, model_spec) and not tool_signatures[0]:
        raise GeminiError("Gemini returned tool calls without the required thought signature.")
    envelope = seal_replay(
        message_signature, tool_signatures, visible, upstream_model, scope, replay_key,
        force=bool(calls),
    )
    content = ([envelope] if envelope is not None else []) + visible
    if not content:
        raise GeminiError("Gemini returned no assistant content.")
    return {
        "id": "msg_" + secrets.token_hex(12),
        "type": "message",
        "role": "assistant",
        "model": requested_model,
        "content": content,
        "stop_reason": _stop_reason(choice.get("finish_reason"), bool(calls)),
        "stop_sequence": None,
        "usage": usage_counts(response.get("usage")),
    }


class GeminiStreamAdapter:
    """Buffer Gemini Chat chunks so the opaque replay block remains first."""

    def __init__(
        self,
        requested_model: str,
        tool_name_map: dict,
        upstream_model: str,
        scope: str,
        replay_key,
        *,
        model_spec: dict | None = None,
    ):
        self.requested_model = requested_model
        self.names = dict(tool_name_map or {})
        self.upstream_model = _valid_model(upstream_model)
        if not isinstance(scope, str) or not scope:
            raise GeminiError("Gemini replay scope must be non-empty text.")
        self.scope = scope
        _key_bytes(replay_key)
        self.replay_key = replay_key
        self.model_spec = copy.deepcopy(model_spec) if isinstance(model_spec, dict) else {}
        self.identifier = "msg_" + secrets.token_hex(12)
        self.text_parts = []
        self.message_signature = None
        self.tools = {}
        self._usage = {"input_tokens": 0, "output_tokens": 0}
        self.finish = None
        self.started = False
        self.ended = False

    @property
    def usage(self) -> dict:
        return dict(self._usage)

    def start(self) -> list[dict]:
        if self.started:
            raise GeminiError("Gemini stream has already started.")
        self.started = True
        return [{
            "type": "message_start",
            "message": {
                "id": self.identifier,
                "type": "message",
                "role": "assistant",
                "model": self.requested_model,
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": self.usage,
            },
        }]

    @staticmethod
    def _merge_signature(current, candidate, label):
        if candidate is None:
            return current
        candidate = _signature(candidate, label)
        if current is not None and current != candidate:
            raise GeminiError(f"{label} changed during streaming.")
        return candidate

    def feed(self, chunk: dict) -> list[dict]:
        if not self.started or self.ended:
            raise GeminiError("Gemini stream is not active.")
        if not isinstance(chunk, dict):
            raise GeminiError("Gemini stream chunk must be an object.")
        if chunk.get("usage") is not None:
            self._usage = usage_counts(chunk["usage"])
        choices = chunk.get("choices", [])
        if not isinstance(choices, list):
            raise GeminiError("Gemini stream choices must be an array.")
        for choice in choices:
            if not isinstance(choice, dict):
                raise GeminiError("Gemini stream choice must be an object.")
            if choice.get("index", 0) != 0:
                continue
            finish = choice.get("finish_reason")
            if finish is not None:
                if not isinstance(finish, str):
                    raise GeminiError("Gemini stream finish reason must be text.")
                self.finish = finish
            delta = choice.get("delta") or {}
            if not isinstance(delta, dict):
                raise GeminiError("Gemini stream delta must be an object.")
            text = _visible_text(delta.get("content"))
            if text:
                self.text_parts.append(text)
            candidate = _google_signature(delta.get("extra_content"), "Gemini assistant message")
            self.message_signature = self._merge_signature(
                self.message_signature, candidate, "Gemini message thought signature",
            )
            tool_deltas = delta.get("tool_calls") or []
            if not isinstance(tool_deltas, list):
                raise GeminiError("Gemini stream tool calls must be an array.")
            for position, tool in enumerate(tool_deltas):
                if not isinstance(tool, dict):
                    raise GeminiError("Gemini stream tool call must be an object.")
                key = tool.get("index", position)
                if type(key) is not int or key < 0:
                    raise GeminiError("Gemini stream tool index is invalid.")
                state = self.tools.setdefault(key, {
                    "id": None, "name": None, "arguments": [], "signature": None,
                })
                identifier = tool.get("id")
                if identifier is not None:
                    if not isinstance(identifier, str) or not identifier:
                        raise GeminiError("Gemini stream tool ID is invalid.")
                    if state["id"] not in (None, identifier):
                        raise GeminiError("Gemini stream tool ID changed during generation.")
                    state["id"] = identifier
                function = tool.get("function") or {}
                if not isinstance(function, dict):
                    raise GeminiError("Gemini stream tool function must be an object.")
                name = function.get("name")
                if name is not None:
                    if not isinstance(name, str) or not name:
                        raise GeminiError("Gemini stream tool name is invalid.")
                    if state["name"] not in (None, name):
                        raise GeminiError("Gemini stream tool name changed during generation.")
                    state["name"] = name
                arguments = function.get("arguments")
                if arguments is not None:
                    if not isinstance(arguments, str):
                        arguments = json.dumps(arguments, ensure_ascii=False)
                    state["arguments"].append(arguments)
                candidate = _google_signature(tool.get("extra_content"), "Gemini tool call")
                state["signature"] = self._merge_signature(
                    state["signature"], candidate, "Gemini tool thought signature",
                )
        return []

    def _assembled(self) -> tuple[list[dict], list[str | None]]:
        visible = []
        text = "".join(self.text_parts)
        if text:
            visible.append({"type": "text", "text": text})
        signatures = []
        tool_ids = set()
        for key in sorted(self.tools):
            state = self.tools[key]
            if not state["id"] or not state["name"]:
                raise GeminiError("Gemini stream returned an incomplete tool call.")
            if state["id"] in tool_ids:
                raise GeminiError("Gemini stream returned duplicate tool call IDs.")
            tool_ids.add(state["id"])
            raw_arguments = "".join(state["arguments"]) or "{}"
            try:
                arguments = json.loads(raw_arguments)
            except ValueError as exc:
                raise GeminiError("Gemini stream returned invalid tool arguments.") from exc
            if not isinstance(arguments, dict):
                raise GeminiError("Gemini stream tool arguments must decode to an object.")
            visible.append({
                "type": "tool_use",
                "id": state["id"],
                "name": self.names.get(state["name"], state["name"]),
                "input": arguments,
            })
            signatures.append(state["signature"])
        return visible, signatures

    def end(self) -> list[dict]:
        if not self.started or self.ended:
            raise GeminiError("Gemini stream is not active.")
        self.ended = True
        if self.finish is None:
            raise GeminiError("Gemini stream ended before a completion signal.")
        visible, signatures = self._assembled()
        if signatures and _strict_tool_signature(self.upstream_model, self.model_spec) and not signatures[0]:
            raise GeminiError("Gemini streamed tool calls without the required thought signature.")
        envelope = seal_replay(
            self.message_signature, signatures, visible, self.upstream_model, self.scope,
            self.replay_key, force=bool(signatures),
        )
        blocks = ([envelope] if envelope is not None else []) + visible
        if not blocks:
            raise GeminiError("Gemini stream returned no assistant content.")
        events = []
        for index, block in enumerate(blocks):
            events.append({"type": "content_block_start", "index": index, "content_block": {
                **copy.deepcopy(block),
                **({"text": ""} if block["type"] == "text" else {}),
                **({"input": {}} if block["type"] == "tool_use" else {}),
            }})
            if block["type"] == "text":
                events.append({"type": "content_block_delta", "index": index,
                               "delta": {"type": "text_delta", "text": block["text"]}})
            elif block["type"] == "tool_use":
                events.append({"type": "content_block_delta", "index": index,
                               "delta": {"type": "input_json_delta", "partial_json": json.dumps(
                                   block["input"], separators=(",", ":"), ensure_ascii=False)}})
            events.append({"type": "content_block_stop", "index": index})
        events.append({
            "type": "message_delta",
            "delta": {"stop_reason": _stop_reason(self.finish, bool(signatures)), "stop_sequence": None},
            "usage": self.usage,
        })
        events.append({"type": "message_stop"})
        return events
