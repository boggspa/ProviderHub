"""Deterministic Meta Model API provider tests; no live inference or credentials."""
from __future__ import annotations

import copy
import json
import unittest

from providers import (
    GATEWAY_USER_AGENT,
    PROVIDERS,
    ProviderError,
    discover,
    prepare_request,
    provider_defaults,
    validate_connection,
)


def default_spec():
    return {
        "id": "muse-spark-1.3",
        "canonical_id": "muse-spark-1.3",
        "display_name": "Muse Spark 1.3",
        "context": 1048576,
        "max_output": 131072,
        "aliases": ["muse-spark-1.3"],
        "tools": True,
        "vision": True,
        "reasoning": True,
        "streaming": True,
        "adaptive_thinking": True,
        "effort_modes": ["minimal", "low", "medium", "high", "xhigh", "max"],
        "fast_mode": False,
        "parallel_tool_calls": True,
        "tool_choice_modes": ["auto"],
        "reasoning_history": "native",
    }


def claude_payload(**changes):
    payload = {
        "model": "claude-fable-5",
        "max_tokens": 4096,
        "messages": [{"role": "user", "content": "Inspect the project."}],
        "thinking": {"type": "adaptive", "display": "omitted"},
        "output_config": {"effort": "high"},
        "tools": [{
            "name": "read_file",
            "description": "Read a file",
            "input_schema": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        }],
        "tool_choice": {"type": "auto", "disable_parallel_tool_use": False},
        "stream": True,
    }
    payload.update(changes)
    return payload


class MuseRegistryTests(unittest.TestCase):
    def test_registry_uses_only_the_model_api_payg_credential_contract(self):
        descriptor = PROVIDERS["muse"]
        self.assertEqual(descriptor["id"], "muse")
        self.assertEqual(descriptor["name"], "Muse (Meta Model API)")
        self.assertEqual(descriptor["protocol"], "anthropic")
        self.assertEqual(descriptor["default_base_url"], "https://api.meta.ai")
        self.assertEqual(descriptor["regions"], {"global": "https://api.meta.ai"})
        self.assertEqual(descriptor["auth_header"], {"name": "Authorization", "prefix": "Bearer "})
        self.assertEqual(descriptor["credential_account"], "MODEL_API_KEY")
        self.assertEqual(descriptor["credential_env"], "MODEL_API_KEY")
        self.assertEqual(descriptor["setup_url"], "https://dev.meta.ai/")
        self.assertNotIn("oauth", json.dumps(descriptor).lower())
        self.assertNotIn("cli", json.dumps(descriptor).lower())
        self.assertEqual(
            provider_defaults()["muse"],
            {"region": "global", "base_url": "https://api.meta.ai"},
        )

    def test_only_official_native_meta_endpoints_are_accepted(self):
        for value in (
            "https://api.meta.ai",
            "https://api.meta.ai/",
            "https://api.meta.ai/v1",
            "https://api.meta.ai/v1/models",
            "https://api.meta.ai/v1/messages",
            "https://api.meta.ai:443/v1/messages",
        ):
            with self.subTest(value=value):
                self.assertEqual(
                    validate_connection("muse", {"base_url": value}),
                    {"region": "global", "base_url": "https://api.meta.ai"},
                )
        for value in (
            "http://api.meta.ai",
            "https://meta.ai/v1/messages",
            "https://dev.meta.ai/v1/messages",
            "https://api.meta.ai.evil.example/v1/messages",
            "https://user:pass@api.meta.ai/v1/messages",
            "https://api.meta.ai:444/v1/messages",
            "https://api.meta.ai/v1/responses",
            "https://api.meta.ai/v1/messages?redirect=https://evil.example",
        ):
            with self.subTest(value=value), self.assertRaises(ProviderError):
                validate_connection("muse", {"base_url": value})


