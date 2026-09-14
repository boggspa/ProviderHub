"""Native Responses HTTP tests using deterministic local upstreams only."""
import copy
import http.client
import json
import socket
import time
import unittest
from unittest.mock import patch

import test_gateway_hub as fixtures
from test_gateway_hub import MockProvider


def response_object(body, second=False):
    output = ([{"type": "message", "id": "message-final", "role": "assistant", "status": "completed",
                "content": [{"type": "output_text", "text": "Checked.", "annotations": []}]}] if second else
              [{"type": "function_call", "id": "fc-native", "call_id": "call-native", "name": "read_file",
                "arguments": '{"path":"fixture.txt"}', "status": "completed"}])
    return {"id": "resp-second" if second else "resp-first", "object": "response", "created_at": 123,
            "model": body["model"], "status": "completed", "output": output, "error": None,
            "usage": {"input_tokens": 22, "output_tokens": 7}, "service_tier": "default"}


def handle_native_response(server):
    body = server.read_body()
    if MockProvider.mode == "quota":
        server.send_json(429, {"error": {"message": "Quota " + fixtures.PROVIDER_KEY}})
        return
    inputs = body.get("input", [])
    second = bool(body.get("previous_response_id")) or any(isinstance(item, dict) and item.get("type") == "function_call_output" for item in inputs)
    result = response_object(body, second)
    if MockProvider.mode == "failed":
        result.update(status="failed", error={"message": "Provider problem " + fixtures.PROVIDER_KEY, "code": "server_error"})
    if not body.get("stream"):
        server.send_json(200, result)
        return
    initial = {**result, "status": "in_progress", "output": [], "usage": None}
    events = [{"type": "response.created", "response": initial}]
    for index, item in enumerate(result["output"]):
        events.append({"type": "response.output_item.added", "output_index": index, "item": {**item, "arguments": ""}})
        if item["type"] == "function_call":
            events += [{"type": "response.function_call_arguments.delta", "item_id": item["id"],
                        "output_index": index, "delta": '{"path":'},
                       {"type": "response.function_call_arguments.delta", "item_id": item["id"],
                        "output_index": index, "delta": '"fixture.txt"}'}]
        events.append({"type": "response.output_item.done", "output_index": index, "item": item})
    if MockProvider.mode == "slow":
        server.send_response(200)
        server.send_header("Content-Type", "text/event-stream")
        server.send_header("Connection", "close")
        server.end_headers()
        server.wfile.write(b"data: " + json.dumps(events[0]).encode() + b"\n\n")
        server.wfile.flush()
        MockProvider.release.wait(4)
        server.close_connection = True
        return
    if MockProvider.mode == "invalid_terminal":
        events.append({"type": "response.completed", "response": {"id": "invalid"}})
    elif MockProvider.mode != "missing_terminal":
        events.append({"type": "response.failed" if MockProvider.mode == "failed" else "response.completed", "response": result})
    server.send_sse(events)


