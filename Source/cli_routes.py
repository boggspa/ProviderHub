"""Route-2 CLI-backed providers: catalogue normalization and turn planning.

"Route 2" means the installed coding-agent CLI owns and refreshes its own
login: the hub spawns the vendor's own binary and never reads, copies, or
refreshes a credential file. This module is the glue between the per-provider
CLI adapters (claude_cli_agent, codex_cli_agent, muse_cli_agent,
grok_cli_agent, agy_cli_agent) and the hub's catalogue and gateway machinery.

Two jobs:

* Discovery. An adapter's ``catalogue()`` speaks its own dialect (codex's
  app-server camelCase, claude/agy's ``reasoning_levels`` dicts, grok's bare
  seed ids). :func:`discover_via_cli` normalizes those into the hub catalogue
  row shape that ``hub_config.project_catalogue`` consumes, and folds in the
  CLI's own read-only auth report, so a signed-out binary is visible at
  refresh time instead of failing the first turn.

* Turns. :func:`plan_turn` translates a planned Anthropic Messages payload
  into an adapter ``run_turn`` request, :func:`run_turn` starts the generator,
  and :func:`relay_cli_turn` translates the yielded events into Anthropic
  Messages wire events (one code path for SSE and buffered JSON). No adapter
  event executes anything: the adapters were built with every tool surface
  stripped, so the desktop harness alone owns the tool loop.
"""
from __future__ import annotations

import importlib
import json
import time
import uuid

import cli_structured_reply
from cli_lifecycle import ManagedTurn, TurnTiming, observe_events
from cli_image_history import compact_image_history
from cli_tool_call import (HOST_EXECUTION_NOTE, MAX_CALLS_PER_TURN, ToolCallError,
                           ToolCallParser, normalize_tools, render_tool_anchor,
                           render_tool_manifest, validate_host_call)
from effort_map import EFFORT_ORDER
from cli_images import (IMAGE_COORDINATE_NOTE, CliImageError, image_label,
                        normalize_image, normalize_images)
from hub_config import MODEL_ID
from providers import documented_context
from catalogue import image_input_blocked

#: provider id -> (adapter module name, binary label). Every adapter exposes
#: the same surface: PROVIDER_ID, TRANSPORT, SYSTEM_PROMPT_TRANSPORT,
#: discover, auth_state, catalogue, build_argv, run_turn.
ADAPTERS = {
    "codex": ("codex_cli_agent", "codex"),
    "claude": ("claude_cli_agent", "claude"),
    "muse": ("muse_cli_agent", "muse"),
    "grok": ("grok_cli_agent", "grok"),
    "antigravity": ("agy_cli_agent", "agy"),
}

#: Effort ladders for providers whose KNOWN_MODELS are bare ids, cited from
#: live evidence rather than claimed: `grok --help` documents
#: --reasoning-effort low/medium/high/xhigh and a streaming turn at each rung
#: completed in verification (grok 1.0.34).
_SEED_EFFORTS = {
    "grok": ["low", "medium", "high", "xhigh"],
}


class CliRouteError(ValueError):
    """A CLI-backed route could not be planned or discovered.

    Subclasses ValueError so the gateway's planning-rejection handling treats
    it like any other not-sent request.
    """


_cache = {}


def adapter_for(provider_id: str):
    """The imported adapter module for a CLI-backed provider, cached."""
    if provider_id in _cache:
        return _cache[provider_id]
    entry = ADAPTERS.get(provider_id)
    if entry is None:
        raise CliRouteError(f"Provider '{provider_id}' has no CLI-backed route.")
    module_name, binary = entry
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise CliRouteError(
            f"The {binary} CLI route is unavailable in this install ({exc}); "
            "rebuild the app bundle or switch this provider to API-key credentials."
        ) from exc
    _cache[provider_id] = module
    return module


def cli_credential_mode(settings: dict, provider_id: str) -> bool:
    """Whether this provider is switched to its installed-CLI credential source."""
    if provider_id not in ADAPTERS:
        return False
    connection = (settings.get("providers") or {}).get(provider_id) or {}
    return connection.get("credential_mode") == "cli"


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def _effort_axis(levels) -> list[str]:
    """Keep only canonical desktop ladder ranks, in ladder order."""
    found = {str(level) for level in (levels or [])}
    return [rank for rank in EFFORT_ORDER if rank in found]


