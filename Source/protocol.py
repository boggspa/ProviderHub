"""Messages/Chat Completions response translation and legacy Mistral request helpers.

The desktop remains the tool executor. This module never executes tool calls.
Unsupported content/tools fail explicitly instead of silently disappearing.
"""
from __future__ import annotations

import collections
import copy
import hashlib
import json
import math
import re
import secrets

from bridge_core import BridgeError, SLOTS
from hub_config import claude_catalogue_rows, claude_routes
from model_names import friendly_model_name
from catalogue import fits_desktop_baseline, status_label
from effort_map import MISTRAL_EFFORT_ALIASES, MISTRAL_REASONING_EFFORTS, cap_high_end, map_effort, mistral_effort_modes
from chat_tool_order import repair_openai_tool_order


def tool_id(value: str) -> str:
    # Mistral tool call IDs require nine alphanumeric characters. The same ID
    # is reconstructed from the complete conversation on every request.
    return hashlib.sha256(value.encode()).hexdigest()[:9]


def function_name(name: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name):
        return name
    return re.sub(r"[^A-Za-z0-9_-]", "_", name)[:50] + "_" + hashlib.sha256(name.encode()).hexdigest()[:10]


def blocks(value):
    if isinstance(value, str):
        return [{"type": "text", "text": value}]
    if isinstance(value, list) and all(isinstance(b, dict) for b in value):
        return value
    raise BridgeError("Message content must be text or an array of content blocks.")


def content_part(block):
    kind = block.get("type")
    if kind == "text":
        return {"type": "text", "text": block.get("text", "")}
    if kind == "image":
        source = block.get("source", {})
        if source.get("type") == "base64":
            media = source.get("media_type", "")
            if media not in {"image/png", "image/jpeg", "image/webp", "image/gif"}:
                raise BridgeError("Unsupported image format.")
            return {"type": "image_url", "image_url": "data:" + media + ";base64," + source.get("data", "")}
        if source.get("type") == "url" and str(source.get("url", "")).startswith("https://"):
            return {"type": "image_url", "image_url": source["url"]}
        raise BridgeError("Images must be base64 data or HTTPS URLs.")
    if kind == "document":
        source = block.get("source", {})
        if source.get("type") == "text":
            return {"type": "text", "text": source.get("data", "")}
        if source.get("type") == "content":
            return {"type": "text", "text": "\n".join(b["text"] for b in blocks(source.get("content", [])) if b.get("type") == "text")}
        raise BridgeError("PDF/document uploads are not supported by this prototype. Use extracted text or images.")
    # We do not generate Anthropic thinking/signatures. Replayed externally
    # signed reasoning must not be sent to a different provider.
    if kind in {"thinking", "redacted_thinking"}:
        return None
    raise BridgeError(f"Unsupported content block: {kind or 'missing type'}.")


def compact_content(parts):
    if all(p.get("type") == "text" for p in parts):
        return "\n".join(p.get("text", "") for p in parts)
    return list(parts)


SLOT_ALIASES = {
    "fable": "claude-fable-5",
    "opus": "claude-opus-5",
    "sonnet": "claude-sonnet-5",
    "haiku": "claude-haiku-4-5",
}

# Claude Code sends some family ids to a gateway with a release date appended
# (claude-haiku-4-5-20251001 for the claude-haiku-4-5 row). The exact id is
# always tried first, so an upstream id that happens to end in a date still
# resolves as itself.
_DATED_SUFFIX = re.compile(r"-20\d{6}$")


def _model_id_candidates(requested: str):
    if requested.endswith("[1m]"):
        requested = requested[:-4]
    yield requested
    undated = _DATED_SUFFIX.sub("", requested)
    if undated != requested:
        yield undated


# A Claude family id the hub does not list itself (an older Sonnet the CLI
# falls back to, a future dated build) still lands on that family's slot.
_FAMILY_ID = re.compile(r"^claude-(fable|mythos|opus|sonnet|haiku)-")


def _family_slot(requested: str) -> str | None:
    base = requested[:-4] if requested.endswith("[1m]") else requested
    match = _FAMILY_ID.match(base)
    if not match:
        return None
    family = "opus" if match.group(1) == "mythos" else match.group(1)
    return SLOT_ALIASES[family]


def resolve_model(requested: str, mappings: dict) -> str:
    for candidate in _model_id_candidates(requested):
        if candidate in mappings:
            return mappings[candidate]
        if candidate in SLOT_ALIASES:
            return mappings[SLOT_ALIASES[candidate]]
        # Only explicitly configured upstream IDs are callable through this gateway.
        if candidate in mappings.values():
            return candidate
    slot = _family_slot(requested)
    if slot in mappings:
        return mappings[slot]
    raise BridgeError(f"Model {requested!r} is not mapped. Choose it in Provider Hub first.")


def resolve_mapping_slot(requested, mappings: dict) -> str | None:
    if not isinstance(requested, str) or not isinstance(mappings, dict):
        return None
    for candidate in _model_id_candidates(requested):
        if candidate in mappings:
            return candidate
        if candidate in SLOT_ALIASES:
            slot = SLOT_ALIASES[candidate]
            return slot if slot in mappings else None
        matches = [slot for slot, route in mappings.items() if route == candidate]
        if len(matches) == 1:
            return matches[0]
    slot = _family_slot(requested)
    return slot if slot in mappings else None


def mapping_options_for(requested, settings: dict) -> dict:
    options = {"omit_system": False, "omit_tools": False, "compact_limit": None}
    if not isinstance(settings, dict):
        return options
    rows = claude_catalogue_rows(settings)
    if rows:
        # Catalogue rows carry only a compaction threshold; the omit switches
        # belong to the slot table. Family ids and aliases follow the tier
        # default they resolve to.
        if isinstance(requested, str):
            try:
                route = resolve_model(requested, claude_routes(settings))
            except BridgeError:
                return options
            limits = [row["compact_limit"] for row in rows
                      if row["route"] == route and type(row.get("compact_limit")) is int and row["compact_limit"] > 0]
            if limits:
                options["compact_limit"] = min(limits)
        return options
    mappings = settings.get("mappings") or {}
    slot = resolve_mapping_slot(requested, mappings)
    if slot is None:
        # A bare route shared by several slots is ambiguous for omit flags,
        # which stay strict so ambiguity never drops harness fields. A
        # compaction threshold can still apply safely: use the smallest one.
        if isinstance(requested, str):
            base = requested[:-4] if requested.endswith("[1m]") else requested
            configured_all = settings.get("mapping_options") or {}
            limits = []
            for entry_slot, route in mappings.items():
                entry = configured_all.get(entry_slot)
                if route == base and isinstance(entry, dict):
                    limit = entry.get("compact_limit")
                    if type(limit) is int and limit > 0:
                        limits.append(limit)
            if limits:
                options["compact_limit"] = min(limits)
        return options
    configured = (settings.get("mapping_options") or {}).get(slot) or {}
    if configured.get("omit_system") is True:
        options["omit_system"] = True
    if configured.get("omit_tools") is True:
        options["omit_tools"] = True
    limit = configured.get("compact_limit")
    if type(limit) is int and limit > 0:
        options["compact_limit"] = limit
    return options


