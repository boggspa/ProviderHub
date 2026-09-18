"""Native Responses routes for Codex. No Messages conversion or prompt logging."""
from __future__ import annotations

import copy
import hashlib
import hmac
import json
import re
import select
import socket
import threading
import time

from bridge_core import BridgeError, atomic_json, read_json
from hub_config import connection_signature, qualify, split_route
from protocol import mask_effort_rejection
from providers import PROVIDERS, ProviderError, _auth_headers, _chat_effort, validate_connection
from responses_tools import (flatten_tools, input_names, normalize_custom_calls, output_names, register,
                             restore_custom_call, split_hosted_search, strip_goal_budget)
from responses_bridge import ENVELOPE_PREFIX, MessagesResponsesAdapter, ReasoningEnvelope, to_messages
from openrouter_provider import OpenRouterError, finalize as openrouter_finalize, app_headers as openrouter_app_headers
from effort_map import cap_high_end, map_effort, nearest_effort, ollama_effort_aliases
from spawn_depth import (SPAWN_NAMESPACE, SPAWN_TOOL, apply_subagent_model,
                         is_spawn_tool_reference)
from rate_limit import (MAX_UPSTREAM_ATTEMPTS, RETRYABLE_STATUSES, SLOT_RETRY_AFTER, SLOT_WAIT_TIMEOUT,
                        THROTTLE_CAP, parse_retry_after, wait_for_slot)


NATIVE_PROVIDERS = frozenset({"grok", "ollama", "openrouter"})
MAX_BODY = 32 * 1024 * 1024
REQUEST_FIELDS = frozenset({
    "model", "input", "instructions", "tools", "tool_choice", "parallel_tool_calls",
    "stream", "stream_options", "store", "previous_response_id", "include", "reasoning",
    "text", "max_output_tokens", "temperature", "top_p", "top_logprobs", "metadata",
    "truncation", "service_tier", "prompt_cache_key", "safety_identifier", "user",
})
LOCAL_FIELDS = frozenset({"client_metadata"})
TERMINAL_EVENTS = frozenset({"response.completed", "response.incomplete", "response.failed", "error"})


class ResponseOwnership:
    """Only ID/account ownership for explicitly stored xAI responses; no content."""

    def __init__(self, root):
        self.path = root / "response-ownership.json"
        self.lock = threading.Lock()
        self.records = read_json(self.path)

    def check(self, identifier, scope):
        with self.lock:
            if not isinstance(identifier, str) or self.records.get(identifier, {}).get("scope") != scope:
                raise BridgeError("This previous response is not stored by this gateway for the selected model and account. Resend the full history.")

    def remember(self, identifier, scope):
        if not isinstance(identifier, str) or not 1 <= len(identifier) <= 512:
            return
        with self.lock:
            self.records[identifier] = {"scope": scope, "created": int(time.time())}
            if len(self.records) > 10000:
                self.records = dict(sorted(self.records.items(), key=lambda item: item[1].get("created", 0))[-10000:])
            atomic_json(self.path, self.records)


_TASK_HEADER_PREFIXES = ("Message Type:", "Task name:", "Sender:", "Payload:")

# Codex multi_agent_v2 call arguments follow the Responses convention where
# arguments travel as a JSON string, so the wire envelope can arrive as a
# list, a dict, or a stringified list/dict. A previous turn's leak can also
# re-enter history as `subagent: [...]` prose once a weaker model echoes it.
_SUBAGENT_ECHO_PREFIX = "subagent:"
_MAX_WIRE_DEPTH = 5
_MAX_SCHEMA_DEPTH = 64
_VACUOUS_SCHEMA_KEYWORDS = ("anyOf", "oneOf", "type")
_SCHEMA_MAPS = ("properties", "patternProperties", "$defs", "definitions", "dependentSchemas")

# Codex typed envelopes (NEW_TASK/MESSAGE/FINAL_ANSWER) also carry a trailing
# end-of-message frame. Like the routing headers it is harness framing, not
# task content, and weaker models echo it back into history verbatim.
_WIRE_FOOTER = "[END_OF_MESSAGE]"

# Our own agent_message attribution. A previous turn's normalized output
# re-enters history once the model echoes it, possibly with a drifted agent
# name (different sibling, case/whitespace drift), so attribution must be
# stripped by shape rather than guarded by exact match.
_SUBAGENT_PREFIX_RE = re.compile(r"\[subagent_[^\[\]]+\]\s*:\s*", re.IGNORECASE)


def _strip_subagent_prefixes(text):
    """Strip stacked `[subagent_*]:` prefixes so attribution is idempotent.

    Removes every leading subagent prefix before the caller adds the
    current one, so echoed attributions collapse to a single prefix
    instead of compounding across turns.
    """
    cleaned = text.strip()
    while True:
        match = _SUBAGENT_PREFIX_RE.match(cleaned)
        if not match:
            return cleaned
        cleaned = cleaned[match.end():]


def _strip_task_header(text):
    """Strip Codex envelope headers and the end-of-message frame.

    Drops routing header lines plus a trailing [END_OF_MESSAGE] marker,
    whether it stands on its own line or trails the final content line
    (may return ""; caller falls back).
    """
    kept = []
    for line in text.splitlines():
        if line.strip().startswith(_TASK_HEADER_PREFIXES):
            continue
        stripped = line.strip()
        if stripped == _WIRE_FOOTER:
            continue
        if stripped.endswith(_WIRE_FOOTER):
            line = line[: line.rfind(_WIRE_FOOTER)].rstrip()
            if not line.strip():
                continue
        kept.append(line)
    return "\n".join(kept).strip()