def _hub_row(provider_id: str, row):
    """One adapter-dialect catalogue row, normalized for project_catalogue.

    Tolerates the dialects the adapters emit - codex's camelCase model/list
    rows, claude/agy's reasoning_levels dicts, agy's already hub-shaped
    family cards (richer fields pass through) - and returns None for anything
    without a routable id.

    ``tools`` is True on purpose: the route never executes a tool (the CLI
    runs with its tool surface stripped), but the desktop harness owns the
    tool loop, and a card advertising tools: False would make the Responses
    planner reject the harness's own tool catalogue outright. The route
    answers with text and thinking only; tool cycles simply never begin.
    """
    if not isinstance(row, dict):
        return None
    identifier = row.get("id") or row.get("model")
    if not isinstance(identifier, str) or not MODEL_ID.fullmatch(identifier):
        return None
    if row.get("hidden") is True:
        return None
    levels = row.get("effort_modes")
    if levels is None:
        levels = row.get("reasoning_levels")
    if levels is None:
        levels = [entry.get("reasoningEffort")
                  for entry in row.get("supportedReasoningEfforts") or []
                  if isinstance(entry, dict)]
    effort = _effort_axis(levels)
    reasoning = row.get("reasoning")
    if not isinstance(reasoning, bool):
        reasoning = bool(effort)
    display = row.get("display_name") or row.get("displayName")
    if not isinstance(display, str) or not display.strip():
        display = identifier
    vision = row.get("vision")
    if not isinstance(vision, bool):
        modalities = row.get("inputModalities")
        vision = ("image" in modalities) if isinstance(modalities, list) else None
    adapter = adapter_for(provider_id)
    if not getattr(adapter, "IMAGE_TRANSPORT", None):
        vision = False
    elif vision is None and identifier in getattr(adapter, "VERIFIED_IMAGE_MODELS", ()):
        vision = True
    result = {
        "id": identifier,
        "canonical_id": row.get("canonical_id") or identifier,
        "display_name": display,
        "aliases": [identifier],
        "context": row.get("context") if type(row.get("context")) is int else None,
        "max_output": row.get("max_output") if type(row.get("max_output")) is int else None,
        "tools": True,
        "vision": vision,
        "reasoning": reasoning,
        "effort_modes": effort,
        "fast_mode": False,
        "inference_status": "advertised",
        "source": "cli",
        "evidence": row.get("evidence") or f"the installed {ADAPTERS[provider_id][1]} CLI",
    }
    if type(row.get("runtime_context")) is int and row["runtime_context"] > 0:
        result["runtime_context"] = row["runtime_context"]
    # Hosted search only where the adapter can relay it and the vendor's own
    # catalogue offers it for this model.
    if row.get("web_search") is True and getattr(adapter, "WEB_SEARCH", False) is True:
        result["web_search"] = True
    for field in ("context_kind", "context_evidence"):
        if isinstance(row.get(field), str) and row[field]:
            result[field] = row[field]
    default_effort = row.get("default_effort") or row.get("defaultReasoningEffort")
    if isinstance(default_effort, str) and default_effort in effort:
        result["default_effort"] = default_effort
    provider_modes = row.get("provider_effort_modes")
    if isinstance(provider_modes, list) and provider_modes:
        result["provider_effort_modes"] = [str(mode) for mode in provider_modes]
    description = row.get("description")
    if isinstance(description, str) and description.strip():
        result["description"] = description
    if not (type(result["context"]) is int and result["context"] > 0):
        context, evidence = documented_context(provider_id, identifier)
        if context is not None:
            result.update(context=context, context_kind="verified_documentation",
                          context_evidence=evidence)
    return result


def _seed_rows(provider_id: str, known_models) -> list[dict]:
    """Rows from an adapter's KNOWN_MODELS when no live discovery exists.

    Seeds remain unverified for account availability. Exact published context
    ceilings are added separately, with their documentation provenance.
    Hosted search comes from the CLI's own model cache where the adapter reads
    one (``search_models``).
    """
    search_models = getattr(adapter_for(provider_id), "search_models", None)
    searchable = search_models() if callable(search_models) else frozenset()
    models = []
    for entry in known_models or ():
        row = {"id": entry} if isinstance(entry, str) else entry
        if isinstance(row, dict) and row.get("id") in searchable:
            row = {**row, "web_search": True}
        normalized = _hub_row(provider_id, row)
        if normalized is None:
            continue
        if not normalized["effort_modes"] and provider_id in _SEED_EFFORTS:
            normalized["effort_modes"] = list(_SEED_EFFORTS[provider_id])
            normalized["reasoning"] = True
        models.append(normalized)
    return models


