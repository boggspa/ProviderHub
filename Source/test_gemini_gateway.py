"""Complete Gemini HTTP tool cycles through Claude and Codex gateway endpoints."""
import copy
import http.client
import json
import time
import unittest
from unittest.mock import patch

import test_gateway_hub as fixtures
from gemini_provider import ENVELOPE_PREFIX, seal_replay, discover as discover_gemini
from codex_catalogue import project_codex
from hub_config import defaults, project_catalogue
from bridge_core import SLOTS


def model_spec():
    return {"context": 1048576, "max_input": 1048576, "max_output": 65536,
            "reasoning": True, "vision": True, "base_model_id": "gemini-3.8-flash",
            "effort_modes": ["low", "medium", "high"], "provider_effort_modes": ["low", "medium", "high"],
            "reasoning_history": "gateway_signed_replay", "version": "001"}


def handle_gemini(server):
    body = server.read_body()
    if fixtures.MockProvider.mode == "rate_limit":
        server.send_json(429, {"error": {"message": "quota " + fixtures.PROVIDER_KEY}})
        return
    second = any(message.get("role") == "tool" for message in body["messages"])
    signature = {} if fixtures.MockProvider.mode == "unsigned" else {"google": {"thought_signature": "opaque-google-tool-state"}}
    if second:
        message = {"role": "assistant", "content": "Gemini cycle complete.", "extra_content": {"google": {"thought_signature": "opaque-google-text-state"}}}
    else:
        name = body["tools"][0]["function"]["name"]
        message = {"role": "assistant", "content": None, "tool_calls": [{"id": "gemini-call", "type": "function",
            "function": {"name": name, "arguments": '{"path":"fixture.txt"}'}, "extra_content": signature}]}
    usage = {"prompt_tokens": 10, "completion_tokens": 6, "total_tokens": 21}
    if not body.get("stream"):
        server.send_json(200, {"choices": [{"message": message, "finish_reason": "stop"}], "usage": usage})
        return
    if second:
        chunks = [{"choices": [{"index": 0, "delta": {"content": message["content"]}, "finish_reason": None}]},
                  {"choices": [{"index": 0, "delta": {"content": "", "extra_content": message["extra_content"]}, "finish_reason": "stop"}]}]
    else:
        call = message["tool_calls"][0]
        chunks = [{"choices": [{"index": 0, "delta": {"tool_calls": [{**call, "index": 0,
                              "function": {"name": call["function"]["name"], "arguments": '{"path":'}}]}, "finish_reason": None}]},
                  {"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "function": {"arguments": '"fixture.txt"}'}}]}, "finish_reason": "stop"}]}]
    chunks.append({"choices": [], "usage": usage})
    server.send_sse(chunks, chat=True)


class GeminiGatewayTests(unittest.TestCase):
    def setUp(self):
        fixtures.GatewayHubHTTPTests.setUp(self)
        self.gemini_patch = patch.object(fixtures.MockProvider, "do_POST", handle_gemini)
        self.gemini_patch.start()

    def tearDown(self):
        fixtures.GatewayHubHTTPTests.tearDown(self)
        self.gemini_patch.stop()

    start_gateway = fixtures.GatewayHubHTTPTests.start_gateway
    request = fixtures.GatewayHubHTTPTests.request

    def assert_completed(self, expected):
        # The HTTP terminator is flushed before calibration/accounting runs.
        # Receiving it is not a barrier for the server thread's finalizer.
        deadline = time.monotonic() + 2
        while self.runtime.status()["completed"] < expected and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertEqual(self.runtime.status()["completed"], expected)

    def claude_cycle(self, stream):
        self.start_gateway("gemini", "gemini-3.8-flash", model_spec())
        body = {"model": "claude-fable-5", "max_tokens": 512, "stream": stream,
                "messages": [{"role": "user", "content": fixtures.LOCAL_REQUEST_TEXT}],
                "tools": [fixtures.tool_definition()], "thinking": {"type": "adaptive"}, "output_config": {"effort": "max"}}
        status, raw, _ = self.request(body)
        self.assertEqual(status, 200, raw)
        first = fixtures.reconstruct_content(fixtures.parse_sse(raw)) if stream else json.loads(raw)["content"]
        self.assertEqual(first[0]["type"], "redacted_thinking")
        self.assertTrue(first[0]["data"].startswith(ENVELOPE_PREFIX))
        call = next(block for block in first if block["type"] == "tool_use")
        body["messages"] += [{"role": "assistant", "content": first}, {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": call["id"], "content": "green"}]}]
        status, raw, _ = self.request(body)
        self.assertEqual(status, 200, raw)
        final = fixtures.reconstruct_content(fixtures.parse_sse(raw)) if stream else json.loads(raw)["content"]
        self.assertEqual(final[-1]["text"], "Gemini cycle complete.")
        body["messages"] += [{"role": "assistant", "content": final}, {"role": "user", "content": "continue"}]
        status, raw, _ = self.request(body)
        self.assertEqual(status, 200, raw)
        calls = fixtures.MockProvider.requests
        assistant = next(row for row in calls[1]["messages"] if row["role"] == "assistant")
        self.assertEqual(assistant["tool_calls"][0]["extra_content"]["google"]["thought_signature"], "opaque-google-tool-state")
        last_assistant = [row for row in calls[2]["messages"] if row["role"] == "assistant"][-1]
        self.assertEqual(last_assistant["extra_content"]["google"]["thought_signature"], "opaque-google-text-state")
        self.assert_completed(3)
        for headers in fixtures.MockProvider.request_headers:
            self.assertEqual(headers["authorization"], "Bearer " + fixtures.PROVIDER_KEY)
            self.assertIn("x-goog-api-client", headers)
            self.assertNotIn(self.runtime.token, json.dumps(headers))
        self.assertNotIn(fixtures.LOCAL_REQUEST_TEXT, (self.root / "activity.jsonl").read_text())

    def test_claude_json_tool_and_text_continuations(self):
        self.claude_cycle(False)

    def test_claude_streaming_preserves_late_signatures(self):
        self.claude_cycle(True)

    def test_codex_json_and_streaming_namespace_tool_cycles(self):
        route = self.start_gateway("gemini", "gemini-3.8-flash", model_spec())
        def request(body):
            client = http.client.HTTPConnection("127.0.0.1", self.gateway.server_port, timeout=6)
            client.request("POST", "/v1/responses", json.dumps(body), {"Content-Type": "application/json", "Authorization": "Bearer " + self.runtime.token})
            response = client.getresponse()
            status, raw = response.status, response.read()
            client.close()
            return status, raw
        for stream in (False, True):
            body = {"model": route, "input": [{"role": "user", "content": "Read the file"}], "store": False, "stream": stream,
                    "reasoning": {"effort": "high"}, "tools": [{"type": "namespace", "name": "functions", "tools": [
                        {"type": "function", "name": "read_file", "parameters": {"type": "object"}}]}]}
            status, raw = request(body)
            self.assertEqual(status, 200, raw)
            first = fixtures.parse_sse(raw)[-1]["response"] if stream else json.loads(raw)
            thought = next(row for row in first["output"] if row["type"] == "reasoning")
            self.assertNotIn("opaque-google", thought["encrypted_content"])
            call = next(row for row in first["output"] if row["type"] == "function_call")
            self.assertEqual((call["namespace"], call["name"]), ("functions", "read_file"))
            body["input"] += first["output"] + [{"type": "function_call_output", "call_id": call["call_id"], "output": "green"}]
            status, raw = request(body)
            self.assertEqual(status, 200, raw)
            last = fixtures.parse_sse(raw)[-1]["response"] if stream else json.loads(raw)
            self.assertEqual(last["status"], "completed")
        self.assert_completed(4)

    def test_missing_signature_and_quota_errors_are_explicit_and_private(self):
        self.start_gateway("gemini", "gemini-3.8-flash", model_spec())
        body = {"model": "claude-sonnet-5", "max_tokens": 512, "messages": [{"role": "user", "content": "read"}], "tools": [fixtures.tool_definition()]}
        fixtures.MockProvider.mode = "unsigned"
        status, raw, _ = self.request(body)
        self.assertEqual(status, 502)
        self.assertIn(b"thought signature", raw)
        fixtures.MockProvider.mode = "rate_limit"
        # The gateway absorbs persistent pressure with backoff before
        # surfacing 429; patch the delays so the round-trip fits the
        # harness timeout. The asserted mapping is unchanged.
        with patch("rate_limit.BACKOFF_BASE", 0.01), \
                patch("rate_limit.BACKOFF_CAP", 0.05), \
                patch("rate_limit.random.uniform", return_value=0):
            status, raw, _ = self.request(body)
        self.assertEqual(status, 429)
        self.assertNotIn(fixtures.PROVIDER_KEY.encode(), raw)
        self.assertEqual(self.runtime.status()["completed"], 0)

    def test_independent_output_limit_and_snapshot_identity(self):
        self.start_gateway("gemini", "gemini-3.8-flash", {**model_spec(), "context": 1024, "max_input": 1024, "max_output": 8192})
        status, raw, _ = self.request({"model": "claude-sonnet-5", "max_tokens": 4096,
                                      "messages": [{"role": "user", "content": "read"}], "tools": [fixtures.tool_definition()]})
        self.assertEqual(status, 200, raw)
        self.assertEqual(fixtures.MockProvider.requests[0]["max_tokens"], 4096)
        first = {"id": "snapshot-a", "canonical_id": "gemini-3.8-flash", "aliases": ["snapshot-a"], **model_spec()}
        second = {**copy.deepcopy(first), "id": "snapshot-b", "aliases": ["snapshot-b"], "version": "002"}
        rows = project_catalogue("gemini", {"provider_id": "gemini", "models": [first, second]}, defaults(SLOTS, "unused"))
        self.assertEqual(len(rows), 2)

    def test_opaque_signature_bytes_do_not_exhaust_a_text_context_estimate(self):
        self.start_gateway("gemini", "gemini-3.8-flash", {**model_spec(), "context": 1024, "max_input": 1024})
        body = {"model": "claude-sonnet-5", "max_tokens": 512,
                "messages": [{"role": "user", "content": "hello"}], "tools": [fixtures.tool_definition()]}
        plan = self.runtime.plan(body)
        visible = [{"type": "text", "text": "previous answer"}]
        envelope = seal_replay("A" * 20000, [], visible, "gemini-3.8-flash", plan["replay_scope"], self.runtime.replay_key)
        body["messages"] += [{"role": "assistant", "content": [envelope] + visible}, {"role": "user", "content": "continue"}]
        status, raw, _ = self.request(body)
        self.assertEqual(status, 200, raw)
        assistant = next(row for row in fixtures.MockProvider.requests[0]["messages"] if row["role"] == "assistant")
        self.assertEqual(assistant["extra_content"]["google"]["thought_signature"], "A" * 20000)

    def test_documented_gemini_default_effort_reaches_codex(self):
        expected = {
            "gemini-3.8-flash": "medium", "gemini-3.7-flash": "medium",
            "gemini-3.6-flash": "medium", "gemini-3.5-flash": "medium",
            "gemini-3.5-flash-lite": "minimal", "gemini-3.1-flash-lite": "minimal",
            "gemini-3.1-pro-preview": "high", "gemini-3-flash-preview": "high",
            "gemini-2.5-flash-lite": "none", "gemini-2.5-pro": None,
            "gemini-2.5-flash": None,
        }
        inventory = discover_gemini({}, "key", transport=lambda _: {"models": [{
            "name": "models/" + model, "supportedGenerationMethods": ["generateContent"],
            "inputTokenLimit": 1048576, "outputTokenLimit": 65536, "thinking": True,
        } for model in expected]})
        settings = defaults(SLOTS, "unused")
        models = project_codex(settings, {"models": project_catalogue("gemini", inventory, settings)})["models"]
        self.assertEqual({row["slug"].removeprefix("gemini/"): row["default_reasoning_level"] for row in models}, expected)


if __name__ == "__main__":
    unittest.main()
