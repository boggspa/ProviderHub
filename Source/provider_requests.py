"""Outbound request planning and preparation for provider endpoints."""
from __future__ import annotations
import copy
import hashlib
import json
import math
import re
from provider_registry import (
    FILE_TOOL_STEERING,
    ProviderError,
    _FUNCTION_NAME,
    _KIMI_FIXED_THINKING_MODELS,
    _LOCAL_CREDENTIAL_FIELDS,
    _MODEL_ID,
    _MUSE_EFFORT_RANKS,
    _auth_headers,
    _needs_file_tool_steering,
    _provider,
    validate_connection,
)
from catalogue import image_input_blocked
from chat_tool_order import repair_openai_tool_order
from effort_map import CEREBRAS_EFFORT_ALIASES, DEEPSEEK_EFFORT_ALIASES, EFFORT_ORDER, MISTRAL_EFFORT_ALIASES, cap_high_end, map_effort, mistral_effort_modes, nearest_effort, ollama_effort_aliases
from fast_models import CLAUDE_FAST_BETA, supports_fast_toggle
from gemini_provider import GeminiError, prepare_request as gemini_prepare_request
from openrouter_provider import OpenRouterError, finalize as openrouter_finalize, normalize_messages as openrouter_controls
from qwen_provider import QwenError, normalize_controls as qwen_controls
from minimax_provider import MiniMaxError, normalize_controls as minimax_controls


def _valid_model_id(value: str) -> str:
    if not isinstance(value, str) or not _MODEL_ID.fullmatch(value):
        raise ProviderError("Upstream model ID is invalid.")
    return value


def _content_blocks(value, *, system: bool = False) -> list[dict]:
    if isinstance(value, str):
        return [{"type": "text", "text": value}]
    if not isinstance(value, list) or not all(isinstance(block, dict) for block in value):
        raise ProviderError("Message content must be text or an array of content blocks.")
    if system:
        if any(block.get("type") != "text" for block in value):
            raise ProviderError("System messages may contain only text blocks.")
        if any(not isinstance(block.get("text", ""), str) for block in value):
            raise ProviderError("System message text must be a string.")
    return copy.deepcopy(value)


def _append_system_value(current, additions: list[dict]):
    if not additions:
        return current
    existing = [] if current in (None, "") else _content_blocks(current, system=True)
    combined = existing + copy.deepcopy(additions)
    return combined


def _output_effort(body: dict):
    output = body.get("output_config")
    if output is None:
        return None
    if not isinstance(output, dict):
        raise ProviderError("output_config must be an object.")
    effort = output.get("effort")
    if effort is not None and not isinstance(effort, str):
        raise ProviderError("Reasoning effort must be text.")
    return effort


def _write_output_effort(body: dict, effort: str | None) -> None:
    output = body.get("output_config")
    if output is None:
        output = {}
    if not isinstance(output, dict):
        raise ProviderError("output_config must be an object.")
    if effort is None:
        output.pop("effort", None)
        if not output:
            body.pop("output_config", None)
            return
    else:
        output["effort"] = effort
    body["output_config"] = output


def _thinking_type(body: dict) -> str | None:
    thinking = body.get("thinking")
    if thinking is None:
        return None
    if not isinstance(thinking, dict):
        raise ProviderError("thinking must be an object.")
    value = thinking.get("type")
    if value is not None and not isinstance(value, str):
        raise ProviderError("thinking.type must be text.")
    return value


def _muse_effort(requested: str, supported: set[str]) -> str:
    if requested == "ultra":
        ranked = [rank for rank in _MUSE_EFFORT_RANKS if not supported or rank in supported]
        if not ranked:
            raise ProviderError(f"Muse model does not support reasoning effort {requested!r}.")
        return ranked[-1]
    if requested in _MUSE_EFFORT_RANKS and (not supported or requested in supported):
        return requested
    # A rank Muse does not publish takes the closest one it does, rather than
    # refusing and costing the client its effort control for the session.
    nearest = nearest_effort(requested, [rank for rank in _MUSE_EFFORT_RANKS
                                         if not supported or rank in supported])
    if nearest is not None:
        return nearest
    raise ProviderError(f"Muse model does not support reasoning effort {requested!r}.")


def _write_thinking_type(body: dict, value: str, provider_name: str) -> None:
    thinking = body.get("thinking")
    if thinking is None:
        thinking = {}
    if not isinstance(thinking, dict):
        raise ProviderError("thinking must be an object.")
    existing = thinking.get("type")
    if existing not in (None, value):
        raise ProviderError(
            f"{provider_name} received conflicting thinking and effort controls.")
    thinking["type"] = value
    body["thinking"] = thinking


