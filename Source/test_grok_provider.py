"""xAI PAYG routing, live-catalogue shapes and Claude control translation."""
import copy
import json
import unittest

from hub_config import project_catalogue
from providers import PROVIDERS, ProviderError, discover, prepare_request, validate_connection


def catalogue(transport=None):
    def default_transport(plan):
        if plan["url"].endswith("/language-models"):
            return {"models": [{"id": "grok-4.6", "aliases": ["grok-latest"],
                                "input_modalities": ["text", "image"], "output_modalities": ["text"]}]}
        return {"data": []}
    return discover("grok", {}, "provider-test-key", transport=transport or default_transport)


def payload(**changes):
    value = {"model": "claude-sonnet-5", "max_tokens": 500, "stream": False,
             "messages": [{"role": "user", "content": "Read the file."}],
             "thinking": {"type": "adaptive"}, "output_config": {"effort": "high"},
             "tools": [{"name": "read_file", "input_schema": {"type": "object"}}]}
    value.update(changes)
    return value


def plan(body, spec=None):
    return prepare_request("grok", {}, "provider-test-key", body, "grok-4.6",
                           spec if spec is not None else catalogue()["models"][0])


class GrokProviderTests(unittest.TestCase):
    def test_registry_and_endpoint_validation_keep_api_lane_separate(self):
        definition = PROVIDERS["grok"]
        self.assertEqual(definition["credential_account"], "XAI_API_KEY")
        self.assertEqual(definition["credential_env"], "XAI_API_KEY")
        self.assertEqual(definition["protocol"], "chat_completions")
        for suffix in ("", "/v1", "/v1/models", "/v1/language-models", "/v1/chat/completions"):
            self.assertEqual(validate_connection("grok", {"base_url": "https://api.x.ai" + suffix}),
                             {"region": "global", "base_url": "https://api.x.ai/v1"})
        for url in ("http://api.x.ai/v1", "https://api.x.ai.evil.example/v1", "https://user:key@api.x.ai/v1",
                    "https://api.x.ai:444/v1", "https://api.x.ai/v1/messages", "https://api.x.ai/v1?key=secret"):
            with self.subTest(url=url), self.assertRaises(ProviderError):
                validate_connection("grok", {"base_url": url})
        with self.assertRaises(ProviderError):
            discover("grok", {}, None, transport=lambda _: self.fail("must not request without a key"))

    def test_language_membership_context_enrichment_and_unknown_models(self):
        requests = []

        def transport(request):
            requests.append(request)
            if request["url"].endswith("/language-models"):
                return {"models": [
                    {"id": "grok-4.6", "aliases": ["latest", "grok-4.6"], "input_modalities": ["text", "image"]},
                    {"id": "future-grok", "output_modalities": ["text"], "input_modalities": ["text"]},
                    {"id": "retired", "deprecated": True},
                    {"id": "image-only", "output_modalities": ["image"]},
                    {"id": "responses-only", "capabilities": {"chat_completions": False}},
                ]}
            return {"data": [
                {"id": "grok-4.6", "context_length": 600000, "max_output_tokens": 32000},
                {"id": "future-grok", "context_length": True},
                {"id": "not-in-language-list", "context_length": 2000000},
            ]}
        result = catalogue(transport)
        models = {entry["id"]: entry for entry in result["models"]}
        self.assertEqual(set(models), {"grok-4.6", "future-grok"})
        self.assertEqual(models["grok-4.6"]["context"], 600000)
        self.assertEqual(models["grok-4.6"]["max_output"], 32000)
        self.assertEqual(models["grok-4.6"]["aliases"], ["grok-4.6", "latest"])
        self.assertTrue(models["grok-4.6"]["vision"])
        self.assertIsNone(models["future-grok"]["context"])
        self.assertFalse(models["future-grok"]["vision"])
        self.assertIsNone(models["future-grok"]["reasoning"])
        self.assertTrue(all(entry["inference_status"] == "advertised" for entry in models.values()))
        for request in requests:
            self.assertEqual(request["method"], "GET")
            self.assertEqual(request["headers"]["Authorization"], "Bearer provider-test-key")
            self.assertNotIn("anthropic-version", request["headers"])

    def test_documentation_is_exact_and_supplement_failure_does_not_block(self):
        def transport(request):
            if request["url"].endswith("/models"):
                raise ProviderError("metadata unavailable")
            return {"models": [{"id": "grok-4.6"}, {"id": "grok-4.6-custom"}]}
        result = catalogue(transport)
        models = {entry["id"]: entry for entry in result["models"]}
        self.assertEqual(models["grok-4.6"]["context"], 500000)
        self.assertEqual(models["grok-4.6"]["context_kind"], "verified_documentation")
        self.assertIsNone(models["grok-4.6-custom"]["context"])
        self.assertTrue(any("metadata was unavailable" in warning for warning in result["warnings"]))

    def test_discovery_does_not_fabricate_models_for_empty_or_bad_responses(self):
        self.assertEqual(catalogue(lambda request: {"models": [], "data": []})["models"], [])
        with self.assertRaises(ProviderError):
            catalogue(lambda request: {"data": [{"id": "grok-4.6"}]})

    def test_provider_projection_retains_brand_and_exact_route(self):
        projected = project_catalogue("grok", catalogue(), {"mappings": {}, "branding_overrides": {}})
        self.assertEqual(len(projected), 1)
        self.assertEqual(projected[0]["id"], "grok/grok-4.6")
        self.assertEqual(projected[0]["presentation"]["runtimeProvider"], "grok")
        self.assertEqual(projected[0]["presentation"]["displayProvider"], "Grok")
        self.assertEqual(projected[0]["context"], 500000)

    def test_effort_maps_to_supported_model_controls(self):
        for requested, expected in (("minimal", "low"), ("low", "low"), ("medium", "medium"),
                                    ("high", "high"), ("xhigh", "xhigh"), ("max", "xhigh"), ("ultra", "xhigh")):
            self.assertEqual(plan(payload(output_config={"effort": requested}))["body"]["reasoning_effort"], expected)
        older = {**catalogue()["models"][0], "effort_modes": ["low", "medium", "high"]}
        self.assertEqual(plan(payload(output_config={"effort": "max"}), older)["body"]["reasoning_effort"], "high")
        for change in ({"thinking": {"type": "disabled"}}, {"output_config": {"effort": "none"}},
                       {"output_config": {"effort": 1}}, {"output_config": {"effort": "invented"}},
                       {"stop_sequences": ["STOP"]}):
            with self.subTest(change=change), self.assertRaises(ProviderError):
                plan(payload(**change))

    def test_unknown_effort_is_not_fabricated_and_nonreasoning_can_run(self):
        unknown = {"reasoning": None, "effort_modes": []}
        self.assertNotIn("reasoning_effort", plan(payload(output_config={}), unknown)["body"])
        with self.assertRaises(ProviderError):
            plan(payload(), unknown)
        nonreasoning = {"reasoning": False, "effort_modes": []}
        self.assertNotIn("reasoning_effort", plan(payload(thinking={"type": "disabled"}, output_config={}), nonreasoning)["body"])

    def test_fast_requests_priority_and_standard_does_not(self):
        self.assertEqual(plan(payload())["body"]["service_tier"], "default")
        for changes in ({"speed": "fast"}, {"service_tier": "priority"}, {"service_tier": "fast"}):
            result = plan(payload(**changes))
            self.assertEqual(result["body"]["service_tier"], "priority")
            self.assertEqual(result["compatibility"]["requested_service_tier"], "priority")
        with self.assertRaises(ProviderError):
            plan(payload(service_tier="flex"))

    def test_multimodal_tool_history_and_cache_hint(self):
        original = payload()
        original["messages"] += [
            {"role": "assistant", "content": [{"type": "tool_use", "id": "toolu_long-id-1",
                                                "name": "read_file", "input": {"path": "file"}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_long-id-1", "is_error": True,
                                           "content": [{"type": "text", "text": "Screenshot"},
                                                       {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "YWJj"}}]}]},
        ]
        before = copy.deepcopy(original)
        result = plan(original)
        self.assertEqual(original, before)
        body = result["body"]
        self.assertEqual(body["model"], "grok-4.6")
        self.assertEqual(body["messages"][1]["tool_calls"][0]["id"], "toolu_long-id-1")
        self.assertEqual(body["messages"][2]["tool_call_id"], "toolu_long-id-1")
        self.assertTrue(body["messages"][2]["content"].startswith("Tool execution error:"))
        self.assertEqual(body["messages"][3]["content"][0]["image_url"], {"url": "data:image/png;base64,YWJj"})
        self.assertEqual(result["headers"]["x-grok-conv-id"], plan(payload())["headers"]["x-grok-conv-id"])
        self.assertNotIn("provider-test-key", result["headers"]["x-grok-conv-id"])
        self.assertEqual(result["url"], "https://api.x.ai/v1/chat/completions")
        self.assertNotIn("thinking", body)
        self.assertNotIn("output_config", body)


if __name__ == "__main__":
    unittest.main()