def apply_mapping_options(payload: dict, settings: dict) -> dict:
    if not isinstance(payload, dict):
        return payload
    options = mapping_options_for(payload.get("model"), settings)
    if not options["omit_system"] and not options["omit_tools"]:
        return payload
    result = dict(payload)
    if options["omit_system"]:
        result.pop("system", None)
    if options["omit_tools"]:
        result.pop("tools", None)
        result.pop("tool_choice", None)
    return result


def _effective_context(spec: dict) -> int | None:
    """Return the effective context window for a route spec.

    For routes with a single numeric context, return that.
    For plan-dependent routes (e.g. Kimi K3 with context_options),
    return the maximum advertised window so the gateway can advertise
    the 1M variant when any plan offers 1M.
    """
    context = spec.get("context")
    if type(context) is int:
        return context
    # context_options: list of available windows for the same route
    options = spec.get("context_options")
    if isinstance(options, list) and options:
        return max(options)
    return None


ContextClaim = collections.namedtuple("ContextClaim", "kind floor ceiling")


def stated_context(spec: dict):
    """What can honestly be claimed about a route's context window.

    Returns ContextClaim(kind, floor, ceiling): ("exact", n, n) for a single
    documented window, ("range", floor, ceiling) where the window follows the
    account, or ("unknown", None, None) where nothing is sourceable.

    A range rather than a bare floor because the floor alone under-claims: a
    K3 route on a larger membership really does have the ceiling, and Kimi
    publishes k3-256k separately for anyone who wants the guarantee. Telling a
    model the span it might have, and that the span depends on the account,
    leaves it able to plan conservatively without pretending capacity it may
    own does not exist.

    _effective_context answers a different question - how much room to plan
    for - and resolves an ambiguous route by taking the largest window it
    might have. That is the right bias for compaction, which fails safe by
    trimming early, and the wrong one for telling a model what it has: an
    account-dependent route would be told the ceiling and would plan around
    capacity it may not own. K3 is the live case, carrying
    context_options [262144, 1048576] because the window follows the
    membership, and Cerebras publishes one window per tier while reporting
    neither. Both resolve to a floor here and to a maximum there, on purpose.
    """
    options = spec.get("context_options") if isinstance(spec, dict) else None
    ranked = sorted({value for value in (options or []) if type(value) is int and value > 0})
    if len(ranked) > 1:
        return ContextClaim("range", ranked[0], ranked[-1])
    context = spec.get("context") if isinstance(spec, dict) else None
    if type(context) is int and context > 0:
        return ContextClaim("exact", context, context)
    if ranked:
        return ContextClaim("exact", ranked[0], ranked[0])
    return ContextClaim("unknown", None, None)


def model_catalog(settings: dict):
    rows, seen = [], set()
    catalogue = claude_catalogue_rows(settings)
    # Catalogue mode serves one tier-tagged row per curated route under its
    # generated id; mapping mode serves the five family slots.
    plan = ([(row["id"], row["route"], row["tier"], row["tier_default"]) for row in catalogue] if catalogue
            else [(slot, settings["mappings"][slot], family, default) for slot, _, family, default in SLOTS])
    for slot, identifier, family, default in plan:
        spec = settings.get("_model_specs", {}).get(identifier)
        if not spec:
            continue
        key = (spec.get("provider_id", "mistral"), spec["id"])
        if key in seen:
            continue
        seen.add(key)
        effective_context = _effective_context(spec)
        context_text = f"{effective_context:,} token context" if type(effective_context) is int else "Provider-managed context"
        provider = spec.get("presentation", {}).get("displayProvider")
        title = spec["display_name"]
        if provider and provider.casefold() not in title.casefold():
            title += " · " + provider
        # Advertise a single [1m]-suffixed slot id for routes whose effective
        # context >= 1M, so Desktop shows one picker row metered at 1M instead
        # of synthesizing a separate 1M variant row (which doubles the offerings).
        row_slot = slot
        if type(effective_context) is int and effective_context >= 1000000:
            row_slot = f"{slot}[1m]"
        row = {"id": row_slot, "type": "model", "display_name": title,
               "description": f"{spec.get('provider_id', 'mistral')} account · {context_text} · {status_label(spec)}",
               "created_at": "2026-09-12T00:00:00Z",
               "anthropic_family_tier": family, "is_family_default": default,
               "fits_desktop_baseline": fits_desktop_baseline(effective_context)}
        if catalogue:
            row["description"] += f" · {family} tier"
        if fits_desktop_baseline(effective_context) is False:
            row["description"] += " · below desktop baseline"
        if type(effective_context) is int:
            # Use the per-model auto-compact threshold as max_input_tokens so
            # Claude Desktop compacts at the hub's catalogue boundary (typically
            # 85%% of context) instead of waiting for the full window.  The
            # gateway's server-side compaction handles the remaining headroom.
            compact_limit = spec.get("auto_compact_token_limit")
            input_limit = compact_limit if type(compact_limit) is int and 0 < compact_limit < effective_context else effective_context
            # supports_1m is deliberately false even for 1M routes: Desktop folds
            # a supports_1m:true entry into a bare + [1m] pair, and the id here
            # already carries the [1m] suffix (row_slot above) as the single 1M
            # offering. Keeping it false stops the second, smaller-window row.
            row.update(max_tokens=effective_context, max_input_tokens=input_limit, supports_1m=False)
        rows.append(row)
    return {"data": rows, "first_id": rows[0]["id"] if rows else None,
            "last_id": rows[-1]["id"] if rows else None, "has_more": False}


def model_effort(payload: dict, spec: dict):
    if not spec.get("reasoning"):
        return None
    if (payload.get("thinking") or {}).get("type") == "disabled":
        return "none"
    requested = (payload.get("output_config") or {}).get("effort")
    supported = list(spec.get("effort_modes") or []) or mistral_effort_modes(spec.get("id") or "", spec.get("reasoning"))
    if requested is None:
        return "high" if "high" in supported else (supported[-1] if supported else None)
    normalized = map_effort(requested, supported, MISTRAL_EFFORT_ALIASES)
    if normalized is None:
        normalized = cap_high_end(MISTRAL_EFFORT_ALIASES.get(requested), supported)
    if normalized is None:
        raise BridgeError("Unsupported effort value. Use Claude's standard effort control.")
    return normalized


def estimated_tokens(payload: dict) -> int:
    # Local estimate, explicitly labelled in the HTTP response and app docs.
    # Includes system instructions and schemas; not used for usage billing.
    images = 0
    def text_only(value):
        nonlocal images
        if isinstance(value, dict):
            if value.get("type") == "image":
                images += 1
                return {"type": "image"}
            return {key: text_only(item) for key, item in value.items()}
        if isinstance(value, list):
            return [text_only(item) for item in value]
        return value
    subset = text_only({k: payload[k] for k in ("system", "messages", "tools") if k in payload})
    return max(1, math.ceil(len(json.dumps(subset, ensure_ascii=False).encode()) / 3) + images * 4096)


# Claude Desktop / managed Claude Code injects standalone
# <total_tokens> / <ctx_window> reminder lines. Default padded-countdown
# mode is a 15,000,000-token task budget that re-anchors on every user
# turn, so the figure is not the selected provider's context window.
# When the catalogue has a known integer context, rewrite matching
# standalone lines to remaining provider context. Unknown-context routes
# and fenced code are left unchanged. The desktop's own session meter is
# separate and is not modified here.
_CONTEXT_REMINDER_TAGS = ("<total_tokens>", "<ctx_window>")
_FENCE_LINE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_CONTEXT_REMINDER_LINE = re.compile(
    r"^( {0,3})<(total_tokens|ctx_window)>"
    r"(?:-?\d{1,12}|Infinite) tokens left"
    r"(, \d{1,12}/\d{1,12} used)?"
    r"</\2>"
    r"([ \t]*)$"
)
_MAX_REMINDER_DIGITS = 12


