"""Exact model IDs with a switchable Fast tier or a fixed fast route.

Fast is a request control only for the first group. The second group is
already served on its faster route; advertising a service-tier toggle for it
would promise a standard-speed version the provider does not offer there.
"""
from model_names import CLAUDE_CLI_ALIASES

# https://learn.chatgpt.com/docs/agent-configuration/speed
# https://developers.openai.com/api/docs/guides/fast-mode
OPENAI_FAST_MODELS = frozenset({
    "gpt-5.5", "gpt-5.6-luna", "gpt-5.6-terra", "gpt-5.6-sol",
    "gpt-6-astra", "gpt-6-luna", "gpt-6-sol", "gpt-6.1-sol", "gpt-6.1-terra",
    "gpt-6.2-astra", "gpt-6.2-sol",
})

# https://platform.claude.com/docs/en/build-with-claude/fast-mode
# Opus 4.7 rejects Fast and 4.6 silently runs at Standard; neither qualifies.
CLAUDE_FAST_MODELS = frozenset({
    "claude-opus-5-5", "claude-opus-5", "claude-opus-4-8",
})
CLAUDE_FAST_BETA = "fast-mode-2026-02-01"

# A fast route selected by ID or provider, with no second speed tier to choose.
# The values are catalogue metadata, not request parameters.
FIXED_SPEED_TIERS = {
    "kimi": {"kimi-for-coding-highspeed": "highspeed", "k3.1-pro-highspeed": "highspeed"},
    "antigravity": {
        "gemini-3.6-flash": "flash", "gemini-3.7-flash": "flash",
        "gemini-3.8-flash": "flash",
    },
    "grok": {"grok-4.7-build-fast": "fast_variant"},
    "cerebras": {
        "gpt-oss-120b": "accelerated_inference",
        "qwen-3.8-27b": "accelerated_inference",
    },
}


def supports_fast_toggle(provider_id: str, model_id: str) -> bool:
    """Whether this exact route has a documented same-model Fast request."""
    if provider_id == "codex":
        return model_id in OPENAI_FAST_MODELS
    if provider_id == "claude":
        return CLAUDE_CLI_ALIASES.get(model_id, model_id) in CLAUDE_FAST_MODELS
    return False


def fixed_speed_tier(provider_id: str, model_id: str) -> str | None:
    """Name a preselected fast route without claiming a speed control."""
    return FIXED_SPEED_TIERS.get(provider_id, {}).get(model_id)


# Ultrafast: service_tier "ultrafast" on the same model.
# https://developers.openai.com/api/docs/guides/ultrafast-mode (checked 2026-11-17)
OPENAI_ULTRAFAST_MODELS = frozenset({"gpt-6.2-astra", "gpt-6.2-sol", "gpt-6-astra", "gpt-6.1-sol"})

# The hub's speed vocabulary. Standard is always available and never listed.
SPEEDS = ("standard", "fast", "ultrafast")

# ChatGPT-included usage per request relative to Standard, for subscription
# (Codex CLI) routes. https://learn.chatgpt.com/docs/pricing#speed-multipliers
# (checked 2026-11-17). API routes pay the API's own per-token speed prices.
_CHATGPT_USAGE = {
    "fast": {"gpt-6.2-astra": "2x", "gpt-6.2-sol": "2x", "gpt-6.1-sol": "2.5x", "gpt-6.1-terra": "2.5x",
             "gpt-6-astra": "2.5x", "gpt-6-sol": "2.5x", "gpt-6-luna": "2.5x"},
    "ultrafast": {"gpt-6.2-astra": "6x", "gpt-6.2-sol": "6x", "gpt-6-astra": "8x", "gpt-6.1-sol": "8x"},
}
# Documented token-speed gains where OpenAI states one per model.
# https://learn.chatgpt.com/docs/agent-configuration/speed
_OPENAI_ULTRAFAST_SPEED = {"gpt-6.2-astra": "up to 4.5x speed", "gpt-6.2-sol": "up to 4.5x speed",
                           "gpt-6-astra": "up to 8x speed"}


def _option(identifier, label, detail, wire=None):
    option = {"id": identifier, "label": label, "detail": detail}
    if wire:
        option["wire"] = wire
    return option