def _normalize_native_controls(
    provider_id: str,
    body: dict,
    upstream_model: str,
    model_spec: dict,
) -> dict:
    descriptor = _provider(provider_id)
    compatibility = {}
    if provider_id == "minimax":
        try:
            return minimax_controls(body, upstream_model, model_spec)
        except MiniMaxError as exc:
            raise ProviderError(str(exc)) from exc
    if provider_id == "claude":
        tier = body.pop("service_tier", None)
        if tier in {"fast", "priority"}:
            body["speed"] = "fast"
        elif tier not in (None, "", "auto", "default", "standard"):
            raise ProviderError("Claude supports only Standard or Fast processing.")
        if body.get("speed") in ("standard", None):
            body.pop("speed", None)
        elif body.get("speed") != "fast":
            raise ProviderError("Claude supports only Standard or Fast processing.")
    explicit_fast = (
        body.get("speed") == "fast"
        or body.get("service_tier") in {"fast", "priority"}
    )
    if explicit_fast and model_spec.get("fast_mode") is not True:
        raise ProviderError(
            f"{descriptor['name']} does not expose a documented same-model Fast control for this route.")

    requested = _output_effort(body)
    thinking_type = _thinking_type(body)
    if thinking_type == "adaptive" and provider_id != "muse":
        if model_spec.get("reasoning") is not True:
            raise ProviderError(
                f"The {descriptor['name']} model does not advertise thinking support.")
        # Fable/Opus clients use adaptive budgeting. These providers expose a
        # coarse enabled switch instead; retain other thinking fields while
        # making that loss of adaptive budgeting explicit in compatibility.
        body["thinking"]["type"] = "enabled"
        thinking_type = "enabled"
        compatibility["adaptive_thinking"] = "normalized_to_enabled"
    if (model_spec.get("reasoning") is False
            and (thinking_type not in (None, "disabled") or requested not in (None, "none"))):
        # Desktop shows the same five-rung slider on every row whatever the
        # route behind it can do, so a model with no reasoning axis will be
        # asked for one. Ignoring that costs the request nothing it could
        # have had; refusing costs the client its effort control for the
        # whole session, because Claude Code reads one refusal as the model
        # not supporting effort at all.
        body.pop("thinking", None)
        _write_output_effort(body, None)
        compatibility["reasoning_effort"] = f"{requested or 'thinking'}_ignored_no_reasoning_axis"
        requested, thinking_type = None, None

    if provider_id == "qwen-token-plan":
        try:
            return {**compatibility, **qwen_controls(body, upstream_model, model_spec)}
        except QwenError as exc:
            raise ProviderError(str(exc)) from exc

    if provider_id == "openrouter":
        try:
            return {**compatibility, **openrouter_controls(body, upstream_model, model_spec)}
        except OpenRouterError as exc:
            raise ProviderError(str(exc)) from exc

    if provider_id == "muse":
        if thinking_type == "disabled" or requested == "none":
            raise ProviderError(
                "Muse Spark reasoning cannot be disabled reliably on the public Meta Model API.")
        if thinking_type not in (None, "adaptive", "enabled"):
            raise ProviderError("Muse supports adaptive or enabled thinking for this route.")
        if requested is not None:
            supported = set(model_spec.get("effort_modes") or [])
            # Missing per-model metadata is not evidence of incompatibility.
            # Forward a documented Meta API effort for the provider to validate;
            # constrain it locally only when an explicit supported set exists.
            # A rank above an account-listed set now takes that set's top rank
            # rather than failing: Desktop offers all five rungs on every row,
            # and one refusal costs the client its effort control for the whole
            # session. The squeeze is recorded in compatibility, so a narrower
            # account is visible rather than silent.
            normalized = _muse_effort(requested, supported)
            _write_output_effort(body, normalized)
            if normalized != requested:
                compatibility["reasoning_effort"] = f"{requested}_normalized_to_{normalized}"
        choice = body.get("tool_choice")
        if choice is not None:
            if not isinstance(choice, dict):
                raise ProviderError("tool_choice must be an object.")
            if choice.get("type") not in (None, "auto"):
                raise ProviderError("Muse supports only automatic tool choice.")
        return compatibility

    if provider_id == "kimi":
        if upstream_model in _KIMI_FIXED_THINKING_MODELS:
            # Thinking is fixed on with no effort ladder, so there is no
            # higher, lower, or off state for a desktop control to select.
            # Codex always sends its slider effort (and the Responses bridge
            # turns "none" into thinking disabled), so rejecting here made
            # the model unusable from Codex. Drop the non-controls on the
            # wire and record the loss; the served model never changes.
            if requested is not None:
                _write_output_effort(body, None)
                compatibility["reasoning_effort"] = f"{requested}_ignored_thinking_fixed_on"
            if thinking_type == "disabled":
                body.pop("thinking", None)
                compatibility["thinking"] = "disabled_ignored_thinking_fixed_on"
            return compatibility
        supported = set(model_spec.get("effort_modes") or [])
        aliases = {
            "ultra": "max", "max": "max", "xhigh": "max",
            "high": "high", "medium": "high",
            "low": "low", "minimal": "low", "minimum": "low", "light": "low",
        }
        if requested == "none":
            # Kimi documents that disabling K3 serves K2.8 Preview instead.
            # Keep an exact K3 route exact; K2.8 can disable thinking in place.
            if upstream_model != "kimi-for-coding":
                raise ProviderError(
                    "Disabling thinking is not an exact-model control for this Kimi route.")
            _write_thinking_type(body, "disabled", descriptor["name"])
        elif requested is not None:
            normalized = aliases.get(requested)
            if normalized is not None and normalized not in supported:
                normalized = cap_high_end(normalized, supported)
            if normalized is None or normalized not in supported:
                normalized = nearest_effort(requested, supported)
            if normalized is None and (requested == "none" or requested not in EFFORT_ORDER):
                raise ProviderError(
                    f"Kimi model does not support reasoning effort {requested!r}.")
            if normalized is None:
                # "Ignored" has to mean it: leave the rank on the body and the
                # provider receives an unvalidated desktop string while the
                # client is told it was dropped.
                _write_output_effort(body, None)
                compatibility["reasoning_effort"] = f"{requested}_ignored_no_known_ranks"
            else:
                _write_thinking_type(body, "enabled", descriptor["name"])
                _write_output_effort(body, normalized)
                if normalized != requested:
                    compatibility["reasoning_effort"] = f"{requested}_normalized_to_{normalized}"
        if thinking_type == "disabled" and upstream_model != "kimi-for-coding":
            raise ProviderError(
                "Disabling thinking is not an exact-model control for this Kimi route.")
        return compatibility

    if provider_id == "mimo":
        if thinking_type not in (None, "enabled", "disabled"):
            raise ProviderError("MiMo supports only enabled or disabled thinking.")
        if requested is not None:
            if requested == "none":
                target = "disabled"
            elif requested in EFFORT_ORDER:
                # Every rank above "none" means the same thing to MiMo: on.
                target = "enabled"
                if requested not in {"minimal", "low", "medium", "high", "xhigh", "max", "ultra"}:
                    compatibility["reasoning_effort"] = f"{requested}_normalized_to_enabled"
            else:
                raise ProviderError(f"MiMo does not support reasoning effort {requested!r}.")
            _write_thinking_type(body, target, descriptor["name"])
            # MiMo's Anthropic surface documents thinking.type, not
            # output_config.effort. Preserve any other output_config fields.
            _write_output_effort(body, None)
        return compatibility

    if provider_id == "deepseek":
        if requested is not None:
            supported = model_spec.get("effort_modes") or []
            normalized = map_effort(requested, supported, DEEPSEEK_EFFORT_ALIASES)
            if normalized is None:
                normalized = cap_high_end(DEEPSEEK_EFFORT_ALIASES.get(requested), supported)
            if normalized is None:
                normalized = nearest_effort(requested, supported)
            if normalized is None and (requested == "none" or requested not in EFFORT_ORDER):
                raise ProviderError(
                    f"DeepSeek model does not support reasoning effort {requested!r}.")
            if normalized is None:
                _write_output_effort(body, None)
                compatibility["reasoning_effort"] = f"{requested}_ignored_no_known_ranks"
            else:
                _write_thinking_type(
                    body, "disabled" if normalized == "none" else "enabled", descriptor["name"])
                _write_output_effort(body, normalized)
                if normalized != requested:
                    compatibility["reasoning_effort"] = f"{requested}_normalized_to_{normalized}"
        return compatibility

    if provider_id == "ollama":
        if thinking_type not in (None, "enabled", "disabled"):
            raise ProviderError("Ollama supports only enabled or disabled thinking.")
        if thinking_type == "enabled" and model_spec.get("reasoning") is not True:
            raise ProviderError("The Ollama model does not advertise thinking support.")
        if requested is not None:
            supported = list(model_spec.get("effort_modes") or [])
            if supported:
                aliases = ollama_effort_aliases(upstream_model)
                normalized = map_effort(requested, supported, aliases)
                if normalized is None:
                    normalized = cap_high_end(aliases.get(requested), supported)
                if normalized is None:
                    normalized = nearest_effort(requested, supported)
                if normalized is None and (requested == "none" or requested not in EFFORT_ORDER):
                    raise ProviderError(
                        f"Ollama model does not support reasoning effort {requested!r}.")
                if normalized is None:
                    normalized = supported[-1]
                    compatibility["reasoning_effort"] = f"{requested}_normalized_to_{normalized}"
                if normalized == "none":
                    _write_thinking_type(body, "disabled", descriptor["name"])
                    _write_output_effort(body, None)
                else:
                    if model_spec.get("reasoning") is not True:
                        raise ProviderError("The Ollama model does not advertise thinking support.")
                    _write_thinking_type(body, "enabled", descriptor["name"])
                    _write_output_effort(body, normalized)
                    if normalized != requested:
                        compatibility["reasoning_effort"] = f"{requested}_normalized_to_{normalized}"
            elif requested == "none":
                _write_thinking_type(body, "disabled", descriptor["name"])
                _write_output_effort(body, None)
            elif requested in {"minimal", "low", "medium", "high", "xhigh", "max", "ultra"}:
                if model_spec.get("reasoning") is not True:
                    raise ProviderError("The Ollama model does not advertise thinking support.")
                _write_thinking_type(body, "enabled", descriptor["name"])
                _write_output_effort(body, None)
            else:
                # No published ranks: any rank above "none" just means on.
                if model_spec.get("reasoning") is not True:
                    raise ProviderError("The Ollama model does not advertise thinking support.")
                _write_thinking_type(body, "enabled", descriptor["name"])
                _write_output_effort(body, None)
                compatibility["reasoning_effort"] = f"{requested}_normalized_to_enabled"
        return compatibility
    return compatibility


