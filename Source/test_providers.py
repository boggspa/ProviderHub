import copy
import json
import unittest

from providers import (
    PROVIDERS,
    ProviderError,
    discover,
    prepare_request,
    provider_defaults,
    validate_connection,
)
from cerebras_replay import sign_thinking


def text_prompt(**changes):
    value = {
        "model": "claude-fable-5",
        "max_tokens": 2048,
        "messages": [{"role": "user", "content": "hello"}],
    }
    value.update(changes)
    return value


class RegistryTests(unittest.TestCase):
    def test_registry_is_plain_json_data_with_expected_contract(self):
        self.assertEqual(
            set(PROVIDERS),
            {"mistral", "kimi", "mimo", "ollama", "deepseek", "cerebras", "muse", "grok", "qwen-token-plan", "openrouter"},
        )
        json.loads(json.dumps(PROVIDERS))
        for provider_id, descriptor in PROVIDERS.items():
            self.assertEqual(descriptor["id"], provider_id)
            for key in (
                "name", "protocol", "default_base_url", "regions", "auth_header",
                "credential_account", "credential_env", "setup_url", "capabilities",
            ):
                self.assertIn(key, descriptor)
            self.assertIsInstance(descriptor["regions"], dict)
            self.assertIn(descriptor["default_region"], descriptor["regions"])
            if provider_id == "ollama":
                self.assertIsNone(descriptor["credential_env"])
            else:
                self.assertIsInstance(descriptor["credential_env"], str)
                self.assertTrue(descriptor["credential_env"])

    def test_defaults_are_fresh_and_contain_no_credentials(self):
        first = provider_defaults()
        second = provider_defaults()
        self.assertEqual(set(first), set(PROVIDERS))
        for provider_id, connection in first.items():
            self.assertEqual(
                connection,
                {
                    "region": PROVIDERS[provider_id]["default_region"],
                    "base_url": PROVIDERS[provider_id]["default_base_url"],
                },
            )
            self.assertFalse(any("key" in key.lower() for key in connection))
        first["mimo"]["region"] = "cn"
        self.assertEqual(second["mimo"]["region"], "ams")


class ConnectionTests(unittest.TestCase):
    def test_official_full_endpoints_normalize_to_bases(self):
        cases = [
            ("mistral", "https://api.mistral.ai/v1/chat/completions", "https://api.mistral.ai/v1"),
            ("kimi", "https://api.kimi.com/coding/v1/messages", "https://api.kimi.com/coding"),
            ("deepseek", "https://api.deepseek.com/anthropic/v1/messages", "https://api.deepseek.com/anthropic"),
            ("cerebras", "https://api.cerebras.ai/v1/models", "https://api.cerebras.ai/v1"),
        ]
        for provider_id, entered, expected in cases:
            with self.subTest(provider_id=provider_id):
                self.assertEqual(
                    validate_connection(provider_id, {"base_url": entered})["base_url"],
                    expected,
                )

    def test_mimo_region_and_endpoint_are_bound_together(self):
        expected = "https://token-plan-sgp.xiaomimimo.com/anthropic"
        self.assertEqual(
            validate_connection("mimo", {"region": "sgp"}),
            {"region": "sgp", "base_url": expected},
        )
        self.assertEqual(
            validate_connection("mimo", {"base_url": expected + "/v1/messages"}),
            {"region": "sgp", "base_url": expected},
        )
        with self.assertRaises(ProviderError):
            validate_connection("mimo", {"region": "cn", "base_url": expected})
        with self.assertRaises(ProviderError):
            validate_connection("mimo", {"region": "unknown"})

    def test_hosted_provider_destination_rejections(self):
        rejected = [
            "http://api.mistral.ai/v1",
            "https://api.mistral.ai.evil.example/v1",
            "https://evil.example/v1",
            "https://user:pass@api.mistral.ai/v1",
            "https://api.mistral.ai:444/v1",
            "https://api.mistral.ai/v1/other",
            "https://api.mistral.ai/v1?redirect=https://evil.example",
            "https://api.mistral.ai/v1#fragment",
        ]
        for value in rejected:
            with self.subTest(value=value), self.assertRaises(ProviderError):
                validate_connection("mistral", {"base_url": value})

    def test_ollama_accepts_only_literal_loopback_daemons(self):
        self.assertEqual(
            validate_connection("ollama", {"base_url": "http://localhost:2222/v1/messages"}),
            {"region": "local", "base_url": "http://127.0.0.1:2222"},
        )
        self.assertEqual(
            validate_connection("ollama", {"base_url": "http://[::1]:11434/api/tags"})["base_url"],
            "http://[::1]:11434",
        )
        for value in (
            "https://127.0.0.1:11434",
            "http://192.168.1.3:11434",
            "http://ollama.local:11434",
            "http://127.0.0.1:11434/proxy",
        ):
            with self.subTest(value=value), self.assertRaises(ProviderError):
                validate_connection("ollama", {"base_url": value})

    def test_connections_cannot_contain_plaintext_credentials(self):
        for key in ("api_key", "api-key", "Authorization", "gateway_token"):
            with self.subTest(key=key), self.assertRaises(ProviderError):
                validate_connection("mistral", {key: "secret"})
        with self.assertRaises(ProviderError):
            validate_connection("unknown", {})
        with self.assertRaises(ProviderError):
            validate_connection("mistral", "not an object")


