"""Map desktop effort sliders onto provider-native reasoning ranks."""
from __future__ import annotations

# Vibe CLI thinking levels: Off, Low, Medium, High, Max. No xhigh or ultra.
MISTRAL_REASONING_EFFORTS = ["none", "low", "medium", "high", "max"]
MISTRAL_EFFORT_ALIASES = {
    "none": "none",
    "minimal": "low",
    "low": "low",
    "medium": "medium",
    "high": "high",
    "xhigh": "max",
    "max": "max",
    "ultra": "max",
}

# DeepSeek thinking-mode docs: Claude/ChatGPT ranks onto none/low/high/max.
DEEPSEEK_EFFORT_ALIASES = {
    "none": "none",
    "minimal": "low",
    "low": "low",
    "medium": "high",
    "high": "high",
    "xhigh": "high",
    "max": "max",
    "ultra": "max",
}

# Ollama OpenAI/Responses: none/low/medium/high/max. GPT-OSS cannot disable.
OLLAMA_THINK_EFFORTS = ["none", "low", "medium", "high", "max"]
OLLAMA_EFFORT_ALIASES = {
    "none": "none",
    "minimal": "low",
    "low": "low",
    "medium": "medium",
    "high": "high",
    "xhigh": "high",
    "max": "max",
    "ultra": "max",
}


def map_effort(requested: str | None, supported, aliases: dict[str, str]) -> str | None:
    if requested is None:
        return None
    normalized = aliases.get(requested)
    allowed = set(supported or [])
    if normalized is None or normalized not in allowed:
        return None
    return normalized


def ollama_effort_modes(identifier: str, reasoning: bool | None) -> list[str]:
    if reasoning is not True:
        return []
    name = identifier.casefold()
    if "deepseek" in name:
        return ["none", "low", "high", "max"]
    if "gpt-oss" in name:
        return ["low", "medium", "high"]
    return list(OLLAMA_THINK_EFFORTS)


def ollama_effort_aliases(identifier: str) -> dict[str, str]:
    return DEEPSEEK_EFFORT_ALIASES if "deepseek" in identifier.casefold() else OLLAMA_EFFORT_ALIASES