def _normalize_native_payload(
    provider_id: str,
    payload: dict,
    upstream_model: str,
    model_spec: dict,
) -> tuple[dict, int, dict]:
    if not isinstance(payload, dict):
        raise ProviderError("Anthropic request payload must be an object.")
    result = copy.deepcopy(payload)
    for key in list(result):
        if str(key).lower() in _LOCAL_CREDENTIAL_FIELDS:
            result.pop(key, None)
    messages = result.get("messages")
    if not isinstance(messages, list) or not messages:
        raise ProviderError("At least one message is required.")

    normalized_messages = []
    message_system = []
    normalized_count = 0
    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            raise ProviderError("Every message must be an object.")
        role = message.get("role")
        if role in {"system", "developer"}:
            blocks = _content_blocks(message.get("content", ""), system=True)
            message_system.append({
                "type": "text",
                "text": (
                    f"[Provider Hub placement note: the following {role} instruction "
                    f"originally appeared at messages[{index}] and governs the next response.]"
                ),
            })
            message_system.extend(blocks)
            normalized_count += 1
            continue
        if role not in {"user", "assistant"}:
            raise ProviderError(f"Unsupported message role: {str(role)[:30]}.")
        normalized_messages.append(copy.deepcopy(message))
    if not normalized_messages:
        raise ProviderError("At least one user or assistant message is required.")
    if message_system:
        # These compatibility endpoints document only user/assistant history
        # roles.  Moving instruction roles into the top-level system field keeps
        # their authority for the one response being generated.  Position notes
        # preserve when each instruction entered the history; exact cache
        # placement cannot be retained across this protocol boundary.
        result["system"] = _append_system_value(result.get("system"), message_system)
    if _needs_file_tool_steering(result.get("tools")):
        result["system"] = _append_system_value(result.get("system"), [{"type": "text", "text": FILE_TOOL_STEERING}])
    result["messages"] = normalized_messages
    result["model"] = upstream_model
    compatibility = _normalize_native_controls(
        provider_id, result, upstream_model, model_spec)
    return result, normalized_count, compatibility


