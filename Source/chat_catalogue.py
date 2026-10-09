"""Chat's client-neutral catalogue and per-conversation account selection.

Presentation comes from the shared TaskWraith-compatible branding contract.
Selections never change the provider settings used by either desktop client.
"""
from __future__ import annotations

import hashlib
import json

from branding import resolve_presentation
from codex_catalogue import choices as codex_choices
from hub_config import claude_routes, cli_account_dir, connection_signature
from model_names import friendly_model_name


def native_search_available(spec, connection, provider):
    return spec.get("web_search") is True and (connection.get("credential_mode") == "cli" or provider == "openrouter")


def chat_connection(settings, provider, account="", expected_scope=None):
    if not isinstance(account, str):
        raise ValueError("Choose a configured account.")
    connections = settings.get("providers") or {}
    if provider not in connections:
        raise ValueError("This provider is no longer configured.")
    connection = dict(connections[provider])
    mode = connection.get("credential_mode")
    field = "cli_account" if mode == "cli" else "key_account"
    rows = connection.get("cli_accounts" if mode == "cli" else "key_accounts") or []
    if account and (mode not in {"cli", "keychain"} or
                    account not in {row["id"] for row in rows}):
        raise ValueError("This account is no longer configured. Choose a model/account for a new chat.")
    connection[field] = account or None
    selected = {**settings, "providers": {**connections, provider: connection}}
    directory = cli_account_dir(selected, provider) if mode == "cli" else None
    identity = [connection_signature(provider, connection), mode, account, directory]
    scope = hashlib.sha256(json.dumps(identity, separators=(",", ":")).encode()).hexdigest()[:32]
    if expected_scope is not None and expected_scope != scope:
        raise ValueError("This connection changed. Choose the model/account again to start a fresh chat.")
    return selected, connection, directory, scope


def chat_choices(settings):
    rows, seen = [], set()
    specs = settings.get("_model_specs") or {}
    inventory = {"models": list({spec["id"]: spec for spec in specs.values()}.values())}
    selected = list(dict.fromkeys([*claude_routes(settings).values(),
                                  *(row["id"] for row in codex_choices(settings, inventory))]))
    for alias in selected:
        spec = specs.get(alias)
        if not spec:
            continue
        route = spec.get("id") or alias
        if route in seen:
            continue
        seen.add(route)
        provider = spec.get("provider_id") or route.split("/", 1)[0]
        connection = (settings.get("providers") or {}).get(provider)
        if not connection or connection.get("protocol") == "agent_session":
            continue
        mode = connection.get("credential_mode")
        field = "cli_account" if mode == "cli" else "key_account"
        accounts = [{"id": "", "label": "Default"}]
        if mode in {"cli", "keychain"}:
            accounts += connection.get("cli_accounts" if mode == "cli" else "key_accounts") or []
        accounts.sort(key=lambda a: a["id"] != (connection.get(field) or ""))
        model = spec.get("model_id") or route.split("/", 1)[-1]
        overrides = settings.get("branding_overrides") or {}
        labels = (overrides.get(provider) or {}).get("modelLabels") or {}
        label = labels.get(model) or labels.get(route) or friendly_model_name(spec.get("canonical_id") or model)
        presentation = resolve_presentation(provider, model, overrides, supplied_label=label)
        connection_presentation = resolve_presentation(provider, overrides=overrides)
        label = presentation.get("modelLabel") or spec.get("display_name") or model
        efforts = spec.get("effort_modes") or []
        if spec.get("reasoning") is False:
            efforts = []
        for account in accounts:
            _, _, _, scope = chat_connection(settings, provider, account["id"])
            rows.append({"id": route + "|" + account["id"], "route": route, "label": label,
                         "provider": presentation["displayProvider"], "account": account["id"],
                         "accountLabel": account["label"], "scope": scope,
                         "efforts": efforts, "context": spec.get("runtime_context") or spec.get("context"),
                         "max_output": spec.get("max_output"), "supportsTools": spec.get("tools") is not False,
                         "supportsWebSearch": native_search_available(spec, connection, provider),
                         "vision": spec.get("vision"),
                         "presentation": presentation, "connectionPresentation": connection_presentation})
    return rows