def discover_via_cli(provider_id: str, *, timeout: int = 45) -> dict:
    """A CLI-backed provider's model inventory, in the cached-catalogue shape.

    Never raises for a wedged or signed-out CLI: the adapters already degrade
    to notes, and this folds their auth report into warnings so the settings
    pane can say why a row is empty instead of failing the whole refresh.
    """
    adapter = adapter_for(provider_id)
    warnings = []
    auth = {}
    try:
        auth = adapter.auth_state() or {}
    except Exception as exc:  # an auth probe must never take discovery down
        auth = {"state": "unknown", "detail": f"auth probe failed: {exc}"}
    state = auth.get("state")
    if state == "missing":
        warnings.append(f"Not signed in: {auth.get('detail', 'sign in through the CLI itself')}.")
    elif state in {"unknown", "unsupported"} and auth.get("detail"):
        warnings.append(f"Auth state {state}: {auth['detail']}")
    rows, notes = [], []
    try:
        rows, notes = adapter.catalogue(timeout=timeout)
    except Exception as exc:
        notes = [f"catalogue probe failed: {exc}"]
    warnings.extend(str(note) for note in (notes or []) if note)
    models = [model for model in (_hub_row(provider_id, row) for row in rows or []) if model]
    if not models:
        models = _seed_rows(provider_id, getattr(adapter, "KNOWN_MODELS", ()))
        if models:
            warnings.append(
                "No live model list exists for this CLI; routes are seeded from "
                "verified or documented ids and remain provider-managed."
            )
    inventory = {
        "provider_id": provider_id,
        "models": models,
        "source": "cli",
        "evidence": f"the installed {ADAPTERS[provider_id][1]} CLI's own reports",
        "warnings": warnings,
    }
    if state:
        inventory["auth_state"] = state
    return inventory


# ---------------------------------------------------------------------------
# Turns
# ---------------------------------------------------------------------------

def _flatten_blocks(content, *, images=None, full_tool_history=False) -> str:
    """Render Messages block-list content as plain transcript text.

    The Responses bridge spells every message as typed blocks. Text
    transports receive numbered image references plus separate binary
    attachments. Codex additionally keeps typed call/result history. Prior
    reasoning remains provider state rather than transcript text.
    """
    if isinstance(content, str):
        return content
    parts = []
    for block in content or []:
        if not isinstance(block, dict):
            parts.append(str(block))
            continue
        kind = block.get("type")
        if kind == "text":
            parts.append(str(block.get("text") or ""))
        elif kind == "thinking":
            continue
        elif kind == "tool_result":
            inner = _flatten_blocks(block.get("content"), images=images, full_tool_history=full_tool_history)
            if full_tool_history:
                parts.append(json.dumps({"type": "tool_result", "tool_use_id": block.get("tool_use_id"),
                                         "is_error": bool(block.get("is_error")), "content": inner}, ensure_ascii=False))
            elif inner.strip():
                parts.append(f"[tool result for {block.get('tool_use_id') or 'call'}]\n{inner}")
        elif kind == "tool_use":
            name = block.get("name") or "tool"
            try:
                args = json.dumps(block.get("input") or {}, ensure_ascii=False)
            except (TypeError, ValueError):
                args = "{}"
            if full_tool_history:
                parts.append(json.dumps({"type": "tool_use", "id": block.get("id"),
                                         "name": name, "input": block.get("input") or {}}, ensure_ascii=False))
            else:
                parts.append(f"[tool call: {name} ({block.get('id') or 'call'}) with {args[:200]}]")
        elif kind in {"input_image", "image"}:
            if images is None:
                raise CliImageError("Images must be delivered through a CLI image transport")
            images.append(normalize_image(block))
            parts.append("[" + image_label(images[-1], len(images)) + "]")
        elif kind in {"input_document", "document"}:
            parts.append("[document omitted: CLI routes are text-only]")
        else:
            text = block.get("text")
            parts.append(str(text) if isinstance(text, str) else "")
    return "\n".join(part for part in parts if part)


