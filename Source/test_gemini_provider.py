import copy
import json
from pathlib import Path
import tempfile
import unittest
import urllib.parse

from gemini_provider import (
    BASE_URL,
    CLIENT_HEADER,
    ENVELOPE_PREFIX,
    GeminiError,
    GeminiStreamAdapter,
    discover,
    prepare_request,
    translate_response,
    validate_connection,
    validate_messages,
)
from responses_bridge import MessagesResponsesAdapter, ReasoningEnvelope, to_messages
from responses_tools import flatten_tools, input_names, output_names


KEY = "AIza-fixture-key-never-sent"
REPLAY_KEY = "local-gateway-replay-key-which-is-long-enough"
SCOPE = "gemini-connection-and-account-scope"
MODEL = "gemini-3.5-flash"
REQUESTED = "claude-sonnet-4-6"


def model_spec(base=MODEL, **changes):
    efforts = {
        "gemini-3.8-flash": ["low", "medium", "high"],
        "gemini-3.5-flash": ["minimal", "low", "medium", "high"],
        "gemini-2.5-pro": ["minimal", "low", "medium", "high"],
        "gemini-2.5-flash": ["none", "minimal", "low", "medium", "high"],
    }[base]
    value = {
        "context": 1_048_576,
        "max_input": 1_048_576,
        "max_output": 65_536,
        "tools": True,
        "vision": True,
        "reasoning": True,
        "effort_modes": efforts,
        "base_model_id": base,
    }
    value.update(changes)
    return value


def request_payload(**changes):
    value = {
        "model": REQUESTED,
        "max_tokens": 2048,
        "messages": [{"role": "user", "content": "Check a file."}],
        "tools": [{
            "name": "workspace.read_file",
            "description": "Read one file",
            "input_schema": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        }],
    }
    value.update(changes)
    return value


def reconstruct(events):
    blocks = {}
    usage = {}
    stop = None
    for event in events:
        kind = event.get("type")
        if kind == "content_block_start":
            block = copy.deepcopy(event["content_block"])
            if block.get("type") == "tool_use":
                block["partial_json"] = ""
            blocks[event["index"]] = block
        elif kind == "content_block_delta":
            block = blocks[event["index"]]
            delta = event["delta"]
            if delta["type"] == "text_delta":
                block["text"] += delta["text"]
            elif delta["type"] == "input_json_delta":
                block["partial_json"] += delta["partial_json"]
        elif kind == "content_block_stop":
            block = blocks[event["index"]]
            if block.get("type") == "tool_use":
                block["input"] = json.loads(block.pop("partial_json") or "{}")
        elif kind == "message_delta":
            stop = event["delta"]["stop_reason"]
            usage = event["usage"]
    return [blocks[index] for index in sorted(blocks)], stop, usage


