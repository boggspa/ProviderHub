"""Complete Grok JSON/SSE tool cycles through the real local gateway; no API calls."""
import json
import unittest
from unittest.mock import patch

import test_gateway_hub as fixtures
from test_gateway_hub import MockProvider, parse_sse, reconstruct_content, tool_definition


def grok_reply(server, body):
    second = fixtures.has_chat_tool_result(body)
    # Deliberately return default even when priority was requested: a request
    # is not proof the provider granted that tier or billed its premium.
    if body.get("stream"):
        if second:
            events = [{"choices": [{"index": 0, "delta": {"content": "File checked."}, "finish_reason": "stop"}]}]
        else:
            events = [
                {"choices": [{"index": 0, "delta": {"tool_calls": [
                    {"index": 0, "id": "grok-call-one", "function": {"name": "read_file", "arguments": '{"path":'}},
                    {"index": 1, "id": "grok-call-two", "function": {"name": "read_file", "arguments": '{"path":'}},
                ]}}]},
                {"choices": [{"index": 0, "delta": {"tool_calls": [
                    {"index": 1, "function": {"arguments": '"two.txt"}'}},
                    {"index": 0, "function": {"arguments": '"one.txt"}'}},
                ]}, "finish_reason": "tool_calls"}]},
            ]
        events += [{"choices": [], "usage": {"prompt_tokens": 20, "completion_tokens": 6}, "service_tier": "default"}]
        server.send_sse(events, chat=True)
    else:
        message = {"role": "assistant", "content": "File checked." if second else ""}
        if not second:
            message["tool_calls"] = [{"id": "grok-call-one", "type": "function", "function": {
                "name": "read_file", "arguments": '{"path":"one.txt"}'}}]
        server.send_json(200, {"id": "grok-response", "model": "grok-4.6", "service_tier": "default",
                              "choices": [{"index": 0, "message": message, "finish_reason": "stop" if second else "tool_calls"}],
                              "usage": {"prompt_tokens": 20, "completion_tokens": 6}})


class GrokGatewayTests(unittest.TestCase):
    setUp = fixtures.GatewayHubHTTPTests.setUp
    tearDown = fixtures.GatewayHubHTTPTests.tearDown
    start_gateway = fixtures.GatewayHubHTTPTests.start_gateway
    request = fixtures.GatewayHubHTTPTests.request
    assert_upstream_secret_boundary = fixtures.GatewayHubHTTPTests.assert_upstream_secret_boundary

    def exercise(self, stream):
        self.start_gateway("grok", "grok-4.6", {"context": 500000, "effort_modes": ["low", "medium", "high", "xhigh"]})
        request = {"model": "claude-fable-5", "max_tokens": 512, "stream": stream, "speed": "fast",
                   "messages": [{"role": "user", "content": fixtures.LOCAL_REQUEST_TEXT}],
                   "tools": [tool_definition()], "thinking": {"type": "adaptive"}, "output_config": {"effort": "max"}}
        with patch.object(MockProvider, "handle_cerebras", grok_reply):
            status, raw, _ = self.request(request, extra_headers={"x-api-key": "must-not-forward"})
            self.assertEqual(status, 200, raw)
            content = reconstruct_content(parse_sse(raw)) if stream else json.loads(raw)["content"]
            calls = [block for block in content if block["type"] == "tool_use"]
            self.assertEqual([call["input"]["path"] for call in calls], ["one.txt", "two.txt"] if stream else ["one.txt"])
            request["messages"] += [{"role": "assistant", "content": content},
                                    {"role": "user", "content": [{"type": "tool_result", "tool_use_id": call["id"],
                                                                   "content": "green"} for call in calls]}]
            status, raw, _ = self.request(request)
            self.assertEqual(status, 200, raw)
            if stream:
                self.assertEqual(parse_sse(raw)[-1]["type"], "message_stop")
            else:
                self.assertEqual(json.loads(raw)["stop_reason"], "end_turn")
        self.assert_upstream_secret_boundary("grok")
        for body in MockProvider.requests:
            self.assertEqual(body["model"], "grok-4.6")
            self.assertEqual(body["reasoning_effort"], "xhigh")
            self.assertEqual(body["service_tier"], "priority")
            if stream:
                self.assertEqual(body["stream_options"], {"include_usage": True})
        messages = MockProvider.requests[1]["messages"]
        self.assertEqual([message["tool_call_id"] for message in messages if message["role"] == "tool"],
                         [call["id"] for call in calls])
        activity = (self.root / "activity.jsonl").read_text()
        self.assertNotIn(fixtures.LOCAL_REQUEST_TEXT, activity)
        records = [json.loads(line) for line in activity.splitlines() if json.loads(line)["event"] == "completed"]
        self.assertEqual(len(records), 2)
        self.assertEqual([item["service_tier"] for item in records], ["default", "default"])
        self.assertEqual(sum(item["usage"]["input_tokens"] for item in records), 40)

    def test_json_round_trip(self):
        self.exercise(False)

    def test_sse_round_trip_with_interleaved_tools_and_final_usage_chunk(self):
        self.exercise(True)

    def test_provider_quota_error_preserves_status_and_redacts_key(self):
        self.start_gateway("grok", "grok-4.6", {"effort_modes": ["high"]})
        MockProvider.mode = "rate_limit"
        status, raw, _ = self.request({"model": "claude-fable-5", "max_tokens": 50,
                                       "messages": [{"role": "user", "content": "hello"}]})
        self.assertEqual(status, 429)
        self.assertNotIn(fixtures.PROVIDER_KEY, raw.decode())


if __name__ == "__main__":
    unittest.main()
