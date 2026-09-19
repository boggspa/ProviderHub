"""Context metadata for CLI routes used by Claude Code Desktop.

Resolve these limits when Claude reads its catalogue or plans a Messages
request, so an old discovery snapshot cannot hide a changed Codex setting.
The shared inventory and Codex Desktop projection remain untouched.
"""
from pathlib import Path
import re
import tomllib


_CODEX_MODELS = frozenset({
    "gpt-5.6-luna", "gpt-5.6-terra", "gpt-5.6-sol", "gpt-6-astra",
})
_CODEX_DEFAULT_CONTEXT = 256_000
_ANTIGRAVITY_GEMINI_MODELS = frozenset({
    "gemini-3.1-pro", "gemini-3.6-flash", "gemini-3.7-flash", "gemini-3.8-flash",
})


def _read_toml(path: Path) -> dict:
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def codex_context_window() -> int | None:
    """Read the context setting from the same home the Codex CLI route uses.

    codex_cli_agent deliberately drops CODEX_HOME from its child environment
    and uses ~/.codex. Do not accidentally read a different parent's home,
    a project's override, or the auto-compaction threshold as its window.
    https://learn.chatgpt.com/docs/config-file/config-reference
    """
    home = Path.home() / ".codex"
    config = _read_toml(home / "config.toml")
    layers = [config]
    profile = config.get("profile")
    if isinstance(profile, str) and re.fullmatch(r"[A-Za-z0-9_-]+", profile):
        # Support older Codex [profiles.name] tables and current sibling files.
        profiles = config.get("profiles")
        legacy = profiles.get(profile) if isinstance(profiles, dict) else None
        if isinstance(legacy, dict):
            layers.append(legacy)
        layers.append(_read_toml(home / f"{profile}.config.toml"))
    for layer in reversed(layers):
        context = layer.get("model_context_window")
        if type(context) is int and context > 0:
            return context
    return None


def claude_context_spec(spec: dict, settings: dict) -> dict:
    """A copy with Claude's effective CLI window, or the original other route."""
    provider = spec.get("provider_id")
    model = spec.get("model_id") or spec.get("id", "").removeprefix(f"{provider}/")
    if provider == "codex":
        connection = (settings.get("providers") or {}).get(provider) or {}
        if connection.get("credential_mode") != "cli":
            return spec
        context = codex_context_window()
        if context is None:
            if model not in _CODEX_MODELS:
                return spec
            context = _CODEX_DEFAULT_CONTEXT
    elif provider == "antigravity":
        # Old snapshots can contain native effort IDs instead of family IDs.
        family = re.sub(r"-(?:low|medium|high)$", "", model)
        if family not in _ANTIGRAVITY_GEMINI_MODELS:
            return spec
        # Gemini 3's published 1M window; agy models reports names/effort only.
        # https://ai.google.dev/gemini-api/docs/gemini-3
        context = 1_000_000
    else:
        return spec
    resolved = {**spec, "context": context}
    # A resolved window must not still advertise an older ambiguous range.
    resolved.pop("context_options", None)
    return resolved
