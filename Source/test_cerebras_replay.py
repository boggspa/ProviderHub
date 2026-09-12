import copy
import json
import unittest

from cerebras_replay import (
    CerebrasReplayError,
    CerebrasStreamAdapter,
    sign_thinking,
    validate_messages,
)
from providers import ProviderError, prepare_request
from protocol import translate_response


TOKEN = "local-gateway-token-for-tests"
MODEL = "gpt-oss-120b"
SCOPE = "cerebras-connection-signature"


def assistant_content():
    return [
        {"type": "text", "text": "I will inspect both files."},
        {"type": "tool_use", "id": "call_one", "name": "read_file", "input": {"path": "a.txt"}},
        {"type": "tool_use", "id": "call_two", "name": "read_file", "input": {"path": "b.txt", "line": 2}},
    ]


class SignatureTests(unittest.TestCase):
    def signed_message(self, reasoning="Check both paths first."):
        visible = assistant_content()
        block = sign_thinking(reasoning, visible, MODEL, SCOPE, TOKEN)
        return {"role": "assistant", "content": [block, *visible]}

    def test_sign_and_validate_round_trip_without_mutation(self):
        message = self.signed_message()
        original = copy.deepcopy(message)
        result = validate_messages([message], MODEL, SCOPE, TOKEN)
        self.assertEqual(result, {0: "Check both paths first."})
        self.assertEqual(message, original)
        signature = message["content"][0]["signature"]
        self.assertTrue(signature.startswith("mb-cerebras-v1."))
        self.assertLess(len(signature), 80)
        self.assertNotIn("Check", signature)
        self.assertEqual(
            signature,
            sign_thinking("Check both paths first.", assistant_content(), MODEL, SCOPE, TOKEN)["signature"],
        )

    def test_reasoning_and_every_bound_assistant_field_detect_tampering(self):
        mutations = {
            "reasoning": lambda message: message["content"][0].__setitem__("thinking", "changed"),
            "text": lambda message: message["content"][1].__setitem__("text", "changed"),
            "tool_id": lambda message: message["content"][2].__setitem__("id", "other"),
            "tool_name": lambda message: message["content"][2].__setitem__("name", "other"),
            "tool_input": lambda message: message["content"][2]["input"].__setitem__("path", "other"),
            "tool_order": lambda message: message["content"].append(message["content"].pop(2)),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name):
                message = self.signed_message()
                mutate(message)
                with self.assertRaisesRegex(CerebrasReplayError, "signature does not match"):
                    validate_messages([message], MODEL, SCOPE, TOKEN)

    def test_wrong_model_scope_token_and_provider_are_rejected(self):
        message = self.signed_message()
        for model, scope, token in (
            ("other-model", SCOPE, TOKEN),
            (MODEL, "other-scope", TOKEN),
            (MODEL, SCOPE, "other-token"),
        ):
            with self.subTest(model=model, scope=scope, token=token):
                with self.assertRaisesRegex(CerebrasReplayError, "signature does not match"):
                    validate_messages([message], model, scope, token)
        foreign = copy.deepcopy(message)
        foreign["content"][0]["signature"] = foreign["content"][0]["signature"].replace(
            "mb-cerebras-v1.", "mb-other-v1.", 1,
        )
        with self.assertRaisesRegex(CerebrasReplayError, "not signed by this Cerebras gateway"):
            validate_messages([foreign], MODEL, SCOPE, TOKEN)

    def test_nullable_reasoning_and_required_tool_reasoning(self):
        self.assertIsNone(sign_thinking(None, assistant_content(), MODEL, SCOPE, TOKEN))
        self.assertIsNone(sign_thinking("", assistant_content(), MODEL, SCOPE, TOKEN))
        plain = {"role": "assistant", "content": [{"type": "text", "text": "done"}]}
        self.assertEqual(validate_messages([plain], MODEL, SCOPE, TOKEN), {})
        unsigned_tool = {"role": "assistant", "content": assistant_content()}
        with self.assertRaisesRegex(CerebrasReplayError, "missing its signed reasoning"):
            validate_messages([unsigned_tool], MODEL, SCOPE, TOKEN)
        self.assertEqual(
            validate_messages([unsigned_tool], MODEL, SCOPE, TOKEN, require_tool_reasoning=False),
            {},
        )

    def test_foreign_redacted_and_multiple_thinking_blocks_are_rejected(self):
        redacted = {
            "role": "assistant",
            "content": [{"type": "redacted_thinking", "data": "opaque"}, *assistant_content()],
        }
        with self.assertRaisesRegex(CerebrasReplayError, "redacted or foreign"):
            validate_messages([redacted], MODEL, SCOPE, TOKEN)
        multiple = self.signed_message()
        multiple["content"].insert(1, copy.deepcopy(multiple["content"][0]))
        with self.assertRaisesRegex(CerebrasReplayError, "exactly one"):
            validate_messages([multiple], MODEL, SCOPE, TOKEN)
        reordered = self.signed_message()
        reordered["content"].append(reordered["content"].pop(0))
        with self.assertRaisesRegex(CerebrasReplayError, "first assistant content block"):
            validate_messages([reordered], MODEL, SCOPE, TOKEN)


