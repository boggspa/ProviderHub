"""Compaction must preserve tool cycles, steering, and authenticated replay."""
import copy
import http.client
import json
from pathlib import Path
import tempfile
import socket
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from bridge_core import BridgeError
from responses_bridge import ReasoningEnvelope
from responses_compact import PREFIX, _split, compact_payload, expand_items, scope_for
from responses_native import prepare_native
from test_cli_host_tools import MODELS


def long_history():
    items = [{"role": "user", "content": "Repair the task without reverting existing work."}]
    for index in range(12):
        items += [{"type": "function_call", "call_id": f"call-{index}", "name": "read_file",
                   "arguments": json.dumps({"path": f"file-{index}.txt"})},
                  {"type": "function_call_output", "call_id": f"call-{index}",
                   "output": f"observed result {index}"}]
    return items


class CompactionTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.runtime = SimpleNamespace(root=Path(directory.name), replay_key="test-replay", token="test-token",
            upstream_url=None, provider_key=lambda provider: "", settings={
                "providers": {provider: {"credential_mode": "cli"} for provider in MODELS},
                "_model_specs": {provider + "/" + model: {"context": 200000, "max_output": 16384,
                    "tools": True, "vision": True} for provider, model in MODELS.items()}})
        self.route = "codex/" + MODELS["codex"]
        self.calls = []

    def summarize(self, route, transcript, instructions):
        self.calls.append((route, transcript, instructions))
        return "Earlier reads are complete. Preserve existing work. Continue the requested repair.", {
            "input_tokens": 100, "output_tokens": 20}

    def compact(self, items=None, **extra):
        return compact_payload(self.runtime, {"model": self.route,
            "input": long_history() if items is None else items, **extra}, self.summarize)

    def test_one_user_many_tools_compacts_and_preserves_complete_tail(self):
        original = long_history()
        before = copy.deepcopy(original)
        result = self.compact(original)
        self.assertEqual(result["object"], "response.compaction")
        self.assertEqual(result["usage"]["total_tokens"], 120)
        self.assertTrue(result["output"][0]["encrypted_content"].startswith(PREFIX))
        self.assertLess(len(result["output"]), len(original))
        self.assertEqual(result["output"][1:], original[-8:])
        self.assertEqual(original, before)
        self.assertIn("observed result 7", self.calls[0][1])
        self.assertNotIn("observed result 11", self.calls[0][1])

    def test_pending_call_and_intervening_steers_are_preserved(self):
        items = long_history()
        pending = {"type": "function_call", "call_id": "pending", "name": "read_file", "arguments": "{}"}
        items += [pending, {"role": "user", "content": "Actually inspect b.txt first."},
                  {"role": "user", "content": "Keep the prior changes."}]
        result = self.compact(items)
        self.assertEqual(result["output"][-3:], items[-3:])
        self.assertNotIn('"call_id": "pending"', self.calls[0][1])

    def test_split_never_separates_parallel_calls_from_results(self):
        items = long_history()
        start = len(items)
        items += [{"type": "function_call", "call_id": f"p-{index}", "name": "read_file",
                   "arguments": "{}"} for index in range(5)]
        items += [{"role": "user", "content": "Steer between call and result."}]
        items += [{"type": "function_call_output", "call_id": f"p-{index}", "output": "done"}
                  for index in range(5)]
        prefix, tail = _split(items)
        self.assertEqual(len(prefix), start)
        self.assertEqual(tail, items[start:])

    def test_standing_instructions_keep_their_role(self):
        instructions = {"role": "developer", "content": "Never change unrelated files."}
        result = self.compact([instructions, *long_history()])
        self.assertEqual(result["output"][0], instructions)

    def test_encrypted_summary_replays_after_restart_and_rejects_tampering(self):
        result = self.compact()
        _, scope = scope_for(self.runtime, self.route)
        envelope = ReasoningEnvelope(self.runtime.root)
        expanded = expand_items(result["output"], envelope, scope)
        self.assertIn("Earlier reads are complete", expanded[0]["content"][0]["text"])
        with self.assertRaises(BridgeError):
            expand_items(result["output"], envelope, "different-account")
        bad = copy.deepcopy(result["output"])
        bad[0]["encrypted_content"] = bad[0]["encrypted_content"][:-4] + "xxxx"
        with self.assertRaises(BridgeError):
            expand_items(bad, envelope, scope)

    def test_every_cli_route_and_vibe_api_route_accept_compacted_input(self):
        self.runtime.settings["providers"]["mistral"] = {"credential_mode": "vibe"}
        vibe_route = "mistral/mistral-vibe-cli-latest"
        self.runtime.settings["_model_specs"][vibe_route] = {"context": 200000, "max_output": 16384}
        for route in self.runtime.settings["_model_specs"]:
            with self.subTest(route=route):
                result = compact_payload(self.runtime, {"model": route, "input": long_history()}, self.summarize)
                payload = {"model": route, "input": result["output"] + [
                    {"role": "user", "content": "Resume with this latest correction."}],
                    "stream": False, "store": False}
                with patch("responses_native.validate_connection", return_value={"base_url": "https://test.invalid"}), \
                        patch("responses_native._auth_headers", return_value={}):
                    plan = prepare_native(self.runtime, payload)
                self.assertEqual(plan["protocol"], "messages_bridge")
                serialized = json.dumps(plan["body"]["messages"])
                self.assertIn("Earlier reads are complete", serialized)
                self.assertIn("Resume with this latest correction.", serialized)
                self.assertIn("observed result 11", serialized)
                self.assertNotIn(PREFIX, serialized)

    def test_short_input_needs_no_summary_and_invalid_summary_keeps_original(self):
        short = [{"role": "user", "content": "hello"}]
        self.assertEqual(self.compact(short)["output"], short)
        self.assertEqual(self.calls, [])
        original = long_history()
        before = copy.deepcopy(original)
        with self.assertRaises(BridgeError):
            compact_payload(self.runtime, {"model": self.route, "input": original}, lambda *args: ("", {}))
        self.assertEqual(original, before)

    def test_nested_compaction_summarizes_prior_summary(self):
        first = self.compact()["output"]
        for index in range(10):
            first += [{"role": "assistant", "content": "progress " + str(index)}]
        second = self.compact(first)
        self.assertEqual(sum(item.get("type") == "compaction" for item in second["output"]), 1)
        self.assertIn("Earlier reads are complete", self.calls[-1][1])

    def test_early_steer_does_not_pin_all_later_tool_history(self):
        original = long_history()
        correction = {"role": "user", "content": "Preserve the old filenames and focus on b.txt."}
        original.insert(3, correction)
        result = self.compact(original)
        self.assertLess(len(result["output"]), len(original) // 2)
        self.assertIn(correction, result["output"])
        self.assertEqual(result["output"][-8:], original[-8:])
        self.assertIn(correction["content"], self.calls[0][1])

    def test_single_large_message_is_summarized(self):
        original = [{"role": "user", "content": "large input " * 10000}]
        result = self.compact(original)
        self.assertGreater(len(self.calls), 1)
        segments = [transcript.split("\n", 1)[1] for _, transcript, _ in self.calls
                    if transcript.startswith("Transcript segment ")]
        restored = json.loads("".join(segments))
        self.assertEqual(restored[0]["content"][0]["text"], original[0]["content"])
        self.assertEqual([item["type"] for item in result["output"]], ["compaction"])
        self.assertLess(len(json.dumps(result["output"])), len(json.dumps(original)))

    def test_large_completed_tool_cycle_is_summarized_as_a_unit(self):
        items = [{"role": "user", "content": "Inspect the output."},
                 {"type": "function_call", "call_id": "large", "name": "read_file", "arguments": "{}"},
                 {"type": "function_call_output", "call_id": "large", "output": "result " * 12000},
                 {"role": "user", "content": "Continue using this correction."}]
        result = self.compact(items)
        self.assertEqual(result["output"][-1], items[-1])
        self.assertFalse(any(item.get("type") in {"function_call", "function_call_output"}
                             for item in result["output"]))
        self.assertIn("large", self.calls[0][1])

    def test_large_unresolved_batch_is_never_summarized(self):
        items = long_history() + [
            {"type": "function_call", "call_id": "pending", "name": "read_file", "arguments": "{}"},
            {"role": "user", "content": "Keep this pending correction " * 4000}]
        prefix, tail = _split(items)
        self.assertNotIn(items[-2], prefix)
        self.assertEqual(tail[-2:], items[-2:])

    def test_oversized_steer_is_summarized_without_reexpanding_history(self):
        correction = {"role": "user", "content": "Preserve this corrected requirement. " * 4000}
        items = long_history() + [correction]
        result = self.compact(items)
        self.assertLess(len(json.dumps(result["output"])), len(json.dumps(items)) // 2)
        self.assertNotIn(correction, result["output"])
        segments = [transcript.split("\n", 1)[1] for _, transcript, _ in self.calls
                    if transcript.startswith("Transcript segment ")]
        restored = json.loads("".join(segments))
        self.assertTrue(any(block.get("text") == correction["content"]
                            for message in restored for block in message["content"]))


class CompactionHTTPTests(unittest.TestCase):
    def setUp(self):
        from test_cli_routes import ResponsesEndToEndTest
        ResponsesEndToEndTest.setUp(self)

    def tearDown(self):
        from test_cli_routes import ResponsesEndToEndTest
        ResponsesEndToEndTest.tearDown(self)

    def request(self, path, body):
        client = http.client.HTTPConnection("127.0.0.1", self.gateway.server_port, timeout=10)
        try:
            client.request("POST", path, json.dumps(body), {
                "Authorization": "Bearer " + self.runtime.token,
                "Content-Type": "application/json"})
            response = client.getresponse()
            return response.status, json.loads(response.read())
        finally:
            client.close()

    def test_compact_then_resume_over_http_preserves_tool_results_and_steering(self):
        import cli_routes
        requests = []
        def run(request, **kwargs):
            requests.append(request)
            yield {"type": "text_delta", "text": "Earlier reads completed; preserve existing changes."
                   if len(requests) == 1 else "Resumed with your correction."}
            yield {"type": "message_stop", "stop_reason": "end_turn"}
        cli_routes._cache["grok"].run_turn = run
        items = long_history() + [{"role": "user", "content": "Actually inspect b.txt next."}]
        status, compacted = self.request("/v1/responses/compact", {
            "model": "grok/grok-4.6", "input": items})
        self.assertEqual(status, 200, compacted)
        self.assertEqual(compacted["object"], "response.compaction")
        self.assertTrue(any(item.get("type") == "compaction" for item in compacted["output"]))
        self.assertEqual(requests[0]["tools"], [])
        status, resumed = self.request("/v1/responses", {
            "model": "grok/grok-4.6", "input": compacted["output"] + [
                {"role": "user", "content": "Keep the original filenames."}], "store": False})
        self.assertEqual(status, 200, resumed)
        self.assertEqual(resumed["status"], "completed")
        text = json.dumps(requests[1]["messages"])
        for retained in ("Earlier reads completed", "observed result 11", "Actually inspect b.txt next.",
                         "Keep the original filenames."):
            self.assertIn(retained, text)
        deadline = time.monotonic() + 2
        while self.runtime.active and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertEqual(self.runtime.active, 0, "gateway did not release the completed request")

    def test_failed_compaction_does_not_return_a_replacement_history(self):
        import cli_routes
        def run(request, **kwargs):
            yield {"type": "error", "message": "injected summary failure"}
        cli_routes._cache["grok"].run_turn = run
        original = long_history()
        status, result = self.request("/v1/responses/compact", {
            "model": "grok/grok-4.6", "input": original})
        self.assertGreaterEqual(status, 400)
        self.assertNotIn("output", result)
        self.assertIn("unchanged", result["error"]["message"])

    def test_disconnect_stops_silent_summary_and_prevents_later_segments(self):
        import cli_routes
        from test_cli_session import FakeProcess, make_session
        entered, finished = threading.Event(), threading.Event()
        requests = []

        def run(request, **kwargs):
            requests.append(request)
            session = make_session(FakeProcess(script=None))
            try:
                entered.set()
                yield from session.events(timeout=30)
            finally:
                session.close()
                finished.set()

        cli_routes._cache["grok"].run_turn = run
        client = http.client.HTTPConnection("127.0.0.1", self.gateway.server_port, timeout=5)
        try:
            client.request("POST", "/v1/responses/compact", json.dumps({
                "model": "grok/grok-4.6", "input": [
                    {"role": "user", "content": "large transcript " * 20000}]}), {
                "Authorization": "Bearer " + self.runtime.token,
                "Content-Type": "application/json"})
            self.assertTrue(entered.wait(3), "summary did not start")
            client.sock.shutdown(socket.SHUT_RDWR)
            client.close()
            self.assertTrue(finished.wait(3), "cancelled compaction left a silent CLI running")
            deadline = time.monotonic() + 2
            while self.runtime.active and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertEqual(self.runtime.active, 0)
            self.assertEqual(len(requests), 1, "a later summary segment started after cancellation")
        finally:
            client.close()


if __name__ == "__main__":
    unittest.main()