def _contains_content_type(value, kinds: set[str]) -> bool:
    if isinstance(value, dict):
        if value.get("type") in kinds:
            return True
        return any(_contains_content_type(item, kinds) for item in value.values())
    if isinstance(value, list):
        return any(_contains_content_type(item, kinds) for item in value)
    return False


def _has_cache_policy(body):
    """Respect caller cache controls, including invalid ones for upstream validation."""
    if "cache_control" in body:
        return True
    groups = [body.get("system"), body.get("tools")]
    groups.extend(message.get("content") for message in body.get("messages", []) if isinstance(message, dict))
    return any(isinstance(block, dict) and "cache_control" in block
               for group in groups if isinstance(group, list) for block in group)


def _claude_prompt_cache(body):
    # Anthropic's automatic cache point follows the last eligible block. Avoid
    # rewriting signed thinking/tool history or colliding with the caller's
    # breakpoint/TTL budget. This runs only for the Anthropic API, never a CLI
    # or another provider speaking the Messages compatibility protocol.
    if not _has_cache_policy(body):
        body["cache_control"] = {"type": "ephemeral"}


def _estimated_input_tokens(payload: dict) -> int:
    images = 0

    def without_image_data(value):
        nonlocal images
        if isinstance(value, dict):
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
        raise ProviderError("Request content must be JSON-serializable.") from exc
    return max(1, math.ceil(size / 3) + images * 4096)


def _image_input_rejected(provider_id: str | None, model_spec: dict) -> bool:
    # Catalogue vision is picker metadata, shared with the Codex projection
    # so an advertised modality and an accepted request always agree.
    return image_input_blocked(provider_id, model_spec)


def _image_capability_error() -> ProviderError:
    return ProviderError("The selected model does not advertise image input.")


def _apply_native_model_limits(body: dict, model_spec: dict, provider_id: str | None = None,
                               estimate_factor: float = 1.0) -> None:
    max_tokens = body.get("max_tokens", 4096)
    if type(max_tokens) is not int or max_tokens <= 0:
        raise ProviderError("max_tokens must be a positive integer.")
    max_output = model_spec.get("max_output")
    if type(max_output) is int and max_output > 0:
        max_tokens = min(max_tokens, max_output)
    context = model_spec.get("context")
    if type(context) is int and context > 0:
        # estimate_factor is the gateway's learned ratio of the provider's
        # real input count to this byte estimate (never above 1.0).
        estimate = max(1, int(_estimated_input_tokens(body) * estimate_factor))
        if estimate >= context:
            raise ProviderError(f"Request (~{estimate:,} tokens) exceeds the model's reported "
                                f"{context:,}-token context limit.")
        max_tokens = min(max_tokens, context - estimate)
    body["max_tokens"] = max_tokens
    if _image_input_rejected(provider_id, model_spec) and _contains_content_type(body.get("messages"), {"image"}):
        raise _image_capability_error()
    if model_spec.get("tools") is False:
        if body.get("tools") or _contains_content_type(body.get("messages"), {"tool_use", "tool_result"}):
            raise ProviderError("The selected model does not support tool use.")


def _chat_name(name: str, mapping: dict) -> str:
    if not isinstance(name, str) or not name:
        raise ProviderError("Tools require a name.")
    if _FUNCTION_NAME.fullmatch(name):
        mapped = name
    else:
        prefix = re.sub(r"[^A-Za-z0-9_-]", "_", name)[:50]
        mapped = prefix + "_" + hashlib.sha256(name.encode()).hexdigest()[:10]
    previous = mapping.get(mapped)
    if previous is not None and previous != name:
        raise ProviderError("Tool name collision after provider normalization.")
    mapping[mapped] = name
    return mapped


def _chat_tool_id(provider_id: str, identifier: str, seen: dict) -> str:
    if not isinstance(identifier, str) or not identifier:
        raise ProviderError("Tool call IDs are required.")
    mapped = hashlib.sha256(identifier.encode()).hexdigest()[:9] if provider_id == "mistral" else identifier
    previous = seen.get(mapped)
    if previous is not None and previous != identifier:
        raise ProviderError("Tool call ID collision after provider normalization.")
    seen[mapped] = identifier
    return mapped


