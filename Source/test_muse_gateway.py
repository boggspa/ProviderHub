"""Meta Model API integration through the local Messages gateway, with mocks."""
import json
import unittest

import test_gateway_hub as gateway_fixtures
from test_gateway_hub import (
    LOCAL_REQUEST_TEXT, MockProvider,
    parse_sse, reconstruct_content, tool_definition,
)


class MuseGatewayTests(unittest.TestCase):
    # Reuse only fixture utilities; do not inherit/re-run unrelated test cases.
    setUp = gateway_fixtures.GatewayHubHTTPTests.setUp
    tearDown = gateway_fixtures.GatewayHubHTTPTests.tearDown
    start_gateway = gateway_fixtures.GatewayHubHTTPTests.start_gateway
    request = gateway_fixtures.GatewayHubHTTPTests.request
    assert_upstream_secret_boundary = gateway_fixtures.GatewayHubHTTPTests.assert_upstream_secret_boundary

    def exercise_tool_cycle(self, stream):
        route = self.start_gateway("muse", "muse-spark-1.3", {
            "context": 1048576, "max_output": 131072,
            "effort_modes": ["minimal", "low", "medium", "high", "xhigh", "max"],
            "reasoning_history": "native",
        })
        requested = "claude-fable-5[1m]"
        payload = {
            "model": requested, "max_tokens": 512, "stream": stream,
            "messages": [{"role": "user", "content": LOCAL_REQUEST_TEXT}],
            "thinking": {"type": "adaptive", "display": "omitted"},
            "output_config": {"effort": "max"},
            "tools": [tool_definition()],
        }
        status, raw, _ = self.request(payload, extra_headers={
            "anthropic-version": "2023-06-01",
            "anthropic-beta": "interleaved-thinking-2025-05-14",
            "x-api-key": "client-key-must-not-forward",
        })
        self.assertEqual(status, 200, raw)
        if stream:
            events = parse_sse(raw)
            self.assertEqual(events[0]["message"]["model"], requested)
            self.assertEqual(events[-1]["type"], "message_stop")
            content = reconstruct_content(events)
        else:
            first = json.loads(raw)
            self.assertEqual(first["model"], requested)
            self.assertEqual(first["stop_reason"], "tool_use")
            content = first["content"]
        self.assertEqual(content[0]["type"], "thinking")
        self.assertEqual(content[0]["signature"], "native-provider-signature")
        call = next(block for block in content if block["type"] == "tool_use")
        payload["messages"] += [
            {"role": "assistant", "content": content},
            {"role": "user", "content": [{
                "type": "tool_result", "tool_use_id": call["id"], "content": "colour=green",
            }]},
        ]
        status, raw, _ = self.request(payload)
        self.assertEqual(status, 200, raw)
        if stream:
            self.assertEqual(parse_sse(raw)[-1]["type"], "message_stop")
        else:
            self.assertEqual(json.loads(raw)["stop_reason"], "end_turn")
        self.assertEqual(len(MockProvider.requests), 2)
        for request in MockProvider.requests:
            self.assertEqual(request["model"], "muse-spark-1.3")
            self.assertEqual(request["output_config"]["effort"], "max")
            self.assertEqual(request["thinking"], {"type": "adaptive", "display": "omitted"})
        self.assertEqual(MockProvider.requests[1]["messages"][1]["content"], content)
        self.assert_upstream_secret_boundary("muse")
        self.assertNotIn("x-api-key", MockProvider.request_headers[0])
        self.assertEqual(self.runtime.status()["last_model"], route)
        self.assertEqual(self.runtime.status()["providers"]["muse"]["completed"], 2)
        self.assertNotIn(LOCAL_REQUEST_TEXT, (self.root / "activity.jsonl").read_text())

    def test_json_tool_cycle_with_meta_auth_and_adaptive_effort(self):
        self.exercise_tool_cycle(False)

    def test_stream_tool_cycle_preserves_native_reasoning(self):
        self.exercise_tool_cycle(True)

    def test_gateway_forwards_xhigh_on_spark_13(self):
        self.start_gateway("muse", "muse-spark-1.3", {
            "context": 1048576, "max_output": 131072,
            "effort_modes": ["minimal", "low", "medium", "high", "xhigh", "max"],
            "reasoning_history": "native",
        })
        status, raw, _ = self.request({
            "model": "claude-fable-5[1m]", "max_tokens": 32, "stream": False,
            "messages": [{"role": "user", "content": LOCAL_REQUEST_TEXT}],
            "thinking": {"type": "adaptive", "display": "omitted"},
            "output_config": {"effort": "xhigh"},
        })
        self.assertEqual(status, 200, raw)
        self.assertEqual(MockProvider.requests[0]["output_config"]["effort"], "xhigh")

    def test_gateway_squeezes_max_onto_the_top_rank_the_account_lists(self):
        self.start_gateway("muse", "muse-spark-1.3-contributor", {
            "context": 524288, "max_output": 65536,
            "effort_modes": ["low", "high"],
            "reasoning_history": "native",
        })
        status, raw, _ = self.request({
            "model": "claude-fable-5", "max_tokens": 32, "stream": False,
            "messages": [{"role": "user", "content": LOCAL_REQUEST_TEXT}],
            "thinking": {"type": "adaptive", "display": "omitted"},
            "output_config": {"effort": "max"},
        })
        # The slider offers five rungs on every row whatever the account
        # lists, so a rank above the top one takes the top one rather than
        # failing the request - a refusal here would cost the client its
        # effort control for the whole session.
        self.assertEqual(status, 200, raw)
        self.assertEqual(MockProvider.requests[0]["output_config"]["effort"], "high")


if __name__ == "__main__":
    unittest.main()
