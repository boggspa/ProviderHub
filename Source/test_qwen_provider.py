"""Token Plan identity, metadata and complete desktop tool cycles; no API spend."""
import copy
import http.client
import json
import unittest
from unittest.mock import patch

import test_gateway_hub as fixtures
import test_responses_bridge as responses_fixtures
from bridge_core import SLOTS
from codex_catalogue import project_codex
from hub_config import defaults, normalize, project_catalogue
from providers import PROVIDERS, ProviderError, discover, prepare_request, validate_connection
from qwen_provider import BASE_URL

IMAGE_URL = ("data:image/png;base64,"
             "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aX1cAAAAASUVORK5CYII=")
IMAGE = {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                      "data": IMAGE_URL.split(",", 1)[1]}}


def spec(model_id="qwen3.8-max"):
    return next(row for row in discover("qwen-token-plan", {}, None)["models"] if row["id"] == model_id)


def payload(**changes):
    value = {"model": "claude-sonnet-5", "max_tokens": 1024, "stream": False,
             "messages": [{"role": "user", "content": "Inspect the file."}],
             "tools": [fixtures.tool_definition()], "output_config": {"effort": "high"},
             "thinking": {"type": "adaptive", "display": "omitted"}}
    value.update(changes)
    return value


class QwenProviderTests(unittest.TestCase):
    def test_only_token_plan_endpoints_and_dedicated_credentials(self):
        provider = PROVIDERS["qwen-token-plan"]
        self.assertEqual(provider["credential_account"], "QWEN_TOKEN_PLAN_API_KEY")
        for url in (BASE_URL, BASE_URL + "/v1/messages", BASE_URL.replace("/apps/anthropic", "/compatible-mode/v1")):
            self.assertEqual(validate_connection("qwen-token-plan", {"base_url": url})["base_url"], BASE_URL)
        for url in ("https://coding-intl.dashscope.aliyuncs.com/apps/anthropic",
                    "https://dashscope-intl.aliyuncs.com/apps/anthropic",
                    BASE_URL.replace("https:", "http:"), BASE_URL + "?api_key=secret",
                    BASE_URL.replace(".com/", ".com.evil.test/")):
            with self.subTest(url=url), self.assertRaises(ProviderError):
                validate_connection("qwen-token-plan", {"base_url": url})
        plan = prepare_request("qwen-token-plan", {}, "sk-sp-test", payload(), "qwen3.8-max", spec())
        self.assertEqual(plan["url"], BASE_URL + "/v1/messages")
        self.assertEqual(plan["headers"]["x-api-key"], "sk-sp-test")
        self.assertNotIn("authorization", {name.lower() for name in plan["headers"]})
        with self.assertRaises(ProviderError):
            prepare_request("qwen-token-plan", {}, None, payload(), "qwen3.8-max", spec())

    def test_documented_roster_metadata_and_existing_settings_migrate(self):
        inventory = discover("qwen-token-plan", {}, None, transport=lambda _: self.fail("No undocumented discovery endpoint"))
        self.assertEqual(len(inventory["models"]), 6)
        limits = {row["id"]: row["context"] for row in inventory["models"]}
        self.assertEqual(limits["qwen3.8-max"], 1000000)
        self.assertEqual(limits["qwen3.8-flash"], 1000000)
        flash = next(row for row in inventory["models"] if row["id"] == "qwen3.8-flash")
        self.assertEqual(flash["max_output"], 131072)
        self.assertEqual(flash["context_kind"], "verified_documentation")
        self.assertIn("qwen3-8-flash", flash["limit_evidence"]["source"])
        self.assertEqual(spec()["limit_evidence"]["kind"], "catalogue_snapshot; not a new inference test")
        self.assertTrue(all(row["inference_status"] == "advertised" for row in inventory["models"]))
        settings = defaults(SLOTS, "mistral-small-latest")
        del settings["providers"]["qwen-token-plan"]
        settings = normalize(settings, SLOTS, "mistral-small-latest")
        self.assertEqual(settings["providers"]["qwen-token-plan"]["base_url"], BASE_URL)
        projected = project_catalogue("qwen-token-plan", inventory, settings)
        models = project_codex(settings, {"models": projected})["models"]
        current = next(model for model in models if model["slug"] == "qwen-token-plan/qwen3.8-max")
        self.assertEqual(current["context_window"], 1000000)
        self.assertEqual(current["default_reasoning_level"], "xhigh")
        self.assertEqual([entry["effort"] for entry in current["supported_reasoning_levels"]], ["none", "low", "medium", "xhigh", "ultra"])

    def test_effort_translation_and_thinking_disable(self):
        for requested, expected in (("low", "low"), ("medium", "medium"), ("high", "xhigh"), ("max", "xhigh")):
            source = payload(output_config={"effort": requested})
            original = copy.deepcopy(source)
            plan = prepare_request("qwen-token-plan", {}, "key", source, "qwen3.8-max", spec())
            self.assertEqual(plan["body"]["output_config"]["effort"], expected)
            self.assertEqual(plan["body"]["thinking"], {"type": "enabled"})
            self.assertEqual(source, original)
        disabled = prepare_request("qwen-token-plan", {}, "key", payload(thinking={"type": "disabled"}, output_config={"effort": "none"}), "qwen3.8-max", spec())
        self.assertEqual(disabled["body"]["thinking"], {"type": "disabled"})
        self.assertNotIn("output_config", disabled["body"])
        earlier = prepare_request("qwen-token-plan", {}, "key", payload(), "qwen3.7-plus", spec("qwen3.7-plus"))
        self.assertNotIn("output_config", earlier["body"])
        self.assertEqual(earlier["body"]["thinking"], {"type": "enabled"})

    def test_conflicting_and_unsupported_controls_fail_explicitly(self):
        for changes in ({"speed": "fast"}, {"service_tier": "priority"}, {"service_tier": "flex"},
                        {"thinking": {"type": "disabled"}, "output_config": {"effort": "high"}}):
            with self.subTest(changes=changes), self.assertRaises(ProviderError):
                prepare_request("qwen-token-plan", {}, "key", payload(**changes), "qwen3.8-max", spec())

    def test_image_tool_results_keep_text_ids_and_image_order(self):
        source = payload(messages=[
            {"role": "user", "content": "Inspect two screenshots."},
            {"role": "assistant", "content": [
                {"type": "tool_use", "id": "shot_a", "name": "screenshot", "input": {}},
                {"type": "tool_use", "id": "shot_b", "name": "screenshot", "input": {}}]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "shot_a", "content": [
                    {"type": "text", "text": "First screen"}, copy.deepcopy(IMAGE),
                    {"type": "text", "text": "after capture"}]},
                {"type": "tool_result", "tool_use_id": "shot_b", "content": [copy.deepcopy(IMAGE)]},
                {"type": "text", "text": "Compare them."}]},
        ])
        original = copy.deepcopy(source)
        plan = prepare_request("qwen-token-plan", {}, "key", source, "qwen3.8-max", spec())
        self.assertEqual(source, original)
        blocks = plan["body"]["messages"][-1]["content"]
        self.assertEqual([block["type"] for block in blocks],
                         ["tool_result", "image", "tool_result", "image", "text"])
        self.assertEqual([blocks[0]["tool_use_id"], blocks[2]["tool_use_id"]], ["shot_a", "shot_b"])
        self.assertTrue(blocks[0]["content"].startswith("First screen\nafter capture\n"))
        self.assertIn("image from this tool result", blocks[0]["content"])
        self.assertIn("image from this tool result", blocks[2]["content"])
        self.assertEqual(blocks[1], IMAGE)
        self.assertEqual(blocks[3], IMAGE)
        self.assertEqual(blocks[4]["text"], "Compare them.")
        text_only = prepare_request("qwen-token-plan", {}, "key", source,
                                    "qwen3.7-max", spec("qwen3.7-max"))
        text_only_blocks = text_only["body"]["messages"][-1]["content"]
        self.assertEqual([block["type"] for block in text_only_blocks],
                         ["tool_result", "tool_result", "text"])
        self.assertTrue(all("omitted" in block["content"] for block in text_only_blocks[:2]))
        self.assertEqual([block["tool_use_id"] for block in text_only_blocks[:2]], ["shot_a", "shot_b"])
        with self.assertRaisesRegex(ProviderError, "does not advertise image input"):
            prepare_request("qwen-token-plan", {}, "key",
                            payload(messages=[{"role": "user", "content": [copy.deepcopy(IMAGE)]}]),
                            "qwen3.7-max", spec("qwen3.7-max"))


