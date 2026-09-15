"""Translated Responses across all six existing Messages/Chat adapters."""
import copy
import http.client
import json
from pathlib import Path
import tempfile
import unittest

import test_gateway_hub as fixtures
from responses_bridge import MessagesResponsesAdapter, ReasoningEnvelope, response_usage, to_messages
from bridge_core import BridgeError
from providers import ProviderError, prepare_request


MODELS = {
    "mistral": ("mistral-medium-latest", ["none", "low", "medium", "high", "max"]),
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

    def test_empty_history_items_do_not_emit_empty_text_blocks(self):
        # A restarted Codex thread replays tool calls whose output was empty as
        # function_call_output items with output:"". Anthropic-compatible
        # providers (Kimi) reject {"type":"text","text":""} with HTTP 400
        # "text content is empty", which permanently wedges the parent thread.
        with tempfile.TemporaryDirectory() as directory:
            envelope = ReasoningEnvelope(Path(directory))
            body = {"input": [
                {"type": "message", "role": "user", "content": "start"},
                {"type": "function_call", "call_id": "tool_1", "name": "exec", "arguments": "{}"},
                {"type": "function_call_output", "call_id": "tool_1", "output": ""},
                {"type": "function_call", "call_id": "tool_2", "name": "exec", "arguments": "{}"},
                {"type": "function_call_output", "call_id": "tool_2", "output": [{"type": "output_text", "text": ""}]},
                {"type": "message", "role": "assistant", "content": ""},
                {"type": "message", "role": "user", "content": [
                    {"type": "input_text", "text": ""},
                    {"type": "input_text", "text": "kept"},
                ]},
            ], "stream": False, "store": False, "tools": []}
            translated = to_messages(body, "kimi/k3", {"context": 131072}, envelope, "scope")
            messages = translated["messages"]
            results = [block for message in messages for block in message["content"]
                       if block["type"] == "tool_result"]
            self.assertEqual(len(results), 2)
            self.assertTrue(all(result["content"] == "" for result in results))
            self.assertEqual(
                [block["text"] for message in messages for block in message["content"]
                 if block["type"] == "text"],
                ["start", "kept"])
            self.assertEqual([message["role"] for message in messages],
                             ["user", "assistant", "user", "assistant", "user"])
            self.assertTrue(all(message["content"] for message in messages))
            with self.assertRaisesRegex(BridgeError, "no content"):
                to_messages({"input": "", "stream": False, "store": False, "tools": []},
                            "kimi/k3", {"context": 131072}, envelope, "scope")

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


class CustomApplyPatchBridgeTests(unittest.TestCase):
    PATCH = "*** Begin Patch\n*** Update File: a.txt\n@@\n-old\n+new\n*** End Patch"
    TOOL_MAP = {"apply_patch": {"namespace": None, "name": "apply_patch", "custom": "apply_patch"}}

    def test_custom_history_translates_to_patch_tool_blocks(self):
        from responses_bridge import to_messages
        body = {"input": [
            {"type": "message", "role": "user", "content": "edit it"},
            {"type": "custom_tool_call", "call_id": "call-1", "name": "apply_patch", "input": self.PATCH},
            {"type": "custom_tool_call_output", "call_id": "call-1", "output": "applied"},
        ], "stream": False, "store": False, "tools": []}
        translated = to_messages(body, "mistral/mistral-medium-latest", {"context": 131072}, None, "scope")
        messages = translated["messages"]
        use = next(block for message in messages if message["role"] == "assistant"
                   for block in message["content"] if block["type"] == "tool_use")
        self.assertEqual(use["input"], {"patch": self.PATCH})
        result = next(block for message in messages if message["role"] == "user"
                      for block in message["content"] if block["type"] == "tool_result")
        self.assertEqual(result["tool_use_id"], "call-1")
        with self.assertRaises(BridgeError):
            to_messages({"input": [{"type": "custom_tool_call", "name": "other_tool"}],
                         "stream": False, "store": False, "tools": []},
                        "mistral/mistral-medium-latest", {"context": 131072}, None, "scope")

    def test_mapped_tool_use_returns_custom_tool_call(self):
        from responses_bridge import MessagesResponsesAdapter
        adapter = MessagesResponsesAdapter("mistral/mistral-medium-latest", None, "scope", dict(self.TOOL_MAP))
        item = adapter.item({"type": "tool_use", "id": "call-1", "name": "apply_patch",
                             "input": {"patch": self.PATCH}})
        self.assertEqual(item["type"], "custom_tool_call")
        self.assertEqual(item["input"], self.PATCH)
        self.assertEqual(item["name"], "apply_patch")
        plain = MessagesResponsesAdapter("mistral/mistral-medium-latest", None, "scope",
                                         dict(self.TOOL_MAP)).item(
            {"type": "tool_use", "id": "call-2", "name": "read_file", "input": {"path": "a"}})
        self.assertEqual(plain["type"], "function_call")

    def test_streaming_drops_json_deltas_for_mapped_calls(self):
        from responses_bridge import MessagesResponsesAdapter
        adapter = MessagesResponsesAdapter("mistral/mistral-medium-latest", None, "scope", dict(self.TOOL_MAP))
        started = adapter.feed({"type": "content_block_start", "index": 0, "content_block": {
            "type": "tool_use", "id": "call-1", "name": "apply_patch", "input": {}}})
        self.assertEqual(started[0]["item"]["type"], "custom_tool_call")
        self.assertEqual(adapter.feed({"type": "content_block_delta", "index": 0, "delta": {
            "type": "input_json_delta", "partial_json": '{"patch": "x"}'}}), [])
        stopped = adapter.feed({"type": "content_block_stop", "index": 0})
        kinds = [event["type"] for event in stopped]
        self.assertNotIn("response.function_call_arguments.done", kinds)
        done = next(event["item"] for event in stopped if event["type"] == "response.output_item.done")
        self.assertEqual(done["type"], "custom_tool_call")
        self.assertEqual(done["input"], "x")


class ReasoningStoreCapTests(unittest.TestCase):
    """Bounded storage of sealed provider thinking traces."""

    TRACE = "".join(f"chunk-{index:06d}\n" for index in range(20000))  # 220,000 characters

    def round_trip(self, cap, blocks):
        with tempfile.TemporaryDirectory() as directory:
            envelope = ReasoningEnvelope(Path(directory), cap)
            token = envelope.seal(copy.deepcopy(blocks), "account-one")
            return envelope.open(token, "account-one")

    def test_default_envelope_preserves_full_trace(self):
        blocks = [{"type": "thinking", "thinking": self.TRACE}]
        self.assertEqual(self.round_trip(0, blocks), blocks)

    def test_bounded_cap_keeps_head_prefix_with_marker(self):
        cap = 16384
        opened = self.round_trip(cap, [{"type": "thinking", "thinking": self.TRACE}])
        thinking = opened[0]["thinking"]
        self.assertTrue(thinking.startswith(self.TRACE[:cap].rstrip()))
        self.assertIn(f"{len(self.TRACE) - cap:,} of {len(self.TRACE):,} characters omitted", thinking)
        self.assertTrue(thinking.endswith("characters omitted]"))
        self.assertLess(len(thinking), cap + 120)
        self.assertNotIn("chunk-019999", thinking)

    def test_short_trace_passes_through(self):
        blocks = [{"type": "thinking", "thinking": "brief"}]
        self.assertEqual(self.round_trip(16384, blocks), blocks)

    def test_redacted_thinking_passes_through(self):
        blocks = [{"type": "redacted_thinking", "data": "x" * 500000}]
        self.assertEqual(self.round_trip(16384, blocks), blocks)

    def test_nonpositive_or_noninteger_cap_is_unbounded(self):
        blocks = [{"type": "thinking", "thinking": "y" * 50000}]
        for cap in (0, -5, None, True, "16384"):
            with self.subTest(cap=cap):
                self.assertEqual(self.round_trip(cap, blocks), blocks)

    def test_streaming_path_seals_bounded_trace(self):
        with tempfile.TemporaryDirectory() as directory:
            envelope = ReasoningEnvelope(Path(directory), 32)
            adapter = MessagesResponsesAdapter("kimi/kimi-for-coding", envelope, "scope")
            adapter.feed({"type": "content_block_start", "index": 0,
                          "content_block": {"type": "thinking", "thinking": ""}})
            adapter.feed({"type": "content_block_delta", "index": 0,
                          "delta": {"type": "thinking_delta", "thinking": "y" * 500}})
            events = adapter.feed({"type": "content_block_stop", "index": 0})
            done = next(event["item"] for event in events if event["type"] == "response.output_item.done")
            self.assertEqual(done["type"], "reasoning")
            opened = envelope.open(done["encrypted_content"], "scope")
            self.assertEqual(opened[0]["thinking"],
                             "y" * 32 + "\n\n[Provider Hub: earlier reasoning truncated - 468 of 500 characters omitted]")

    def test_store_cap_advertised_only_on_rambling_providers(self):
        from providers import PROVIDERS
        for provider_id in ("kimi", "mimo", "qwen-token-plan"):
            self.assertEqual(PROVIDERS[provider_id].get("reasoning_store_cap"), 16384)
        for provider_id in ("mistral", "deepseek", "cerebras", "ollama", "grok", "openrouter", "muse"):
            self.assertIsNone(PROVIDERS[provider_id].get("reasoning_store_cap"))

if __name__ == "__main__":
    unittest.main()
