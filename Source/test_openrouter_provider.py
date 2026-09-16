"""OpenRouter curation, routed context variants, and both desktop wire formats."""
import copy
import json
from pathlib import Path
import unittest

from bridge_core import SLOTS
from codex_catalogue import project_codex, catalogue_digest
from hub_config import defaults, project_catalogue
from providers import ProviderError, discover, prepare_request, validate_connection
from openrouter_provider import (BASE_URL, MODELS_URL, CURATED, OpenRouterError, finalize, normalize_messages,
                                 normalized_effort, reasoning_axis, _entries)
from catalogue_lifecycle import catalogue_fingerprint
import test_gateway_hub as fixtures
import test_responses_native as native_fixtures


MODEL = "z-ai/glm-5.2"


def card(identifier=MODEL):
    return {"id": identifier, "architecture": {"input_modalities": ["text"], "output_modalities": ["text"]},
            "supported_parameters": ["tools", "tool_choice", "reasoning", "reasoning_effort"],
            "context_length": 1048576,
            "reasoning": {"mandatory": False, "supported_efforts": ["high", "xhigh"], "default_effort": "high"}}


def endpoint(tag, context, output=8192):
    return {"tag": tag, "model_id": MODEL, "context_length": context, "max_completion_tokens": output,
            "supported_parameters": ["tools", "tool_choice", "reasoning", "reasoning_effort"], "status": 0}


def catalogue(endpoints=None, cards=None):
    if endpoints is None:
        endpoints = [endpoint("host-a/large", 1048576), endpoint("host-b", 262144), endpoint("host-b/large", 1048576)]
    return discover("openrouter", {}, "test-key", transport=lambda plan:
                    {"data": cards if cards is not None else [card()]} if plan["url"] == MODELS_URL else
                    {"data": {"id": MODEL, "endpoints": endpoints}})


def spec(context=262144):
    return next(row for row in catalogue()["models"] if row["context"] == context)