class NativeResponsesTests(unittest.TestCase):
    def setUp(self):
        fixtures.GatewayHubHTTPTests.setUp(self)
        self.patch = patch.object(MockProvider, "do_POST", handle_native_response)
        self.patch.start()

    def tearDown(self):
        fixtures.GatewayHubHTTPTests.tearDown(self)
        self.patch.stop()

    start_gateway = fixtures.GatewayHubHTTPTests.start_gateway

    def start(self, provider="grok"):
        self.provider = provider
        self.route = self.start_gateway(provider, "grok-4.6" if provider == "grok" else "sample:cloud", {
            "context": 500000, "vision": True, "effort_modes": ["low", "medium", "high", "xhigh"],
        })

    def request(self, body, token=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.gateway.server_port, timeout=6)
        connection.request("POST", "/v1/responses", json.dumps(body), {
            "Authorization": "Bearer " + (token or self.runtime.token), "Content-Type": "application/json", **(headers or {}),
        })
        response = connection.getresponse()
        result = (response.status, response.read())
        connection.close()
        deadline = time.monotonic() + 2
        while self.runtime.status()["active"] and time.monotonic() < deadline:
            time.sleep(.01)
        return result

    def body(self, **changes):
        return {"model": self.route, "input": [{"role": "user", "content": fixtures.LOCAL_REQUEST_TEXT}],
                "store": False, "stream": False,
                "tools": [{"type": "function", "name": "read_file", "parameters": {"type": "object"}}], **changes}

    def test_json_tool_history_and_native_reasoning_remain_intact(self):
        self.start()
        body = self.body(reasoning={"effort": "max"}, service_tier="fast", client_metadata={"local_session": "not-for-provider"})
        before = copy.deepcopy(body)
        status, raw = self.request(body, headers={"x-api-key": "CLIENT-KEY-NOT-FORWARDED"})
        self.assertEqual(status, 200, raw)
        result = json.loads(raw)
        self.assertEqual(result["model"], self.route)
        body["input"] += result["output"] + [{"type": "reasoning", "encrypted_content": "opaque-provider-reasoning"},
                                             {"type": "function_call_output", "call_id": "call-native", "output": "read result"}]
        status, raw = self.request(body)
        self.assertEqual(status, 200, raw)
        self.assertEqual(json.loads(raw)["output"][0]["content"][0]["text"], "Checked.")
        first, second = MockProvider.requests
        self.assertEqual(first["model"], "grok-4.6")
        self.assertNotIn("client_metadata", first)
        self.assertEqual(first["reasoning"]["effort"], "xhigh")
        self.assertEqual(first["service_tier"], "priority")
        self.assertEqual(first["input"], before["input"])
        self.assertEqual(second["input"][2]["encrypted_content"], "opaque-provider-reasoning")
        for headers in MockProvider.request_headers:
            self.assertEqual(headers["authorization"], "Bearer " + fixtures.PROVIDER_KEY)
            self.assertNotIn(self.runtime.token, json.dumps(headers))
            self.assertNotIn("CLIENT-KEY-NOT-FORWARDED", json.dumps(headers))
        self.assertNotIn(fixtures.LOCAL_REQUEST_TEXT, (self.root / "activity.jsonl").read_text())

    def test_streaming_preserves_function_deltas_and_terminal_usage(self):
        self.start()
        status, raw = self.request(self.body(stream=True))
        self.assertEqual(status, 200, raw)
        events = fixtures.parse_sse(raw)
        self.assertEqual(events[0]["response"]["model"], self.route)
        self.assertEqual(events[-1]["type"], "response.completed")
        self.assertEqual(events[-1]["response"]["usage"]["input_tokens"], 22)
        args = ''.join(event.get("delta", "") for event in events if event["type"] == "response.function_call_arguments.delta")
        self.assertEqual(json.loads(args), {"path": "fixture.txt"})
        self.assertEqual(self.runtime.status()["completed"], 1)

    def test_explicit_stored_xai_continuations_are_scoped_to_account(self):
        self.start()
        status, raw = self.request(self.body(store=True))
        self.assertEqual(status, 200, raw)
        identifier = json.loads(raw)["id"]
        status, raw = self.request(self.body(previous_response_id=identifier, input="Continue"))
        self.assertEqual(status, 200, raw)
        self.assertNotIn(fixtures.LOCAL_REQUEST_TEXT, (self.root / "response-ownership.json").read_text())
        self.runtime.key = "rotated-key"
        status, _ = self.request(self.body(previous_response_id=identifier))
        self.assertEqual(status, 400)
        self.assertEqual(len(MockProvider.requests), 2)

    def test_store_false_does_not_enable_id_continuations(self):
        self.start()
        status, raw = self.request(self.body())
        status, _ = self.request(self.body(previous_response_id=json.loads(raw)["id"]))
        self.assertEqual(status, 400)
        self.assertFalse((self.root / "response-ownership.json").exists())

    def test_ollama_uses_daemon_auth_and_full_history(self):
        self.start("ollama")
        status, raw = self.request(self.body())
        self.assertEqual(status, 200, raw)
        self.assertEqual(MockProvider.request_headers[0]["authorization"], "Bearer ollama")
        self.assertEqual(MockProvider.requests[0]["model"], "sample:cloud")
        for changes in ({"store": True}, {"previous_response_id": "resp-first"}, {"service_tier": "fast"}):
            self.assertEqual(self.request(self.body(**changes))[0], 400)

    def test_ollama_forwards_images_even_when_catalogue_omits_vision(self):
        self.provider = "ollama"
        self.route = self.start_gateway("ollama", "sample:cloud", {
            "context": 500000, "vision": False, "tools": True,
            "effort_modes": ["low", "medium", "high", "xhigh"],
        })
        image = {"type": "input_image", "image_url": "data:image/png;base64,AAAA"}
        status, raw = self.request(self.body(input=[{
            "role": "user",
            "content": [{"type": "input_text", "text": "describe"}, image],
        }]))
        self.assertEqual(status, 200, raw)
        self.assertEqual(MockProvider.requests[0]["input"][0]["content"][1], image)

    def test_non_ollama_native_route_still_rejects_unadvertised_images(self):
        self.start("grok")
        self.runtime.settings["_model_specs"][self.route]["vision"] = False
        status, raw = self.request(self.body(input=[{
            "role": "user",
            "content": [
                {"type": "input_text", "text": "describe"},
                {"type": "input_image", "image_url": "data:image/png;base64,AAAA"},
            ],
        }]))
        self.assertEqual(status, 400, raw)
        self.assertIn(b"does not advertise image input", raw)
        self.assertEqual(len(MockProvider.requests), 0)

    def test_unsupported_tools_fields_and_providers_are_explicit(self):
        self.start()
        for changes in ({"tools": [{"type": "custom", "name": "apply_patch"}]}, {"background": True},
                        {"input": [{"type": "custom_tool_call"}]}, {"stream": "true"},
                        {"model": "muse/muse-spark-1.3"}):
            self.assertEqual(self.request(self.body(**changes))[0], 400)
        self.assertEqual(len(MockProvider.requests), 0)

    def test_authentication_and_browser_origin_checks_apply(self):
        self.start()
        self.assertEqual(self.request(self.body(), token="incorrect")[0], 401)
        self.assertEqual(self.request(self.body(), headers={"Origin": "https://example.invalid"})[0], 403)
        self.assertEqual(len(MockProvider.requests), 0)

    def test_quota_and_native_failed_response_redact_credentials(self):
        self.start()
        MockProvider.mode = "quota"
        status, raw = self.request(self.body())
        self.assertEqual(status, 429)
        self.assertNotIn(fixtures.PROVIDER_KEY, raw.decode())
        MockProvider.mode = "failed"
        for stream in (False, True):
            status, raw = self.request(self.body(stream=stream))
            self.assertEqual(status, 200)
            self.assertNotIn(fixtures.PROVIDER_KEY, raw.decode())
        self.assertEqual(self.runtime.status()["failed"], 3)

    def test_truncated_or_malformed_stream_is_not_recorded_as_complete(self):
        self.start()
        for mode in ("missing_terminal", "invalid_terminal"):
            MockProvider.mode = mode
            status, raw = self.request(self.body(stream=True))
            self.assertEqual(status, 200)
            self.assertEqual(fixtures.parse_sse(raw)[-1]["type"], "error")
        self.assertEqual(self.runtime.status()["completed"], 0)
        self.assertEqual(self.runtime.status()["failed"], 2)

    def test_client_cancellation_releases_shared_capacity(self):
        self.start()
        MockProvider.mode = "slow"
        connection = http.client.HTTPConnection("127.0.0.1", self.gateway.server_port, timeout=6)
        connection.request("POST", "/v1/responses", json.dumps(self.body(stream=True)), {
            "Authorization": "Bearer " + self.runtime.token, "Content-Type": "application/json"})
        response = connection.getresponse()
        response.read(30)
        sock = getattr(getattr(response.fp, "raw", None), "_sock", None)
        if sock:
            sock.shutdown(socket.SHUT_RDWR)
        response.close()
        connection.close()
        deadline = time.monotonic() + 3
        while self.runtime.status()["active"] and time.monotonic() < deadline:
            time.sleep(.03)
        self.assertEqual(self.runtime.status()["active"], 0)
        self.assertIn('"event": "cancelled"', (self.root / "activity.jsonl").read_text())

    def test_multi_agent_v2_items_are_normalized_to_message_items(self):
        """Verify that Codex multi_agent_v2 history items are translated to standard message items at ingress."""
        from responses_native import _normalize_multi_agent_items
        
        # Test multi_agent_call -> message with subagent_* alias
        inputs = [
            {"type": "message", "role": "user", "content": "Hello"},
            {"type": "multi_agent_call", "call_id": "call_123", "agent": "researcher", "arguments": {"task": "research"}},
            {"type": "multi_agent_call_output", "call_id": "call_123", "output": "Research complete"},
            {"type": "agent_message", "agent": "researcher", "content": "Found some data", "role": "assistant"},
            {"type": "function_call", "call_id": "fc_456", "name": "read_file", "arguments": '{}'},
        ]
        normalized = _normalize_multi_agent_items(inputs)
        
        self.assertEqual(len(normalized), 5)
        
        # First item: message passes through unchanged
        self.assertEqual(normalized[0]["type"], "message")
        self.assertEqual(normalized[0]["role"], "user")
        self.assertEqual(normalized[0]["content"], "Hello")
        
        # Second item: multi_agent_call -> message with subagent_ alias
        self.assertEqual(normalized[1]["type"], "message")
        self.assertEqual(normalized[1]["role"], "user")
        self.assertIn("subagent_researcher", normalized[1]["content"])
        self.assertIn("[Task from parent agent", normalized[1]["content"])
        self.assertIn("research", normalized[1]["content"])
        
        # Third item: multi_agent_call_output -> message with subagent_ alias
        self.assertEqual(normalized[2]["type"], "message")
        self.assertEqual(normalized[2]["role"], "user")
        self.assertIn("subagent_call_123", normalized[2]["content"])
        self.assertIn("Research complete", normalized[2]["content"])
        
        # Fourth item: agent_message -> message with subagent_ prefix
        self.assertEqual(normalized[3]["type"], "message")
        self.assertEqual(normalized[3]["role"], "user")
        self.assertIn("[subagent_researcher]", normalized[3]["content"])
        self.assertIn("Found some data", normalized[3]["content"])
        
        # Fifth item: standard function_call passes through unchanged
        self.assertEqual(normalized[4]["type"], "function_call")
        self.assertEqual(normalized[4]["name"], "read_file")

    def test_multi_agent_v2_normalization_preserves_call_id_in_content(self):
        """Verify that subagent call/output pairs maintain call_id references in their message content."""
        from responses_native import _normalize_multi_agent_items
        
        inputs = [
            {"type": "multi_agent_call", "call_id": "sub_1", "agent": "analyst"},
            {"type": "multi_agent_call_output", "call_id": "sub_1", "output": "Analysis done"},
            {"type": "multi_agent_call", "id": "sub_2", "agent": "writer"},
            {"type": "multi_agent_call_output", "call_id": "sub_2", "result": "Text generated"},
        ]
        normalized = _normalize_multi_agent_items(inputs)
        
        # Verify call_id is preserved in the message content
        self.assertIn("sub_1", normalized[0]["content"])
        self.assertIn("sub_1", normalized[1]["content"])
        self.assertIn("sub_2", normalized[2]["content"])
        self.assertIn("sub_2", normalized[3]["content"])
        
        # Verify all are message items
        for item in normalized:
            self.assertEqual(item["type"], "message")
        
        # Verify content format
        self.assertIn("analyst", normalized[0]["content"])
        self.assertIn("[Task from parent agent", normalized[0]["content"])
        self.assertIn("returned", normalized[1]["content"])
        self.assertIn("Analysis done", normalized[1]["content"])

    def test_multi_agent_v2_with_missing_fields_uses_defaults(self):
        """Verify normalization handles multi-agent items with missing optional fields gracefully."""
        from responses_native import _normalize_multi_agent_items
        
        # multi_agent_call with minimal fields
        inputs = [
            {"type": "multi_agent_call"},
            {"type": "multi_agent_call_output"},
            {"type": "agent_message"},
        ]
        normalized = _normalize_multi_agent_items(inputs)
        
        # All should be message items
        self.assertEqual(normalized[0]["type"], "message")
        self.assertEqual(normalized[0]["role"], "user")
        self.assertIn("subagent_agent", normalized[0]["content"])
        self.assertIn("[Task from parent agent", normalized[0]["content"])
        # Verify fallback call_id is in content
        self.assertIn("subagent_agent ()", normalized[0]["content"])
        
        self.assertEqual(normalized[1]["type"], "message")
        self.assertEqual(normalized[1]["role"], "user")
        self.assertIn("subagent_call_1", normalized[1]["content"])
        self.assertIn("returned", normalized[1]["content"])
        
        self.assertEqual(normalized[2]["type"], "message")
        self.assertEqual(normalized[2]["role"], "user")
        self.assertIn("[subagent_subagent]", normalized[2]["content"])

    def test_multi_agent_v2_passthrough_non_dict_items(self):
        """Verify non-dict items (e.g., strings) pass through unchanged."""
        from responses_native import _normalize_multi_agent_items
        
        inputs = ["string_item", {"type": "message", "role": "user", "content": "test"}]
        normalized = _normalize_multi_agent_items(inputs)
        
        self.assertEqual(normalized[0], "string_item")
        self.assertEqual(normalized[1]["type"], "message")

    def test_multi_agent_v2_non_list_passthrough(self):
        """Verify non-list input passes through unchanged."""
        from responses_native import _normalize_multi_agent_items
        
        result = _normalize_multi_agent_items("not a list")
        self.assertEqual(result, "not a list")

    def test_multi_agent_v2_stringifies_non_string_payloads(self):
        """Verify that dict/list content in multi-agent items is stringified."""
        from responses_native import _normalize_multi_agent_items
        
        inputs = [
            {"type": "multi_agent_call", "call_id": "call_1", "agent": "researcher", "arguments": {"complex": {"nested": "data"}}},
            {"type": "multi_agent_call_output", "call_id": "call_2", "output": {"result": ["a", "b", "c"]}},
            {"type": "agent_message", "agent": "writer", "content": {"text": "hello"}},
        ]
        normalized = _normalize_multi_agent_items(inputs)
        
        # All should be message items with string content
        for item in normalized:
            self.assertEqual(item["type"], "message")
            self.assertIsInstance(item["content"], str)
        
        # Verify content contains stringified data
        self.assertIn("complex", normalized[0]["content"])
        self.assertIn("nested", normalized[0]["content"])
        self.assertIn("result", normalized[1]["content"])
        self.assertIn("text", normalized[2]["content"])

    def test_multi_agent_v2_guards_list_content(self):
        """Verify that list content in agent_message doesn't crash on .startswith."""
        from responses_native import _normalize_multi_agent_items
        
        # agent_message with list content (which would crash .startswith)
        inputs = [
            {"type": "agent_message", "agent": "researcher", "content": ["item1", "item2"]},
        ]
        normalized = _normalize_multi_agent_items(inputs)
        
        self.assertEqual(len(normalized), 1)
        self.assertEqual(normalized[0]["type"], "message")
        self.assertIsInstance(normalized[0]["content"], str)
        self.assertIn("researcher", normalized[0]["content"])

    def test_subagent_call_aliases_are_normalized(self):
        """Verify that subagent_call and subagent_call_output aliases are also normalized to message items."""
        from responses_native import _normalize_multi_agent_items
        
        inputs = [
            {"type": "message", "role": "user", "content": "Hello"},
            {"type": "subagent_call", "call_id": "sub_1", "agent": "coder", "arguments": {"task": "write code"}},
            {"type": "subagent_call_output", "call_id": "sub_1", "output": "Code written"},
        ]
        normalized = _normalize_multi_agent_items(inputs)
        
        self.assertEqual(len(normalized), 3)
        
        # First item: standard message passes through
        self.assertEqual(normalized[0]["type"], "message")
        self.assertEqual(normalized[0]["role"], "user")
        self.assertEqual(normalized[0]["content"], "Hello")
        
        # Second item: subagent_call -> message
        self.assertEqual(normalized[1]["type"], "message")
        self.assertEqual(normalized[1]["role"], "user")
        self.assertIn("subagent_coder", normalized[1]["content"])
        self.assertIn("sub_1", normalized[1]["content"])
        self.assertIn("[Task from parent agent", normalized[1]["content"])
        
        # Third item: subagent_call_output -> message
        self.assertEqual(normalized[2]["type"], "message")
        self.assertEqual(normalized[2]["role"], "user")
        self.assertIn("sub_1", normalized[2]["content"])
        self.assertIn("returned", normalized[2]["content"])
        self.assertIn("Code written", normalized[2]["content"])

    def test_mixed_multi_agent_and_subagent_aliases(self):
        """Verify mixed multi_agent_* and subagent_* items are all normalized together."""
        from responses_native import _normalize_multi_agent_items

        inputs = [
            {"type": "multi_agent_call", "call_id": "ma_1", "agent": "analyst"},
            {"type": "subagent_call", "call_id": "sa_1", "agent": "writer"},
            {"type": "multi_agent_call_output", "call_id": "ma_1", "output": "Analysis"},
            {"type": "subagent_call_output", "call_id": "sa_1", "output": "Text"},
            {"type": "agent_message", "agent": "coordinator", "content": "Done"},
        ]
        normalized = _normalize_multi_agent_items(inputs)

        self.assertEqual(len(normalized), 5)

        # All should be message items
        for item in normalized:
            self.assertEqual(item["type"], "message")

        # Check roles
        self.assertEqual(normalized[0]["role"], "user")
        self.assertEqual(normalized[1]["role"], "user")
        self.assertEqual(normalized[2]["role"], "user")
        self.assertEqual(normalized[3]["role"], "user")
        self.assertEqual(normalized[4]["role"], "user")

        # Check content contains expected identifiers
        self.assertIn("analyst", normalized[0]["content"])
        self.assertIn("writer", normalized[1]["content"])
        self.assertIn("Analysis", normalized[2]["content"])
        self.assertIn("Text", normalized[3]["content"])
        self.assertIn("coordinator", normalized[4]["content"])


