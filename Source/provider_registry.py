"""Provider registry, connection validation, and authentication helpers.

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


from chat_tool_order import repair_openai_tool_order
GATEWAY_USER_AGENT = "ProviderHub/0.5"


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
        "reasoning_store_cap": 16384,
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
        "reasoning_store_cap": 16384,
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
    # Ordinary Anthropic API access. The Claude Code CLI login belongs to the
    # separate "cli" credential mode and is never read here.
    "claude": {
        "id": "claude",
        "name": "Claude (Anthropic API)",
        "protocol": "anthropic",
        "default_base_url": "https://api.anthropic.com",
        "default_region": "global",
        "regions": {"global": "https://api.anthropic.com"},
        "auth_header": {"name": "x-api-key", "prefix": ""},
        "credential_account": "ANTHROPIC_API_KEY",
        "credential_env": "ANTHROPIC_API_KEY",
        "setup_url": "https://console.anthropic.com/settings/keys",
        "capabilities": {
            "streaming": True,
            "tools": True,
            "thinking": True,
            "vision": True,
            "model_discovery": "api",
            "reasoning_history": "native",
        },
    },
    # Ordinary OpenAI API access. The ChatGPT subscription login the Codex CLI
    # holds belongs to the separate "cli" credential mode and is never read here.
    "codex": {
        "id": "codex",
        "name": "Codex (OpenAI API)",
        "protocol": "chat_completions",
        "default_base_url": "https://api.openai.com/v1",
        "default_region": "global",
        "regions": {"global": "https://api.openai.com/v1"},
        "auth_header": {"name": "Authorization", "prefix": "Bearer "},
        "credential_account": "OPENAI_API_KEY",
        "credential_env": "OPENAI_API_KEY",
        "setup_url": "https://platform.openai.com/api-keys",
        "capabilities": {
            "streaming": True,
            "tools": True,
            "thinking": True,
            "vision": "model_dependent",
            "model_discovery": "api",
            "reasoning_history": "not_required",
        },
    },
    # AntiGravity has no hosted model API of its own: the installed `agy`
    # binary fronting its own login is the whole transport. The descriptor
    # exists to carry that CLI credential mode; routing fields are inert
    # placeholders, and validate_connection special-cases the id.
    "antigravity": {
        "id": "antigravity",
        "name": "AntiGravity",
        "protocol": "cli",
        "default_base_url": "",
        "default_region": "local",
        "regions": {"local": ""},
        "auth_header": {"name": "", "prefix": ""},
        "credential_account": None,
        "credential_env": None,
        "setup_url": "https://antigravity.google/",
        "capabilities": {
            "streaming": True,
            "tools": False,
            "thinking": True,
            "vision": False,
            "model_discovery": "cli",
            "reasoning_history": "not_applicable",
        },
    },
}


# Canonical paths accepted from settings.  A user may paste a provider base URL
# or its documented request endpoint; both normalize to the same safe base.
from qwen_provider import DESCRIPTOR as QWEN_DESCRIPTOR, OFFICIAL_PATHS as QWEN_PATHS, QwenError, catalogue as qwen_catalogue, normalize_controls as qwen_controls
from openrouter_provider import DESCRIPTOR as OPENROUTER_DESCRIPTOR, OFFICIAL_PATHS as OPENROUTER_PATHS, OpenRouterError, discover as openrouter_discover, finalize as openrouter_finalize, normalize_messages as openrouter_controls, app_headers as openrouter_app_headers
from gemini_provider import DESCRIPTOR as GEMINI_DESCRIPTOR, OFFICIAL_PATHS as GEMINI_PATHS, GeminiError, discover as gemini_discover, prepare_request as gemini_prepare_request, validate_connection as gemini_validate_connection
from devin_agent import DESCRIPTOR as DEVIN_DESCRIPTOR, OFFICIAL_PATHS as DEVIN_PATHS, DevinAgentError, catalogue as devin_catalogue, validate_connection as devin_validate_connection
from effort_map import EFFORT_ORDER, CEREBRAS_EFFORT_ALIASES, DEEPSEEK_EFFORT_ALIASES, MISTRAL_EFFORT_ALIASES, MISTRAL_REASONING_EFFORTS, cap_high_end, map_effort, mistral_effort_modes, nearest_effort, ollama_effort_aliases, ollama_effort_modes

PROVIDERS[QWEN_DESCRIPTOR["id"]] = QWEN_DESCRIPTOR
PROVIDERS[OPENROUTER_DESCRIPTOR["id"]] = OPENROUTER_DESCRIPTOR
PROVIDERS[GEMINI_DESCRIPTOR["id"]] = GEMINI_DESCRIPTOR
PROVIDERS[DEVIN_DESCRIPTOR["id"]] = DEVIN_DESCRIPTOR

_OFFICIAL_PATHS = {
    "mistral": {"", "/v1", "/v1/models", "/v1/chat/completions"},
    "kimi": {"", "/coding", "/coding/v1/messages", "/coding/v1/chat/completions"},
    "mimo": {"", "/anthropic", "/anthropic/v1/messages"},
    "deepseek": {"", "/anthropic", "/anthropic/v1/messages"},
    "cerebras": {"", "/v1", "/v1/models", "/v1/chat/completions"},
    "muse": {"", "/v1", "/v1/models", "/v1/messages"},
    "grok": {"", "/v1", "/v1/models", "/v1/language-models", "/v1/chat/completions"},
    "claude": {"", "/v1", "/v1/models", "/v1/messages"},
    "codex": {"", "/v1", "/v1/models", "/v1/chat/completions", "/v1/responses"},
    "qwen-token-plan": QWEN_PATHS,
    "openrouter": OPENROUTER_PATHS,
    "gemini": GEMINI_PATHS,
    "devin": DEVIN_PATHS,
}

_OLLAMA_PATHS = {"", "/v1", "/v1/messages", "/api/tags"}
_MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/+\-]{0,199}\Z")
_FUNCTION_NAME = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
_LOCAL_CREDENTIAL_FIELDS = {
    "api_key", "api-key", "x-api-key", "authorization", "gateway_token", "gateway-token",
}

# Claude Desktop renders its per-turn file rows and +N/-N diff chips only
# for the Edit, Write, MultiEdit, and NotebookEdit tool uses; file changes
# made through Bash heredocs never appear as close-out cards. When the
# harness offers both, steer the model toward the tracked file tools.
FILE_EDIT_TOOLS = frozenset({"Edit", "Write", "MultiEdit", "NotebookEdit"})
FILE_TOOL_STEERING = (
    "When editing files in the workspace, use the Edit, Write, MultiEdit, or NotebookEdit "
    "tools instead of shell redirection, heredocs, or other Bash file writes, "
    "so every file change is tracked per file."
)


def _needs_file_tool_steering(source_tools) -> bool:
    if not isinstance(source_tools, list) or not source_tools:
        return False
    names = {tool.get("name") for tool in source_tools if isinstance(tool, dict)}
    return bool(names & FILE_EDIT_TOOLS) and "Bash" in names

_KIMI_DOCS = "https://www.kimi.com/code/docs/en/kimi-code/models.html"
# K2.7 Code HighSpeed is documented as "Thinking: ON" with no reasoning_effort
# ladder, and it is not one of the routes ("the K3 series and K2.8 Preview")
# that Kimi serves as K2.8 Preview when thinking is off. Neither a desktop
# effort rank nor a thinking-off request selects anything on this route.
# Its catalogue ladder below is a placeholder slider position, not a control.
_KIMI_FIXED_THINKING_MODELS = frozenset({"kimi-for-coding-highspeed"})
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
        # Kimi documents "Thinking: ON" and no reasoning_effort ladder here.
        # "high" is a deliberate placeholder slider position, not a wire
        # control: an empty ladder left Codex with no position and it
        # persisted effort "none"; one base rank also lets the synthesized
        # Ultra alias carry the multi-agent affordance on this fast route.
        # The gateway drops any rank sent here (_KIMI_FIXED_THINKING_MODELS).
        "effort_modes": ["high"],
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
#: Where a model's publisher documents a context window, it supersedes what
#: the local tag declares. An Ollama tag reports the GGUF's rope ceiling,
#: which is an upper bound on what the weights can address rather than the
#: window the publisher serves - Ollama's own pages show the split, with the
#: cloud tag of Devstral Small 2 reading 256K while the local tag of the same
#: model claims 384K. Keyed by family (the part before the tag), which holds
#: for these three because each is a single model; a family whose tags really
#: do differ would need per-tag keys.
_OLLAMA_PUBLISHED_CONTEXT = {
    "north-mini-code-1.0": (262144, "https://docs.cohere.com/docs/models"),
    "devstral-small-2": (262144, "https://docs.mistral.ai/models/devstral-small-2-25-12"),
    # Ollama's page reads "512K", which is 524288 under its own /1024 display;
    # the 512000 stored here was that figure mis-transcribed, and it collides
    # with MiniMax's documented maximum OUTPUT.
    "minimax-m3": (524288, "https://platform.minimax.io/docs/guides/text-generation"),
}


def _published_ollama_context(identifier: str, declared):
    """The publisher's window for an Ollama tag, where one is documented."""
    published = _OLLAMA_PUBLISHED_CONTEXT.get(identifier.split(":", 1)[0].casefold())
    if published is None:
        return declared, None
    return published