class QwenGatewayTests(unittest.TestCase):
    setUp = fixtures.GatewayHubHTTPTests.setUp
    tearDown = fixtures.GatewayHubHTTPTests.tearDown
    start_gateway = fixtures.GatewayHubHTTPTests.start_gateway
    request = fixtures.GatewayHubHTTPTests.request

    def exercise(self, stream):
        self.start_gateway("qwen-token-plan", "qwen3.8-max", spec())
        body = payload(stream=stream)
        status, raw, _ = self.request(body)
        self.assertEqual(status, 200, raw)
        first = fixtures.reconstruct_content(fixtures.parse_sse(raw)) if stream else json.loads(raw)["content"]
        call = next(block for block in first if block["type"] == "tool_use")
        body["messages"] += [{"role": "assistant", "content": first}, {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": call["id"], "content": [{"type": "text", "text": "green"}, {"type": "text", "text": "verified"}]}]}]
        status, raw, _ = self.request(body)
        self.assertEqual(status, 200, raw)
        self.assertEqual(fixtures.MockProvider.requests[1]["messages"][1]["content"], first)
        self.assertEqual(fixtures.MockProvider.requests[1]["messages"][2]["content"][0]["content"], "green\nverified")
        self.assertEqual(self.runtime.status()["completed"], 2)
        for headers in fixtures.MockProvider.request_headers:
            self.assertEqual(headers["x-api-key"], fixtures.PROVIDER_KEY)
            self.assertNotIn(self.runtime.token, json.dumps(headers))
        self.assertNotIn("Inspect the file.", (self.root / "activity.jsonl").read_text())

    def test_claude_json_tool_cycle(self):
        self.exercise(False)

    def test_claude_streaming_tool_cycle(self):
        self.exercise(True)

    def test_codex_json_and_streaming_tool_cycles(self):
        with patch.dict(responses_fixtures.MODELS, {"qwen-token-plan": ("qwen3.8-max", ["none", "low", "medium", "xhigh"])}):
            for stream in (False, True):
                with self.subTest(stream=stream):
                    responses_fixtures.ResponsesBridgeTests().exercise("qwen-token-plan", stream)

    def exercise_codex_image_tool_result(self, model_id):
        model = spec(model_id)
        route = self.start_gateway("qwen-token-plan", model_id, model)

        def request(body):
            client = http.client.HTTPConnection("127.0.0.1", self.gateway.server_port, timeout=8)
            client.request("POST", "/v1/responses", json.dumps(body), {
                "Authorization": "Bearer " + self.runtime.token, "Content-Type": "application/json"})
            response = client.getresponse()
            status, raw = response.status, response.read()
            client.close()
            return status, raw

        body = {"model": route, "input": [{"role": "user", "content": "Inspect the screen."}],
                "max_output_tokens": 512, "stream": False, "store": False,
                "tools": [{"type": "function", "name": "read_file", "parameters": {"type": "object"}}]}
        status, raw = request(body)
        self.assertEqual(status, 200, raw)
        first = json.loads(raw)
        call = next(item for item in first["output"] if item["type"] == "function_call")
        body["input"] += first["output"] + [{"type": "function_call_output",
                                              "call_id": call["call_id"], "output": [
            {"type": "input_text", "text": "Current screen"},
            {"type": "input_image", "image_url": IMAGE_URL}]}]
        status, raw = request(body)
        self.assertEqual(status, 200, raw)
        second = json.loads(raw)
        sent = fixtures.MockProvider.requests[1]["messages"][2]["content"]
        self.assertEqual([block["type"] for block in sent],
                         ["tool_result", "image"] if model["vision"] else ["tool_result"])
        self.assertEqual(sent[0]["tool_use_id"], call["call_id"])
        self.assertTrue(sent[0]["content"].startswith("Current screen\n"))
        if model["vision"]:
            self.assertEqual(sent[1], IMAGE)
        else:
            self.assertIn("omitted because this Qwen model does not advertise image input", sent[0]["content"])
        body["input"] += second["output"] + [{"role": "user", "content": "What next?"}]
        status, raw = request(body)
        self.assertEqual(status, 200, raw)
        self.assertEqual(json.loads(raw)["status"], "completed")
        self.assertEqual(fixtures.MockProvider.requests[2]["messages"][2]["content"], sent)
        self.assertEqual(self.runtime.status()["completed"], 3)

    def test_codex_image_tool_result_can_continue_on_later_turns(self):
        self.exercise_codex_image_tool_result("qwen3.8-max")

    def test_text_only_codex_route_can_continue_after_image_tool_result(self):
        self.exercise_codex_image_tool_result("qwen3.7-max")


if __name__ == "__main__":
    unittest.main()