def _contains_context_reminder(value) -> bool:
    if isinstance(value, str):
        return any(tag in value for tag in _CONTEXT_REMINDER_TAGS)
    if isinstance(value, list):
        return any(_contains_context_reminder(item) for item in value)
    if isinstance(value, dict):
        return any(
            _contains_context_reminder(item)
            for key, item in value.items()
            if key != "data"
        )
    return False


def _rewrite_reminder_text(text: str, remaining: int, used: int, window: int) -> tuple[str, bool]:
    if not isinstance(text, str) or not _contains_context_reminder(text):
        return text, False
    lines = text.split("\n")
    fence = None
    changed = False
    rewritten = []
    for line in lines:
        raw = line[:-1] if line.endswith("\r") else line
        marker = _FENCE_LINE.match(raw)
        if marker:
            token = marker.group(1)
            if fence is None:
                fence = token
            elif token[0] == fence[0] and len(token) >= len(fence):
                fence = None
            rewritten.append(line)
            continue
        match = None if fence is not None else _CONTEXT_REMINDER_LINE.match(raw)
        if match is None:
            rewritten.append(line)
            continue
        indent, tag, used_suffix, trailing = match.group(1), match.group(2), match.group(3), match.group(4)
        body = f"{remaining} tokens left"
        if used_suffix:
            body += f", {used}/{window} used"
        replacement = f"{indent}<{tag}>{body}</{tag}>{trailing}"
        if line.endswith("\r"):
            replacement += "\r"
        if replacement != line:
            changed = True
        rewritten.append(replacement)
    return "\n".join(rewritten) if changed else text, changed


def _rewrite_reminder_block(block: dict, remaining: int, used: int, window: int) -> bool:
    kind = block.get("type")
    if kind == "text" and isinstance(block.get("text"), str):
        rewritten, changed = _rewrite_reminder_text(block["text"], remaining, used, window)
        if changed:
            block["text"] = rewritten
        return changed
    if kind == "tool_result" and "content" in block:
        content = block["content"]
        if isinstance(content, str):
            rewritten, changed = _rewrite_reminder_text(content, remaining, used, window)
            if changed:
                block["content"] = rewritten
            return changed
        return _rewrite_reminder_content(content, remaining, used, window)
    return False


def _rewrite_reminder_content(value, remaining: int, used: int, window: int) -> bool:
    if not isinstance(value, list):
        return False
    changed = False
    for index, item in enumerate(value):
        if isinstance(item, str):
            rewritten, item_changed = _rewrite_reminder_text(item, remaining, used, window)
            if item_changed:
                value[index] = rewritten
                changed = True
        elif isinstance(item, dict) and _rewrite_reminder_block(item, remaining, used, window):
            changed = True
    return changed


def rewrite_context_reminders(payload: dict, context, used):
    """Rewrite Claude Desktop token-reminder lines to remaining catalogue context.

    No-op when the catalogue has no known integer context, the payload has no
    matching standalone reminder lines, or the replacement would not fit the
    Desktop tag grammar. Matching lines inside fenced code are left intact.
    """
    if not isinstance(payload, dict):
        return payload
    if type(context) is not int or context <= 0:
        return payload
    if type(used) is not int or used < 0:
        return payload
    remaining = max(0, context - used)
    limit = 10 ** _MAX_REMINDER_DIGITS
    if remaining >= limit or used >= limit or context >= limit:
        return payload
    if not _contains_context_reminder(payload.get("system")) and not _contains_context_reminder(payload.get("messages")):
        return payload
    result = copy.deepcopy(payload)
    changed = False
    system = result.get("system")
    if isinstance(system, str):
        rewritten, system_changed = _rewrite_reminder_text(system, remaining, used, context)
        if system_changed:
            result["system"] = rewritten
            changed = True
    elif isinstance(system, list) and _rewrite_reminder_content(system, remaining, used, context):
        changed = True
    messages = result.get("messages")
    if isinstance(messages, list):
        for message in messages:
            if not isinstance(message, dict):
                continue
            content = message.get("content")
            if isinstance(content, str):
                rewritten, item_changed = _rewrite_reminder_text(content, remaining, used, context)
                if item_changed:
                    message["content"] = rewritten
                    changed = True
            elif isinstance(content, list) and _rewrite_reminder_content(content, remaining, used, context):
                changed = True
    return result if changed else payload


# Claude Code's Ultracode level (xhigh effort plus standing dynamic-workflow
# orchestration) announces itself to the model through reminders in user
# turns. A non-Claude model gets one extra, explicit system note so the
# orchestration intent survives the provider translation; Claude Code's own
# reminder still names the workflow authoring reference.
_ULTRACODE_ON = ("Ultracode is on:", "Ultracode is still on")
_ULTRACODE_OFF = "Ultracode is off"
_ULTRACODE_KEYWORD = 'The user included the keyword "ultracode"'
ULTRACODE_NOTE = (
    "Ultracode is on for this session. You are a non-Claude model working through Provider Hub, so be explicit "
    "about orchestration: on every substantive task call the Workflow tool with a script that fans out subagents "
    "for investigation, implementation and verification, loading the workflow-authoring skill first for the script "
    "API. Answer solo only on conversational or trivial turns."
)


def _last_typed_user_index(messages: list) -> int:
    """Index of the last user turn the person actually typed into.

    A tool-using turn ends with a user message carrying only tool_result
    blocks, so "the final message" is the wrong anchor for a per-turn
    reminder: the keyword sits in the typed turn that opened the cycle, and
    Claude Code folds it into that prompt rather than repeating it.
    """
    latest = -1
    for index, message in enumerate(messages):
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        if not any(isinstance(block, dict) and block.get("type") == "text"
                   for block in blocks(message.get("content", ""))):
            continue
        # A user message that lands straight after a tool result is the
        # harness talking, not the person: a skill body, a compaction
        # continuation. Treating it as a new turn is how this went wrong -
        # the note tells the model to load the workflow-authoring skill, the
        # Skill tool delivers that skill as its own user message, and the
        # note switched itself off one tool cycle after it first appeared.
        previous = messages[index - 1] if index else None
        if (isinstance(previous, dict) and previous.get("role") == "user"
                and any(isinstance(block, dict) and block.get("type") == "tool_result"
                        for block in blocks(previous.get("content", "")))):
            continue
        latest = index
    return latest if latest >= 0 else len(messages) - 1


def ultracode_active(payload: dict) -> bool:
    """True when Claude Code's own reminders show Ultracode is on for this turn.

    The newest standing reminder wins; the per-turn keyword reminder counts
    only when it sits in the last turn the person typed - not the last
    message, which on a tool cycle is a tool_result the keyword never
    reaches.
    """
    state = False
    messages = payload.get("messages") if isinstance(payload, dict) else None
    if not isinstance(messages, list):
        return False
    last_typed = _last_typed_user_index(messages)
    for index, message in enumerate(messages):
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        for block in blocks(message.get("content", "")):
            if not isinstance(block, dict) or block.get("type") != "text":
                continue
            text = block.get("text", "")
            if not isinstance(text, str):
                continue
            if any(marker in text for marker in _ULTRACODE_ON):
                state = True
            elif _ULTRACODE_OFF in text:
                state = False
            if _ULTRACODE_KEYWORD in text and index == last_typed:
                state = True
    return state


