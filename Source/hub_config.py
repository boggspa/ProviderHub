"""Provider-aware settings and catalogue projection for the desktop hub.

Presentation is deliberately downstream of provider/credential selection.
This module never reads or serializes an API key.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re

from providers import PROVIDERS, provider_defaults, validate_connection
from branding import resolve_presentation, validate_overrides
from ollama_lifecycle import DEFAULT_LEASE_SECONDS, KEEP_RESIDENT, MAX_LEASE_SECONDS
from effort_map import mistral_ladder_for_model
from fast_models import fixed_speed_tier, supports_fast_toggle
from model_names import (CLAUDE_CLI_ALIASES, CLAUDE_MODEL_LABELS, PINNED_LABELS,
                         claude_model_label, friendly_model_name)

MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/+\-]{0,299}\Z")
MAX_CREDENTIAL_REVISION = 9_007_199_254_740_991

# These fields can change whether a route is admissible or how a request is
# planned.  Rows coalesce only when every value is exactly equal.  Descriptive
# and provenance fields remain on the deterministic representative row.
MODEL_VARIANT_FIELDS = (
    "context", "context_options", "context_kind", "runtime_context",
    "max_input", "max_output", "advertised_context", "advertised_max_output",
    "tools", "vision", "reasoning", "effort_modes", "default_effort", "fast_mode", "speed_tier",
    "streaming", "tool_choice", "parallel_tool_calls", "service_tiers",
    "reasoning_history", "complete_tool_cycles", "capabilities", "billing_model_name",
    "upstream_model_id", "routing_endpoints", "routing_ignore", "routing_all_tags", "effort_control", "reasoning_mandatory",
    "version", "base_model_id", "provider_effort_modes",
    "multi_agent_version", "multi_agent_reasoning_effort",
)

#: Number of preferred model ranks exposed by the Hub UI. Codex's native
#: spawn description lists this many priority rows, but the runtime accepts
#: every catalogue route (measured on 0.155.0-alpha.9.2). subagent_catalogue
#: advertises the rest too. Priority is one global ordering, not a per-parent
#: permission. Lives here because settings validation also needs it.
SUBAGENT_POOL_SIZE = 5


#: Providers that can run on an installed coding-agent CLI's own login instead
#: of an API key.  Opt-in per provider and local-only: the default stays the
#: API-key mode, so an existing install changes nothing until that one
#: provider's credential source is switched to "cli" by hand.  There is no
#: master flag - the per-provider choice *is* the gate.
#:
#: This is deliberately the "let the CLI own its credential" route rather than
#: resolving a token ourselves.  These are rotating OAuth grants, not the
#: long-lived API key the Vibe route resolves: a second holder of the same
#: refresh token revokes the first, and ChatGPT's rotates on every use.  So
#: the hub spawns the vendor's own runtime and never reads, copies, or
#: refreshes a credential file.
CLI_AUTH_PROVIDERS = frozenset({"codex", "claude", "muse", "grok", "antigravity"})


def cli_auth_available(provider_id: str) -> bool:
    """Whether this provider offers a CLI-login credential source at all."""
    return provider_id in CLI_AUTH_PROVIDERS and provider_id in PROVIDERS


def _default_credential_mode(provider_id: str) -> str:
    if provider_id == "antigravity":
        # CLI-only provider: its installed `agy` binary is the whole transport.
        return "cli"
    if provider_id == "mistral":
        return "vibe"
    if provider_id == "ollama":
        return "none"
    return "keychain"


def credential_modes(provider_id: str) -> list[str]:
    """The credential sources a provider offers, in picker order.

    AntiGravity has no hosted API of its own, so its installed CLI is the only
    source. The other CLI-capable providers keep their API-key sources beside
    it, and the API key stays the default everywhere: the per-provider choice
    is the whole gate, so an existing install changes nothing until a provider
    is switched by hand.
    """
    if provider_id == "antigravity":
        return ["cli"]
    if provider_id == "ollama":
        return ["none"]
    modes = (["vibe"] if provider_id == "mistral" else []) + ["keychain", "environment"]
    if provider_id in CLI_AUTH_PROVIDERS:
        modes.append("cli")
    return modes


def _credential_mode(provider_id: str, value) -> str:
    if value not in credential_modes(provider_id):
        raise ValueError(f"Unsupported credential source for {PROVIDERS[provider_id]['name']}.")
    return value


def _credential_revision(value) -> int:
    if (type(value) is not int or value < 0
            or value > MAX_CREDENTIAL_REVISION):
        raise ValueError("Provider credential revision must be a non-negative safe integer.")
    return value


#: One level of delegation by default: a top-level thread may spawn, its
#: children may not. Unbounded was never a considered choice - the flag
#: simply had no default - and unbounded is the one setting nobody wants
#: here, because every grandchild is another full-context streaming request
#: against the same provider quota. Depth 1 is the shape Ultra is useful in.
DEFAULT_SPAWN_DEPTH_LIMIT = 1


def _spawn_depth_limit(value):
    """Validate the per-provider subagent spawn-depth flag.

    Absent/None takes DEFAULT_SPAWN_DEPTH_LIMIT, 0 removes the spawn tool
    from every request on the route, and 1 removes it once the request
    already runs below the top level. Deeper limits are rejected: a
    depth-1 and depth-2 requester are indistinguishable from one
    request's history, so only 0 and 1 have enforceable meanings.
    """
    if value is None:
        return DEFAULT_SPAWN_DEPTH_LIMIT
    if type(value) is not int or value not in (0, 1):
        raise ValueError("spawn_depth_limit must be 0 (no subagent spawning) or 1 (children cannot spawn).")
    return value


def _idle_unload_seconds(provider_id: str, value):
    """Validate the post-turn keep_alive lease for a local model.

    Absent/None takes the module default, which is the point of the
    setting: a hub that never says anything leaves every model it touches
    resident for the daemon's own five minutes. ``0`` unloads the model
    as soon as its turn ends, and ``-1`` is Ollama's own "never expire"
    for a machine that would rather hold the weights than reload them.

    Only Ollama takes it. A hosted provider has no resident weights to
    lease, so accepting the field there would be a setting that silently
    does nothing.
    """
    if value is None:
        return None
    if provider_id != "ollama":
        raise ValueError("idle_unload_seconds applies to the Ollama connection; a hosted provider keeps no model resident.")
    if type(value) is not int or not KEEP_RESIDENT <= value <= MAX_LEASE_SECONDS:
        raise ValueError(f"idle_unload_seconds must be -1 (keep resident), 0 (unload when the turn ends), "
                         f"or up to {MAX_LEASE_SECONDS} seconds. The default is {DEFAULT_LEASE_SECONDS}.")
    return value


def qualify(provider_id: str, model_id: str) -> str:
    if provider_id not in PROVIDERS:
        raise ValueError("Unknown inference provider.")
    # For agent_session protocol (like Devin), model_id is actually a session_id or mode
    provider_protocol = PROVIDERS[provider_id].get("protocol", "chat_completions")
    if provider_protocol == "agent_session":
        # For agents, we use provider/mode or provider/session_id format
        if not isinstance(model_id, str) or not model_id.strip():
            raise ValueError("Enter a valid agent mode or session ID.")
        return provider_id + "/" + model_id
    if not isinstance(model_id, str) or not MODEL_ID.fullmatch(model_id):
        raise ValueError("Enter an exact model ID without whitespace.")
    return provider_id + "/" + model_id


def split_route(value: str) -> tuple[str, str]:
    if not isinstance(value, str):
        raise ValueError("Model route must be text.")
    value = value.strip()
    first, separator, remainder = value.partition("/")
    if separator and first in PROVIDERS:
        # Check if this is an agent provider
        provider_protocol = PROVIDERS[first].get("protocol", "chat_completions")
        if provider_protocol == "agent_session":
            # For agents, remainder can be mode, session_id, or task
            if not remainder or not remainder.strip():
                raise ValueError("Agent route must include a mode, session ID, or task.")
            return first, remainder
        else:
            qualify(first, remainder)
            return first, remainder
    # The v0.2 settings format contained bare Mistral model IDs.
    qualify("mistral", value)
    return "mistral", value


def connection_signature(provider_id: str, connection: dict) -> str:
    normalized = validate_connection(provider_id, connection)
    mode = _credential_mode(
        provider_id, connection.get("credential_mode", _default_credential_mode(provider_id)))
    revision = _credential_revision(connection.get("credential_revision", 0))
    value = json.dumps(
        [provider_id, normalized, mode, revision], sort_keys=True, separators=(",", ":")
    ).encode()
    return hashlib.sha256(value).hexdigest()[:20]


def _normalize_mapping_options(value, slots) -> dict:
    if value in (None, {}):
        return {}
    if not isinstance(value, dict):
        raise ValueError("Mapping options must be an object.")
    known = {slot[0] for slot in slots}
    result = {}
    for slot, options in value.items():
        if slot not in known:
            continue
        if not isinstance(options, dict):
            raise ValueError("Each mapping option must be an object.")
        unknown = set(options) - {"omit_system", "omit_tools", "compact_limit"}
        if unknown:
            raise ValueError("Unknown mapping option field.")
        omit_system = options.get("omit_system", False)
        omit_tools = options.get("omit_tools", False)
        if type(omit_system) is not bool or type(omit_tools) is not bool:
            raise ValueError("omit_system and omit_tools must be true or false.")
        limit = options.get("compact_limit")
        if limit is not None and (type(limit) is not int or not 1000 <= limit <= 15000000):
            raise ValueError("compact_limit must be an integer between 1000 and 15000000 tokens.")
        if omit_system or omit_tools or limit is not None:
            entry = {"omit_system": omit_system, "omit_tools": omit_tools}
            if limit is not None:
                entry["compact_limit"] = limit
            result[slot] = entry
    return result


# Opt-in Claude Desktop third-party profile features (see
# bridge_core.ClaudeProfile.prepare for the profile field each one sets).
CLAUDE_FEATURE_KEYS = ("dictation", "builtin_browser", "claude_in_chrome", "scheduled_tasks", "cowork_tab")

# The Claude Desktop model slots served in mapping mode: id, label, family
# tier and whether the row is that tier's default.
SLOTS = [
    ("claude-fable-5", "Fable 5", "fable", True),
    ("claude-opus-5", "Opus 5", "opus", True),
    ("claude-sonnet-5", "Sonnet 5", "sonnet", True),
    ("claude-haiku-4-5", "Haiku 4.5", "haiku", True),
    ("claude-sonnet-4-6", "Sonnet 4.6", "sonnet", False),
]

# Family tiers a curated Claude catalogue row can carry. Claude Desktop only
# lists a non-Claude model id when its row declares one of these, and the
# managed Claude Code borrows the tier's Claude model for capabilities and
# effort through the behavesAs row the hub writes (bridge_core.ClaudeProfile).
CLAUDE_TIERS = ("fable", "opus", "sonnet", "haiku")
CLAUDE_TIER_MODELS = {"fable": "claude-fable-5", "opus": "claude-opus-5",
                      "sonnet": "claude-sonnet-5", "haiku": "claude-haiku-4-5"}
# Which tier stands in when a catalogue has no row of the requested tier.
_TIER_FALLBACKS = {"fable": ("opus", "sonnet", "haiku"), "opus": ("fable", "sonnet", "haiku"),
                   "sonnet": ("opus", "fable", "haiku"), "haiku": ("sonnet", "opus", "fable")}
_ROW_SLUG_UNSAFE = re.compile(r"[^a-z0-9]+")
_ROW_SLUG_TIDY = re.compile(r"-{2,}")

#: Claude Desktop drops a picker row whose id names a third-party model family.
#: The check runs in two places and they do not agree: discovery keeps a row
#: that carries a valid `anthropic_family_tier` even when the id fails, then the
#: picker filter re-applies the same test with no tier escape and the row is
#: gone.  That is why an id-only problem reads as `Model discovery: 9 found;
#: picker = 0 (empty)` in ~/Library/Logs/Claude-3p/main.log while config health
#: still says `healthy` - nothing in the app names the id as the reason.
#:
#: Ported from the installed bundle's `Ixe` (Claude.app 2.2553.1, read from
#: app.asar on 18 Sep 2026).  It is a copy of someone else's list, so a Desktop
#: update can add a token this copy has never heard of; re-read it from the
#: bundle when the picker empties again.  Note `ollama` matches through `llama`.
_FOREIGN_FAMILY = re.compile(
    r"ark-code|astron|command-r|deepseek|doubao|gemini|gemma|glm|gpt|grok|hermes|hy3|kimi|lfm|\bling\b"
    r"|llama|longcat|mimo|minimax|mistral|mixtral|moonshot|nemotron|openai|phi-|qianfan|qwen|tc-code"
    r"|\bunic\b|yi-|stepfun|step-3|seed-|bytedance|hunyuan|granite|amazon\.nova|nova-|devstral"
    r"|ministral|ernie|codex|arcee|trinity|abab|phi\d|\bk2\.|\bm2\.|jamba|arctic|solar|mercury"
    r"|zamba|kat-coder|\bds-|dpsk"
)

#: A defusing pass inserts one hyphen per round, so the bound is only there to
#: stop a slug that cannot be defused from spinning; it falls back to a digest.
_MAX_DEFUSE_ROUNDS = 12


def _defuse_row_slug(slug: str):
    r"""Hyphenate the third-party family names out of a row slug, legibly.

    Desktop matches plain substrings, so one hyphen inside the word is enough,
    and splitting near the middle keeps the slug readable: `mistral-glm-5-2`
    becomes `mis-tral-g-lm-5-2`.  The split is not free of consequence - a
    hyphen makes a word boundary, and three of the denylist's entries want one
    (`\bds-`, `\bk2\.`, `\bm2\.`) - so each candidate is re-checked and the
    cut moved along until the occurrence is genuinely gone.  Returns None when
    no arrangement clears, which is the caller's cue to fall back to a digest.
    """
    for _ in range(_MAX_DEFUSE_ROUNDS):
        hit = _FOREIGN_FAMILY.search(slug)
        if hit is None:
            return slug
        start, width = hit.start(), hit.end() - hit.start()
        for cut in sorted(range(1, width), key=lambda c: (abs(c - width // 2), c)):
            candidate = _ROW_SLUG_TIDY.sub("-", f"{slug[:start + cut]}-{slug[start + cut:]}").strip("-")
            probe = _FOREIGN_FAMILY.search(candidate)
            if probe is None:
                return candidate
            if probe.start() > start:
                slug = candidate
                break
        else:
            return None
    return None


def _row_slug_digest(route: str) -> str:
    """A last-resort slug that cannot name a family: hex spells none of them.

    "abab" is the one denylist entry writable in hex, so the digest is rolled
    until it is clear rather than assumed to be.
    """
    digest = hashlib.blake2s(route.encode(), digest_size=8).hexdigest()
    while _FOREIGN_FAMILY.search(digest):
        digest = hashlib.blake2s(digest.encode(), digest_size=8).hexdigest()
    return digest[:8]


def _normalize_claude_catalogue(value) -> list[dict]:
    if not isinstance(value, list) or not value:
        raise ValueError("The Claude catalogue must be a non-empty list of model rows.")
    rows, seen = [], set()
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("Each Claude catalogue row must be an object.")
        if set(item) - {"route", "tier", "tier_default", "compact_limit"}:
            raise ValueError("Unknown Claude catalogue row field.")
        route = qualify(*split_route(item.get("route")))
        tier = item.get("tier")
        if tier not in CLAUDE_TIERS:
            raise ValueError("Each Claude catalogue row needs a family tier: fable, opus, sonnet or haiku.")
        flag = item.get("tier_default", False)
        if type(flag) is not bool:
            raise ValueError("tier_default must be true or false.")
        if route in seen:
            continue
        seen.add(route)
        row = {"route": route, "tier": tier, "tier_default": flag}
        limit = item.get("compact_limit")
        if limit is not None:
            if type(limit) is not int or not 1000 <= limit <= 15000000:
                raise ValueError("compact_limit must be an integer between 1000 and 15000000 tokens.")
            row["compact_limit"] = limit
        rows.append(row)
    # Exactly one default per tier present: the first flagged row, else the
    # first row of that tier.
    for tier in CLAUDE_TIERS:
        members = [row for row in rows if row["tier"] == tier]
        if not members:
            continue
        chosen = next((row for row in members if row["tier_default"]), members[0])
        for row in members:
            row["tier_default"] = row is chosen
    return rows


#: Every catalogue row id starts with this, whatever tier the row declares.
#: Desktop hands each picker row an effort descriptor from `rPt` (app.asar
#: @9878575), a compiled table keyed on the normalized id: eight literal
#: Anthropic ids, plus one wildcard `GNt` @9875965 =
#: /^(?:claude-)?(?:fable|mythos)(?:-|$)/ granting the full low..max ladder.
#: GNt is a PREFIX test, so it is the only key that tolerates a readable
#: suffix - which is why the ladder survives here and would not under an
#: opus/sonnet/haiku prefix.  A row whose id misses both loses the slider
#: entirely (the control is dropped, not disabled), silently, with config
#: health still green: that is how it was lost on 16 Sep.
#:
#: The cost is on the other side: the CLI's own prefix test collapses every
#: such id to `claude-fable-5`, so all rows inherit Fable's baked
#: capabilities - a leaner system-prompt bundle, no disabling of thinking,
#: and a 1M assumed window even for a smaller route.  The gateway's own
#: window check still enforces the real limit.  The row's true tier lives in
#: `anthropic_family_tier`, which is what drives picker grouping,
#: is_family_default and routing.
#:
#: One place still reads the id as a family and now reads every row as fable:
#: `protocol._family_slot`, the fallback that recovers a route for an id no
#: longer in the catalogue.  A stale id therefore lands on the fable tier
#: default and its mapping options rather than its own tier's.  That is a
#: degradation of an already-approximate rescue path, not a routing rule -
#: every live id is an exact key - but it is the reason this prefix is not
#: simply an opaque token.
CLAUDE_LADDER_PREFIX = "claude-fable-5"

#: The CLI resolves the longer id first, so a slug beginning "1" would read
#: as Fable 5.1 rather than Fable 5 and take a different capability set.
_LADDER_PREFIX_COLLISION = CLAUDE_LADDER_PREFIX + "-1"


def claude_row_id(route: str, tier: str) -> str:
    """The model id Claude Desktop and Claude Code see for a catalogue row.

    It starts with CLAUDE_LADDER_PREFIX so Desktop grants the row an effort
    ladder, then names the provider and model. The provider prefix is dropped
    when the model id already starts with it. The tier is not in the id; it
    travels in the row's `anthropic_family_tier` field.

    The name the provider uses cannot survive intact: Desktop refuses a picker
    row whose id spells a third-party family, so the slug is hyphenated until
    it no longer does (see _defuse_row_slug).  What the picker shows is the
    row's display_name, not this, so the rename costs nothing on screen.
    """
    provider_id, model_id = split_route(route)
    provider_slug = _ROW_SLUG_UNSAFE.sub("-", provider_id.casefold()).strip("-")
    model_slug = _ROW_SLUG_UNSAFE.sub("-", model_id.casefold()).strip("-")
    slug = model_slug if model_slug.startswith(provider_slug + "-") else f"{provider_slug}-{model_slug}"
    defused = _defuse_row_slug(slug)
    identifier = f"{CLAUDE_LADDER_PREFIX}-{defused}" if defused else None
    # The ladder prefix cannot contribute a family name on its own, but the
    # join is checked rather than trusted: a miss here empties the whole picker.
    if identifier is None or _FOREIGN_FAMILY.search(identifier):
        identifier = f"{CLAUDE_LADDER_PREFIX}-{_row_slug_digest(route)}"
    if identifier.startswith(_LADDER_PREFIX_COLLISION):
        # Push the slug off the collision without disturbing the prefix the
        # ladder test reads.
        identifier = f"{CLAUDE_LADDER_PREFIX}-r{identifier[len(CLAUDE_LADDER_PREFIX) + 1:]}"
    return identifier


def claude_catalogue_rows(settings: dict) -> list[dict]:
    """Served row plan for a curated Claude catalogue; empty in mapping mode."""
    rows, used = [], set()
    for entry in settings.get("claude_catalogue") or []:
        identifier = claude_row_id(entry["route"], entry["tier"])
        candidate, counter = identifier, 2
        while candidate in used:
            candidate, counter = f"{identifier}-{counter}", counter + 1
        used.add(candidate)
        rows.append({"id": candidate, **entry})
    return rows


def claude_picker_rows(settings: dict) -> list[dict]:
    """Group picker rows by account provider, retaining each provider's order.

    Allocate IDs in saved order before sorting so slug collisions keep their
    existing IDs. Routing and tier defaults still use claude_catalogue_rows;
    this order is only for Desktop discovery and managed Code picker options.
    Use the route provider, not the model maker's display branding, so models
    reached through a shared connection such as Ollama stay together.
    """
    return sorted(claude_catalogue_rows(settings),
                  key=lambda row: split_route(row["route"])[0].casefold())


def claude_routes(settings: dict) -> dict:
    """Model id -> provider route for whichever Claude mode is active.

    Mapping mode returns the slot mappings. Catalogue mode returns every
    generated row id plus the family slot ids pointing at their tier
    defaults, so the tier aliases and dated family ids Claude Code resolves
    on its own still reach a catalogue model.
    """
    rows = claude_catalogue_rows(settings)
    if not rows:
        return dict(settings.get("mappings") or {})
    routes = {row["id"]: row["route"] for row in rows}
    defaults = {row["tier"]: row["route"] for row in rows if row["tier_default"]}
    for slot, _, family, _ in SLOTS:
        stand_ins = (family, *_TIER_FALLBACKS[family])
        routes[slot] = next((defaults[tier] for tier in stand_ins if tier in defaults), rows[0]["route"])
    return routes


def defaults(slots, vibe_model: str, port: int = 11436) -> dict:
    connections = provider_defaults()
    for provider_id, connection in connections.items():
        connection["credential_mode"] = _default_credential_mode(provider_id)
        # The UI increments this non-secret generation after replacing or
        # removing a provider credential.  It lets account-scoped catalogues
        # fail stale without hashing or persisting any secret material.
        connection["credential_revision"] = 0
    return {"schema_version": 3, "port": port,
            "mappings": {slot[0]: qualify("mistral", vibe_model) for slot in slots},
            "mapping_options": {},
            "providers": connections, "branding_overrides": {},
            "auto_stop": True, "auto_mode": False, "codex_model": None,
            "codex_catalogue": None, "codex_chatgpt_account": False, "codex_apply_patch_all": False,
            "claude_features": {key: False for key in CLAUDE_FEATURE_KEYS},
            "claude_catalogue": None, "claude_code_settings": True, "claude_workflows": False,
            "codex_accent_slider": False, "codex_hide_usage_banner": False,
            "codex_unlock_composer": False, "codex_goal_budget": False,
            "codex_subagent_rank": None, "codex_subagent_route": None}


def normalize(value: dict, slots, vibe_model: str, port: int = 11436) -> dict:
    if not isinstance(value, dict):
        raise ValueError("Settings must be a JSON object.")
    schema_version = value.get("schema_version", 0)
    if type(schema_version) is not int or schema_version < 0:
        raise ValueError("Settings schema_version must be a non-negative integer.")
    if schema_version > 3:
        raise ValueError("These settings belong to a newer version of Provider Hub.")
    legacy_mode = value.get("credential_mode")
    if "credential_mode" in value and legacy_mode not in {"vibe", "separate"}:
        raise ValueError("Choose Vibe credentials or a separate API key.")
    result = defaults(slots, vibe_model, port)
    requested_port = value.get("port", result["port"])
    if type(requested_port) is not int or not 1024 <= requested_port <= 65535:
        raise ValueError("Choose a port between 1024 and 65535.")
    result["port"] = requested_port
    mappings = value.get("mappings", result["mappings"])
    if not isinstance(mappings, dict):
        raise ValueError("Model mappings must be an object.")
    for slot, *_ in slots:
        if slot not in mappings:
            raise ValueError("Choose a model for every Claude slot.")
        result["mappings"][slot] = qualify(*split_route(mappings[slot]))
    result["mapping_options"] = _normalize_mapping_options(value.get("mapping_options", {}), slots)
    configured = value.get("providers", {})
    if not isinstance(configured, dict):
        raise ValueError("Provider connections must be an object.")
    if set(configured) - set(PROVIDERS):
        raise ValueError("The settings contain an unknown provider connection.")
    for provider_id, baseline in result["providers"].items():
        requested = configured.get(provider_id, {})
        if not isinstance(requested, dict):
            raise ValueError("Provider connection must be an object.")
        if any(key in requested for key in ("api_key", "apiKey", "token", "password", "headers")):
            raise ValueError("API keys belong in Keychain, not the settings file.")
        merged = {**baseline, **requested}
        if "region" in requested and "base_url" not in requested:
            merged.pop("base_url", None)
        connection = validate_connection(provider_id, merged)
        mode = requested.get("credential_mode", baseline["credential_mode"])
        if provider_id == "mistral" and "credential_mode" in value and not requested:
            mode = "vibe" if legacy_mode == "vibe" else "keychain"
        connection["credential_mode"] = _credential_mode(provider_id, mode)
        connection["credential_revision"] = _credential_revision(
            requested.get("credential_revision", baseline["credential_revision"]))
        connection["spawn_depth_limit"] = _spawn_depth_limit(
            requested.get("spawn_depth_limit", baseline.get("spawn_depth_limit")))
        lease = _idle_unload_seconds(provider_id, requested.get("idle_unload_seconds", baseline.get("idle_unload_seconds")))
        if lease is not None:
            connection["idle_unload_seconds"] = lease
        result["providers"][provider_id] = connection
    result["branding_overrides"] = validate_overrides(value.get("branding_overrides", {}))
    if value.get("codex_model") is not None:
        result["codex_model"] = qualify(*split_route(value["codex_model"]))
    catalogue = value.get("codex_catalogue")
    if catalogue is not None:
        if not isinstance(catalogue, list) or not catalogue:
            raise ValueError("The Codex catalogue selection must be a non-empty list of model routes.")
        selected = []
        for route in catalogue:
            qualified = qualify(*split_route(route))
            if qualified not in selected:
                selected.append(qualified)
        result["codex_catalogue"] = selected
        if result["codex_model"] is not None and result["codex_model"] not in selected:
            raise ValueError("Choose the Codex default model from the catalogue selection.")
    # codex_subagent_rank orders preferred routes first in the global
    # delegation catalogue. The native description lists five recommendations;
    # subagent_catalogue adds the remaining eligible routes. An absent rank
    # follows the ranked routes; it does not make the model unavailable.
    ranks = value.get("codex_subagent_rank")
    if ranks is not None:
        if not isinstance(ranks, dict):
            raise ValueError("The Codex sub-agent ranks must be a mapping of model route to rank.")
        ranked = {}
        for route, rank in ranks.items():
            # bool is an int subclass and would silently rank a route at 1.
            if type(rank) is not int or not 1 <= rank <= SUBAGENT_POOL_SIZE:
                raise ValueError(f"A Codex sub-agent rank must be a whole number from 1 to {SUBAGENT_POOL_SIZE}.")
            ranked[qualify(*split_route(route))] = rank
        result["codex_subagent_rank"] = ranked or None
    # codex_subagent_route is the model a spawned sub-agent runs on when the
    # parent does not name one itself. Codex has a configuration key for this,
    # default_subagent_model, and on 26.908 it is inert - a real spawn with it
    # set still brings the child up on the parent's model - so the choice is
    # written into the spawn call's own `model` argument on the way out
    # instead (see spawn_depth.apply_subagent_model). Absent means the child
    # inherits its parent, which is Codex's own default.
    route = value.get("codex_subagent_route")
    if route is not None:
        result["codex_subagent_route"] = qualify(*split_route(route))
    # codex_apply_patch qualifies routes for the JSON-wrapped apply_patch
    # projection one by one; codex_apply_patch_exclude holds routes back once
    # codex_apply_patch_all switches the projection on for the whole
    # catalogue (see codex_catalogue.apply_patch_qualified).
    for key, label in (("codex_apply_patch", "qualification"), ("codex_apply_patch_exclude", "exclusion")):
        routes = value.get(key)
        if routes is None:
            continue
        if not isinstance(routes, list):
            raise ValueError(f"The Codex apply_patch {label} must be a list of model routes.")
        qualified_routes = []
        for route in routes:
            qualified = qualify(*split_route(route))
            if qualified not in qualified_routes:
                qualified_routes.append(qualified)
        result[key] = qualified_routes
    requested_features = value.get("claude_features", {})
    if not isinstance(requested_features, dict) or set(requested_features) - set(CLAUDE_FEATURE_KEYS):
        raise ValueError("Claude feature toggles must be an object with known keys.")
    for key in CLAUDE_FEATURE_KEYS:
        flag = requested_features.get(key, False)
        if type(flag) is not bool:
            raise ValueError(f"Claude feature {key} must be true or false.")
        result["claude_features"][key] = flag
    # claude_catalogue replaces the slot mappings with curated, tier-tagged
    # rows when present (see claude_routes); the mappings stay saved for a
    # switch back.
    if value.get("claude_catalogue") is not None:
        result["claude_catalogue"] = _normalize_claude_catalogue(value["claude_catalogue"])
    # codex_chatgpt_account: present the user's ChatGPT sign-in to the Codex
    # desktop while the hub provider is active (see CodexProfile.provider).
    # codex_apply_patch_all: the Codex tab's apply_patch switch.
    # claude_code_settings: write behavesAs rows for catalogue ids into
    # ~/.claude/settings.json while the Claude profile is active.
    # claude_workflows: also enable Claude Code dynamic workflows there, which
    # the Ultracode effort level needs.
    # codex_accent_slider: launch Codex through the DevTools-pipe helper that
    # tints its power slider per model (see codex_accent).
    # codex_hide_usage_banner: that helper's stylesheet also hides the app's
    # ChatGPT usage banner.
    # codex_unlock_composer: that helper's watcher also keeps the composer's
    # send button usable for hub routes once the ChatGPT plan's usage is
    # exhausted (see codex_accent).
    # codex_goal_budget: leave the optional token budget on Codex's goal tools.
    # Off - the default - deletes the property, so a goal starts unlimited on
    # every route instead of wherever the model honoured Codex's "omit unless
    # explicitly requested" (see responses_tools.strip_goal_budget).
    for key, fallback in (("auto_stop", True), ("auto_mode", False), ("codex_chatgpt_account", False),
                          ("codex_apply_patch_all", False), ("claude_code_settings", True),
                          ("claude_workflows", False), ("codex_accent_slider", False),
                          ("codex_hide_usage_banner", False), ("codex_unlock_composer", False),
                          ("codex_goal_budget", False)):
        requested = value.get(key, fallback)
        if type(requested) is not bool:
            raise ValueError(f"{key} must be true or false.")
        result[key] = requested
    return result


def _model_variant_key(item: dict) -> tuple[str, str]:
    canonical = item.get("canonical_id")
    if not isinstance(canonical, str) or not canonical:
        canonical = item["id"]
    try:
        variant = json.dumps(
            [[field, item.get(field)] for field in MODEL_VARIANT_FIELDS],
            sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("Provider catalogue capability metadata must be JSON data.") from exc
    return canonical, variant


def _catalogue_groups(provider_id: str, inventory: dict) -> list[dict]:
    """Return deterministic model groups with cross-group aliases removed."""
    if provider_id not in PROVIDERS:
        raise ValueError("Unknown inference provider.")
    claimed_provider = inventory.get("provider_id")
    if claimed_provider not in (None, provider_id):
        raise ValueError("Provider catalogue identity does not match its connection.")
    models = inventory.get("models", [])
    if not isinstance(models, list):
        raise ValueError("Provider catalogue models must be an array.")

    grouped = {}
    for index, source in enumerate(models):
        if not isinstance(source, dict):
            raise ValueError(f"Provider catalogue model {index} must be an object.")
        item = copy.deepcopy(source)
        identifier = item.get("id")
        try:
            qualify(provider_id, identifier)
        except ValueError as exc:
            raise ValueError(f"Provider catalogue model {index} has an invalid id.") from exc
        entry_provider = item.get("provider_id")
        if entry_provider not in (None, provider_id):
            raise ValueError("Provider catalogue model identity does not match its connection.")
        aliases = item.get("aliases") or []
        if not isinstance(aliases, list):
            raise ValueError(f"Provider catalogue model {identifier} aliases must be an array.")
        raw_ids = {identifier}
        for alias in aliases:
            try:
                qualify(provider_id, alias)
            except ValueError as exc:
                raise ValueError(
                    f"Provider catalogue model {identifier} has an invalid alias.") from exc
            raw_ids.add(alias)
        key = _model_variant_key(item)
        grouped.setdefault(key, []).append((item, raw_ids))

    raw_owners = {}
    alias_claims = {}
    for key, entries in grouped.items():
        for item, raw_ids in entries:
            raw_owners.setdefault(item["id"], set()).add(key)
            for identifier in raw_ids:
                alias_claims.setdefault(identifier, set()).add(key)
    conflicting_raw = sorted(
        identifier for identifier, owners in raw_owners.items() if len(owners) > 1)
    if conflicting_raw:
        raise ValueError(
            f"Provider catalogue gives model id {conflicting_raw[0]!r} conflicting specifications.")

    output = []
    for key, entries in sorted(grouped.items(), key=lambda pair: pair[0]):
        entries = sorted(
            entries,
            key=lambda pair: (
                pair[0]["id"],
                json.dumps(pair[0], sort_keys=True, separators=(",", ":"), ensure_ascii=False),
            ),
        )
        raw_ids = {item["id"] for item, _ in entries}
        all_ids = set().union(*(identifiers for _, identifiers in entries))
        kept = []
        dropped = set()
        for identifier in sorted(all_ids):
            claimants = alias_claims.get(identifier, {key})
            owners = raw_owners.get(identifier, set())
            if len(claimants) == 1 or key in owners:
                kept.append(identifier)
            else:
                dropped.add(identifier)
        for item, _ in entries:
            removed = item.get("ambiguous_aliases_removed") or []
            if isinstance(removed, list):
                dropped.update(value for value in removed if isinstance(value, str))
        output.append({
            "key": key,
            "entries": entries,
            "raw_ids": raw_ids,
            "kept": kept,
            "dropped": sorted(dropped),
        })
    return output


def _claude_cli_inventory(inventory: dict) -> dict:
    """Upgrade cached CLI seeds without changing their freshness or account.

    Short aliases remain accepted route IDs but share one versioned picker
    entry. Preserve exact-version metadata and any unknown/custom CLI models.
    """
    # Validate the original snapshot before replacing any of its seed rows.
    _catalogue_groups("claude", inventory)
    from claude_cli_agent import KNOWN_MODELS
    from cli_routes import _hub_row

    originals = {row["id"]: row for row in inventory.get("models", [])}
    models = [copy.deepcopy(row) for row in inventory.get("models", [])
              if row["id"] not in CLAUDE_MODEL_LABELS and row["id"] not in CLAUDE_CLI_ALIASES]
    for seed in KNOWN_MODELS:
        identifier = seed["id"]
        aliases = [alias for alias, target in CLAUDE_CLI_ALIASES.items() if target == identifier]
        row = _hub_row("claude", seed)
        # Only exact-version records supply capability metadata. Moving alias
        # metadata may have described an older model when it was captured.
        row.update(copy.deepcopy(originals.get(identifier, {})))
        row.update(id=identifier, canonical_id=identifier, display_name=seed["display_name"])
        row["aliases"] = list(dict.fromkeys([identifier, *(row.get("aliases") or []), *aliases]))
        models.append(row)
    return {**inventory, "models": models}


def project_catalogue(provider_id: str, inventory: dict, settings: dict, observations=None) -> list[dict]:
    """Qualify raw provider IDs once and attach validated presentation data."""
    observations = observations or {}
    output = []
    claude_cli = provider_id == "claude" and inventory.get("source") == "cli"
    if claude_cli:
        inventory = _claude_cli_inventory(inventory)
    selected = [split_route(route)[1] for route in claude_routes(settings).values() if split_route(route)[0] == provider_id]
    if claude_cli:
        # Keep saved Codex defaults/curated routes usable as well as Claude
        # selections. A fresh selection uses the versioned ID from the seed.
        codex_routes = [settings.get("codex_model"), *(settings.get("codex_catalogue") or [])]
        selected = [split_route(route)[1] for route in codex_routes
                    if route and split_route(route)[0] == provider_id] + selected
    for group in _catalogue_groups(provider_id, inventory):
        canonical = group["key"][0]
        preferred = next((identifier for identifier in selected if identifier in group["kept"]), None)
        if preferred is None:
            preferred = canonical if canonical in group["raw_ids"] else min(group["raw_ids"])
        source, _ = next(
            ((item, identifiers) for item, identifiers in group["entries"]
             if item["id"] == preferred),
            next(((item, identifiers) for item, identifiers in group["entries"]
                  if preferred in identifiers), group["entries"][0]),
        )
        item = copy.deepcopy(source)
        # Upgrade older saved provider inventories as well as fresh discovery:
        # picker metadata must follow the currently documented exact ID.
        if provider_id in {"codex", "claude"}:
            item["fast_mode"] = supports_fast_toggle(provider_id, preferred)
        fixed_tier = fixed_speed_tier(provider_id, preferred)
        if fixed_tier is not None:
            item["speed_tier"] = fixed_tier
            item["fast_mode"] = False
        if provider_id == "mistral":
            # The cached discovery snapshot is trusted verbatim downstream
            # (Hub UI, Codex projection, gateway request specs), so snapshot
            # bytes from before the per-model ladders would otherwise keep
            # overriding them without any refresh. Re-derive authoritatively
            # from every name the card carries.
            item["effort_modes"] = mistral_ladder_for_model(
                preferred, item.get("reasoning"),
                (item.get("canonical_id"), item.get("billing_model_name"),
                 item.get("display_name")))
        route = qualify(provider_id, preferred)
        overrides = settings.get("branding_overrides", {})
        labels = overrides.get(provider_id, {}).get("modelLabels", {})
        override_label = labels.get(preferred) or labels.get(route)
        advertised = item.get("display_name")
        canonical = item.get("canonical_id") or preferred
        if provider_id == "claude":
            advertised = claude_model_label(canonical) or claude_model_label(preferred) or advertised
        # A moving Mistral alias can still advertise a resolved, versioned
        # billing-model identifier. Use it for presentation, never for routing.
        label_identifier = canonical
        billing_model = item.get("billing_model_name")
        if (provider_id == "mistral" and canonical not in PINNED_LABELS
                and isinstance(billing_model, str) and MODEL_ID.fullmatch(billing_model)
                and re.search(r"(?:^|[-_.])\d", billing_model)):
            label_identifier = billing_model
        # Repeated raw IDs are not human labels and must not suppress known
        # product names such as Mistral Small 4 or Large 3.
        friendly = advertised if advertised and advertised not in {item["id"], canonical, label_identifier} else friendly_model_name(label_identifier)
        presentation = resolve_presentation(
            provider_id, preferred, overrides, override_label or friendly)
        item.update(id=route, model_id=preferred, provider_id=provider_id,
                    aliases=[route] + [qualify(provider_id, identifier)
                                       for identifier in group["kept"] if identifier != preferred],
                    display_name=presentation.get("modelLabel", item.get("display_name", preferred)),
                    presentation=presentation,
                    discovery_source=inventory.get("source", item.get("source", "provider")))
        if group["dropped"]:
            item["ambiguous_aliases_removed"] = group["dropped"]
        else:
            item.pop("ambiguous_aliases_removed", None)
        observation = observations.get(route, {})
        if observation:
            item["inference_status"] = observation.get("status", "advertised")
            item["last_success"] = observation.get("last_success")
        else:
            item.setdefault("inference_status", "advertised")
        output.append(item)
    return sorted(output, key=lambda item: (item["display_name"].casefold(), item["id"]))


def provider_presentations(settings: dict) -> list[dict]:
    return [{**descriptor, "id": provider_id,
             "credential_modes": credential_modes(provider_id),
             "presentation": resolve_presentation(provider_id, overrides=settings.get("branding_overrides"))}
            for provider_id, descriptor in PROVIDERS.items()]


def provider_display_name(settings: dict, provider_id: str) -> str:
    """The provider as the Providers pane lists it, user renames included."""
    return resolve_presentation(provider_id, overrides=settings.get("branding_overrides"))["displayProvider"]


def provider_label(settings: dict, provider_id: str) -> str:
    """How launch and refresh messages name a provider for its credential source.

    The registry name names the provider's API ("Codex (OpenAI API)"), which is
    right for every key-based source. The installed-CLI source runs the
    vendor's own binary and login instead, so it is named the way the Providers
    pane shows it: the listed provider name and its "Installed CLI" source.
    """
    connection = (settings.get("providers") or {}).get(provider_id) or {}
    if connection.get("credential_mode") != "cli":
        return PROVIDERS[provider_id]["name"]
    return f"{provider_display_name(settings, provider_id)} (installed CLI)"