def _messages_for_cli(messages, system, *, images=None, full_tool_history=False):
    """Fold developer/system messages into system; flatten block content.

    The Responses bridge (Codex desktop) delivers developer-role messages in
    the array and block-spelled content everywhere. Text and image references
    form the transcript while screenshot bytes travel through image inputs.
    Developer text follows the system field in
    encounter order, so harness instructions stay at the head of the prompt,
    and a developer message that merely repeats the system text is dropped
    rather than doubled.
    """
    parts = [system] if isinstance(system, str) and system.strip() else []
    cleaned = []
    for message in messages or []:
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        text = _flatten_blocks(message.get("content"), images=images, full_tool_history=full_tool_history)
        if role in {"developer", "system"}:
            if text.strip() and all(text.strip() not in part for part in parts):
                parts.append(text.strip())
            continue
        if role not in {"user", "assistant"}:
            continue
        cleaned.append({"role": role, "content": text})
    combined = "\n\n".join(parts) if parts else None
    return cleaned, combined


def plan_turn(provider_id: str, upstream_model: str, payload: dict, spec: dict,
              *, wanted_output) -> dict:
    """Translate a planned Messages payload into an adapter run_turn request.

    The gateway's universal pre-processing (identity note, compaction, window
    enforcement) has already run on ``payload``; what remains is dialect: the
    effort axis arrives as output_config.effort, the system prompt may be
    None, and max_tokens is already clamped to the model's output cap.
    """
    adapter = adapter_for(provider_id)
    effort = (payload.get("output_config") or {}).get("effort")
    system = payload.get("system")
    if isinstance(system, list):
        # Anthropic's block spelling, which is how the gateway's identity note
        # arrives. The adapters take one plain string.
        try:
            system = _flatten_blocks(system).strip() or None
        except CliImageError as exc:
            raise CliRouteError("CLI images must be in user messages or tool results, not system instructions") from exc
    if system is not None and not isinstance(system, str):
        system = None
    images = []
    structured_surface = (provider_id == "antigravity" or
                          provider_id in {"muse", "grok"} and payload.get("_provider_hub_surface") != "responses")
    try:
        history, image_compaction = compact_image_history(payload.get("messages"))
        messages, system = _messages_for_cli(history, system, images=images,
                                            full_tool_history=structured_surface)
        if images:
            if not getattr(adapter, "IMAGE_TRANSPORT", None):
                raise CliImageError(f"The {provider_id} CLI does not support screenshot/image input. "
                                    "Choose an image-capable CLI route or use text/accessibility results.")
            if image_input_blocked(provider_id, spec):
                raise CliImageError("The selected CLI model does not support image input")
            images = normalize_images(images)
    except CliImageError as exc:
        raise CliRouteError(str(exc)) from exc
    raw_tools = payload.get("tools")
    tools = normalize_tools(raw_tools)
    tool_choice = payload.get("tool_choice")
    if isinstance(tool_choice, dict) and tool_choice.get("type") == "none":
        tools, tool_choice = [], None
    dynamic_tools = getattr(adapter, "HOST_TOOL_TRANSPORT", None) == "dynamic"
    # AntiGravity enforces its response schema on both desktop surfaces.
    # Muse/Grok retain their existing Responses handoff; their Messages
    # transport needs a schema to avoid native-tool name collisions.
    structured_tools = bool(tools) and structured_surface
    manifest = (cli_structured_reply.render_manifest(tools, tool_choice) if structured_tools
                else render_tool_manifest(tools, tool_choice) if tools and not dynamic_tools else "")
    if manifest:
        # The tool surface rides the system text: the harness's definitions,
        # the call convention, and the anti-simulation rules. Nothing else
        # about the request changes - adapters stay tool-agnostic.
        system = manifest + ("\n\n" + system if system else "")
        # And a compact anchor joins the final user turn, because a CLI whose
        # own persona is tool-anchored (codex is the proven case) believes
        # its session inventory over the system text.
        for message in reversed(messages):
            if message["role"] == "user":
                anchor = cli_structured_reply.render_anchor() if structured_tools else render_tool_anchor(tools)
                message["content"] = (message["content"] + "\n\n" + anchor) if message["content"] else anchor
                break
    if dynamic_tools:
        system = HOST_EXECUTION_NOTE + ("\n\n" + system if system else "")
    if images:
        system = (system + "\n\n" if system else "") + IMAGE_COORDINATE_NOTE
    request = {
        "model": upstream_model,
        "messages": messages,
        "system": system,
        "effort": effort if isinstance(effort, str) else None,
        "max_tokens": wanted_output if type(wanted_output) is int and wanted_output > 0 else None,
        "stream": bool(payload.get("stream")),
        "tools": tools,
        "tool_choice": tool_choice,
    }
    if structured_tools:
        request["host_tool_schema"] = cli_structured_reply.reply_schema(tools, tool_choice)
    thinking = payload.get("thinking")
    if isinstance(thinking, dict):
        request["thinking"] = {key: thinking[key] for key in ("type", "display") if key in thinking}
    summary = payload.get("reasoning_summary")
    if summary is not None:
        if provider_id != "codex" or summary not in {"auto", "concise", "detailed"}:
            raise CliRouteError("This CLI route does not support the requested published reasoning summary mode.")
        request["reasoning_summary"] = summary
    if images:
        request["images"] = images
    if provider_id == "codex" and payload.get("service_tier") is not None:
        request["service_tier"] = payload["service_tier"]
    search = payload.get("_web_search")
    if search is not None:
        # Set only by the Responses planner, for a route whose catalogue entry
        # carries web_search; the adapter runs the search itself.
        if getattr(adapter, "WEB_SEARCH", False) is not True:
            raise CliRouteError("This CLI route cannot run hosted web search.")
        request["web_search"] = search
    if dynamic_tools:
        # Keep typed tool calls/results for Codex's native history injection.
        # Flattening these into a new user transcript loses the tool loop.
        request["history"] = [message for message in history
                              if isinstance(message, dict) and message.get("role") in {"user", "assistant"}]
    return {
        "cli": True,
        "cli_tool_calls": bool(tools),
        "protocol": "cli",
        "url": None,
        "headers": {},
        "body": request,
        "compatibility": {
            "cli_transport": getattr(adapter, "TRANSPORT", "unknown"),
            "system_prompt_transport": getattr(adapter, "SYSTEM_PROMPT_TRANSPORT", "prompt"),
            "cli_image_transport": getattr(adapter, "IMAGE_TRANSPORT", None),
            "cli_images": len(images),
            **({"cli_image_compaction": image_compaction} if image_compaction["removed"] else {}),
            **({"cli_host_tools": "structured"} if structured_tools else {}),
            **({"cli_tools": len(tools),
                "cli_tools_dropped": len(raw_tools) - len(tools)}
               if tools and isinstance(raw_tools, list) and len(raw_tools) != len(tools)
               else {"cli_tools": len(tools)} if tools else {}),
        },
    }