#: Claude Code reads the text of a relayed 400 and, on one that names the
#: effort parameter as the thing refused, latches the model as
#: effort-unsupported for the rest of the session - the control goes dead and
#: does not come back until the session does.
#:
#: So the trigger words are renamed rather than the sentence replaced. The
#: activity ledger deliberately stores no error text (Runtime.record), and
#: last_error points back at this same message, so a message that says "see
#: the log" would be sending a person somewhere the wording has never been:
#: whatever diagnosis survives has to survive here, in the relayed sentence.
_EFFORT_TERMS = re.compile(r"output_config\.effort|output_config|reasoning effort|effort", re.I)
_EFFORT_TERM_NAMES = {
    "output_config.effort": "the reasoning-depth setting",
    "output_config": "the reasoning-depth field",
    "reasoning effort": "reasoning depth",
    "effort": "depth",
}


def mask_effort_rejection(detail: str) -> str:
    """Reword a relayed rejection so it cannot be read as "effort unsupported".

    Applied to every 400 the hub relays, whatever its shape: a message that
    only mentions the parameter in passing loses nothing by being reworded,
    while one that would have latched costs the user the control for a whole
    session.
    """
    if not detail:
        return detail
    return _EFFORT_TERMS.sub(lambda hit: _EFFORT_TERM_NAMES[hit.group(0).lower()], detail)


def without_reasoning_controls(payload: dict):
    """Strip effort and thinking from a request bound for a route without them.

    Desktop grants a picker row its effort ladder from one compiled key, not
    one per model, so every hub row now offers the full ladder whatever its
    provider can actually do. A route with no reasoning axis will therefore
    be asked for an effort it cannot take. Refusing is the worse answer:
    Claude Code reads one such rejection as the model not supporting effort
    at all and latches that for the rest of the session, so the control would
    die on the first request and stay dead until the app restarts. Dropping
    the controls costs the request nothing it could have had.

    This overlaps the shared control block in providers, which ignores the
    same controls for the same reason, and the overlap is deliberate. That
    block is not the only gate: Grok, Gemini and every adaptive-thinking path
    refuse in their own builders before reaching it, so removing this left
    eleven live Ollama rows hard-refusing adaptive thinking. Stripping here
    covers the ones the shared block never sees.
    """
    dropped = []
    if payload.get("thinking") is not None:
        dropped.append("thinking")
    output = payload.get("output_config")
    effort = output.get("effort") if isinstance(output, dict) else None
    if effort is not None and effort != "none":
        dropped.append("effort")
    if not dropped:
        return payload, []
    payload = {key: value for key, value in payload.items() if key != "thinking"}
    if "effort" in dropped:
        remaining = {key: value for key, value in output.items() if key != "effort"}
        if remaining:
            payload["output_config"] = remaining
        else:
            payload.pop("output_config", None)
    return payload, dropped


def _thousands(value: int) -> str:
    return f"{value:,}"


def identity_note(spec: dict, harness: str = "Claude Desktop") -> str | None:
    """One line telling a model what it actually is, at the top of its prompt.

    Everything served here arrives wearing a Claude model id, because that is
    the only shape the desktop picker accepts, and the harness's own system
    prompt then tells the model it is that Claude model. Left alone, a smaller
    model infers capabilities it does not have and a larger one defers to
    limits that are not its own; both plan around a context window belonging
    to something else. This says who is actually answering.

    It states the harness's effort setting rather than the rank the gateway
    will end up sending, because the two can differ and only the provider
    builders know the second - predicting it here would mean a second copy of
    that resolution. The model's own ladder is given instead, which is both
    knowable at this point and more useful: a model that can see its ladder
    can tell where the requested rung falls on it.
    """
    if not isinstance(spec, dict):
        return None
    identifier = spec.get("id")
    if not isinstance(identifier, str) or not identifier:
        return None
    advertised = spec.get("display_name")
    name = friendly_model_name(identifier.rsplit("/", 1)[-1],
                               advertised if isinstance(advertised, str) and advertised else None)
    # Say who makes it as well as what it is - "K3" alone leaves the model to
    # guess, which is the whole problem this line exists to stop.
    provider = (spec.get("presentation") or {}).get("displayProvider")
    if not (isinstance(provider, str) and provider):
        provider_id = spec.get("provider_id") or (identifier.split("/", 1)[0] if "/" in identifier else None)
        provider = provider_id.replace("-", " ").title() if isinstance(provider_id, str) and provider_id else None
    who = f"{name} ({provider})" if provider else name

    kind, floor, ceiling = stated_context(spec)
    if kind == "exact":
        window = f"Your context window is {_thousands(floor)} tokens."
    elif kind == "range":
        window = (f"Your context window is at least {_thousands(floor)} tokens and at most "
                  f"{_thousands(ceiling)}; which applies depends on this account's plan, so treat "
                  f"the lower figure as the one you can rely on.")
    else:
        window = ("Your context window is not published to this gateway, so do not assume a size - "
                  "ask before relying on a long context.")

    ladder = [mode for mode in (spec.get("effort_modes") or []) if isinstance(mode, str)]
    if spec.get("reasoning") is False:
        reasoning = "You have no adjustable reasoning setting."
    elif len(ladder) > 1:
        reasoning = ("Your reasoning ladder is " + "/".join(ladder)
                     + "; the harness may ask for a level outside it, which is mapped onto the "
                       "nearest one you have.")
    elif ladder:
        reasoning = f"Your reasoning runs at a fixed {ladder[0]} setting."
    else:
        reasoning = "Your reasoning setting is the model's own default."

    return (f"You are {who}, reached through Provider Hub inside the {harness} harness. "
            f"{window} {reasoning} "
            "The surrounding system prompt describes that harness and its conventions, and those "
            "conventions apply to you - follow them. Where it names a model, a context window or a "
            "capability, this line is the accurate one.")


def with_identity_note(system, spec: dict, harness: str = "Claude Desktop"):
    note = identity_note(spec, harness)
    return system if note is None else append_system_text(system, note)


def append_system_text(system, text: str):
    """Append one note to a request's system field, in whichever spelling it came."""
    note = {"type": "text", "text": text}
    if system is None or system == "":
        return [note]
    if isinstance(system, str):
        return [{"type": "text", "text": system}, note]
    if isinstance(system, list):
        return [*system, note]
    return system


def with_ultracode_note(system):
    """Append the Ultracode note to a request's top-level system field.

    Claude Desktop sends `system` as a string or as a list of content blocks,
    and omits it entirely on a bare request; all three arrive here.
    """
    return append_system_text(system, ULTRACODE_NOTE)