def _requires_cerebras_replay(provider_id: str, model_spec: dict) -> bool:
    return provider_id == "cerebras" and model_spec.get("reasoning_history") in {
        "adapter_required", "gateway_signed_replay", "gateway_signed_replay_required",
    }


def _chat_content_part(provider_id: str, block: dict, model_spec: dict):
    kind = block.get("type")
    if kind == "text":
        text = block.get("text", "")
        if not isinstance(text, str):
            raise ProviderError("Message text must be a string.")
        return {"type": "text", "text": text}
    if kind == "image":
        if _image_input_rejected(provider_id, model_spec):
            raise _image_capability_error()
        source = block.get("source") if isinstance(block.get("source"), dict) else {}
        if source.get("type") == "base64":
            media = source.get("media_type", "")
            if media not in {"image/png", "image/jpeg", "image/webp", "image/gif"}:
                raise ProviderError("Unsupported image format.")
            data = source.get("data", "")
            if not isinstance(data, str):
                raise ProviderError("Image data must be base64 text.")
            url = "data:" + media + ";base64," + data
        elif source.get("type") == "url" and str(source.get("url", "")).startswith("https://"):
            if provider_id == "cerebras":
                raise ProviderError("Cerebras image inputs must be embedded as data URIs.")
            url = source["url"]
        else:
            raise ProviderError("Images must be base64 data or HTTPS URLs.")
        return {
            "type": "image_url",
            "image_url": {"url": url} if provider_id in {"cerebras", "grok"} else url,
        }
    if kind == "document":
        source = block.get("source") if isinstance(block.get("source"), dict) else {}
        if source.get("type") == "text":
            text = source.get("data", "")
            if not isinstance(text, str):
                raise ProviderError("Document text must be a string.")
            return {"type": "text", "text": text}
        if source.get("type") == "content":
            blocks = _content_blocks(source.get("content", []))
            if any(part.get("type") != "text" for part in blocks):
                raise ProviderError("Document content must contain only text blocks.")
            return {"type": "text", "text": "\n".join(part.get("text", "") for part in blocks)}
        raise ProviderError("PDF/document uploads require extracted text or images.")
    if kind in {"thinking", "redacted_thinking"}:
        if _requires_cerebras_replay(provider_id, model_spec):
            raise ProviderError(
                "Cerebras reasoning history requires a gateway-validated replay adapter; "
                "an Anthropic thinking block cannot be silently discarded or trusted as Chat reasoning."
            )
        # A signed Anthropic trace cannot be replayed to a different provider.
        return None
    raise ProviderError(f"Unsupported content block: {kind or 'missing type'}.")


def _compact_chat_content(parts: list[dict]):
    if all(part.get("type") == "text" for part in parts):
        return "\n".join(part.get("text", "") for part in parts)
    return list(parts)


def _chat_effort(provider_id: str, payload: dict, model_spec: dict, upstream_model: str | None = None):
    if provider_id == "grok":
        thinking = payload.get("thinking") or {}
        output = payload.get("output_config") or {}
        if not isinstance(thinking, dict) or not isinstance(output, dict):
            raise ProviderError("Thinking and output_config must be objects.")
        if thinking.get("type") not in (None, "enabled", "disabled", "adaptive"):
            raise ProviderError("Unsupported Grok thinking mode.")
        requested = output.get("effort")
        if requested is not None and not isinstance(requested, str):
            raise ProviderError("Grok reasoning effort must be a string.")
        if thinking.get("type") == "disabled" or requested == "none":
            if model_spec.get("reasoning") is not False:
                raise ProviderError("Reasoning cannot be disabled on this Grok model. Select a non-reasoning model if available.")
            return None
        if requested is None:
            return None
        supported = model_spec.get("effort_modes") or []
        target = {"minimal": "low", "max": "xhigh", "ultra": "xhigh"}.get(requested, requested)
        if target not in supported:
            target = cap_high_end(target, supported)
        if target is None or target not in supported:
            raise ProviderError(f"Grok effort {requested!r} is not advertised for this model. Use its default reasoning setting.")
        return target
    if not model_spec.get("reasoning"):
        return None
    thinking = payload.get("thinking") or {}
    output_config = payload.get("output_config") or {}
    if not isinstance(thinking, dict) or not isinstance(output_config, dict):
        raise ProviderError("Thinking and output_config must be objects.")
    disabled = thinking.get("type") == "disabled"
    requested = output_config.get("effort")
    if provider_id == "mistral":
        supported = list(model_spec.get("effort_modes") or []) or mistral_effort_modes(upstream_model or "", model_spec.get("reasoning"))
        if disabled:
            requested = "none"
        if requested is None:
            return "high" if "high" in supported else (supported[-1] if supported else None)
        normalized = map_effort(requested, supported, MISTRAL_EFFORT_ALIASES)
        if normalized is None:
            normalized = cap_high_end(MISTRAL_EFFORT_ALIASES.get(requested), supported)
        if normalized is None:
            normalized = nearest_effort(requested, supported)
        if normalized is None and (requested == "none" or requested not in EFFORT_ORDER):
            raise ProviderError("Unsupported Mistral reasoning effort.")
        if normalized is None:
            return "high" if "high" in supported else (supported[-1] if supported else None)
        return normalized
    if provider_id == "cerebras":
        supported = model_spec.get("effort_modes") or []
        if disabled:
            requested = "none"
        if requested is None:
            return None
        normalized = CEREBRAS_EFFORT_ALIASES.get(requested, requested)
        if normalized not in supported:
            normalized = cap_high_end(normalized, supported)
        if normalized is None or normalized not in supported:
            normalized = nearest_effort(requested, supported)
        if normalized is None and (requested == "none" or requested not in EFFORT_ORDER):
            # Not a rank at all, or a request to switch reasoning off that
            # this model cannot honour. Neither is a near-miss to round.
            raise ProviderError(f"Cerebras model does not support reasoning effort {requested!r}.")
        if normalized is None:
            # A known rank with no published ranks to compare it against:
            # leave the field off and let the model use its own default.
            return None
        return normalized
    return None


