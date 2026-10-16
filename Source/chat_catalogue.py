"""Chat's client-neutral catalogue and per-conversation account selection.

Presentation comes from the shared TaskWraith-compatible branding contract.
Selections never change the provider settings used by either desktop client.

A provider may offer two credential sources at once (hub_config.chat_sources):
the active one, whose rows carry bare account ids as they always have, and a
second one, whose rows carry ``<source>:<id>`` so a saved chat keeps naming
the login it ran on.
"""
from __future__ import annotations

import hashlib
import json

from branding import resolve_presentation
from cli_routes import native_context_limits, native_context_spec
from codex_catalogue import choices as codex_choices
from hub_config import (CHAT_SOURCE_LABELS, chat_extra_source, chat_sources, claude_routes, cli_account_dir,
                        connection_signature, split_chat_account)
from model_names import friendly_model_name


def native_search_available(spec, connection, provider):
    return spec.get("web_search") is True and (connection.get("credential_mode") == "cli" or provider == "openrouter")


def chat_connection(settings, provider, account="", expected_scope=None):
    source, account = split_chat_account(account)
    connections = settings.get("providers") or {}
    if provider not in connections:
        raise ValueError("This provider is no longer configured.")
    connection = dict(connections[provider])
    if source is not None:
        if source not in chat_sources(connection, provider):
            raise ValueError("This account's credential source is no longer offered in Chat. "
                             "Choose a model/account for a new chat.")
        connection["credential_mode"] = source
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
    """One row per route and account, on every credential source Chat offers.

    The active source lists the launcher-enabled routes, as the picker always
    has. A second source has no tick list of its own, so it lists every model
    its catalogue holds; the picker's search narrows it.
    """
    specs = settings.get("_model_specs") or {}
    inventory = {"models": list({spec["id"]: spec for spec in specs.values()}.values())}
    selected = list(dict.fromkeys([*claude_routes(settings).values(),
                                  *(row["id"] for row in codex_choices(settings, inventory))]))
    native = {}
    rows = _rows(settings, specs, selected, native=native)
    for provider, sources in (settings.get("_chat_source_specs") or {}).items():
        connection = (settings.get("providers") or {}).get(provider) or {}
        extra = chat_extra_source(connection, provider)
        if extra and extra in sources:
            rows.extend(_rows(settings, sources[extra], list(sources[extra]), source=extra, only=provider, native=native))
    return rows


def _rows(settings, specs, candidates, *, source=None, only=None, native=None):
    # Per-account native limits, read once per (provider, folder) per listing.
    native = {} if native is None else native
    rows, seen = [], set()
    for alias in candidates:
        spec = specs.get(alias)
        if not spec:
            continue
        route = spec.get("id") or alias
        if route in seen:
            continue
        seen.add(route)
        provider = spec.get("provider_id") or route.split("/", 1)[0]
        if only is not None and provider != only:
            continue
        connection = (settings.get("providers") or {}).get(provider)
        if not connection or connection.get("protocol") == "agent_session":
            continue
        labelled = len(chat_sources(connection, provider)) > 1
        if source is not None:
            connection = {**connection, "credential_mode": source}
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
            identifier = account["id"] if source is None else source + ":" + account["id"]
            _, _, directory, scope = chat_connection(settings, provider, identifier)
            account_spec = spec
            if mode == "cli":
                # The gateway's frozen specs follow the active account at its
                # start; each login shows the window its own cards allow now.
                key = (provider, directory)
                if key not in native:
                    native[key] = native_context_limits(provider, config_dir=directory)
                account_spec = native_context_spec(provider, model, spec, limits=native[key].get(model) or {})
            account_label = account["label"] + (" · " + CHAT_SOURCE_LABELS.get(mode, mode) if labelled else "")
            rows.append({"id": route + "|" + identifier, "route": route, "label": label,
                         "provider": presentation["displayProvider"], "account": identifier,
                         "accountLabel": account_label, "source": mode, "scope": scope,
                         "efforts": efforts, "context": account_spec.get("runtime_context") or account_spec.get("context"),
                         "max_output": spec.get("max_output"), "supportsTools": spec.get("tools") is not False,
                         "supportsWebSearch": native_search_available(spec, connection, provider),
                         "vision": spec.get("vision"),
                         "presentation": presentation, "connectionPresentation": connection_presentation})
    return rows