def _looks_like_wire_json(text):
    stripped = text.lstrip()
    return stripped.startswith(("{", "["))


def _extract_wire_value(value, depth=0):
    """Extract plaintext from a parsed wire-envelope value, or None.

    Returns None for shapes that are not recognisably part of the Codex
    inter-assistant protocol, so callers keep their existing stringification
    fallback for genuinely opaque data.
    """
    if depth > _MAX_WIRE_DEPTH:
        return None
    if isinstance(value, list):
        parts = []
        matched = False
        for block in value:
            if isinstance(block, dict):
                kind = block.get("type")
                if kind == "input_text":
                    matched = True
                    text = block.get("text", "")
                    cleaned = _clean_wire_text(text, depth + 1) if isinstance(text, str) else str(text)
                    if cleaned:
                        parts.append(cleaned)
                elif kind == "encrypted_content":
                    matched = True
                    inner = block.get("encrypted_content", "")
                    parts.append(_clean_wire_text(inner, depth + 1) if isinstance(inner, str) else str(inner))
                else:
                    nested = _extract_wire_value(block, depth + 1)
                    if nested is not None:
                        matched = True
                        parts.append(nested)
                    elif block:
                        parts.append(json.dumps(block))
            elif isinstance(block, str):
                nested = _clean_wire_text(block, depth + 1)
                if nested != block:
                    matched = True
                parts.append(nested)
            elif block is not None:
                parts.append(str(block))
        if not matched:
            return None
        return "\n".join(part for part in parts if part).strip()
    if isinstance(value, dict):
        kind = value.get("type")
        if kind == "input_text" and isinstance(value.get("text"), str):
            return _clean_wire_text(value["text"], depth + 1)
        if kind == "encrypted_content":
            inner = value.get("encrypted_content", "")
            return _clean_wire_text(inner, depth + 1) if isinstance(inner, str) else str(inner)
        # Dict-form envelope: the encrypted_content/input_text keys are
        # protocol vocabulary, so their values are always unwrapped. A bare
        # "text" key is generic prose storage: only treat it as an envelope
        # when cleaning actually removes protocol markers, otherwise the
        # caller stringifies (preserving key names for opaque data).
        if isinstance(value.get("encrypted_content"), str):
            return _clean_wire_text(value["encrypted_content"], depth + 1)
        if isinstance(value.get("input_text"), str):
            return _clean_wire_text(value["input_text"], depth + 1)
        if isinstance(value.get("text"), str):
            cleaned = _clean_wire_text(value["text"], depth + 1)
            if cleaned != value["text"]:
                return cleaned
        for key in ("input", "arguments", "blocks", "content", "payload"):
            if key in value:
                nested = _extract_wire_value(value[key], depth + 1)
                if nested is not None:
                    return nested
        return None
    return None


def _clean_wire_text(text, depth=0):
    """Reduce inter-assistant protocol text to the plaintext it carries.

    Plain prose passes through unchanged. Routing headers are stripped, and
    a remainder that is itself a stringified envelope (including a previous
    turn's `subagent: [...]` model echo) is unwrapped recursively so wire
    JSON never compounds across turns.
    """
    if not isinstance(text, str) or depth > _MAX_WIRE_DEPTH:
        return text
    stripped = _strip_task_header(text)
    if not stripped:
        # Pure routing headers (or blank): no payload to forward. Falling
        # back to the original here would re-emit the headers as the leak.
        return ""
    candidate = stripped
    lowered = candidate.lstrip().lower()
    if lowered.startswith(_SUBAGENT_ECHO_PREFIX):
        remainder = candidate.lstrip()[len(_SUBAGENT_ECHO_PREFIX):].strip()
        if _looks_like_wire_json(remainder):
            try:
                parsed = json.loads(remainder)
            except ValueError:
                parsed = None
            extracted = _extract_wire_value(parsed, depth + 1) if parsed is not None else None
            if extracted:
                return extracted
        elif remainder:
            return _clean_wire_text(remainder, depth + 1)
        return candidate
    if candidate != text.strip() and not _looks_like_wire_json(candidate):
        return candidate
    if _looks_like_wire_json(candidate):
        try:
            parsed = json.loads(candidate)
        except ValueError:
            return candidate
        extracted = _extract_wire_value(parsed, depth + 1)
        if extracted is not None:
            return extracted
    return candidate


def _extract_subagent_task(args):
    """Extract a human-readable instruction from a subagent call payload.

    Codex multi_agent_v2 wraps delegations in a wire envelope: a list of
    blocks where input_text blocks carry routing headers and an
    encrypted_content block carries the plaintext instruction. The envelope
    may also arrive stringified (Responses arguments are strings by
    convention) or in dict form. Unknown shapes fall back to readable
    stringification so content is never silently dropped.
    """
    if isinstance(args, str):
        if not args.strip():
            return str(args)
        return _clean_wire_text(args)
    if isinstance(args, (list, dict)):
        extracted = _extract_wire_value(args)
        if extracted:
            return extracted
        if isinstance(args, list):
            combined = "\n".join(
                json.dumps(block) if isinstance(block, dict) else str(block)
                for block in args if block is not None
            ).strip()
            if combined:
                return combined
        return json.dumps(args)
    return str(args)