class ConnectionAndDiscoveryTests(unittest.TestCase):
    def test_connection_accepts_only_the_official_api_key_surface(self):
        self.assertEqual(validate_connection(None), {"region": "global", "base_url": BASE_URL})
        self.assertEqual(
            validate_connection({"base_url": BASE_URL + "/chat/completions"}),
            {"region": "global", "base_url": BASE_URL},
        )
        for value in (
            "http://generativelanguage.googleapis.com/v1beta/openai",
            "https://generativelanguage.googleapis.com.evil.test/v1beta/openai",
            "https://user:pass@generativelanguage.googleapis.com/v1beta/openai",
            "https://generativelanguage.googleapis.com:444/v1beta/openai",
            "https://generativelanguage.googleapis.com/v1beta",
            BASE_URL + "?key=plaintext",
        ):
            with self.subTest(value=value), self.assertRaises(GeminiError):
                validate_connection({"base_url": value})
        with self.assertRaises(GeminiError):
            validate_connection({"api_key": KEY})
        with self.assertRaises(GeminiError):
            validate_connection({"region": "europe"})

    def test_paginated_discovery_uses_account_membership_and_exact_api_limits(self):
        plans = []

        def transport(plan):
            plans.append(copy.deepcopy(plan))
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(plan["url"]).query)
            if "pageToken" not in query:
                return {
                    "models": [
                        {
                            "name": "models/gemini-3.8-flash",
                            "baseModelId": "gemini-3.8-flash",
                            "version": "3.8",
                            "displayName": "Gemini 3.8 Flash",
                            "description": "Text model",
                            "inputTokenLimit": 1_000_000,
                            "outputTokenLimit": 64_000,
                            "supportedGenerationMethods": ["generateContent", "countTokens"],
                            "thinking": True,
                        },
                        {
                            "name": "models/gemini-3.1-flash-image",
                            "baseModelId": "gemini-3.1-flash-image",
                            "inputTokenLimit": 65_536,
                            "outputTokenLimit": 32_768,
                            "supportedGenerationMethods": ["generateContent"],
                            "thinking": True,
                        },
                        {
                            "name": "models/gemini-embedding-001",
                            "baseModelId": "gemini-embedding-001",
                            "inputTokenLimit": 2048,
                            "outputTokenLimit": 1,
                            "supportedGenerationMethods": ["embedContent"],
                            "thinking": False,
                        },
                    ],
                    "nextPageToken": "page two+/=",
                }
            self.assertEqual(query["pageToken"], ["page two+/="])
            return {
                "models": [
                    {
                        "name": "models/gemini-2.5-pro-001",
                        "baseModelId": "gemini-2.5-pro",
                        "displayName": "Gemini 2.5 Pro 001",
                        "inputTokenLimit": 1_048_576,
                        "outputTokenLimit": 65_536,
                        "supportedGenerationMethods": ["generateContent"],
                        "thinking": True,
                    },
                    {
                        "name": "models/gemini-3.5-flash-no-limit",
                        "baseModelId": "gemini-3.5-flash",
                        "supportedGenerationMethods": ["generateContent"],
                        "thinking": True,
                    },
                ]
            }

        result = discover({}, KEY, transport=transport)
        self.assertEqual(len(plans), 2)
        self.assertTrue(all(plan["method"] == "GET" for plan in plans))
        self.assertTrue(all(plan["headers"]["x-goog-api-key"] == KEY for plan in plans))
        self.assertTrue(all(plan["headers"]["x-goog-api-client"] == CLIENT_HEADER for plan in plans))
        self.assertTrue(all("Authorization" not in plan["headers"] for plan in plans))
        self.assertNotIn(KEY, json.dumps(result))
        by_id = {model["id"]: model for model in result["models"]}
        self.assertEqual(set(by_id), {"gemini-3.8-flash", "gemini-2.5-pro-001"})
        self.assertEqual(by_id["gemini-3.8-flash"]["context"], 1_000_000)
        self.assertEqual(by_id["gemini-3.8-flash"]["max_input"], 1_000_000)
        self.assertEqual(by_id["gemini-3.8-flash"]["max_output"], 64_000)
        self.assertEqual(by_id["gemini-3.8-flash"]["effort_modes"], ["low", "medium", "high"])
        self.assertFalse(by_id["gemini-3.8-flash"]["fast_mode"])
        self.assertEqual(
            by_id["gemini-2.5-pro-001"]["effort_modes"],
            ["minimal", "low", "medium", "high"],
        )
        self.assertEqual(by_id["gemini-2.5-pro-001"]["canonical_id"], "gemini-2.5-pro")
        self.assertTrue(any("exact API-reported" in warning for warning in result["warnings"]))

    def test_missing_thinking_field_keeps_effort_unknown(self):
        result = discover({}, KEY, transport=lambda _plan: {"models": [{
            "name": "models/gemini-3.5-flash",
            "baseModelId": "gemini-3.5-flash",
            "inputTokenLimit": 1_048_576,
            "outputTokenLimit": 65_536,
            "supportedGenerationMethods": ["generateContent"],
        }]})
        self.assertIsNone(result["models"][0]["reasoning"])
        self.assertEqual(result["models"][0]["effort_modes"], [])
        self.assertTrue(any("thinking capability" in warning for warning in result["warnings"]))

    def test_exact_known_id_falls_back_when_base_model_is_missing_or_family_only(self):
        result = discover({}, KEY, transport=lambda _plan: {"models": [
            {
                "name": "models/gemini-3.8-flash",
                "version": "3.8",
                "inputTokenLimit": 1_000_000,
                "outputTokenLimit": 64_000,
                "supportedGenerationMethods": ["generateContent"],
                "thinking": True,
            },
            {
                "name": "models/gemini-3.1-pro-preview",
                "baseModelId": "gemini-3.1-pro",
                "version": "3.1-preview",
                "inputTokenLimit": 1_048_576,
                "outputTokenLimit": 65_536,
                "supportedGenerationMethods": ["generateContent"],
                "thinking": True,
            },
            # A family-looking media suffix is not an exact roster key.
            {
                "name": "models/gemini-3.1-pro-preview-image",
                "baseModelId": "gemini-3.1-pro",
                "inputTokenLimit": 65_536,
                "outputTokenLimit": 32_768,
                "supportedGenerationMethods": ["generateContent"],
                "thinking": True,
            },
        ]})
        by_id = {model["id"]: model for model in result["models"]}
        self.assertEqual(set(by_id), {"gemini-3.8-flash", "gemini-3.1-pro-preview"})
        self.assertIsNone(by_id["gemini-3.8-flash"]["base_model_id"])
        self.assertEqual(by_id["gemini-3.8-flash"]["canonical_id"], "gemini-3.8-flash")
        self.assertEqual(by_id["gemini-3.8-flash"]["effort_modes"], ["low", "medium", "high"])
        self.assertEqual(by_id["gemini-3.1-pro-preview"]["base_model_id"], "gemini-3.1-pro")
        self.assertEqual(by_id["gemini-3.1-pro-preview"]["canonical_id"], "gemini-3.1-pro")
        self.assertEqual(
            by_id["gemini-3.1-pro-preview"]["provider_effort_modes"],
            ["low", "medium", "high"],
        )

    def test_every_documented_tool_model_gets_its_own_effort_set(self):
        expected = {
            "gemini-3.8-flash": ["low", "medium", "high"],
            "gemini-3.7-flash": ["low", "medium", "high"],
            "gemini-3.6-flash": ["minimal", "low", "medium", "high"],
            "gemini-3.5-flash": ["minimal", "low", "medium", "high"],
            "gemini-3.5-flash-lite": ["minimal", "low", "medium", "high"],
            "gemini-3.1-pro-preview": ["minimal", "low", "medium", "high"],
            "gemini-3.1-flash-lite": ["minimal", "low", "medium", "high"],
            "gemini-3-flash-preview": ["minimal", "low", "medium", "high"],
            "gemini-2.5-pro": ["minimal", "low", "medium", "high"],
            "gemini-2.5-flash": ["none", "minimal", "low", "medium", "high"],
            "gemini-2.5-flash-lite": ["none", "minimal", "low", "medium", "high"],
        }
        raw = {"models": [{
            "name": "models/" + model,
            "baseModelId": model,
            "inputTokenLimit": 1_048_576,
            "outputTokenLimit": 65_536,
            "supportedGenerationMethods": ["generateContent"],
            "thinking": True,
        } for model in expected]}
        result = discover({}, KEY, transport=lambda _plan: raw)
        self.assertEqual(
            {model["id"]: model["effort_modes"] for model in result["models"]},
            expected,
        )

    def test_discovery_rejects_conflicting_duplicate_cards_and_pagination_loops(self):
        first = {
            "name": "models/gemini-3.5-flash",
            "baseModelId": "gemini-3.5-flash",
            "inputTokenLimit": 100,
            "outputTokenLimit": 20,
            "supportedGenerationMethods": ["generateContent"],
            "thinking": True,
        }
        second = {**first, "inputTokenLimit": 200}
        with self.assertRaises(GeminiError):
            discover({}, KEY, transport=lambda _plan: {"models": [first, second]})

        calls = 0

        def looping(_plan):
            nonlocal calls
            calls += 1
            return {"models": [], "nextPageToken": "same"}

        with self.assertRaises(GeminiError):
            discover({}, KEY, transport=looping)
        self.assertEqual(calls, 2)


