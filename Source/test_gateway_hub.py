"""HTTP integration tests for provider-qualified native and Cerebras routes.

All upstreams are local deterministic mocks.  No credential lookup or paid
inference is performed.
"""
from __future__ import annotations

import copy
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from bridge_core import SLOTS, atomic_json, default_settings
from cerebras_replay import validate_messages
from gateway import Runtime, Server
from hub_config import connection_signature
from protocol import estimated_tokens


LOCAL_REQUEST_TEXT = "LOCAL-REQUEST-TEXT-MUST-NOT-BE-LOGGED"
PROVIDER_KEY = "PROVIDER-UPSTREAM-KEY"


def tool_definition(name="read_file"):
    return {
        "name": name,
        "description": "Read a local file",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    }


def has_native_tool_result(body):
    return any(
        isinstance(message, dict)
        and isinstance(message.get("content"), list)
        and any(isinstance(block, dict) and block.get("type") == "tool_result"
                for block in message["content"])
        for message in body.get("messages", [])
    )


def has_chat_tool_result(body):
    return any(
        isinstance(message, dict) and message.get("role") == "tool"
        for message in body.get("messages", [])
    )


def native_message(content, stop_reason, *, usage=None):
    return {
        "id": "msg_native_mock",
        "type": "message",
        "role": "assistant",
        "model": "deepseek-flash",
        "content": content,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": usage or {"input_tokens": 12, "output_tokens": 7},
    }


def native_stream_first():
    return [
        {"type": "message_start", "message": {
            "id": "msg_native_stream",
            "type": "message",
            "role": "assistant",
            "model": "deepseek-flash",
            "content": [],
            "stop_reason": None,
            "stop_sequence": None,
            "usage": {"input_tokens": 14, "output_tokens": 0},
        }},
        {"type": "content_block_start", "index": 0,
         "content_block": {"type": "thinking", "thinking": "", "signature": ""}},
        {"type": "content_block_delta", "index": 0,
         "delta": {"type": "thinking_delta", "thinking": "Read before answering."}},
        {"type": "content_block_delta", "index": 0,
         "delta": {"type": "signature_delta", "signature": "native-provider-signature"}},
        {"type": "content_block_stop", "index": 0},
        {"type": "content_block_start", "index": 1,
         "content_block": {"type": "tool_use", "id": "native_call_stream", "name": "read_file", "input": {}}},
        {"type": "content_block_delta", "index": 1,
         "delta": {"type": "input_json_delta", "partial_json": '{"path":"stream.txt"}'}},
        {"type": "content_block_stop", "index": 1},
        {"type": "message_delta", "delta": {"stop_reason": "tool_use", "stop_sequence": None},
         "usage": {"input_tokens": 14, "output_tokens": 8}},
        {"type": "message_stop"},
    ]


def native_stream_final():
    return [
        {"type": "message_start", "message": {
            "id": "msg_native_final",
            "type": "message",
            "role": "assistant",
            "model": "deepseek-flash",
            "content": [],
            "stop_reason": None,
            "stop_sequence": None,
            "usage": {"input_tokens": 20, "output_tokens": 0},
        }},
        {"type": "content_block_start", "index": 0,
         "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0,
         "delta": {"type": "text_delta", "text": "Native stream complete."}},
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None},
         "usage": {"input_tokens": 20, "output_tokens": 4}},
        {"type": "message_stop"},
    ]