# Whole-turn budget. Keep it inside Claude Code's ping-only ceiling (see
# gateway.CLI_KEEPALIVE_REPEAT) so a stuck turn ends with the hub's own error
# rather than a client-side idle abort that retries the turn from scratch.
CLI_TURN_TIMEOUT = 1140


def run_turn(provider_id: str, request: dict, *, parse_tool_calls: bool = False, timeout: int = CLI_TURN_TIMEOUT):
    """Start the adapter's turn generator (text_delta/thinking_delta/stop)."""
    adapter = adapter_for(provider_id)
    timing = TurnTiming(provider_id, request.get("model"))
    timing.cancel = request.get("_cli_cancel")
    if request.get("_cli_queue_ms") is not None:
        timing.label(gateway_queue_ms=request["_cli_queue_ms"])
    request = {**request, "_cli_timing": timing}

    def start():
        remaining = max(0.01, timeout - (time.monotonic() - timing.started))
        events = (_tool_turn(adapter, request, timeout=remaining) if parse_tool_calls
                  else observe_events(adapter.run_turn(request, timeout=remaining), timing))
        thinking = request.get("thinking") or {}
        if isinstance(thinking, dict) and (thinking.get("type") == "disabled" or thinking.get("display") == "omitted"):
            return _without_thinking(events)
        return events

    return ManagedTurn(start, timing, timeout,
                       inline_close=bool(getattr(adapter, "NONBLOCKING_CLOSE", False)))