def _reasoning_map(provider_id: str, value) -> dict[int, str]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ProviderError("Verified reasoning must be an index-to-text object.")
    if provider_id != "cerebras" and value:
        raise ProviderError("Verified Cerebras reasoning cannot be used with another provider.")
    result = {}
    for index, reasoning in value.items():
        if type(index) is not int or index < 0 or not isinstance(reasoning, str) or not reasoning:
            raise ProviderError("Verified reasoning entries require a non-negative message index and non-empty text.")
        result[index] = reasoning
    return result


def _translate_chat_payload(
    provider_id: str,
    payload: dict,
    upstream_model: str,
    model_spec: dict,
    *,
    reasoning_by_message=None,
    estimate_factor: float = 1.0,
) -> tuple[dict, dict]:
    if not isinstance(payload, dict):
        raise ProviderError("Anthropic request payload must be an object.")
    source_messages = payload.get("messages")
    if not isinstance(source_messages, list) or not source_messages:
        raise ProviderError("At least one message is required.")
    if model_spec.get("tools") is False:
        if payload.get("tools") or _contains_content_type(source_messages, {"tool_use", "tool_result"}):
            raise ProviderError("The selected model does not support tool use.")
    verified_reasoning = _reasoning_map(provider_id, reasoning_by_message)
    used_reasoning = set()
    replay_required = _requires_cerebras_replay(provider_id, model_spec)
    max_tokens = payload.get("max_tokens", 4096)
    if type(max_tokens) is not int or max_tokens <= 0:
        raise ProviderError("max_tokens must be a positive integer.")
    max_output = model_spec.get("max_output")
    if type(max_output) is int and max_output > 0:
        max_tokens = min(max_tokens, max_output)
    context = model_spec.get("context")
    if type(context) is int and context > 0:
        # estimate_factor: see _apply_native_model_limits.
        estimate = max(1, int(_estimated_input_tokens(payload) * estimate_factor))
        if estimate >= context:
            raise ProviderError(f"Request (~{estimate:,} tokens) exceeds the model's reported "
                                f"{context:,}-token context limit.")
        max_tokens = min(max_tokens, context - estimate)

    messages = []
    # For Cerebras: Qwen 3.8 27B's chat template requires exactly one system
    # message at the beginning. Collect system parts to consolidate.
    system_parts = []
    if payload.get("system") not in (None, ""):
        blocks = _content_blocks(payload["system"], system=True)
        content = "\n".join(block.get("text", "") for block in blocks)
        if provider_id == "cerebras":
            system_parts.append(content)
        else:
            messages.append({"role": "system", "content": content})
    name_map = {}
    id_map = {}
    for source_index, message in enumerate(source_messages):
        if not isinstance(message, dict):
            raise ProviderError("Every message must be an object.")
        role = message.get("role")
        if role in {"system", "developer"}:
            blocks = _content_blocks(message.get("content", ""), system=True)
            # Cerebras chat templates reject the developer role: Qwen 3.8 27B
            # fails with "Unexpected message role" while GPT-OSS tolerated it.
            # The system role is universally supported, so map both here.
            content = "\n".join(block.get("text", "") for block in blocks)
            if provider_id == "cerebras":
                system_parts.append(content)
                continue
            output_role = "system"
            messages.append({"role": output_role, "content": content})
            continue
        if role not in {"user", "assistant"}:
            raise ProviderError(f"Unsupported message role: {str(role)[:30]}.")
        pending = []
        calls = []
        trace = verified_reasoning.get(source_index)
        trace_seen = False
        trace_attached = False

        def flush():
            nonlocal trace_attached
            if pending or calls:
                item = {"role": role, "content": _compact_chat_content(pending)}
                if calls:
                    item["tool_calls"] = list(calls)
                if provider_id == "mistral" and message.get("prefix") is True:
                    item["prefix"] = True
                if trace is not None:
                    if role != "assistant" or trace_attached:
                        raise ProviderError("Verified reasoning does not identify one assistant message.")
                    item["reasoning"] = trace
                    trace_attached = True
                messages.append(item)
                pending.clear()
                calls.clear()

        message_blocks = _content_blocks(message.get("content", ""))
        message_has_tools = any(block.get("type") == "tool_use" for block in message_blocks)
        for block in message_blocks:
            kind = block.get("type")
            if kind in {"thinking", "redacted_thinking"} and replay_required:
                if role != "assistant" or trace is None:
                    raise ProviderError(
                        "Cerebras reasoning history requires a caller-verified gateway signature."
                    )
                if kind != "thinking" or block.get("thinking") != trace:
                    raise ProviderError("Verified Cerebras reasoning does not match its thinking block.")
                trace_seen = True
                continue
            if kind == "tool_use":
                if role != "assistant":
                    raise ProviderError("Tool calls must be assistant messages.")
                if not isinstance(block.get("input", {}), dict):
                    raise ProviderError("Tool call input must be an object.")
                mapped_id = _chat_tool_id(provider_id, block.get("id"), id_map)
                mapped_name = _chat_name(block.get("name"), name_map)
                calls.append({
                    "id": mapped_id,
                    "type": "function",
                    "function": {
                        "name": mapped_name,
                        "arguments": json.dumps(block.get("input", {}), ensure_ascii=False),
                    },
                })
            elif kind == "tool_result":
                if role != "user":
                    raise ProviderError("Tool results must be user messages.")
                flush()
                mapped_id = _chat_tool_id(provider_id, block.get("tool_use_id"), id_map)
                result_parts = [
                    _chat_content_part(provider_id, part, model_spec)
                    for part in _content_blocks(block.get("content", ""))
                ]
                result_parts = [part for part in result_parts if part is not None]
                text_parts = [part for part in result_parts if part.get("type") == "text"]
                images = [part for part in result_parts if part.get("type") == "image_url"]
                text = _compact_chat_content(text_parts)
                if block.get("is_error"):
                    text = "Tool execution error:\n" + text
                if images:
                    text += "\nThe tool's images follow in the next user message."
                messages.append({"role": "tool", "tool_call_id": mapped_id, "content": text})
                pending.extend(images)
            else:
                part = _chat_content_part(provider_id, block, model_spec)
                if part is not None:
                    pending.append(part)
        flush()
        if trace is not None:
            if not trace_seen or not trace_attached:
                raise ProviderError("Verified reasoning did not match a complete assistant envelope.")
            used_reasoning.add(source_index)
        if (replay_required and role == "assistant" and message_has_tools
                and source_index not in used_reasoning):
            raise ProviderError("Cerebras tool history is missing verified reasoning.")

    # For Cerebras: insert the consolidated system message at the beginning.
    # Qwen 3.8 27B's chat template requires exactly one system message at messages[0].
    steer_files = _needs_file_tool_steering(payload.get("tools"))
    if provider_id == "cerebras" and steer_files:
        system_parts.append(FILE_TOOL_STEERING)
    if provider_id == "cerebras" and system_parts:
        messages.insert(0, {"role": "system", "content": "\n\n".join(system_parts)})

    if set(verified_reasoning) != used_reasoning:
        raise ProviderError("Verified reasoning refers to a message that was not replayed.")

    source_tools = payload.get("tools", [])
    if not isinstance(source_tools, list):
        raise ProviderError("tools must be an array.")
    tools = []
    for tool in source_tools:
        if not isinstance(tool, dict) or "input_schema" not in tool:
            name = tool.get("name", tool.get("type", "unknown")) if isinstance(tool, dict) else "unknown"
            raise ProviderError(f"Hosted tool {name!r} is not supported by this provider adapter.")
        mapped_name = _chat_name(tool.get("name"), name_map)
        if not isinstance(tool["input_schema"], dict):
            raise ProviderError("Tool input_schema must be an object.")
        function = {
            "name": mapped_name,
            "description": tool.get("description", ""),
            "parameters": copy.deepcopy(tool["input_schema"]),
        }
        tools.append({"type": "function", "function": function})

    body = {
        "model": upstream_model,
        "messages": messages,
        "max_tokens": max_tokens,
        "stream": bool(payload.get("stream", False)),
    }
    if tools:
        body["tools"] = tools
    choice = payload.get("tool_choice") or {}
    if not isinstance(choice, dict):
        raise ProviderError("tool_choice must be an object.")
    if choice:
        choice_type = choice.get("type")
        if choice_type == "tool":
            body["tool_choice"] = {
                "type": "function",
                "function": {"name": _chat_name(choice.get("name"), name_map)},
            }
        elif choice_type in {"any", "auto", "none"}:
            body["tool_choice"] = {"any": "required", "auto": "auto", "none": "none"}[choice_type]
        else:
            raise ProviderError("Unsupported tool_choice.")
        if "disable_parallel_tool_use" in choice:
            body["parallel_tool_calls"] = not bool(choice["disable_parallel_tool_use"])
    if tools and model_spec.get("parallel_tool_calls") is False:
        body["parallel_tool_calls"] = False
    for key in ("temperature", "top_p"):
        if key in payload:
            body[key] = payload[key]
    if payload.get("stop_sequences"):
        body["stop"] = copy.deepcopy(payload["stop_sequences"])
    effort = _chat_effort(provider_id, payload, model_spec, upstream_model)
    if effort is not None:
        body["reasoning_effort"] = effort
    if provider_id == "mistral":
        if payload.get("speed") == "fast" or payload.get("service_tier") in {"fast", "priority"}:
            raise ProviderError("Fast mode is not available for this Mistral connection.")
        body["prompt_cache_key"] = hashlib.sha256(json.dumps(
            {"system": payload.get("system"), "tools": payload.get("tools")},
            sort_keys=True,
        ).encode()).hexdigest()
    elif provider_id == "codex":
        tier = payload.get("service_tier")
        if payload.get("speed") == "fast" or tier in {"fast", "priority"}:
            if model_spec.get("fast_mode") is not True or not supports_fast_toggle(provider_id, upstream_model):
                raise ProviderError("OpenAI Fast mode is unavailable for this model.")
            body["service_tier"] = "fast"
        elif tier in {"default", "standard"}:
            body["service_tier"] = "default"
        elif tier not in (None, "", "auto"):
            raise ProviderError("Unsupported OpenAI service tier.")
    elif provider_id == "cerebras":
        if payload.get("speed") == "fast":
            raise ProviderError("Claude Fast mode is not a Cerebras shared-endpoint service tier.")
        service_tier = payload.get("service_tier")
        if service_tier in {"default", "auto", "flex"}:
            body["service_tier"] = service_tier
        elif service_tier not in (None, ""):
            raise ProviderError("Requested Cerebras service tier is unavailable on the shared endpoint.")
    elif provider_id == "grok":
        if model_spec.get("reasoning") is True and payload.get("stop_sequences"):
            raise ProviderError("Grok reasoning models do not support stop sequences.")
        tier = payload.get("service_tier")
        if payload.get("speed") == "fast" or tier in {"fast", "priority"}:
            body["service_tier"] = "priority"
        elif tier in (None, "", "auto", "default", "standard"):
            body["service_tier"] = "default"
        else:
            raise ProviderError("Grok supports only default or priority processing.")
        if body["stream"]:
            body["stream_options"] = {"include_usage": True}
    body["messages"] = repair_openai_tool_order(body["messages"])
    if provider_id == "mistral" and body["messages"]:
        last = body["messages"][-1]
        if isinstance(last, dict) and last.get("role") == "assistant" and not last.get("tool_calls"):
            last["prefix"] = True
    if steer_files and provider_id != "cerebras":
        # Trailing instruction: appending keeps every existing message index
        # stable, and the Mistral prefill marker above still lands on the
        # final assistant message.
        body["messages"].append({"role": "system", "content": FILE_TOOL_STEERING})
    return body, name_map