def translate_request(payload: dict, settings: dict):
    requested = payload.get("model", "")
    upstream = resolve_model(requested, claude_routes(settings))
    spec = settings.get("_model_specs", {}).get(upstream)
    effective_context = _effective_context(spec) if spec else None
    if type(effective_context) is not int or effective_context <= 0:
        raise BridgeError("Model limits are not in the current catalogue. Refresh models in Provider Hub first.")
    if requested.endswith("[1m]") and effective_context < 1000000:
        raise BridgeError("The selected model does not have a 1M context window.")
    if payload.get("speed") == "fast" or payload.get("service_tier") in {"fast", "priority"}:
        raise BridgeError("Claude Fast mode is not available for this Mistral connection. Use standard speed; the Vibe fast alias is a different model.")
    if not isinstance(payload.get("messages"), list) or not payload["messages"]:
        raise BridgeError("At least one message is required.")
    max_tokens = payload.get("max_tokens", 4096)
    if type(max_tokens) is not int or max_tokens <= 0:
        raise BridgeError("max_tokens must be a positive integer.")
    estimate = estimated_tokens(payload)
    if estimate >= effective_context:
        raise BridgeError(f"This conversation is above the model's reported {effective_context:,}-token context limit. Compact it or start a new session.")
    payload = rewrite_context_reminders(payload, effective_context, estimate)
    messages = []
    if payload.get("system"):
        system = [content_part(p) for p in blocks(payload["system"])]
        if any(p is None or p.get("type") != "text" for p in system):
            raise BridgeError("System instructions must contain text.")
        messages.append({"role": "system", "content": compact_content(system)})
    names = {}
    identifiers = {}
    for message in payload["messages"]:
        role = message.get("role")
        if role in {"system", "developer"}:
            parts = [content_part(p) for p in blocks(message.get("content", ""))]
            if any(p is None or p.get("type") != "text" for p in parts):
                raise BridgeError("System/developer messages must contain text.")
            messages.append({"role": "system", "content": compact_content(parts)})
            continue
        if role not in {"user", "assistant"}:
            raise BridgeError(f"Unsupported message role: {str(role)[:30]}.")
        pending = []
        calls = []

        def flush():
            if pending or calls:
                item = {"role": role, "content": compact_content(pending)}
                if calls:
                    item["tool_calls"] = list(calls)
                messages.append(item)
                pending.clear()
                calls.clear()

        for block in blocks(message.get("content", "")):
            kind = block.get("type")
            if kind == "tool_use":
                if role != "assistant":
                    raise BridgeError("Tool calls must be assistant messages.")
                original_id = block.get("id", "")
                name = block.get("name", "")
                if not original_id or not name:
                    raise BridgeError("Tool call IDs and names are required.")
                mapped = tool_id(original_id)
                if mapped in identifiers and identifiers[mapped] != original_id:
                    raise BridgeError("Tool identifier collision; start a new session.")
                identifiers[mapped] = original_id
                names[function_name(name)] = name
                calls.append({"id": mapped, "type": "function", "function": {
                    "name": function_name(name), "arguments": json.dumps(block.get("input", {}), ensure_ascii=False)}})
            elif kind == "tool_result":
                if role != "user":
                    raise BridgeError("Tool results must be user messages.")
                flush()
                original_id = block.get("tool_use_id", "")
                if not original_id:
                    raise BridgeError("Tool results must reference a tool call.")
                parts = [content_part(p) for p in blocks(block.get("content", ""))]
                text_parts = [p for p in parts if p and p["type"] == "text"]
                images = [p for p in parts if p and p["type"] == "image_url"]
                text = compact_content(text_parts)
                if block.get("is_error"):
                    text = "Tool execution error:\n" + text
                if images:
                    text += "\nThe tool's images follow in the next user message."
                messages.append({"role": "tool", "tool_call_id": tool_id(original_id), "content": text})
                if images:
                    pending.extend(images)
            else:
                part = content_part(block)
                if part:
                    pending.append(part)
        flush()
    if ultracode_active(payload):
        first_turn = next((index for index, item in enumerate(messages) if item["role"] != "system"), len(messages))
        messages.insert(first_turn, {"role": "system", "content": ULTRACODE_NOTE})
    tools = []
    for tool in payload.get("tools", []):
        if "input_schema" not in tool:
            raise BridgeError(f"Hosted tool {tool.get('name', tool.get('type', 'unknown'))!r} is not supported. Disable that tool in this Claude profile.")
        name = tool.get("name", "")
        if not isinstance(name, str) or not name:
            raise BridgeError("Tools require a name.")
        mapped = function_name(name)
        if mapped in names and names[mapped] != name:
            raise BridgeError("Tool name collision.")
        names[mapped] = name
        tools.append({"type": "function", "function": {"name": mapped, "description": tool.get("description", ""),
                      "parameters": tool["input_schema"]}})
    result = {"model": upstream, "messages": messages, "max_tokens": min(max_tokens, effective_context - estimate),
              "stream": bool(payload.get("stream", False))}
    if tools:
        result["tools"] = tools
    choice = payload.get("tool_choice", {})
    if choice:
        if choice.get("type") == "tool":
            result["tool_choice"] = {"type": "function", "function": {"name": function_name(choice.get("name", ""))}}
        elif choice.get("type") in {"any", "auto", "none"}:
            result["tool_choice"] = {"any": "required", "auto": "auto", "none": "none"}[choice["type"]]
        else:
            raise BridgeError("Unsupported tool_choice.")
        if "disable_parallel_tool_use" in choice:
            result["parallel_tool_calls"] = not choice["disable_parallel_tool_use"]
    for key in ("temperature", "top_p"):
        if key in payload:
            result[key] = payload[key]
    if payload.get("stop_sequences"):
        result["stop"] = payload["stop_sequences"]
    effort = model_effort(payload, spec)
    if effort is not None:
        result["reasoning_effort"] = effort
    # Stable cache hint; Mistral decides whether cached prefixes can be reused.
    result["prompt_cache_key"] = hashlib.sha256(json.dumps({"system": payload.get("system"), "tools": payload.get("tools")}, sort_keys=True).encode()).hexdigest()
    result["messages"] = repair_openai_tool_order(result["messages"])

    return result, names


def visible_text(content) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(p.get("text", "") for p in content if p.get("type") == "text")
    raise BridgeError("The provider returned an unsupported content format.")


def usage_counts(usage):
    return {"input_tokens": max(0, usage.get("prompt_tokens", 0)),
            "output_tokens": max(0, usage.get("completion_tokens", 0))}


def stop_reason(reason, has_tools=False):
    if reason == "length":
        return "max_tokens"
    return "tool_use" if reason == "tool_calls" or has_tools else "end_turn"


# Mistral sometimes answers tool turns by typing the invocation as plain text
# (for example Bash{"command": ...}) instead of using the structured
# tool_calls channel. Recovery converts those predicted calls, but only when
# the shape is unambiguous, so prose about tool calls is never converted.
_IDENTIFIER_RUN = re.compile(r"[A-Za-z_][A-Za-z0-9_-]*")
# Give up holding an unbalanced candidate tail beyond this size so runaway
# near-JSON prose cannot buffer the whole stream. Genuine arguments from Edit
# and similar tools stay well under this.
_MAX_TOOL_CALL_HOLD = 65536


def _balanced_object_end(text: str, start: int):
    """Index just past the JSON object opening at `start`, string-aware."""
    depth, in_string, escaped = 0, False, False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i + 1
    return None


