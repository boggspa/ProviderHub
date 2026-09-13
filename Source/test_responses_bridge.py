"""Translated Responses across all six existing Messages/Chat adapters."""
import copy
import http.client
import json
from pathlib import Path
import tempfile
import unittest

import test_gateway_hub as fixtures
from responses_bridge import ReasoningEnvelope, response_usage, to_messages
from bridge_core import BridgeError
from providers import ProviderError, prepare_request


MODELS = {
    "mistral": ("mistral-medium-latest", ["none", "high"]),
    "kimi": ("kimi-for-coding", ["low", "high", "max"]),
    "mimo": ("mimo-v2.5-pro", ["none", "high"]),
    "deepseek": ("deepseek-flash", ["none", "low", "high", "max"]),
    "muse": ("muse-spark-1.3", ["minimal", "low", "medium", "high", "xhigh", "max"]),
    "cerebras": ("gpt-oss-120b", ["low", "medium", "high"]),
}


class ResponsesBridgeTests(unittest.TestCase):
    def exercise(self, provider, stream):
        fixture = fixtures.GatewayHubHTTPTests()
        fixture.setUp()
        try:
            model, efforts = MODELS[provider]
            route = fixture.start_gateway(provider, model, {"context": 1048576, "effort_modes": efforts,
                          "reasoning_history": "gateway_signed_replay" if provider == "cerebras" else "native"})

            def request(body):
                client = http.client.HTTPConnection("127.0.0.1", fixture.gateway.server_port, timeout=8)
                client.request("POST", "/v1/responses", json.dumps(body), {
                    "Authorization": "Bearer " + fixture.runtime.token, "Content-Type": "application/json"})
                response = client.getresponse()
                status, raw = response.status, response.read()
                client.close()
                return status, raw

            body = {"model": route, "input": [{"role": "user", "content": fixtures.LOCAL_REQUEST_TEXT}],
                    "max_output_tokens": 512, "reasoning": {"effort": "high"}, "stream": stream, "store": False,
                    "tools": [{"type": "function", "name": "read_file", "parameters": {"type": "object"}}]}
            status, raw = request(body)
            self.assertEqual(status, 200, raw)
            if stream:
                events = fixtures.parse_sse(raw)
                self.assertEqual(events[-1]["type"], "response.completed", raw)
                first = events[-1]["response"]
            else:
                first = json.loads(raw)
            calls = [item for item in first["output"] if item["type"] == "function_call"]
            self.assertTrue(calls)
            if provider != "mistral":
                thinking = next(item for item in first["output"] if item["type"] == "reasoning")
                self.assertTrue(thinking["encrypted_content"].startswith("ph_reasoning_v1."))
                self.assertNotIn("native-provider-signature", thinking["encrypted_content"])
            body["input"] += first["output"] + [{"type": "function_call_output", "call_id": call["call_id"], "output": "green"} for call in calls]
            status, raw = request(body)
            self.assertEqual(status, 200, raw)
            final = fixtures.parse_sse(raw)[-1]["response"] if stream else json.loads(raw)
            self.assertEqual(final["status"], "completed", raw)
            self.assertTrue(any(item["type"] == "message" for item in final["output"]))
            self.assertEqual(len(fixtures.MockProvider.requests), 2)
            # Only inner provider calls are counted; the local translation hop
            # must not double-count requests or consume another concurrency slot.
            self.assertEqual(fixture.runtime.status()["completed"], 2)
            self.assertNotIn(fixtures.LOCAL_REQUEST_TEXT, (fixture.root / "activity.jsonl").read_text())
            if provider == "cerebras":
                assistant = next(message for message in fixtures.MockProvider.requests[1]["messages"] if message["role"] == "assistant")
                self.assertTrue(assistant.get("reasoning"))
            elif provider != "mistral":
                assistant = fixtures.MockProvider.requests[1]["messages"][1]
                self.assertTrue(any(block["type"] == "thinking" for block in assistant["content"]))
            for headers in fixtures.MockProvider.request_headers:
                self.assertNotIn(fixture.runtime.token, json.dumps(headers))
        finally:
            fixture.tearDown()

    def test_all_six_json_tool_cycles(self):
        for provider in MODELS:
            with self.subTest(provider=provider):
                self.exercise(provider, False)

    def test_all_six_streaming_tool_cycles(self):
        for provider in MODELS:
            with self.subTest(provider=provider):
                self.exercise(provider, True)

    def test_encrypted_reasoning_survives_restart_and_rejects_tampering_or_other_accounts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = ReasoningEnvelope(root)
            blocks = [{"type": "thinking", "thinking": "PRIVATE THINKING", "signature": "provider-signature"}]
            token = first.seal(blocks, "account-one")
            self.assertNotIn("PRIVATE THINKING", token)
            self.assertEqual(ReasoningEnvelope(root).open(token, "account-one"), blocks)
            with self.assertRaises(BridgeError): first.open(token, "account-two")
            with self.assertRaises(BridgeError): first.open(token[:-3] + "abc", "account-one")
            with self.assertRaises(BridgeError): first.open("foreign-token", "account-one")
            self.assertNotIn("PRIVATE THINKING", ''.join(path.read_text() for path in root.iterdir() if path.is_file()))

    def test_unknown_context_does_not_prevent_request_translation(self):
        with tempfile.TemporaryDirectory() as directory:
            envelope = ReasoningEnvelope(Path(directory))
            body = {"input": "hello", "stream": False, "store": False, "tools": []}
            translated = to_messages(body, "deepseek/deepseek-flash", {"context": None}, envelope, "scope")
            self.assertEqual(translated["messages"], [{"role": "user", "content": [{"type": "text", "text": "hello"}]}])
            self.assertEqual(translated["max_tokens"], 16384)

    def test_chatgpt_effort_slider_and_fast_reach_translated_messages(self):
        with tempfile.TemporaryDirectory() as directory:
            envelope = ReasoningEnvelope(Path(directory))
            body = {"input": "hello", "stream": False, "store": False, "tools": [],
                    "reasoning": {"effort": "xhigh"}, "service_tier": "fast"}
            translated = to_messages(
                body, "kimi/k3", {"context": 131072, "max_output": 4096}, envelope, "scope")
            self.assertEqual(translated["output_config"], {"effort": "xhigh"})
            self.assertEqual(translated["service_tier"], "fast")
            self.assertNotIn("thinking", translated)
            body["reasoning"] = {"effort": "none"}
            disabled = to_messages(
                body, "kimi/k3", {"context": 131072, "max_output": 4096}, envelope, "scope")
            self.assertEqual(disabled["thinking"], {"type": "disabled"})

    def test_muse_slider_xhigh_and_max_reach_native_messages(self):
        spec = {
            "id": "muse-spark-1.3", "context": 1048576, "max_output": 131072,
            "reasoning": True, "effort_modes": MODELS["muse"][1], "fast_mode": False,
        }
        with tempfile.TemporaryDirectory() as directory:
            envelope = ReasoningEnvelope(Path(directory))
            for requested in ("xhigh", "max"):
                with self.subTest(requested=requested):
                    translated = to_messages(
                        {"input": "hello", "stream": False, "store": False, "tools": [],
                         "reasoning": {"effort": requested}},
                        "muse/muse-spark-1.3", spec, envelope, "scope",
                    )
                    self.assertEqual(translated["output_config"]["effort"], requested)
                    plan = prepare_request(
                        "muse", {}, "key", translated, "muse-spark-1.3", spec,
                    )
                    self.assertEqual(plan["body"]["output_config"]["effort"], requested)
            omitted = {**spec, "effort_modes": ["minimal", "low", "medium", "high"]}
            translated = to_messages(
                {"input": "hello", "stream": False, "store": False, "tools": [],
                 "reasoning": {"effort": "max"}},
                "muse/muse-spark-1.3", omitted, envelope, "scope",
            )
            with self.assertRaisesRegex(ProviderError, "does not support"):
                prepare_request("muse", {}, "key", translated, "muse-spark-1.3", omitted)

    def test_anthropic_cache_usage_is_included_in_codex_context_accounting(self):
        self.assertEqual(response_usage({"input_tokens": 10, "cache_creation_input_tokens": 20,
                                         "cache_read_input_tokens": 30, "output_tokens": 4}),
                         {"input_tokens": 60, "output_tokens": 4, "total_tokens": 64,
                          "input_tokens_details": {"cached_tokens": 30}})


if __name__ == "__main__":
    unittest.main()
