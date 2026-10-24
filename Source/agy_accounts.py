"""Antigravity account homes, without reading or moving any credentials.

Unlike Gemini CLI's GEMINI_CLI_HOME, agy resolves ~/.gemini from HOME.
An extra account therefore owns an entire subprocess home, including XDG
state and macOS's home-relative Keychain lookup. The default login is untouched.
"""
from __future__ import annotations

import os
from pathlib import Path
import shlex

from cli_session import minimal_env, resolve_binary


def account_home(config_dir: str) -> Path:
    if (not isinstance(config_dir, str) or not config_dir.strip()
            or any(c in config_dir for c in ("\0", "\n", "\r"))):
        raise ValueError("Choose a separate Antigravity account home folder.")
    path = Path(config_dir).expanduser()
    if not path.is_absolute():
        raise ValueError("An Antigravity account home must be an absolute path.")
    try:
        path = path.resolve()
    except (OSError, RuntimeError) as exc:
        raise ValueError("The Antigravity account home could not be resolved.") from exc
    default = Path.home().resolve()
    gemini = (default / ".gemini").resolve()
    if path == default or path in default.parents or path == gemini or gemini in path.parents:
        raise ValueError("Choose a separate Antigravity home outside your existing ~/.gemini directory.")
    if path.exists() and not path.is_dir():
        raise ValueError("An Antigravity account home must be a folder.")
    for relative in (".gemini", ".gemini/antigravity-cli", ".gemini/config", ".config",
                     ".local/share", ".local/state", ".cache", ".run"):
        target = (path / relative).resolve()
        if not target.is_relative_to(path):
            raise ValueError("Antigravity state folders must stay inside their account home.")
    return path


def account_environment(config_dir: str | None = None) -> dict[str, str]:
    if config_dir is None:
        return {}
    root = account_home(config_dir)
    return {
        "HOME": str(root),
        "GEMINI_CLI_HOME": str(root),
        "XDG_CONFIG_HOME": str(root / ".config"),
        "XDG_DATA_HOME": str(root / ".local/share"),
        "XDG_CACHE_HOME": str(root / ".cache"),
        "XDG_STATE_HOME": str(root / ".local/state"),
        "XDG_RUNTIME_DIR": str(root / ".run"),
        # The shared installed executable is resolved before HOME changes.
        "AGY_CLI_DISABLE_AUTO_UPDATE": "1",
    }


def prepare_login(config_dir: str | None = None) -> dict:
    """Prepare private folders and a vendor-owned interactive sign-in command."""
    extra = account_environment(config_dir)
    binary = resolve_binary(("agy", "antigravity"),
                            extra_dirs=(str(Path.home() / ".local/bin"), "/opt/homebrew/bin", "/usr/local/bin"))
    if not binary:
        raise ValueError("The agy CLI is not installed or not on PATH.")
    if config_dir is not None:
        for value in (extra["HOME"], extra["XDG_RUNTIME_DIR"]):
            Path(value).mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(value, 0o700)
    env = minimal_env(extra)
    command = "env -i " + " ".join(shlex.quote(f"{key}={value}") for key, value in sorted(env.items()))
    command += " " + shlex.quote(binary)
    # Login is never launched inside an arbitrary existing work project.
    command = "cd " + shlex.quote(env.get("HOME", str(Path.home()))) + " && " + command
    return {"config_dir": extra.get("HOME"), "script": command}