def segment_predicted_calls(text: str, names: dict) -> list:
    """Split assistant text into ("text", str) and ("tool", name, arguments) segments.

    A tool segment is `Name{...}` (whitespace allowed before the brace) where
    Name is a tool advertised for this request and {...} parses to a JSON
    object. Anything else stays text.
    """
    segments, pos, cursor = [], 0, 0
    while cursor < len(text):
        match = _IDENTIFIER_RUN.search(text, cursor)
        if not match:
            break
        name = match.group(0)
        brace = match.end()
        while brace < len(text) and text[brace] in " \t":
            brace += 1
        end = None
        if name in names and brace < len(text) and text[brace] == "{":
            end = _balanced_object_end(text, brace)
        if end is not None:
            try:
                value = json.loads(text[brace:end])
            except ValueError:
                value = None
            if isinstance(value, dict):
                if match.start() > pos:
                    segments.append(("text", text[pos:match.start()]))
                segments.append(("tool", name, text[brace:end]))
                pos = cursor = end
                continue
        cursor = match.end()
    if pos < len(text):
        segments.append(("text", text[pos:]))
    return segments


def _synthetic_tool_id(occurrence: int, name: str, arguments: str) -> str:
    # Deterministic so tests reproduce, and unique per occurrence so identical
    # repeated calls stay distinguishable. The desktop echoes this ID in its
    # tool_result and the next request re-hashes it for the provider, so the
    # recovered call becomes a structured call in provider history.
    digest = hashlib.sha256(f"{occurrence}:{name}:{arguments}".encode()).hexdigest()
    return "toolu_" + digest[:22]


def translate_response(response, requested, names):
    try:
        choice = response["choices"][0]
        msg = choice["message"]
        content = []
        converted = 0
        text = visible_text(msg.get("content"))
        if text:
            for segment in segment_predicted_calls(text, names):
                if segment[0] == "text":
                    content.append({"type": "text", "text": segment[1]})
                else:
                    _, name, arguments = segment
                    content.append({"type": "tool_use", "id": _synthetic_tool_id(converted, name, arguments),
                                    "name": names.get(name, name), "input": json.loads(arguments)})
                    converted += 1
        for tool in msg.get("tool_calls") or []:
            fn = tool["function"]
            args = fn.get("arguments", "{}")
            content.append({"type": "tool_use", "id": tool.get("id") or "toolu_" + secrets.token_hex(10),
                            "name": names.get(fn["name"], fn["name"]),
                            "input": json.loads(args) if isinstance(args, str) else args})
        return {"id": "msg_" + secrets.token_hex(12), "type": "message", "role": "assistant", "model": requested,
                "content": content,
                "stop_reason": stop_reason(choice.get("finish_reason"), bool(msg.get("tool_calls")) or converted > 0),
                "stop_sequence": None, "usage": usage_counts(response.get("usage", {}))}
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise BridgeError("The provider returned a malformed response or invalid tool arguments.") from exc


class StreamTranslator:
    def __init__(self, requested, names):
        self.requested = requested
        self.names = names
        self.identifier = "msg_" + secrets.token_hex(12)
        self.next_index = 0
        self.text_index = None
        self.text_open = False
        self.open_indices = set()
        self.tools = {}
        # Content text is buffered so predicted tool calls typed as plain text
        # can be converted before their deltas reach the desktop. Only the
        # undecided tail that could still become a candidate is held back.
        self.buffer = ""
        self.converted = []
        self.usage = {"input_tokens": 0, "output_tokens": 0}
        self.finish = None

    def start(self):
        return [{"type": "message_start", "message": {"id": self.identifier, "type": "message", "role": "assistant",
                "model": self.requested, "content": [], "stop_reason": None, "stop_sequence": None, "usage": self.usage.copy()}}]

    def feed(self, chunk):
        events = []
        if chunk.get("usage"):
            self.usage = usage_counts(chunk["usage"])
        for choice in chunk.get("choices", []):
            if choice.get("index", 0) != 0:
                continue
            if choice.get("finish_reason"):
                self.finish = choice["finish_reason"]
            delta = choice.get("delta") or {}
            text = visible_text(delta.get("content"))
            if text:
                self.buffer += text
                self._flush_buffer(events)
            for tool in delta.get("tool_calls") or []:
                key = tool.get("index", 0)
                state = self.tools.setdefault(key, {"id": None, "name": None, "arguments": "", "buffer": "", "index": None})
                fn = tool.get("function", {})
                if tool.get("id"):
                    state["id"] = tool["id"]
                if fn.get("name"):
                    state["name"] = fn["name"]
                arguments = fn.get("arguments", "")
                if not isinstance(arguments, str):
                    arguments = json.dumps(arguments)
                state["arguments"] += arguments
                state["buffer"] += arguments
                if state["index"] is None and state["id"] and state["name"]:
                    state["index"] = self.next_index
                    self.next_index += 1
                    self.open_indices.add(state["index"])
                    events.append({"type": "content_block_start", "index": state["index"], "content_block": {
                        "type": "tool_use", "id": state["id"], "name": self.names.get(state["name"], state["name"]), "input": {}}})
                if state["index"] is not None and state["buffer"]:
                    events.append({"type": "content_block_delta", "index": state["index"], "delta": {
                        "type": "input_json_delta", "partial_json": state["buffer"]}})
                    state["buffer"] = ""
        return events

    def _emit_text(self, events, text):
        if not self.text_open:
            self.text_index = self.next_index
            self.next_index += 1
            self.open_indices.add(self.text_index)
            events.append({"type": "content_block_start", "index": self.text_index,
                           "content_block": {"type": "text", "text": ""}})
            self.text_open = True
        events.append({"type": "content_block_delta", "index": self.text_index,
                       "delta": {"type": "text_delta", "text": text}})

    def _close_text(self, events):
        if self.text_open:
            events.append({"type": "content_block_stop", "index": self.text_index})
            self.open_indices.discard(self.text_index)
            self.text_open = False

    def _decided_prefix(self):
        """Length of the buffer prefix whose interpretation cannot change.

        Complete predicted calls and text before them are decided. A suffix
        that could still become a candidate — a known tool name (or a prefix
        of one) at the buffer end, or an unbalanced '{' — is held back for
        more deltas.
        """
        if not self.names:
            return len(self.buffer)
        limit = len(self.buffer)
        cursor = 0
        while cursor < limit:
            match = _IDENTIFIER_RUN.search(self.buffer, cursor)
            if not match:
                break
            name = match.group(0)
            brace = match.end()
            while brace < limit and self.buffer[brace] in " \t":
                brace += 1
            if name in self.names and brace < limit and self.buffer[brace] == "{":
                end = _balanced_object_end(self.buffer, brace)
                if end is None:
                    if limit - match.start() > _MAX_TOOL_CALL_HOLD:
                        return limit  # stop holding a runaway near-JSON tail
                    return match.start()
                cursor = end  # balanced: appends cannot change this slice
            elif brace >= limit and (name in self.names or any(n.startswith(name) for n in self.names)):
                return match.start()  # appending may complete the name and '{'
            else:
                cursor = match.end()  # followed by ordinary text: never a call
        return limit

    def _flush_buffer(self, events, *, final=False):
        decided = len(self.buffer) if final else self._decided_prefix()
        if not decided:
            return
        head, self.buffer = self.buffer[:decided], self.buffer[decided:]
        for segment in segment_predicted_calls(head, self.names):
            if segment[0] == "text":
                self._emit_text(events, segment[1])
                continue
            _, name, arguments = segment
            self._close_text(events)
            index = self.next_index
            self.next_index += 1
            events.append({"type": "content_block_start", "index": index, "content_block": {
                "type": "tool_use", "id": _synthetic_tool_id(len(self.converted), name, arguments),
                "name": self.names.get(name, name), "input": {}}})
            events.append({"type": "content_block_delta", "index": index, "delta": {
                "type": "input_json_delta", "partial_json": arguments}})
            events.append({"type": "content_block_stop", "index": index})
            self.converted.append((name, arguments))

    def end(self):
        if self.finish is None:
            raise BridgeError("The provider closed the stream before a completion signal.")
        events = []
        self._flush_buffer(events, final=True)
        for tool in self.tools.values():
            if tool["index"] is None:
                raise BridgeError("The provider returned an incomplete tool call.")
            try:
                value = json.loads(tool["arguments"] or "{}")
                if not isinstance(value, dict):
                    raise ValueError()
            except ValueError as exc:
                raise BridgeError("The provider returned invalid tool arguments.") from exc
        result = [{"type": "content_block_stop", "index": i} for i in sorted(self.open_indices)]
        result.append({"type": "message_delta", "delta": {"stop_reason": stop_reason(self.finish, bool(self.tools) or bool(self.converted)), "stop_sequence": None}, "usage": self.usage})
        result.append({"type": "message_stop"})
        return events + result