class OpenRouterProviderTests(unittest.TestCase):
    def test_official_endpoint_and_bearer_contract(self):
        for url in (BASE_URL, BASE_URL + "/v1", BASE_URL + "/v1/messages", BASE_URL + "/v1/responses"):
            self.assertEqual(validate_connection("openrouter", {"base_url": url})["base_url"], BASE_URL)
        for url in ("http://openrouter.ai/api", "https://evil.test/api", BASE_URL + "?key=secret"):
            with self.subTest(url=url), self.assertRaises(ProviderError):
                validate_connection("openrouter", {"base_url": url})
        with self.assertRaises(ProviderError):
            discover("openrouter", {}, None, transport=lambda _: {})

    def test_curated_membership_and_context_variants_keep_actual_upstream_id(self):
        inventory = catalogue(cards=[card(), card("unlisted/vendor"), {"id": []}])
        self.assertEqual(len(inventory["models"]), 2)
        large, small = inventory["models"]
        self.assertEqual(large["id"], MODEL)
        self.assertEqual(small["id"], MODEL + "/context-262144")
        self.assertEqual(small["upstream_model_id"], MODEL)
        self.assertEqual(small["context"], 262144)
        self.assertEqual(small["routing_ignore"], ["host-b/large"])
        self.assertEqual(large["routing_ignore"], [])
        self.assertEqual(large["effort_modes"], ["none", "high", "xhigh"])
        settings = defaults(SLOTS, "unused")
        rows = project_catalogue("openrouter", inventory, settings)
        models = project_codex(settings, {"models": rows})["models"]
        self.assertEqual({m["context_window"] for m in models}, {1048576, 262144})
        # Composer labels carry a provider suffix only on collision. These
        # context variants already have distinct upstream names, so they
        # stand alone exactly as the Composer will render them.
        self.assertEqual({m["display_name"] for m in models},
                         {"GLM 5.2 · 1,048,576 context", "GLM 5.2 · 262,144 context"})

    def test_unsupported_nested_endpoints_cannot_escape_base_slug_routing(self):
        rows = catalogue(endpoints=[endpoint("host", 262144), endpoint("host/fast", 1048576),
                                    {**endpoint("host/no-tools", 1048576), "supported_parameters": []}])["models"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["routing_ignore"], ["host/fast", "host/no-tools"])
        self.assertEqual(catalogue(endpoints=[endpoint("same", 262144), endpoint("same", 1048576)])["models"], [])

    def test_missing_metadata_never_copies_aggregate_context_onto_unbound_hosts(self):
        def fetch(plan):
            if plan["url"] == MODELS_URL:
                return {"data": [card()]}
            raise ProviderError("metadata unavailable")
        row = discover("openrouter", {}, "key", transport=fetch)["models"][0]
        self.assertIsNone(row["context"])
        self.assertIsNone(row["max_output"])
        self.assertEqual(row["routing_endpoints"], [])
        self.assertEqual(catalogue(cards=[])["models"], [])

    def test_native_messages_controls_routing_and_sticky_session(self):
        metadata = spec()
        body = {"model": "claude-sonnet-5", "max_tokens": 1024, "messages": [{"role": "user", "content": "hello"}],
                "thinking": {"type": "adaptive"}, "output_config": {"effort": "low"}}
        original = copy.deepcopy(body)
        first = prepare_request("openrouter", {}, "secret", body, metadata["id"], metadata)
        self.assertEqual(body, original)
        self.assertEqual(first["url"], BASE_URL + "/v1/messages")
        self.assertEqual(first["headers"]["Authorization"], "Bearer secret")
        self.assertEqual(first["body"]["model"], MODEL)
        self.assertEqual(first["body"]["output_config"]["effort"], "high")
        self.assertEqual(first["body"]["provider"], {"require_parameters": False, "only": ["host-b"], "ignore": ["host-b/large"]})
        body["messages"].append({"role": "assistant", "content": "hello again"})
        second = prepare_request("openrouter", {}, "secret", body, metadata["id"], metadata)
        self.assertEqual(first["body"]["session_id"], second["body"]["session_id"])
        self.assertNotIn("hello", first["body"]["session_id"])

    def test_actual_effort_ladders_and_explicit_unsupported_options(self):
        self.assertEqual(normalized_effort("max", spec()), "xhigh")
        with self.assertRaises(OpenRouterError):
            normalized_effort("none", {"effort_modes": ["high"], "reasoning_mandatory": True})
        for changes in ({"service_tier": "priority"}, {"store": True}, {"previous_response_id": "foreign"},
                        {"provider": {"only": ["foreign-host"]}}, {"models": ["other/model"]}):
            with self.subTest(changes=changes), self.assertRaises(OpenRouterError):
                finalize({"input": "hello", **changes}, spec(), "key", responses=True)
        body = {"input": "hello", "store": False, "reasoning": {"effort": "none"}}
        finalize(body, spec(), "key", responses=True)
        self.assertEqual(body["reasoning"], {"enabled": False})

    def test_reasoning_off_metadata_and_conflicts(self):
        for metadata in ({"supported_efforts": ["none", "high"]}, {"default_enabled": False}):
            row = _entries({**card(), "reasoning": metadata}, {"data": {"endpoints": [endpoint("host", 8192)]}})[0]
            self.assertIn("none", row["effort_modes"])
        restricted = {**spec(), "effort_modes": ["high"], "reasoning_mandatory": None}
        with self.assertRaises(OpenRouterError):
            finalize({"input": "hi", "reasoning": {"enabled": False}}, restricted, "key", responses=True)
        for controls in ({"enabled": False, "effort": "high"}, {"enabled": True, "effort": "none"}):
            with self.assertRaises(OpenRouterError):
                finalize({"input": "hi", "reasoning": controls}, spec(), "key", responses=True)
        with self.assertRaises(ProviderError):
            prepare_request("openrouter", {}, "key", {"messages": [{"role": "user", "content": "hi"}],
                            "thinking": {"type": "disabled"}}, restricted["id"], restricted)

    def test_structural_defaults_do_not_exclude_fugu_or_inkling_and_sessions_are_stable(self):
        model = card("sakana/fugu-max")
        params = ["reasoning", "reasoning_effort", "tools"]
        row = _entries(model, {"data": {"id": model["id"], "endpoints": [{**endpoint("sakana", 1000000),
                       "model_id": model["id"], "supported_parameters": params}]}})[0]
        self.assertIsNone(row["max_input"])
        for responses in (False, True):
            body = ({"input": "hello", "max_output_tokens": 512, "parallel_tool_calls": False,
                     "tool_choice": "auto", "reasoning": {"effort": "high"}} if responses else
                    {"messages": [{"role": "user", "content": "hello"}], "max_tokens": 512,
                     "tool_choice": {"type": "auto"}, "output_config": {"effort": "high"}})
            body["tools"] = [{"name": "read"}]
            finalize(body, row, "key", responses=responses)
            self.assertFalse(body["provider"]["require_parameters"])
            self.assertNotIn("tool_choice", body)
            if responses:
                second = {"input": [{"role": "user", "content": [{"type": "input_text", "text": "hello"}]}]}
                finalize(second, row, "key", responses=True)
                self.assertEqual(body["session_id"], second["session_id"])

    def test_stealth_union_alpha_is_curated_by_id_and_carries_the_stealth_gold(self):
        identifier = "stealth/union-alpha"
        self.assertEqual(CURATED[identifier], "Union Alpha")
        model = card(identifier)
        rows = _entries(model, {"data": {"id": identifier, "endpoints": [
            {**endpoint("stealth", 262144, 131072), "model_id": identifier}]}})
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["upstream_model_id"], identifier)
        self.assertEqual((rows[0]["context"], rows[0]["max_output"]), (262144, 131072))
        settings = defaults(SLOTS, "unused")
        projected = project_catalogue("openrouter", {"models": rows, "source": "provider_api"}, settings)[0]
        self.assertEqual(projected["id"], "openrouter/" + identifier)
        self.assertEqual(projected["display_name"], "Union Alpha")
        # An anonymous upstream brand is presentation only: the connection,
        # the key and the bill stay OpenRouter's.
        self.assertEqual(projected["presentation"]["runtimeProvider"], "openrouter")
        self.assertEqual(projected["presentation"]["displayProvider"], "Stealth")
        self.assertEqual(projected["presentation"]["accent"], "#9E6C00")
        body = {"input": "hello", "tools": [{"name": "read"}]}
        finalize(body, rows[0], "key", responses=True)
        self.assertEqual(body["model"], identifier)
        self.assertEqual(body["provider"]["only"], ["stealth"])

    def test_a_model_with_no_reasoning_axis_takes_reasoning_off_instead_of_refusing_it(self):
        """A model that never reasons is already switched off, so a client
        asking it not to think is satisfied, not refused. The shared provider
        layer admits `none` and a disabled thinking block for exactly these
        models; refusing them here made Union Alpha unusable from Claude
        Desktop, which sends a disabled block whenever thinking is off."""
        # Union Alpha's live card: no reasoning, reasoning_effort or include_reasoning.
        parameters = ["max_tokens", "response_format", "temperature", "tool_choice", "tools", "top_p"]
        model = {"id": "stealth/union-alpha", "context_length": 262144, "reasoning": None,
                 "architecture": {"input_modalities": ["text", "image"], "output_modalities": ["text"]},
                 "supported_parameters": parameters}
        row = _entries(model, {"data": {"id": "stealth/union-alpha", "endpoints": [
            {"tag": "stealth", "model_id": "stealth/union-alpha", "context_length": 262144,
             "max_completion_tokens": 131072, "status": 0, "supported_parameters": parameters}]}})[0]
        self.assertEqual((row["reasoning"], row["effort_modes"], row["effort_control"]), (False, [], "none"))
        self.assertFalse(reasoning_axis(row))

        body = {"model": "m", "messages": [{"role": "user", "content": "hi"}], "thinking": {"type": "disabled"}}
        self.assertEqual(normalize_messages(body, "stealth/union-alpha", row),
                         {"reasoning_control": "dropped_model_has_no_reasoning"})
        # The control is removed, not just ignored: left in place it would make
        # the endpoint selector demand a reasoning-capable host.
        self.assertEqual(body, {"model": "m", "messages": [{"role": "user", "content": "hi"}]})
        keeps = {"model": "m", "messages": [], "output_config": {"effort": "none", "verbosity": "low"}}
        normalize_messages(keeps, "stealth/union-alpha", row)
        self.assertEqual(keeps["output_config"], {"verbosity": "low"})

        responses = {"input": "hi", "store": False, "tools": [{"name": "read"}], "reasoning": {"enabled": False}}
        finalize(responses, row, "key", responses=True)
        self.assertNotIn("reasoning", responses)
        self.assertEqual(responses["provider"]["only"], ["stealth"])

        # Asking such a model to think is a real mismatch and still refused.
        for asked in ({"output_config": {"effort": "high"}}, {"thinking": {"type": "enabled"}}):
            with self.subTest(asked=asked), self.assertRaises(OpenRouterError):
                normalize_messages({"model": "m", "messages": [], **asked}, "stealth/union-alpha", row)
        with self.assertRaises(OpenRouterError):
            finalize({"input": "hi", "store": False, "reasoning": {"enabled": True}}, row, "key", responses=True)
        with self.assertRaises(OpenRouterError):
            finalize({"input": "hi", "store": False, "reasoning": {"enabled": "yes"}}, row, "key", responses=True)
        # A ladder that genuinely cannot be switched off is untouched by this.
        mandatory = {**row, "reasoning": True, "effort_modes": ["high"], "effort_control": "levels",
                     "reasoning_mandatory": True}
        self.assertTrue(reasoning_axis(mandatory))
        with self.assertRaises(OpenRouterError):
            normalize_messages({"model": "m", "messages": [], "thinking": {"type": "disabled"}}, "m", mandatory)

    def test_a_withdrawn_stealth_preview_leaves_no_route_behind(self):
        """The seven-day window is OpenRouter's to close, not ours to encode:
        membership comes from the live list, so a withdrawal removes the
        choice and says so instead of leaving a dead route selectable."""
        inventory = catalogue(cards=[card()])
        self.assertNotIn("stealth/union-alpha", {row["upstream_model_id"] for row in inventory["models"]})
        self.assertTrue(any("stealth/union-alpha" in warning for warning in inventory["warnings"]))

    def test_thinking_display_survives_and_speed_variants_are_ignored(self):
        row = catalogue(endpoints=[endpoint("host", 262144), endpoint("host/fast-us", 1048576)])["models"][0]
        self.assertEqual(row["routing_ignore"], ["host/fast-us"])
        plan = prepare_request("openrouter", {}, "key", {"messages": [{"role": "user", "content": "hi"}],
             "thinking": {"type": "adaptive", "display": "omitted", "budget_tokens": 1000},
             "output_config": {"effort": "high"}}, row["id"], row)
        self.assertEqual(plan["body"]["thinking"], {"type": "enabled", "display": "omitted"})

    def test_endpoint_and_control_changes_invalidate_the_gateway_snapshot(self):
        settings = defaults(SLOTS, "unused")
        settings["mappings"] = {slot[0]: "openrouter/" + MODEL for slot in SLOTS}
        first = catalogue()
        later = copy.deepcopy(first)
        later["models"][0]["routing_endpoints"][0]["tag"] = "replacement/large"
        one = {"models": project_catalogue("openrouter", first, settings)}
        two = {"models": project_catalogue("openrouter", later, settings)}
        self.assertNotEqual(catalogue_fingerprint(settings, Path("."), model_specs={m["id"]: m for m in one["models"]}),
                            catalogue_fingerprint(settings, Path("."), model_specs={m["id"]: m for m in two["models"]}))
        self.assertNotEqual(catalogue_digest(settings, one), catalogue_digest(settings, two))