class RequestRoundTripTests(unittest.TestCase):
    def test_nonstream_response_to_verified_tool_request_round_trip(self):
        raw = {
            "choices": [{
                "message": {
                    "role": "assistant",
                    "reasoning": "The requested file must be read before answering.",
                    "content": "I will read it.",
                    "tool_calls": [{
                        "id": "call_preserved_exactly",
                        "type": "function",
                        "function": {"name": "read_file", "arguments": '{"path":"a.txt"}'},
                    }],
                },
                "finish_reason": "tool_calls",
            }],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }
        translated = translate_response(raw, "claude-fable-5", {"read_file": "read_file"})
        thinking = sign_thinking(
            raw["choices"][0]["message"]["reasoning"],
            translated["content"],
            MODEL,
            SCOPE,
            TOKEN,
        )
        assistant = {"role": "assistant", "content": [thinking, *translated["content"]]}
        messages = [
            {"role": "user", "content": "Read a.txt"},
            assistant,
            {"role": "user", "content": [{
                "type": "tool_result",
                "tool_use_id": "call_preserved_exactly",
                "content": "contents",
            }]},
        ]
        verified = validate_messages(messages, MODEL, SCOPE, TOKEN)
        self.assertEqual(verified, {1: raw["choices"][0]["message"]["reasoning"]})
        plan = prepare_request(
            "cerebras",
            {},
            "cerebras-key",
            {
                "model": "claude-fable-5",
                "max_tokens": 2048,
                "messages": messages,
                "tools": [{
                    "name": "read_file",
                    "description": "Read a file",
                    "input_schema": {
                        "type": "object",
                        "properties": {"path": {"type": "string"}},
                        "required": ["path"],
                    },
                }],
            },
            MODEL,
            {
                "tools": True,
                "reasoning": True,
                "effort_modes": ["low", "medium", "high"],
                "reasoning_history": "gateway_signed_replay",
            },
            reasoning_by_message=verified,
        )
        chat_assistant = next(message for message in plan["body"]["messages"] if message["role"] == "assistant")
        self.assertEqual(chat_assistant["reasoning"], raw["choices"][0]["message"]["reasoning"])
        self.assertEqual(chat_assistant["tool_calls"][0]["id"], "call_preserved_exactly")
        tool_result = next(message for message in plan["body"]["messages"] if message["role"] == "tool")
        self.assertEqual(tool_result["tool_call_id"], "call_preserved_exactly")
        self.assertEqual(plan["compatibility"]["verified_reasoning_messages"], 1)
        self.assertTrue(plan["compatibility"]["complete_tool_cycles"])

    def test_request_planner_does_not_trust_unverified_or_misindexed_reasoning(self):
        visible = assistant_content()
        signed = sign_thinking("verified", visible, MODEL, SCOPE, TOKEN)
        payload = {
            "model": "claude-fable-5",
            "messages": [{"role": "assistant", "content": [signed, *visible]}],
        }
        spec = {"reasoning": True, "reasoning_history": "gateway_signed_replay"}
        with self.assertRaisesRegex(ProviderError, "caller-verified"):
            prepare_request("cerebras", {}, "key", payload, MODEL, spec)
        with self.assertRaisesRegex(ProviderError, "does not match"):
            prepare_request(
                "cerebras", {}, "key", payload, MODEL, spec,
                reasoning_by_message={0: "different"},
            )
        without_block = copy.deepcopy(payload)
        without_block["messages"][0]["content"] = visible
        with self.assertRaisesRegex(ProviderError, "missing verified reasoning"):
            prepare_request("cerebras", {}, "key", without_block, MODEL, spec)
        with self.assertRaisesRegex(ProviderError, "caller-verified"):
            prepare_request(
                "cerebras", {}, "key", payload, MODEL, spec,
                reasoning_by_message={1: "verified"},
            )
        with self.assertRaisesRegex(ProviderError, "another provider"):
            prepare_request(
                "mistral", {}, "key", {"model": "x", "messages": [{"role": "user", "content": "hi"}]},
                "mistral-model", {}, reasoning_by_message={0: "forged"},
            )


def reconstruct_blocks(events):
    blocks = {}
    for event in events:
        if event.get("type") == "content_block_start":
            block = copy.deepcopy(event["content_block"])
            if block["type"] == "tool_use":
                block["partial_json"] = ""
            blocks[event["index"]] = block
        elif event.get("type") == "content_block_delta":
            block = blocks[event["index"]]
            delta = event["delta"]
            if delta["type"] == "thinking_delta":
                block["thinking"] += delta["thinking"]
            elif delta["type"] == "signature_delta":
                block["signature"] += delta["signature"]
            elif delta["type"] == "text_delta":
                block["text"] += delta["text"]
            elif delta["type"] == "input_json_delta":
                block["partial_json"] += delta["partial_json"]
    output = []
    for index in sorted(blocks):
        block = blocks[index]
        if block["type"] == "tool_use":
            block["input"] = json.loads(block.pop("partial_json") or "{}")
        output.append(block)
    return output