def _normalize_multi_agent_items(input_list):
    """Normalize Codex multi_agent_v2 history items to standard message items at ingress.
    
    Maps:
    - multi_agent_call / subagent_call -> user message (extracted task instruction)
    - multi_agent_call_output / subagent_call_output -> user message (synthetic subagent result description)
    - agent_message -> user message (with agent prefix)
    
    All multi-agent items become standard message items with role and string content,
    ensuring they pass the Responses whitelist validation while preserving subagent
    context for the model. Subagent aliases use the subagent_* naming convention.
    """
    if not isinstance(input_list, list):
        return input_list
    
    normalized = []
    for index, item in enumerate(input_list):
        if not isinstance(item, dict):
            normalized.append(item)
            continue
        
        item_type = item.get("type", "message")
        
        # multi_agent_call / subagent_call -> user message with the extracted task.
        # The delegation is a prompt for the subagent to execute, not assistant
        # speech; a user-role tail also sidesteps the Mistral prefill trap.
        if item_type in ("multi_agent_call", "subagent_call"):
            agent_name = item.get("agent") or item.get("recipient") or "agent"
            args = item.get("arguments") or item.get("input") or {}
            call_id = item.get("id", item.get("call_id", ""))
            task = _extract_subagent_task(args)
            content = f"[Task from parent agent for subagent_{agent_name} ({call_id})]: {task}"
            normalized.append({
                "type": "message",
                "role": "user",
                "content": content,
            })
        # multi_agent_call_output / subagent_call_output -> message describing the subagent result
        elif item_type in ("multi_agent_call_output", "subagent_call_output"):
            call_id = item.get("call_id") or item.get("id") or f"subagent_call_{index}"
            output = item.get("output") or item.get("result") or ""
            if isinstance(output, str):
                output_str = _clean_wire_text(output) if output.strip() else ""
            elif isinstance(output, (dict, list)):
                output_str = _extract_subagent_task(output)
            else:
                output_str = str(output)
            content = f"[subagent_{call_id} returned: {output_str}]"
            normalized.append({
                "type": "message",
                "role": "user",
                "content": content,
            })
        # agent_message -> user message with agent prefix.
        # Synthetic subagent speech is third-party content, not this turn's
        # assistant output: a user role keeps it out of the Mistral prefill
        # trap (see multi_agent_call above) so the model reads it as
        # received history instead of continuing it as its own prefill.
        elif item_type == "agent_message":
            agent_name = item.get("agent") or item.get("sender") or "subagent"
            content = item.get("content") or ""
            if isinstance(content, str):
                # Break the echo chamber: a previous turn's leaked envelope
                # re-enters here as `subagent: [...]` prose once the model
                # repeats it. Unwrap it instead of amplifying it.
                content = _clean_wire_text(content) if content else ""
                content = _strip_subagent_prefixes(content)
                prefix = f"[subagent_{agent_name}]"
                content = f"{prefix}: {content}" if content else prefix
            elif isinstance(content, (dict, list)):
                extracted = _extract_wire_value(content)
                if extracted:
                    content = f"[subagent_{agent_name}]: {_strip_subagent_prefixes(extracted)}"
                else:
                    # Stringify non-string content
                    content = f"{agent_name}: {json.dumps(content)}"
            else:
                content = f"{agent_name}: {str(content)}"
            normalized.append({
                "type": "message",
                "role": "user",
                "content": content,
            })
        else:
            normalized.append(item)

    return normalized


def _request_shape(payload):
    """Summarize a Responses request for last-responses-shape.json.

    Records the ordered input-type sequence plus multi-agent
    attribution (agent names, call ids) and pre-flattening tool names,
    so a later transcript can be correlated with what each
    parent/child request actually carried. All free text is truncated;
    no message content or credentials are recorded.
    """
    shape = {
        "fields": sorted(payload),
        "model": str(payload.get("model", ""))[:120],
    }
    inputs = payload.get("input", [])
    if isinstance(inputs, str):
        shape["input"] = "text"
    elif isinstance(inputs, list):
        items = []
        for item in inputs:
            if not isinstance(item, dict):
                items.append({"type": type(item).__name__[:20]})
                continue
            kind = str(item.get("type", "message"))[:50]
            entry = {"type": kind}
            if kind in ("multi_agent_call", "subagent_call"):
                entry["agent"] = str(item.get("agent") or item.get("recipient") or "")[:80]
                entry["call_id"] = str(item.get("id", item.get("call_id", "")))[:80]
            elif kind in ("multi_agent_call_output", "subagent_call_output"):
                entry["call_id"] = str(item.get("call_id") or item.get("id") or "")[:80]
            elif kind == "agent_message":
                entry["agent"] = str(item.get("agent") or item.get("sender") or "")[:80]
                entry["role"] = str(item.get("role") or "")[:20]
            elif kind == "function_call":
                entry["name"] = str(item.get("name") or "")[:80]
            elif kind == "function_call_output":
                entry["call_id"] = str(item.get("call_id") or "")[:80]
            elif kind == "message":
                entry["role"] = str(item.get("role") or "")[:20]
            items.append(entry)
        shape["input_types"] = [entry["type"] for entry in items]
        shape["input_items"] = items
    else:
        shape["input"] = type(inputs).__name__[:20]
    tools = payload.get("tools", [])
    if isinstance(tools, list):
        names = []
        for tool in tools:
            if not isinstance(tool, dict):
                continue
            if tool.get("type") == "namespace":
                namespace = str(tool.get("name", ""))[:60]
                children = tool.get("tools")
                if isinstance(children, list):
                    for child in children:
                        if isinstance(child, dict):
                            names.append(f"{namespace}.{child.get('name', '')}"[:120])
                else:
                    names.append(namespace + ".*")
            else:
                names.append(str(tool.get("name", ""))[:120])
        shape["tool_names"] = sorted(set(names))
    shape["tool_choice"] = str(payload.get("tool_choice", ""))[:120]
    return shape