class MockProvider(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    mode = "normal"
    requests = []
    request_headers = []
    release = threading.Event()
    lock = threading.Lock()

    def log_message(self, *_):
        pass

    @classmethod
    def reset(cls, mode="normal"):
        cls.mode = mode
        cls.requests = []
        cls.request_headers = []
        cls.release = threading.Event()

    def read_body(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        body = json.loads(raw)
        with type(self).lock:
            type(self).requests.append(body)
            type(self).request_headers.append({key.lower(): value for key, value in self.headers.items()})
        return body

    def send_json(self, status, value):
        raw = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(raw)
        self.close_connection = True

    def send_sse(self, events, *, chat=False):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            for event in events:
                self.wfile.write(b"data: " + json.dumps(event).encode() + b"\n\n")
                self.wfile.flush()
            if chat:
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
        except OSError:
            pass
        self.close_connection = True

    def do_POST(self):
        body = self.read_body()
        if self.mode == "rate_limit":
            self.send_json(429, {"error": {"message": "quota denied for " + PROVIDER_KEY}})
            return
        if self.mode == "http_error":
            self.send_json(503, {"error": {"message": "mock overload"}})
            return
        if self.mode == "native_stream_error":
            self.send_sse([
                native_stream_first()[0],
                {"type": "error", "error": {"type": "overloaded_error", "message": "mock stream failure"}},
            ])
            return
        if self.mode == "slow_native":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Connection", "close")
            self.end_headers()
            try:
                first = native_stream_first()[0]
                self.wfile.write(b"data: " + json.dumps(first).encode() + b"\n\n")
                self.wfile.flush()
                type(self).release.wait(4)
                self.wfile.write(b"data: " + json.dumps({"type": "message_stop"}).encode() + b"\n\n")
                self.wfile.flush()
            except OSError:
                pass
            self.close_connection = True
            return
        if self.path == "/v1/messages":
            self.handle_native(body)
            return
        if self.path == "/chat/completions":
            self.handle_cerebras(body)
            return
        self.send_json(404, {"error": {"message": "unexpected mock path"}})

    def handle_native(self, body):
        second = has_native_tool_result(body)
        if body.get("stream"):
            self.send_sse(native_stream_final() if second else native_stream_first())
            return
        if second:
            self.send_json(200, native_message(
                [{"type": "text", "text": "Native JSON complete."}],
                "end_turn",
                usage={"input_tokens": 20, "output_tokens": 4},
            ))
        else:
            self.send_json(200, native_message([
                {
                    "type": "thinking",
                    "thinking": "Read before answering.",
                    "signature": "native-provider-signature",
                },
                {
                    "type": "tool_use",
                    "id": "native_call_json",
                    "name": "read_file",
                    "input": {"path": "json.txt"},
                },
            ], "tool_use"))

    def handle_cerebras(self, body):
        second = has_chat_tool_result(body)
        if body.get("stream"):
            if second:
                events = [
                    {"choices": [{"index": 0, "delta": {"reasoning": "The tool results are sufficient."}}]},
                    {"choices": [{"index": 0, "delta": {"content": "Cerebras stream complete."}}]},
                    {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                     "usage": {"prompt_tokens": 30, "completion_tokens": 9}},
                ]
            else:
                events = [
                    {"choices": [{"index": 0, "delta": {"reasoning": "Inspect "}}]},
                    {"choices": [{"index": 0, "delta": {"reasoning": "both files."}}]},
                    {"choices": [{"index": 0, "delta": {"tool_calls": [
                        {"index": 0, "id": "cerebras_call_one", "function": {
                            "name": "read_file", "arguments": '{"path":'}},
                        {"index": 1, "id": "cerebras_call_two", "function": {
                            "name": "read_file", "arguments": '{"path":'}},
                    ]}}]},
                    {"choices": [{"index": 0, "delta": {"tool_calls": [
                        {"index": 1, "function": {"arguments": '"b.txt"}'}},
                        {"index": 0, "function": {"arguments": '"a.txt"}'}},
                    ]}}]},
                    {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
                     "usage": {"prompt_tokens": 18, "completion_tokens": 12}},
                ]
            self.send_sse(events, chat=True)
            return
        if second:
            response = {
                "id": "chatcmpl_final",
                "object": "chat.completion",
                "model": "gpt-oss-120b",
                "choices": [{
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "reasoning": "The file contents answer the request.",
                        "content": "Cerebras JSON complete.",
                        "tool_calls": None,
                    },
                    "finish_reason": "stop",
                }],
                "usage": {"prompt_tokens": 24, "completion_tokens": 8},
            }
        else:
            response = {
                "id": "chatcmpl_tool",
                "object": "chat.completion",
                "model": "gpt-oss-120b",
                "choices": [{
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "reasoning": "The file must be read before answering.",
                        "content": "I will read it.",
                        "tool_calls": [{
                            "id": "cerebras_call_json",
                            "type": "function",
                            "function": {"name": "read_file", "arguments": '{"path":"json.txt"}'},
                        }],
                    },
                    "finish_reason": "tool_calls",
                }],
                "usage": {"prompt_tokens": 16, "completion_tokens": 10},
            }
        self.send_json(200, response)