class MultiAgentNormalizationTests(unittest.TestCase):
    """Socket-free unit tests for _normalize_multi_agent_items function.
    
    These tests can run in restricted sandboxes since they only test the pure
    normalization function and don't require network access or server setup.
    """

    def test_multi_agent_v2_items_are_normalized_to_message_items(self):
        """Verify that Codex multi_agent_v2 history items are translated to standard message items at ingress."""
        from responses_native import _normalize_multi_agent_items

        # Test multi_agent_call -> message with subagent_* alias
        inputs = [
            {"type": "message", "role": "user", "content": "Hello"},
            {"type": "multi_agent_call", "call_id": "call_123", "agent": "researcher", "arguments": {"task": "research"}},
            {"type": "multi_agent_call_output", "call_id": "call_123", "output": "Research complete"},
            {"type": "agent_message", "agent": "researcher", "content": "Found some data", "role": "assistant"},
            {"type": "function_call", "call_id": "fc_456", "name": "read_file", "arguments": '{}'},
        ]
        normalized = _normalize_multi_agent_items(inputs)

        self.assertEqual(len(normalized), 5)

        # First item: message passes through unchanged
        self.assertEqual(normalized[0]["type"], "message")
        self.assertEqual(normalized[0]["role"], "user")
        self.assertEqual(normalized[0]["content"], "Hello")

        # Second item: multi_agent_call -> message with subagent_ alias
        self.assertEqual(normalized[1]["type"], "message")
        self.assertEqual(normalized[1]["role"], "user")
        self.assertIn("subagent_researcher", normalized[1]["content"])
        self.assertIn("[Task from parent agent", normalized[1]["content"])
        self.assertIn("research", normalized[1]["content"])

        # Third item: multi_agent_call_output -> message with subagent_ alias
        self.assertEqual(normalized[2]["type"], "message")
        self.assertEqual(normalized[2]["role"], "user")
        self.assertIn("subagent_call_123", normalized[2]["content"])
        self.assertIn("Research complete", normalized[2]["content"])

        # Fourth item: agent_message -> message with subagent_ prefix
        self.assertEqual(normalized[3]["type"], "message")
        self.assertEqual(normalized[3]["role"], "user")
        self.assertIn("[subagent_researcher]", normalized[3]["content"])
        self.assertIn("Found some data", normalized[3]["content"])

        # Fifth item: standard function_call passes through unchanged
        self.assertEqual(normalized[4]["type"], "function_call")
        self.assertEqual(normalized[4]["name"], "read_file")

    def test_multi_agent_v2_normalization_preserves_call_id_in_content(self):
        """Verify that subagent call/output pairs maintain call_id references in their message content."""
        from responses_native import _normalize_multi_agent_items

        inputs = [
            {"type": "multi_agent_call", "call_id": "sub_1", "agent": "analyst"},
            {"type": "multi_agent_call_output", "call_id": "sub_1", "output": "Analysis done"},
            {"type": "multi_agent_call", "id": "sub_2", "agent": "writer"},
            {"type": "multi_agent_call_output", "call_id": "sub_2", "result": "Text generated"},
        ]
        normalized = _normalize_multi_agent_items(inputs)

        # Verify call_id is preserved in the message content
        self.assertIn("sub_1", normalized[0]["content"])
        self.assertIn("sub_1", normalized[1]["content"])
        self.assertIn("sub_2", normalized[2]["content"])
        self.assertIn("sub_2", normalized[3]["content"])

        # Verify all are message items
        for item in normalized:
            self.assertEqual(item["type"], "message")

        # Verify content format
        self.assertIn("analyst", normalized[0]["content"])
        self.assertIn("[Task from parent agent", normalized[0]["content"])
        self.assertIn("returned", normalized[1]["content"])
        self.assertIn("Analysis done", normalized[1]["content"])

    def test_multi_agent_v2_with_missing_fields_uses_defaults(self):
        """Verify normalization handles multi-agent items with missing optional fields gracefully."""
        from responses_native import _normalize_multi_agent_items

        # multi_agent_call with minimal fields
        inputs = [
            {"type": "multi_agent_call"},
            {"type": "multi_agent_call_output"},
            {"type": "agent_message"},
        ]
        normalized = _normalize_multi_agent_items(inputs)

        # All should be message items
        self.assertEqual(normalized[0]["type"], "message")
        self.assertEqual(normalized[0]["role"], "user")
        self.assertIn("subagent_agent", normalized[0]["content"])
        self.assertIn("[Task from parent agent", normalized[0]["content"])
        # Verify fallback call_id is in content
        self.assertIn("subagent_agent ()", normalized[0]["content"])

        self.assertEqual(normalized[1]["type"], "message")
        self.assertEqual(normalized[1]["role"], "user")
        self.assertIn("subagent_call_1", normalized[1]["content"])
        self.assertIn("returned", normalized[1]["content"])

        self.assertEqual(normalized[2]["type"], "message")
        self.assertEqual(normalized[2]["role"], "user")
        self.assertIn("[subagent_subagent]", normalized[2]["content"])

    def test_multi_agent_v2_passthrough_non_dict_items(self):
        """Verify non-dict items (e.g., strings) pass through unchanged."""
        from responses_native import _normalize_multi_agent_items

        inputs = ["string_item", {"type": "message", "role": "user", "content": "test"}]
        normalized = _normalize_multi_agent_items(inputs)

        self.assertEqual(normalized[0], "string_item")
        self.assertEqual(normalized[1]["type"], "message")

    def test_multi_agent_v2_non_list_passthrough(self):
        """Verify non-list input passes through unchanged."""
        from responses_native import _normalize_multi_agent_items

        result = _normalize_multi_agent_items("not a list")
        self.assertEqual(result, "not a list")

    def test_multi_agent_v2_stringifies_non_string_payloads(self):
        """Verify that dict/list content in multi-agent items is stringified."""
        from responses_native import _normalize_multi_agent_items

        inputs = [
            {"type": "multi_agent_call", "call_id": "call_1", "agent": "researcher", "arguments": {"complex": {"nested": "data"}}},
            {"type": "multi_agent_call_output", "call_id": "call_2", "output": {"result": ["a", "b", "c"]}},
            {"type": "agent_message", "agent": "writer", "content": {"text": "hello"}},
        ]
        normalized = _normalize_multi_agent_items(inputs)

        # All should be message items with string content
        for item in normalized:
            self.assertEqual(item["type"], "message")
            self.assertIsInstance(item["content"], str)

        # Verify content contains stringified data
        self.assertIn("complex", normalized[0]["content"])
        self.assertIn("nested", normalized[0]["content"])
        self.assertIn("result", normalized[1]["content"])
        self.assertIn("text", normalized[2]["content"])

    def test_multi_agent_v2_guards_list_content(self):
        """Verify that list content in agent_message doesn't crash on .startswith."""
        from responses_native import _normalize_multi_agent_items

        # agent_message with list content (which would crash .startswith)
        inputs = [
            {"type": "agent_message", "agent": "researcher", "content": ["item1", "item2"]},
        ]
        normalized = _normalize_multi_agent_items(inputs)

        self.assertEqual(len(normalized), 1)
        self.assertEqual(normalized[0]["type"], "message")
        self.assertIsInstance(normalized[0]["content"], str)
        self.assertIn("researcher", normalized[0]["content"])

    def test_subagent_call_aliases_are_normalized(self):
        """Verify that subagent_call and subagent_call_output aliases are also normalized to message items."""
        from responses_native import _normalize_multi_agent_items

        inputs = [
            {"type": "message", "role": "user", "content": "Hello"},
            {"type": "subagent_call", "call_id": "sub_1", "agent": "coder", "arguments": {"task": "write code"}},
            {"type": "subagent_call_output", "call_id": "sub_1", "output": "Code written"},
        ]
        normalized = _normalize_multi_agent_items(inputs)

        self.assertEqual(len(normalized), 3)

        # First item: standard message passes through
        self.assertEqual(normalized[0]["type"], "message")
        self.assertEqual(normalized[0]["role"], "user")
        self.assertEqual(normalized[0]["content"], "Hello")

        # Second item: subagent_call -> message
        self.assertEqual(normalized[1]["type"], "message")
        self.assertEqual(normalized[1]["role"], "user")
        self.assertIn("subagent_coder", normalized[1]["content"])
        self.assertIn("sub_1", normalized[1]["content"])
        self.assertIn("[Task from parent agent", normalized[1]["content"])

        # Third item: subagent_call_output -> message
        self.assertEqual(normalized[2]["type"], "message")
        self.assertEqual(normalized[2]["role"], "user")
        self.assertIn("sub_1", normalized[2]["content"])
        self.assertIn("returned", normalized[2]["content"])
        self.assertIn("Code written", normalized[2]["content"])

    def test_mixed_multi_agent_and_subagent_aliases(self):
        """Verify mixed multi_agent_* and subagent_* items are all normalized together."""
        from responses_native import _normalize_multi_agent_items

        inputs = [
            {"type": "multi_agent_call", "call_id": "ma_1", "agent": "analyst"},
            {"type": "subagent_call", "call_id": "sa_1", "agent": "writer"},
            {"type": "multi_agent_call_output", "call_id": "ma_1", "output": "Analysis"},
            {"type": "subagent_call_output", "call_id": "sa_1", "output": "Text"},
            {"type": "agent_message", "agent": "coordinator", "content": "Done"},
        ]
        normalized = _normalize_multi_agent_items(inputs)

        self.assertEqual(len(normalized), 5)

        # All should be message items
        for item in normalized:
            self.assertEqual(item["type"], "message")

        # Check roles
        self.assertEqual(normalized[0]["role"], "user")
        self.assertEqual(normalized[1]["role"], "user")
        self.assertEqual(normalized[2]["role"], "user")
        self.assertEqual(normalized[3]["role"], "user")
        self.assertEqual(normalized[4]["role"], "user")

        # Check content contains expected identifiers
        self.assertIn("analyst", normalized[0]["content"])
        self.assertIn("writer", normalized[1]["content"])
        self.assertIn("Analysis", normalized[2]["content"])
        self.assertIn("Text", normalized[3]["content"])
        self.assertIn("coordinator", normalized[4]["content"])

    def test_non_dict_item_error_includes_type(self):
        """Regression test for N1: non-dict items must raise BridgeError (not AttributeError) via prepare_native."""
        from types import SimpleNamespace

        from bridge_core import BridgeError
        from hub_config import qualify
        from responses_native import prepare_native

        route = qualify("mistral", "mistral-medium-2508")
        # Validation runs before any provider/network access, so a minimal
        # stub runtime carrying only the model spec is sufficient.
        runtime = SimpleNamespace(settings={"_model_specs": {route: {}}})

        for bad_item, expected in (("string_item", "str"), (42, "int")):
            with self.subTest(item=bad_item):
                with self.assertRaises(BridgeError) as ctx:
                    prepare_native(runtime, {"model": route, "input": [bad_item]})
                self.assertIn("Unsupported Responses history item", str(ctx.exception))
                self.assertIn("'%s'" % expected, str(ctx.exception))

        # Dict path unchanged: unknown dict types still report their 'type' value.
        with self.assertRaises(BridgeError) as ctx:
            prepare_native(runtime, {"model": route, "input": [{"type": "weird_future"}]})
        self.assertIn("'weird_future'", str(ctx.exception))

    def test_extract_subagent_task_prefers_encrypted_content(self):
        """Dict-form envelopes expose the plaintext instruction, not the routing text."""
        from responses_native import _extract_subagent_task

        args = {
            "input_text": "Message Type: NEW_TASK\nSender: User\nActual task here",
            "encrypted_content": "Plain text extraction",
        }
        self.assertEqual(_extract_subagent_task(args), "Plain text extraction")

    def test_extract_subagent_task_strips_headers_from_input_text(self):
        """Dict-form routing text is header-stripped like list-form blocks."""
        from responses_native import _extract_subagent_task

        args = {"input_text": "Message Type: NEW_TASK\nSender: User\nActual task here"}
        self.assertEqual(_extract_subagent_task(args), "Actual task here")

    def test_extract_subagent_task_fallback(self):
        """Plain strings pass through; unknown dicts stay JSON-stringified."""
        import json

        from responses_native import _extract_subagent_task

        self.assertEqual(_extract_subagent_task("Just a string"), "Just a string")
        # Unknown shapes keep the suite's JSON stringification contract (see
        # test_multi_agent_v2_stringifies_non_string_payloads), not str().
        self.assertEqual(_extract_subagent_task({"unknown": "data"}), json.dumps({"unknown": "data"}))

    def test_stringified_envelope_arguments_are_unwrapped(self):
        """Responses arguments travel as strings: a stringified wire envelope unwraps to plaintext."""
        import json

        from responses_native import _normalize_multi_agent_items

        envelope = [
            {"type": "input_text",
             "text": "Message Type: NEW_TASK\nTask name: /root/doc_analyzer_2\nSender: /root\nPayload:\n"},
            {"type": "encrypted_content", "encrypted_content": "Review the provider docs."},
        ]
        inputs = [
            {"type": "multi_agent_call", "call_id": "call_1", "agent": "doc_analyzer_2",
             "arguments": json.dumps(envelope)},
        ]
        normalized = _normalize_multi_agent_items(inputs)

        self.assertEqual(normalized[0]["role"], "user")
        self.assertIn("Review the provider docs.", normalized[0]["content"])
        for token in ("input_text", "encrypted_content", "Message Type:", "Task name:"):
            self.assertNotIn(token, normalized[0]["content"])

    def test_output_envelopes_are_unwrapped_not_dumped(self):
        """Subagent outputs carrying FINAL_ANSWER envelopes (incl. nested echoes) unwrap to plaintext."""
        import json

        from responses_native import _normalize_multi_agent_items

        nested_echo = (
            "Message Type: FINAL_ANSWER\nTask name: /root\nSender: /root/doc_analyzer_2\nPayload:\n"
            "subagent: " + json.dumps([
                {"type": "input_text",
                 "text": "Message Type: NEW_TASK\nTask name: /root/doc_analyzer_2\nSender: /root\nPayload:\n"},
                {"type": "encrypted_content", "encrypted_content": "Review the provider docs."},
            ])
        )
        inputs = [
            {"type": "multi_agent_call_output", "call_id": "call_1",
             "output": [{"type": "input_text", "text": nested_echo}]},
            {"type": "multi_agent_call_output", "call_id": "call_2",
             "output": json.dumps([{"type": "input_text", "text": nested_echo}])},
        ]
        normalized = _normalize_multi_agent_items(inputs)

        for item in normalized:
            self.assertEqual(item["role"], "user")
            self.assertIn("Review the provider docs.", item["content"])
            for token in ("input_text", "encrypted_content", "Message Type:", "Task name:", "subagent:"):
                self.assertNotIn(token, item["content"])

    def test_agent_message_subagent_echo_is_unwrapped(self):
        """A previous turn's `subagent: [...]` model echo unwraps instead of compounding."""
        import json

        from responses_native import _normalize_multi_agent_items

        envelope = [
            {"type": "input_text",
             "text": "Message Type: NEW_TASK\nTask name: /root/doc_analyzer_2\nSender: /root\nPayload:\n"},
            {"type": "encrypted_content", "encrypted_content": "Review the provider docs."},
        ]
        inputs = [
            {"type": "agent_message", "agent": "doc_analyzer_2", "role": "assistant",
             "content": "subagent: " + json.dumps(envelope)},
        ]
        normalized = _normalize_multi_agent_items(inputs)

        self.assertEqual(normalized[0]["role"], "user")
        self.assertIn("Review the provider docs.", normalized[0]["content"])
        for token in ("input_text", "encrypted_content", "Message Type:", "subagent: ["):
            self.assertNotIn(token, normalized[0]["content"])

    def test_multi_agent_plain_prose_passes_through_unchanged(self):
        """Ordinary prose in multi-agent items is never rewritten by wire cleanup."""
        from responses_native import _normalize_multi_agent_items

        inputs = [
            {"type": "multi_agent_call", "call_id": "call_1", "agent": "coder",
             "arguments": "Write the migration guide."},
            {"type": "multi_agent_call_output", "call_id": "call_1", "output": "Guide written."},
            {"type": "agent_message", "agent": "coder", "role": "assistant", "content": "Working on it."},
        ]
        normalized = _normalize_multi_agent_items(inputs)

        self.assertIn("Write the migration guide.", normalized[0]["content"])
        self.assertIn("Guide written.", normalized[1]["content"])
        self.assertIn("Working on it.", normalized[2]["content"])

    def test_envelope_footer_is_stripped_not_forwarded(self):
        """The trailing [END_OF_MESSAGE] frame is harness framing, not task content."""
        from responses_native import _normalize_multi_agent_items

        inputs = [
            {"type": "multi_agent_call", "call_id": "call_1", "agent": "doc_explorer",
             "arguments": [{"type": "input_text",
                            "text": "Message Type: NEW_TASK\nTask name: /root/doc_explorer\nPayload:\n"},
                           {"type": "encrypted_content",
                            "encrypted_content": "Explore the repository.\n[END_OF_MESSAGE]"}]},
            {"type": "agent_message", "agent": "doc_explorer", "role": "assistant",
             "content": "Return the list of files found. [END_OF_MESSAGE]"},
        ]
        normalized = _normalize_multi_agent_items(inputs)

        self.assertIn("Explore the repository.", normalized[0]["content"])
        self.assertNotIn("[END_OF_MESSAGE]", normalized[0]["content"])
        self.assertIn("Return the list of files found.", normalized[1]["content"])
        self.assertNotIn("[END_OF_MESSAGE]", normalized[1]["content"])

    def test_agent_message_maps_to_user_role(self):
        """Synthetic subagent speech is received history, never a Mistral prefill."""
        from responses_native import _normalize_multi_agent_items

        inputs = [
            {"type": "agent_message", "agent": "researcher", "role": "assistant",
             "content": "Found some data"},
            {"type": "agent_message", "agent": "writer", "content": "Draft done"},
        ]
        normalized = _normalize_multi_agent_items(inputs)

        for item in normalized:
            self.assertEqual(item["type"], "message")
            self.assertEqual(item["role"], "user")
        self.assertIn("[subagent_researcher]", normalized[0]["content"])
        self.assertIn("[subagent_writer]", normalized[1]["content"])

    def test_agent_message_prefix_is_idempotent(self):
        """Echoed attributions collapse to a single prefix instead of stacking."""
        from responses_native import _normalize_multi_agent_items

        inputs = [
            {"type": "agent_message", "agent": "subagent", "role": "assistant",
             "content": "[subagent_subagent]: [subagent_subagent]: Analyze AGENTS.md"},
            {"type": "agent_message", "agent": "subagent", "role": "assistant",
             "content": "[subagent_subagent2]: Review the Source/ directory"},
            {"type": "agent_message", "agent": "researcher", "role": "assistant",
             "content": "  [Subagent_Researcher] :  Found some data"},
        ]
        normalized = _normalize_multi_agent_items(inputs)

        self.assertEqual(
            normalized[0]["content"], "[subagent_subagent]: Analyze AGENTS.md")
        self.assertEqual(
            normalized[1]["content"],
            "[subagent_subagent]: Review the Source/ directory")
        self.assertEqual(
            normalized[2]["content"], "[subagent_researcher]: Found some data")


if __name__ == "__main__":
    unittest.main()
