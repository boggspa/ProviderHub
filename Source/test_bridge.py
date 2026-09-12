"""Offline regression suite. Never uses real credentials or Claude directories."""
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

from bridge_core import (BridgeError, ClaudeProfile, PROFILE_ID, atomic_json, default_settings,
                         read_json, validate_settings)
from gateway import Runtime, Server
from protocol import (StreamTranslator, function_name, model_catalog, tool_id, translate_request,
                      translate_response)
from catalogue import build_catalogue, read_observations, route_specs


def config():
    value = default_settings()
    value["port"] = 11436
    value["mappings"] = {k: "test-model" for k in value["mappings"]}
    value["_model_specs"] = {"test-model": {"id": "test-model", "canonical_id": "test-model", "display_name": "Test Model",
        "context": 240000, "aliases": ["test-model"], "reasoning": True, "vision": True, "tools": True, "inference_status": "advertised"}}
    return value


def prompt(**extra):
    value = {"model": "claude-fable-5", "max_tokens": 64, "messages": [{"role": "user", "content": "hello"}]}
    value.update(extra)
    return value


class ProtocolTests(unittest.TestCase):
    def test_desktop_instruction_roles_preserved(self):
        result, _ = translate_request(prompt(messages=[{"role": "system", "content": "System rule"},
            {"role": "developer", "content": [{"type": "text", "text": "Developer rule"}]},
            {"role": "user", "content": "Hello"}]), config())
        self.assertEqual([m["content"] for m in result["messages"]], ["System rule", "Developer rule", "Hello"])
        self.assertEqual([m["role"] for m in result["messages"]], ["system", "system", "user"])

    def test_system_images_and_tools_preserved(self):
        body = prompt(system=[{"type": "text", "text": "Follow these instructions.", "cache_control": {"type": "ephemeral"}}],
                      tools=[{"name": "local.read/file", "description": "Read file", "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}}}])
        body["messages"][0]["content"] = [{"type": "text", "text": "What is here?"}, {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "YWJj"}}]
        result, names = translate_request(body, config())
        self.assertEqual(result["messages"][0]["content"], "Follow these instructions.")
        self.assertEqual(result["messages"][1]["content"][1]["image_url"], "data:image/png;base64,YWJj")
        self.assertEqual(names[result["tools"][0]["function"]["name"]], "local.read/file")
        self.assertEqual(result["model"], "test-model")

    def test_multi_turn_tools_ids_order_and_errors(self):
        body = prompt(messages=[
            {"role": "user", "content": "Read and edit."},
            {"role": "assistant", "content": [{"type": "text", "text": "Reading."},
                {"type": "tool_use", "id": "toolu_very-long-1", "name": "Read", "input": {"path": "a.txt"}},
                {"type": "tool_use", "id": "toolu_very-long-2", "name": "Read", "input": {"path": "b.txt"}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_very-long-1", "content": "alpha"},
                {"type": "tool_result", "tool_use_id": "toolu_very-long-2", "content": "not found", "is_error": True},
                {"type": "text", "text": "Now edit a.txt."}]}])
        result, _ = translate_request(body, config())
        items = result["messages"]
        self.assertEqual([m["role"] for m in items], ["user", "assistant", "tool", "tool", "user"])
        self.assertEqual(items[1]["tool_calls"][0]["id"], items[2]["tool_call_id"])
        self.assertEqual(len(items[2]["tool_call_id"]), 9)
        self.assertEqual(items[3]["content"], "Tool execution error:\nnot found")
        self.assertEqual(items[4]["content"], "Now edit a.txt.")
        # Replay of an upstream ID is normalised consistently on both sides.
        self.assertEqual(tool_id("abcdef123"), tool_id("abcdef123"))

    def test_unknown_models_and_hosted_tools_fail(self):
        with self.assertRaises(BridgeError): translate_request(prompt(model="unmapped"), config())
        with self.assertRaises(BridgeError): translate_request(prompt(tools=[{"type": "web_search_20250305", "name": "web_search"}]), config())
        with self.assertRaises(BridgeError): translate_request(prompt(messages=[{"role": "user", "content": [{"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": "abc"}}]}]), config())

    def test_context_limits_and_model_mappings(self):
        small = config(); small["_model_specs"]["test-model"]["context"] = 8000
        with self.assertRaises(BridgeError): translate_request(prompt(messages=[{"role": "user", "content": "x" * 30000}]), small)
        models = model_catalog(config())["data"]
        self.assertEqual(len(models), 1)
        self.assertEqual(models[0]["display_name"], "Test Model")
        self.assertTrue(models[0]["is_family_default"])
        self.assertEqual(translate_request(prompt(model="sonnet"), config())[0]["model"], "test-model")

    def test_tool_choice_and_explicit_thinking_disable(self):
        settings = config()
        result, _ = translate_request(prompt(thinking={"type": "disabled"}, tool_choice={"type": "any", "disable_parallel_tool_use": True}), settings)
        self.assertEqual(result["tool_choice"], "required")
        self.assertFalse(result["parallel_tool_calls"])
        self.assertEqual(result["reasoning_effort"], "none")

    def test_effort_comes_from_claude_and_never_changes_the_model(self):
        for native, expected in [("low", "none"), ("medium", "high"), ("high", "high"), ("xhigh", "high"), ("max", "high")]:
            result, _ = translate_request(prompt(output_config={"effort": native}), config())
            self.assertEqual(result["reasoning_effort"], expected)
            self.assertEqual(result["model"], "test-model")
        plain = config(); plain["_model_specs"]["test-model"]["reasoning"] = False
        result, _ = translate_request(prompt(output_config={"effort": "max"}), plain)
        self.assertNotIn("reasoning_effort", result)

    def test_exact_context_limits_and_no_fabricated_fast_mode(self):
        settings = config(); spec = settings["_model_specs"]["test-model"]
        spec["context"] = 1048576
        self.assertEqual(model_catalog(settings)["data"][0]["max_input_tokens"], 1048576)
        self.assertTrue(model_catalog(settings)["data"][0]["supports_1m"])
        result, _ = translate_request(prompt(model="claude-fable-5[1m]", max_tokens=50000), settings)
        self.assertEqual(result["max_tokens"], 50000)
        spec["context"] = 32768
        result, _ = translate_request(prompt(max_tokens=64000), settings)
        self.assertLess(result["max_tokens"], 32768)
        self.assertGreater(result["max_tokens"], 30000)
        with self.assertRaises(BridgeError): translate_request(prompt(model="claude-fable-5[1m]"), settings)
        with self.assertRaises(BridgeError): translate_request(prompt(speed="fast"), settings)

    def test_missing_metadata_does_not_guess_context(self):
        settings = config(); settings.pop("_model_specs")
        self.assertEqual(model_catalog(settings)["data"], [])
        with self.assertRaises(BridgeError): translate_request(prompt(), settings)

    def test_nonstream_response_tools_and_usage(self):
        response = {"choices": [{"message": {"content": "", "tool_calls": [{"id": "123456789", "function": {"name": "Read", "arguments": '{"path":"a"}'}}]}, "finish_reason": "tool_calls"}], "usage": {"prompt_tokens": 10, "completion_tokens": 3}}
        result = translate_response(response, "claude-fable-5", {"Read": "Read"})
        self.assertEqual(result["stop_reason"], "tool_use")
        self.assertEqual(result["content"][0]["input"], {"path": "a"})
        self.assertEqual(result["usage"], {"input_tokens": 10, "output_tokens": 3})

    def test_stream_text_and_interleaved_partial_arguments(self):
        translator = StreamTranslator("claude-fable-5", {"fn_one": "fn.one", "fn_two": "fn.two"})
        events = translator.start()
        events += translator.feed({"choices": [{"delta": {"content": "First "}}]})
        events += translator.feed({"choices": [{"delta": {"content": "read."}}]})
        for idx, name in [(0, "fn_one"), (1, "fn_two")]:
            events += translator.feed({"choices": [{"delta": {"tool_calls": [{"index": idx, "id": f"call{idx}", "function": {"name": name, "arguments": '{"path":'}}]}}]})
        for idx in (1, 0):
            events += translator.feed({"choices": [{"delta": {"tool_calls": [{"index": idx, "function": {"arguments": f'"{idx}.txt"}}'}}]}}]})
        events += translator.feed({"choices": [{"delta": {}, "finish_reason": "tool_calls"}], "usage": {"prompt_tokens": 24, "completion_tokens": 20}})
        events += translator.end()
        starts = [e for e in events if e["type"] == "content_block_start"]
        self.assertEqual([s["content_block"]["type"] for s in starts], ["text", "tool_use", "tool_use"])
        self.assertEqual(starts[1]["content_block"]["name"], "fn.one")
        for start in starts[1:]:
            fragments = [e["delta"]["partial_json"] for e in events if e["type"] == "content_block_delta" and e["index"] == start["index"]]
            self.assertIn("path", json.loads("".join(fragments)))
        self.assertEqual(events[-2]["delta"]["stop_reason"], "tool_use")
        self.assertEqual(events[-1]["type"], "message_stop")

    def test_late_tool_metadata_and_truncated_stream(self):
        translator = StreamTranslator("x", {})
        self.assertEqual(translator.feed({"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": '{"x":1}'}}]}}]}), [])
        events = translator.feed({"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "abc", "function": {"name": "Read"}}]}, "finish_reason": "tool_calls"}]})
        self.assertEqual(events[0]["type"], "content_block_start")
        self.assertEqual(events[1]["delta"]["partial_json"], '{"x":1}')
        self.assertEqual(translator.end()[-1]["type"], "message_stop")
        with self.assertRaises(BridgeError): StreamTranslator("x", {}).end()

    def test_invalid_arguments_cannot_complete_stream(self):
        translator = StreamTranslator("x", {})
        translator.feed({"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "abc", "function": {"name": "Read", "arguments": "{"}}]}, "finish_reason": "tool_calls"}]})
        with self.assertRaises(BridgeError): translator.end()


class ProfileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.profile = ClaudeProfile(self.base / "bridge", self.base / "support")
        self.previous = {"appliedId": "ollama-profile", "entries": [{"id": "ollama-profile", "name": "Ollama"}], "custom": 7}
        atomic_json(self.profile.meta, self.previous)
        atomic_json(self.profile.normal, {"deploymentMode": "1p", "mcpServers": {"keep": {}}, "unrelated": "retained"})
        atomic_json(self.profile.third_party, {"deploymentMode": "3p", "preferences": {"test": 1}})
        self.session = self.base / "support/Claude-3p/claude-code-sessions/session.json"
        self.session.parent.mkdir(parents=True)
        self.session.write_text('{"conversation":"leave exactly alone"}')

    def tearDown(self): self.tmp.cleanup()

    def test_activate_restore_preserves_sessions_and_previous_profile(self):
        before = self.session.read_bytes()
        self.profile.activate(config(), "local-token", require_closed=False)
        self.assertTrue(self.profile.active())
        self.assertEqual(read_json(self.profile.normal)["mcpServers"], {"keep": {}})
        self.assertEqual(read_json(self.profile.profile)["inferenceCredentialKind"], "static")
        self.assertIs(read_json(self.profile.profile)["modelPrefer1mContext"], True)
        result = self.profile.restore(require_closed=False)
        self.assertTrue(result["restored"])
        self.assertEqual(read_json(self.profile.meta), self.previous)
        self.assertEqual(read_json(self.profile.normal)["deploymentMode"], "1p")
        self.assertEqual(read_json(self.profile.third_party)["deploymentMode"], "3p")
        self.assertEqual(self.session.read_bytes(), before)
        self.assertFalse(self.profile.profile.exists())
        self.assertFalse(self.profile.journal.exists())

    def test_restore_preserves_unrelated_concurrent_edits(self):
        self.profile.activate(config(), "local-token", require_closed=False)
        normal = read_json(self.profile.normal); normal["unrelated"] = "new value"; atomic_json(self.profile.normal, normal)
        meta = read_json(self.profile.meta); meta["entries"].append({"id": "another", "name": "New profile"}); atomic_json(self.profile.meta, meta)
        self.profile.restore(require_closed=False)
        self.assertEqual(read_json(self.profile.normal)["unrelated"], "new value")
        self.assertEqual(read_json(self.profile.meta)["appliedId"], "ollama-profile")
        self.assertEqual([e["id"] for e in read_json(self.profile.meta)["entries"]], ["ollama-profile", "another"])

    def test_external_profile_switch_wins(self):
        self.profile.activate(config(), "local-token", require_closed=False)
        meta = read_json(self.profile.meta); meta["appliedId"] = "different-profile"; atomic_json(self.profile.meta, meta)
        result = self.profile.restore(require_closed=False)
        self.assertGreater(result["preserved_external_changes"], 0)
        self.assertEqual(read_json(self.profile.meta)["appliedId"], "different-profile")
        self.assertNotIn(PROFILE_ID, [e["id"] for e in read_json(self.profile.meta)["entries"]])

    def test_running_claude_is_never_switched(self):
        with patch("bridge_core.claude_running", return_value=True):
            with self.assertRaises(BridgeError): self.profile.activate(config(), "token")
        self.assertFalse(self.profile.journal.exists())
        self.assertEqual(read_json(self.profile.meta), self.previous)

    def test_interrupted_write_rolls_back(self):
        import bridge_core
        original = bridge_core.atomic_json
        calls = [0]
        def fail_once(path, value):
            calls[0] += 1
            if calls[0] == 4: raise OSError("Injected interrupted write")
            return original(path, value)
        with patch("bridge_core.atomic_json", side_effect=fail_once):
            with self.assertRaises(OSError): self.profile.activate(config(), "token", require_closed=False)
        self.assertEqual(read_json(self.profile.meta), self.previous)
        self.assertEqual(read_json(self.profile.normal)["deploymentMode"], "1p")
        self.assertFalse(self.profile.profile.exists())

    def test_symlink_and_invalid_journal_are_rejected(self):
        self.profile.profile.symlink_to(self.session)
        with self.assertRaises(BridgeError): self.profile.activate(config(), "token", require_closed=False)
        self.assertEqual(self.session.read_text(), '{"conversation":"leave exactly alone"}')
        self.profile.profile.unlink()
        atomic_json(self.profile.journal, {"version": 1, "operations": [{"path": str(self.session)}]})
        with self.assertRaises(BridgeError): self.profile.restore(require_closed=False)


class CatalogueTests(unittest.TestCase):
    def test_aliases_grouped_but_context_variants_remain_distinct(self):
        def card(identifier, canonical, context, **extra):
            return {"id": identifier, "name": canonical, "max_context_length": context,
                    "capabilities": {"completion_chat": True, "function_calling": True, "reasoning": True}, **extra}
        raw = {"data": [card("model-v1", "model", 262144), card("model-latest", "model", 262144),
                        card("model-1m", "model", 1048576), card("old", "old", 1000, deprecation="2020-01-01")]}
        settings = config(); settings["mappings"] = {key: "model-latest" for key in settings["mappings"]}
        result = build_catalogue(raw, settings, {})
        self.assertEqual(len(result["models"]), 2)
        self.assertEqual(result["retired_ids"], 1)
        specs = route_specs(result)
        self.assertIs(specs["model-v1"], specs["model-latest"])
        self.assertEqual(specs["model-latest"]["id"], "model-latest")
        self.assertEqual(specs["model-1m"]["context"], 1048576)
        self.assertNotEqual(specs["model-1m"]["display_name"], specs["model-v1"]["display_name"])

    def test_quota_failure_does_not_mean_model_retirement(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            events = [{"event": "completed", "model": "model", "time": "a"},
                      {"event": "error", "model": "model", "status": 429, "time": "b"}]
            (root / "activity.jsonl").write_text("\n".join(json.dumps(event) for event in events))
            observed = read_observations(root)
            self.assertEqual(observed["model"]["status"], "quota_limited")
            self.assertEqual(observed["model"]["last_success"], "a")


class MockMistral(BaseHTTPRequestHandler):
    mode = "normal"
    requests = []
    protocol_version = "HTTP/1.1"
    def log_message(self, *_): pass
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).requests.append(body)
        if self.mode == "rate_limit":
            raw = b'{"message":"Test quota exceeded"}'
            self.send_response(429); self.send_header("Content-Length", str(len(raw))); self.end_headers(); self.wfile.write(raw); return
        if not body["stream"]:
            raw = json.dumps({"choices": [{"message": {"content": "Connected", "tool_calls": None}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 5, "completion_tokens": 2}}).encode()
            self.send_response(200); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(raw))); self.end_headers(); self.wfile.write(raw); return
        self.send_response(200); self.send_header("Content-Type", "text/event-stream"); self.send_header("Connection", "close"); self.end_headers()
        self.wfile.write(b'data: {"choices":[{"delta":{"content":"Hello"}}]}\n\n'); self.wfile.flush()
        if self.mode == "slow":
            time.sleep(2)
        if self.mode != "truncated":
            try:
                self.wfile.write(b'data: {"choices":[{"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":5,"completion_tokens":2}}\n\ndata: [DONE]\n\n'); self.wfile.flush()
            except OSError: pass
        self.close_connection = True


class GatewayTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        atomic_json(self.root / "settings.json", config())
        atomic_json(self.root / "catalog.json", {"schema_version": 2, "models": list(config()["_model_specs"].values())})
        MockMistral.mode = "normal"; MockMistral.requests = []
        self.upstream = ThreadingHTTPServer(("127.0.0.1", 0), MockMistral)
        self.upstream.daemon_threads = True
        threading.Thread(target=self.upstream.serve_forever, daemon=True).start()
        self.runtime = Runtime(self.root, f"http://127.0.0.1:{self.upstream.server_port}/v1", key="TEST-SECRET-NEVER-LOG")
        self.server = Server(self.runtime, 0)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.upstream.shutdown(); self.upstream.server_close(); self.tmp.cleanup()

    def request(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=4)
        hs = {"Authorization": "Bearer " + self.runtime.token, "Content-Type": "application/json"}
        hs.update(headers or {})
        connection.request(method, path, json.dumps(body) if body is not None else None, hs)
        response = connection.getresponse(); data = response.read(); status = response.status; info = dict(response.getheaders()); connection.close()
        deadline = time.monotonic() + 1
        while self.runtime.status()["active"] and time.monotonic() < deadline: time.sleep(.01)
        return status, data, info

    def test_auth_origin_and_host_rejections(self):
        self.assertEqual(self.request("GET", "/v1/models", headers={"Authorization": "Bearer bad"})[0], 401)
        self.assertEqual(self.request("GET", "/v1/models", headers={"Origin": "https://untrusted.example"})[0], 403)
        self.assertEqual(self.request("GET", "/v1/models", headers={"Host": "untrusted.example"})[0], 403)
        self.assertEqual(self.request("GET", "/v1/models")[0], 200)

    def test_text_roundtrip_and_private_logs(self):
        status, data, _ = self.request("POST", "/v1/messages", prompt())
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(data)["content"][0]["text"], "Connected")
        self.assertEqual(MockMistral.requests[0]["model"], "test-model")
        self.assertEqual(self.runtime.completed, 1)
        log = (self.root / "activity.jsonl").read_text()
        for secret in ("TEST-SECRET-NEVER-LOG", "hello", "Connected"):
            self.assertNotIn(secret, log)

    def test_stream_is_valid_anthropic_sse(self):
        status, data, headers = self.request("POST", "/v1/messages", prompt(stream=True))
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "text/event-stream")
        events = [json.loads(line[6:]) for line in data.decode().splitlines() if line.startswith("data: ")]
        self.assertEqual(events[0]["type"], "message_start")
        self.assertEqual(events[-1]["type"], "message_stop")
        self.assertEqual(events[-2]["usage"]["output_tokens"], 2)

    def test_truncation_is_an_error_not_success(self):
        MockMistral.mode = "truncated"
        status, data, _ = self.request("POST", "/v1/messages", prompt(stream=True))
        self.assertEqual(status, 200)
        self.assertIn(b'"type": "error"', data)
        self.assertNotIn(b'"type": "message_stop"', data)
        self.assertEqual(self.runtime.failed, 1)

    def test_rate_limit_and_token_estimate(self):
        MockMistral.mode = "rate_limit"
        status, data, _ = self.request("POST", "/v1/messages", prompt())
        self.assertEqual(status, 429)
        self.assertEqual(json.loads(data)["error"]["type"], "rate_limit_error")
        status, data, headers = self.request("POST", "/v1/messages/count_tokens", prompt())
        self.assertEqual(status, 200)
        self.assertEqual(headers["X-Mistral-Bridge-Token-Count"], "estimate")
        self.assertGreater(json.loads(data)["input_tokens"], 0)

    def test_client_cancellation_clears_active_request(self):
        MockMistral.mode = "slow"
        connection = socket.create_connection(("127.0.0.1", self.server.server_port), timeout=3)
        data = json.dumps(prompt(stream=True)).encode()
        request = f"POST /v1/messages HTTP/1.1\r\nHost: 127.0.0.1:{self.server.server_port}\r\nAuthorization: Bearer {self.runtime.token}\r\nContent-Type: application/json\r\nContent-Length: {len(data)}\r\n\r\n".encode() + data
        connection.sendall(request)
        connection.recv(4096)
        connection.shutdown(socket.SHUT_RDWR); connection.close()
        deadline = time.monotonic() + 1.5
        while self.runtime.active and time.monotonic() < deadline: time.sleep(.05)
        self.assertEqual(self.runtime.active, 0)
        self.assertEqual(self.runtime.completed, 0)


if __name__ == "__main__": unittest.main(verbosity=2)