def _native_speed(tier):
    """Map one native Codex ``serviceTiers`` card entry to the hub's speed id."""
    identifier = str(tier.get("id") or "").lower()
    name = str(tier.get("name") or "").lower()
    if identifier == "ultrafast" or name == "ultrafast":
        return "ultrafast"
    if identifier in {"priority", "fast"} or name == "fast":
        return "fast"
    return None


def speed_options(provider_id: str, model_id: str, *, source: str = "api", native_tiers=None) -> list[dict]:
    """The speeds beyond Standard this exact route can request, cheapest first.

    Each option is ``{"id", "label", "detail"}`` plus, for a subscription CLI
    route, ``"wire"``: the tier id the account's own model card advertises.
    A Codex CLI route offers only what that card lists (``native_tiers``), so
    a plan without Ultrafast never sees it; without a card it offers the
    documented Fast alone. API routes follow the published model lists.
    """
    model = CLAUDE_CLI_ALIASES.get(model_id, model_id) if provider_id == "claude" else model_id
    if provider_id == "codex":
        if source == "cli" and native_tiers is not None:
            options = {}
            for tier in native_tiers:
                speed = _native_speed(tier) if isinstance(tier, dict) else None
                if speed is None or speed in options:
                    continue
                if speed == "fast" and model not in OPENAI_FAST_MODELS:
                    continue
                described = str(tier.get("description") or "").strip()
                usage = _CHATGPT_USAGE[speed].get(model)
                if usage and "usage" not in described.lower():
                    described = (described + " · " if described else "") + usage + " usage"
                elif usage:
                    described = described.replace("increased usage", usage + " usage")
                options[speed] = _option(speed, speed.title(), described or "Faster, uses more of your plan",
                                         wire=str(tier.get("id")))
            return [options[speed] for speed in SPEEDS if speed in options]
        options = []
        if model in OPENAI_FAST_MODELS:
            if source == "cli":
                usage = _CHATGPT_USAGE["fast"].get(model)
                options.append(_option("fast", "Fast", (usage + " usage") if usage else "Faster, uses more of your plan",
                                       wire="priority"))
            else:
                options.append(_option("fast", "Fast", "Faster · API Fast pricing"))
        if model in OPENAI_ULTRAFAST_MODELS and source != "cli":
            options.append(_option("ultrafast", "Ultrafast",
                                   _OPENAI_ULTRAFAST_SPEED.get(model, "Fastest") + " · API Ultrafast pricing"))
        return options
    if provider_id == "claude" and model in CLAUDE_FAST_MODELS:
        return [_option("fast", "Fast", "Up to 2.5x output speed · premium pricing")]
    if provider_id == "grok" and source != "cli":
        return [_option("fast", "Fast", "xAI Priority · premium token rates")]
    return []


def route_speeds(provider_id: str, spec: dict, model_id: str | None = None) -> list[dict]:
    """The speed options for one catalogue entry.

    Options stored at discovery (a CLI account's own card) win. An entry saved
    before speeds existed derives them from the published lists, and an entry
    that never advertised Fast offers nothing beyond Standard.
    """
    if not isinstance(spec, dict):
        return []
    stored = spec.get("speed_options")
    if isinstance(stored, list):
        return [dict(option) for option in stored
                if isinstance(option, dict) and option.get("id") in SPEEDS[1:]]
    if spec.get("fast_mode") is not True:
        return []
    model = model_id or spec.get("model_id") or spec.get("canonical_id") or spec.get("id") or ""
    return speed_options(provider_id, str(model).split("/", 1)[-1], source=spec.get("source") or "api")


def requested_speed(payload: dict) -> str:
    """Normalise a request's speed fields to standard, fast or ultrafast.

    Accepts Anthropic's ``speed`` and OpenAI's ``service_tier`` (``priority``
    is OpenAI's catalogue id for Fast). Anything else is returned unchanged
    for the planner to reject.
    """
    speed, tier = payload.get("speed"), payload.get("service_tier")
    if speed == "ultrafast" or tier == "ultrafast":
        return "ultrafast"
    if speed == "fast" or tier in {"fast", "priority"}:
        return "fast"
    return "standard"