def prune_vacuous_schema(value, depth=0):
    """Drop the structured-output keywords Ollama's GGUF runner cannot compile.

    llama.cpp turns a json_schema format into a GBNF grammar, and an empty
    alternation compiles to a rule with no body - a literal `root ::= ` - which
    its own grammar parser then refuses. The request dies as "Failed to
    initialize samplers: failed to parse grammar", which names neither the
    schema nor the keyword, so the turn fails with nothing in it to act on.
    Measured against a local GGUF model, an empty anyOf, oneOf or type does it
    at any depth including through $defs; an empty allOf or enum converts
    cleanly and is left alone.

    Each of the three is vacuous in the first place. An empty anyOf or oneOf
    admits no value at all and an empty type permits no type, so a schema
    carrying one cannot be satisfied by any output the model could produce -
    there is no constraint here to preserve. Dropping the keyword leaves that
    position unconstrained, which is the one reading that yields a usable
    grammar, and it is already how the same request behaves on Ollama's MLX
    runner, which does not go through GBNF at all. GGUF was the odd one out,
    not the request.

    Tool schemas are deliberately untouched: the same empty anyOf in a tool's
    parameters is accepted, with a free, forced or required tool choice alike,
    because tool calls do not take this path. Repairing them too would be
    rewriting schemas that upstream had no quarrel with.
    """
    repairs = 0
    if depth > _MAX_SCHEMA_DEPTH:
        return value, repairs
    if isinstance(value, list):
        for item in value:
            repairs += prune_vacuous_schema(item, depth + 1)[1]
        return value, repairs
    if not isinstance(value, dict):
        return value, repairs
    for keyword in _VACUOUS_SCHEMA_KEYWORDS:
        if isinstance(value.get(keyword), list) and not value[keyword]:
            del value[keyword]
            repairs += 1
    for key, item in value.items():
        # Under properties and its siblings the keys are property names, not
        # keywords, so a field that happens to be called "type" is descended
        # into rather than read as one.
        children = item.values() if key in _SCHEMA_MAPS and isinstance(item, dict) else (item,)
        for child in children:
            repairs += prune_vacuous_schema(child, depth + 1)[1]
    return value, repairs