class RequestAndReplayTests(unittest.TestCase):
    def test_initial_request_uses_bearer_auth_tools_usage_and_per_model_effort(self):
        payload = request_payload(
            stream=True,
            output_config={"effort": "xhigh"},
        )
        plan = prepare_request({}, KEY, payload, MODEL, model_spec(), SCOPE, REPLAY_KEY)
        self.assertEqual(plan["url"], BASE_URL + "/chat/completions")
        self.assertEqual(plan["headers"]["Authorization"], "Bearer " + KEY)
        self.assertEqual(plan["headers"]["x-goog-api-client"], CLIENT_HEADER)
        self.assertNotIn("x-goog-api-key", plan["headers"])
        self.assertEqual(plan["body"]["reasoning_effort"], "high")
        self.assertEqual(plan["body"]["stream_options"], {"include_usage": True})
        self.assertEqual(plan["compatibility"]["reasoning_effort"], "xhigh_normalized_to_high")
        function = plan["body"]["tools"][0]["function"]
        self.assertRegex(function["name"], r"^[A-Za-z0-9_-]+$")
        self.assertEqual(plan["tool_name_map"][function["name"]], "workspace.read_file")

    def test_model_specific_effort_and_fast_controls_fail_closed(self):
        # A rank between two the model serves takes the nearer one rather
        # than refusing: one refused effort and the client stops offering the
        # control on this model for the whole session.
        nearer = prepare_request(
            {}, KEY, request_payload(output_config={"effort": "minimal"}),
            "gemini-3.8-flash", model_spec("gemini-3.8-flash"), SCOPE, REPLAY_KEY,
        )
        self.assertEqual(nearer["body"]["reasoning_effort"], "low")
        self.assertEqual(nearer["compatibility"]["reasoning_effort"], "minimal_normalized_to_low")
        with self.assertRaisesRegex(GeminiError, "cannot be disabled"):
            prepare_request(
                {}, KEY, request_payload(thinking={"type": "disabled"}),
                "gemini-2.5-pro", model_spec("gemini-2.5-pro"), SCOPE, REPLAY_KEY,
            )
        disabled = prepare_request(
            {}, KEY, request_payload(thinking={"type": "disabled"}),
            "gemini-2.5-flash", model_spec("gemini-2.5-flash"), SCOPE, REPLAY_KEY,
        )
        self.assertEqual(disabled["body"]["reasoning_effort"], "none")
        with self.assertRaisesRegex(GeminiError, "Fast mode"):
            prepare_request({}, KEY, request_payload(speed="fast"), MODEL, model_spec(), SCOPE, REPLAY_KEY)
        with self.assertRaisesRegex(GeminiError, "unknown"):
            prepare_request(
                {}, KEY, request_payload(output_config={"effort": "high"}), MODEL,
                model_spec(effort_modes=[], reasoning=None), SCOPE, REPLAY_KEY,
            )

    def test_ultra_effort_caps_to_highest_gemini_rank(self):
        plan = prepare_request(
            {}, KEY, request_payload(output_config={"effort": "ultra"}),
            MODEL, model_spec(), SCOPE, REPLAY_KEY,
        )
        self.assertEqual(plan["body"]["reasoning_effort"], "high")
        self.assertEqual(plan["compatibility"]["reasoning_effort"], "ultra_normalized_to_high")

    def test_separate_input_and_output_limits_are_not_combined(self):
        payload = {
            "model": REQUESTED,
            "max_tokens": 190,
            "messages": [{"role": "user", "content": "hi"}],
        }
        spec = model_spec(context=200, max_input=200, max_output=190)
        plan = prepare_request({}, KEY, payload, MODEL, spec, SCOPE, REPLAY_KEY)
        self.assertEqual(plan["body"]["max_tokens"], 190)
        with self.assertRaisesRegex(GeminiError, "input limit"):
            prepare_request(
                {}, KEY, payload, MODEL, {**spec, "context": 10, "max_input": 10},
                SCOPE, REPLAY_KEY,
            )

    def test_complete_sequential_tool_cycle_replays_each_signature_exactly(self):
        first_payload = request_payload()
        first_plan = prepare_request({}, KEY, first_payload, MODEL, model_spec(), SCOPE, REPLAY_KEY)
        wire_name = first_plan["body"]["tools"][0]["function"]["name"]
        first = translate_response(
            {
                "choices": [{
                    "message": {
                        "role": "assistant",
                        "tool_calls": [{
                            "id": "call_a",
                            "type": "function",
                            "function": {"name": wire_name, "arguments": '{"path":"a.txt"}'},
                            "extra_content": {"google": {"thought_signature": "opaque-signature-A"}},
                        }],
                    },
                    "finish_reason": "tool_calls",
                }],
                "usage": {"prompt_tokens": 20, "completion_tokens": 4, "total_tokens": 31},
            },
            REQUESTED, first_plan["tool_name_map"], MODEL, SCOPE, REPLAY_KEY,
            model_spec=model_spec(),
        )
        self.assertEqual(first["content"][0]["type"], "redacted_thinking")
        self.assertTrue(first["content"][0]["data"].startswith(ENVELOPE_PREFIX))
        self.assertNotIn("opaque-signature-A", first["content"][0]["data"])
        self.assertEqual(first["content"][1]["name"], "workspace.read_file")
        self.assertEqual(first["usage"], {"input_tokens": 20, "output_tokens": 11})

        second_payload = request_payload(messages=[
            {"role": "user", "content": "Check a file."},
            {"role": "assistant", "content": first["content"]},
            {"role": "user", "content": [{
                "type": "tool_result", "tool_use_id": "call_a", "content": "file A",
            }]},
        ])
        second_plan = prepare_request({}, KEY, second_payload, MODEL, model_spec(), SCOPE, REPLAY_KEY)
        assistant = next(message for message in second_plan["body"]["messages"] if message["role"] == "assistant")
        self.assertEqual(
            assistant["tool_calls"][0]["extra_content"]["google"]["thought_signature"],
            "opaque-signature-A",
        )
        tool_result = next(message for message in second_plan["body"]["messages"] if message["role"] == "tool")
        self.assertEqual(tool_result["name"], wire_name)

        second = translate_response(
            {
                "choices": [{
                    "message": {"role": "assistant", "tool_calls": [{
                        "id": "call_b",
                        "type": "function",
                        "function": {"name": wire_name, "arguments": '{"path":"b.txt"}'},
                        "extra_content": {"google": {"thought_signature": "opaque-signature-B"}},
                    }]},
                    "finish_reason": "tool_calls",
                }],
                "usage": {"prompt_tokens": 31, "completion_tokens": 5, "total_tokens": 42},
            },
            REQUESTED, second_plan["tool_name_map"], MODEL, SCOPE, REPLAY_KEY,
            model_spec=model_spec(),
        )
        third_payload = request_payload(messages=second_payload["messages"] + [
            {"role": "assistant", "content": second["content"]},
            {"role": "user", "content": [{
                "type": "tool_result", "tool_use_id": "call_b", "content": "file B",
            }]},
        ])
        third_plan = prepare_request({}, KEY, third_payload, MODEL, model_spec(), SCOPE, REPLAY_KEY)
        assistants = [message for message in third_plan["body"]["messages"] if message["role"] == "assistant"]
        self.assertEqual([
            message["tool_calls"][0]["extra_content"]["google"]["thought_signature"]
            for message in assistants
        ], ["opaque-signature-A", "opaque-signature-B"])

    def test_parallel_call_signature_positions_and_unsigned_25_are_preserved(self):
        names = {"read_file": "workspace.read_file"}
        response = translate_response(
            {"choices": [{"message": {"tool_calls": [
                {
                    "id": "a", "function": {"name": "read_file", "arguments": '{"path":"a"}'},
                    "extra_content": {"google": {"thought_signature": "sig-first"}},
                },
                {"id": "b", "function": {"name": "read_file", "arguments": '{"path":"b"}'}},
            ]}, "finish_reason": "tool_calls"}]},
            REQUESTED, names, MODEL, SCOPE, REPLAY_KEY, model_spec=model_spec(),
        )
        plan = prepare_request(
            {}, KEY,
            request_payload(messages=[
                {"role": "user", "content": "both"},
                {"role": "assistant", "content": response["content"]},
                {"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": "a", "content": "A"},
                    {"type": "tool_result", "tool_use_id": "b", "content": "B"},
                ]},
            ]),
            MODEL, model_spec(), SCOPE, REPLAY_KEY,
        )
        calls = next(message for message in plan["body"]["messages"] if message["role"] == "assistant")["tool_calls"]
        self.assertEqual(calls[0]["extra_content"]["google"]["thought_signature"], "sig-first")
        self.assertNotIn("extra_content", calls[1])

        old_model = "gemini-2.5-flash"
        unsigned = translate_response(
            {"choices": [{"message": {"tool_calls": [{
                "id": "old", "function": {"name": "read_file", "arguments": "{}"},
            }]}, "finish_reason": "tool_calls"}]},
            REQUESTED, names, old_model, SCOPE, REPLAY_KEY, model_spec=model_spec(old_model),
        )
        self.assertEqual(unsigned["content"][0]["type"], "redacted_thinking")
        old_plan = prepare_request(
            {}, KEY,
            request_payload(messages=[
                {"role": "user", "content": "old"},
                {"role": "assistant", "content": unsigned["content"]},
                {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "old", "content": "ok"}]},
            ]),
            old_model, model_spec(old_model), SCOPE, REPLAY_KEY,
        )
        old_call = next(message for message in old_plan["body"]["messages"] if message["role"] == "assistant")["tool_calls"][0]
        self.assertNotIn("extra_content", old_call)

    def test_replay_rejects_tamper_foreign_blocks_model_and_account_changes(self):
        response = translate_response(
            {"choices": [{"message": {"tool_calls": [{
                "id": "call", "function": {"name": "read_file", "arguments": '{"path":"safe"}'},
                "extra_content": {"google": {"thought_signature": "sig"}},
            }]}, "finish_reason": "tool_calls"}]},
            REQUESTED, {}, MODEL, SCOPE, REPLAY_KEY, model_spec=model_spec(),
        )
        assistant = {"role": "assistant", "content": response["content"]}
        self.assertEqual(validate_messages([assistant], MODEL, SCOPE, REPLAY_KEY)[0]["tools"], ["sig"])

        changed_output = copy.deepcopy(assistant)
        changed_output["content"][1]["input"]["path"] = "tampered"
        changed_envelope = copy.deepcopy(assistant)
        original_data = changed_envelope["content"][0]["data"]
        changed_envelope["content"][0]["data"] = original_data[:-1] + (
            "A" if original_data[-1] != "A" else "B"
        )
        cases = [
            (changed_output, MODEL, SCOPE),
            (changed_envelope, MODEL, SCOPE),
            (assistant, "gemini-3.6-flash", SCOPE),
            (assistant, MODEL, "another-account-scope"),
        ]
        for message, model, scope in cases:
            with self.subTest(model=model, scope=scope), self.assertRaises(GeminiError):
                validate_messages([message], model, scope, REPLAY_KEY)
        with self.assertRaisesRegex(GeminiError, "foreign"):
            validate_messages([{"role": "assistant", "content": [
                {"type": "redacted_thinking", "data": "provider-native-data"},
                {"type": "text", "text": "answer"},
            ]}], MODEL, SCOPE, REPLAY_KEY)
        with self.assertRaisesRegex(GeminiError, "missing"):
            validate_messages([{"role": "assistant", "content": [
                {"type": "tool_use", "id": "x", "name": "read_file", "input": {}},
            ]}], MODEL, SCOPE, REPLAY_KEY)

    def test_text_signature_survives_the_codex_responses_encryption_round_trip(self):
        translated = translate_response(
            {"choices": [{"message": {
                "content": "The answer.",
                "extra_content": {"google": {"thought_signature": "opaque-text-signature"}},
            }, "finish_reason": "stop"}]},
            REQUESTED, {}, MODEL, SCOPE, REPLAY_KEY, model_spec=model_spec(),
        )
        self.assertEqual(translated["content"][0]["type"], "redacted_thinking")
        with tempfile.TemporaryDirectory() as directory:
            envelope = ReasoningEnvelope(Path(directory))
            response_adapter = MessagesResponsesAdapter("gemini/" + MODEL, envelope, "responses-scope")
            codex_response = response_adapter.from_message(translated)
            reasoning = next(item for item in codex_response["output"] if item["type"] == "reasoning")
            self.assertTrue(reasoning["encrypted_content"].startswith("ph_reasoning_v1."))
            self.assertNotIn("opaque-text-signature", reasoning["encrypted_content"])
            responses_body = {
                "input": [
                    {"role": "user", "content": "Question"},
                    *codex_response["output"],
                    {"role": "user", "content": "Follow up"},
                ],
                "stream": False,
                "store": False,
            }
            messages_payload = to_messages(
                responses_body, "gemini/" + MODEL, model_spec(), envelope, "responses-scope",
            )
            plan = prepare_request(
                {}, KEY, messages_payload, MODEL, model_spec(), SCOPE, REPLAY_KEY,
            )
        assistant = next(message for message in plan["body"]["messages"] if message["role"] == "assistant")
        self.assertEqual(
            assistant["extra_content"]["google"]["thought_signature"],
            "opaque-text-signature",
        )

    def test_tool_signature_survives_a_codex_function_result_round_trip(self):
        translated = translate_response(
            {"choices": [{"message": {"tool_calls": [{
                "id": "codex_call",
                "function": {"name": "read_file", "arguments": '{"path":"notes.txt"}'},
                "extra_content": {"google": {"thought_signature": "opaque-codex-tool-signature"}},
            }]}, "finish_reason": "tool_calls"}]},
            REQUESTED, {}, MODEL, SCOPE, REPLAY_KEY, model_spec=model_spec(),
        )
        with tempfile.TemporaryDirectory() as directory:
            envelope = ReasoningEnvelope(Path(directory))
            response_adapter = MessagesResponsesAdapter("gemini/" + MODEL, envelope, "responses-scope")
            codex_response = response_adapter.from_message(translated)
            reasoning = next(item for item in codex_response["output"] if item["type"] == "reasoning")
            call = next(item for item in codex_response["output"] if item["type"] == "function_call")
            self.assertNotIn("opaque-codex-tool-signature", reasoning["encrypted_content"])
            self.assertEqual(call["call_id"], "codex_call")
            responses_body = {
                "input": [
                    {"role": "user", "content": "Read notes"},
                    *codex_response["output"],
                    {"type": "function_call_output", "call_id": call["call_id"], "output": "notes"},
                ],
                "stream": False,
                "store": False,
                "tools": [{
                    "type": "function", "name": "read_file", "parameters": {"type": "object"},
                }],
            }
            messages_payload = to_messages(
                responses_body, "gemini/" + MODEL, model_spec(), envelope, "responses-scope",
            )
            plan = prepare_request(
                {}, KEY, messages_payload, MODEL, model_spec(), SCOPE, REPLAY_KEY,
            )
        assistant = next(message for message in plan["body"]["messages"] if message["role"] == "assistant")
        self.assertEqual(
            assistant["tool_calls"][0]["extra_content"]["google"]["thought_signature"],
            "opaque-codex-tool-signature",
        )
        result = next(message for message in plan["body"]["messages"] if message["role"] == "tool")
        self.assertEqual(result["tool_call_id"], "codex_call")
        self.assertEqual(result["name"], "read_file")

    def test_codex_namespace_projection_does_not_break_signature_binding(self):
        flattened, tool_map = flatten_tools([{
            "type": "namespace",
            "name": "workspace",
            "tools": [{"type": "function", "description": "Read", "name": "read_file",
                       "parameters": {"type": "object"}}],
        }])
        wire_name = flattened[0]["name"]
        translated = translate_response(
            {"choices": [{"message": {"tool_calls": [{
                "id": "namespaced_call",
                "function": {"name": wire_name, "arguments": '{"path":"notes.txt"}'},
                "extra_content": {"google": {"thought_signature": "namespaced-signature"}},
            }]}, "finish_reason": "tool_calls"}]},
            REQUESTED, {wire_name: wire_name}, MODEL, SCOPE, REPLAY_KEY,
            model_spec=model_spec(),
        )
        with tempfile.TemporaryDirectory() as directory:
            envelope = ReasoningEnvelope(Path(directory))
            response_adapter = MessagesResponsesAdapter("gemini/" + MODEL, envelope, "responses-scope")
            codex_response = response_adapter.from_message(translated)
            call = next(item for item in codex_response["output"] if item["type"] == "function_call")
            output_names(call, tool_map)
            self.assertEqual((call["namespace"], call["name"]), ("workspace", "read_file"))
            items = [
                {"role": "user", "content": "Read notes"},
                *codex_response["output"],
                {"type": "function_call_output", "call_id": call["call_id"], "output": "notes"},
            ]
            input_names(items, tool_map)
            self.assertEqual(call["name"], wire_name)
            messages_payload = to_messages(
                {"input": items, "stream": False, "store": False, "tools": flattened},
                "gemini/" + MODEL, model_spec(), envelope, "responses-scope",
            )
            plan = prepare_request(
                {}, KEY, messages_payload, MODEL, model_spec(), SCOPE, REPLAY_KEY,
            )
        replayed = next(message for message in plan["body"]["messages"] if message["role"] == "assistant")
        self.assertEqual(replayed["tool_calls"][0]["function"]["name"], wire_name)
        self.assertEqual(
            replayed["tool_calls"][0]["extra_content"]["google"]["thought_signature"],
            "namespaced-signature",
        )