class DiscoveryTests(unittest.TestCase):
    def test_documented_kimi_catalogue_is_honest_about_tier_dependent_k3(self):
        called = []
        result = discover("kimi", {}, None, transport=lambda plan: called.append(plan))
        self.assertEqual(called, [])
        by_id = {model["id"]: model for model in result["models"]}
        self.assertEqual(
            set(by_id),
            {"k3", "k3-256k", "kimi-for-coding", "kimi-for-coding-highspeed"},
        )
        self.assertIsNone(by_id["k3"]["context"])
        self.assertEqual(by_id["k3"]["context_options"], [262144, 1048576])
        self.assertEqual(by_id["k3-256k"]["context"], 262144)
        self.assertEqual(by_id["kimi-for-coding"]["context"], 1048576)
        self.assertFalse(by_id["kimi-for-coding-highspeed"]["fast_mode"])
        self.assertEqual(by_id["kimi-for-coding-highspeed"]["speed_tier"], "highspeed")
        self.assertEqual(result["source"], "provider_documentation")
        self.assertTrue(all(model["inference_status"] == "advertised" for model in by_id.values()))
        self.assertTrue(any("membership" in warning for warning in result["warnings"]))

    def test_documented_mimo_catalogue_has_only_messages_models(self):
        result = discover("mimo", {"region": "cn"}, None)
        by_id = {model["id"]: model for model in result["models"]}
        self.assertEqual(set(by_id), {"mimo-v2.5", "mimo-v2.5-pro"})
        self.assertTrue(all(model["context"] == 1048576 for model in by_id.values()))
        self.assertTrue(all(model["max_output"] == 131072 for model in by_id.values()))
        self.assertFalse(by_id["mimo-v2.5-pro"]["vision"])
        self.assertTrue(by_id["mimo-v2.5"]["vision"])

    def test_mistral_api_discovery_normalizes_metadata_and_filters_non_chat(self):
        plans = []

        def transport(plan):
            plans.append(plan)
            return {
                "object": "list",
                "data": [
                    {
                        "id": "model-v1",
                        "name": "model",
                        "aliases": ["model-latest"],
                        "max_context_length": 262144,
                        "capabilities": {
                            "completion_chat": True,
                            "function_calling": True,
                            "vision": False,
                            "reasoning": True,
                        },
                    },
                    {"id": "embed", "capabilities": {"completion_chat": False}},
                    {"id": "archived", "archived": True, "capabilities": {"completion_chat": True}},
                    {"id": "unknown-capabilities"},
                ],
            }

        result = discover("mistral", {}, "secret", transport=transport)
        self.assertEqual(len(plans), 1)
        self.assertEqual(plans[0]["url"], "https://api.mistral.ai/v1/models")
        self.assertEqual(plans[0]["headers"]["Authorization"], "Bearer secret")
        self.assertNotIn("secret", json.dumps(result))
        self.assertEqual(len(result["models"]), 1)
        model = result["models"][0]
        self.assertEqual(model["id"], "model-v1")
        self.assertEqual(model["canonical_id"], "model")
        self.assertEqual(model["aliases"], ["model-v1", "model-latest"])
        self.assertEqual(model["context"], 262144)
        self.assertEqual(model["effort_modes"], ["none", "high"])

    def test_mistral_equivalent_cards_group_and_conflicting_aliases_are_order_independent(self):
        def card(identifier, canonical, context, aliases):
            return {
                "id": identifier,
                "name": canonical,
                "aliases": aliases,
                "max_context_length": context,
                "capabilities": {
                    "completion_chat": True,
                    "function_calling": True,
                    "vision": False,
                    "reasoning": True,
                },
            }

        equivalent = discover(
            "mistral", {}, "key", transport=lambda _plan: {"data": [
                card("family-v1", "family", 131072, ["family-shared"]),
                card("family-v2", "family", 131072, ["family-shared"]),
            ]},
        )
        self.assertEqual(len(equivalent["models"]), 1)
        self.assertEqual(equivalent["models"][0]["id"], "family-v1")
        self.assertEqual(
            equivalent["models"][0]["aliases"],
            ["family-v1", "family-shared", "family-v2"],
        )

        conflicting_cards = [
            card("model-a", "family-a", 32768, ["shared-route"]),
            card("model-b", "family-b", 262144, ["shared-route"]),
        ]
        first = discover(
            "mistral", {}, "key",
            transport=lambda _plan: {"data": copy.deepcopy(conflicting_cards)},
        )
        second = discover(
            "mistral", {}, "key",
            transport=lambda _plan: {"data": copy.deepcopy(list(reversed(conflicting_cards)))},
        )
        self.assertEqual(first["models"], second["models"])
        self.assertEqual(
            {model["id"]: model["context"] for model in first["models"]},
            {"model-a": 32768, "model-b": 262144},
        )
        self.assertNotIn(
            "shared-route",
            {alias for model in first["models"] for alias in model["aliases"]},
        )
        self.assertTrue(any("shared-route" in warning for warning in first["warnings"]))

    def test_deepseek_discovery_uses_bearer_list_route_but_keeps_limits_unknown(self):
        plans = []
        result = discover(
            "deepseek",
            {},
            "ds-secret",
            transport=lambda plan: plans.append(plan) or {
                "object": "list",
                "data": [{"id": "deepseek-flash", "object": "model", "owned_by": "deepseek"}],
            },
        )
        self.assertEqual(plans[0]["url"], "https://api.deepseek.com/models")
        self.assertEqual(plans[0]["headers"]["Authorization"], "Bearer ds-secret")
        self.assertNotIn("x-api-key", plans[0]["headers"])
        model = result["models"][0]
        self.assertEqual(model["id"], "deepseek-flash")
        self.assertIsNone(model["context"])
        self.assertTrue(model["vision"])
        self.assertTrue(model["tools"])
        self.assertEqual(model["effort_modes"], ["none", "low", "high", "max"])
        self.assertEqual(model["advertised_context"], "1M")
        self.assertEqual(model["inference_status"], "advertised")

    def test_ollama_discovery_uses_daemon_and_does_not_invent_capabilities(self):
        plans = []
        result = discover(
            "ollama",
            {"base_url": "http://127.0.0.1:12345"},
            None,
            transport=lambda plan: plans.append(plan) or {
                "models": [{
                    "name": "qwen3-coder:latest",
                    "model": "qwen3-coder:latest",
                    "details": {"family": "qwen3", "quantization_level": "Q4_K_M"},
                }],
            },
        )
        self.assertEqual(plans[0]["url"], "http://127.0.0.1:12345/api/tags")
        model = result["models"][0]
        self.assertIsNone(model["context"])
        self.assertIsNone(model["tools"])
        self.assertIsNone(model["reasoning"])
        self.assertEqual(model["details"]["family"], "qwen3")
        self.assertIsNone(model["runtime_context"])
        self.assertTrue(any("effective runtime context" in warning for warning in model["warnings"]))

    def test_ollama_show_enriches_each_completion_model_with_exact_metadata(self):
        plans = []

        def transport(plan):
            plans.append(copy.deepcopy(plan))
            if plan["url"].endswith("/api/tags"):
                return {"models": [
                    {"name": "vision-model", "model": "vision-model", "details": {"family": "tag-family"}},
                    {"name": "embed-model", "model": "embed-model"},
                ]}
            if plan["body"]["model"] == "vision-model":
                return {
                    "capabilities": ["completion", "tools", "vision", "thinking"],
                    "details": {"family": "show-family"},
                    "model_info": {
                        "general.architecture": "textarch",
                        "textarch.context_length": 131072,
                        "clip.context_length": 2048,
                    },
                }
            return {
                "capabilities": ["embedding"],
                "model_info": {"general.architecture": "embed", "embed.context_length": 8192},
            }

        result = discover("ollama", {}, None, transport=transport)
        self.assertEqual([plan["method"] for plan in plans], ["GET", "POST", "POST"])
        self.assertEqual(plans[1]["url"], "http://127.0.0.1:11434/api/show")
        self.assertEqual(plans[1]["body"], {"model": "vision-model"})
        self.assertEqual(plans[1]["headers"]["Content-Type"], "application/json")
        self.assertEqual(len(result["models"]), 1)
        model = result["models"][0]
        self.assertEqual(model["id"], "vision-model")
        self.assertEqual(model["context"], 131072)
        self.assertEqual(model["context_kind"], "model_declared_maximum")
        self.assertTrue(model["tools"])
        self.assertTrue(model["vision"])
        self.assertTrue(model["reasoning"])
        self.assertEqual(model["details"]["family"], "show-family")
        self.assertTrue(any("embed-model" in warning and "omitted" in warning for warning in result["warnings"]))

    def test_cerebras_discovery_marks_reasoning_replay_gap_per_exact_model(self):
        result = discover(
            "cerebras",
            {},
            "cb-secret",
            transport=lambda _plan: {
                "object": "list",
                "data": [{"id": "gpt-oss-120b"}, {"id": "future-model"}],
            },
        )
        by_id = {model["id"]: model for model in result["models"]}
        self.assertEqual(by_id["gpt-oss-120b"]["effort_modes"], ["low", "medium", "high"])
        self.assertEqual(by_id["gpt-oss-120b"]["context"], 131072)
        self.assertEqual(by_id["gpt-oss-120b"]["max_output"], 40960)
        self.assertEqual(by_id["gpt-oss-120b"]["context_kind"], "verified_documentation")
        self.assertEqual(by_id["gpt-oss-120b"]["reasoning_history"], "gateway_signed_replay")
        self.assertTrue(by_id["gpt-oss-120b"]["complete_tool_cycles"])
        self.assertIsNone(by_id["future-model"]["reasoning"])
        self.assertIsNone(by_id["future-model"]["complete_tool_cycles"])
        self.assertTrue(any("reasoning replay" in warning for warning in result["warnings"]))

    def test_cerebras_public_metadata_enriches_only_account_available_models(self):
        plans = []

        def transport(plan):
            plans.append(copy.deepcopy(plan))
            if plan["url"] == "https://api.cerebras.ai/v1/models":
                return {"object": "list", "data": [
                    {"id": "account-model"},
                    {
                        "id": "account-rich",
                        "max_context_length": 222222,
                        "capabilities": {"vision": False},
                    },
                ]}
            self.assertEqual(plan["url"], "https://api.cerebras.ai/public/v1/models")
            self.assertNotIn("Authorization", plan["headers"])
            return {"object": "list", "data": [
                {
                    "id": "account-model",
                    "name": "Account Model",
                    "capabilities": {
                        "tools": True,
                        "vision": True,
                        "reasoning": False,
                        "parallel_tool_calls": True,
                    },
                    "limits": {"max_context_length": 65536, "max_completion_tokens": 8192},
                },
                {
                    "id": "account-rich",
                    "name": "Account Rich",
                    "capabilities": {"tools": True, "vision": True, "reasoning": False},
                    "limits": {"max_context_length": 111111, "max_completion_tokens": 4096},
                },
                {"id": "not-in-account", "limits": {"max_context_length": 999999}},
            ]}

        result = discover("cerebras", {}, "secret", transport=transport)
        self.assertEqual(len(plans), 2)
        # Cerebras blocks urllib's default identity, including on public metadata.
        for plan in plans:
            self.assertEqual(plan["headers"]["User-Agent"], "ProviderHub/0.4")
        inference = prepare_request("cerebras", {}, "secret", text_prompt(), "account-model", {})
        self.assertEqual(inference["headers"]["User-Agent"], "ProviderHub/0.4")
        by_id = {model["id"]: model for model in result["models"]}
        self.assertEqual(set(by_id), {"account-model", "account-rich"})
        self.assertEqual(by_id["account-model"]["display_name"], "Account Model")
        self.assertEqual(by_id["account-model"]["context"], 65536)
        self.assertEqual(by_id["account-model"]["max_output"], 8192)
        self.assertTrue(by_id["account-model"]["tools"])
        self.assertTrue(by_id["account-model"]["vision"])
        self.assertFalse(by_id["account-model"]["reasoning"])
        self.assertTrue(by_id["account-model"]["complete_tool_cycles"])
        self.assertTrue(by_id["account-model"]["parallel_tool_calls"])
        # Exact account-scoped fields take precedence over public metadata.
        self.assertEqual(by_id["account-rich"]["context"], 222222)
        self.assertFalse(by_id["account-rich"]["vision"])

    def test_discovery_rejects_missing_keys_and_malformed_responses(self):
        with self.assertRaises(ProviderError):
            discover("mistral", {}, None, transport=lambda _plan: {})
        with self.assertRaises(ProviderError):
            discover("mistral", {}, "key", transport=lambda _plan: [])
        with self.assertRaises(ProviderError):
            discover("mistral", {}, "key", transport=lambda _plan: {"data": "wrong"})