def parse_sse(raw):
    events = []
    for group in raw.decode().replace("\r\n", "\n").split("\n\n"):
        data = "\n".join(line[6:] for line in group.splitlines() if line.startswith("data: "))
        if data:
            events.append(json.loads(data))
    return events


def reconstruct_content(events):
    blocks = {}
    for event in events:
        kind = event.get("type")
        if kind == "content_block_start":
            block = copy.deepcopy(event["content_block"])
            if block.get("type") == "tool_use":
                block["partial_json"] = ""
            blocks[event["index"]] = block
        elif kind == "content_block_delta":
            block = blocks[event["index"]]
            delta = event["delta"]
            if delta["type"] == "thinking_delta":
                block["thinking"] += delta["thinking"]
            elif delta["type"] == "signature_delta":
                block["signature"] += delta["signature"]
            elif delta["type"] == "text_delta":
                block["text"] += delta["text"]
            elif delta["type"] == "input_json_delta":
                block["partial_json"] += delta["partial_json"]
    result = []
    for index in sorted(blocks):
        block = blocks[index]
        if block.get("type") == "tool_use":
            block["input"] = json.loads(block.pop("partial_json") or "{}")
        result.append(block)
    return result


class GatewayHubHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        MockProvider.reset()
        self.upstream = ThreadingHTTPServer(("127.0.0.1", 0), MockProvider)
        self.upstream.daemon_threads = True
        threading.Thread(target=self.upstream.serve_forever, daemon=True).start()
        self.runtime = None
        self.gateway = None

    def tearDown(self):
        MockProvider.release.set()
        if self.gateway is not None:
            self.gateway.shutdown()
            self.gateway.server_close()
        self.upstream.shutdown()
        self.upstream.server_close()
        self.temp.cleanup()

    def start_gateway(self, provider_id, model_id, spec):
        vibe = {
            "active_model": "unused",
            "active_display_name": "Unused",
            "key_name": "MISTRAL_API_KEY",
            "vibe_home": str(self.root / "vibe"),
            "configured_models": [],
        }
        with patch("bridge_core.vibe_settings", return_value=vibe):
            settings = default_settings()
        route = provider_id + "/" + model_id
        settings["mappings"] = {slot[0]: route for slot in SLOTS}
        atomic_json(self.root / "settings.json", settings)
        entry = {
            "id": model_id,
            "canonical_id": model_id,
            "display_name": "Mock " + model_id,
            "context": 131072,
            "aliases": [model_id],
            "tools": True,
            "vision": False,
            "reasoning": True,
            "effort_modes": [],
            "fast_mode": False,
            "inference_status": "advertised",
            "source": "mock_metadata",
            "evidence": "local-test",
            **spec,
        }
        atomic_json(self.root / "catalogues" / (provider_id + ".json"), {
            "provider_id": provider_id,
            "source": "mock_metadata",
            "connection_signature": connection_signature(
                provider_id, settings["providers"][provider_id]),
            "models": [entry],
        })
        upstream_url = f"http://127.0.0.1:{self.upstream.server_port}"
        with patch("bridge_core.vibe_settings", return_value=vibe):
            self.runtime = Runtime(self.root, upstream_url=upstream_url, key=PROVIDER_KEY)
        # Make accidental forwarding easy to detect without changing the token
        # that Runtime persisted in its private temporary state.
        self.assertNotEqual(self.runtime.token, PROVIDER_KEY)
        self.gateway = Server(self.runtime, 0)
        threading.Thread(target=self.gateway.serve_forever, daemon=True).start()
        return route

    def request(self, body, *, token=None, timeout=6, extra_headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.gateway.server_port, timeout=timeout)
        headers = {
            "Authorization": "Bearer " + (token or self.runtime.token),
            "Content-Type": "application/json",
        }
        headers.update(extra_headers or {})
        connection.request("POST", "/v1/messages", json.dumps(body), headers)
        response = connection.getresponse()
        raw = response.read()
        status = response.status
        response_headers = dict(response.getheaders())
        connection.close()
        deadline = time.monotonic() + 2
        while self.runtime.status()["active"] and time.monotonic() < deadline:
            time.sleep(.01)
        return status, raw, response_headers

    def assert_upstream_secret_boundary(self, provider_id):
        self.assertTrue(MockProvider.request_headers)
        for headers in MockProvider.request_headers:
            combined = json.dumps(headers)
            self.assertNotIn(self.runtime.token, combined)
            self.assertNotIn(self.runtime.replay_key, combined)
            if provider_id == "deepseek":
                self.assertEqual(headers.get("x-api-key"), PROVIDER_KEY)
                self.assertNotIn("authorization", headers)
            else:
                self.assertEqual(headers.get("authorization"), "Bearer " + PROVIDER_KEY)
        for body in MockProvider.requests:
            encoded = json.dumps(body)
            self.assertNotIn(self.runtime.token, encoded)
            self.assertNotIn(self.runtime.replay_key, encoded)

    def test_native_json_qualified_route_raw_model_auth_and_complete_tool_cycle(self):
        route = self.start_gateway("deepseek", "deepseek-flash", {
            "effort_modes": ["none", "low", "high", "max"],
            "reasoning_history": "native",
        })
        first_payload = {
            "model": "claude-fable-5",
            "max_tokens": 256,
            "messages": [{"role": "user", "content": LOCAL_REQUEST_TEXT}],
            "tools": [tool_definition()],
        }
        status, raw, _ = self.request(first_payload, extra_headers={
            "anthropic-version": "2023-06-01",
            "anthropic-beta": "interleaved-thinking-2025-05-14",
            "x-api-key": "client-key-must-not-forward",
            "X-Client-Identity": "client-identity-must-not-forward",
        })
        self.assertEqual(status, 200)
        first = json.loads(raw)
        self.assertEqual(first["model"], "claude-fable-5")
        self.assertEqual(first["stop_reason"], "tool_use")
        self.assertEqual(first["content"][0], {
            "type": "thinking",
            "thinking": "Read before answering.",
            "signature": "native-provider-signature",
        })
        tool = first["content"][1]
        self.assertEqual(tool["id"], "native_call_json")

        second_payload = copy.deepcopy(first_payload)
        second_payload["messages"] += [
            {"role": "assistant", "content": first["content"]},
            {"role": "user", "content": [{
                "type": "tool_result",
                "tool_use_id": tool["id"],
                "content": "mock file contents",
            }]},
        ]
        status, raw, _ = self.request(second_payload)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)["content"][0]["text"], "Native JSON complete.")
        self.assertEqual([body["model"] for body in MockProvider.requests], ["deepseek-flash", "deepseek-flash"])
        self.assertEqual(MockProvider.requests[1]["messages"][1]["content"], first["content"])
        first_headers = MockProvider.request_headers[0]
        self.assertEqual(first_headers.get("anthropic-version"), "2023-06-01")
        self.assertEqual(first_headers.get("anthropic-beta"), "interleaved-thinking-2025-05-14")
        self.assertEqual(first_headers.get("x-api-key"), PROVIDER_KEY)
        self.assertNotIn("x-client-identity", first_headers)
        self.assertEqual(self.runtime.status()["last_model"], route)
        self.assertEqual(self.runtime.status()["providers"]["deepseek"]["completed"], 2)
        self.assert_upstream_secret_boundary("deepseek")
        log = (self.root / "activity.jsonl").read_text()
        self.assertNotIn(LOCAL_REQUEST_TEXT, log)
        self.assertNotIn(PROVIDER_KEY, log)

    def test_desktop_context_reminders_are_rewritten_on_native_routes(self):
        self.start_gateway("deepseek", "deepseek-flash", {
            "effort_modes": ["none", "low", "high", "max"],
            "reasoning_history": "native",
        })
        reminder = "<total_tokens>15000000 tokens left</total_tokens>"
        payload = {
            "model": "claude-fable-5",
            "max_tokens": 256,
            "system": reminder,
            "messages": [{"role": "user", "content": LOCAL_REQUEST_TEXT + "\n" + reminder}],
        }
        remaining = 131072 - estimated_tokens(payload)
        status, raw, _ = self.request(payload)
        self.assertEqual(status, 200, raw)
        combined = json.dumps(MockProvider.requests[0])
        self.assertNotIn("15000000", combined)
        self.assertIn(f"<total_tokens>{remaining} tokens left</total_tokens>", combined)
        self.assertIn(LOCAL_REQUEST_TEXT, combined)
        self.assertEqual(self.runtime.plan(payload)["compatibility"]["context_reminders"], "catalogue_remaining")

    def test_mapping_options_omit_native_system_and_tools_before_context_check(self):
        self.start_gateway("deepseek", "deepseek-flash", {
            "effort_modes": ["none", "low", "high", "max"],
            "reasoning_history": "native",
            "context": 2048,
        })
        tools = [tool_definition()]
        tools[0]["description"] = "T" * 4000
        payload = {
            "model": "claude-fable-5",
            "max_tokens": 256,
            "system": "S" * 4000,
            "messages": [{"role": "user", "content": LOCAL_REQUEST_TEXT}],
            "tools": tools,
        }
        status, raw, _ = self.request(payload)
        self.assertEqual(status, 400, raw)
        self.runtime.settings["mapping_options"] = {
            "claude-fable-5": {"omit_system": True, "omit_tools": True},
        }
        status, raw, _ = self.request(payload)
        self.assertEqual(status, 200, raw)
        upstream = MockProvider.requests[-1]
        self.assertNotIn("tools", upstream)
        self.assertNotIn("system", upstream)
        self.assertIn(LOCAL_REQUEST_TEXT, json.dumps(upstream))


    def test_native_sse_preserves_thinking_and_completes_tool_cycle(self):
        self.start_gateway("deepseek", "deepseek-flash", {
            "effort_modes": ["none", "low", "high", "max"],
            "reasoning_history": "native",
        })
        payload = {
            "model": "deepseek/deepseek-flash",
            "max_tokens": 256,
            "stream": True,
            "messages": [{"role": "user", "content": "read stream.txt"}],
            "tools": [tool_definition()],
        }
        status, raw, headers = self.request(payload)
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "text/event-stream")
        first_events = parse_sse(raw)
        self.assertEqual(first_events[0]["message"]["model"], "deepseek/deepseek-flash")
        first_content = reconstruct_content(first_events)
        self.assertEqual(first_content[0]["type"], "thinking")
        self.assertEqual(first_content[0]["signature"], "native-provider-signature")
        self.assertEqual(first_content[1]["input"], {"path": "stream.txt"})

        second = copy.deepcopy(payload)
        second["messages"] += [
            {"role": "assistant", "content": first_content},
            {"role": "user", "content": [{
                "type": "tool_result",
                "tool_use_id": first_content[1]["id"],
                "content": "stream contents",
            }]},
        ]
        status, raw, _ = self.request(second)
        self.assertEqual(status, 200)
        final_events = parse_sse(raw)
        self.assertEqual(reconstruct_content(final_events), [
            {"type": "text", "text": "Native stream complete."},
        ])
        self.assertEqual(final_events[-1]["type"], "message_stop")
        self.assertEqual(MockProvider.requests[1]["messages"][1]["content"], first_content)
        self.assert_upstream_secret_boundary("deepseek")

    def test_upstream_http_rate_and_error_statuses_are_private_and_attributed(self):
        self.start_gateway("deepseek", "deepseek-flash", {"reasoning_history": "native"})
        payload = {
            "model": "claude-fable-5",
            "max_tokens": 64,
            "messages": [{"role": "user", "content": "hello"}],
        }
        for mode, expected_status, expected_type in (
            ("rate_limit", 429, "rate_limit_error"),
            ("http_error", 503, "overloaded_error"),
        ):
            with self.subTest(mode=mode):
                MockProvider.mode = mode
                status, raw, _ = self.request(payload)
                self.assertEqual(status, expected_status)
                result = json.loads(raw)
                self.assertEqual(result["error"]["type"], expected_type)
                self.assertNotIn(PROVIDER_KEY, raw.decode())
                self.assertNotIn(self.runtime.token, raw.decode())
        status = self.runtime.status()
        self.assertEqual(status["failed"], 2)
        self.assertEqual(status["providers"]["deepseek"]["failed"], 2)
        log = (self.root / "activity.jsonl").read_text()
        self.assertNotIn(PROVIDER_KEY, log)

    def test_native_stream_error_and_client_cancellation_do_not_record_success(self):
        self.start_gateway("deepseek", "deepseek-flash", {"reasoning_history": "native"})
        payload = {
            "model": "claude-fable-5",
            "max_tokens": 64,
            "stream": True,
            "messages": [{"role": "user", "content": "hello"}],
        }
        MockProvider.mode = "native_stream_error"
        status, raw, _ = self.request(payload)
        self.assertEqual(status, 200)
        events = parse_sse(raw)
        self.assertEqual(events[-1]["type"], "error")
        self.assertFalse(any(event.get("type") == "message_stop" for event in events))
        self.assertEqual(self.runtime.status()["failed"], 1)

        MockProvider.mode = "slow_native"
        connection = socket.create_connection(("127.0.0.1", self.gateway.server_port), timeout=3)
        encoded = json.dumps(payload).encode()
        request = (
            f"POST /v1/messages HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{self.gateway.server_port}\r\n"
            f"Authorization: Bearer {self.runtime.token}\r\n"
            "Content-Type: application/json\r\n"
            f"Content-Length: {len(encoded)}\r\n\r\n"
        ).encode() + encoded
        received = b""
        try:
            connection.sendall(request)
            deadline = time.monotonic() + 2
            while b"message_start" not in received and time.monotonic() < deadline:
                chunk = connection.recv(4096)
                if not chunk:
                    break
                received += chunk
            self.assertIn(b"message_start", received)
            connection.shutdown(socket.SHUT_RDWR)
        finally:
            connection.close()
        deadline = time.monotonic() + 2
        while self.runtime.status()["active"] and time.monotonic() < deadline:
            time.sleep(.02)
        self.assertEqual(self.runtime.status()["active"], 0)
        self.assertEqual(self.runtime.status()["completed"], 0)
        self.assertEqual(self.runtime.status()["failed"], 1)
        self.assertIn('"event": "cancelled"', (self.root / "activity.jsonl").read_text())

    def test_cerebras_json_signed_reasoning_replays_into_chat_tool_history(self):
        self.start_gateway("cerebras", "gpt-oss-120b", {
            "effort_modes": ["low", "medium", "high"],
            "max_output": 40960,
            "reasoning_history": "gateway_signed_replay",
            "complete_tool_cycles": True,
        })
        payload = {
            "model": "claude-fable-5",
            "max_tokens": 256,
            "messages": [{"role": "user", "content": "read json.txt"}],
            "tools": [tool_definition()],
            "output_config": {"effort": "medium"},
        }
        status, raw, _ = self.request(payload)
        self.assertEqual(status, 200)
        first = json.loads(raw)
        self.assertEqual(first["content"][0]["type"], "thinking")
        self.assertTrue(first["content"][0]["signature"].startswith("mb-cerebras-v1."))
        scope = self.runtime.plan(payload)["replay_scope"]
        self.assertEqual(
            validate_messages(
                [{"role": "assistant", "content": first["content"]}],
                "gpt-oss-120b",
                scope,
                self.runtime.replay_key,
            ),
            {0: "The file must be read before answering."},
        )
        tool = next(block for block in first["content"] if block["type"] == "tool_use")
        second = copy.deepcopy(payload)
        second["messages"] += [
            {"role": "assistant", "content": first["content"]},
            {"role": "user", "content": [{
                "type": "tool_result",
                "tool_use_id": tool["id"],
                "content": "json contents",
            }]},
        ]
        status, raw, _ = self.request(second)
        self.assertEqual(status, 200)
        final = json.loads(raw)
        self.assertEqual(final["content"][0]["type"], "thinking")
        self.assertEqual(final["content"][1]["text"], "Cerebras JSON complete.")
        self.assertEqual([body["model"] for body in MockProvider.requests], ["gpt-oss-120b", "gpt-oss-120b"])
        assistant = next(message for message in MockProvider.requests[1]["messages"] if message["role"] == "assistant")
        self.assertEqual(assistant["reasoning"], "The file must be read before answering.")
        self.assertEqual(assistant["tool_calls"][0]["id"], "cerebras_call_json")
        self.assert_upstream_secret_boundary("cerebras")

    def test_cerebras_stream_signed_reasoning_replays_multiple_tools_end_to_end(self):
        self.start_gateway("cerebras", "gpt-oss-120b", {
            "effort_modes": ["low", "medium", "high"],
            "max_output": 40960,
            "reasoning_history": "gateway_signed_replay",
            "complete_tool_cycles": True,
        })
        payload = {
            "model": "cerebras/gpt-oss-120b",
            "max_tokens": 256,
            "stream": True,
            "messages": [{"role": "user", "content": "read both"}],
            "tools": [tool_definition()],
            "output_config": {"effort": "medium"},
        }
        status, raw, _ = self.request(payload)
        self.assertEqual(status, 200)
        events = parse_sse(raw)
        content = reconstruct_content(events)
        self.assertEqual([block["type"] for block in content], ["thinking", "tool_use", "tool_use"])
        self.assertEqual(content[0]["thinking"], "Inspect both files.")
        self.assertTrue(content[0]["signature"].startswith("mb-cerebras-v1."))
        self.assertEqual([block["id"] for block in content[1:]], ["cerebras_call_one", "cerebras_call_two"])
        scope = self.runtime.plan(payload)["replay_scope"]
        self.assertEqual(
            validate_messages(
                [{"role": "assistant", "content": content}],
                "gpt-oss-120b",
                scope,
                self.runtime.replay_key,
            ),
            {0: "Inspect both files."},
        )

        second = copy.deepcopy(payload)
        second["messages"].append({"role": "assistant", "content": content})
        second["messages"].append({"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": content[1]["id"], "content": "a contents"},
            {"type": "tool_result", "tool_use_id": content[2]["id"], "content": "b contents"},
        ]})
        status, raw, _ = self.request(second)
        self.assertEqual(status, 200)
        final_events = parse_sse(raw)
        final_content = reconstruct_content(final_events)
        self.assertEqual([block["type"] for block in final_content], ["thinking", "text"])
        self.assertEqual(final_content[1]["text"], "Cerebras stream complete.")
        assistant = next(message for message in MockProvider.requests[1]["messages"] if message["role"] == "assistant")
        self.assertEqual(assistant["reasoning"], "Inspect both files.")
        self.assertEqual(
            [call["id"] for call in assistant["tool_calls"]],
            ["cerebras_call_one", "cerebras_call_two"],
        )
        self.assertEqual(final_events[-1]["type"], "message_stop")
        self.assertEqual(self.runtime.status()["providers"]["cerebras"]["completed"], 2)
        self.assert_upstream_secret_boundary("cerebras")


if __name__ == "__main__":
    unittest.main(verbosity=2)