class StreamingTests(unittest.TestCase):
    def test_fragmented_stream_buffers_signature_tools_usage_and_finish_quirk(self):
        adapter = GeminiStreamAdapter(
            REQUESTED, {"read_file": "workspace.read_file"}, MODEL, SCOPE, REPLAY_KEY,
            model_spec=model_spec(),
        )
        starts = adapter.start()
        self.assertEqual(starts[0]["type"], "message_start")
        chunks = [
            {"choices": [{"index": 0, "delta": {"content": "Checking "}}]},
            {"choices": [{"index": 0, "delta": {"tool_calls": [{
                "index": 0,
                "id": "stream_call",
                "function": {"name": "read_file", "arguments": '{"path":'},
                "extra_content": {"google": {"thought_signature": "stream-signature"}},
            }]}}]},
            {"choices": [{"index": 0, "delta": {"tool_calls": [{
                "index": 0, "function": {"arguments": '"README.md"}'},
            }]}}]},
            # The compatibility endpoint has returned stop for tool-only
            # streams; presence of calls remains authoritative for Messages.
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
             "usage": {"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 20}},
        ]
        for chunk in chunks:
            self.assertEqual(adapter.feed(chunk), [])
        blocks, stop, usage = reconstruct(adapter.end())
        self.assertEqual([block["type"] for block in blocks], ["redacted_thinking", "text", "tool_use"])
        self.assertEqual(blocks[1]["text"], "Checking ")
        self.assertEqual(blocks[2]["name"], "workspace.read_file")
        self.assertEqual(blocks[2]["input"], {"path": "README.md"})
        self.assertEqual(stop, "tool_use")
        self.assertEqual(usage, {"input_tokens": 10, "output_tokens": 10})
        replay = validate_messages(
            [{"role": "assistant", "content": blocks}], MODEL, SCOPE, REPLAY_KEY,
        )
        self.assertEqual(replay[0]["tools"], ["stream-signature"])

    def test_message_signature_can_arrive_with_empty_final_text_chunk(self):
        adapter = GeminiStreamAdapter(
            REQUESTED, {}, MODEL, SCOPE, REPLAY_KEY, model_spec=model_spec(),
        )
        adapter.start()
        adapter.feed({"choices": [{"delta": {"content": "Final answer"}, "index": 0}]})
        adapter.feed({"choices": [{"delta": {
            "content": "",
            "extra_content": {"google": {"thought_signature": "late-text-signature"}},
        }, "finish_reason": "stop", "index": 0}]})
        blocks, stop, _usage = reconstruct(adapter.end())
        self.assertEqual([block["type"] for block in blocks], ["redacted_thinking", "text"])
        self.assertEqual(blocks[1]["text"], "Final answer")
        self.assertEqual(stop, "end_turn")
        replay = validate_messages(
            [{"role": "assistant", "content": blocks}], MODEL, SCOPE, REPLAY_KEY,
        )
        self.assertEqual(replay[0]["message"], "late-text-signature")

    def test_stream_accepts_complete_parallel_calls_without_indices(self):
        adapter = GeminiStreamAdapter(
            REQUESTED, {}, MODEL, SCOPE, REPLAY_KEY, model_spec=model_spec(),
        )
        adapter.start()
        adapter.feed({"choices": [{"delta": {"tool_calls": [
            {
                "id": "a", "function": {"name": "one", "arguments": '{"x":1}'},
                "extra_content": {"google": {"thought_signature": "sig"}},
            },
            {"id": "b", "function": {"name": "two", "arguments": '{"y":2}' }},
        ]}, "finish_reason": "stop", "index": 0}]})
        blocks, stop, _usage = reconstruct(adapter.end())
        self.assertEqual([block.get("id") for block in blocks[1:]], ["a", "b"])
        self.assertEqual(stop, "tool_use")

    def test_stream_fails_for_missing_required_signature_incomplete_or_changed_data(self):
        missing = GeminiStreamAdapter(
            REQUESTED, {}, MODEL, SCOPE, REPLAY_KEY, model_spec=model_spec(),
        )
        missing.start()
        missing.feed({"choices": [{"delta": {"tool_calls": [{
            "index": 0, "id": "a", "function": {"name": "one", "arguments": "{}"},
        }]}, "finish_reason": "tool_calls"}]})
        with self.assertRaisesRegex(GeminiError, "required thought signature"):
            missing.end()

        incomplete = GeminiStreamAdapter(
            REQUESTED, {}, MODEL, SCOPE, REPLAY_KEY, model_spec=model_spec(),
        )
        incomplete.start()
        incomplete.feed({"choices": [{"delta": {"content": "partial"}}]})
        with self.assertRaisesRegex(GeminiError, "completion signal"):
            incomplete.end()

        changed = GeminiStreamAdapter(
            REQUESTED, {}, MODEL, SCOPE, REPLAY_KEY, model_spec=model_spec(),
        )
        changed.start()
        changed.feed({"choices": [{"delta": {"tool_calls": [{
            "index": 0, "id": "a", "function": {"name": "one", "arguments": "{}"},
            "extra_content": {"google": {"thought_signature": "first"}},
        }]}}]})
        with self.assertRaisesRegex(GeminiError, "changed during streaming"):
            changed.feed({"choices": [{"delta": {"tool_calls": [{
                "index": 0,
                "extra_content": {"google": {"thought_signature": "second"}},
            }]}}]})


if __name__ == "__main__":
    unittest.main()