def compact_threshold(context, options=None, *, reserve_output=None) -> int | None:
    """Return the estimated token count that triggers gateway-side compaction.

    An explicit per-slot ``compact_limit`` override always applies and clamps
    to the catalogue window, so small-window routes can compact earlier than
    Claude Desktop's own session meter would. Otherwise compaction starts at
    85 percent of a known window larger than 10,000 tokens. When
    ``reserve_output`` names the requested output size, the threshold also
    tightens to leave that much headroom, so input pressure cannot silently
    shrink the response below what the client asked for. Absurd reservations
    that would leave under 1,000 tokens are ignored. Returns None when no
    automatic compaction applies.
    """
    if type(context) is not int or context <= 0:
        return None
    override = (options or {}).get("compact_limit")
    if type(override) is int and override > 0:
        threshold = min(override, context)
    elif context > 10000:
        threshold = int(context * 0.85)
    else:
        return None
    if type(reserve_output) is int and reserve_output > 0:
        headroom = context - reserve_output
        if headroom >= 1000:
            threshold = min(threshold, headroom)
    return threshold


def _tool_use_ids(message):
    content = message.get("content") if isinstance(message, dict) else None
    if not (isinstance(message, dict) and message.get("role") == "assistant" and isinstance(content, list)):
        return set()
    return {block["id"] for block in content
            if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("id")}


def _tool_result_ids(message):
    content = message.get("content") if isinstance(message, dict) else None
    if not (isinstance(message, dict) and message.get("role") == "user" and isinstance(content, list)):
        return set()
    return {block["tool_use_id"] for block in content
            if isinstance(block, dict) and block.get("type") == "tool_result" and block.get("tool_use_id")}


def conversation_units(messages):
    """Group messages into units that must survive or fall together.

    An assistant message that issues tool calls forms one unit with the
    messages that answer those calls (tool results, plus any user text the
    harness interleaves before the last result). Every other message is a
    unit of its own. Dropping whole units keeps every tool call paired with
    its result, which chat-completions upstreams such as Mistral require.
    """
    units = []
    index = 0
    while index < len(messages):
        message = messages[index]
        pending = set(_tool_use_ids(message))
        unit = [message]
        index += 1
        while pending and index < len(messages):
            candidate = messages[index]
            if _tool_use_ids(candidate):
                break
            answered = _tool_result_ids(candidate)
            if not answered and index + 1 < len(messages) and not (pending & _tool_result_ids(messages[index + 1])):
                break
            unit.append(candidate)
            pending -= answered
            index += 1
        units.append(unit)
    return units


def _plain_user_unit(unit):
    return (len(unit) == 1 and isinstance(unit[0], dict) and unit[0].get("role") == "user"
            and not _tool_result_ids(unit[0]))


def _as_blocks(content):
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else []
    return list(content) if isinstance(content, list) else []


def _user_with_note(message, note, prefix=None):
    """One user message carrying ``prefix``'s content, then ``note``, then ``message``."""
    merged = dict(message)
    head = prefix.get("content") if isinstance(prefix, dict) else None
    body = message.get("content")
    if isinstance(body, str) and (head is None or isinstance(head, str)):
        merged["content"] = "\n\n".join(part for part in (head, note, body) if part)
    else:
        merged["content"] = _as_blocks(head) + [{"type": "text", "text": note}] + _as_blocks(body)
    return merged


def _append_note(message, note):
    """``message`` with ``note`` added after its own content."""
    merged = dict(message)
    body = message.get("content")
    if isinstance(body, str):
        merged["content"] = "\n\n".join(part for part in (body, note) if part)
    else:
        merged["content"] = _as_blocks(body) + [{"type": "text", "text": note}]
    return merged


# Kept in front of a compacted tail only while it stays a small share of the
# budget, so a huge opening paste cannot crowd out the recent turns.
COMPACT_HEAD_SHARE = 0.25


def compact_conversation(payload: dict, max_tokens: int, estimate=None) -> dict:
    """Compact conversation history to fit within max_tokens budget.

    Strategy: keep the newest contiguous run of conversation units that fits
    the budget and drop everything older, preserving the system prompt and
    chronological order. Units bind an assistant tool call to its results
    (see ``conversation_units``), so compaction never leaves a tool result
    without its call or a call without its result. The opening user message
    is pinned when it is small (it usually carries the task), and a short
    user-role note marks the cut so the history still starts with a user
    turn, which chat and Messages upstreams both require. The newest unit is
    kept even when it does not fit on its own. Returns a new payload; the
    input is unchanged.
    """
    import copy
    estimator = estimate or estimated_tokens
    result = copy.deepcopy(payload)

    messages = result.get("messages", [])
    if not messages:
        return result

    # Always preserve system message if present
    system_message = None
    non_system_messages = []

    for msg in messages:
        if isinstance(msg, dict) and msg.get("role") == "system":
            system_message = msg
        else:
            non_system_messages.append(msg)

    if not non_system_messages:
        return result

    units = conversation_units(non_system_messages)
    system_prefix = [system_message] if system_message else []
    head = units[0] if len(units) > 1 and _plain_user_unit(units[0]) else None
    if head is not None and estimator({"messages": system_prefix + head, "system": result.get("system")}) > max_tokens * COMPACT_HEAD_SHARE:
        head = None
    candidates = units[1:] if head is not None else units
    kept_units = []
    for unit in reversed(candidates):
        trial = {"messages": system_prefix + (head or []) + unit + [m for u in kept_units for m in u],
                 "system": result.get("system")}
        if kept_units and estimator(trial) > max_tokens:
            break
        kept_units.insert(0, unit)
    dropped = sum(len(unit) for unit in candidates[:len(candidates) - len(kept_units)])
    kept = [message for unit in kept_units for message in unit]
    if dropped:
        note = (f"[Provider Hub removed {dropped} earlier message{'s' if dropped != 1 else ''} from this conversation "
                "so it fits the model's context window.]")
        if kept and isinstance(kept[0], dict) and kept[0].get("role") == "user":
            kept[0] = _user_with_note(kept[0], note, prefix=head[0] if head else None)
        else:
            kept = [(_append_note(head[0], note) if head else {"role": "user", "content": note})] + kept
    elif head is not None:
        kept = head + kept

    kept_tool_ids = set()
    for msg in kept:
        kept_tool_ids |= _tool_use_ids(msg)
    # A tool result without its call is meaningless to every upstream. Units
    # keep cycles whole, so this only trims results whose call never existed
    # in the history (or sat outside its unit), dropping messages left empty.
    survivors = []
    for msg in kept:
        content = msg.get("content") if isinstance(msg, dict) else None
        if isinstance(msg, dict) and msg.get("role") == "user" and isinstance(content, list):
            remaining = [block for block in content
                         if not (isinstance(block, dict) and block.get("type") == "tool_result"
                                 and block.get("tool_use_id") not in kept_tool_ids)]
            if not remaining:
                continue
            msg["content"] = remaining
        survivors.append(msg)
    kept = survivors

    result["messages"] = ([system_message] if system_message else []) + kept
    return result