def _without_thinking(events):
    """Honor display suppression without turning reasoning into answer text."""
    try:
        pinged = False
        for event in events:
            if event.get("type") == "thinking_delta":
                if not pinged:
                    yield {"type": "ping"}
                    pinged = True
            else:
                yield event
    finally:
        close = getattr(events, "close", None)
        if callable(close):
            close()


def _tool_turn(adapter, request, *, timeout):
    """Allow one protocol correction, with no replay of executed host calls."""
    deadline = time.monotonic() + timeout
    native = getattr(adapter, "HOST_TOOL_TRANSPORT", None) == "dynamic"
    attempts = 1 if native else 2
    current = request
    for attempt in range(attempts):
        parser = cli_structured_reply.parse_stream if request.get("host_tool_schema") else _parse_tool_stream
        options = ({"stop_after_object": bool(getattr(adapter, "EARLY_STRUCTURED_REPLY", False))}
                   if request.get("host_tool_schema") else {"native": native})
        events = parser(observe_events(adapter.run_turn(current, timeout=max(0.01, deadline - time.monotonic())),
                                       request.get("_cli_timing")),
                        tools=request.get("tools"), tool_choice=request.get("tool_choice"), **options)
        retry = False
        pending_text = []
        pending_bytes = 0
        try:
            for event in events:
                if event.get("code") == "invalid_cli_tool_call" and attempt + 1 < attempts \
                        and time.monotonic() < deadline:
                    retry = True
                    break
                kind = event.get("type")
                if kind == "text_delta" and not native:
                    # An invalid attempt has not done any host work. Keep its
                    # narration private until the handoff/reply is validated,
                    # so retrying cannot print the same promises twice.
                    pending_bytes += len((event.get("text") or "").encode("utf-8"))
                    if pending_bytes > 1024 * 1024:
                        yield {"type": "error", "message": "The CLI reply exceeded the buffered text limit."}
                        return
                    pending_text.append(event)
                    if len(pending_text) == 1:
                        # Start the gateway's heartbeat without exposing a
                        # provisional reply while this attempt is validated.
                        yield {"type": "ping"}
                    continue
                if kind in {"tool_call", "message_stop"}:
                    yield from pending_text
                    pending_text.clear()
                yield event
        finally:
            events.close()
        if not retry:
            return
        # Only the rejected reply is undone. Every host result already in the
        # transcript really happened, so this must not read as "do that last
        # call again": a model told to "reissue the intended call" re-ran a
        # completed edit, which then failed on a file it had already changed.
        current = {**request, "messages": [*request.get("messages", []), {
            "role": "user",
            "content": "Your previous response was rejected for its formatting alone, and nothing in it "
                       "was executed. Every host tool result already in this conversation is real and "
                       "complete: do not perform that work again or restate its outcome as new. Continue "
                       "from those results and issue only the call that comes next - or the final answer "
                       "if no call remains - "
                       + ("as the structured response, with text and tool_calls whose arguments are "
                          "JSON-encoded objects. " if request.get("host_tool_schema") else
                          "using a complete JSON object between the exact tool-call delimiters from the "
                          "tool instructions. ")
                       + "Never simulate a result.",
        }]}


