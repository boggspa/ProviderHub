"""Native Responses routes for Codex. No Messages conversion or prompt logging."""
from __future__ import annotations

import copy
import hashlib
import hmac
import json
import select
import socket
import threading
import time

from bridge_core import BridgeError, atomic_json, read_json
from hub_config import connection_signature, qualify, split_route
from providers import PROVIDERS, ProviderError, _auth_headers, _chat_effort, validate_connection
from responses_tools import flatten_tools, input_names, output_names, register
from responses_bridge import ENVELOPE_PREFIX, MessagesResponsesAdapter, ReasoningEnvelope, to_messages
from openrouter_provider import OpenRouterError, finalize as openrouter_finalize, app_headers as openrouter_app_headers
from effort_map import cap_high_end, map_effort, ollama_effort_aliases


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


def _strip_task_header(text):
    """Strip Codex NEW_TASK routing header lines (may return ""; caller falls back)."""
    kept = [line for line in text.splitlines() if not line.strip().startswith(_TASK_HEADER_PREFIXES)]
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
    - multi_agent_call_output / subagent_call_output -> message (synthetic subagent result description)
    - agent_message -> message (with agent prefix)
    
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
        # agent_message -> message with agent prefix
        elif item_type == "agent_message":
            role = item.get("role") or "assistant"
            if role not in {"user", "assistant", "system", "developer"}:
                role = "assistant"
            agent_name = item.get("agent") or item.get("sender") or "subagent"
            content = item.get("content") or ""
            # Guard: only add prefix if content is a string and doesn't already have it
            if isinstance(content, str):
                # Break the echo chamber: a previous turn's leaked envelope
                # re-enters here as `subagent: [...]` prose once the model
                # repeats it. Unwrap it instead of amplifying it.
                content = _clean_wire_text(content) if content else ""
                prefix = f"[subagent_{agent_name}]"
                if not content.startswith(prefix):
                    content = f"{prefix}: {content}" if content else prefix
            elif isinstance(content, (dict, list)):
                extracted = _extract_wire_value(content)
                if extracted:
                    content = f"[subagent_{agent_name}]: {extracted}"
                else:
                    # Stringify non-string content
                    content = f"{agent_name}: {json.dumps(content)}"
            else:
                content = f"{agent_name}: {str(content)}"
            normalized.append({
                "type": "message",
                "role": role,
                "content": content,
            })
        else:
            normalized.append(item)
    
    return normalized


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
    body["tools"], tool_map = flatten_tools(tools)
    if tools and spec.get("tools") is False:
        raise BridgeError("The selected model does not support tool calls.")
    if isinstance(body["input"], list):
        body["input"] = _normalize_multi_agent_items(body["input"])
        for item in body["input"]:
            if not isinstance(item, dict) or item.get("type", "message") not in {"message", "function_call", "function_call_output", "reasoning"}:
                offending_type = item.get("type", "unknown") if isinstance(item, dict) else type(item).__name__
                raise BridgeError(f"Unsupported Responses history item '{offending_type}'. Use the function-tool catalogue.")
            content = item.get("content")
            # Ollama's Responses endpoint accepts or rejects images itself.
            # Catalogue vision is picker metadata, not a request interceptor.
            if isinstance(content, list) and provider_id != "ollama" and spec.get("vision") is False:
                if any(isinstance(part, dict) and part.get("type") == "input_image" for part in content):
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
        envelope = ReasoningEnvelope(runtime.root)
        translated = to_messages(body, route, spec, envelope, scope)
        return {"body": translated, "headers": {"Content-Type": "application/json", "Authorization": "Bearer " + runtime.token},
                "url": None, "route": route, "requested": requested, "provider_id": provider_id,
                "scope": scope, "tool_map": tool_map, "private_key": key,
                "provider_name": PROVIDERS[provider_id]["name"], "protocol": "messages_bridge",
                "adapter": MessagesResponsesAdapter(requested, envelope, scope)}
    if isinstance(body["input"], list) and any(isinstance(item, dict) and str(item.get("encrypted_content", "")).startswith(ENVELOPE_PREFIX) for item in body["input"]):
        raise BridgeError("This reasoning history belongs to a different provider connection. Start a new task when changing providers.")
    if provider_id == "openrouter":
        try:
            openrouter_finalize(body, spec, key, responses=True)
        except OpenRouterError as exc:
            raise BridgeError(str(exc)) from exc
        url = connection["base_url"] + "/v1/responses"
    elif provider_id == "ollama":
        if body.get("previous_response_id") or body.get("store"):
            raise BridgeError("Ollama Responses is stateless. Send the full input history with store:false.")
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
                raise BridgeError(f"Ollama model does not support reasoning effort {requested_effort!r}.")
            if mapped is not None:
                reasoning["effort"] = mapped
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
            "private_key": key, "provider_name": PROVIDERS[provider_id]["name"]}


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
        atomic_json(runtime.root / "last-responses-shape.json", {
            "fields": sorted(payload),
            "input_types": sorted({str(item.get("type", "message"))[:50] for item in payload.get("input", []) if isinstance(item, dict)}),
            "tool_types": sorted({tool.get("type", "") for tool in payload.get("tools", [])}),
        })
    except (ValueError, TypeError, KeyError, BridgeError, ProviderError) as exc:
        handler.error(400, str(exc))
        return
    delegated = plan["protocol"] == "messages_bridge"
    if delegated:
        plan["url"] = f"http://127.0.0.1:{handler.server.server_port}/v1/messages"
    if not delegated and not runtime.semaphore.acquire(blocking=False):
        handler.error(429, "Eight requests are already active. Try again shortly.")
        return
    closed = threading.Event()
    disconnected = threading.Event()
    write_lock = threading.Lock()
    connection = response = upstream_socket = None
    streaming = False
    terminal = None
    final = None
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
            output_names(item, plan["tool_map"])
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
                    sock = connection.sock or upstream_socket
                    if sock:
                        sock.shutdown(socket.SHUT_RDWR)
                    connection.close()
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
        connection, endpoint = runtime.upstream(plan["url"])
        with runtime.lock:
            runtime.connections.add(connection)
        threading.Thread(target=cancel_monitor, daemon=True).start()
        connection.request("POST", endpoint, json.dumps(plan["body"]).encode(), {
            **plan["headers"], "Accept": "text/event-stream" if plan["body"]["stream"] else "application/json",
        })
        response = connection.getresponse()
        upstream_socket = connection.sock or getattr(getattr(response.fp, "raw", None), "_sock", None)
        if response.status != 200:
            data = response.read(65536)
            try:
                error = json.loads(data).get("error", {})
                detail = error.get("message", "") if isinstance(error, dict) else ""
            except (ValueError, AttributeError):
                detail = ""
            status = response.status if response.status in {400, 401, 402, 403, 404, 413, 429, 500, 502, 503, 504} else 502
            if not delegated:
                runtime.record("error", plan["route"], status)
            handler.error(status, plan["provider_name"] + f" returned HTTP {response.status}. " + redact(detail))
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
                        if isinstance(event.get("item"), dict):
                            output_names(event["item"], plan["tool_map"])
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