class StreamAdapterTests(unittest.TestCase):
    def adapter(self, **options):
        return CerebrasStreamAdapter(
            "claude-fable-5",
            {"fn_one": "fn.one", "fn_two": "fn.two"},
            MODEL,
            SCOPE,
            TOKEN,
            **options,
        )

    def test_reasoning_text_and_multiple_chunked_tools_form_one_signed_stream(self):
        adapter = self.adapter()
        start = adapter.start()
        self.assertEqual(start[0]["type"], "message_start")
        chunks = [
            {"choices": [{"index": 0, "delta": {"reasoning": "Inspect "}}]},
            {"choices": [{"index": 0, "delta": {"reasoning": "both.", "content": "Checking."}}]},
            {"choices": [{"index": 0, "delta": {"tool_calls": [
                {"index": 0, "id": "call_one", "function": {"name": "fn_one", "arguments": '{"path":'}},
                {"index": 1, "id": "call_two", "function": {"name": "fn_two", "arguments": '{"path":'}},
            ]}}]},
            {"choices": [{"index": 0, "delta": {"tool_calls": [
                {"index": 1, "function": {"arguments": '"b.txt","line":2}'}},
                {"index": 0, "function": {"arguments": '"a.txt"}'}},
            ]}}]},
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
             "usage": {"prompt_tokens": 24, "completion_tokens": 20}},
        ]
        for chunk in chunks:
            self.assertEqual(adapter.feed(chunk), [])
        events = adapter.end()
        starts = [event for event in events if event["type"] == "content_block_start"]
        self.assertEqual([event["index"] for event in starts], [0, 1, 2, 3])
        self.assertEqual(
            [event["content_block"]["type"] for event in starts],
            ["thinking", "text", "tool_use", "tool_use"],
        )
        thinking_stop = next(i for i, event in enumerate(events) if event == {"type": "content_block_stop", "index": 0})
        first_visible = next(i for i, event in enumerate(events) if event.get("type") == "content_block_start" and event["index"] == 1)
        self.assertLess(thinking_stop, first_visible)
        blocks = reconstruct_blocks(events)
        self.assertEqual(blocks[0]["thinking"], "Inspect both.")
        self.assertEqual(blocks[1]["text"], "Checking.")
        self.assertEqual(blocks[2]["name"], "fn.one")
        self.assertEqual(blocks[2]["input"], {"path": "a.txt"})
        self.assertEqual(blocks[3]["name"], "fn.two")
        self.assertEqual(blocks[3]["input"], {"path": "b.txt", "line": 2})
        self.assertEqual(
            validate_messages([{"role": "assistant", "content": blocks}], MODEL, SCOPE, TOKEN),
            {0: "Inspect both."},
        )
        self.assertEqual(adapter.usage, {"input_tokens": 24, "output_tokens": 20})
        self.assertEqual(events[-2]["delta"]["stop_reason"], "tool_use")
        self.assertEqual(events[-1]["type"], "message_stop")

    def test_nullable_reasoning_text_streams_without_a_thinking_block(self):
        adapter = self.adapter()
        adapter.start()
        self.assertEqual(adapter.feed({"choices": [{"delta": {"reasoning": None, "content": "Ready."}}]}), [])
        self.assertEqual(adapter.feed({
            "choices": [{"delta": {}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 2, "completion_tokens": 1},
        }), [])
        events = adapter.end()
        starts = [event for event in events if event["type"] == "content_block_start"]
        self.assertEqual([event["content_block"]["type"] for event in starts], ["text"])
        self.assertEqual(starts[0]["index"], 0)
        self.assertEqual(reconstruct_blocks(events), [{"type": "text", "text": "Ready."}])

    def test_missing_reasoning_incomplete_json_and_truncation_fail_before_release(self):
        missing = self.adapter()
        missing.start()
        self.assertEqual(missing.feed({"choices": [{"delta": {"tool_calls": [{
            "index": 0,
            "id": "call",
            "function": {"name": "fn_one", "arguments": "{}"},
        }]}, "finish_reason": "tool_calls"}]}), [])
        with self.assertRaisesRegex(CerebrasReplayError, "missing required reasoning"):
            missing.end()

        invalid = self.adapter()
        invalid.start()
        invalid.feed({"choices": [{"delta": {
            "reasoning": "Need a tool.",
            "tool_calls": [{
                "index": 0,
                "id": "call",
                "function": {"name": "fn_one", "arguments": "{"},
            }],
        }, "finish_reason": "tool_calls"}]})
        with self.assertRaises(CerebrasReplayError):
            invalid.end()

        truncated = self.adapter()
        truncated.start()
        self.assertEqual(truncated.feed({"choices": [{"delta": {"content": "partial"}}]}), [])
        with self.assertRaisesRegex(CerebrasReplayError, "before a valid completion signal"):
            truncated.end()


if __name__ == "__main__":
    unittest.main(verbosity=2)