class NativePlanTests(unittest.TestCase):
    def test_native_endpoints_and_authentication_are_provider_specific(self):
        cases = [
            ("kimi", {}, "key-kimi", "https://api.kimi.com/coding/v1/messages", "x-api-key", "key-kimi"),
            ("mimo", {"region": "sgp"}, "key-mimo", "https://token-plan-sgp.xiaomimimo.com/anthropic/v1/messages", "api-key", "key-mimo"),
            ("deepseek", {}, "key-deepseek", "https://api.deepseek.com/anthropic/v1/messages", "x-api-key", "key-deepseek"),
            ("ollama", {}, None, "http://127.0.0.1:11434/v1/messages", "x-api-key", "ollama"),
        ]
        for provider_id, connection, key, url, header, value in cases:
            with self.subTest(provider_id=provider_id):
                plan = prepare_request(provider_id, connection, key, text_prompt(), "exact-model", {})
                self.assertEqual(plan["url"], url)
                self.assertEqual(plan["headers"][header], value)
                self.assertEqual(plan["headers"]["anthropic-version"], "2023-06-01")
                self.assertEqual(plan["protocol"], "anthropic")
                self.assertEqual(plan["body"]["model"], "exact-model")
                self.assertEqual(plan["tool_name_map"], {})
                self.assertTrue(plan["compatibility"]["complete_tool_cycles"])
        self.assertEqual(
            prepare_request("kimi", {}, "key", text_prompt(), "k3-256k", {})["headers"]["User-Agent"],
            "ProviderHub/0.4",
        )

    def test_native_plan_preserves_thinking_tools_and_tool_ids(self):
        payload = {
            "model": "claude-fable-5",
            "api_key": "LOCAL-GATEWAY-SECRET",
            "authorization": "Bearer LOCAL-GATEWAY-SECRET",
            "system": [{"type": "text", "text": "base", "cache_control": {"type": "ephemeral"}}],
            "messages": [
                {"role": "system", "content": "leading"},
                {"role": "user", "content": "start"},
                {"role": "assistant", "content": [
                    {"type": "thinking", "thinking": "consider", "signature": "signed"},
                    {"type": "tool_use", "id": "toolu_long_native_id", "name": "read.file", "input": {"path": "a"}},
                ]},
                {"role": "system", "content": [{"type": "text", "text": "new instruction", "cache_control": {"type": "ephemeral"}}]},
                {"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": "toolu_long_native_id", "content": "done"},
                ]},
            ],
            "thinking": {"type": "enabled", "budget_tokens": 1024},
            "tools": [{"name": "read.file", "description": "Read", "input_schema": {"type": "object"}}],
        }
        original = copy.deepcopy(payload)
        plan = prepare_request("deepseek", {}, "upstream-key", payload, "deepseek-flash", {})
        body = plan["body"]
        self.assertEqual(payload, original)
        self.assertNotIn("api_key", body)
        self.assertNotIn("authorization", body)
        self.assertNotIn("LOCAL-GATEWAY-SECRET", json.dumps(plan))
        system_text = [part["text"] for part in body["system"]]
        self.assertEqual(system_text[0], "base")
        self.assertIn("messages[0]", system_text[1])
        self.assertEqual(system_text[2], "leading")
        self.assertIn("messages[3]", system_text[3])
        self.assertEqual(system_text[4], "new instruction")
        self.assertEqual(body["messages"][1]["content"], original["messages"][2]["content"])
        self.assertEqual(body["messages"][1]["content"][1]["id"], "toolu_long_native_id")
        self.assertEqual(
            body["messages"],
            [original["messages"][1], original["messages"][2], original["messages"][4]],
        )
        self.assertEqual(body["system"][4]["cache_control"], {"type": "ephemeral"})
        self.assertEqual(body["messages"][2]["content"][0]["tool_use_id"], "toolu_long_native_id")
        self.assertEqual(body["thinking"], original["thinking"])
        self.assertEqual(body["tools"], original["tools"])
        self.assertIn("Provider Hub placement note", system_text[1])
        self.assertNotIn("Mistral Bridge placement note", json.dumps(body))
        self.assertEqual(plan["compatibility"]["normalized_message_system_roles"], 2)
        self.assertEqual(
            plan["compatibility"]["system_role_normalization"],
            "top_level_with_position_markers",
        )

    def test_native_validation_does_not_silently_drop_non_text_system_content(self):
        payload = text_prompt(messages=[
            {"role": "user", "content": "hi"},
            {"role": "system", "content": [{"type": "image", "source": {"type": "base64"}}]},
        ])
        with self.assertRaises(ProviderError):
            prepare_request("mimo", {}, "key", payload, "mimo-v2.5", {})
        with self.assertRaises(ProviderError):
            prepare_request("deepseek", {}, "key", text_prompt(), "claude fake", {})

    def test_native_plan_applies_only_documented_model_limits(self):
        plan = prepare_request(
            "mimo", {}, "key", text_prompt(max_tokens=200000), "mimo-v2.5",
            {"max_output": 131072, "vision": True, "tools": True},
        )
        self.assertEqual(plan["body"]["max_tokens"], 131072)
        image = text_prompt(messages=[{"role": "user", "content": [{
            "type": "image",
            "source": {"type": "base64", "media_type": "image/png", "data": "AAAA"},
        }]}])
        with self.assertRaises(ProviderError):
            prepare_request("mimo", {}, "key", image, "mimo-v2.5-pro", {"vision": False})
        tools = text_prompt(tools=[{"name": "read", "input_schema": {"type": "object"}}])
        with self.assertRaises(ProviderError):
            prepare_request("deepseek", {}, "key", tools, "deepseek-flash", {"tools": False})

    def test_native_fast_requires_a_documented_same_model_control(self):
        for provider_id, model in (
            ("kimi", "kimi-for-coding-highspeed"),
            ("mimo", "mimo-v2.5"),
            ("deepseek", "deepseek-flash"),
            ("ollama", "qwen3-coder"),
        ):
            for request in ({"speed": "fast"}, {"service_tier": "priority"}):
                with self.subTest(provider=provider_id, request=request), self.assertRaises(ProviderError):
                    prepare_request(
                        provider_id, {}, None if provider_id == "ollama" else "key",
                        text_prompt(**request), model,
                        {"reasoning": True, "fast_mode": False},
                    )

    def test_kimi_effort_aliases_are_normalized_without_changing_model(self):
        spec = {"reasoning": True, "effort_modes": ["low", "high", "max"], "fast_mode": False}
        for requested, expected in (("low", "low"), ("medium", "high"), ("xhigh", "max"), ("ultra", "max")):
            payload = text_prompt(output_config={"effort": requested}, future_option={"keep": True})
            before = copy.deepcopy(payload)
            plan = prepare_request("kimi", {}, "key", payload, "k3-256k", spec)
            with self.subTest(requested=requested):
                self.assertEqual(plan["body"]["output_config"]["effort"], expected)
                self.assertEqual(plan["body"]["future_option"], {"keep": True})
                self.assertEqual(plan["body"]["model"], "k3-256k")
                self.assertEqual(payload, before)
        with self.assertRaises(ProviderError):
            prepare_request(
                "kimi", {}, "key", text_prompt(output_config={"effort": "none"}),
                "k3-256k", spec,
            )
        with self.assertRaisesRegex(ProviderError, "conflicting"):
            prepare_request(
                "kimi", {}, "key",
                text_prompt(thinking={"type": "disabled"}, output_config={"effort": "high"}),
                "kimi-for-coding", spec,
            )
        disabled = prepare_request(
            "kimi", {}, "key", text_prompt(output_config={"effort": "none"}),
            "kimi-for-coding", spec,
        )["body"]
        self.assertEqual(disabled["thinking"]["type"], "disabled")

    def test_mimo_maps_effort_to_its_documented_coarse_thinking_switch(self):
        spec = {"reasoning": True, "effort_modes": ["none", "high"], "fast_mode": False}
        for requested, expected in (("none", "disabled"), ("minimal", "enabled"), ("max", "enabled")):
            payload = text_prompt(output_config={
                "effort": requested,
                "future_output_option": {"keep": True},
            })
            body = prepare_request("mimo", {}, "key", payload, "mimo-v2.5", spec)["body"]
            with self.subTest(requested=requested):
                self.assertEqual(body["thinking"]["type"], expected)
                self.assertNotIn("effort", body["output_config"])
                self.assertEqual(body["output_config"]["future_output_option"], {"keep": True})
        with self.assertRaisesRegex(ProviderError, "conflicting"):
            prepare_request(
                "mimo", {}, "key",
                text_prompt(thinking={"type": "disabled"}, output_config={"effort": "high"}),
                "mimo-v2.5", spec,
            )

    def test_deepseek_normalizes_only_documented_effort_aliases(self):
        spec = {
            "reasoning": True,
            "effort_modes": ["none", "low", "high", "max"],
            "fast_mode": False,
        }
        for requested, expected in (
            ("none", "none"), ("minimal", "low"), ("low", "low"),
            ("medium", "high"), ("xhigh", "high"), ("max", "max"),
        ):
            body = prepare_request(
                "deepseek", {}, "key", text_prompt(output_config={"effort": requested}),
                "deepseek-flash", spec,
            )["body"]
            with self.subTest(requested=requested):
                self.assertEqual(body["output_config"]["effort"], expected)
                self.assertEqual(
                    body["thinking"]["type"], "disabled" if expected == "none" else "enabled")
        with self.assertRaises(ProviderError):
            prepare_request(
                "deepseek", {}, "key", text_prompt(output_config={"effort": "ultra"}),
                "deepseek-flash", spec,
            )
        with self.assertRaisesRegex(ProviderError, "conflicting"):
            prepare_request(
                "deepseek", {}, "key",
                text_prompt(thinking={"type": "disabled"}, output_config={"effort": "high"}),
                "deepseek-flash", spec,
            )

    def test_ollama_maps_auto_sent_effort_to_discovered_coarse_thinking(self):
        enabled = text_prompt(thinking={"type": "enabled", "budget_tokens": 1024})
        body = prepare_request(
            "ollama", {}, None, enabled, "qwen3-coder",
            {"reasoning": True, "effort_modes": [], "fast_mode": False},
        )["body"]
        self.assertEqual(body["thinking"], enabled["thinking"])
        disabled = prepare_request(
            "ollama", {}, None, text_prompt(output_config={"effort": "none"}),
            "qwen3-coder", {"reasoning": True, "effort_modes": [], "fast_mode": False},
        )["body"]
        self.assertEqual(disabled["thinking"]["type"], "disabled")
        self.assertNotIn("output_config", disabled)
        coarse = prepare_request(
            "ollama", {}, None,
            text_prompt(output_config={"effort": "high", "future_option": {"keep": True}}),
            "qwen3-coder", {"reasoning": True, "effort_modes": [], "fast_mode": False},
        )["body"]
        self.assertEqual(coarse["thinking"]["type"], "enabled")
        self.assertNotIn("effort", coarse["output_config"])
        self.assertEqual(coarse["output_config"]["future_option"], {"keep": True})
        adaptive_plan = prepare_request(
            "ollama", {}, None,
            text_prompt(thinking={"type": "adaptive"}, output_config={"effort": "high"}),
            "qwen3-coder", {"reasoning": True, "effort_modes": [], "fast_mode": False},
        )
        self.assertEqual(adaptive_plan["body"]["thinking"]["type"], "enabled")
        self.assertEqual(
            adaptive_plan["compatibility"]["adaptive_thinking"],
            "normalized_to_enabled",
        )
        with self.assertRaises(ProviderError):
            prepare_request(
                "ollama", {}, None, enabled, "plain-model",
                {"reasoning": False, "effort_modes": [], "fast_mode": False},
            )

    def test_adaptive_thinking_normalizes_to_provider_coarse_switch(self):
        payload = text_prompt(
            thinking={"type": "adaptive", "future_budget_option": "keep"},
            output_config={"effort": "high"},
        )
        cases = [
            ("kimi", "k3-256k", ["low", "high", "max"]),
            ("mimo", "mimo-v2.5", ["none", "high"]),
            ("deepseek", "deepseek-flash", ["none", "low", "high", "max"]),
        ]
        for provider_id, model, efforts in cases:
            with self.subTest(provider=provider_id):
                plan = prepare_request(
                    provider_id, {}, "key", payload, model,
                    {"reasoning": True, "effort_modes": efforts, "fast_mode": False},
                )
                self.assertEqual(plan["body"]["thinking"], {
                    "type": "enabled", "future_budget_option": "keep",
                })
                self.assertEqual(
                    plan["compatibility"]["adaptive_thinking"],
                    "normalized_to_enabled",
                )


