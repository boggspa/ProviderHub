"""Exact model IDs with a switchable Fast tier or a fixed fast route.

Fast is a request control only for the first group. The second group is
already served on its faster route; advertising a service-tier toggle for it
would promise a standard-speed version the provider does not offer there.
"""

# https://learn.chatgpt.com/docs/agent-configuration/speed
# https://developers.openai.com/api/docs/guides/fast-mode
OPENAI_FAST_MODELS = frozenset({
    "gpt-5.5", "gpt-5.6-luna", "gpt-5.6-terra", "gpt-5.6-sol",
    "gpt-6-astra", "gpt-6-luna", "gpt-6-sol",
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
    "kimi": {"kimi-for-coding-highspeed": "highspeed"},
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
        return model_id in CLAUDE_FAST_MODELS
    return False


def fixed_speed_tier(provider_id: str, model_id: str) -> str | None:
    """Name a preselected fast route without claiming a speed control."""
    return FIXED_SPEED_TIERS.get(provider_id, {}).get(model_id)