class MuseDiscoveryTests(unittest.TestCase):
    def test_general_model_list_excludes_non_chat_and_retired_models(self):
        result = discover("muse", {}, "key", transport=lambda _plan: {
            "data": [
                {"id": "muse-image-1.0"},
                {"id": "muse-voice-transcribe-1.0"},
                {"id": "future-image", "output_modalities": ["image"]},
                {"id": "retired-spark", "deprecated": True},
                {"id": "archived-spark", "archived": True},
                {"id": "muse-spark-1.3"},
                {"id": "future-chat", "input_modalities": ["text", "image"], "output_modalities": ["text"]},
            ],
        })
        self.assertEqual({model["id"] for model in result["models"]}, {"muse-spark-1.3", "future-chat"})

    def test_authenticated_model_list_and_exact_documentation_enrichment(self):
        plans = []

        def transport(plan):
            plans.append(copy.deepcopy(plan))
            return {
                "object": "list",
                "data": [
                    {"id": "muse-spark-1.3", "object": "model", "owned_by": "meta"},
                    {
                        "id": "muse-spark-1.3-contributor",
                        "object": "model",
                        "name": "Muse Spark 1.3 Contributor",
                        "limits": {
                            "max_context_length": 524288,
                            "max_completion_tokens": 65536,
                        },
                        "capabilities": {
                            "tools": True,
                            "vision": False,
                            "reasoning": True,
                            "streaming": True,
                            "parallel_tool_calls": False,
                        },
                        "effort_modes": ["low", "high"],
                    },
                    {"id": "future-muse-model", "object": "model"},
                    {
                        "id": "muse-image-only",
                        "object": "model",
                        "capabilities": {"completion_chat": False},
                    },
                ],
            }

        result = discover("muse", {}, "LLM|123|secret", transport=transport)
        self.assertEqual(len(plans), 1)
        plan = plans[0]
        self.assertEqual(plan["method"], "GET")
        self.assertEqual(plan["url"], "https://api.meta.ai/v1/models")
        self.assertEqual(plan["headers"]["Authorization"], "Bearer LLM|123|secret")
        self.assertEqual(plan["headers"]["User-Agent"], GATEWAY_USER_AGENT)
        self.assertEqual(plan["headers"]["anthropic-version"], "2023-06-01")
        self.assertNotIn("secret", json.dumps(result))

        by_id = {model["id"]: model for model in result["models"]}
        standard = by_id["muse-spark-1.3"]
        self.assertEqual(standard["canonical_id"], "muse-spark-1.3")
        self.assertEqual(standard["aliases"], ["muse-spark-1.3"])
        self.assertEqual(standard["context"], 1048576)
        self.assertEqual(standard["max_output"], 131072)
        self.assertEqual(standard["context_kind"], "verified_documentation")
        self.assertEqual(standard["effort_modes"], ["minimal", "low", "medium", "high", "xhigh", "max"])
        self.assertTrue(standard["tools"])
        self.assertTrue(standard["vision"])
        self.assertTrue(standard["reasoning"])
        self.assertTrue(standard["adaptive_thinking"])
        self.assertTrue(standard["parallel_tool_calls"])
        self.assertFalse(standard["fast_mode"])
        self.assertIn("reasoning.md", standard["metadata_evidence"])

        contributor = by_id["muse-spark-1.3-contributor"]
        self.assertEqual(contributor["canonical_id"], "muse-spark-1.3-contributor")
        self.assertEqual(contributor["aliases"], ["muse-spark-1.3-contributor"])
        self.assertEqual(contributor["context"], 524288)
        self.assertEqual(contributor["max_output"], 65536)
        self.assertEqual(contributor["context_kind"], "provider_reported")
        self.assertTrue(contributor["tools"])
        self.assertFalse(contributor["vision"])
        self.assertEqual(contributor["effort_modes"], ["low", "high"])
        self.assertFalse(contributor["parallel_tool_calls"])
        self.assertFalse(contributor["fast_mode"])
        unknown = by_id["future-muse-model"]
        self.assertIsNone(unknown["context"])
        self.assertIsNone(unknown["tools"])
        self.assertIsNone(unknown["vision"])
        self.assertIsNone(unknown["reasoning"])
        self.assertEqual(unknown["effort_modes"], [])
        self.assertNotIn("muse-image-only", by_id)
        self.assertEqual(result["source"], "provider_api")
        self.assertEqual(result["evidence"], "https://api.meta.ai/v1/models")
        self.assertTrue(all(model["inference_status"] == "advertised" for model in by_id.values()))
        self.assertTrue(any("first-party" in warning for warning in result["warnings"]))

    def test_provider_reported_limits_override_fixed_id_documentation(self):
        result = discover("muse", {}, "key", transport=lambda _plan: {
            "data": [{
                "id": "muse-spark-1.3",
                "max_context_length": 777777,
                "max_output_tokens": 33333,
            }],
        })
        model = result["models"][0]
        self.assertEqual(model["context"], 777777)
        self.assertEqual(model["max_output"], 33333)
        self.assertEqual(model["context_kind"], "provider_reported")

    def test_discovery_never_invents_a_model_when_the_account_list_is_empty(self):
        result = discover(
            "muse", {}, "key", transport=lambda _plan: {"object": "list", "data": []},
        )
        self.assertEqual(result["models"], [])
        with self.assertRaises(ProviderError):
            discover("muse", {}, None, transport=lambda _plan: {"data": []})
        with self.assertRaises(ProviderError):
            discover("muse", {}, "key", transport=lambda _plan: {"data": "not-an-array"})


