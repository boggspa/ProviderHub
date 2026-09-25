"""MiniMax routing, control mapping and complete native tool cycles, offline."""
import copy
import json
import unittest
from pathlib import Path

import test_gateway_hub as fixtures
from branding import resolve_presentation
from bridge_core import SLOTS
from hub_config import defaults, normalize
from minimax_provider import BASE_URL
from providers import PROVIDERS, ProviderError, discover, prepare_request, validate_connection


def spec(model_id="MiniMax-M3"):
    return next(row for row in discover("minimax", {}, None)["models"] if row["id"] == model_id)


def payload(**changes):
    value = {"model": "claude-sonnet-5", "max_tokens": 1024, "stream": False,
             "messages": [{"role": "user", "content": "Inspect the file."}],
             "tools": [fixtures.tool_definition()], "output_config": {"effort": "high"},
             "thinking": {"type": "adaptive", "display": "omitted"}}
    value.update(changes)
    return value


class MiniMaxProviderTests(unittest.TestCase):
    def test_registry_credentials_and_existing_configuration(self):
        self.assertEqual(PROVIDERS["minimax"]["credential_account"], "MINIMAX_API_KEY")
        old = defaults(SLOTS, "mistral-small-latest")
        old["providers"].pop("minimax")
        self.assertIn("minimax", normalize(old, SLOTS, "mistral-small-latest")["providers"])
        for url in (BASE_URL, BASE_URL + "/v1/messages", "https://api.minimax.io"):
            self.assertEqual(validate_connection("minimax", {"base_url": url})["base_url"], BASE_URL)
        with self.assertRaises(ProviderError):
            validate_connection("minimax", {"base_url": "https://example.com/anthropic"})

    def test_current_roster_and_brand_match_taskwraith(self):
        self.assertEqual([m["id"] for m in discover("minimax", {}, None)["models"]],
                         ["MiniMax-M3", "MiniMax-M2.7", "MiniMax-M2.7-highspeed"])
        self.assertEqual(spec()["context"], 1000000)
        self.assertTrue(spec()["vision"])
        self.assertEqual(spec("MiniMax-M2.7-highspeed")["effort_modes"], ["high"])
        for model in ("", "MiniMax-M3", "MiniMax-M2.7", "MiniMax-M2.7-highspeed"):
            brand = resolve_presentation("minimax", model)
            self.assertEqual(brand["runtimeProvider"], "minimax")
            self.assertEqual(brand["displayProvider"], "MiniMax")
            self.assertEqual(brand["accent"], "#C044A4")
        self.assertIn(" minimax_provider ", (Path(__file__).parent / "build.sh").read_text())

    def test_m3_boolean_thinking_and_dedicated_auth(self):
        source = payload()
        original = copy.deepcopy(source)
        plan = prepare_request("minimax", {}, "subscription-key", source, "MiniMax-M3", spec())
        self.assertEqual(source, original)
        self.assertEqual(plan["url"], BASE_URL + "/v1/messages")
        self.assertEqual(plan["headers"]["x-api-key"], "subscription-key")
        self.assertEqual(plan["body"]["thinking"], {"type": "adaptive"})
        self.assertNotIn("output_config", plan["body"])
        disabled = prepare_request("minimax", {}, "key", payload(
            thinking={"type": "disabled"}, output_config={"effort": "none"}), "MiniMax-M3", spec())
        self.assertEqual(disabled["body"]["thinking"], {"type": "disabled"})
        priority = prepare_request("minimax", {}, "key", payload(service_tier="priority"), "MiniMax-M3", spec())
        self.assertEqual(priority["body"]["service_tier"], "priority")

    def test_m2_fixed_thinking_is_reported_and_highspeed_is_not_a_toggle(self):
        model = "MiniMax-M2.7-highspeed"
        plan = prepare_request("minimax", {}, "key", payload(
            thinking={"type": "disabled"}, output_config={"effort": "none"}), model, spec(model))
        self.assertNotIn("thinking", plan["body"])
        self.assertEqual(plan["compatibility"]["reasoning_effort"], "fixed_thinking_always_on")
        self.assertEqual(plan["body"]["model"], model)
        with self.assertRaisesRegex(ProviderError, "Highspeed"):
            prepare_request("minimax", {}, "key", payload(speed="fast"), model, spec(model))
        with self.assertRaisesRegex(ProviderError, "conflicting"):
            prepare_request("minimax", {}, "key", payload(thinking={"type": "disabled"}), "MiniMax-M3", spec())


class MiniMaxGatewayTests(unittest.TestCase):
    setUp = fixtures.GatewayHubHTTPTests.setUp
    tearDown = fixtures.GatewayHubHTTPTests.tearDown
    start_gateway = fixtures.GatewayHubHTTPTests.start_gateway
    request = fixtures.GatewayHubHTTPTests.request

    def exercise(self, stream):
        self.start_gateway("minimax", "MiniMax-M3", spec())
        body = payload(stream=stream)
        status, raw, _ = self.request(body)
        self.assertEqual(status, 200, raw)
        first = fixtures.reconstruct_content(fixtures.parse_sse(raw)) if stream else json.loads(raw)["content"]
        call = next(block for block in first if block["type"] == "tool_use")
        body["messages"] += [{"role": "assistant", "content": first}, {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": call["id"], "content": "green"}]}]
        status, raw, _ = self.request(body)
        self.assertEqual(status, 200, raw)
        self.assertEqual(fixtures.MockProvider.requests[1]["messages"][1]["content"], first)
        self.assertEqual(fixtures.MockProvider.requests[1]["messages"][2]["content"][0]["tool_use_id"], call["id"])

    def test_json_tool_cycle(self):
        self.exercise(False)

    def test_streaming_tool_cycle(self):
        self.exercise(True)