def _parse_tool_stream(events, *, tools=None, tool_choice=None, native=False):
    """Rewrite one adapter event stream, cutting call envelopes out of text.

    Text passes through unless the parser is mid-envelope; each complete,
    valid envelope becomes a {"type": "tool_call"} event, and a turn that
    produced any call stops with stop_reason "tool_use" so the harness runs
    its loop. Calls are released only after a successful terminal event, so a
    later malformed envelope or cancellation cannot execute a partial batch.
    Malformed envelopes become explicit errors without leaking arguments. The inner
    generator is always closed with the wrapper - that is what kills the
    child CLI when a turn is abandoned.
    """
    parser = ToolCallParser()
    calls = []

    def flush(chunks):
        for ptype, payload in chunks:
            if ptype == "text":
                if payload:
                    yield {"type": "text_delta", "text": payload}
            else:
                add_call(payload)

    def add_call(call):
        if len(calls) >= MAX_CALLS_PER_TURN:
            raise ToolCallError("too many tool calls in one turn")
        validated = validate_host_call(call, tools)
        if any(prior["id"] == validated["id"] for prior in calls):
            raise ToolCallError("duplicate tool call id")
        calls.append(validated)

    try:
        for event in events:
            kind = event.get("type") if isinstance(event, dict) else None
            if kind == "text_delta":
                if native:
                    yield event
                else:
                    yield from flush(parser.feed(event.get("text") or ""))
            elif kind == "tool_call":
                add_call(event)
            elif kind == "message_stop":
                if _stop_reason(event.get("stop_reason")) is None:
                    yield {"type": "error", "message": "The CLI turn did not complete successfully "
                           f"({event.get('stop_reason')!s})."}
                    return
                yield from flush(parser.finish())
                choice = tool_choice if isinstance(tool_choice, dict) else {}
                if choice.get("type") in {"any", "required", "tool"} and not calls:
                    raise ToolCallError("the model did not make the required host tool call")
                if choice.get("type") == "tool" and any(
                        call["name"] != choice.get("name") for call in calls):
                    raise ToolCallError("the model did not call the requested host tool")
                for call in calls:
                    yield {**call, "type": "tool_call"}
                stop_reason = "tool_use" if calls else event.get("stop_reason")
                yield {"type": "message_stop", "stop_reason": stop_reason}
                return
            elif kind == "error":
                yield event
                return
            else:
                yield event
        parser.finish()
        yield {"type": "error", "message": "The CLI stream ended before completing the turn."}
    except ToolCallError as exc:
        yield {"type": "error", "code": "invalid_cli_tool_call",
               "message": f"Invalid CLI host tool call: {exc}."}
    finally:
        close = getattr(events, "close", None)
        if callable(close):
            close()


# ---------------------------------------------------------------------------
# Wire translation
# ---------------------------------------------------------------------------

_STOP_REASONS = {"end_turn", "max_tokens", "stop_sequence", "tool_use"}


def _stop_reason(value):
    if value in {"completed", "success", "stop"}:
        return "end_turn"
    return value if value in _STOP_REASONS else None


