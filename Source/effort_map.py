"""Map desktop effort sliders onto provider-native reasoning ranks."""
from __future__ import annotations

# Canonical desktop-to-provider rank order, low to high. Shared by the Claude
# effort control and the Codex effort slider; provider aliases map onto it.
EFFORT_ORDER = ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra")

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

# Mistral reasoning ladders are model-specific, not a single shared ladder.
# GLM 5.2 (Mistral hosted), Mistral Medium 3.5 and Mistral Small 4 expose the
# full Off | Low | Medium | High | Max ladder; every other Mistral reasoning
# model (Mistral Large 3, Codestral, Leanstral, ...) follows the Off | High
# principle. An earlier spike tested only two API models and wrongly assumed
# every Mistral model accepted only Off | High.
MISTRAL_NARROW_EFFORTS = ["none", "high"]


def mistral_effort_modes(identifier: str, reasoning: bool | None) -> list[str]:
    if reasoning is not True:
        return []
    name = identifier.rsplit("/", 1)[-1].casefold()
    if "glm" in name or "mistral-medium" in name or "mistral-small" in name:
        return list(MISTRAL_REASONING_EFFORTS)
    return list(MISTRAL_NARROW_EFFORTS)

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


# Cerebras reasoning ladders top out at xhigh or high depending on the model.
# Aliases stay exact; above-range requests cap to the advertised top rank.
CEREBRAS_EFFORT_ALIASES = {
    "minimal": "low",
    "low": "low",
    "medium": "medium",
    "high": "high",
    "xhigh": "xhigh",
    "max": "max",
    "ultra": "ultra",
}


def map_effort(requested: str | None, supported, aliases: dict[str, str]) -> str | None:
    if requested is None:
        return None
    normalized = aliases.get(requested)
    allowed = set(supported or [])
    if normalized is None or normalized not in allowed:
        return None
    return normalized


def cap_high_end(normalized: str | None, supported) -> str | None:
    """Cap an above-range native rank to the top advertised rank.

    Only requests at or above the model's own top rank are capped, so an
    explicit rank the model could have served differently (or a request to
    disable reasoning) still fails closed in the caller. Unknown ranks and
    models with no known ranks also return None.
    """
    if normalized is None or normalized not in EFFORT_ORDER:
        return None
    allowed = set(supported or [])
    if normalized in allowed:
        return normalized
    known = [rank for rank in allowed if rank in EFFORT_ORDER]
    if not known:
        return None
    top = max(known, key=EFFORT_ORDER.index)
    if EFFORT_ORDER.index(normalized) >= EFFORT_ORDER.index(top):
        return top
    return None


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
