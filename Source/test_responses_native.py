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


if __name__ == "__main__":
    unittest.main()