_DEEPSEEK_MODEL_METADATA = {
    "deepseek-flash": {
        # DeepSeek's own config.json, max_position_embeddings. The Ollama
        # route to the same weights already asserted this; the direct route
        # asserted nothing, so one model answered two different windows.
        "context": 1048576,
        "context_evidence": "https://huggingface.co/deepseek-ai/DeepSeek-V4.1-Flash/blob/dba1be0a40aa45a94ad051997016db3960a90277/config.json",
        "max_output": 393216,
        "tools": True,
        "vision": True,
        "reasoning": True,
        "effort_modes": ["none", "low", "high", "max"],
    },
    "deepseek-v4-pro": {
        "context": 1048576,
        "context_evidence": "https://huggingface.co/deepseek-ai/DeepSeek-V4-Pro/blob/b5968e9190ef611bbf34a7229255be88a0e937c1/config.json",
        "tools": True,
        "vision": False,
        "reasoning": True,
        "effort_modes": ["none", "low", "high", "max"],
    },
}

_MUSE_COOKBOOK = "https://github.com/meta-models/meta-model-cookbook"
_MUSE_REASONING_DOCS = "https://dev.meta.ai/docs/reasoning.md"
# First-party Spark 1.3 ranks from the current reasoning page. `none` is omitted
# because Meta documents HTTP 400 when reasoning is turned off on Spark.
_MUSE_EFFORT_RANKS = ["minimal", "low", "medium", "high", "xhigh", "max"]
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
        "context": 500000,
        "reasoning": True, "effort_modes": ["low", "medium", "high"],
        "metadata_evidence": "https://docs.x.ai/developers/models/grok-4.5",
    },
}


