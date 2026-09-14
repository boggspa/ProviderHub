#!/usr/bin/env python3
"""Provider Hub's private worker for Python 3.11 or newer."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import hmac
import http.client
import ipaddress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import select
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request

from bridge_core import (BridgeError, ClaudeProfile, atomic_json, attach_model_specs, bootstrap_metadata, cached_catalogue, credentials, discover_provider, gateway_token,
                         inspect_state, load_settings, model_labels, private_directory, private_token, read_json, ssl_context, state_root, validate_settings)
from catalogue_lifecycle import (CataloguePreparationError, catalogue_fingerprint,
                                 prepare_launch, refresh_all, require_prepared,
                                 runtime_fingerprint_error,
                                 validate_prepared_launch)
from catalogue import build_catalogue, read_observations
from hub_config import connection_signature, provider_presentations, qualify, split_route
from providers import PROVIDERS, prepare_request, ProviderError
from devin_agent import (DESCRIPTOR as DEVIN_DESCRIPTOR, DevinAgentError,
                         create_session as devin_create_session,
                         list_sessions as devin_list_sessions,
                         get_session as devin_get_session,
                         archive_session as devin_archive_session,
                         cancel_session as devin_cancel_session,
                         submit_task as devin_submit_task,
                         catalogue as devin_catalogue)
from cerebras_replay import CerebrasReplayError, CerebrasStreamAdapter, sanitize_compacted_messages, sign_thinking, validate_messages
from gemini_provider import GeminiError, GeminiStreamAdapter, translate_response as translate_gemini_response, _estimated_input_tokens as estimated_gemini_tokens
from protocol import (StreamTranslator, apply_mapping_options, compact_conversation, compact_threshold, estimated_tokens, mapping_options_for, validate_mistral_roles,
                      model_catalog, resolve_model, rewrite_context_reminders, translate_request, translate_response)
from responses_native import ResponseOwnership, handle_responses, NATIVE_PROVIDERS
from codex_catalogue import catalogue_digest, choices as codex_choices, launch_settings as codex_launch_settings
from codex_profile import CodexProfile
from codex_runtime import qualify_runtime, runtime_signature

MAX_BODY = 32 * 1024 * 1024


class Runtime:
    def __init__(self, root: Path, upstream_url=None, key=None, request_planner=None):
        self.root = root
        self.settings, self.catalogue = attach_model_specs(load_settings(root), root)
        # Runtime deliberately snapshots model planning metadata at startup.
        # Activation checks this fingerprint after launch preparation so a
        # newly refreshed selected route cannot be served by an older snapshot.
        self.catalogue_fingerprint = catalogue_fingerprint(
            self.settings, root, model_specs=self.settings["_model_specs"])
        self.token = gateway_token(root)
        self.replay_key = private_token(root, "reasoning-signing-key")
        self.response_ownership = ResponseOwnership(root)
        self.codex_catalogue_digest = catalogue_digest(self.settings, self.catalogue)
        self.key = key
        self.source = "Provider-specific credentials"
        self.upstream_url = upstream_url
        self.request_planner = request_planner
        self.credential_cache = {}
        self.lock = threading.Lock()
        self.semaphore = threading.BoundedSemaphore(8)
        self.active = 0
        self.completed = 0
        self.failed = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.last_error = ""
        self.last_model = ""
        self.started = time.time()
        self.stopping = threading.Event()
        self.connections = set()
        self.provider_counts = {provider_id: {"completed": 0, "failed": 0, "input_tokens": 0, "output_tokens": 0} for provider_id in PROVIDERS}

    def resolve_route(self, requested):
        if not isinstance(requested, str):
            raise BridgeError("Choose a model route.")
        try:
            route = resolve_model(requested, self.settings["mappings"])
        except BridgeError:
            route = qualify(*split_route(requested.removesuffix("[1m]")))
        if route not in self.settings["_model_specs"]:
            raise BridgeError("This route is not in the current provider catalogue. Refresh its models first.")
        return route

    def provider_key(self, provider_id):
        if self.key is not None:
            return self.key
        with self.lock:
            cached = self.credential_cache.get(provider_id)
        if cached is None:
            cached = credentials(self.settings, provider_id)
            with self.lock:
                self.credential_cache[provider_id] = cached
        return cached[0]

    def plan(self, payload):
        route = self.resolve_route(payload.get("model"))
        provider_id, upstream_model = split_route(route)
        spec = self.settings["_model_specs"][route]
        context = spec.get("context")
        options = mapping_options_for(payload.get("model"), self.settings)
        payload = apply_mapping_options(payload, self.settings)
        estimate_fn = estimated_gemini_tokens if provider_id == "gemini" else estimated_tokens
        estimate = estimate_fn(payload)
        # Compact before enforcing the hard window: Claude Desktop meters the
        # session at 200K/1M regardless of the selected route, so a small-window
        # route would otherwise fail instead of trimming old turns first. The
        # threshold also reserves room for the requested output so input
        # pressure cannot silently shrink the response below what the client
        # asked for (effective request after the model's own output cap).
        wanted_output = payload.get("max_tokens")
        if type(wanted_output) is not int or wanted_output <= 0:
            wanted_output = 4096
        model_output_cap = spec.get("max_output")
        if type(model_output_cap) is int and model_output_cap > 0:
            wanted_output = min(wanted_output, model_output_cap)
        auto_compact = None
        threshold = compact_threshold(context, options, reserve_output=wanted_output)
        if threshold is not None and estimate >= threshold:
            compacted_payload = compact_conversation(payload, threshold, estimate=estimate_fn)
            after = estimate_fn(compacted_payload)
            if after < estimate:
                auto_compact = {"threshold": threshold, "requested_output": wanted_output,
                                "estimated_before": estimate, "estimated_after": after}
                payload = compacted_payload
                estimate = after
                self.record("compacted", route, 200, {"estimated_before": auto_compact["estimated_before"],
                                                      "estimated_after": after})
        if type(context) is int and estimate >= context:
            raise BridgeError(f"This conversation is above the provider's reported {context:,}-token context limit.")
        if payload.get("model", "").endswith("[1m]") and (type(context) is not int or context < 1000000):
            raise BridgeError("A 1M context window has not been established for this route.")
        # Validate Mistral-specific role requirements
        if provider_id == "mistral":
            validate_mistral_roles(payload)
        rewritten = rewrite_context_reminders(payload, context, estimate)
        reminders_rewritten = rewritten is not payload
        payload = rewritten
        key = self.provider_key(provider_id)
        scope = connection_signature(provider_id, self.settings["providers"][provider_id])
        # Bind replay to the actual credential as well as the configured revision.
        # This covers Vibe/environment credential rotation between gateway runs.
        scope += ":" + hmac.new(self.replay_key.encode(), key.encode(), hashlib.sha256).hexdigest()
        replay_required = provider_id == "cerebras" and spec.get("reasoning_history") in {"adapter_required", "gateway_signed_replay", "gateway_signed_replay_required"}
        cerebras_repair = None
        try:
            if provider_id == "cerebras":
                try:
                    replay = validate_messages(payload.get("messages"), upstream_model, scope, self.replay_key,
                                               require_tool_reasoning=replay_required)
                except CerebrasReplayError:
                    # Desktop compaction rewrites old assistant output, which
                    # invalidates gateway-bound signatures. Repair the history
                    # (strip broken traces, drop unverified tool cycles) so the
                    # follow-up turn runs instead of failing with HTTP 400.
                    sanitized, replay, cerebras_repair = sanitize_compacted_messages(
                        payload.get("messages"), upstream_model, scope, self.replay_key,
                        require_tool_reasoning=replay_required)
                    payload = {**payload, "messages": sanitized}
            else:
                replay = None
            request_options = {"reasoning_by_message": replay} if provider_id == "cerebras" else {}
            if provider_id == "gemini":
                request_options.update(replay_scope=scope, replay_key=self.replay_key)
            plan = (self.request_planner or prepare_request)(provider_id, self.settings["providers"][provider_id], key, payload, upstream_model, spec, **request_options)
        except (ProviderError, CerebrasReplayError) as exc:
            raise BridgeError(str(exc).replace(key, "[redacted]") if key else str(exc)) from exc
        plan.update(route=route, provider_id=provider_id, provider_name=PROVIDERS[provider_id]["name"],
                    private_key=key, upstream_model=upstream_model, replay_scope=scope, replay_required=replay_required,
                    model_spec=spec)
        if reminders_rewritten:
            plan.setdefault("compatibility", {})["context_reminders"] = "catalogue_remaining"
        dropped = [name for name, on in (("system", options["omit_system"]), ("tools", options["omit_tools"])) if on]
        if dropped:
            plan.setdefault("compatibility", {})["omitted_mapping_fields"] = dropped
        if auto_compact is not None:
            plan.setdefault("compatibility", {})["auto_compact"] = auto_compact
        if cerebras_repair is not None and cerebras_repair.get("repaired"):
            plan.setdefault("compatibility", {})["cerebras_history_repair"] = cerebras_repair
        if type(context) is int and provider_id != "gemini":
            allowed_output = min(plan["body"].get("max_tokens", 4096), max(1, context - estimate))
            plan["body"]["max_tokens"] = allowed_output
            if allowed_output < wanted_output:
                # Compaction could not free enough room (system/tools bound):
                # record that the response was shrunk below the request so a
                # later max_tokens truncation is not misread as a harness cap.
                plan.setdefault("compatibility", {})["output_headroom_clamped"] = {
                    "requested": wanted_output, "allowed": allowed_output}
        # The only override is an explicit in-process test-harness argument,
        # never a client request field or persisted provider setting.
        if self.upstream_url is not None:
            endpoint = "/v1/messages" if plan["protocol"] == "anthropic" else "/chat/completions"
            plan["url"] = self.upstream_url.rstrip("/") + endpoint
        return plan

    def status(self):
        with self.lock:
            return {"running": True, "active": self.active, "completed": self.completed, "failed": self.failed,
                    "input_tokens": self.input_tokens, "output_tokens": self.output_tokens,
                    "last_error": self.last_error, "last_model": self.last_model,
                    "credential_source": self.source, "uptime_seconds": int(time.time() - self.started), "pid": os.getpid(),
                    "catalogue_fingerprint": self.catalogue_fingerprint,
                    "codex_catalogue_digest": self.codex_catalogue_digest,
                    "providers": {key: dict(value) for key, value in self.provider_counts.items()}}

    def record(self, kind, model="", code=None, usage=None, model_unavailable=False, service_tier=None):
        # Deliberately excludes prompts, tool arguments, response text, headers,
        # credentials, and raw upstream error bodies.
        event = {"time": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "event": kind}
        if model:
            event["model"] = model
            event["provider_id"] = split_route(model)[0]
        if code is not None:
            event["status"] = code
        if usage:
            event["usage"] = usage
        if service_tier in {"default", "priority"}:
            event["service_tier"] = service_tier
        if model_unavailable:
            event["model_unavailable"] = True
        with self.lock:
            if kind == "completed":
                self.completed += 1
                self.input_tokens += (usage or {}).get("input_tokens", 0)
                self.output_tokens += (usage or {}).get("output_tokens", 0)
                self.last_error = ""
            elif kind == "error":
                self.failed += 1
                self.last_error = f"Request failed (HTTP {code}). See the message in the desktop client."
            self.last_model = model or self.last_model
            if model:
                counters = self.provider_counts[split_route(model)[0]]
                if kind == "completed":
                    counters["completed"] += 1
                    counters["input_tokens"] += (usage or {}).get("input_tokens", 0)
                    counters["output_tokens"] += (usage or {}).get("output_tokens", 0)
                elif kind == "error":
                    counters["failed"] += 1
            path = self.root / "activity.jsonl"
            if path.exists() and path.stat().st_size > 512000:
                path.replace(self.root / "activity.previous.jsonl")
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, "a") as stream:
                stream.write(json.dumps(event) + "\n")

    def upstream(self, url):
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme == "https":
            connection = http.client.HTTPSConnection(parsed.hostname, parsed.port or 443, timeout=180, context=ssl_context())
        elif parsed.scheme == "http" and ipaddress.ip_address(parsed.hostname).is_loopback:
            connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=10 if self.upstream_url else 180)
        else:
            raise BridgeError("Hosted provider connections require HTTPS.")
        return connection, parsed.path + ("?" + parsed.query if parsed.query else "")


def error_type(status):
    return {400: "invalid_request_error", 401: "authentication_error", 403: "permission_error",
            404: "not_found_error", 413: "request_too_large", 429: "rate_limit_error", 503: "overloaded_error"}.get(status, "api_error")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "ProviderHub/0.5.3"

    @property
    def runtime(self):
        return self.server.runtime

    def setup(self):
        super().setup()
        self.connection.settimeout(30)

    def log_message(self, *_):
        pass

    def json_response(self, status, body, headers=None):
        encoded = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(encoded)
        self.close_connection = True

    def error(self, status, message):
        self.json_response(status, {"type": "error", "error": {"type": error_type(status), "message": message}})

    # ------------------------------------------------------------------
    # Devin agent-session endpoints. Devin is a session-based agent
    # (protocol "agent_session"), not a chat-completions provider, so these
    # routes sit beside the LLM routes instead of flowing through plan().
    # ------------------------------------------------------------------
    def _devin_key(self):
        # A test-harness key override wins, exactly like Runtime.provider_key().
        if self.runtime.key is not None:
            return self.runtime.key
        key, _source = credentials(self.runtime.settings, "devin")
        return key

    def _devin_org_id(self):
        connection = self.runtime.settings.get("providers", {}).get("devin", {})
        org_id = connection.get("org_id")
        if not org_id:
            raise DevinAgentError("Devin requires an organization ID. Set one in the provider connection.")
        return org_id

    def _devin_transport(self):
        # Reuse the gateway's own HTTPS outbound path so Devin session calls
        # share the same TLS context and local-only policy as model requests.
        # A test-harness upstream_url override rewrites the target exactly as
        # plan() does, without touching the real api.devin.ai default.
        override = self.runtime.upstream_url
        def transport(plan):
            method = plan.get("method", "GET")
            body = plan.get("body")
            encoded = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
            url = plan["url"]
            if override is not None:
                base = override.rstrip("/")
                parsed_plan = urllib.parse.urlsplit(url)
                url = base + parsed_plan.path + ("?" + parsed_plan.query if parsed_plan.query else "")
            parsed = urllib.parse.urlsplit(url)
            if parsed.scheme == "https":
                connection = http.client.HTTPSConnection(parsed.hostname, parsed.port or 443, timeout=180, context=ssl_context())
            elif parsed.scheme == "http" and ipaddress.ip_address(parsed.hostname).is_loopback:
                connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=10)
            else:
                raise DevinAgentError("Devin requests require HTTPS on the official endpoint.")
            endpoint = parsed.path + ("?" + parsed.query if parsed.query else "")
            try:
                connection.request(method, endpoint, encoded, plan.get("headers", {}))
                response = connection.getresponse()
                raw = response.read(MAX_BODY + 1)
                if len(raw) > MAX_BODY:
                    raise DevinAgentError("Devin response exceeded the response limit.")
                if response.status >= 400:
                    raise DevinAgentError(f"Devin returned HTTP {response.status}: {raw[:200].decode('utf-8', 'replace')}")
                if not raw:
                    return {}
                return json.loads(raw)
            finally:
                connection.close()
        return transport

    def _read_json_body(self):
        if self.headers.get("Transfer-Encoding"):
            self.error(400, "Use a Content-Length request body.")
            return None
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= MAX_BODY:
                self.error(413, "Request body is empty or above 32 MB.")
                return None
            body = self.rfile.read(length)
            if len(body) != length:
                self.error(400, "The request body was interrupted.")
                return None
            payload = json.loads(body)
            if not isinstance(payload, dict):
                self.error(400, "The request must be a JSON object.")
                return None
            return payload
        except (ValueError, TypeError) as exc:
            self.error(400, str(exc))
            return None

    def _handle_list_agent_sessions(self):
        try:
            key = self._devin_key()
            org_id = self._devin_org_id()
            sessions, cursor = devin_list_sessions(key, org_id, transport=self._devin_transport())
        except DevinAgentError as exc:
            self.error(400, str(exc))
            return
        self.json_response(200, {"data": [session.to_dict() for session in sessions], "cursor": cursor})

    def _handle_get_agent_session(self, path):
        session_id = path[len("/v1/agents/sessions/"):].strip("/")
        if not session_id or "/" in session_id:
            self.error(404, "Unknown agent endpoint.")
            return
        try:
            key = self._devin_key()
            org_id = self._devin_org_id()
            session = devin_get_session(key, org_id, session_id, transport=self._devin_transport())
        except DevinAgentError as exc:
            self.error(400, str(exc))
            return
        self.json_response(200, session.to_dict())

    def _handle_create_agent_session(self):
        payload = self._read_json_body()
        if payload is None:
            return
        try:
            key = self._devin_key()
            org_id = self._devin_org_id()
            task = payload.get("task")
            mode = payload.get("mode", "normal")
            tags = payload.get("tags")
            metadata = payload.get("metadata")
            session = devin_create_session(key, org_id, task, mode=mode, tags=tags, metadata=metadata,
                                           transport=self._devin_transport())
        except (DevinAgentError, TypeError) as exc:
            self.error(400, str(exc))
            return
        self.json_response(200, session.to_dict())

    def _handle_agent_session_action(self, path):
        remainder = path[len("/v1/agents/sessions/"):].strip("/")
        parts = remainder.split("/") if remainder else []
        if not parts:
            self.error(404, "Unknown agent endpoint.")
            return
        session_id = parts[0]
        action = parts[1] if len(parts) > 1 else ""
        payload = {}
        content_length = self.headers.get("Content-Length")
        if self.command == "POST" and content_length and int(content_length) > 0:
            payload = self._read_json_body() or {}
        try:
            key = self._devin_key()
            org_id = self._devin_org_id()
            if action == "cancel" or action == "archive":
                session = (devin_cancel_session(key, org_id, session_id, transport=self._devin_transport())
                           if action == "cancel"
                           else devin_archive_session(key, org_id, session_id, transport=self._devin_transport()))
            elif action == "messages" or action == "tasks":
                message = (payload or {}).get("content") or (payload or {}).get("message")
                result = devin_submit_task(key, org_id, session_id, message, transport=self._devin_transport())
                self.json_response(200, result if isinstance(result, dict) else {"data": result})
                return
            else:
                self.error(404, "Unknown agent endpoint.")
                return
        except DevinAgentError as exc:
            self.error(400, str(exc))
            return
        self.json_response(200, session.to_dict())

    def allowed(self, *, health=False):
        host = self.headers.get("Host", "").split(":")[0]
        if host not in {"127.0.0.1", "localhost"} or self.headers.get("Origin"):
            self.error(403, "This gateway accepts local desktop requests only.")
            return False
        if health:
            return True
        auth = self.headers.get("Authorization", "")
        token = auth[7:] if auth.startswith("Bearer ") else self.headers.get("x-api-key", "")
        if not hmac.compare_digest(token, self.runtime.token):
            self.error(401, "The local gateway credential is missing or incorrect.")
            return False
        return True

    def do_GET(self):
        path = urllib.parse.urlsplit(self.path).path
        if not self.allowed(health=path == "/_bridge/health"):
            return
        if path == "/_bridge/health":
            self.json_response(200, {"service": "mistral-bridge", "product": "Provider Hub", "version": "0.5.3"})
        elif path == "/_bridge/status":
            self.json_response(200, self.runtime.status())
        elif path == "/v1/models":
            self.json_response(200, model_catalog(self.runtime.settings))
        elif path == "/_bridge/codex/models":
            self.json_response(200, {"models": codex_choices(self.runtime.settings, self.runtime.catalogue)})
        elif path == "/v1/agents/sessions":
            return self._handle_list_agent_sessions()
        elif path.startswith("/v1/agents/sessions/"):
            return self._handle_get_agent_session(path)
        elif path == "/v1/agents/catalogue":
            modes, warnings, docs = devin_catalogue()
            self.json_response(200, {"provider_id": "devin", "modes": modes, "warnings": warnings, "documentation": docs})
        else:
            self.error(404, "Unknown gateway endpoint.")

    def do_POST(self):
        if not self.allowed():
            return
        path = urllib.parse.urlsplit(self.path).path
        if path == "/v1/responses":
            return handle_responses(self)
        if path == "/v1/agents/sessions":
            return self._handle_create_agent_session()
        if path.startswith("/v1/agents/sessions/"):
            return self._handle_agent_session_action(path)
        if path not in {"/v1/messages", "/v1/messages/count_tokens"}:
            self.error(404, "Unknown gateway endpoint.")
            return
        if self.headers.get("Transfer-Encoding"):
            self.error(400, "Use a Content-Length request body.")
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= MAX_BODY:
                self.error(413, "Request body is empty or above 32 MB.")
                return
            body = self.rfile.read(length)
            if len(body) != length:
                raise BridgeError("The request body was interrupted.")
            payload = json.loads(body)
            if not isinstance(payload, dict):
                raise BridgeError("The request must be a JSON object.")
            atomic_json(self.runtime.root / "last-request-shape.json", {
                "fields": sorted(payload),
                "roles": [str(m.get("role"))[:30] for m in payload.get("messages", []) if isinstance(m, dict)],
                "content_types": sorted({str(b.get("type"))[:50] for m in payload.get("messages", []) if isinstance(m, dict)
                                         and isinstance(m.get("content"), list) for b in m["content"] if isinstance(b, dict)}),
                "tool_formats": sorted({str(t.get("type", "function"))[:50] + (":schema" if "input_schema" in t else ":no-schema")
                                         for t in payload.get("tools", []) if isinstance(t, dict)})})
            settings = self.runtime.settings
            if path == "/v1/messages/count_tokens":
                self.runtime.resolve_route(payload.get("model", ""))
                counted = apply_mapping_options(payload, settings)
                self.json_response(200, {"input_tokens": estimated_tokens(counted)}, {"X-Mistral-Bridge-Token-Count": "estimate"})
                return
            plan = self.runtime.plan(payload)
            upstream, names = plan["body"], plan.get("tool_name_map", {})
            if plan["protocol"] == "anthropic":
                # Protocol capability negotiation belongs to the Messages request.
                # Never copy local authentication or arbitrary client headers.
                for name in ("anthropic-version", "anthropic-beta"):
                    value = self.headers.get(name)
                    if value and len(value) <= 4096 and "\r" not in value and "\n" not in value:
                        plan["headers"][name] = value
        except (ValueError, TypeError, KeyError, AttributeError, BridgeError) as exc:
            self.error(400, str(exc))
            return
        if not self.runtime.semaphore.acquire(blocking=False):
            self.error(429, "Eight requests are already active. Try again shortly.")
            return
        connection = None
        response = None
        upstream_socket = None
        disconnected = threading.Event()
        write_lock = threading.Lock()
        streaming = False
        closed = threading.Event()
        monitor = None
        ping = None
        usage = {}
        service_tier = None
        try:
            with self.runtime.lock:
                self.runtime.active += 1
            connection, endpoint = self.runtime.upstream(plan["url"])
            with self.runtime.lock:
                self.runtime.connections.add(connection)

            def cancel_monitor():
                while not closed.wait(.25):
                    if self.runtime.stopping.is_set():
                        disconnected.set()
                    else:
                        try:
                            readable, _, _ = select.select([self.connection], [], [], 0)
                            if readable and self.connection.recv(1, socket.MSG_PEEK | socket.MSG_DONTWAIT) == b"":
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

            monitor = threading.Thread(target=cancel_monitor, daemon=True)
            monitor.start()
            encoded = json.dumps(upstream, ensure_ascii=False).encode()
            headers = {**plan["headers"], "Accept": "text/event-stream" if upstream.get("stream") else "application/json"}
            connection.request("POST", endpoint, encoded, headers)
            response = connection.getresponse()
            upstream_socket = connection.sock or getattr(getattr(response.fp, "raw", None), "_sock", None)
            if response.status != 200:
                raw = response.read(65536)
                try:
                    data = json.loads(raw)
                    error = data.get("error", data)
                    detail = error.get("message", data.get("detail", "")) if isinstance(error, dict) else str(error)
                except (ValueError, AttributeError):
                    detail = ""
                detail = str(detail)
                if plan["private_key"]:
                    detail = detail.replace(plan["private_key"], "[redacted]")
                detail = detail[:700]
                message = f"{plan['provider_name']} returned HTTP {response.status}" + (": " + detail if detail else ".")
                unavailable = response.status in {400, 404, 410} and any(term in detail.lower() for term in ("invalid model", "model not found", "model has been deprecated", "model is no longer"))
                self.runtime.record("error", plan["route"], response.status, model_unavailable=unavailable)
                self.error(response.status if response.status in {400, 401, 402, 403, 404, 413, 429, 500, 502, 503, 504} else 502, message)
                return
            if not upstream.get("stream"):
                raw = response.read(MAX_BODY + 1)
                if len(raw) > MAX_BODY:
                    raise BridgeError("The provider response exceeded the response limit.")
                decoded = json.loads(raw)
                if plan["provider_id"] == "grok" and decoded.get("service_tier") in {"default", "priority"}:
                    service_tier = decoded["service_tier"]
                if plan["provider_id"] == "gemini":
                    result = translate_gemini_response(decoded, payload["model"], names, plan["upstream_model"],
                                                       plan["replay_scope"], self.runtime.replay_key, model_spec=plan["model_spec"])
                else:
                    result = translate_response(decoded, payload["model"], names) if plan["protocol"] == "chat_completions" else decoded
                if result.get("type") != "message" or not isinstance(result.get("content"), list):
                    raise BridgeError("The provider returned an invalid Messages response.")
                if plan["protocol"] == "anthropic":
                    result["model"] = payload["model"]
                if plan["provider_id"] == "cerebras":
                    reasoning = decoded.get("choices", [{}])[0].get("message", {}).get("reasoning")
                    if plan["replay_required"] and any(block.get("type") == "tool_use" for block in result["content"]) and not reasoning:
                        raise BridgeError("Cerebras returned tool calls without the reasoning required to continue this model.")
                    thinking = sign_thinking(reasoning, result["content"], plan["upstream_model"], plan["replay_scope"], self.runtime.replay_key)
                    if thinking is not None:
                        result["content"].insert(0, thinking)
                usage = result.get("usage", {})
                self.json_response(200, result)
            else:
                if plan["provider_id"] == "gemini":
                    translator = GeminiStreamAdapter(payload["model"], names, plan["upstream_model"], plan["replay_scope"],
                                                     self.runtime.replay_key, model_spec=plan["model_spec"])
                elif plan["provider_id"] == "cerebras":
                    translator = CerebrasStreamAdapter(payload["model"], names, plan["upstream_model"], plan["replay_scope"], self.runtime.replay_key,
                                                       require_tool_reasoning=plan["replay_required"])
                else:
                    translator = StreamTranslator(payload["model"], names) if plan["protocol"] == "chat_completions" else None
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Transfer-Encoding", "chunked")
                self.send_header("Connection", "close")
                self.end_headers()
                streaming = True

                def write_chunk(data):
                    if disconnected.is_set():
                        raise BrokenPipeError()
                    with write_lock:
                        self.wfile.write(f"{len(data):x}\r\n".encode() + data + b"\r\n")
                        self.wfile.flush()

                def emit(event):
                    write_chunk(("event: " + event["type"] + "\ndata: " + json.dumps(event, ensure_ascii=False) + "\n\n").encode())

                def ping_loop():
                    while not closed.wait(10):
                        try:
                            emit({"type": "ping"})
                        except (OSError, ValueError):
                            disconnected.set()
                            return

                ping = threading.Thread(target=ping_loop, daemon=True)
                ping.start()
                if translator:
                    for event in translator.start():
                        emit(event)
                event_lines = []
                event_size = 0
                done = False
                while not disconnected.is_set():
                    line = response.readline(MAX_BODY + 1)
                    if not line:
                        break
                    event_size += len(line)
                    if event_size > MAX_BODY:
                        raise BridgeError("The provider sent an oversized stream event.")
                    if line in (b"\n", b"\r\n"):
                        data = b"\n".join(event_lines)
                        event_lines.clear()
                        event_size = 0
                        if data == b"[DONE]":
                            done = translator is not None
                            break
                        if data:
                            chunk = json.loads(data)
                            if plan["provider_id"] == "grok" and chunk.get("service_tier") in {"default", "priority"}:
                                service_tier = chunk["service_tier"]
                            if chunk.get("error") or chunk.get("type") == "error":
                                raise BridgeError("The provider reported an error during generation.")
                            if translator:
                                for event in translator.feed(chunk):
                                    emit(event)
                            else:
                                kind = chunk.get("type")
                                if kind not in {"message_start", "content_block_start", "content_block_delta", "content_block_stop", "message_delta", "message_stop", "ping"}:
                                    raise BridgeError("The provider sent an unsupported Messages event.")
                                if kind == "message_start":
                                    chunk["message"]["model"] = payload["model"]
                                    usage.update(chunk.get("message", {}).get("usage", {}))
                                elif kind == "message_delta":
                                    usage.update(chunk.get("usage", {}))
                                emit(chunk)
                                if kind == "message_stop":
                                    done = True
                                    break
                    elif line.startswith(b"data:"):
                        event_lines.append(line[5:].strip())
                if disconnected.is_set():
                    raise BrokenPipeError()
                if not done:
                    raise BridgeError("The provider stream ended unexpectedly. Please retry.")
                if translator:
                    for event in translator.end():
                        emit(event)
                    usage = translator.usage
                closed.set()
                with write_lock:
                    self.wfile.write(b"0\r\n\r\n")
                    self.wfile.flush()
                self.close_connection = True
            self.runtime.record("completed", plan["route"], 200, usage, service_tier=service_tier)
        except (BrokenPipeError, ConnectionResetError):
            self.runtime.record("cancelled", plan["route"])
        except Exception as exc:
            if disconnected.is_set():
                self.runtime.record("cancelled", plan["route"])
            else:
                message = str(exc) if isinstance(exc, (BridgeError, CerebrasReplayError, GeminiError)) else "The provider connection failed. Check your connection and try again."
                if plan["private_key"]:
                    message = message.replace(plan["private_key"], "[redacted]")
                self.runtime.record("error", plan["route"], 502)
                try:
                    if streaming:
                        emit({"type": "error", "error": {"type": "api_error", "message": message}})
                        closed.set()
                        with write_lock:
                            self.wfile.write(b"0\r\n\r\n")
                    else:
                        self.error(502, message)
                except OSError:
                    pass
        finally:
            closed.set()
            if response:
                response.close()
            if connection:
                connection.close()
            with self.runtime.lock:
                self.runtime.connections.discard(connection)
                self.runtime.active -= 1
            self.runtime.semaphore.release()
            self.close_connection = True


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, runtime, port=None):
        self.runtime = runtime
        super().__init__(("127.0.0.1", runtime.settings["port"] if port is None else port), Handler)

    def handle_error(self, *_):
        # Base class prints request tracebacks. Keep request content private.
        pass


def serve(root, parent_pipe=False):
    private_directory(root)
    lock = open(root / "gateway.lock", "a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        raise BridgeError("This provider gateway is already running.") from exc
    runtime = Runtime(root)
    try:
        server = Server(runtime)
    except OSError as exc:
        raise BridgeError(f"Port {runtime.settings['port']} is in use. Choose another port in Providers settings.") from exc

    def stop(*_):
        runtime.stopping.set()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    if parent_pipe:
        def parent_watch():
            sys.stdin.buffer.read()
            stop()
        threading.Thread(target=parent_watch, daemon=True).start()
    print(json.dumps({"ready": True, "port": server.server_address[1], "credential_source": runtime.source}), flush=True)
    server.serve_forever(poll_interval=.2)
    runtime.stopping.set()
    with runtime.lock:
        connections = list(runtime.connections)
    for connection in connections:
        try:
            if connection.sock:
                connection.sock.shutdown(socket.SHUT_RDWR)
            connection.close()
        except OSError:
            pass
    server.server_close()
    lock.close()


def catalogue_command_result(settings, root, lifecycle):
    inventory = cached_catalogue(settings, root)
    return {
        "models": inventory.get("models", []),
        "friendly_names": model_labels(settings, inventory.get("models", [])),
        "catalog_summary": {key: value for key, value in inventory.items()
                            if key not in {"models", "raw"}},
        "provider_definitions": provider_presentations(settings),
        "codex_models": codex_choices(settings, inventory),
        "catalogue_lifecycle": lifecycle,
        "catalogue_fingerprint": lifecycle.get("catalogue_fingerprint"),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["inspect", "validate", "save", "discover", "refresh-all", "prepare-launch", "activate", "restore", "serve", "codex-status", "codex-prepare", "codex-activate", "codex-restore"])
    parser.add_argument("--provider", choices=list(PROVIDERS), default="mistral")
    parser.add_argument("--parent-pipe", action="store_true")
    args = parser.parse_args()
    root = state_root()
    bootstrap_metadata(root)
    if args.command == "serve":
        serve(root, args.parent_pipe)
        return
    if args.command == "inspect":
        result = inspect_state(root)
        settings = load_settings(root)
        result.update(CodexProfile(root).status())
        result["codex_models"] = codex_choices(settings, cached_catalogue(settings, root))
    elif args.command == "codex-status":
        settings = load_settings(root)
        result = {**CodexProfile(root).status(), "codex_models": codex_choices(settings, cached_catalogue(settings, root))}
    elif args.command == "codex-prepare":
        settings = load_settings(root)
        # Freshness-scoped preparation only: an unconditional refresh-all here
        # would drift the catalogue digest away from a gateway already running
        # for the other desktop harness.
        lifecycle = require_prepared(prepare_launch(codex_launch_settings(settings), root))
        inventory = cached_catalogue(settings, root)
        available = codex_choices(settings, inventory)
        available_ids = {model["id"] for model in available}
        missing = [route for route in settings.get("codex_catalogue") or []
                   if route not in available_ids]
        if missing:
            raise BridgeError("The Codex catalogue selection is not fully advertised: "
                              + ", ".join(missing)
                              + ". Refresh those providers or remove the routes from the Codex catalogue.")
        if settings.get("codex_model") not in available_ids:
            raise BridgeError("The selected model is not available in the Codex catalogue. Refresh its provider metadata or choose another listed model.")
        qualification = qualify_runtime(settings, inventory)
        prepared = {"catalogue_digest": catalogue_digest(settings, inventory), "model": settings["codex_model"],
                    "runtime_signature": qualification["runtime_signature"]}
        atomic_json(root / "codex-prepared.json", prepared)
        result = {**catalogue_command_result(settings, root, lifecycle), **prepared}
    elif args.command == "codex-activate":
        settings = load_settings(root)
        require_prepared(validate_prepared_launch(codex_launch_settings(settings), root))
        inventory = cached_catalogue(settings, root)
        expected = catalogue_digest(settings, inventory)
        prepared = read_json(root / "codex-prepared.json")
        if prepared != {"catalogue_digest": expected, "model": settings["codex_model"], "runtime_signature": runtime_signature()}:
            raise BridgeError("Prepare the Codex model catalogue again before switching.")
        request = urllib.request.Request(f"http://127.0.0.1:{settings['port']}/_bridge/status", headers={"Authorization": "Bearer " + gateway_token(root)})
        try:
            with urllib.request.urlopen(request, timeout=3) as response:
                status = json.load(response)
            if not status.get("running") or status.get("codex_catalogue_digest") != expected:
                raise ValueError()
        except Exception as exc:
            raise BridgeError("Start the gateway with the prepared Codex catalogue before switching.") from exc
        result = CodexProfile(root).activate(settings, inventory)
    elif args.command == "codex-restore":
        result = CodexProfile(root).restore()
    elif args.command in {"validate", "save"}:
        settings = validate_settings(json.load(sys.stdin))
        if args.command == "save":
            private_directory(root)
            atomic_json(root / "settings.json", settings)
        inventory = cached_catalogue(settings, root)
        result = {"saved": args.command == "save", "settings": settings,
                  "friendly_names": model_labels(settings, inventory.get("models", [])), "models": inventory.get("models", []),
                  "catalog_summary": {k: v for k, v in inventory.items() if k != "models"},
                  "provider_definitions": provider_presentations(settings), "codex_models": codex_choices(settings, inventory)}
    elif args.command == "discover":
        settings = load_settings(root)
        source = discover_provider(settings, args.provider, root)
        inventory = cached_catalogue(settings, root)
        result = {"models": inventory["models"], "credential_source": source, "friendly_names": model_labels(settings, inventory["models"]),
                  "codex_models": codex_choices(settings, inventory),
                  "catalog_summary": {k: v for k, v in inventory.items() if k not in {"models", "raw"}}}
    elif args.command in {"refresh-all", "prepare-launch"}:
        settings = load_settings(root)
        lifecycle = (refresh_all(settings, root) if args.command == "refresh-all"
                     else prepare_launch(settings, root))
        if args.command == "prepare-launch":
            require_prepared(lifecycle)
        result = catalogue_command_result(settings, root, lifecycle)
    elif args.command == "activate":
        settings = load_settings(root)
        lifecycle = require_prepared(validate_prepared_launch(settings, root))
        token = gateway_token(root)
        # Authenticate the readiness probe so an unrelated process on this port
        # cannot be mistaken for this app's gateway.
        request = urllib.request.Request(f"http://127.0.0.1:{settings['port']}/_bridge/status", headers={"Authorization": "Bearer " + token})
        try:
            with urllib.request.urlopen(request, timeout=3) as response:
                runtime_status = json.load(response)
                if not runtime_status.get("running"):
                    raise ValueError()
        except Exception as exc:
            raise BridgeError("Start the gateway before launching Claude.") from exc
        fingerprint_error = runtime_fingerprint_error(
            lifecycle["catalogue_fingerprint"],
            runtime_status.get("catalogue_fingerprint"), settings,
        )
        if fingerprint_error:
            raise BridgeError(fingerprint_error)
        result = ClaudeProfile(root).activate(settings, token)
        result["catalogue_lifecycle"] = lifecycle
    else:
        result = ClaudeProfile(root).restore()
    print(json.dumps({"ok": True, **result}))


if __name__ == "__main__":
    try:
        main()
    except CataloguePreparationError as exc:
        print(json.dumps({"ok": False, "error": str(exc),
                          "catalogue_lifecycle": exc.result}), flush=True)
        sys.exit(1)
    except (BridgeError, OSError, ValueError, subprocess.TimeoutExpired) as exc:
        message = str(exc) if isinstance(exc, (BridgeError, CerebrasReplayError)) else "The operation could not finish. Check the local runtime and configuration."
        print(json.dumps({"ok": False, "error": message}), flush=True)
        sys.exit(1)
