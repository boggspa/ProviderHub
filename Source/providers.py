"""Provider registry, model discovery, and outbound request planning.

This module deliberately has no imports from the rest of Mistral Bridge.  It is
safe for bridge_core, the gateway, and tests to import without creating a
cycle.  Network discovery is metadata-only; inference is never performed here.

The endpoint and compatibility data below is based on provider documentation
checked on 2026-09-12.  Model-list responses remain authoritative whenever a
provider exposes an account-scoped list API.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone
import hashlib
import ipaddress
import json
import math
import re
import ssl
import urllib.error
import urllib.parse
import urllib.request


class ProviderError(ValueError):
    """A provider configuration, catalogue, or request cannot be used safely."""


GATEWAY_USER_AGENT = "ProviderHub/0.4"


PROVIDERS = {
    "mistral": {
        "id": "mistral",
        "name": "Mistral",
        "protocol": "chat_completions",
        "default_base_url": "https://api.mistral.ai/v1",
        "default_region": "global",
        "regions": {"global": "https://api.mistral.ai/v1"},
        "auth_header": {"name": "Authorization", "prefix": "Bearer "},
        "credential_account": "MISTRAL_API_KEY",
        "credential_env": "MISTRAL_API_KEY",
        "setup_url": "https://console.mistral.ai/api-keys/",
        "capabilities": {
            "streaming": True,
            "tools": True,
            "thinking": True,
            "vision": True,
            "model_discovery": "api",
            "reasoning_history": "not_applicable",
        },
    },
    "kimi": {
        "id": "kimi",
        "name": "Kimi Code",
        "protocol": "anthropic",
        "default_base_url": "https://api.kimi.com/coding",
        "default_region": "global",
        "regions": {"global": "https://api.kimi.com/coding"},
        "auth_header": {"name": "x-api-key", "prefix": ""},
        "credential_account": "KIMI_CODE_API_KEY",
        "credential_env": "KIMI_CODE_API_KEY",
        "setup_url": "https://www.kimi.com/code/console",
        "capabilities": {
            "streaming": True,
            "tools": True,
            "thinking": True,
            "vision": True,
            "model_discovery": "documentation",
            "reasoning_history": "native",
        },
    },
    "mimo": {
        "id": "mimo",
        "name": "Xiaomi MiMo Token Plan",
        "protocol": "anthropic",
        "default_base_url": "https://token-plan-ams.xiaomimimo.com/anthropic",
        "default_region": "ams",
        "regions": {
            "cn": "https://token-plan-cn.xiaomimimo.com/anthropic",
            "sgp": "https://token-plan-sgp.xiaomimimo.com/anthropic",
            "ams": "https://token-plan-ams.xiaomimimo.com/anthropic",
        },
        "auth_header": {"name": "api-key", "prefix": ""},
        "credential_account": "MIMO_TOKEN_PLAN_API_KEY",
        "credential_env": "MIMO_API_KEY",
        "setup_url": "https://platform.xiaomimimo.com/",
        "capabilities": {
            "streaming": True,
            "tools": True,
            "thinking": True,
            "vision": True,
            "model_discovery": "documentation",
            "reasoning_history": "native",
        },
    },
    "ollama": {
        "id": "ollama",
        "name": "Ollama",
        "protocol": "anthropic",
        "default_base_url": "http://127.0.0.1:11434",
        "default_region": "local",
        "regions": {"local": "http://127.0.0.1:11434"},
        "auth_header": {"name": "x-api-key", "prefix": ""},
        "credential_account": None,
        "credential_env": None,
        "setup_url": "https://docs.ollama.com/api/anthropic-compatibility",
        "capabilities": {
            "streaming": True,
            "tools": True,
            "thinking": True,
            "vision": True,
            "model_discovery": "daemon",
            "model_capabilities": "model_dependent",
            "reasoning_history": "native",
        },
    },
    "deepseek": {
        "id": "deepseek",
        "name": "DeepSeek",
        "protocol": "anthropic",
        "default_base_url": "https://api.deepseek.com/anthropic",
        "default_region": "global",
        "regions": {"global": "https://api.deepseek.com/anthropic"},
        "auth_header": {"name": "x-api-key", "prefix": ""},
        "credential_account": "DEEPSEEK_API_KEY",
        "credential_env": "DEEPSEEK_API_KEY",
        "setup_url": "https://platform.deepseek.com/api_keys",
        "capabilities": {
            "streaming": True,
            "tools": True,
            "thinking": True,
            "vision": None,
            "model_discovery": "api",
            "reasoning_history": "native",
        },
    },
    "cerebras": {
        "id": "cerebras",
        "name": "Cerebras",
        "protocol": "chat_completions",
        "default_base_url": "https://api.cerebras.ai/v1",
        "default_region": "global",
        "regions": {"global": "https://api.cerebras.ai/v1"},
        "auth_header": {"name": "Authorization", "prefix": "Bearer "},
        "credential_account": "CEREBRAS_API_KEY",
        "credential_env": "CEREBRAS_API_KEY",
        "setup_url": "https://cloud.cerebras.ai/",
        "capabilities": {
            "streaming": True,
            "tools": True,
            "thinking": True,
            "vision": "model_dependent",
            "model_discovery": "api",
            "reasoning_history": "gateway_signed_replay",
        },
    },
    # Ordinary Meta Model API/PAYG access. Muse Code login and its onboarding
    # key belong to the separate Muse session host and are never read here.
    "muse": {
        "id": "muse",
        "name": "Muse (Meta Model API)",
        "protocol": "anthropic",
        "default_base_url": "https://api.meta.ai",
        "default_region": "global",
        "regions": {"global": "https://api.meta.ai"},
        "auth_header": {"name": "Authorization", "prefix": "Bearer "},
        "credential_account": "MODEL_API_KEY",
        "credential_env": "MODEL_API_KEY",
        "setup_url": "https://dev.meta.ai/",
        "capabilities": {
            "streaming": True,
            "tools": True,
            "thinking": True,
            "vision": True,
            "model_discovery": "api",
            "reasoning_history": "native",
        },
    },
    "grok": {
        "id": "grok",
        "name": "Grok (xAI API)",
        "protocol": "chat_completions",
        "default_base_url": "https://api.x.ai/v1",
        "default_region": "global",
        "regions": {"global": "https://api.x.ai/v1"},
        "auth_header": {"name": "Authorization", "prefix": "Bearer "},
        "credential_account": "XAI_API_KEY",
        "credential_env": "XAI_API_KEY",
        "setup_url": "https://console.x.ai/",
        "capabilities": {
            "streaming": True,
            "tools": True,
            "thinking": True,
            "vision": "model_dependent",
            "model_discovery": "api",
            "reasoning_history": "not_required",
        },
    },
}


# Canonical paths accepted from settings.  A user may paste a provider base URL
# or its documented request endpoint; both normalize to the same safe base.
from qwen_provider import DESCRIPTOR as QWEN_DESCRIPTOR, OFFICIAL_PATHS as QWEN_PATHS, QwenError, catalogue as qwen_catalogue, normalize_controls as qwen_controls

PROVIDERS[QWEN_DESCRIPTOR["id"]] = QWEN_DESCRIPTOR

_OFFICIAL_PATHS = {
    "mistral": {"", "/v1", "/v1/models", "/v1/chat/completions"},
    "kimi": {"", "/coding", "/coding/v1/messages", "/coding/v1/chat/completions"},
    "mimo": {"", "/anthropic", "/anthropic/v1/messages"},
    "deepseek": {"", "/anthropic", "/anthropic/v1/messages"},
    "cerebras": {"", "/v1", "/v1/models", "/v1/chat/completions"},
    "muse": {"", "/v1", "/v1/models", "/v1/messages"},
    "grok": {"", "/v1", "/v1/models", "/v1/language-models", "/v1/chat/completions"},
    "qwen-token-plan": QWEN_PATHS,
}

_OLLAMA_PATHS = {"", "/v1", "/v1/messages", "/api/tags"}
_MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/+\-]{0,199}\Z")
_FUNCTION_NAME = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
_LOCAL_CREDENTIAL_FIELDS = {
    "api_key", "api-key", "x-api-key", "authorization", "gateway_token", "gateway-token",
}

_KIMI_DOCS = "https://www.kimi.com/code/docs/en/kimi-code/models.html"
_KIMI_MODELS = [
    {
        "id": "k3",
        "display_name": "K3",
        # Moderato receives 262144; Allegretto and above receive 1048576.
        "context": None,
        "context_options": [262144, 1048576],
        "tools": True,
        "vision": True,
        "reasoning": True,
        "effort_modes": ["low", "high", "max"],
        "fast_mode": False,
    },
    {
        "id": "k3-256k",
        "display_name": "K3 256K",
        "context": 262144,
        "tools": True,
        "vision": True,
        "reasoning": True,
        "effort_modes": ["low", "high", "max"],
        "fast_mode": False,
    },
    {
        "id": "kimi-for-coding",
        "display_name": "Kimi for Coding",
        "context": 1048576,
        "tools": True,
        "vision": True,
        "reasoning": True,
        "effort_modes": ["low", "high", "max"],
        "fast_mode": False,
    },
    {
        "id": "kimi-for-coding-highspeed",
        "display_name": "Kimi for Coding HighSpeed",
        "context": 262144,
        "tools": True,
        "vision": True,
        "reasoning": True,
        "effort_modes": [],
        # HighSpeed is a distinct model/tier selected by this exact id.  It is
        # not evidence for Claude's same-model Fast request control.
        "fast_mode": False,
        "speed_tier": "highspeed",
    },
]

_MIMO_DOCS = "https://mimo.mi.com/docs/en-US/api/chat/anthropic-api"
_MIMO_MODELS = [
    {
        "id": "mimo-v2.5-pro",
        "display_name": "MiMo V2.5 Pro",
        "context": 1048576,
        "max_output": 131072,
        "tools": True,
        "vision": False,
        "reasoning": True,
        "effort_modes": ["none", "high"],
        "fast_mode": False,
    },
    {
        "id": "mimo-v2.5",
        "display_name": "MiMo V2.5",
        "context": 1048576,
        "max_output": 131072,
        "tools": True,
        "vision": True,
        "reasoning": True,
        "effort_modes": ["none", "high"],
        "fast_mode": False,
    },
]

_DEEPSEEK_DOCS = "https://api-docs.deepseek.com/quick_start/pricing/"
_DEEPSEEK_MODEL_METADATA = {
    "deepseek-flash": {
        "tools": True,
        "vision": True,
        "reasoning": True,
        "effort_modes": ["none", "low", "high", "max"],
    },
    "deepseek-v4-pro": {
        "tools": True,
        "vision": False,
        "reasoning": True,
        "effort_modes": ["none", "low", "high", "max"],
    },
}

_MUSE_COOKBOOK = "https://github.com/meta-models/meta-model-cookbook"
_GROK_MODEL_DOCS = "https://docs.x.ai/developers/grok-4-6"
_GROK_REASONING_DOCS = "https://docs.x.ai/developers/model-capabilities/text/reasoning"
# Only enrich exact, account-listed IDs. A moving alias or a future model is
# not evidence for a particular version, context limit, or reasoning control.
_GROK_MODEL_METADATA = {
    "grok-4.6": {
        "context": 500000, "tools": True, "vision": True, "reasoning": True,
        "effort_modes": ["low", "medium", "high", "xhigh"],
        "metadata_evidence": _GROK_MODEL_DOCS,
    },
    "grok-4.5": {
        "reasoning": True, "effort_modes": ["low", "medium", "high"],
        "metadata_evidence": _GROK_REASONING_DOCS,
    },
}


_MUSE_MODEL_METADATA = {
    "muse-spark-1.3": {
        "display_name": "Muse Spark 1.3",
        "context": 1048576,
        "max_output": 131072,
        "tools": True,
        "vision": True,
        "reasoning": True,
        "streaming": True,
        "adaptive_thinking": True,
        "effort_modes": ["minimal", "low", "medium", "high"],
        "fast_mode": False,
        "parallel_tool_calls": True,
        "tool_choice_modes": ["auto"],
        "reasoning_history": "native",
        "metadata_evidence": _MUSE_COOKBOOK,
    },
}

# Meta's general /v1/models list also returns these exact image-generation and
# transcription IDs without capability fields. They have separate endpoints,
# not the Messages/tool contract used by either coding harness. See the
# first-party cookbook's 05_muse_image and 06_muse_voice sections.
_MUSE_NON_CHAT_MODELS = {"muse-image-1.0", "muse-voice-transcribe-1.0"}

# The list API does not return capability or context metadata.  Only exact IDs
# covered by current Cerebras documentation receive these additions.  Every
# Reasoning entries require authenticated replay through cerebras_replay.  The
# provider module accepts only the validator's out-of-band index -> reasoning
# map; it never trusts client-supplied annotations.
_CEREBRAS_PUBLIC_DOCS = "https://inference-docs.cerebras.ai/api-reference/models/public-models"
_CEREBRAS_PUBLIC_MODELS_URL = "https://api.cerebras.ai/public/v1/models"
_CEREBRAS_MODEL_METADATA = {
    "gpt-oss-120b": {
        "context": 131072,
        "max_output": 40960,
        "tools": True,
        "vision": False,
        "reasoning": True,
        "effort_modes": ["low", "medium", "high"],
        "evidence": _CEREBRAS_PUBLIC_DOCS,
    },
    "gemma-4-31b": {"reasoning": True, "effort_modes": ["none", "low", "medium", "high"]},
    "qwen-3.8-27b": {"reasoning": True, "effort_modes": ["none", "low", "medium", "high"]},
    "kimi-k2.7-code": {"reasoning": True, "effort_modes": []},
    "zai-glm-4.7": {"reasoning": True, "effort_modes": ["none"]},
}


def provider_defaults() -> dict:
    """Return fresh, credential-free connection settings for every provider."""
    return {
        provider_id: {
            "region": descriptor["default_region"],
            "base_url": descriptor["default_base_url"],
        }
        for provider_id, descriptor in PROVIDERS.items()
    }


def _provider(provider_id: str) -> dict:
    try:
        return PROVIDERS[provider_id]
    except (KeyError, TypeError) as exc:
        raise ProviderError(f"Unknown provider: {provider_id!r}.") from exc


def _split_url(value: str) -> urllib.parse.SplitResult:
    if not isinstance(value, str) or not value.strip():
        raise ProviderError("Provider base URL must be a non-empty URL.")
    try:
        parsed = urllib.parse.urlsplit(value.strip())
        # Accessing port validates malformed or out-of-range ports.
        parsed.port
    except ValueError as exc:
        raise ProviderError("Provider base URL has an invalid port.") from exc
    if parsed.username is not None or parsed.password is not None:
        raise ProviderError("Provider base URL cannot contain credentials.")
    if parsed.query or parsed.fragment:
        raise ProviderError("Provider base URL cannot contain a query or fragment.")
    return parsed


def _validate_ollama_base(value: str) -> str:
    parsed = _split_url(value)
    if parsed.scheme.lower() != "http":
        raise ProviderError("Ollama must use its local HTTP daemon.")
    hostname = (parsed.hostname or "").lower().rstrip(".")
    if hostname == "localhost":
        address = ipaddress.ip_address("127.0.0.1")
    else:
        try:
            address = ipaddress.ip_address(hostname)
        except ValueError as exc:
            raise ProviderError("Ollama must resolve to a literal loopback address.") from exc
        if not address.is_loopback:
            raise ProviderError("Ollama must use a loopback address.")
    path = parsed.path.rstrip("/")
    if path not in _OLLAMA_PATHS:
        raise ProviderError("Ollama base URL has an unsupported path.")
    port = parsed.port or 11434
    host = f"[{address.compressed}]" if address.version == 6 else address.compressed
    return f"http://{host}:{port}"


def _official_base(provider_id: str, value: str) -> tuple[str, str]:
    parsed = _split_url(value)
    if parsed.scheme.lower() != "https" or parsed.port not in (None, 443):
        raise ProviderError("Hosted providers require HTTPS on the official endpoint.")
    hostname = (parsed.hostname or "").lower().rstrip(".")
    path = parsed.path.rstrip("/")
    if path not in _OFFICIAL_PATHS[provider_id]:
        raise ProviderError("Provider base URL has an unsupported path.")
    for region_id, region_base in PROVIDERS[provider_id]["regions"].items():
        expected = urllib.parse.urlsplit(region_base)
        if hostname == expected.hostname:
            return region_id, region_base
    raise ProviderError("Provider base URL must use an official provider domain.")


def validate_connection(provider_id: str, connection: dict | None) -> dict:
    """Normalize routing fields and reject non-official or non-local targets.

    Credentials are intentionally not accepted in connection dictionaries.
    Callers may keep non-routing metadata such as ``credential_mode`` beside
    this returned dictionary.
    """
    descriptor = _provider(provider_id)
    if connection is None:
        connection = {}
    if not isinstance(connection, dict):
        raise ProviderError("Provider connection must be an object.")
    if any(str(key).lower() in _LOCAL_CREDENTIAL_FIELDS for key in connection):
        raise ProviderError("Provider credentials cannot be stored in connection settings.")

    requested_region = connection.get("region")
    if requested_region is not None and not isinstance(requested_region, str):
        raise ProviderError("Provider region must be a string.")
    known_regions = descriptor["regions"]
    region_id = requested_region or descriptor["default_region"]
    if region_id not in known_regions:
        raise ProviderError(f"Unknown region for {descriptor['name']}.")

    value = connection.get("base_url")
    if value is None or value == "":
        value = known_regions[region_id]
    if provider_id == "ollama":
        normalized_base = _validate_ollama_base(value)
        derived_region = "local"
    else:
        derived_region, normalized_base = _official_base(provider_id, value)
    if requested_region is not None and requested_region != derived_region:
        raise ProviderError("Provider region does not match its base URL.")
    return {"region": derived_region, "base_url": normalized_base}


def _auth_headers(provider_id: str, api_key: str | None, *, content_type: bool) -> dict:
    descriptor = _provider(provider_id)
    # Identify the actual integration on metadata and inference requests.
    # Cerebras rejects urllib's default Python user-agent at its edge.
    headers = {"Accept": "application/json", "User-Agent": GATEWAY_USER_AGENT}
    if content_type:
        headers["Content-Type"] = "application/json"
    if provider_id == "ollama":
        key = api_key.strip() if isinstance(api_key, str) and api_key.strip() else "ollama"
    else:
        if not isinstance(api_key, str) or not api_key.strip():
            raise ProviderError(f"An API key is required for {descriptor['name']}.")
        key = api_key.strip()
    if "\r" in key or "\n" in key:
        raise ProviderError("Provider API key contains invalid characters.")
    auth = descriptor["auth_header"]
    headers[auth["name"]] = auth["prefix"] + key
    if descriptor["protocol"] == "anthropic":
        headers["anthropic-version"] = "2023-06-01"
    return headers


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
    return None


def _discovery_plan(provider_id: str, connection: dict, api_key: str | None) -> dict:
    base = connection["base_url"]
    if provider_id in {"mistral", "cerebras"}:
        url = base + "/models"
    elif provider_id == "muse":
        url = base + "/v1/models"
    elif provider_id == "grok":
        url = base + "/language-models"
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
        models.append(_catalogue_entry(
            identifier,
            display_name=card.get("name") if isinstance(card.get("name"), str) else identifier,
            context=context,
            tools=("tools" in capabilities) if capabilities is not None else None,
            vision=("vision" in capabilities) if capabilities is not None else None,
            reasoning=("thinking" in capabilities) if capabilities is not None else None,
            effort_modes=[],
            fast_mode=False,
            source="ollama_daemon",
            evidence=show_plan["url"],
            details=copy.deepcopy(details) if isinstance(details, dict) else {},
            context_kind="model_declared_maximum" if context is not None else "unknown",
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
        public_limits.get("max_completion_tokens"),
        public_card.get("max_completion_tokens"),
        public_card.get("max_output_length"),
        public_card.get("max_output_tokens"),
    )
    context = reported_context or _positive_integer(metadata.get("context"))
    max_output = reported_output or _positive_integer(metadata.get("max_output"))
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
        vision = _boolean(public_capabilities.get("vision"))
    if vision is None:
        vision = _boolean(metadata.get("vision"))
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
        tools=tools,
        vision=vision,
        reasoning=reasoning,
        effort_modes=effort_modes,
        fast_mode=False,
        source="provider_api",
        evidence=source_evidence,
        max_output=max_output,
        context_kind=("provider_reported" if reported_context is not None else
                      "verified_documentation" if context is not None else "unknown"),
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
                effort_modes=["none", "high"] if capabilities.get("reasoning") is True else [],
                fast_mode=False,
                source="provider_api",
                evidence=evidence,
                deprecation=card.get("deprecation"),
                billing_model_name=(card.get("billing_model_name")
                                    if isinstance(card.get("billing_model_name"), str) else None),
            ))
        elif provider_id == "deepseek":
            metadata = _DEEPSEEK_MODEL_METADATA.get(identifier, {})
            models.append(_catalogue_entry(
                identifier,
                context=None,
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
    if provider_id == "mistral":
        return _coalesce_mistral_models(models)
    return sorted(models, key=lambda model: (model["display_name"].casefold(), model["id"]))


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
                "Exact Muse Spark 1.3 limits and capabilities were enriched from Meta's first-party Model API cookbook."
            )
        if any(model.get("context") is None for model in models):
            warnings.append(
                "At least one Meta Model API model did not report an exact context limit; it remains provider-managed."
            )
    if provider_id == "grok":
        warnings.append("Grok uses xAI API billing. Fast requests Priority processing at a premium token price; the actual tier is recorded when returned.")
        if any(model.get("context") is None for model in models):
            warnings.append("At least one Grok model has no exact reported context limit; it remains provider-managed.")
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
    if model_spec.get("reasoning") is False and thinking_type not in (None, "disabled"):
        raise ProviderError("The selected model is documented as not supporting thinking.")
    if (model_spec.get("reasoning") is False
            and requested not in (None, "none")):
        raise ProviderError("The selected model is documented as not supporting reasoning effort.")

    if provider_id == "qwen-token-plan":
        try:
            return {**compatibility, **qwen_controls(body, upstream_model, model_spec)}
        except QwenError as exc:
            raise ProviderError(str(exc)) from exc

    if provider_id == "muse":
        if thinking_type == "disabled" or requested == "none":
            raise ProviderError(
                "Muse Spark reasoning cannot be disabled reliably on the public Meta Model API.")
        if thinking_type not in (None, "adaptive", "enabled"):
            raise ProviderError("Muse supports adaptive or enabled thinking for this route.")
        if requested is not None:
            aliases = {
                "minimal": "minimal", "low": "low", "medium": "medium", "high": "high",
                "xhigh": "high", "max": "high", "ultra": "high",
            }
            normalized = aliases.get(requested)
            supported = set(model_spec.get("effort_modes") or [])
            # Missing per-model metadata is not evidence of incompatibility.
            # Forward a documented Meta API effort for the provider to validate;
            # constrain it locally only when an explicit supported set exists.
            if normalized is None or (supported and normalized not in supported):
                raise ProviderError(f"Muse model does not support reasoning effort {requested!r}.")
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
            if normalized is None or normalized not in supported:
                raise ProviderError(
                    f"Kimi model does not support reasoning effort {requested!r}.")
            _write_thinking_type(body, "enabled", descriptor["name"])
            _write_output_effort(body, normalized)
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
            elif requested in {"minimal", "low", "medium", "high", "xhigh", "max", "ultra"}:
                target = "enabled"
            else:
                raise ProviderError(f"MiMo does not support reasoning effort {requested!r}.")
            _write_thinking_type(body, target, descriptor["name"])
            # MiMo's Anthropic surface documents thinking.type, not
            # output_config.effort. Preserve any other output_config fields.
            _write_output_effort(body, None)
        return compatibility

    if provider_id == "deepseek":
        if requested is not None:
            aliases = {
                "none": "none", "minimal": "low", "low": "low",
                "medium": "high", "high": "high", "xhigh": "high", "max": "max",
            }
            normalized = aliases.get(requested)
            supported = set(model_spec.get("effort_modes") or [])
            if normalized is None or normalized not in supported:
                raise ProviderError(
                    f"DeepSeek model does not support reasoning effort {requested!r}.")
            _write_thinking_type(
                body, "disabled" if normalized == "none" else "enabled", descriptor["name"])
            _write_output_effort(body, normalized)
        return compatibility

    if provider_id == "ollama":
        if thinking_type not in (None, "enabled", "disabled"):
            raise ProviderError("Ollama supports only enabled or disabled thinking.")
        if thinking_type == "enabled" and model_spec.get("reasoning") is not True:
            raise ProviderError("The Ollama model does not advertise thinking support.")
        if requested is not None:
            if requested == "none":
                target = "disabled"
            elif requested in {"minimal", "low", "medium", "high", "xhigh", "max", "ultra"}:
                if model_spec.get("reasoning") is not True:
                    raise ProviderError("The Ollama model does not advertise thinking support.")
                target = "enabled"
            else:
                raise ProviderError(f"Ollama does not support reasoning effort {requested!r}.")
            _write_thinking_type(body, target, descriptor["name"])
            # Ollama documents the Anthropic thinking switch, but its daemon
            # does not advertise distinct effort levels. Preserve any other
            # output_config fields while translating effort to that switch.
            _write_output_effort(body, None)
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


def _apply_native_model_limits(body: dict, model_spec: dict) -> None:
    max_tokens = body.get("max_tokens", 4096)
    if type(max_tokens) is not int or max_tokens <= 0:
        raise ProviderError("max_tokens must be a positive integer.")
    max_output = model_spec.get("max_output")
    if type(max_output) is int and max_output > 0:
        max_tokens = min(max_tokens, max_output)
    context = model_spec.get("context")
    if type(context) is int and context > 0:
        estimate = _estimated_input_tokens(body)
        if estimate >= context:
            raise ProviderError(f"Request exceeds the model's reported {context}-token context limit.")
        max_tokens = min(max_tokens, context - estimate)
    body["max_tokens"] = max_tokens
    if model_spec.get("vision") is False and _contains_content_type(body.get("messages"), {"image"}):
        raise ProviderError("The selected model is documented as text-only.")
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
        if model_spec.get("vision") is False:
            raise ProviderError("The selected model is documented as text-only.")
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


def _chat_effort(provider_id: str, payload: dict, model_spec: dict):
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
        if target == "xhigh" and "xhigh" not in supported and "high" in supported:
            target = "high"
        if target not in supported:
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
        if disabled or requested in {"none", "minimal", "low"}:
            return "none"
        if requested in {None, "medium", "high", "xhigh", "max"}:
            return "high"
        raise ProviderError("Unsupported Mistral reasoning effort.")
    if provider_id == "cerebras":
        supported = model_spec.get("effort_modes") or []
        if disabled:
            requested = "none"
        if requested is None:
            return None
        if requested not in supported:
            if not supported:
                raise ProviderError("Cerebras reasoning effort controls are not known for this model.")
            raise ProviderError(f"Cerebras model does not support reasoning effort {requested!r}.")
        return requested
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
        estimate = _estimated_input_tokens(payload)
        if estimate >= context:
            raise ProviderError(f"Request exceeds the model's reported {context}-token context limit.")
        max_tokens = min(max_tokens, context - estimate)

    messages = []
    if payload.get("system") not in (None, ""):
        blocks = _content_blocks(payload["system"], system=True)
        messages.append({"role": "system", "content": "\n".join(block.get("text", "") for block in blocks)})
    name_map = {}
    id_map = {}
    for source_index, message in enumerate(source_messages):
        if not isinstance(message, dict):
            raise ProviderError("Every message must be an object.")
        role = message.get("role")
        if role in {"system", "developer"}:
            blocks = _content_blocks(message.get("content", ""), system=True)
            output_role = "developer" if provider_id == "cerebras" and role == "developer" else "system"
            messages.append({"role": output_role, "content": "\n".join(block.get("text", "") for block in blocks)})
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
    effort = _chat_effort(provider_id, payload, model_spec)
    if effort is not None:
        body["reasoning_effort"] = effort
    if provider_id == "mistral":
        if payload.get("speed") == "fast" or payload.get("service_tier") in {"fast", "priority"}:
            raise ProviderError("Fast mode is not available for this Mistral connection.")
        body["prompt_cache_key"] = hashlib.sha256(json.dumps(
            {"system": payload.get("system"), "tools": payload.get("tools")},
            sort_keys=True,
        ).encode()).hexdigest()
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
    headers = _auth_headers(provider_id, api_key, content_type=True)
    base = normalized["base_url"]
    if descriptor["protocol"] == "anthropic":
        body, normalized_system_roles, control_compatibility = _normalize_native_payload(
            provider_id, anthropic_payload, upstream_model, model_spec)
        _apply_native_model_limits(body, model_spec)
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
        reasoning_by_message=normalized_reasoning,
    )
    reasoning_replay = _requires_cerebras_replay(provider_id, model_spec)
    controls = {}
    if provider_id == "grok":
        # Stable, opaque routing hint, built locally instead of forwarding any
        # client's identity header. No credentials or prompt text leave in it.
        headers["x-grok-conv-id"] = hashlib.sha256(json.dumps({
            "model": upstream_model,
            "system": anthropic_payload.get("system"),
            "first_message": anthropic_payload["messages"][0],
        }, sort_keys=True).encode()).hexdigest()
        requested = (anthropic_payload.get("output_config") or {}).get("effort")
        actual = body.get("reasoning_effort")
        if requested is not None and requested != actual:
            controls["reasoning_effort"] = f"{requested}_normalized_to_{actual}"
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
