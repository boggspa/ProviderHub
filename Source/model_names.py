"""Display-only model labels. Routing always retains the unmodified API ID."""
from __future__ import annotations

from datetime import datetime
import re

BRANDS = {
    "mistral": "Mistral", "ministral": "Ministral", "codestral": "Codestral",
    "devstral": "Devstral", "magistral": "Magistral", "pixtral": "Pixtral",
    "voxtral": "Voxtral", "nemo": "NeMo", "glm": "GLM", "zai": "Z.ai",
    "kimi": "Kimi", "mimo": "MiMo", "deepseek": "DeepSeek", "qwen": "Qwen",
    "grok": "Grok", "llama": "Llama", "gpt": "GPT", "oss": "OSS",
    "api": "API", "cli": "CLI", "tts": "TTS", "ocr": "OCR", "vl": "VL",
    "fp8": "FP8", "fp16": "FP16", "bf16": "BF16", "mamba": "Mamba",
    "mlx": "MLX", "lfm": "LFM", "minimax": "MiniMax", "minicpm": "MiniCPM", "xs": "XS",
}

# Versioned IDs with known product names. Keep moving "latest" aliases out of
# this table; those use live provider/Vibe metadata instead.
PINNED_LABELS = {
    "mistral-small-2603": "Mistral Small 4",
    "mistral-large-2512": "Mistral Large 3",
    "ministral-3b-2512": "Ministral 3 · 3B",
    "ministral-8b-2512": "Ministral 3 · 8B",
    "ministral-14b-2512": "Ministral 3 · 14B",
    "labs-leanstral-1-5-1": "Leanstral 1.5.1 · Labs",
}


# Claude's versioned catalogue, in family/version order. Keep this separate
# from generic name parsing: another provider (notably AntiGravity) may offer
# the same family with its own lifecycle and display metadata.
CLAUDE_MODEL_LABELS = {
    "claude-fable-5-1": "Claude Fable 5.1",
    "claude-fable-5": "Claude Fable 5 (Legacy)",
    "claude-opus-5-5": "Claude Opus 5.5",
    "claude-opus-5": "Claude Opus 5 (Legacy)",
    "claude-opus-4-8": "Claude Opus 4.8 (Legacy)",
    "claude-opus-4-7": "Claude Opus 4.7 (Legacy)",
    "claude-opus-4-6": "Claude Opus 4.6 (Legacy)",
    "claude-sonnet-5-5": "Claude Sonnet 5.5",
    "claude-sonnet-5": "Claude Sonnet 5 (Legacy)",
    "claude-sonnet-4-6": "Claude Sonnet 4.6 (Legacy)",
    "claude-haiku-5-5": "Claude Haiku 5.5",
    "claude-haiku-4-5": "Claude Haiku 4.5 (Legacy)",
}

# Compatibility for selections saved before the versioned CLI catalogue.
# New picker rows use the full ID; short names are never extra picker rows.
CLAUDE_CLI_ALIASES = {
    "fable": "claude-fable-5-1",
    "opus": "claude-opus-5-5",
    "sonnet": "claude-sonnet-5-5",
    "haiku": "claude-haiku-5-5",
}


def claude_model_label(identifier: str) -> str | None:
    """A known version's label, including dated API IDs, for Claude only."""
    version = re.sub(r"-\d{8}$", "", identifier).replace(".", "-")
    return CLAUDE_MODEL_LABELS.get(version)


def release_date(value: str) -> str | None:
    if not value.isdigit():
        return None
    formats = {4: ("%y%m", "%b %Y"), 6: ("%y%m%d", "%d %b %Y"), 8: ("%Y%m%d", "%d %b %Y")}
    if len(value) not in formats:
        return None
    fmt, display = formats[len(value)]
    try:
        parsed = datetime.strptime(value, fmt)
    except ValueError:
        return None
    if not 2020 <= parsed.year <= 2039:
        return None
    return parsed.strftime(display).lstrip("0")


def word_label(word: str) -> str:
    lower = word.lower()
    if lower in BRANDS:
        return BRANDS[lower]
    if re.fullmatch(r"\d+(?:\.\d+)?[bkmt]", lower):
        return word[:-1] + word[-1].upper()
    if re.fullmatch(r"v\d+(?:\.\d+)*", lower):
        return "V" + word[1:]
    if re.fullmatch(r"q\d+", lower):
        return word.upper()
    if re.fullmatch(r"[rkm]\d+(?:\.\d+)*", lower):
        return word[0].upper() + word[1:]
    if re.fullmatch(r"[a-zA-Z]+\d+(?:\.\d+)*", word):
        match = re.fullmatch(r"([a-zA-Z]+)(.+)", word)
        return BRANDS.get(match[1].lower(), match[1].capitalize()) + " " + match[2]
    return word.capitalize()


def friendly_model_name(identifier: str, advertised_name: str | None = None) -> str:
    """Keep date, parameter size, preview and cloud distinctions in the label.

    Opaque aliases are not assumed to identify an underlying model version.
    The caller can supply a verified display name from provider/Vibe metadata.
    """
    original = identifier.strip()
    if original in PINNED_LABELS and (not advertised_name or advertised_name.strip() == original):
        return PINNED_LABELS[original]
    route = original
    if "/" in route:
        namespace, route = route.rsplit("/", 1)
    else:
        namespace = ""
    segments = route.split(":")
    route, qualifiers = segments[0], segments[1:]
    suffixes = []
    tokens = re.split(r"[-_]", route)
    if tokens:
        date = release_date(tokens[-1])
        if date:
            tokens.pop()
            suffixes.append(date)
    if tokens and tokens[-1].lower() in {"latest", "preview", "beta", "experimental"}:
        suffixes.insert(0, tokens.pop().capitalize())
    route = "-".join(tokens)
    # GLM 5-2 / Small 3-2 are version numbers. Do not join large dimensions,
    # parameter counts such as 14b, or the date suffix already extracted above.
    route = re.sub(r"(?<![\w.])(v?\d{1,2})-(\d{1,2})(?=-|$)", r"\1.\2", route, flags=re.I)
    if route.startswith("zai-glm-"):
        route = route[4:]
    if route.startswith("open-mistral-"):
        route = route[5:]
    if route == "mistral-vibe-cli":
        label = "Mistral Vibe"
    else:
        label = " ".join(word_label(word) for word in re.split(r"[-_\s]+", route) if word)
    # Human-readable upstream metadata wins over our spelling heuristics.
    if advertised_name and advertised_name.strip() != original and " " in advertised_name.strip():
        label = " ".join(advertised_name.split())
    for qualifier in qualifiers:
        suffixes.append(" ".join(word_label(word) for word in re.split(r"[-_]", qualifier) if word))
    if namespace and namespace.lower() not in {"mistral", "mistralai", "zai", "meta-llama"}:
        suffixes.append(" ".join(word_label(word) for word in re.split(r"[-_]", namespace) if word))
    suffixes = [suffix for suffix in suffixes if suffix and suffix.casefold() not in label.casefold()]
    return " · ".join([label or original, *suffixes])


def label_catalog(identifiers, entries=(), vibe=None):
    advertised = {entry["id"]: entry.get("advertised_name") for entry in entries if entry.get("id")}
    result = {identifier: friendly_model_name(identifier, advertised.get(identifier)) for identifier in identifiers}
    if vibe and vibe.get("active_model") in result and vibe.get("active_display_name"):
        label = vibe["active_display_name"]
        if " " not in label:
            label = friendly_model_name(label)
        if vibe["active_model"].startswith("mistral-vibe-"):
            label += " · Vibe"
        result[vibe["active_model"]] = label
    return result