def prepare_native(runtime, payload):
    if not isinstance(payload, dict):
        raise BridgeError("The Responses request must be an object.")
    requested = payload.get("model")
    provider_id, model_id = split_route(requested)
    if provider_id not in PROVIDERS:
        raise BridgeError("Choose a provider-qualified model from the Codex catalogue.")
    route = qualify(provider_id, model_id)
    spec = runtime.settings["_model_specs"].get(route)
    if spec is None:
        raise BridgeError("This model is absent from the current provider catalogue. Refresh before launching Codex.")
    unknown = [key for key, value in payload.items() if key not in REQUEST_FIELDS | LOCAL_FIELDS and value not in (None, False, [], {}, "")]
    if unknown:
        raise BridgeError("This Responses route does not support request field " + str(unknown[0])[:80] + ".")
    body = {key: copy.deepcopy(value) for key, value in payload.items() if key in REQUEST_FIELDS}
    if not isinstance(body.get("input"), (str, list)):
        raise BridgeError("Responses input must be text or an array of input items.")
    for flag in ("stream", "store", "parallel_tool_calls"):
        if flag in body and type(body[flag]) is not bool:
            raise BridgeError(f"{flag} must be true or false.")
    # Codex uses full history and store:false. Preserve explicit xAI storage
    # requests, but do not opt an omitted store flag into cloud persistence.
    body.setdefault("store", False)
    body.setdefault("stream", False)
    body["model"] = model_id
    if "max_output_tokens" in body:
        count = body["max_output_tokens"]
        if type(count) is not int or count <= 0:
            raise BridgeError("max_output_tokens must be a positive integer.")
        if type(spec.get("max_output")) is int:
            body["max_output_tokens"] = min(count, spec["max_output"])
    tools = body.get("tools", [])
    # The hosted search tool is OpenAI's, and nothing here serves OpenAI, so it
    # is lifted out before flattening and answered by the provider's own search
    # if the provider has one. Only a route whose catalogue entry carries
    # web_search has that translation written for it; the rest say so plainly,
    # because a model told it can search and then handed no search answers from
    # memory and presents it as fresh.
    tools, search = split_hosted_search(tools)
    # Codex marks a goal's token budget optional and asks the model to omit
    # it. Taking the property away is the same instruction stated where a
    # weaker route cannot decline it, so goals start unlimited everywhere
    # rather than only where the description happened to land.
    if runtime.settings.get("codex_goal_budget") is not True:
        strip_goal_budget(tools)
    body["tools"], tool_map = flatten_tools(tools)
    if tools and spec.get("tools") is False:
        raise BridgeError("The selected model does not support tool calls.")
    if search is not None and not spec.get("web_search"):
        raise BridgeError("This route's provider does not run web search of its own, so the hosted web_search tool "
                          "cannot be honoured. Turn Codex web search off for this route, or choose a route on a "
                          "provider that searches.")
    if isinstance(body["input"], list):
        body["input"] = _normalize_multi_agent_items(body["input"])
        normalize_custom_calls(body["input"], tool_map)
        for item in body["input"]:
            if not isinstance(item, dict) or item.get("type", "message") not in {"message", "function_call", "function_call_output", "reasoning"}:
                offending_type = item.get("type", "unknown") if isinstance(item, dict) else type(item).__name__
                raise BridgeError(f"Unsupported Responses history item '{offending_type}'. Use the function-tool catalogue.")
            # Ollama's Responses endpoint accepts or rejects images itself.
            # Catalogue vision is picker metadata, not a request interceptor.
            # A tool result carries its parts in `output`, not `content`: a
            # Computer Use screenshot or a view_image call returns an
            # input_image there, so both fields have to be checked or the
            # provider rejects the turn instead of this gateway.
            if provider_id != "ollama" and spec.get("vision") is False:
                for parts in (item.get("content"), item.get("output")):
                    if isinstance(parts, list) and any(
                            isinstance(part, dict) and part.get("type") == "input_image" for part in parts):
                        raise BridgeError("The selected model does not advertise image input.")
    input_names(body["input"], tool_map)
    choice = body.get("tool_choice")
    if isinstance(choice, dict) and choice.get("type") == "function":
        choice["name"] = register(tool_map, choice.pop("namespace", None), choice.get("name"))
    key = runtime.provider_key(provider_id)
    connection = validate_connection(provider_id, runtime.settings["providers"][provider_id])
    scope = hmac.new(runtime.replay_key.encode(), json.dumps([
        route, connection_signature(provider_id, runtime.settings["providers"][provider_id]), key,
    ]).encode(), hashlib.sha256).hexdigest()
    headers = _auth_headers(provider_id, key, content_type=True)
    if provider_id == "openrouter":
        headers.update(openrouter_app_headers())
    if provider_id not in NATIVE_PROVIDERS:
        # Bound stored thinking traces for verbose reasoning providers;
        # descriptors without the field keep full fidelity.
        envelope = ReasoningEnvelope(runtime.root, PROVIDERS[provider_id].get("reasoning_store_cap") or 0)
        translated = to_messages(body, route, spec, envelope, scope)
        return {"body": translated, "headers": {"Content-Type": "application/json", "Authorization": "Bearer " + runtime.token},
                "url": None, "route": route, "requested": requested, "provider_id": provider_id,
                "scope": scope, "tool_map": tool_map, "private_key": key,
                "subagent_route": runtime.settings.get("codex_subagent_route"),
                "provider_name": PROVIDERS[provider_id]["name"], "protocol": "messages_bridge",
                "adapter": MessagesResponsesAdapter(requested, envelope, scope, tool_map)}
    if isinstance(body["input"], list) and any(isinstance(item, dict) and str(item.get("encrypted_content", "")).startswith(ENVELOPE_PREFIX) for item in body["input"]):
        raise BridgeError("This reasoning history belongs to a different provider connection. Start a new task when changing providers.")
    if provider_id == "openrouter":
        try:
            openrouter_finalize(body, spec, key, responses=True, search=search)
        except OpenRouterError as exc:
            raise BridgeError(str(exc)) from exc
        url = connection["base_url"] + "/v1/responses"
    elif provider_id == "ollama":
        if body.get("previous_response_id") or body.get("store"):
            raise BridgeError("Ollama Responses is stateless. Send the full input history with store:false.")
        text_format = body.get("text")
        text_format = text_format.get("format") if isinstance(text_format, dict) else None
        if isinstance(text_format, dict) and isinstance(text_format.get("schema"), dict):
            prune_vacuous_schema(text_format["schema"])
        # The daemon owns its cloud login. Never send the local gateway token.
        headers = {"Content-Type": "application/json", "Authorization": "Bearer ollama", "User-Agent": "ProviderHub/0.5"}
        tier = body.pop("service_tier", None)
        if tier not in (None, "auto", "default", "standard"):
            raise BridgeError("Ollama does not advertise a Responses Fast service tier.")
        reasoning = body.get("reasoning")
        if isinstance(reasoning, dict) and reasoning.get("effort") is not None:
            requested_effort = reasoning.get("effort")
            if not isinstance(requested_effort, str):
                raise BridgeError("reasoning.effort must be text.")
            supported = spec.get("effort_modes") or []
            aliases = ollama_effort_aliases(model_id)
            mapped = map_effort(requested_effort, supported, aliases)
            if mapped is None and supported:
                mapped = cap_high_end(aliases.get(requested_effort), supported)
            if mapped is None and supported:
                # The closest rank this model does serve, rather than a
                # refusal: Codex and Claude both read one refusal as the
                # model having no effort control at all.
                mapped = nearest_effort(requested_effort, supported)
            if mapped is not None:
                reasoning["effort"] = mapped
            elif supported:
                # nearest_effort answers every real rank but "none", so what
                # is left here asks to switch reasoning off on a model that
                # cannot, or is not a rank at all. Forwarding it verbatim
                # would hand the provider an unvalidated desktop string.
                raise BridgeError(
                    f"Ollama model does not support reasoning effort {requested_effort!r}.")
        url = connection["base_url"] + "/v1/responses"
    else:
        reasoning = body.get("reasoning")
        if reasoning is not None:
            if not isinstance(reasoning, dict):
                raise BridgeError("reasoning must be an object.")
            effort = _chat_effort("grok", {"output_config": {"effort": reasoning.get("effort")}}, spec)
            if effort is not None:
                reasoning["effort"] = effort
        tier = body.get("service_tier")
        if tier in {"fast", "priority"}:
            body["service_tier"] = "priority"
        elif tier in (None, "auto", "default", "standard"):
            body["service_tier"] = "default"
        else:
            raise BridgeError("Grok supports default or Priority processing.")
        if body.get("previous_response_id"):
            runtime.response_ownership.check(body["previous_response_id"], scope)
        url = connection["base_url"] + "/responses"
    if runtime.upstream_url is not None:
        url = runtime.upstream_url.rstrip("/") + "/v1/responses"
    return {"body": body, "headers": headers, "url": url, "route": route,
            "protocol": "responses",
            "requested": requested, "provider_id": provider_id, "scope": scope,
            "tool_map": tool_map,
            "subagent_route": runtime.settings.get("codex_subagent_route"),
            "private_key": key, "provider_name": PROVIDERS[provider_id]["name"]}


