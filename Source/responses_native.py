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
from openrouter_provider import OpenRouterError, finalize as openrouter_finalize
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
        for item in body["input"]:
            if not isinstance(item, dict) or item.get("type", "message") not in {"message", "function_call", "function_call_output", "reasoning"}:
                raise BridgeError("Unsupported Responses history item. Use the function-tool catalogue.")
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