class TokenCalibration:
    """Learn how far the byte-based estimate sits from a provider's count.

    ``estimated_tokens`` is deliberately pessimistic (bytes / 3), which fits
    prose and dense JSON but over-counts source code by roughly 40 percent on
    Mistral's tokenizer. Every completed request reports the provider's real
    input count for a payload whose estimate is known, so the gateway keeps
    an exponential moving average of real / estimate per route and scales the
    next estimate by it. The factor only ever lowers an estimate, clamps to
    [FLOOR, 1.0] and keeps a small safety margin, so an unlearned route or a
    JSON-heavy conversation stays exactly as conservative as before.
    """

    FLOOR = 0.5
    MARGIN = 1.05
    ALPHA = 0.3
    MIN_REAL = 2000

    def __init__(self):
        import threading
        self._lock = threading.Lock()
        self._ratios = {}

    def observe(self, route, estimate, real):
        """Record one provider-reported input count against its estimate."""
        if type(estimate) is not int or type(real) is not int or estimate <= 0 or real < self.MIN_REAL:
            return None
        ratio = real / estimate
        with self._lock:
            previous = self._ratios.get(route)
            value = ratio if previous is None else previous + self.ALPHA * (ratio - previous)
            self._ratios[route] = value
        return value

    def factor(self, route):
        with self._lock:
            value = self._ratios.get(route)
        if value is None:
            return 1.0
        return min(1.0, max(self.FLOOR, value * self.MARGIN))

    def calibrated(self, route, estimate):
        return max(1, int(estimate * self.factor(route)))

    def snapshot(self):
        with self._lock:
            return {route: round(value, 3) for route, value in self._ratios.items()}


def reported_input_tokens(usage):
    """Total prompt tokens the provider billed, including cached prefixes."""
    if not isinstance(usage, dict):
        return None
    total = 0
    seen = False
    for key in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"):
        value = usage.get(key)
        if type(value) is int and value >= 0:
            total += value
            seen = True
    return total if seen else None


def validate_mistral_roles(payload: dict) -> None:
    """Validate message roles for Mistral API compatibility.

    Mistral requires the last message to be from user, tool, or assistant with prefix=True.
    System and developer messages at the tail are skipped because
    ``_translate_chat_payload`` reorders them to the front before the wire
    request is built.  Raises BridgeError if validation fails.
    """
    messages = payload.get("messages", [])
    if not messages:
        return

    # Walk backwards past any trailing system/developer messages — translation
    # will move them to the front, so they cannot be the wire's last message.
    last_message = None
    for msg in reversed(messages):
        if isinstance(msg, dict) and msg.get("role") in {"system", "developer"}:
            continue
        last_message = msg
        break
    if last_message is None:
        # All messages are system/developer; translation will handle it.
        return
    role = last_message.get("role")

    # Mistral accepts: user, tool, or assistant with prefix=True
    if role == "assistant":
        # Check if prefix flag is present and True
        if not last_message.get("prefix", False):
            raise BridgeError(
                "Mistral API requires the last assistant message to have prefix=True. "
                "Start a new conversation or ensure proper message ordering."
            )
    elif role not in {"user", "tool"}:
        raise BridgeError(
            f"Mistral API requires the last message role to be 'user', 'tool', or 'assistant' with prefix=True, "
            f"but got '{role}'. Start a new conversation."
        )


def apply_mistral_prefix(payload: dict) -> dict:
    """Mark a trailing bare assistant message as a Mistral prefill prefix.

    Mistral only accepts a trailing assistant message with prefix=True;
    anything else is a guaranteed HTTP 400. Synthetic history such as
    subagent delegation events legitimately lands at the tail, so translate
    it instead of failing. Assistant turns carrying tool calls are left
    untouched so validation still rejects genuinely incomplete tool loops.
    Mirrors the trailing system/developer skip in validate_mistral_roles.
    Copy-on-write: the input payload is never mutated.
    """
    messages = payload.get("messages", [])
    if not isinstance(messages, list) or not messages:
        return payload
    target_index = None
    for index in range(len(messages) - 1, -1, -1):
        msg = messages[index]
        if isinstance(msg, dict) and msg.get("role") in {"system", "developer"}:
            continue
        target_index = index
        break
    if target_index is None:
        return payload
    last_message = messages[target_index]
    if not isinstance(last_message, dict) or last_message.get("role") != "assistant":
        return payload
    if last_message.get("prefix", False):
        return payload
    if last_message.get("tool_calls"):
        return payload
    content = last_message.get("content", "")
    blocks = content if isinstance(content, list) else []
    if any(isinstance(block, dict) and block.get("type") == "tool_use" for block in blocks):
        return payload
    patched = dict(last_message)
    patched["prefix"] = True
    updated = list(messages)
    updated[target_index] = patched
    return {**payload, "messages": updated}


def normalize_native_message(value):
    """Coerce a provider's own Messages-shaped JSON reply into a usable one.

    A one-token probe such as Claude Desktop's health check can leave a
    thinking model with nothing to say, and some compatible endpoints then
    serialise ``content`` as null or omit it, or leave ``type`` off. Those
    replies are still messages: fill the shape in so the desktop's own
    validator (a content array with text blocks) accepts them. Anything
    that is not message-shaped is returned unchanged for the caller to
    reject.
    """
    if not isinstance(value, dict):
        return value
    looks_like_message = value.get("type") == "message" or value.get("role") == "assistant" or "content" in value
    if not looks_like_message:
        return value
    message = dict(value)
    content = message.get("content")
    if content is None:
        message["content"] = []
    elif isinstance(content, str):
        message["content"] = [{"type": "text", "text": content}] if content else []
    elif isinstance(content, dict):
        message["content"] = [content]
    if message.get("type") is None:
        message["type"] = "message"
    message.setdefault("role", "assistant")
    message.setdefault("stop_reason", "end_turn" if message["content"] else "max_tokens")
    message.setdefault("stop_sequence", None)
    if not isinstance(message.get("usage"), dict):
        message["usage"] = {"input_tokens": 0, "output_tokens": 0}
    return message


def response_shape(value) -> dict:
    """A privacy-safe description of a provider reply: shape, never content."""
    if not isinstance(value, dict):
        return {"json": type(value).__name__}
    shape = {"keys": sorted(str(key)[:40] for key in value)[:16], "type": str(value.get("type"))[:40]}
    content = value.get("content")
    shape["content"] = ("list:" + ",".join(sorted({str(b.get("type"))[:20] for b in content if isinstance(b, dict)})[:8])
                        if isinstance(content, list) else type(content).__name__)
    error = value.get("error")
    if isinstance(error, dict):
        shape["error_type"] = str(error.get("type"))[:40]
    return shape
