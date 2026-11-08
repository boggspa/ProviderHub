"""Permanent, append-only per-request usage ledger.

``activity.jsonl`` is a diagnostic tail: it rotates at 512 KB (a few hours
of heavy use) and names no account, so nothing downstream can total a
month of usage or split it between two subscriptions of one provider.
This ledger keeps one line per completed request, for good, in monthly
files under ``usage-ledger/``. Every line carries a unique request id, so a
reader that re-reads a file never counts a request twice, and the account
that served it, so a reader such as Limit Counter can file the tokens under
the right account.

Deliberately excludes prompts, tool arguments, response text, headers,
credentials and config-folder paths. A CLI account's folder appears only as
a SHA-256 digest of its resolved path, which a reader holding the same
folder grant can recompute to match accounts.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import time
import uuid
from pathlib import Path

SCHEMA = 1
DIRECTORY = "usage-ledger"
#: Provider-reported token counts copied verbatim when they are non-negative ints.
TOKEN_FIELDS = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens",
                "cache_creation_5m_tokens", "cache_creation_1h_tokens")


def new_request_id() -> str:
    return uuid.uuid4().hex


def folder_digest(config_dir: str | None) -> str | None:
    """SHA-256 of a CLI account folder's resolved absolute path, or None for the CLI's default folder."""
    if not config_dir:
        return None
    return hashlib.sha256(os.path.realpath(os.path.expanduser(config_dir)).encode()).hexdigest()


def uses_folder_accounts(provider_id: str, mode: str | None) -> bool:
    """Matches chat_catalogue: CLI sources, and Mistral's Vibe key, pick a folder account."""
    return mode == "cli" or (provider_id == "mistral" and mode == "vibe")


def account(provider_id: str, connection: dict | None, config_dir: str | None = None) -> dict:
    """The account a request runs as: its Hub account id, source and, for folder accounts, folder digest.

    ``connection`` is the provider connection the request was planned with.
    ``config_dir`` is the folder a CLI turn actually ran in; the folder
    account is looked up by it, so a switch mid-request cannot mislabel it.
    """
    connection = connection or {}
    mode = connection.get("credential_mode")
    if uses_folder_accounts(provider_id, mode):
        rows = connection.get("cli_accounts") or ()
        if config_dir is None and connection.get("cli_account"):
            config_dir = next((row.get("config_dir") for row in rows if row.get("id") == connection["cli_account"]), None)
        wanted = folder_digest(config_dir)
        row = next((row for row in rows if wanted and folder_digest(row.get("config_dir")) == wanted), None)
        return {"id": row["id"] if row else "default", "label": row["label"] if row else "Default",
                "source": mode, "folder_sha256": wanted}
    active = connection.get("key_account")
    row = next((row for row in connection.get("key_accounts") or () if row.get("id") == active), None)
    return {"id": row["id"] if row else "default", "label": row["label"] if row else "Default",
            "source": mode or "unknown", "folder_sha256": None}


def entry(request_id: str, route: str, provider_id: str, account: dict | None, usage: dict | None,
          surface: str | None = None, service_tier: str | None = None, now: float | None = None) -> dict:
    now = time.time() if now is None else now
    tokens = {key: value for key, value in (usage or {}).items()
              if key in TOKEN_FIELDS and type(value) is int and value >= 0}
    details = (usage or {}).get("output_tokens_details")
    reasoning = details.get("reasoning_tokens") if isinstance(details, dict) else None
    if type(reasoning) is int and reasoning >= 0:
        tokens["reasoning_output_tokens"] = reasoning
    record = {"schema": SCHEMA, "request_id": request_id,
              "time": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now)) + f".{int(now * 1000) % 1000:03d}Z",
              "route": route, "provider_id": provider_id, "account": account or account_unknown(), "usage": tokens,
              # A gateway estimate (the provider reported no usage) is kept but flagged,
              # so a reader can show it without treating it as billed tokens.
              "estimated": isinstance(usage, dict) and usage.get("provider_hub_usage_source") == "estimate"}
    if surface:
        record["surface"] = surface
    if service_tier:
        record["service_tier"] = service_tier
    return record


def account_unknown() -> dict:
    return {"id": "unknown", "label": "Unknown", "source": "unknown", "folder_sha256": None}


def ledger_path(root: Path, epoch: float) -> Path:
    return Path(root) / DIRECTORY / (time.strftime("%Y-%m", time.gmtime(epoch)) + ".jsonl")


def append(root: Path, record: dict, now: float | None = None) -> bool:
    """Append one line, never rotating or truncating. Returns False instead of raising:
    a full disk or a permissions slip must not fail the request that was just served."""
    try:
        path = ledger_path(root, time.time() if now is None else now)
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        line = (json.dumps(record, separators=(",", ":"), sort_keys=True) + "\n").encode()
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            # Several gateway processes may share one root; the lock keeps lines whole.
            fcntl.flock(fd, fcntl.LOCK_EX)
            os.write(fd, line)
        finally:
            os.close(fd)
        return True
    except (OSError, TypeError, ValueError):
        return False