class ChatPlanTests(unittest.TestCase):
    def tool_prompt(self, **changes):
        payload = {
            "model": "claude-fable-5",
            "max_tokens": 9000,
            "system": "base",
            "messages": [
                {"role": "user", "content": "read"},
                {"role": "assistant", "content": [
                    {"type": "thinking", "thinking": "private", "signature": "signed"},
                    {"type": "text", "text": "calling"},
                    {"type": "tool_use", "id": "toolu_preserve_or_map", "name": "read.file", "input": {"path": "a"}},
                ]},
                {"role": "system", "content": "interstitial"},
                {"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": "toolu_preserve_or_map", "content": "done"},
                ]},
            ],
            "tools": [{
                "name": "read.file",
                "description": "Read a file",
                "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}},
            }],
            "tool_choice": {"type": "any", "disable_parallel_tool_use": True},
            "stream": True,
        }
        payload.update(changes)
        return payload

    def test_mistral_chat_translation_is_mistral_specific(self):
        payload = self.tool_prompt(output_config={"effort": "low"})
        plan = prepare_request(
            "mistral",
            {},
            "mistral-key",
            payload,
            "mistral-model",
            {"context": 262144, "reasoning": True, "vision": True},
        )
        self.assertEqual(plan["url"], "https://api.mistral.ai/v1/chat/completions")
        self.assertEqual(plan["headers"]["Authorization"], "Bearer mistral-key")
        self.assertEqual(plan["body"]["model"], "mistral-model")
        self.assertEqual(plan["body"]["reasoning_effort"], "none")
        self.assertIn("prompt_cache_key", plan["body"])
        assistant = next(message for message in plan["body"]["messages"] if message["role"] == "assistant")
        mapped_id = assistant["tool_calls"][0]["id"]
        mapped_name = assistant["tool_calls"][0]["function"]["name"]
        self.assertRegex(mapped_id, r"^[a-f0-9]{9}$")
        self.assertNotEqual(mapped_id, "toolu_preserve_or_map")
        self.assertNotEqual(mapped_name, "read.file")
        self.assertEqual(plan["tool_name_map"][mapped_name], "read.file")
        tool_result = next(message for message in plan["body"]["messages"] if message["role"] == "tool")
        self.assertEqual(tool_result["tool_call_id"], mapped_id)
        self.assertEqual(plan["body"]["tool_choice"], "required")
        self.assertFalse(plan["body"]["parallel_tool_calls"])
        self.assertNotIn("private", json.dumps(plan["body"]))

    def test_cerebras_keeps_tool_ids_and_uses_exact_model_effort(self):
        payload = self.tool_prompt(output_config={"effort": "medium"}, service_tier="auto")
        payload["messages"][1]["content"] = [
            block for block in payload["messages"][1]["content"] if block["type"] != "thinking"
        ]
        reasoning = "verified Cerebras reasoning"
        payload["messages"][1]["content"].insert(0, sign_thinking(
            reasoning,
            payload["messages"][1]["content"],
            "gpt-oss-120b",
            "test-scope",
            "gateway-token",
        ))
        plan = prepare_request(
            "cerebras",
            {},
            "cerebras-key",
            payload,
            "gpt-oss-120b",
            {
                "reasoning": True,
                "effort_modes": ["low", "medium", "high"],
                "max_output": 4096,
                "reasoning_history": "gateway_signed_replay",
            },
            reasoning_by_message={1: reasoning},
        )
        self.assertEqual(plan["body"]["max_tokens"], 4096)
        self.assertEqual(plan["body"]["reasoning_effort"], "medium")
        self.assertEqual(plan["body"]["service_tier"], "auto")
        self.assertNotIn("prompt_cache_key", plan["body"])
        assistant = next(message for message in plan["body"]["messages"] if message["role"] == "assistant")
        self.assertEqual(assistant["tool_calls"][0]["id"], "toolu_preserve_or_map")
        self.assertEqual(assistant["reasoning"], reasoning)
        self.assertTrue(plan["compatibility"]["complete_tool_cycles"])
        self.assertEqual(plan["compatibility"]["reasoning_history"], "gateway_signed_replay")
        self.assertEqual(plan["compatibility"]["verified_reasoning_messages"], 1)

    def test_cerebras_required_reasoning_trace_is_never_silently_stripped(self):
        with self.assertRaisesRegex(ProviderError, "caller-verified gateway signature"):
            prepare_request(
                "cerebras",
                {},
                "key",
                self.tool_prompt(output_config={"effort": "medium"}),
                "gpt-oss-120b",
                {
                    "reasoning": True,
                    "effort_modes": ["low", "medium", "high"],
                    "reasoning_history": "adapter_required",
                },
            )

    def test_cerebras_reasoning_and_service_tiers_do_not_use_mistral_mapping(self):
        spec = {"reasoning": True, "effort_modes": ["low", "medium", "high"]}
        with self.assertRaises(ProviderError):
            prepare_request(
                "cerebras", {}, "key", text_prompt(output_config={"effort": "xhigh"}),
                "gpt-oss-120b", spec,
            )
        with self.assertRaises(ProviderError):
            prepare_request(
                "cerebras", {}, "key", text_prompt(thinking={"type": "disabled"}),
                "gpt-oss-120b", spec,
            )
        with self.assertRaises(ProviderError):
            prepare_request(
                "cerebras", {}, "key", text_prompt(service_tier="priority"),
                "gpt-oss-120b", spec,
            )

    def test_provider_specific_image_shapes_and_cerebras_data_uri_rule(self):
        image = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AAAA"}}
        payload = text_prompt(messages=[{"role": "user", "content": [image]}])
        mistral = prepare_request("mistral", {}, "key", payload, "model", {"vision": True})
        cerebras = prepare_request("cerebras", {}, "key", payload, "model", {"vision": True})
        self.assertEqual(
            mistral["body"]["messages"][0]["content"][0]["image_url"],
            "data:image/png;base64,AAAA",
        )
        self.assertEqual(
            cerebras["body"]["messages"][0]["content"][0]["image_url"],
            {"url": "data:image/png;base64,AAAA"},
        )
        remote = text_prompt(messages=[{"role": "user", "content": [{
            "type": "image", "source": {"type": "url", "url": "https://images.example/a.png"},
        }]}])
        with self.assertRaises(ProviderError):
            prepare_request("cerebras", {}, "key", remote, "model", {"vision": True})
        with self.assertRaises(ProviderError):
            prepare_request("cerebras", {}, "key", payload, "model", {"vision": False})

    def test_chat_plan_does_not_mutate_input_and_rejects_hosted_tools(self):
        payload = self.tool_prompt()
        original = copy.deepcopy(payload)
        prepare_request("mistral", {}, "key", payload, "model", {"reasoning": False})
        self.assertEqual(payload, original)
        hosted = text_prompt(tools=[{"type": "web_search_20250305", "name": "web_search"}])
        with self.assertRaises(ProviderError):
            prepare_request("cerebras", {}, "key", hosted, "model", {})
        with self.assertRaises(ProviderError):
            prepare_request("cerebras", {}, "key", payload, "model", {"tools": False})

    def test_reported_context_limits_are_enforced_without_guessing_unknown_limits(self):
        oversized = text_prompt(messages=[{"role": "user", "content": "x" * 30000}])
        with self.assertRaises(ProviderError):
            prepare_request("mistral", {}, "key", oversized, "model", {"context": 8000})
        with self.assertRaises(ProviderError):
            prepare_request("mimo", {}, "key", oversized, "mimo-v2.5", {"context": 8000})
        # A missing context is deliberately left to the provider rather than
        # filled from a model-name guess.
        plan = prepare_request("cerebras", {}, "key", oversized, "future-model", {"context": None})
        self.assertEqual(plan["body"]["max_tokens"], 2048)


if __name__ == "__main__":
    unittest.main(verbosity=2)