def rewrite_stream_event(event, tool_map, custom_items, subagent_route=None, spawn_items=None):
    """Restore provider tool names on one relayed Responses stream event.

    Returns False when the event is a JSON argument delta for a mapped
    apply_patch call. Codex core builds custom calls from
    output_item.done, so forwarding the JSON bytes would corrupt the
    patch input buffer; the full converted call still arrives via the
    output_item events.

    A spawn_agent call with no model of its own is given the configured
    sub-agent route. Its arguments are then spelled two ways in the same
    stream, so the id is remembered at output_item.added - the one event
    that carries the name while the arguments are still empty - and the
    rewrite is applied to every later spelling of that call. The deltas
    are dropped rather than rewritten: they are fragments of the original
    JSON and cannot be edited a piece at a time.
    """
    spawn_items = spawn_items if spawn_items is not None else set()
    if isinstance(event.get("item"), dict):
        if restore_custom_call(event["item"], tool_map):
            if isinstance(event["item"].get("id"), str):
                custom_items.add(event["item"]["id"])
        else:
            output_names(event["item"], tool_map)
            item = event["item"]
            if subagent_route and _is_spawn_call_item(item):
                if isinstance(item.get("id"), str):
                    spawn_items.add(item["id"])
                apply_subagent_model(item, subagent_route)
    kind = event.get("type")
    if kind in {"response.function_call_arguments.delta", "response.function_call_arguments.done"}:
        if event.get("item_id") in custom_items:
            return False
        if event.get("item_id") in spawn_items:
            if kind.endswith(".delta"):
                return False
            rewritten = {"type": "function_call", "namespace": SPAWN_NAMESPACE,
                         "name": SPAWN_TOOL, "arguments": event.get("arguments")}
            if apply_subagent_model(rewritten, subagent_route):
                event["arguments"] = rewritten["arguments"]
    return True


def _is_spawn_call_item(item):
    return (item.get("type") == "function_call"
            and is_spawn_tool_reference(item.get("namespace"), item.get("name")))


def _client_gone(handler):
    """True when the local client disconnected (socket peek only, like the monitors)."""
    try:
        ready, _, _ = select.select([handler.connection], [], [], 0)
        return bool(ready and handler.connection.recv(1, socket.MSG_PEEK | socket.MSG_DONTWAIT) == b"")
    except (OSError, ValueError):
        return True


def usage_metadata(value):
    usage = value if isinstance(value, dict) else {}
    return {key: usage[key] if type(usage.get(key)) is int and usage[key] >= 0 else 0
            for key in ("input_tokens", "output_tokens")}


