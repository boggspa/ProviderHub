"""Native Responses HTTP tests using deterministic local upstreams only."""
import copy
import http.client
import json
import socket
import threading
import time
import unittest
from unittest.mock import patch

import test_gateway_hub as fixtures
from test_gateway_hub import MockProvider


class CustomApplyPatchUnitTests(unittest.TestCase):
    """Socket-free coverage for the custom_tool_call adapter path."""

    PATCH = "*** Begin Patch\n*** Update File: a.txt\n@@\n-old\n+new\n*** End Patch"

    def test_stream_rewrite_restores_custom_items_and_drops_json_deltas(self):
        from responses_native import rewrite_stream_event
        from responses_tools import flatten_tools
        _, tool_map = flatten_tools([{"type": "custom", "name": "apply_patch"}])
        tracked = set()
        added = {"type": "response.output_item.added", "output_index": 0, "item": {
            "type": "function_call", "id": "fc-1", "call_id": "call-1",
            "name": "apply_patch", "arguments": "", "status": "in_progress"}}
        self.assertTrue(rewrite_stream_event(added, tool_map, tracked))
        self.assertEqual(added["item"]["type"], "custom_tool_call")
        self.assertEqual(tracked, {"fc-1"})
        delta = {"type": "response.function_call_arguments.delta", "item_id": "fc-1",
                 "output_index": 0, "delta": '{"patch": "'}
        self.assertFalse(rewrite_stream_event(delta, tool_map, tracked))
        done_args = {"type": "response.function_call_arguments.done", "item_id": "fc-1",
                     "output_index": 0, "arguments": '{"patch": "x"}'}
        self.assertFalse(rewrite_stream_event(done_args, tool_map, tracked))
        # Deltas for untracked items still pass through untouched.
        other = {"type": "response.function_call_arguments.delta", "item_id": "fc-2",
                 "output_index": 1, "delta": "{}"}
        self.assertTrue(rewrite_stream_event(other, tool_map, tracked))
        # Plain namespaced tools keep the existing restore path.
        flat, namespaced = flatten_tools([
            {"type": "namespace", "name": "ws", "tools": [{"type": "function", "name": "read"}]}])
        event = {"type": "response.output_item.done", "output_index": 0, "item": {
            "type": "function_call", "id": "fc-3", "call_id": "call-3",
            "name": flat[0]["name"], "arguments": "{}"}}
        self.assertTrue(rewrite_stream_event(event, namespaced, set()))
        self.assertEqual(event["item"]["type"], "function_call")
        self.assertEqual(event["item"]["name"], "read")
        self.assertEqual(event["item"]["namespace"], "ws")

    def test_prepare_native_projects_custom_tool_and_history(self):
        from types import SimpleNamespace
        import responses_native
        from responses_tools import flatten_tools  # noqa: F401 (adapter import check)
        route = "grok/grok-4.6"
        runtime = SimpleNamespace(
            settings={"providers": {"grok": {"base_url": "https://x.invalid"}},
                      "_model_specs": {route: {"context": 500000, "vision": True,
                                               "effort_modes": ["low", "high"]}}},
            replay_key="replay", token="token", upstream_url=None,
            provider_key=lambda provider_id: "provider-key")
        payload = {"model": route, "store": False, "stream": False,
                   "tools": [{"type": "custom", "name": "apply_patch",
                              "format": {"type": "grammar", "syntax": "lark"}}],
                   "input": [{"role": "user", "content": "edit it"},
                             {"type": "custom_tool_call", "call_id": "call-1",
                              "name": "apply_patch", "input": self.PATCH},
                             {"type": "custom_tool_call_output", "call_id": "call-1",
                              "output": "applied"}]}
        with patch.object(responses_native, "validate_connection",
                          return_value={"base_url": "https://x.invalid"}), \
                patch.object(responses_native, "_auth_headers", return_value={}), \
                patch.object(responses_native, "connection_signature", return_value="sig"):
            plan = responses_native.prepare_native(runtime, copy.deepcopy(payload))
        self.assertEqual(plan["protocol"], "responses")
        self.assertEqual(plan["body"]["tools"][0]["type"], "function")
        self.assertEqual(plan["body"]["tools"][0]["parameters"]["required"], ["patch"])
        kinds = [item.get("type") for item in plan["body"]["input"]]
        self.assertNotIn("custom_tool_call", kinds)
        self.assertNotIn("custom_tool_call_output", kinds)
        call = next(item for item in plan["body"]["input"] if item.get("type") == "function_call")
        self.assertEqual(json.loads(call["arguments"]), {"patch": self.PATCH})
        self.assertEqual(plan["tool_map"]["apply_patch"]["custom"], "apply_patch")

    GOAL_TOOLS = [
        {"type": "function", "name": "create_goal",
         "description": "Start pursuing a goal.",
         "parameters": {"type": "object", "properties": {
             "objective": {"type": "string", "description": "Required. The concrete objective to start pursuing."},
             "token_budget": {"type": "integer",
                              "description": "Positive token budget for the new goal. Omit unless explicitly requested."}},
             "required": ["objective"]}},
        {"type": "function", "name": "update_goal",
         "parameters": {"type": "object", "properties": {
             "status": {"type": "string"},
             "token_budget": {"type": "integer"}}, "required": ["status", "token_budget"]}},
        {"type": "function", "name": "get_goal",
         "parameters": {"type": "object", "properties": {}}},
    ]

    def _goal_plan(self, *, allow_budget):
        from types import SimpleNamespace
        import responses_native
        route = "grok/grok-4.6"
        runtime = SimpleNamespace(
            settings={"providers": {"grok": {"base_url": "https://x.invalid"}},
                      "codex_goal_budget": allow_budget,
                      "_model_specs": {route: {"context": 500000, "effort_modes": ["low", "high"]}}},
            replay_key="replay", token="token", upstream_url=None,
            provider_key=lambda provider_id: "provider-key")
        payload = {"model": route, "store": False, "stream": False,
                   "tools": copy.deepcopy(self.GOAL_TOOLS),
                   "input": [{"role": "user", "content": "ship it"}]}
        with patch.object(responses_native, "validate_connection",
                          return_value={"base_url": "https://x.invalid"}), \
                patch.object(responses_native, "_auth_headers", return_value={}), \
                patch.object(responses_native, "connection_signature", return_value="sig"):
            plan = responses_native.prepare_native(runtime, payload)
        return {tool["name"]: tool for tool in plan["body"]["tools"]}, payload

    def test_goal_tools_lose_their_token_budget_by_default(self):
        """A goal the model cannot budget is a goal that runs to its objective."""
        tools, payload = self._goal_plan(allow_budget=False)
        self.assertNotIn("token_budget", tools["create_goal"]["parameters"]["properties"])
        self.assertEqual(tools["create_goal"]["parameters"]["required"], ["objective"])
        # The objective and the rest of the schema are untouched.
        self.assertIn("objective", tools["create_goal"]["parameters"]["properties"])
        self.assertEqual(tools["create_goal"]["description"], "Start pursuing a goal.")
        # update_goal loses it too, and a required listing goes with the property:
        # a required name with no property is a schema some providers reject.
        self.assertNotIn("token_budget", tools["update_goal"]["parameters"]["properties"])
        self.assertEqual(tools["update_goal"]["parameters"]["required"], ["status"])
        # A goal tool that never carried a budget is left alone.
        self.assertEqual(tools["get_goal"]["parameters"]["properties"], {})
        # The caller's payload is not edited underneath it.
        self.assertIn("token_budget", payload["tools"][0]["parameters"]["properties"])

    def test_goal_tools_keep_their_token_budget_when_the_switch_is_on(self):
        tools, _ = self._goal_plan(allow_budget=True)
        self.assertIn("token_budget", tools["create_goal"]["parameters"]["properties"])
        self.assertIn("token_budget", tools["update_goal"]["parameters"]["properties"])

    def test_strip_goal_budget_tolerates_shapes_it_did_not_expect(self):
        from responses_tools import strip_goal_budget
        self.assertEqual(strip_goal_budget(None), 0)
        self.assertEqual(strip_goal_budget([None, "tool", {}]), 0)
        self.assertEqual(strip_goal_budget([{"name": "create_goal"}]), 0)
        self.assertEqual(strip_goal_budget([{"name": "create_goal", "parameters": {}}]), 0)
        self.assertEqual(strip_goal_budget([{"name": "exec_command", "parameters": {
            "properties": {"token_budget": {"type": "integer"}}}}]), 0)
        tools = [{"name": "create_goal", "parameters": {"properties": {"token_budget": {}}}}]
        self.assertEqual(strip_goal_budget(tools), 1)
        self.assertEqual(strip_goal_budget(tools), 0)


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
    if MockProvider.mode == "flaky_quota":
        # Fail twice with a provider Retry-After hint, then recover, so a
        # single client request can prove the gateway absorbs transient
        # pressure instead of forwarding it.
        with MockProvider.lock:
            seen = len(MockProvider.requests)
        if seen <= 2:
            server.send_json(429, {"error": {"message": "mock quota exhausted"}},
                             headers={"Retry-After": "0.01"})
            return
        # Fall through to normal handling below.
    if MockProvider.mode == "exhausted_quota":
        server.send_json(429, {"error": {"message": "mock monthly quota exhausted"}},
                         headers={"Retry-After": "3600"})
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

    def test_tool_result_images_are_rejected_for_a_route_without_vision(self):
        # Computer Use returns a screenshot as an input_image inside the tool
        # result, where the parts live under `output` rather than `content`.
        self.start("grok")
        self.runtime.settings["_model_specs"][self.route]["vision"] = False
        status, raw = self.request(self.body(input=[
            {"role": "user", "content": fixtures.LOCAL_REQUEST_TEXT},
            {"type": "function_call", "call_id": "call-shot", "name": "read_file", "arguments": "{}"},
            {"type": "function_call_output", "call_id": "call-shot", "output": [
                {"type": "input_text", "text": "screenshot"},
                {"type": "input_image", "image_url": "data:image/png;base64,AAAA"},
            ]},
        ]))
        self.assertEqual(status, 400, raw)
        self.assertIn(b"does not advertise image input", raw)
        self.assertEqual(len(MockProvider.requests), 0)

    def test_tool_result_images_reach_a_route_that_advertises_vision(self):
        self.start("grok")
        image = {"type": "input_image", "image_url": "data:image/png;base64,AAAA"}
        status, raw = self.request(self.body(input=[
            {"role": "user", "content": fixtures.LOCAL_REQUEST_TEXT},
            {"type": "function_call", "call_id": "call-shot", "name": "read_file", "arguments": "{}"},
            {"type": "function_call_output", "call_id": "call-shot", "output": [
                {"type": "input_text", "text": "screenshot"}, image]},
        ]))
        self.assertEqual(status, 200, raw)
        self.assertEqual(MockProvider.requests[0]["input"][2]["output"][1], image)

    def test_unsupported_tools_fields_and_providers_are_explicit(self):
        self.start()
        for changes in ({"tools": [{"type": "custom", "name": "other_freeform"}]}, {"background": True},
                        {"input": [{"type": "custom_tool_call", "name": "other_freeform"}]}, {"stream": "true"},
                        {"model": "muse/muse-spark-1.3"}):
            self.assertEqual(self.request(self.body(**changes))[0], 400)
        self.assertEqual(len(MockProvider.requests), 0)

    def test_custom_apply_patch_round_trip_projects_and_restores(self):
        self.start()
        patch = "*** Begin Patch\n*** Update File: a.txt\n@@\n-old\n+new\n*** End Patch"
        body = self.body(
            tools=[{"type": "custom", "name": "apply_patch",
                    "format": {"type": "grammar", "syntax": "lark", "definition": "start: patch"}}],
            input=[{"role": "user", "content": fixtures.LOCAL_REQUEST_TEXT},
                   {"type": "custom_tool_call", "call_id": "call-prior", "name": "apply_patch", "input": patch},
                   {"type": "custom_tool_call_output", "call_id": "call-prior", "output": "applied"}])
        status, raw = self.request(body)
        self.assertEqual(status, 200, raw)
        sent = MockProvider.requests[0]
        self.assertEqual(sent["tools"][0]["type"], "function")
        self.assertEqual(sent["tools"][0]["name"], "apply_patch")
        self.assertEqual(sent["tools"][0]["parameters"]["required"], ["patch"])
        kinds = [item.get("type") for item in sent["input"]]
        self.assertNotIn("custom_tool_call", kinds)
        self.assertNotIn("custom_tool_call_output", kinds)
        call = next(item for item in sent["input"] if item.get("type") == "function_call")
        self.assertEqual(json.loads(call["arguments"]), {"patch": patch})

    def test_streaming_apply_patch_returns_custom_tool_calls_without_json_deltas(self):
        self.start()
        patch_text = "*** Begin Patch\n*** Update File: a.txt\n@@\n-old\n+new\n*** End Patch"
        arguments = json.dumps({"patch": patch_text})

        def handler(server):
            upstream = server.read_body()
            item = {"type": "function_call", "id": "fc-patch", "call_id": "call-patch",
                    "name": "apply_patch", "arguments": arguments, "status": "completed"}
            terminal = {"id": "resp-patch", "object": "response", "created_at": 123,
                        "model": upstream["model"], "status": "completed", "output": [item],
                        "error": None, "usage": {"input_tokens": 9, "output_tokens": 3}}
            server.send_sse([
                {"type": "response.created",
                 "response": {**terminal, "status": "in_progress", "output": []}},
                {"type": "response.output_item.added", "output_index": 0,
                 "item": {**item, "arguments": ""}},
                {"type": "response.function_call_arguments.delta", "item_id": "fc-patch",
                 "output_index": 0, "delta": '{"patch": "'},
                {"type": "response.output_item.done", "output_index": 0, "item": item},
                {"type": "response.completed", "response": terminal},
            ])

        self.patch.stop()
        try:
            with patch.object(MockProvider, "do_POST", handler):
                status, raw = self.request(self.body(
                    stream=True,
                    tools=[{"type": "custom", "name": "apply_patch",
                            "format": {"type": "grammar", "syntax": "lark"}}]))
        finally:
            self.patch.start()
        self.assertEqual(status, 200, raw)
        events = fixtures.parse_sse(raw)
        kinds = [event["type"] for event in events]
        self.assertNotIn("response.function_call_arguments.delta", kinds)
        added = next(event["item"] for event in events if event["type"] == "response.output_item.added")
        done = next(event["item"] for event in events if event["type"] == "response.output_item.done")
        self.assertEqual(added["type"], "custom_tool_call")
        self.assertEqual(done["type"], "custom_tool_call")
        self.assertEqual(done["name"], "apply_patch")
        self.assertEqual(done["input"], patch_text)
        self.assertEqual(events[-1]["response"]["output"][0]["type"], "custom_tool_call")

    def test_authentication_and_browser_origin_checks_apply(self):
        self.start()
        self.assertEqual(self.request(self.body(), token="incorrect")[0], 401)
        # Browser-origin requests used to be 403-rejected, but the desktop
        # webview sends Origin on its model-discovery fetches, so the gateway
        # now CORS-enables them instead. The token check still applies: a
        # cross-site page gets a readable 401 and nothing reaches the provider.
        connection = http.client.HTTPConnection("127.0.0.1", self.gateway.server_port, timeout=6)
        connection.request("POST", "/v1/responses", json.dumps(self.body()), {
            "Authorization": "Bearer incorrect", "Content-Type": "application/json",
            "Origin": "https://example.invalid"})
        response = connection.getresponse()
        self.assertEqual(response.status, 401)
        self.assertEqual(response.getheader("Access-Control-Allow-Origin"), "https://example.invalid")
        response.read()
        connection.close()
        self.assertEqual(len(MockProvider.requests), 0)

    def test_quota_and_native_failed_response_redact_credentials(self):
        self.start()
        MockProvider.mode = "quota"
        # Sustained pressure still reaches the client, but only after the
        # gateway's own absorb-and-retry budget is spent. The delays are
        # patched down; real backoff timing is unit-covered in test_rate_limit.
        with patch("rate_limit.BACKOFF_BASE", 0.01), \
                patch("rate_limit.BACKOFF_CAP", 0.05), \
                patch("rate_limit.random.uniform", return_value=0):
            status, raw = self.request(self.body())
        self.assertEqual(status, 429)
        self.assertNotIn(fixtures.PROVIDER_KEY, raw.decode())
        MockProvider.mode = "failed"
        for stream in (False, True):
            status, raw = self.request(self.body(stream=stream))
            self.assertEqual(status, 200)
            self.assertNotIn(fixtures.PROVIDER_KEY, raw.decode())
        self.assertEqual(self.runtime.status()["failed"], 3)

    def raw_request(self, body, timeout=10):
        connection = http.client.HTTPConnection("127.0.0.1", self.gateway.server_port, timeout=timeout)
        connection.request("POST", "/v1/responses", json.dumps(body), {
            "Authorization": "Bearer " + self.runtime.token, "Content-Type": "application/json"})
        response = connection.getresponse()
        status, raw = response.status, response.read()
        headers = {key.lower(): value for key, value in response.getheaders()}
        connection.close()
        deadline = time.monotonic() + 2
        while self.runtime.status()["active"] and time.monotonic() < deadline:
            time.sleep(.01)
        return status, raw, headers

    def test_transient_quota_is_absorbed_with_backoff(self):
        self.start("ollama")
        MockProvider.mode = "flaky_quota"
        with patch("rate_limit.random.uniform", return_value=0):
            status, raw, _ = self.raw_request(self.body())
        self.assertEqual(status, 200, raw)
        self.assertEqual(len(MockProvider.requests), 3)
        self.assertEqual(self.runtime.status()["completed"], 1)
        self.assertEqual(self.runtime.status()["failed"], 0)
        log = (self.root / "activity.jsonl").read_text()
        self.assertEqual(log.count('"event": "throttled"'), 2)
        self.assertIn('"status": 429', log)

    def test_long_retry_after_is_handed_back_not_absorbed(self):
        self.start("ollama")
        MockProvider.mode = "exhausted_quota"
        status, raw, headers = self.raw_request(self.body())
        self.assertEqual(status, 429, raw)
        # One upstream hit only: an hour-long quota wait must not pin a
        # worker slot, so the provider's own hint is passed straight back.
        self.assertEqual(len(MockProvider.requests), 1)
        self.assertEqual(headers.get("retry-after"), "3600")

    def test_sustained_quota_reaches_client_after_absorb_budget(self):
        self.start("ollama")
        MockProvider.mode = "quota"
        with patch("rate_limit.BACKOFF_BASE", 0.01), \
                patch("rate_limit.BACKOFF_CAP", 0.05), \
                patch("rate_limit.random.uniform", return_value=0):
            status, raw, headers = self.raw_request(self.body())
        self.assertEqual(status, 429)
        self.assertEqual(len(MockProvider.requests), 5)
        self.assertEqual(self.runtime.status()["failed"], 1)
        self.assertGreaterEqual(int(headers.get("retry-after", "0")), 1)
        self.assertNotIn(fixtures.PROVIDER_KEY, raw.decode())
        log = (self.root / "activity.jsonl").read_text()
        self.assertEqual(log.count('"event": "throttled"'), 4)

    def test_ninth_request_queues_for_a_slot(self):
        self.start("ollama")
        for _ in range(8):
            self.assertTrue(self.runtime.semaphore.acquire(blocking=False))
        outcome = {}

        def ninth():
            try:
                outcome["result"] = self.raw_request(self.body(), timeout=10)
            except Exception as exc:
                outcome["error"] = exc

        with patch("responses_native.SLOT_WAIT_TIMEOUT", 5):
            thread = threading.Thread(target=ninth)
            thread.start()
            time.sleep(0.5)
            # Still queued: the old fail-fast 429 would have answered by now.
            self.assertNotIn("result", outcome)
            self.assertNotIn("error", outcome)
            self.runtime.semaphore.release()
            thread.join(timeout=10)
            for _ in range(7):
                self.runtime.semaphore.release()
        self.assertNotIn("error", outcome)
        status, raw, _ = outcome["result"]
        self.assertEqual(status, 200, raw)

    def test_slot_wait_expiry_returns_retry_after(self):
        self.start("ollama")
        for _ in range(8):
            self.assertTrue(self.runtime.semaphore.acquire(blocking=False))
        try:
            with patch("responses_native.SLOT_WAIT_TIMEOUT", 0.2):
                status, raw, headers = self.raw_request(self.body())
        finally:
            for _ in range(8):
                self.runtime.semaphore.release()
        self.assertEqual(status, 429)
        self.assertEqual(json.loads(raw)["error"]["type"], "rate_limit_error")
        self.assertGreaterEqual(int(headers.get("retry-after", "0")), 1)

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

    def test_a_schema_ollama_cannot_compile_is_repaired_instead_of_failing_the_turn(self):
        """llama.cpp compiles a json_schema format into a GBNF grammar, and an
        empty alternation becomes a rule with no body that its own parser then
        refuses: the turn dies as "failed to parse grammar", naming neither the
        schema nor the keyword. Measured against a local GGUF model, anyOf,
        oneOf and type do it at any depth including through $defs, while allOf
        and enum compile cleanly. All three are vacuous - nothing satisfies an
        empty alternation - so dropping them costs no constraint the model
        could have met, and each case below turns a 400 into a 200 upstream."""
        from responses_native import prune_vacuous_schema

        schema = {"$defs": {"x": {"oneOf": []}}, "type": "object", "required": ["a"],
                  "properties": {"a": {"anyOf": []}, "b": {"type": []}, "type": {"type": "string"}}}
        repaired, repairs = prune_vacuous_schema(schema)
        self.assertEqual(repairs, 3)
        self.assertEqual(repaired["$defs"]["x"], {})
        self.assertEqual(repaired["properties"]["a"], {})
        self.assertEqual(repaired["properties"]["b"], {})
        # Under properties the keys are field names, not keywords, so a field
        # that happens to be called "type" keeps its own schema, and the
        # object's own declared type is untouched.
        self.assertEqual(repaired["properties"]["type"], {"type": "string"})
        self.assertEqual((repaired["type"], repaired["required"]), ("object", ["a"]))
        # allOf and enum are accepted upstream, so they are not rewritten.
        self.assertEqual(prune_vacuous_schema({"allOf": [], "enum": []}), ({"allOf": [], "enum": []}, 0))

    def test_ollama_gets_the_repaired_schema_and_the_caller_keeps_its_own(self):
        """The repair belongs on the Ollama branch: it is that runner's grammar
        compiler that cannot take the schema, and no other provider is rewritten
        on its behalf. Tool schemas stay out of it - the same empty anyOf in a
        tool's parameters is accepted upstream under a free, forced or required
        tool choice alike, so there is nothing there to repair."""
        from types import SimpleNamespace

        import responses_native

        route = "ollama/ornith-1.5:9b"
        runtime = SimpleNamespace(
            settings={"providers": {"ollama": {"base_url": "http://127.0.0.1:11434"}},
                      "_model_specs": {route: {"context": 131072}}},
            replay_key="replay", token="token", upstream_url=None,
            provider_key=lambda provider_id: "")
        payload = {"model": route, "store": False, "stream": False, "input": "hi",
                   "tools": [{"type": "function", "name": "cua",
                              "parameters": {"type": "object", "properties": {"arg": {"anyOf": []}}}}],
                   "text": {"format": {"type": "json_schema", "name": "out",
                                       "schema": {"type": "object", "properties": {"a": {"anyOf": []}}}}}}
        with patch.object(responses_native, "validate_connection",
                          return_value={"base_url": "http://127.0.0.1:11434"}), \
                patch.object(responses_native, "_auth_headers", return_value={}), \
                patch.object(responses_native, "connection_signature", return_value="sig"):
            plan = responses_native.prepare_native(runtime, copy.deepcopy(payload))
        self.assertEqual(plan["body"]["text"]["format"]["schema"]["properties"]["a"], {})
        self.assertEqual(plan["body"]["tools"][0]["parameters"]["properties"]["arg"], {"anyOf": []})
        # The caller's own payload is never edited underneath it.
        self.assertEqual(payload["text"]["format"]["schema"]["properties"]["a"], {"anyOf": []})

    def test_hosted_search_is_refused_by_name_where_the_provider_runs_no_search(self):
        """A route whose provider has no search of its own says exactly that,
        rather than the turn dying on the tool array as a whole. Validation
        runs before any provider or network access, so a stub runtime carrying
        the model spec is enough to reach it."""
        from types import SimpleNamespace

        from bridge_core import BridgeError
        from hub_config import qualify
        from responses_native import prepare_native

        route = qualify("mistral", "mistral-medium-2508")
        runtime = SimpleNamespace(settings={"_model_specs": {route: {}}})
        with self.assertRaises(BridgeError) as ctx:
            prepare_native(runtime, {"model": route, "input": "hi", "tools": [{"type": "web_search"}]})
        message = str(ctx.exception)
        self.assertIn("does not run web search", message)
        # Not the flatten refusal: the request was understood and answered on
        # its merits, not rejected as a shape the adapter cannot read.
        self.assertNotIn("separate adapter", message)

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

    def test_request_shape_records_attribution_not_content(self):
        """The shape log captures agent/call-id/tool names without message text."""
        import json

        from responses_native import _request_shape

        payload = {
            "model": "mistral/mistral-medium-2508",
            "input": [
                {"type": "message", "role": "user", "content": "secret prompt"},
                {"type": "multi_agent_call", "call_id": "call_1", "agent": "researcher",
                 "arguments": {"task": "secret task"}},
                {"type": "agent_message", "agent": "researcher", "role": "assistant",
                 "content": "secret findings"},
                "string_item",
            ],
            "tools": [
                {"type": "namespace", "name": "collaboration",
                 "tools": [{"type": "function", "name": "spawn_agent"}]},
                {"type": "function", "name": "read_file"},
            ],
            "tool_choice": "auto",
        }
        shape = _request_shape(payload)

        self.assertEqual(shape["model"], "mistral/mistral-medium-2508")
        self.assertEqual(shape["input_types"],
                         ["message", "multi_agent_call", "agent_message", "str"])
        self.assertEqual(shape["input_items"][1],
                         {"type": "multi_agent_call", "agent": "researcher",
                          "call_id": "call_1"})
        self.assertEqual(shape["input_items"][2],
                         {"type": "agent_message", "agent": "researcher",
                          "role": "assistant"})
        self.assertEqual(shape["tool_names"],
                         ["collaboration.spawn_agent", "read_file"])
        self.assertEqual(shape["tool_choice"], "auto")
        blob = json.dumps(shape)
        for secret in ("secret prompt", "secret task", "secret findings"):
            self.assertNotIn(secret, blob)


if __name__ == "__main__":
    unittest.main()
