"""Catalogue refresh and launch-readiness policy for Provider Hub.

Discovery is metadata-only.  A catalogue is fresh for 24 hours.  Launch may
temporarily use a catalogue up to seven days old when a refresh fails for a
transient network, rate-limit, or server error, but only when it belongs to the
current connection and contains every exact selected route.  Credentials are
always checked independently of cached metadata.

Imports from bridge_core are deliberately lazy: bridge_core owns persistence
and credentials while importing providers and hub_config itself.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import hashlib
import json
import multiprocessing
from multiprocessing.connection import wait as wait_connections
from pathlib import Path
import re
import time


FRESH_SECONDS = 24 * 60 * 60
STALE_FALLBACK_SECONDS = 7 * 24 * 60 * 60
MAX_PARALLEL_REFRESHES = 8
BATCH_TIMEOUT_SECONDS = 58

_TRANSIENT_HTTP = {408, 425, 429}
_HTTP_STATUS = re.compile(r"\bHTTP\s+(\d{3})\b", re.IGNORECASE)
_PLANNING_FIELDS = (
    "context", "context_options", "context_kind", "runtime_context",
    "max_input", "max_output", "advertised_context",
    "advertised_max_output", "tools", "vision", "reasoning",
    "effort_modes", "fast_mode", "speed_tier", "streaming",
    "tool_choice", "parallel_tool_calls", "service_tiers",
    "reasoning_history", "complete_tool_cycles", "capabilities",
    "default_effort", "upstream_model_id", "routing_endpoints", "routing_ignore",
    "routing_all_tags", "effort_control", "reasoning_mandatory",
    "version", "base_model_id", "provider_effort_modes",
)


class CataloguePreparationError(Exception):
    """A structured launch preparation result that is not ready."""

    def __init__(self, result: dict):
        self.result = result
        messages = [issue["message"] for issue in result.get("errors", [])]
        super().__init__("\n".join(messages) or "The selected model routes are not ready.")


def _dependencies():
    from bridge_core import credentials, discover_provider, read_json
    from catalogue import route_specs
    from hub_config import claude_routes, connection_signature, project_catalogue, split_route
    from providers import PROVIDERS
    return {
        "credentials": credentials,
        "discover_provider": discover_provider,
        "read_json": read_json,
        "route_specs": route_specs,
        "connection_signature": connection_signature,
        "project_catalogue": project_catalogue,
        "split_route": split_route,
        "claude_routes": claude_routes,
        "providers": PROVIDERS,
    }


def _now(value=None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc)


def _age_seconds(value, current: datetime) -> int | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        fetched = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if fetched.tzinfo is None:
        return None
    # A small clock skew must not make otherwise current metadata unusable.
    return max(0, int((current - fetched.astimezone(timezone.utc)).total_seconds()))


def _clean_error(exc: Exception) -> str:
    message = " ".join(str(exc).split())
    return message[:1000] or "Provider model discovery failed."


def _transient_error(message: str) -> bool:
    match = _HTTP_STATUS.search(message)
    if match:
        status = int(match.group(1))
        return status in _TRANSIENT_HTTP or 500 <= status <= 599
    lowered = message.casefold()
    return any(phrase in lowered for phrase in (
        "could not reach", "timed out", "timeout", "temporarily unavailable",
        "connection reset", "connection refused", "network is unreachable",
        "name or service not known", "temporary failure in name resolution",
    ))


def _selected(settings: dict) -> dict[str, list[dict]]:
    dependencies = _dependencies()
    split_route = dependencies["split_route"]
    selected: dict[str, list[dict]] = {}
    # Catalogue mode plans every curated row (plus the family slots that
    # point at tier defaults); mapping mode plans the five slots.
    for slot_id, route_id in sorted(dependencies["claude_routes"](settings).items()):
        provider_id, _ = split_route(route_id)
        selected.setdefault(provider_id, []).append({
            "slot_id": slot_id,
            "route_id": route_id,
        })
    return selected


def _base_provider_result(provider_id: str, mappings=()) -> dict:
    providers = _dependencies()["providers"]
    rows = [dict(row) for row in mappings]
    return {
        "provider_id": provider_id,
        "provider_name": providers[provider_id]["name"],
        "routes": sorted({row["route_id"] for row in rows}),
        "slots": sorted({row["slot_id"] for row in rows}),
        "mappings": rows,
    }


def _issue(code: str, provider: dict, message: str) -> dict:
    return {
        "code": code,
        "provider_id": provider["provider_id"],
        "provider_name": provider["provider_name"],
        "routes": list(provider.get("routes", [])),
        "slots": list(provider.get("slots", [])),
        "message": message,
    }


def _route_description(provider: dict) -> str:
    routes = provider.get("routes", [])
    slots = provider.get("slots", [])
    route_word = "route" if len(routes) == 1 else "routes"
    slot_word = "selection" if len(slots) == 1 else "selections"
    return (f"selected {route_word} {', '.join(routes)} "
            f"({slot_word} {', '.join(slots)})")


def _cache_state(settings: dict, root: Path, provider_id: str, current: datetime) -> dict:
    deps = _dependencies()
    path = Path(root) / "catalogues" / f"{provider_id}.json"
    try:
        inventory = deps["read_json"](path)
    except Exception as exc:
        return {
            "exists": path.exists(), "signature_valid": False,
            "fetched_at": None, "age_seconds": None, "model_count": 0,
            "specs": {}, "error": _clean_error(exc),
        }
    expected = deps["connection_signature"](
        provider_id, settings["providers"][provider_id])
    signature_valid = bool(inventory) and inventory.get("connection_signature") == expected
    fetched_at = inventory.get("fetched_at") if inventory else None
    state = {
        "exists": bool(inventory),
        "signature_valid": signature_valid,
        "fetched_at": fetched_at,
        "age_seconds": _age_seconds(fetched_at, current),
        "model_count": 0,
        "source": inventory.get("source") if inventory else None,
        "specs": {},
    }
    if not signature_valid:
        return state
    try:
        projected = deps["project_catalogue"](
            provider_id, inventory, settings, observations={})
        state["model_count"] = len(projected)
        state["specs"] = deps["route_specs"]({"models": projected})
    except Exception as exc:
        state["signature_valid"] = False
        state["error"] = _clean_error(exc)
    return state


def _state_fields(state: dict) -> dict:
    return {
        key: state.get(key) for key in (
            "fetched_at", "age_seconds", "model_count", "source",
            "signature_valid",
        )
    }


def _result(mode: str, provider_results: dict, current: datetime, *, ready=None) -> dict:
    ordered = sorted(provider_results.items())
    warnings = [value["warning"] for _, value in ordered if value.get("warning")]
    errors = [value["error"] for _, value in ordered if value.get("error")]
    result = {
        "mode": mode,
        "checked_at": current.isoformat(),
        "freshness": {
            "fresh_seconds": FRESH_SECONDS,
            "stale_fallback_seconds": STALE_FALLBACK_SECONDS,
        },
        "providers": dict(sorted(provider_results.items())),
        "warnings": warnings,
        "errors": errors,
        "refreshed_provider_ids": sorted(
            provider_id for provider_id, value in provider_results.items()
            if value.get("status") == "refreshed"),
        "cached_provider_ids": sorted(
            provider_id for provider_id, value in provider_results.items()
            if value.get("status") in {"current", "cached_after_transient_error"}),
    }
    if ready is not None:
        result["ready"] = ready
    return result


def _isolated_call(sender, worker, arguments) -> None:
    """Run one real provider refresh behind a terminable process boundary."""
    try:
        sender.send((True, worker(*arguments)))
    except BaseException as exc:
        try:
            sender.send((False, _clean_error(exc)))
        except (BrokenPipeError, EOFError, OSError):
            pass
    finally:
        sender.close()


def _isolated_results(items, worker, timeout_seconds: int) -> dict:
    """Return provider results without exceeding the batch wall-clock bound.

    Provider discovery can contain several individually bounded requests (for
    example Ollama /api/show per model).  The macOS helper therefore runs each
    real provider refresh in its own process and terminates unfinished work at
    one shared deadline.  Atomic catalogue writes preserve the previous file
    if a worker is interrupted.
    """
    context = multiprocessing.get_context("fork")
    records = {}
    results = {}
    for key, arguments in items:
        receiver, sender = context.Pipe(duplex=False)
        process = context.Process(
            target=_isolated_call,
            args=(sender, worker, arguments),
            name=f"catalogue-{key}",
            daemon=True,
        )
        process.start()
        sender.close()
        records[receiver] = (key, process)

    deadline = time.monotonic() + timeout_seconds
    pending = set(records)
    while pending:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        for receiver in wait_connections(pending, timeout=min(remaining, .25)):
            key, process = records[receiver]
            try:
                succeeded, value = receiver.recv()
            except (EOFError, OSError):
                succeeded, value = False, "Provider refresh worker stopped unexpectedly."
            results[key] = value if succeeded else {"_worker_error": value}
            pending.remove(receiver)
            process.join(timeout=.05)

    for receiver in pending:
        key, process = records[receiver]
        results[key] = {
            "_worker_timeout": (
                f"Provider catalogue refresh exceeded the {timeout_seconds}-second batch limit."
            )
        }
        if process.is_alive():
            process.terminate()
    for receiver, (_, process) in records.items():
        process.join(timeout=.2)
        if process.is_alive():
            process.kill()
            process.join(timeout=.2)
        receiver.close()
    return results


def _threaded_results(items, worker, max_workers: int) -> dict:
    """Run injected/test workers concurrently without forking their fixtures."""
    results = {}
    workers = max(1, min(max_workers, len(items))) if items else 1
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="catalogue") as pool:
        futures = {pool.submit(worker, *arguments): key
                   for key, arguments in items}
        for future in as_completed(futures):
            key = futures[future]
            try:
                results[key] = future.result()
            except Exception as exc:
                results[key] = {"_worker_error": _clean_error(exc)}
    return results


def catalogue_fingerprint(
    settings: dict, root: Path, *, now=None, model_specs: dict | None = None,
) -> str:
    """Fingerprint Runtime-relevant metadata for the exact selected routes.

    Fetch timestamps, UI presentation, discovery provenance, and observed
    inference status are intentionally excluded.  Unrelated provider changes
    therefore cannot force a safe idle gateway restart.
    """
    current = _now(now)
    selected = _selected(settings)
    material = {"mappings": [], "providers": {}, "routes": {}}
    connection_signature = _dependencies()["connection_signature"]
    for provider_id, mappings in sorted(selected.items()):
        specs = (model_specs if model_specs is not None else
                 _cache_state(settings, root, provider_id, current)["specs"])
        material["providers"][provider_id] = connection_signature(
            provider_id, settings["providers"][provider_id])
        for row in mappings:
            material["mappings"].append([row["slot_id"], row["route_id"]])
        for route_id in sorted({row["route_id"] for row in mappings}):
            spec = specs.get(route_id)
            material["routes"][route_id] = (
                None if spec is None else
                {key: spec.get(key) for key in _PLANNING_FIELDS if key in spec}
            )
    options = settings.get("mapping_options") or {}
    if options:
        material["mapping_options"] = options
    encoded = json.dumps(
        material, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _credential_check(credentials_fn, settings: dict, provider: dict) -> str | None:
    try:
        _, source = credentials_fn(settings, provider["provider_id"])
        return source
    except Exception as exc:
        detail = _clean_error(exc)
        if provider.get("routes"):
            message = (f"{provider['provider_name']} credentials are unavailable for "
                       f"{_route_description(provider)}: {detail}")
        else:
            message = detail
        provider.update(
            status="blocked" if provider.get("routes") else "skipped_unconfigured",
            error=_issue("missing_credentials", provider, message)
            if provider.get("routes") else None,
            skip_reason=detail if not provider.get("routes") else None,
        )
        return None


def refresh_all(
    settings: dict,
    root: Path,
    *,
    provider_ids=None,
    now=None,
    credentials_fn=None,
    discover_fn=None,
    max_workers: int = MAX_PARALLEL_REFRESHES,
) -> dict:
    """Refresh configured providers independently for native app startup."""
    deps = _dependencies()
    current = _now(now)
    isolate_real_discovery = (
        credentials_fn is None and discover_fn is None
        and "fork" in multiprocessing.get_all_start_methods()
    )
    credentials_fn = credentials_fn or deps["credentials"]
    discover_fn = discover_fn or deps["discover_provider"]
    identifiers = sorted(deps["providers"] if provider_ids is None else provider_ids)

    def refresh_one(provider_id: str) -> dict:
        provider = _base_provider_result(provider_id)
        source = _credential_check(credentials_fn, settings, provider)
        if source is None:
            # Missing credentials are normal for an unselected account on app
            # startup; retain the reason without reporting a global failure.
            provider.pop("error", None)
            return provider
        provider["credential_source"] = source
        try:
            discover_fn(settings, provider_id, root)
        except Exception as exc:
            state = _cache_state(settings, root, provider_id, current)
            detail = _clean_error(exc)
            issue = _issue(
                "discovery_failed", provider,
                f"{provider['provider_name']} catalogue refresh failed: {detail}",
            )
            provider.update(_state_fields(state))
            provider.update(status="error", error=issue,
                            cache_available=state["signature_valid"])
            return provider
        state = _cache_state(settings, root, provider_id, current)
        provider.update(_state_fields(state))
        if not state["signature_valid"]:
            issue = _issue(
                "catalogue_not_saved", provider,
                f"{provider['provider_name']} discovery completed without a usable catalogue for this connection.",
            )
            provider.update(status="error", error=issue, cache_available=False)
        else:
            provider.update(status="refreshed", cache_available=True)
        return provider

    items = [(provider_id, (provider_id,)) for provider_id in identifiers]
    provider_results = (
        _isolated_results(items, refresh_one, BATCH_TIMEOUT_SECONDS)
        if isolate_real_discovery else
        _threaded_results(items, refresh_one, max_workers)
    )
    for provider_id, value in list(provider_results.items()):
        marker = value.get("_worker_timeout") or value.get("_worker_error")
        if not marker:
            continue
        provider = _base_provider_result(provider_id)
        code = "refresh_timeout" if value.get("_worker_timeout") else "refresh_worker_failed"
        issue = _issue(
            code, provider,
            f"{provider['provider_name']} catalogue refresh failed: {marker}",
        )
        state = _cache_state(settings, root, provider_id, current)
        provider.update(_state_fields(state))
        provider.update(status="error", error=issue,
                        cache_available=state["signature_valid"])
        provider_results[provider_id] = provider
    result = _result("app_open", provider_results, current)
    result["catalogue_fingerprint"] = catalogue_fingerprint(settings, root, now=current)
    return result


def prepare_launch(
    settings: dict,
    root: Path,
    *,
    now=None,
    credentials_fn=None,
    discover_fn=None,
    max_workers: int = MAX_PARALLEL_REFRESHES,
) -> dict:
    """Prepare exact selected routes, returning structured launch blockers."""
    deps = _dependencies()
    current = _now(now)
    isolate_real_discovery = (
        credentials_fn is None and discover_fn is None
        and "fork" in multiprocessing.get_all_start_methods()
    )
    credentials_fn = credentials_fn or deps["credentials"]
    discover_fn = discover_fn or deps["discover_provider"]
    selected = _selected(settings)

    def prepare_one(provider_id: str, mappings: list[dict]) -> dict:
        provider = _base_provider_result(provider_id, mappings)
        source = _credential_check(credentials_fn, settings, provider)
        if source is None:
            return provider
        provider["credential_source"] = source
        before = _cache_state(settings, root, provider_id, current)
        exact_routes = all(route in before["specs"] for route in provider["routes"])
        fresh = (before["signature_valid"] and exact_routes
                 and before["age_seconds"] is not None
                 and before["age_seconds"] <= FRESH_SECONDS)
        if fresh:
            provider.update(_state_fields(before))
            provider["status"] = "current"
            return provider

        try:
            discover_fn(settings, provider_id, root)
        except Exception as exc:
            detail = _clean_error(exc)
            can_fallback = (
                _transient_error(detail)
                and before["signature_valid"]
                and exact_routes
                and before["age_seconds"] is not None
                and before["age_seconds"] <= STALE_FALLBACK_SECONDS
            )
            provider.update(_state_fields(before))
            if can_fallback:
                issue = _issue(
                    "using_recent_cache", provider,
                    f"{provider['provider_name']} catalogue refresh failed for "
                    f"{_route_description(provider)}; using same-connection metadata "
                    f"from {before['fetched_at']}: {detail}",
                )
                provider.update(status="cached_after_transient_error", warning=issue)
                return provider
            if before["exists"] and not before["signature_valid"]:
                suffix = (" Cached metadata belongs to a different connection "
                          "and cannot be used.")
            elif exact_routes and before["age_seconds"] is not None:
                suffix = (f" Cached metadata is {before['age_seconds']} seconds old "
                          "and is outside the seven-day fallback window.")
            else:
                suffix = " No usable same-connection metadata contains every exact selected route."
            issue = _issue(
                "discovery_failed", provider,
                f"{provider['provider_name']} catalogue refresh failed for "
                f"{_route_description(provider)}: {detail}.{suffix}",
            )
            provider.update(status="blocked", error=issue)
            return provider

        after = _cache_state(settings, root, provider_id, current)
        provider.update(_state_fields(after))
        if not after["signature_valid"]:
            issue = _issue(
                "catalogue_not_saved", provider,
                f"{provider['provider_name']} discovery did not save metadata for "
                f"{_route_description(provider)} and the current connection.",
            )
            provider.update(status="blocked", error=issue)
            return provider
        missing = [route for route in provider["routes"]
                   if route not in after["specs"]]
        if missing:
            missing_rows = [row for row in mappings if row["route_id"] in missing]
            missing_provider = _base_provider_result(provider_id, missing_rows)
            issue = _issue(
                "route_not_advertised", missing_provider,
                f"{provider['provider_name']} catalogue does not contain selected "
                f"{'route' if len(missing) == 1 else 'routes'} {', '.join(missing)} "
                f"({'selection' if len(missing_provider['slots']) == 1 else 'selections'} "
                f"{', '.join(missing_provider['slots'])}). Choose an exact model ID "
                "advertised for this account.",
            )
            provider.update(status="blocked", error=issue)
            return provider
        provider["status"] = "refreshed"
        return provider

    items = sorted(selected.items())
    work = [(provider_id, (provider_id, mappings))
            for provider_id, mappings in items]
    provider_results = (
        _isolated_results(work, prepare_one, BATCH_TIMEOUT_SECONDS)
        if isolate_real_discovery else
        _threaded_results(work, prepare_one, max_workers)
    )
    for provider_id, value in list(provider_results.items()):
        marker = value.get("_worker_timeout") or value.get("_worker_error")
        if not marker:
            continue
        provider = _base_provider_result(provider_id, selected[provider_id])
        code = ("preparation_timeout" if value.get("_worker_timeout")
                else "preparation_worker_failed")
        state = _cache_state(settings, root, provider_id, current)
        exact_routes = all(route in state["specs"] for route in provider["routes"])
        can_fallback = (
            bool(value.get("_worker_timeout"))
            and state["signature_valid"]
            and exact_routes
            and state["age_seconds"] is not None
            and state["age_seconds"] <= STALE_FALLBACK_SECONDS
        )
        provider.update(_state_fields(state))
        if can_fallback:
            issue = _issue(
                "using_recent_cache", provider,
                f"{provider['provider_name']} catalogue refresh timed out for "
                f"{_route_description(provider)}; using same-connection metadata "
                f"from {state['fetched_at']}.",
            )
            provider.update(status="cached_after_transient_error", warning=issue)
            provider_results[provider_id] = provider
            continue
        issue = _issue(
            code, provider,
            f"{provider['provider_name']} launch preparation failed for "
            f"{_route_description(provider)}: {marker}",
        )
        provider.update(status="blocked", error=issue)
        provider_results[provider_id] = provider
    ready = not any(value.get("status") == "blocked"
                    for value in provider_results.values())
    result = _result("prepare_launch", provider_results, current, ready=ready)
    result["catalogue_fingerprint"] = catalogue_fingerprint(settings, root, now=current)
    return result


def validate_prepared_launch(
    settings: dict,
    root: Path,
    *,
    now=None,
    credentials_fn=None,
    max_workers: int = MAX_PARALLEL_REFRESHES,
) -> dict:
    """Recheck prepared cache identity and credentials without discovery.

    The native launch flow calls ``prepare-launch`` before starting Runtime.
    Activation uses this read-only validation so a transient fallback is not
    immediately followed by the same network request a second time.
    """
    deps = _dependencies()
    current = _now(now)
    credentials_fn = credentials_fn or deps["credentials"]
    selected = _selected(settings)

    def validate_one(provider_id: str, mappings: list[dict]) -> dict:
        provider = _base_provider_result(provider_id, mappings)
        source = _credential_check(credentials_fn, settings, provider)
        if source is None:
            return provider
        provider["credential_source"] = source
        state = _cache_state(settings, root, provider_id, current)
        provider.update(_state_fields(state))
        if not state["signature_valid"]:
            issue = _issue(
                "catalogue_connection_mismatch", provider,
                f"{provider['provider_name']} has no catalogue for the current "
                f"connection and {_route_description(provider)}. Prepare the "
                "selected routes before starting the gateway.",
            )
            provider.update(status="blocked", error=issue)
            return provider
        missing = [route for route in provider["routes"]
                   if route not in state["specs"]]
        if missing:
            missing_rows = [row for row in mappings if row["route_id"] in missing]
            missing_provider = _base_provider_result(provider_id, missing_rows)
            issue = _issue(
                "route_not_advertised", missing_provider,
                f"{provider['provider_name']} catalogue does not contain selected "
                f"{'route' if len(missing) == 1 else 'routes'} {', '.join(missing)}. "
                "Prepare the exact selected routes before starting the gateway.",
            )
            provider.update(status="blocked", error=issue)
            return provider
        age = state["age_seconds"]
        if age is None or age > STALE_FALLBACK_SECONDS:
            issue = _issue(
                "catalogue_too_old", provider,
                f"{provider['provider_name']} catalogue metadata for "
                f"{_route_description(provider)} is missing a usable fetch time or "
                "is older than seven days. Prepare the selected routes again.",
            )
            provider.update(status="blocked", error=issue)
            return provider
        provider["status"] = ("current" if age <= FRESH_SECONDS
                              else "recent_cache_validated")
        return provider

    work = [(provider_id, (provider_id, mappings))
            for provider_id, mappings in sorted(selected.items())]
    provider_results = _threaded_results(work, validate_one, max_workers)
    for provider_id, value in list(provider_results.items()):
        marker = value.get("_worker_error")
        if not marker:
            continue
        provider = _base_provider_result(provider_id, selected[provider_id])
        issue = _issue(
            "validation_worker_failed", provider,
            f"{provider['provider_name']} launch validation failed for "
            f"{_route_description(provider)}: {marker}",
        )
        provider.update(status="blocked", error=issue)
        provider_results[provider_id] = provider
    ready = not any(value.get("status") == "blocked"
                    for value in provider_results.values())
    result = _result("activate_validation", provider_results, current, ready=ready)
    result["catalogue_fingerprint"] = catalogue_fingerprint(settings, root, now=current)
    return result


def require_prepared(result: dict) -> dict:
    """Return a ready result or raise its structured CLI-facing error."""
    if not result.get("ready"):
        raise CataloguePreparationError(result)
    return result


def runtime_fingerprint_error(
    expected: str, actual: str | None, settings: dict,
) -> str | None:
    """Explain when a running gateway predates selected planning metadata."""
    if isinstance(actual, str) and actual == expected:
        return None
    routes = sorted(set(_dependencies()["claude_routes"](settings).values()))
    return ("The running gateway loaded an older catalogue snapshot for selected "
            f"routes {', '.join(routes)}. Restart the gateway after preparing these "
            "routes, then launch the desktop app again.")