def handle_responses(handler):
    """Relay complete native Responses JSON/SSE with shared lifecycle control."""
    runtime = handler.runtime
    try:
        if handler.headers.get("Transfer-Encoding"):
            raise BridgeError("Use a Content-Length request body.")
        size = int(handler.headers.get("Content-Length", "0"))
        if not 0 < size <= MAX_BODY:
            handler.error(413, "Responses request is too large or empty.")
            return
        raw = handler.rfile.read(size)
        if len(raw) != size:
            raise BridgeError("The request body was interrupted.")
        payload = json.loads(raw)
        plan = prepare_native(runtime, payload)
        atomic_json(runtime.root / "last-responses-shape.json", _request_shape(payload))
    except (ValueError, TypeError, KeyError, BridgeError, ProviderError) as exc:
        handler.error(400, str(exc))
        return
    delegated = plan["protocol"] == "messages_bridge"
    if delegated:
        plan["url"] = f"http://127.0.0.1:{handler.server.server_port}/v1/messages"
    # Queue for a worker slot instead of failing fast: Codex subagent bursts
    # briefly exceed the worker count by design, and every instant 429 burns
    # one of the desktop client's retries toward its "exceeded retry limit"
    # terminal state. Mirrors the Messages path that shields Mistral.
    if not delegated and not wait_for_slot(runtime.semaphore, cancel=lambda: _client_gone(handler),
                                           timeout=SLOT_WAIT_TIMEOUT):
        if _client_gone(handler):
            runtime.record("cancelled", plan["route"])
            return
        handler.error(429, "Eight requests are already active. Try again shortly.",
                      headers={"Retry-After": str(SLOT_RETRY_AFTER)})
        return
    closed = threading.Event()
    disconnected = threading.Event()
    write_lock = threading.Lock()
    connection = response = upstream_socket = None
    streaming = False
    terminal = None
    final = None
    custom_items = set()
    spawn_items = set()
    service_tier = None
    usage = {}

    def redact(value):
        text = str(value)
        for secret in (plan["private_key"], runtime.token):
            if secret:
                text = text.replace(secret, "[redacted]")
        return text[:700]

    def write_chunk(data):
        with write_lock:
            handler.wfile.write(f"{len(data):x}\r\n".encode() + data + b"\r\n")
            handler.wfile.flush()

    def emit(event):
        write_chunk(("event: " + event["type"] + "\ndata: " + json.dumps(event, ensure_ascii=False) + "\n\n").encode())

    def clean_error(value):
        if isinstance(value, str):
            return redact(value)
        if isinstance(value, dict):
            return {key: clean_error(item) for key, item in value.items()}
        if isinstance(value, list):
            return [clean_error(item) for item in value]
        return value

    def finish_response(value):
        if (not isinstance(value, dict) or not isinstance(value.get("id"), str)
                or not isinstance(value.get("output"), list)
                or value.get("status") not in {"completed", "incomplete", "failed"}):
            raise BridgeError("The provider returned an invalid terminal Responses object.")
        value["model"] = plan["requested"]
        for item in value["output"]:
            if not restore_custom_call(item, plan["tool_map"]):
                output_names(item, plan["tool_map"])
                apply_subagent_model(item, plan.get("subagent_route"))
        if value.get("error"):
            value["error"] = clean_error(value["error"])
        if plan["provider_id"] == "grok" and plan["body"]["store"] and value["status"] != "failed":
            # Commit the ID before exposing it to a client that can immediately
            # send a continuation on another connection.
            runtime.response_ownership.remember(value["id"], plan["scope"])

    def cancel_monitor():
        while not closed.wait(.25):
            if runtime.stopping.is_set():
                disconnected.set()
            else:
                try:
                    ready, _, _ = select.select([handler.connection], [], [], 0)
                    if ready and handler.connection.recv(1, socket.MSG_PEEK | socket.MSG_DONTWAIT) == b"":
                        disconnected.set()
                except (OSError, ValueError):
                    disconnected.set()
            if disconnected.is_set():
                try:
                    # Retry backoff leaves no live connection between attempts.
                    live = connection if connection is not None else None
                    sock = (live.sock if live is not None else None) or upstream_socket
                    if sock:
                        sock.shutdown(socket.SHUT_RDWR)
                    if live is not None:
                        live.close()
                except OSError:
                    pass
                return

    def ping():
        while not closed.wait(5):
            try:
                write_chunk(b": provider-hub keepalive\n\n")
            except OSError:
                disconnected.set()
                return

    try:
        if not delegated:
            with runtime.lock:
                runtime.active += 1
        threading.Thread(target=cancel_monitor, daemon=True).start()
        encoded = json.dumps(plan["body"]).encode()
        headers = {**plan["headers"], "Accept": "text/event-stream" if plan["body"]["stream"] else "application/json"}
        attempts = 0
        while True:
            # Serialize on the shared provider gate: a sibling's 429 parks
            # this route, so bursts pause here instead of firing requests a
            # depleted quota is certain to reject. Delegated requests rely on
            # the inner Messages handler's own gate and retry budget.
            if not delegated and not runtime.throttle.wait(plan["provider_id"],
                                                           cancel=lambda: _client_gone(handler)):
                runtime.record("cancelled", plan["route"])
                return
            connection, endpoint = runtime.upstream(plan["url"])
            with runtime.lock:
                runtime.connections.add(connection)
            connection.request("POST", endpoint, encoded, headers)
            response = connection.getresponse()
            upstream_socket = connection.sock or getattr(getattr(response.fp, "raw", None), "_sock", None)
            if response.status == 200:
                if not delegated:
                    runtime.throttle.note_success(plan["provider_id"])
                break
            data = response.read(65536)
            try:
                error = json.loads(data).get("error", {})
                detail = error.get("message", "") if isinstance(error, dict) else ""
            except (ValueError, AttributeError):
                detail = ""
            detail = redact(detail)
            retry_after = parse_retry_after(response.getheader("Retry-After")) if response.status in RETRYABLE_STATUSES else None
            if (not delegated and response.status in RETRYABLE_STATUSES
                    and attempts + 1 < MAX_UPSTREAM_ATTEMPTS
                    and (retry_after is None or retry_after <= THROTTLE_CAP)):
                # Transient provider pressure: absorb it in-bridge with
                # backoff instead of burning one of the client's retries.
                # Absorbed attempts are logged, never counted as failures.
                attempts += 1
                runtime.throttle.note_limit(plan["provider_id"], retry_after, attempts - 1)
                runtime.record("throttled", plan["route"], response.status)
                response.close()
                connection.close()
                with runtime.lock:
                    runtime.connections.discard(connection)
                connection = None
                response = None
                upstream_socket = None
                continue
            status = response.status if response.status in {400, 401, 402, 403, 404, 413, 429, 500, 502, 503, 504} else 502
            if not delegated:
                runtime.record("error", plan["route"], status)
            terminal_headers = None
            if response.status in RETRYABLE_STATUSES:
                hint = retry_after if retry_after is not None else SLOT_RETRY_AFTER
                terminal_headers = {"Retry-After": str(max(1, int(hint)))}
            if delegated and response.getheader("X-Provider-Hub-Origin") == "gateway":
                # The inner Messages handler rejected the request itself
                # (context window, capability mismatch); it never reached
                # the provider, so do not present it as a provider reply.
                message = detail or "Provider Hub rejected this request."
            elif detail.startswith(plan["provider_name"] + " returned HTTP"):
                message = detail
            else:
                message = plan["provider_name"] + f" returned HTTP {response.status}. " + detail
            if status == 400:
                # Same reasoning as the Messages relay: a 400 that reads as
                # the provider refusing the effort parameter costs the client
                # its effort control for the whole session, so the trigger
                # words are renamed. Which endpoint a request arrived on is
                # no reason for it to be protected or not.
                message = mask_effort_rejection(message)
            handler.error(status, message, headers=terminal_headers)
            return
        if not plan["body"]["stream"]:
            raw = response.read(MAX_BODY + 1)
            if len(raw) > MAX_BODY:
                raise BridgeError("The Responses result exceeded the response limit.")
            final = json.loads(raw)
            if delegated:
                final = plan["adapter"].from_message(final)
            finish_response(final)
            terminal = "response.failed" if final.get("status") == "failed" else "response.completed"
            handler.json_response(200, final)
        else:
            handler.send_response(200)
            handler.send_header("Content-Type", "text/event-stream")
            handler.send_header("Cache-Control", "no-store")
            handler.send_header("Transfer-Encoding", "chunked")
            handler.send_header("Connection", "close")
            handler.end_headers()
            streaming = True
            threading.Thread(target=ping, daemon=True).start()
            lines = []
            count = 0
            while True:
                line = response.readline(MAX_BODY + 1)
                if not line:
                    break
                count += len(line)
                if count > MAX_BODY:
                    raise BridgeError("The provider sent an oversized Responses event.")
                if line in (b"\n", b"\r\n"):
                    data = b"\n".join(lines)
                    lines.clear()
                    count = 0
                    if not data or data == b"[DONE]":
                        continue
                    decoded = json.loads(data)
                    events = plan["adapter"].feed(decoded) if delegated else [decoded]
                    for event in events:
                        kind = event.get("type") if isinstance(event, dict) else None
                        if not isinstance(kind, str) or not (kind.startswith("response.") or kind == "error"):
                            raise BridgeError("The provider returned an unsupported Responses event.")
                        if isinstance(event.get("response"), dict):
                            event["response"]["model"] = plan["requested"]
                            if event["response"].get("error"):
                                event["response"]["error"] = clean_error(event["response"]["error"])
                        if not rewrite_stream_event(event, plan["tool_map"], custom_items,
                                                    plan.get("subagent_route"), spawn_items):
                            continue
                        if kind == "error":
                            event = {"type": "error", "code": "provider_error", "message": redact(event.get("message", "Provider stream error.")), "param": None}
                        if kind in TERMINAL_EVENTS and kind != "error":
                            finish_response(event.get("response"))
                        emit(event)
                        if kind in TERMINAL_EVENTS:
                            terminal = kind
                            final = event.get("response")
                            break
                    if terminal is not None:
                        break
                elif line.startswith(b"data:"):
                    lines.append(line[5:].strip())
            if disconnected.is_set():
                raise BrokenPipeError()
            if terminal is None:
                raise BridgeError("The Responses stream ended without a terminal event. Retry the turn.")
            closed.set()
            with write_lock:
                handler.wfile.write(b"0\r\n\r\n")
                handler.wfile.flush()
            handler.close_connection = True
        if isinstance(final, dict):
            usage = usage_metadata(final.get("usage"))
            tier = final.get("service_tier")
            if tier in ("default", "priority"):
                service_tier = tier
        if not delegated:
            runtime.record("error" if terminal in {"error", "response.failed"} else "completed",
                           plan["route"], 502 if terminal in {"error", "response.failed"} else 200,
                           usage, service_tier=service_tier)
    except Exception as exc:
        if disconnected.is_set() or isinstance(exc, (BrokenPipeError, ConnectionResetError)):
            if not delegated:
                runtime.record("cancelled", plan["route"])
        else:
            if not delegated:
                runtime.record("error", plan["route"], 502)
            message = redact(exc) if isinstance(exc, BridgeError) else "The provider connection could not complete this Responses request."
            if streaming:
                try:
                    closed.set()
                    emit({"type": "error", "code": "provider_error", "message": message, "param": None})
                    with write_lock:
                        handler.wfile.write(b"0\r\n\r\n")
                        handler.wfile.flush()
                except OSError:
                    pass
            else:
                handler.error(502, message)
    finally:
        closed.set()
        if response is not None:
            response.close()
        if connection is not None:
            connection.close()
        with runtime.lock:
            runtime.connections.discard(connection)
            if not delegated:
                runtime.active -= 1
        if not delegated:
            runtime.semaphore.release()
        handler.close_connection = True
        runtime.release_local_model(plan)