class OpenRouterMessagesTests(unittest.TestCase):
    setUp = fixtures.GatewayHubHTTPTests.setUp
    tearDown = fixtures.GatewayHubHTTPTests.tearDown
    start_gateway = fixtures.GatewayHubHTTPTests.start_gateway
    request = fixtures.GatewayHubHTTPTests.request

    def test_claude_json_and_streaming_tool_cycles(self):
        metadata = spec()
        self.start_gateway("openrouter", metadata["id"], metadata)
        for stream in (False, True):
            body = {"model": "claude-fable-5", "max_tokens": 512, "stream": stream,
                    "messages": [{"role": "user", "content": fixtures.LOCAL_REQUEST_TEXT}],
                    "tools": [fixtures.tool_definition()], "thinking": {"type": "adaptive"}, "output_config": {"effort": "max"}}
            status, raw, _ = self.request(body)
            self.assertEqual(status, 200, raw)
            content = fixtures.reconstruct_content(fixtures.parse_sse(raw)) if stream else json.loads(raw)["content"]
            call = next(block for block in content if block["type"] == "tool_use")
            body["messages"] += [{"role": "assistant", "content": content}, {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": call["id"], "content": "green"}]}]
            status, raw, _ = self.request(body)
            self.assertEqual(status, 200, raw)
            self.assertEqual(fixtures.MockProvider.requests[-1]["messages"][1]["content"], content)
        self.assertTrue(all(row["model"] == MODEL for row in fixtures.MockProvider.requests))
        self.assertEqual(self.runtime.status()["completed"], 4)


class OpenRouterResponsesTests(unittest.TestCase):
    setUp = native_fixtures.NativeResponsesTests.setUp
    tearDown = native_fixtures.NativeResponsesTests.tearDown
    start_gateway = fixtures.GatewayHubHTTPTests.start_gateway
    request = native_fixtures.NativeResponsesTests.request

    def test_codex_native_json_and_streaming_context_variant_cycles(self):
        metadata = spec()
        route = self.start_gateway("openrouter", metadata["id"], metadata)
        for stream in (False, True):
            body = {"model": route, "input": [{"role": "user", "content": "read the fixture"}], "store": False, "stream": stream,
                    "reasoning": {"effort": "medium"}, "tools": [{"type": "function", "name": "read_file", "parameters": {"type": "object"}}]}
            status, raw = self.request(body)
            self.assertEqual(status, 200, raw)
            first = fixtures.parse_sse(raw)[-1]["response"] if stream else json.loads(raw)
            self.assertEqual(first["model"], route)
            body["input"] += first["output"] + [{"type": "reasoning", "encrypted_content": "opaque-openrouter-state"},
                {"type": "function_call_output", "call_id": "call-native", "output": "green"}]
            status, raw = self.request(body)
            self.assertEqual(status, 200, raw)
            self.assertEqual(fixtures.MockProvider.requests[-1]["input"][-2]["encrypted_content"], "opaque-openrouter-state")
        for body, headers in zip(fixtures.MockProvider.requests, fixtures.MockProvider.request_headers):
            self.assertEqual(body["model"], MODEL)
            self.assertEqual(body["provider"]["only"], ["host-b"])
            self.assertEqual(headers["authorization"], "Bearer " + fixtures.PROVIDER_KEY)
            self.assertNotIn(self.runtime.token, json.dumps(headers))
        self.assertEqual(self.runtime.status()["completed"], 4)


if __name__ == "__main__":
    unittest.main()