def relay_cli_turn(events, emit, *, model, input_tokens=0) -> dict:
    """Translate adapter turn events into Anthropic Messages wire events.

    ``emit`` receives ready wire dicts (the gateway writes them as SSE chunks
    for a streaming client or collects them for a buffered reply). A valid
    adapter usage snapshot replaces the gateway's input estimate and unknown
    (zero) output count. Snapshots are per request, never lifetime counters;
    repeated updates replace rather than accumulate. Providers without usage
    telemetry retain the existing estimate instead of an invented measurement.

    Returns a summary: ``error`` (None on success), ``started`` (whether any
    content reached the wire), ``stop_reason`` and ``usage``. Exactly one
    terminal envelope (message_delta + message_stop) is emitted on success;
    on error none is, so the caller can map the failure to its transport's
    own error shape without a half-open message.
    """
    message_id = "msg_" + uuid.uuid4().hex[:24]
    started = False
    open_block = None
    open_source = None
    index = 0
    stop_reason = "end_turn"
    error = None
    error_status = 502
    timing = getattr(events, "timing", None)
    usage = {"input_tokens": int(input_tokens or 0), "output_tokens": 0}

    def start_message():
        nonlocal started
        emit({"type": "message_start", "message": {
            "id": message_id, "type": "message", "role": "assistant",
            "model": model, "content": [], "stop_reason": None,
            "stop_sequence": None,
            "usage": dict(usage)}})
        started = True

    def open(kind, source=None):
        nonlocal open_block, open_source, index
        if open_block == kind and open_source == source:
            return
        if open_block is not None:
            emit({"type": "content_block_stop", "index": index})
            index += 1
        emit({"type": "content_block_start", "index": index,
              "content_block": ({"type": "thinking", "thinking": ""} if kind == "thinking"
                                else {"type": "text", "text": ""})})
        open_block = kind
        open_source = source

    searches = {}

    def search(event):
        """A hosted search the CLI ran, as the Messages server tool it is.

        The server_tool_use block opens when the search starts, so the client
        can show it running, and closes with its query and action followed by
        the web_search_tool_result that the protocol pairs with it.
        """
        nonlocal open_block, open_source, index
        if not started:
            start_message()
        source = event.get("id")
        running = open_block == "search" and open_source == source
        if not running:
            if open_block is not None:
                emit({"type": "content_block_stop", "index": index})
                index += 1
            searches[source] = "srvtoolu_" + uuid.uuid4().hex[:24]
            emit({"type": "content_block_start", "index": index, "content_block": {
                "type": "server_tool_use", "id": searches[source], "name": "web_search", "input": {}}})
            open_block, open_source = "search", source
        if event.get("status") != "completed":
            return
        action = event.get("action") if isinstance(event.get("action"), dict) else {"type": "other"}
        query = action.get("query") or action.get("url") or ""
        emit({"type": "content_block_delta", "index": index, "delta": {
            "type": "input_json_delta",
            "partial_json": json.dumps({"query": query, "action": action}, ensure_ascii=False,
                                       separators=(",", ":"))}})
        emit({"type": "content_block_stop", "index": index})
        index += 1
        emit({"type": "content_block_start", "index": index, "content_block": {
            "type": "web_search_tool_result", "tool_use_id": searches.pop(source),
            "content": [{"type": "web_search_result", "url": result["url"], "title": result.get("title", "")}
                        for result in event.get("results") or []]}})
        emit({"type": "content_block_stop", "index": index})
        index += 1
        open_block = open_source = None

    try:
        for event in events:
            kind = event.get("type") if isinstance(event, dict) else None
            if kind == "ping":
                emit({"type": "ping"})
                continue
            if kind == "usage":
                reported = event.get("usage")
                fields = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
                if isinstance(reported, dict) and all(
                        type(reported.get(key)) is int and reported[key] >= 0
                        for key in fields[:2]) and all(
                        type(reported[key]) is int and reported[key] >= 0
                        for key in fields[2:] if key in reported):
                    # Explicit zeros clear cache counts already sent at
                    # message_start when a later snapshot has no cache use.
                    usage = {key: reported.get(key, 0) for key in fields}
                continue
            if kind == "error":
                error = str(event.get("message") or "The CLI turn failed.")
                if event.get("http_status") == 400:
                    error_status = 400
                break
            if kind == "message_stop":
                stop_reason = _stop_reason(event.get("stop_reason"))
                if stop_reason is None:
                    error = f"The CLI turn did not complete successfully ({event.get('stop_reason')!s})."
                break
            if kind == "thinking_delta":
                text = event.get("text")
                if not text:
                    continue
                if not started:
                    start_message()
                open("thinking", (event.get("source_id"), event.get("thinking_kind"), event.get("part_index")))
                emit({"type": "content_block_delta", "index": index,
                      "delta": {"type": "thinking_delta", "thinking": text}})
            elif kind == "text_delta":
                text = event.get("text")
                if not text:
                    continue
                if not started:
                    start_message()
                open("text", event.get("source_id"))
                emit({"type": "content_block_delta", "index": index,
                      "delta": {"type": "text_delta", "text": text}})
                if timing:
                    timing.mark("first_visible_text")
            elif kind == "web_search":
                search(event)
            elif kind == "tool_call":
                if not started:
                    start_message()
                if open_block is not None:
                    emit({"type": "content_block_stop", "index": index})
                    index += 1
                    open_block = None
                emit({"type": "content_block_start", "index": index,
                      "content_block": {"type": "tool_use", "id": event["id"],
                                        "name": event["name"], "input": {}}})
                emit({"type": "content_block_delta", "index": index,
                      "delta": {"type": "input_json_delta",
                                "partial_json": json.dumps(event["input"], ensure_ascii=False,
                                                           separators=(",", ":"))}})
                emit({"type": "content_block_stop", "index": index})
                index += 1
        else:
            error = "The CLI stream ended before completing the turn."
    except BaseException:
        close = getattr(events, "close", None)
        if callable(close):
            close()
        raise
    try:
        if error is not None:
            return {"error": error, "started": started, "stop_reason": None, "usage": usage,
                    "http_status": error_status}
        if not started:
            start_message()
        if open_block is not None:
            emit({"type": "content_block_stop", "index": index})
        emit({"type": "message_delta",
              "delta": {"stop_reason": stop_reason, "stop_sequence": None},
              "usage": dict(usage)})
        emit({"type": "message_stop"})
        if timing:
            timing.delivered = True
            timing.mark("response_complete")
        return {"error": None, "started": True, "stop_reason": stop_reason, "usage": usage}
    finally:
        close = getattr(events, "close", None)
        if callable(close):
            close()