def prepare_request(
    provider_id: str,
    connection: dict | None,
    api_key: str | None,
    anthropic_payload: dict,
    upstream_model: str,
    model_spec: dict | None,
    *,
    reasoning_by_message=None,
    replay_scope=None,
    replay_key=None,
    estimate_factor: float = 1.0,
) -> dict:
    """Build a plain outbound request plan for the streaming gateway.

    The returned headers are newly constructed from the selected provider key;
    no incoming gateway credential or client identity is accepted or forwarded.
    """
    descriptor = _provider(provider_id)
    normalized_reasoning = _reasoning_map(provider_id, reasoning_by_message)
    normalized = validate_connection(provider_id, connection)
    upstream_model = _valid_model_id(upstream_model)
    model_spec = copy.deepcopy(model_spec) if isinstance(model_spec, dict) else {}
    if provider_id == "gemini":
        if not isinstance(replay_scope, str) or not replay_scope or not replay_key:
            raise ProviderError("Gemini requests require the gateway's authenticated reasoning scope.")
        try:
            return gemini_prepare_request(normalized, api_key, anthropic_payload, upstream_model, model_spec,
                                          replay_scope, replay_key, translate_chat=_translate_chat_payload)
        except GeminiError as exc:
            raise ProviderError(str(exc)) from exc
    headers = _auth_headers(provider_id, api_key, content_type=True)
    base = normalized["base_url"]
    if descriptor["protocol"] == "anthropic":
        body, normalized_system_roles, control_compatibility = _normalize_native_payload(
            provider_id, anthropic_payload, upstream_model, model_spec)
        if provider_id == "claude" and body.get("speed") == "fast":
            if not supports_fast_toggle(provider_id, upstream_model):
                raise ProviderError("Claude Fast mode is unavailable for this model.")
            headers["anthropic-beta"] = CLAUDE_FAST_BETA
        _apply_native_model_limits(body, model_spec, provider_id, estimate_factor)
        if provider_id == "claude":
            _claude_prompt_cache(body)
        if provider_id == "openrouter":
            try:
                openrouter_finalize(body, model_spec, api_key)
            except OpenRouterError as exc:
                raise ProviderError(str(exc)) from exc
        return {
            "url": base + "/v1/messages",
            "headers": headers,
            "body": body,
            "protocol": "anthropic",
            "tool_name_map": {},
            "compatibility": {
                "complete_tool_cycles": True,
                "reasoning_history": "native",
                "normalized_message_system_roles": normalized_system_roles,
                "system_role_normalization": (
                    "top_level_with_position_markers" if normalized_system_roles else "none"
                ),
                **control_compatibility,
            },
        }
    body, name_map = _translate_chat_payload(
        provider_id,
        anthropic_payload,
        upstream_model,
        model_spec,
        reasoning_by_message=normalized_reasoning, estimate_factor=estimate_factor
    )
    reasoning_replay = _requires_cerebras_replay(provider_id, model_spec)
    controls = {}
    requested = (anthropic_payload.get("output_config") or {}).get("effort")
    actual = body.get("reasoning_effort")
    if requested is not None and actual is not None and requested != actual:
        controls["reasoning_effort"] = f"{requested}_normalized_to_{actual}"
    if provider_id == "grok":
        # Stable, opaque routing hint, built locally instead of forwarding any
        # client's identity header. No credentials or prompt text leave in it.
        headers["x-grok-conv-id"] = hashlib.sha256(json.dumps({
            "model": upstream_model,
            "system": anthropic_payload.get("system"),
            "first_message": anthropic_payload["messages"][0],
        }, sort_keys=True).encode()).hexdigest()
        controls["requested_service_tier"] = body["service_tier"]
    return {
        "url": base + "/chat/completions",
        "headers": headers,
        "body": body,
        "protocol": "chat_completions",
        "tool_name_map": name_map,
        "compatibility": {
            "complete_tool_cycles": True,
            "reasoning_history": "gateway_signed_replay" if reasoning_replay else "not_required_or_unknown",
            "verified_reasoning_messages": len(normalized_reasoning),
            **controls,
        },
    }