_CLAUDE_CONTEXT_DOCS = "https://platform.claude.com/docs/en/build-with-claude/context-windows"
# Exact published model ceilings, checked 2026-09-19. These are model
# capacities, not a claim that a particular subscription grants access.
_CLAUDE_CONTEXT_WINDOWS = {
    "claude-fable-5-1": 1000000,
    "claude-fable-5": 1000000,
    "claude-opus-5": 1000000,
    "claude-opus-4-8": 1000000,
    "claude-opus-4-7": 1000000,
    "claude-opus-4-6": 1000000,
    "claude-sonnet-5": 1000000,
    "claude-sonnet-4-6": 1000000,
    "claude-haiku-4-5": 200000,
    "claude-haiku-4-5-20251001": 200000,
}


def documented_context(provider_id: str, identifier: str) -> tuple[int | None, str | None]:
    """Published ceiling for an exact first-party route; never guess aliases."""
    if provider_id == "claude":
        context = _CLAUDE_CONTEXT_WINDOWS.get(identifier)
        return context, _CLAUDE_CONTEXT_DOCS if context else None
    if provider_id == "deepseek":
        metadata = _DEEPSEEK_MODEL_METADATA.get(identifier, {})
        return metadata.get("context"), metadata.get("context_evidence")
    if provider_id == "grok":
        metadata = _GROK_MODEL_METADATA.get(identifier, {})
        return metadata.get("context"), metadata.get("metadata_evidence")
    return None, None


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
        "effort_modes": list(_MUSE_EFFORT_RANKS),
        "fast_mode": False,
        "parallel_tool_calls": True,
        "tool_choice_modes": ["auto"],
        "reasoning_history": "native",
        "metadata_evidence": _MUSE_REASONING_DOCS,
    },
    # The contributor tier is the same model on a different entitlement, and
    # Meta documents the same window for it. Discovery returned context null
    # here, so the route carried no window at all while its standard-tier
    # sibling carried 1M.
    "muse-spark-1.3-contributor": {
        "context": 1048576,
        "max_output": 131072,
        "metadata_evidence": "https://developer.meta.com/ai/models/muse-spark/",
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
        # Cerebras publishes one window per tier - 65,536 on the free trial,
        # 131,072 on a paid key - and its account endpoint reports neither, so
        # which applies here is not known. Both are recorded so a caller can
        # say the floor rather than assume the ceiling.
        "context_options": [65536, 131072],
        "max_output": 40960,
        "tools": True,
        "vision": False,
        "reasoning": True,
        "effort_modes": ["low", "medium", "high"],
        "evidence": _CEREBRAS_PUBLIC_DOCS,
    },
    "gemma-4-31b": {
        "context": 131072,
        "max_output": 40000,
        "vision": True,
        "reasoning": True,
        "effort_modes": ["none", "low", "medium", "high"],
    },
    "qwen-3.8-27b": {
        "context": 131072,
        "context_options": [65536, 131072],
        "max_output": 40960,
        "vision": True,
        "reasoning": True,
        "effort_modes": ["none", "low", "medium", "high"],
    },
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
    if provider_id == "gemini":
        try:
            return gemini_validate_connection(connection)
        except GeminiError as exc:
            raise ProviderError(str(exc)) from exc
    if connection is None:
        connection = {}
    if not isinstance(connection, dict):
        raise ProviderError("Provider connection must be an object.")
    if any(str(key).lower() in _LOCAL_CREDENTIAL_FIELDS for key in connection):
        raise ProviderError("Provider credentials cannot be stored in connection settings.")

    if provider_id == "antigravity":
        # CLI-only provider: there is no hosted endpoint to validate. The
        # routing fields exist so settings round-trip and the connection
        # signature stays stable; the installed `agy` binary is the transport.
        return {"region": "local", "base_url": ""}

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
    result = {"region": derived_region, "base_url": normalized_base}
    if provider_id == "devin":
        # Devin is session-based and needs an org id on the connection to
        # address /v3/organizations/{org_id}/sessions. It is a non-secret
        # routing field, validated by devin_agent.validate_connection.
        try:
            normalized_devin = devin_validate_connection(connection)
        except DevinAgentError as exc:
            raise ProviderError(str(exc)) from exc
        org_id = normalized_devin.get("org_id")
        if org_id is not None:
            result["org_id"] = org_id
    return result


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
    if provider_id == "openrouter":
        # Free-tier OpenRouter models gated to "agentic harnesses" 403
        # without HTTP-Referer and X-Title identifying the calling app.
        headers.update(openrouter_app_headers())
    return headers