class MusePlanningTests(unittest.TestCase):
    def test_default_claude_adaptive_payload_is_preserved_on_native_messages(self):
        payload = claude_payload(
            api_key="LOCAL-GATEWAY-KEY",
            authorization="Bearer LOCAL-GATEWAY-KEY",
        )
        original = copy.deepcopy(payload)
        plan = prepare_request(
            "muse", {}, "LLM|123|provider-secret", payload, "muse-spark-1.3", default_spec(),
        )
        self.assertEqual(payload, original)
        self.assertEqual(plan["url"], "https://api.meta.ai/v1/messages")
        self.assertEqual(plan["protocol"], "anthropic")
        self.assertEqual(plan["headers"]["Authorization"], "Bearer LLM|123|provider-secret")
        self.assertEqual(plan["headers"]["User-Agent"], GATEWAY_USER_AGENT)
        self.assertEqual(plan["headers"]["anthropic-version"], "2023-06-01")
        self.assertEqual(plan["body"]["model"], "muse-spark-1.3")
        self.assertEqual(
            plan["body"]["thinking"],
            {"type": "adaptive", "display": "omitted"},
        )
        self.assertEqual(plan["body"]["output_config"], {"effort": "high"})
        self.assertEqual(plan["body"]["tool_choice"], payload["tool_choice"])
        self.assertTrue(plan["body"]["stream"])
        self.assertNotIn("api_key", plan["body"])
        self.assertNotIn("authorization", plan["body"])
        self.assertNotIn("adaptive_thinking", plan["compatibility"])

    def test_claude_and_codex_ranks_forward_current_meta_values(self):
        for requested, expected, note in (
            ("minimal", "minimal", None),
            ("low", "low", None),
            ("medium", "medium", None),
            ("high", "high", None),
            ("xhigh", "xhigh", None),
            ("max", "max", None),
            ("ultra", "max", "ultra_normalized_to_max"),
        ):
            with self.subTest(requested=requested):
                plan = prepare_request(
                    "muse", {}, "key",
                    claude_payload(output_config={"effort": requested}),
                    "muse-spark-1.3", default_spec(),
                )
                self.assertEqual(plan["body"]["output_config"]["effort"], expected)
                if note is None:
                    self.assertNotIn("reasoning_effort", plan["compatibility"])
                else:
                    self.assertEqual(plan["compatibility"]["reasoning_effort"], note)

    def test_unknown_model_metadata_does_not_block_documented_api_effort(self):
        plan = prepare_request(
            "muse", {}, "key", claude_payload(), "future-muse-model",
            {"reasoning": None, "effort_modes": [], "context": None},
        )
        self.assertEqual(plan["body"]["output_config"]["effort"], "high")
        self.assertEqual(plan["body"]["model"], "future-muse-model")
        for requested in ("xhigh", "max"):
            with self.subTest(requested=requested):
                forwarded = prepare_request(
                    "muse", {}, "key", claude_payload(output_config={"effort": requested}),
                    "future-muse-model", {"reasoning": None, "effort_modes": [], "context": None},
                )
                self.assertEqual(forwarded["body"]["output_config"]["effort"], requested)
        narrowed = prepare_request(
            "muse", {}, "key", claude_payload(), "limited-muse-model",
            {"reasoning": True, "effort_modes": ["low"]},
        )
        self.assertEqual(narrowed["body"]["output_config"]["effort"], "low")
        self.assertEqual(narrowed["compatibility"]["reasoning_effort"], "high_normalized_to_low")

    def test_account_listed_modes_constrain_or_reject_standard_tier_max(self):
        contributor = {"reasoning": True, "effort_modes": ["low", "high"]}
        for requested in ("xhigh", "max"):
            with self.subTest(requested=requested):
                squeezed = prepare_request(
                    "muse", {}, "key", claude_payload(output_config={"effort": requested}),
                    "muse-spark-1.3-contributor", contributor,
                )
                self.assertEqual(squeezed["body"]["output_config"]["effort"], "high")
                self.assertEqual(squeezed["compatibility"]["reasoning_effort"],
                                 f"{requested}_normalized_to_high")
        ultra = prepare_request(
            "muse", {}, "key", claude_payload(output_config={"effort": "ultra"}),
            "muse-spark-1.3-contributor", contributor,
        )
        self.assertEqual(ultra["body"]["output_config"]["effort"], "high")
        self.assertEqual(ultra["compatibility"]["reasoning_effort"], "ultra_normalized_to_high")

    def test_unreliable_reasoning_disable_and_forced_tool_choice_fail_clearly(self):
        with self.assertRaisesRegex(ProviderError, "cannot be disabled reliably"):
            prepare_request(
                "muse", {}, "key", claude_payload(output_config={"effort": "none"}),
                "muse-spark-1.3", default_spec(),
            )
        with self.assertRaisesRegex(ProviderError, "cannot be disabled reliably"):
            prepare_request(
                "muse", {}, "key", claude_payload(thinking={"type": "disabled"}),
                "muse-spark-1.3", default_spec(),
            )
        for choice in ("none", "any", "tool"):
            with self.subTest(choice=choice), self.assertRaisesRegex(ProviderError, "automatic tool choice"):
                value = {"type": choice}
                if choice == "tool":
                    value["name"] = "read_file"
                prepare_request(
                    "muse", {}, "key", claude_payload(tool_choice=value),
                    "muse-spark-1.3", default_spec(),
                )

    def test_native_thinking_tool_ids_and_results_survive_a_complete_cycle(self):
        messages = [
            {"role": "user", "content": "Read project.txt"},
            {"role": "assistant", "content": [
                {
                    "type": "thinking",
                    "thinking": "I should inspect the file.",
                    "signature": "opaque-meta-signature",
                },
                {
                    "type": "tool_use",
                    "id": "meta_tool_call_exact",
                    "name": "read_file",
                    "input": {"path": "project.txt"},
                },
            ]},
            {"role": "user", "content": [{
                "type": "tool_result",
                "tool_use_id": "meta_tool_call_exact",
                "content": "project contents",
            }]},
        ]
        payload = claude_payload(messages=messages)
        original = copy.deepcopy(payload)
        body = prepare_request(
            "muse", {}, "key", payload, "muse-spark-1.3", default_spec(),
        )["body"]
        self.assertEqual(payload, original)
        self.assertEqual(body["messages"], messages)
        self.assertEqual(body["messages"][1]["content"][0]["signature"], "opaque-meta-signature")
        self.assertEqual(body["messages"][1]["content"][1]["id"], "meta_tool_call_exact")
        self.assertEqual(
            body["messages"][2]["content"][0]["tool_use_id"],
            "meta_tool_call_exact",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
