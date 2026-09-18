"""Offline regression suite. Never uses real credentials or Claude directories."""
import copy
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from bridge_core import (BridgeError, ClaudeProfile, PROFILE_ID, atomic_json, default_settings,
                         read_json, validate_settings)
from gateway import Runtime, Server, rejection_details
from protocol import (StreamTranslator, TokenCalibration, ULTRACODE_NOTE, apply_mapping_options, compact_conversation, compact_threshold, conversation_units,
                      estimated_tokens, function_name, reported_input_tokens,
                      mapping_options_for, model_catalog, rewrite_context_reminders, tool_id,
                      translate_request, translate_response, resolve_model, resolve_mapping_slot, ultracode_active)
from hub_config import claude_routes
from catalogue import build_catalogue, read_observations, route_specs


def config():
    value = default_settings()
    value["port"] = 11436
    value["mappings"] = {k: "test-model" for k in value["mappings"]}
    value["_model_specs"] = {"test-model": {"id": "test-model", "canonical_id": "test-model", "display_name": "Test Model",
        "context": 240000, "aliases": ["test-model"], "reasoning": True, "vision": True, "tools": True, "inference_status": "advertised",
        "effort_modes": ["none", "low", "medium", "high", "max"]}}
    return value


def prompt(**extra):
    value = {"model": "claude-fable-5", "max_tokens": 64, "messages": [{"role": "user", "content": "hello"}]}
    value.update(extra)
    return value


def sized_conversation(turns, chars_per_turn, **extra):
    messages = []
    for index in range(turns):
        messages.append({"role": "user", "content": f"u{index}-" + "U" * chars_per_turn})
        messages.append({"role": "assistant", "content": f"a{index}-" + "A" * chars_per_turn})
    return prompt(messages=messages, **extra)


class ProtocolTests(unittest.TestCase):
    def test_desktop_instruction_roles_preserved(self):
        result, _ = translate_request(prompt(messages=[{"role": "system", "content": "System rule"},
            {"role": "developer", "content": [{"type": "text", "text": "Developer rule"}]},
            {"role": "user", "content": "Hello"}]), config())
        self.assertEqual([m["content"] for m in result["messages"]], ["System rule", "Developer rule", "Hello"])
        self.assertEqual([m["role"] for m in result["messages"]], ["system", "system", "user"])

    def test_system_images_and_tools_preserved(self):
        body = prompt(system=[{"type": "text", "text": "Follow these instructions.", "cache_control": {"type": "ephemeral"}}],
                      tools=[{"name": "local.read/file", "description": "Read file", "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}}}])
        body["messages"][0]["content"] = [{"type": "text", "text": "What is here?"}, {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "YWJj"}}]
        result, names = translate_request(body, config())
        self.assertEqual(result["messages"][0]["content"], "Follow these instructions.")
        self.assertEqual(result["messages"][1]["content"][1]["image_url"], "data:image/png;base64,YWJj")
        self.assertEqual(names[result["tools"][0]["function"]["name"]], "local.read/file")
        self.assertEqual(result["model"], "test-model")

    def test_multi_turn_tools_ids_order_and_errors(self):
        body = prompt(messages=[
            {"role": "user", "content": "Read and edit."},
            {"role": "assistant", "content": [{"type": "text", "text": "Reading."},
                {"type": "tool_use", "id": "toolu_very-long-1", "name": "Read", "input": {"path": "a.txt"}},
                {"type": "tool_use", "id": "toolu_very-long-2", "name": "Read", "input": {"path": "b.txt"}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_very-long-1", "content": "alpha"},
                {"type": "tool_result", "tool_use_id": "toolu_very-long-2", "content": "not found", "is_error": True},
                {"type": "text", "text": "Now edit a.txt."}]}])
        result, _ = translate_request(body, config())
        items = result["messages"]
        self.assertEqual([m["role"] for m in items], ["user", "assistant", "tool", "tool", "user"])
        self.assertEqual(items[1]["tool_calls"][0]["id"], items[2]["tool_call_id"])
        self.assertEqual(len(items[2]["tool_call_id"]), 9)
        self.assertEqual(items[3]["content"], "Tool execution error:\nnot found")
        self.assertEqual(items[4]["content"], "Now edit a.txt.")
        # Replay of an upstream ID is normalised consistently on both sides.
        self.assertEqual(tool_id("abcdef123"), tool_id("abcdef123"))

    def test_unknown_models_and_hosted_tools_fail(self):
        with self.assertRaises(BridgeError): translate_request(prompt(model="unmapped"), config())
        with self.assertRaises(BridgeError): translate_request(prompt(tools=[{"type": "web_search_20250305", "name": "web_search"}]), config())
        with self.assertRaises(BridgeError): translate_request(prompt(messages=[{"role": "user", "content": [{"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": "abc"}}]}]), config())

    def test_mapping_options_omit_system_and_tools_without_mutating(self):
        settings = config()
        settings["mapping_options"] = {"claude-fable-5": {"omit_system": True, "omit_tools": True}}
        tools = [{"name": "Read", "description": "read", "input_schema": {"type": "object"}}]
        body = prompt(system="HARNESS", tools=tools, tool_choice={"type": "auto"})
        original = copy.deepcopy(body)
        stripped = apply_mapping_options(body, settings)
        self.assertEqual(body, original)
        self.assertIsNot(stripped, body)
        self.assertNotIn("system", stripped)
        self.assertNotIn("tools", stripped)
        self.assertNotIn("tool_choice", stripped)
        self.assertEqual(stripped["messages"], body["messages"])
        self.assertTrue(mapping_options_for("claude-fable-5", settings)["omit_system"])
        self.assertTrue(mapping_options_for("fable", settings)["omit_system"])
        self.assertTrue(mapping_options_for("claude-fable-5[1m]", settings)["omit_tools"])
        self.assertFalse(mapping_options_for("claude-haiku-4-5", settings)["omit_tools"])
        untouched = apply_mapping_options(body, config())
        self.assertIs(untouched, body)
        settings["mappings"]["claude-haiku-4-5"] = "other-model"
        settings["mapping_options"]["claude-haiku-4-5"] = {"omit_system": True, "omit_tools": False}
        shared = apply_mapping_options(prompt(model="test-model", system="keep"), settings)
        self.assertEqual(shared.get("system"), "keep")

    def test_mapping_options_compact_limit_defaults_to_none(self):
        settings = config()
        self.assertIsNone(mapping_options_for("claude-fable-5", settings)["compact_limit"])
        settings["mapping_options"] = {"claude-fable-5": {"compact_limit": 100000}}
        options = mapping_options_for("claude-fable-5", settings)
        self.assertEqual(options["compact_limit"], 100000)
        self.assertFalse(options["omit_system"])
        self.assertFalse(options["omit_tools"])
        self.assertEqual(mapping_options_for("fable", settings)["compact_limit"], 100000)
        self.assertEqual(mapping_options_for("claude-fable-5[1m]", settings)["compact_limit"], 100000)
        self.assertIsNone(mapping_options_for("claude-haiku-4-5", settings)["compact_limit"])

    def test_resolve_model_accepts_dated_gateway_family_ids(self):
        mappings = {"claude-fable-5": "mistral/a", "claude-haiku-4-5": "mistral/d", "claude-sonnet-5": "mistral/x-20250101"}
        self.assertEqual(resolve_model("claude-haiku-4-5-20251001", mappings), "mistral/d")
        self.assertEqual(resolve_model("claude-haiku-4-5-20251001[1m]", mappings), "mistral/d")
        self.assertEqual(resolve_mapping_slot("claude-haiku-4-5-20251001", mappings), "claude-haiku-4-5")
        # An upstream id that itself ends in a date still resolves exactly.
        self.assertEqual(resolve_model("mistral/x-20250101", mappings), "mistral/x-20250101")
        # An unlisted Claude family id lands on its family's slot; other ids stay unmapped.
        self.assertEqual(resolve_model("claude-haiku-4-5-2025", mappings), "mistral/d")
        self.assertEqual(resolve_model("claude-opus-4-8[1m]", {**mappings, "claude-opus-5": "mistral/o"}), "mistral/o")
        self.assertEqual(resolve_mapping_slot("claude-sonnet-4-6", mappings), "claude-sonnet-5")
        self.assertIsNone(resolve_mapping_slot("claude-opus-4-8", mappings))
        with self.assertRaises(BridgeError):
            resolve_model("gpt-5", mappings)
        with self.assertRaises(BridgeError):
            resolve_model("claude-opus-4-8", mappings)

    def catalogue_settings(self):
        settings = config()
        settings["_model_specs"]["big-model"] = {**settings["_model_specs"]["test-model"], "id": "big-model", "canonical_id": "big-model",
                                                 "display_name": "Big Model", "context": 1200000, "aliases": ["big-model"]}
        settings["claude_catalogue"] = [
            {"route": "test-model", "tier": "fable", "tier_default": True},
            {"route": "big-model", "tier": "sonnet", "tier_default": True, "compact_limit": 900000},
        ]
        return settings

    def test_catalogue_mode_serves_tier_rows_under_generated_ids(self):
        settings = self.catalogue_settings()
        rows = model_catalog(settings)["data"]
        self.assertEqual([row["id"] for row in rows], ["claude-fable-5-mis-tral-test-model", "claude-sonnet-5-mis-tral-big-model[1m]"])
        self.assertEqual([row["anthropic_family_tier"] for row in rows], ["fable", "sonnet"])
        self.assertTrue(all(row["is_family_default"] for row in rows))
        self.assertIn("fable tier", rows[0]["description"])
        self.assertEqual(rows[1]["max_tokens"], 1200000)
        self.assertIs(rows[1]["supports_1m"], False)
        self.assertEqual(rows[0]["display_name"], "Test Model")
        # The slot table is ignored while a catalogue is set.
        settings["mappings"] = {slot: "missing-model" for slot in settings["mappings"]}
        self.assertEqual(len(model_catalog(settings)["data"]), 2)

    def test_catalogue_mode_resolves_rows_aliases_and_family_stand_ins(self):
        settings = self.catalogue_settings()
        routes = claude_routes(settings)
        self.assertEqual(resolve_model("claude-sonnet-5-mis-tral-big-model[1m]", routes), "big-model")
        self.assertEqual(resolve_model("claude-fable-5-mis-tral-test-model", routes), "test-model")
        self.assertEqual(resolve_model("sonnet", routes), "big-model")
        self.assertEqual(resolve_model("fable", routes), "test-model")
        # No haiku or opus rows: Claude Code's own haiku and opus requests use the nearest tier.
        self.assertEqual(resolve_model("claude-haiku-4-5-20251001", routes), "big-model")
        self.assertEqual(resolve_model("claude-opus-5", routes), "test-model")
        self.assertEqual(resolve_model("claude-sonnet-4-6", routes), "big-model")
        with self.assertRaises(BridgeError):
            resolve_model("gpt-5", routes)
        options = mapping_options_for("claude-sonnet-5-mis-tral-big-model[1m]", settings)
        self.assertEqual(options["compact_limit"], 900000)
        self.assertFalse(options["omit_system"])
        self.assertEqual(mapping_options_for("haiku", settings)["compact_limit"], 900000)
        self.assertIsNone(mapping_options_for("fable", settings)["compact_limit"])
        self.assertIsNone(mapping_options_for("gpt-5", settings)["compact_limit"])
        result, _ = translate_request(prompt(model="claude-fable-5-mis-tral-test-model"), settings)
        self.assertEqual(result["model"], "test-model")
        result, _ = translate_request(prompt(model="claude-sonnet-5-mis-tral-big-model[1m]"), settings)
        self.assertEqual(result["model"], "big-model")

    def test_ultracode_reminders_add_an_orchestration_note_for_the_provider(self):
        settings = config()
        body = prompt(system="Base rules", messages=[
            {"role": "user", "content": [
                {"type": "text", "text": "<system-reminder>Ultracode is on: optimize for the most exhaustive, correct answer.</system-reminder>"},
                {"type": "text", "text": "Fix the bug"}]},
            {"role": "assistant", "content": "On it."},
            {"role": "user", "content": "Next task"}])
        self.assertTrue(ultracode_active(body))
        result, _ = translate_request(body, settings)
        self.assertEqual([m["role"] for m in result["messages"][:3]], ["system", "system", "user"])
        self.assertEqual(result["messages"][0]["content"], "Base rules")
        self.assertEqual(result["messages"][1]["content"], ULTRACODE_NOTE)
        off = prompt(messages=body["messages"] + [{"role": "user", "content": "<system-reminder>Ultracode is off \u2014 the Workflow tool's standard opt-in rule applies again.</system-reminder>"}])
        self.assertFalse(ultracode_active(off))
        translated, _ = translate_request(off, settings)
        self.assertNotIn(ULTRACODE_NOTE, [m.get("content") for m in translated["messages"]])
        keyword = prompt(messages=[{"role": "user", "content": 'The user included the keyword "ultracode", opting this turn into multi-agent orchestration.'}])
        self.assertTrue(ultracode_active(keyword))
        stale = prompt(messages=keyword["messages"] + [{"role": "assistant", "content": "done"}, {"role": "user", "content": "thanks"}])
        self.assertFalse(ultracode_active(stale))
        self.assertFalse(ultracode_active(prompt()))
        self.assertFalse(ultracode_active({"messages": "nope"}))

    def test_compact_conversation_drops_oldest_and_keeps_chronological_order(self):
        body = sized_conversation(5, 1500)
        full = estimated_tokens(body)
        compacted = compact_conversation(body, int(full * 0.5))
        self.assertIsNot(compacted, body)
        self.assertEqual(body["messages"][0]["content"][:3], "u0-")
        kept = compacted["messages"]
        self.assertLess(len(kept), len(body["messages"]))
        self.assertLessEqual(estimated_tokens(compacted), int(full * 0.5) + full * 0.2)
        original = [message["content"][:3] for message in body["messages"]]
        markers = [message["content"][:3] for message in kept]
        # Newest turns survive, the middle is dropped, order stays
        # chronological. The small opening prompt is pinned and carries the
        # compaction note, merged with the first surviving user turn so the
        # history still alternates.
        self.assertEqual(markers[-2:], ["u4-", "a4-"])
        self.assertEqual(markers[0], "u0-")
        self.assertIn("Provider Hub removed", kept[0]["content"])
        self.assertNotIn("u1-", "".join(m["content"] for m in kept))
        self.assertEqual(markers[1:], [marker for marker in original if marker in set(markers[1:])])
        roles = [message["role"] for message in kept]
        for first, second in zip(roles, roles[1:]):
            self.assertNotEqual(first, second)

    def test_compact_threshold_reserves_output_headroom(self):
        self.assertEqual(compact_threshold(131072, {}, reserve_output=32000), 99072)
        self.assertEqual(
            compact_threshold(131072, {"compact_limit": 100000}, reserve_output=32000), 99072)
        # A small request leaves the default threshold alone.
        self.assertEqual(compact_threshold(131072, {}, reserve_output=64), int(131072 * 0.85))
        # An absurd request cannot gut history below a usable floor.
        self.assertEqual(compact_threshold(131072, {}, reserve_output=200000), int(131072 * 0.85))
        self.assertIsNone(compact_threshold(None, {}, reserve_output=32000))
        self.assertIsNone(compact_threshold(8000, {}, reserve_output=32000))

    def test_mapping_options_compact_limit_falls_back_on_shared_route(self):
        settings = config()
        settings["mapping_options"] = {
            "claude-fable-5": {"compact_limit": 100000},
            "claude-opus-5": {"compact_limit": 80000},
        }
        # Every slot maps to test-model, so the bare route is ambiguous; the
        # smallest threshold still applies.
        self.assertEqual(mapping_options_for("test-model", settings)["compact_limit"], 80000)
        self.assertEqual(mapping_options_for("claude-fable-5", settings)["compact_limit"], 100000)
        # Omit flags stay strict: ambiguity never drops harness fields.
        settings["mapping_options"]["claude-fable-5"]["omit_system"] = True
        self.assertFalse(mapping_options_for("test-model", settings)["omit_system"])
        self.assertTrue(mapping_options_for("claude-fable-5", settings)["omit_system"])

    def test_compact_conversation_drops_tool_results_for_dropped_calls(self):
        body = prompt(messages=[
            {"role": "user", "content": "old work " + "U" * 3000},
            {"role": "assistant", "content": [
                {"type": "text", "text": "reading"},
                {"type": "tool_use", "id": "toolu_old", "name": "Read", "input": {"path": "old.txt"}},
            ]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "toolu_old", "content": "old contents"},
            ]},
            {"role": "user", "content": "new work " + "N" * 3000},
        ])
        compacted = compact_conversation(body, estimated_tokens(body) // 2)
        transcript = json.dumps(compacted["messages"])
        self.assertNotIn("toolu_old", transcript)
        self.assertIn("new work", transcript)
        self.assertTrue(all(message.get("content") for message in compacted["messages"]))

    def test_compact_conversation_keeps_tool_cycles_whole_and_contiguous(self):
        messages = [{"role": "user", "content": "Read the sources and summarise."}]
        for index in range(12):
            messages.append({"role": "assistant", "content": [
                {"type": "tool_use", "id": f"toolu_{index}", "name": "shell", "input": {"command": ["cat", f"f{index}.py"]}}]})
            messages.append({"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": f"toolu_{index}", "content": f"r{index}-" + "X" * 2500}]})
        messages.append({"role": "user", "content": "Reply with DONE"})
        body = prompt(messages=messages)
        budget = estimated_tokens(body) // 3
        compacted = compact_conversation(body, budget)
        kept = compacted["messages"]
        self.assertLess(len(kept), len(messages))
        self.assertEqual(kept[-1]["content"], "Reply with DONE")
        self.assertLessEqual(estimated_tokens(compacted), budget)
        calls = {block["id"] for message in kept if message["role"] == "assistant"
                 for block in message["content"] if block.get("type") == "tool_use"}
        results = {block["tool_use_id"] for message in kept
                   if message["role"] == "user" and isinstance(message["content"], list)
                   for block in message["content"] if block.get("type") == "tool_result"}
        # Every surviving call has its result and vice versa (Mistral rejects
        # either orphan), and the survivors are a contiguous tail.
        self.assertTrue(calls)
        self.assertEqual(calls, results)
        # The opening prompt is pinned with the note; after it, the survivors
        # are a contiguous tail of the original history.
        self.assertTrue(kept[0]["content"].startswith("Read the sources"))
        self.assertIn("Provider Hub removed", kept[0]["content"])
        self.assertEqual(kept[1]["role"], "assistant")
        positions = [messages.index(message) for message in kept[1:]]
        self.assertEqual(positions, list(range(positions[0], len(messages))))

    def test_compact_conversation_binds_interleaved_user_text_to_its_tool_cycle(self):
        body = prompt(messages=[
            {"role": "user", "content": "old work " + "O" * 6000},
            {"role": "assistant", "content": [{"type": "tool_use", "id": "toolu_a", "name": "Read", "input": {}}]},
            {"role": "user", "content": [{"type": "text", "text": "note while reading"}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_a", "content": "A" * 2000}]},
            {"role": "user", "content": "new question"},
        ])
        self.assertEqual([len(unit) for unit in conversation_units(body["messages"])], [1, 3, 1])
        compacted = compact_conversation(body, estimated_tokens(body) // 2)
        transcript = json.dumps(compacted["messages"])
        self.assertIn("new question", transcript)
        self.assertNotIn("OOOO", transcript)
        self.assertIn("toolu_a", transcript)
        self.assertIn("note while reading", transcript)
        self.assertEqual(transcript.count('"tool_use"'), transcript.count('"tool_result"'))

    def test_compact_conversation_keeps_the_newest_unit_even_when_it_does_not_fit(self):
        body = prompt(messages=[
            {"role": "user", "content": "earlier " + "E" * 3000},
            {"role": "assistant", "content": "ok"},
            {"role": "user", "content": "latest " + "L" * 3000},
        ])
        compacted = compact_conversation(body, 10)
        self.assertEqual(len(compacted["messages"]), 1)
        self.assertEqual(compacted["messages"][0]["role"], "user")
        self.assertIn("Provider Hub removed 2 earlier messages", compacted["messages"][0]["content"])
        self.assertTrue(compacted["messages"][0]["content"].endswith("L" * 10))

    def test_token_calibration_only_lowers_estimates_within_bounds(self):
        calibration = TokenCalibration()
        self.assertEqual(calibration.factor("mistral/x"), 1.0)
        self.assertEqual(calibration.calibrated("mistral/x", 9000), 9000)
        # Tiny requests are ignored: fixed overhead dominates their ratio.
        self.assertIsNone(calibration.observe("mistral/x", 400, 300))
        self.assertEqual(calibration.factor("mistral/x"), 1.0)
        calibration.observe("mistral/x", 10000, 7000)
        self.assertAlmostEqual(calibration.factor("mistral/x"), 0.735)
        self.assertEqual(calibration.calibrated("mistral/x", 10000), 7350)
        self.assertEqual(calibration.snapshot(), {"mistral/x": 0.7})
        # A provider counting above the estimate never lifts it past 1.0.
        dense = TokenCalibration()
        dense.observe("kimi/y", 10000, 13000)
        self.assertEqual(dense.factor("kimi/y"), 1.0)
        # The floor bounds how far a run of small ratios can pull.
        floor = TokenCalibration()
        for _ in range(20):
            floor.observe("r", 10000, 2000)
        self.assertEqual(floor.factor("r"), 0.5)
        # Cached prefixes count as real input for the ratio.
        self.assertEqual(reported_input_tokens({"input_tokens": 100, "cache_read_input_tokens": 5000, "output_tokens": 9}), 5100)
        self.assertIsNone(reported_input_tokens({"output_tokens": 9}))
        self.assertIsNone(reported_input_tokens(None))

    def test_context_limits_and_model_mappings(self):
        small = config(); small["_model_specs"]["test-model"]["context"] = 8000
        with self.assertRaises(BridgeError): translate_request(prompt(messages=[{"role": "user", "content": "x" * 30000}]), small)
        models = model_catalog(config())["data"]
        self.assertEqual(len(models), 1)
        self.assertEqual(models[0]["display_name"], "Test Model")
        self.assertTrue(models[0]["is_family_default"])
        self.assertEqual(translate_request(prompt(model="sonnet"), config())[0]["model"], "test-model")

    def test_tool_choice_and_explicit_thinking_disable(self):
        settings = config()
        result, _ = translate_request(prompt(thinking={"type": "disabled"}, tool_choice={"type": "any", "disable_parallel_tool_use": True}), settings)
        self.assertEqual(result["tool_choice"], "required")
        self.assertFalse(result["parallel_tool_calls"])
        self.assertEqual(result["reasoning_effort"], "none")

    def test_effort_comes_from_claude_and_never_changes_the_model(self):
        # Bare model IDs route through Mistral by default (v0.2 compat).
        # The Vibe CLI collapses all thinking levels to "none"/"high" on the
        # wire — the alias table mirrors that collapse so the API never sees
        # a value it rejects.
        for native, expected in [("low", "none"), ("medium", "high"), ("high", "high"), ("xhigh", "high"), ("max", "high")]:
            result, _ = translate_request(prompt(output_config={"effort": native}), config())
            self.assertEqual(result["reasoning_effort"], expected)
            self.assertEqual(result["model"], "test-model")
        plain = config(); plain["_model_specs"]["test-model"]["reasoning"] = False
        result, _ = translate_request(prompt(output_config={"effort": "max"}), plain)
        self.assertNotIn("reasoning_effort", result)

    def test_exact_context_limits_and_no_fabricated_fast_mode(self):
        settings = config(); spec = settings["_model_specs"]["test-model"]
        spec["context"] = 1048576
        row = model_catalog(settings)["data"][0]
        self.assertEqual(row["max_input_tokens"], 1048576)
        self.assertTrue(row["id"].endswith("[1m]"))
        self.assertFalse(row["supports_1m"])
        result, _ = translate_request(prompt(model="claude-fable-5[1m]", max_tokens=50000), settings)
        self.assertEqual(result["max_tokens"], 50000)
        spec["context"] = 32768
        result, _ = translate_request(prompt(max_tokens=64000), settings)
        self.assertLess(result["max_tokens"], 32768)
        self.assertGreater(result["max_tokens"], 30000)
        with self.assertRaises(BridgeError): translate_request(prompt(model="claude-fable-5[1m]"), settings)
        with self.assertRaises(BridgeError): translate_request(prompt(speed="fast"), settings)

    def test_missing_metadata_does_not_guess_context(self):
        settings = config(); settings.pop("_model_specs")
        self.assertEqual(model_catalog(settings)["data"], [])
        with self.assertRaises(BridgeError): translate_request(prompt(), settings)

    def test_model_catalog_flags_desktop_baseline_fit(self):
        settings = config()
        self.assertTrue(model_catalog(settings)["data"][0]["fits_desktop_baseline"])
        self.assertNotIn("below desktop baseline", model_catalog(settings)["data"][0]["description"])
        settings["_model_specs"]["test-model"]["context"] = 32768
        small = model_catalog(settings)["data"][0]
        self.assertIs(small["fits_desktop_baseline"], False)
        self.assertIn("below desktop baseline", small["description"])
        settings["_model_specs"]["test-model"]["context"] = 65536
        self.assertTrue(model_catalog(settings)["data"][0]["fits_desktop_baseline"])
        settings["_model_specs"]["test-model"].pop("context")
        unknown = model_catalog(settings)["data"][0]
        self.assertIsNone(unknown["fits_desktop_baseline"])
        self.assertNotIn("below desktop baseline", unknown["description"])

    def test_desktop_context_reminders_rewrite_to_remaining_catalogue_context(self):
        reminder = "<total_tokens>15000000 tokens left</total_tokens>"
        window_line = "<ctx_window>Infinite tokens left, 12/15000000 used</ctx_window>"
        payload = prompt(
            system=reminder,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": "hello\n" + window_line},
                    {"type": "tool_result", "tool_use_id": "toolu_1", "content": reminder},
                ],
            }],
        )
        original = copy.deepcopy(payload)
        estimate = estimated_tokens(payload)
        remaining = 240000 - estimate
        rewritten = rewrite_context_reminders(payload, 240000, estimate)
        self.assertEqual(payload, original)
        self.assertIsNot(rewritten, payload)
        self.assertEqual(rewritten["system"], f"<total_tokens>{remaining} tokens left</total_tokens>")
        self.assertEqual(
            rewritten["messages"][0]["content"][0]["text"],
            f"hello\n<ctx_window>{remaining} tokens left, {estimate}/240000 used</ctx_window>",
        )
        self.assertEqual(
            rewritten["messages"][0]["content"][1]["content"],
            f"<total_tokens>{remaining} tokens left</total_tokens>",
        )
        body = prompt(system=reminder, messages=[{"role": "user", "content": "hello\n" + reminder}])
        remaining = 240000 - estimated_tokens(body)
        result, _ = translate_request(body, config())
        texts = [message["content"] for message in result["messages"] if isinstance(message["content"], str)]
        self.assertEqual(texts[0], f"<total_tokens>{remaining} tokens left</total_tokens>")
        self.assertEqual(texts[1], f"hello\n<total_tokens>{remaining} tokens left</total_tokens>")
        self.assertNotIn("15000000", json.dumps(result))

    def test_desktop_context_reminders_stay_put_without_catalogue_or_in_fences(self):
        fenced = "```\n<total_tokens>15000000 tokens left</total_tokens>\n```"
        payload = prompt(messages=[{"role": "user", "content": fenced}])
        self.assertIs(rewrite_context_reminders(payload, 240000, 10), payload)
        self.assertEqual(payload["messages"][0]["content"], fenced)
        tagged = prompt(messages=[{"role": "user", "content": "<total_tokens>15000000 tokens left</total_tokens>"}])
        self.assertIs(rewrite_context_reminders(tagged, None, 10), tagged)
        self.assertIs(rewrite_context_reminders(tagged, "262144", 10), tagged)
        prose = prompt(messages=[{"role": "user", "content": "see <total_tokens>15000000 tokens left</total_tokens> please"}])
        self.assertIs(rewrite_context_reminders(prose, 240000, 10), prose)
        indented = {"messages": [{"role": "user", "content": "   <total_tokens>15000000 tokens left</total_tokens>\r\nkeep"}]}
        rewritten = rewrite_context_reminders(indented, 1000, 10)
        self.assertEqual(
            rewritten["messages"][0]["content"],
            "   <total_tokens>990 tokens left</total_tokens>\r\nkeep",
        )

    def test_nonstream_response_tools_and_usage(self):
        response = {"choices": [{"message": {"content": "", "tool_calls": [{"id": "123456789", "function": {"name": "Read", "arguments": '{"path":"a"}'}}]}, "finish_reason": "tool_calls"}], "usage": {"prompt_tokens": 10, "completion_tokens": 3}}
        result = translate_response(response, "claude-fable-5", {"Read": "Read"})
        self.assertEqual(result["stop_reason"], "tool_use")
        self.assertEqual(result["content"][0]["input"], {"path": "a"})
        self.assertEqual(result["usage"], {"input_tokens": 10, "output_tokens": 3})

    def test_nonstream_predicted_tool_calls_recovered(self):
        content = 'Bash{"command": "ls", "description": "list"}Bash{"command": "pwd", "description": "where"}'
        response = {"choices": [{"message": {"content": content}, "finish_reason": "stop"}]}
        result = translate_response(response, "claude-fable-5", {"Bash": "Bash"})
        self.assertEqual([b["type"] for b in result["content"]], ["tool_use", "tool_use"])
        self.assertEqual(result["content"][0]["input"], {"command": "ls", "description": "list"})
        self.assertEqual(result["content"][1]["input"], {"command": "pwd", "description": "where"})
        self.assertNotEqual(result["content"][0]["id"], result["content"][1]["id"])
        self.assertEqual(result["stop_reason"], "tool_use")

    def test_predicted_calls_unmap_names_and_keep_surrounding_prose(self):
        content = 'I will read it. Read{"path": "a.txt"} Then fn_one {"x": 1} done.'
        response = {"choices": [{"message": {"content": content}, "finish_reason": "stop"}]}
        result = translate_response(response, "claude-fable-5", {"Read": "Read", "fn_one": "fn.one"})
        blocks = result["content"]
        self.assertEqual([b["type"] for b in blocks], ["text", "tool_use", "text", "tool_use", "text"])
        self.assertEqual(blocks[0]["text"], "I will read it. ")
        self.assertEqual(blocks[1]["name"], "Read")
        self.assertEqual(blocks[1]["input"], {"path": "a.txt"})
        self.assertEqual(blocks[2]["text"], " Then ")
        self.assertEqual(blocks[3]["name"], "fn.one")
        self.assertEqual(blocks[4]["text"], " done.")
        self.assertEqual(result["stop_reason"], "tool_use")

    def test_predicted_calls_stay_text_when_unconfident(self):
        cases = [
            ('Bash{"command": "ls"}', {"Read": "Read"}),            # unknown tool name
            ('Bash{"command": }', {"Bash": "Bash"}),                # malformed JSON
            ("Bash{1}", {"Bash": "Bash"}),                          # JSON is not an object
            ('Bash{"command": "ls"}', {}),                          # no tools advertised
        ]
        for content, names in cases:
            response = {"choices": [{"message": {"content": content}, "finish_reason": "stop"}]}
            result = translate_response(response, "claude-fable-5", names)
            self.assertEqual(result["content"], [{"type": "text", "text": content}], content)
            self.assertEqual(result["stop_reason"], "end_turn")

    def test_recovered_tool_call_round_trips_through_request_translation(self):
        response = {"choices": [{"message": {"content": 'Read{"path": "a.txt"}'}, "finish_reason": "stop"}]}
        result = translate_response(response, "claude-fable-5", {"Read": "Read"})
        call = result["content"][0]
        self.assertEqual(call["type"], "tool_use")
        body = prompt(messages=[
            {"role": "user", "content": "Read a.txt"},
            {"role": "assistant", "content": [call]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": call["id"], "content": "alpha"}]}])
        upstream, _ = translate_request(body, config())
        assistant, tool_message = upstream["messages"][1], upstream["messages"][2]
        self.assertEqual(assistant["tool_calls"][0]["function"]["name"], "Read")
        self.assertEqual(json.loads(assistant["tool_calls"][0]["function"]["arguments"]), {"path": "a.txt"})
        self.assertEqual(assistant["tool_calls"][0]["id"], tool_message["tool_call_id"])
        self.assertEqual(len(tool_message["tool_call_id"]), 9)

    def test_stream_predicted_tool_calls_recovered_across_chunks(self):
        translator = StreamTranslator("claude-fable-5", {"Bash": "Bash", "Read": "Read"})
        events = translator.start()
        # The call is held while its name and braces are still incomplete.
        self.assertEqual(translator.feed({"choices": [{"delta": {"content": "Rea"}}]}), [])
        self.assertEqual(translator.feed({"choices": [{"delta": {"content": 'd{"pa'}}]}), [])
        events += translator.feed({"choices": [{"delta": {"content": 'th": "a.txt"}'}}]})
        events += translator.feed({"choices": [{"delta": {"content": ' and Bash{"command": "ls"} done'}}]})
        events += translator.feed({"choices": [{"delta": {}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 9, "completion_tokens": 7}})
        events += translator.end()
        starts = [e for e in events if e["type"] == "content_block_start"]
        self.assertEqual([s["content_block"]["type"] for s in starts], ["tool_use", "text", "tool_use", "text"])
        self.assertEqual(starts[0]["content_block"]["name"], "Read")
        self.assertEqual(starts[2]["content_block"]["name"], "Bash")
        fragments = [e["delta"]["partial_json"] for e in events
                     if e["type"] == "content_block_delta" and e["index"] == starts[0]["index"]]
        self.assertEqual(json.loads("".join(fragments)), {"path": "a.txt"})
        text_deltas = [e["delta"]["text"] for e in events
                       if e["type"] == "content_block_delta" and e["delta"]["type"] == "text_delta"]
        self.assertEqual("".join(text_deltas), " and  done")
        stops = sorted(e["index"] for e in events if e["type"] == "content_block_stop")
        self.assertEqual(stops, [0, 1, 2, 3])
        self.assertEqual(events[-2]["delta"]["stop_reason"], "tool_use")
        self.assertEqual(events[-1]["type"], "message_stop")

    def test_stream_prose_immediately_then_predicted_call(self):
        translator = StreamTranslator("claude-fable-5", {"Bash": "Bash"})
        events = translator.start()
        events += translator.feed({"choices": [{"delta": {"content": 'Running Bash{"command": "ls"} now.'}}]})
        events += translator.feed({"choices": [{"delta": {}, "finish_reason": "stop"}]})
        events += translator.end()
        starts = [e for e in events if e["type"] == "content_block_start"]
        self.assertEqual([s["content_block"]["type"] for s in starts], ["text", "tool_use", "text"])
        self.assertEqual(starts[1]["content_block"]["name"], "Bash")
        self.assertEqual(events[-2]["delta"]["stop_reason"], "tool_use")

    def test_stream_unbalanced_predicted_call_stays_text(self):
        translator = StreamTranslator("claude-fable-5", {"Bash": "Bash"})
        events = translator.start()
        self.assertEqual(translator.feed({"choices": [{"delta": {"content": 'Bash{"command": "ls"'}}]}), [])
        events += translator.feed({"choices": [{"delta": {}, "finish_reason": "stop"}]})
        events += translator.end()
        starts = [e for e in events if e["type"] == "content_block_start"]
        self.assertEqual([s["content_block"]["type"] for s in starts], ["text"])
        text_deltas = [e["delta"]["text"] for e in events if e["type"] == "content_block_delta"]
        self.assertEqual("".join(text_deltas), 'Bash{"command": "ls"')
        self.assertEqual(events[-2]["delta"]["stop_reason"], "end_turn")

    def test_stream_hold_cap_flushes_near_json_as_text(self):
        translator = StreamTranslator("claude-fable-5", {"Bash": "Bash"})
        translator.start()
        with patch("protocol._MAX_TOOL_CALL_HOLD", 8):
            events = translator.feed({"choices": [{"delta": {"content": 'Bash{"abcdefghij'}}]})
        self.assertEqual("".join(e["delta"]["text"] for e in events if e["type"] == "content_block_delta"),
                         'Bash{"abcdefghij')

    def test_stream_text_and_interleaved_partial_arguments(self):
        translator = StreamTranslator("claude-fable-5", {"fn_one": "fn.one", "fn_two": "fn.two"})
        events = translator.start()
        events += translator.feed({"choices": [{"delta": {"content": "First "}}]})
        events += translator.feed({"choices": [{"delta": {"content": "read."}}]})
        for idx, name in [(0, "fn_one"), (1, "fn_two")]:
            events += translator.feed({"choices": [{"delta": {"tool_calls": [{"index": idx, "id": f"call{idx}", "function": {"name": name, "arguments": '{"path":'}}]}}]})
        for idx in (1, 0):
            events += translator.feed({"choices": [{"delta": {"tool_calls": [{"index": idx, "function": {"arguments": f'"{idx}.txt"}}'}}]}}]})
        events += translator.feed({"choices": [{"delta": {}, "finish_reason": "tool_calls"}], "usage": {"prompt_tokens": 24, "completion_tokens": 20}})
        events += translator.end()
        starts = [e for e in events if e["type"] == "content_block_start"]
        self.assertEqual([s["content_block"]["type"] for s in starts], ["text", "tool_use", "tool_use"])
        self.assertEqual(starts[1]["content_block"]["name"], "fn.one")
        for start in starts[1:]:
            fragments = [e["delta"]["partial_json"] for e in events if e["type"] == "content_block_delta" and e["index"] == start["index"]]
            self.assertIn("path", json.loads("".join(fragments)))
        self.assertEqual(events[-2]["delta"]["stop_reason"], "tool_use")
        self.assertEqual(events[-1]["type"], "message_stop")

    def test_late_tool_metadata_and_truncated_stream(self):
        translator = StreamTranslator("x", {})
        self.assertEqual(translator.feed({"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": '{"x":1}'}}]}}]}), [])
        events = translator.feed({"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "abc", "function": {"name": "Read"}}]}, "finish_reason": "tool_calls"}]})
        self.assertEqual(events[0]["type"], "content_block_start")
        self.assertEqual(events[1]["delta"]["partial_json"], '{"x":1}')
        self.assertEqual(translator.end()[-1]["type"], "message_stop")
        with self.assertRaises(BridgeError): StreamTranslator("x", {}).end()

    def test_invalid_arguments_cannot_complete_stream(self):
        translator = StreamTranslator("x", {})
        translator.feed({"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "abc", "function": {"name": "Read", "arguments": "{"}}]}, "finish_reason": "tool_calls"}]})
        with self.assertRaises(BridgeError): translator.end()


class RejectionDetailsTests(unittest.TestCase):
    def test_routed_model_attributed_verbatim(self):
        self.assertEqual(rejection_details({"model": "mistral/test-model"}), ("mistral/test-model", None))

    def test_unparseable_route_kept_in_usage(self):
        model, usage = rejection_details({"model": "no such model!"})
        self.assertEqual(model, "")
        self.assertEqual(usage, {"requested": "no such model!"})

    def test_long_garbage_truncated(self):
        _, usage = rejection_details({"model": "x" * 500})
        self.assertEqual(usage, {"requested": "x" * 120})

    def test_nothing_attributable_yields_none(self):
        for payload in (None, [], "text", {}, {"model": None}, {"model": 42}, {"model": ""}, {"messages": []}):
            with self.subTest(payload=payload):
                self.assertIsNone(rejection_details(payload))


class PlanRejectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        atomic_json(self.root / "settings.json", config())
        atomic_json(self.root / "catalog.json", {"schema_version": 2, "models": list(config()["_model_specs"].values())})
        self.runtime = Runtime(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_context_rejection_reports_estimate_and_limit(self):
        body = prompt(system="S" * 800000, messages=[{"role": "user", "content": "hi"}])
        with self.assertRaises(BridgeError) as raised:
            self.runtime.plan(body)
        self.assertIn("~", str(raised.exception))
        self.assertIn("240,000-token context limit", str(raised.exception))

    def test_unknown_route_rejection_names_catalogue_refresh(self):
        with self.assertRaises(BridgeError) as raised:
            self.runtime.plan(prompt(model="no/such-model"))
        self.assertIn("not in the current provider catalogue", str(raised.exception))


class ProfileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.profile = ClaudeProfile(self.base / "bridge", self.base / "support", self.base / "claude-home")
        self.previous = {"appliedId": "ollama-profile", "entries": [{"id": "ollama-profile", "name": "Ollama"}], "custom": 7}
        atomic_json(self.profile.meta, self.previous)
        atomic_json(self.profile.normal, {"deploymentMode": "1p", "mcpServers": {"keep": {}}, "unrelated": "retained"})
        atomic_json(self.profile.third_party, {"deploymentMode": "3p", "preferences": {"test": 1}})
        self.session = self.base / "support/Claude-3p/claude-code-sessions/session.json"
        self.session.parent.mkdir(parents=True)
        self.session.write_text('{"conversation":"leave exactly alone"}')

    def tearDown(self): self.tmp.cleanup()

    def test_activate_restore_preserves_sessions_and_previous_profile(self):
        before = self.session.read_bytes()
        self.profile.activate(config(), "local-token", require_closed=False)
        self.assertTrue(self.profile.active())
        self.assertEqual(read_json(self.profile.normal)["mcpServers"], {"keep": {}})
        self.assertEqual(read_json(self.profile.profile)["inferenceCredentialKind"], "static")
        self.assertIs(read_json(self.profile.profile)["modelPrefer1mContext"], True)
        # Profile features default off: Claude's config check flags any key
        # its build does not recognise, even an explicit false, so disabled
        # features are omitted entirely (claudeInChromeEnabled is never
        # written at all — it is control-plane-only).
        written = read_json(self.profile.profile)
        for field in ("dictationEnabled", "builtinBrowserEnabled", "claudeInChromeEnabled", "scheduledTasksEnabled", "coworkTabEnabled"):
            self.assertNotIn(field, written)
        result = self.profile.restore(require_closed=False)
        self.assertTrue(result["restored"])
        self.assertEqual(read_json(self.profile.meta), self.previous)
        self.assertEqual(read_json(self.profile.normal)["deploymentMode"], "1p")
        self.assertEqual(read_json(self.profile.third_party)["deploymentMode"], "3p")
        self.assertEqual(self.session.read_bytes(), before)
        self.assertFalse(self.profile.profile.exists())
        self.assertFalse(self.profile.journal.exists())

    def test_enabled_claude_features_reach_the_profile(self):
        settings = config()
        settings["claude_features"] = {**settings["claude_features"], "dictation": True, "cowork_tab": True,
                                       "builtin_browser": True, "claude_in_chrome": True}
        self.profile.activate(settings, "local-token", require_closed=False)
        written = read_json(self.profile.profile)
        self.assertIs(written["dictationEnabled"], True)
        self.assertIs(written["coworkTabEnabled"], True)
        self.assertIs(written["builtinBrowserEnabled"], True)
        self.assertNotIn("claudeInChromeEnabled", written)
        self.profile.restore(require_closed=False)
        self.assertFalse(self.profile.profile.exists())

    def test_restore_preserves_unrelated_concurrent_edits(self):
        self.profile.activate(config(), "local-token", require_closed=False)
        normal = read_json(self.profile.normal); normal["unrelated"] = "new value"; atomic_json(self.profile.normal, normal)
        meta = read_json(self.profile.meta); meta["entries"].append({"id": "another", "name": "New profile"}); atomic_json(self.profile.meta, meta)
        self.profile.restore(require_closed=False)
        self.assertEqual(read_json(self.profile.normal)["unrelated"], "new value")
        self.assertEqual(read_json(self.profile.meta)["appliedId"], "ollama-profile")
        self.assertEqual([e["id"] for e in read_json(self.profile.meta)["entries"]], ["ollama-profile", "another"])

    def catalogue_settings(self):
        settings = config()
        settings["claude_catalogue"] = [{"route": "test-model", "tier": "opus", "tier_default": True}]
        settings["_display_names"] = {"test-model": "Test Model"}
        return settings

    def test_catalogue_rows_are_taught_to_claude_code_and_removed_on_restore(self):
        code = self.profile.code_settings
        self.assertFalse(code.exists())
        result = self.profile.activate(self.catalogue_settings(), "local-token", require_closed=False)
        self.assertEqual(result["claude_code_settings"], "written")
        written = read_json(code)
        self.assertEqual(written["modelPicker"]["options"], [
            {"model": "claude-opus-5-mis-tral-test-model", "label": "Test Model",
             "description": "Provider Hub \u00b7 test-model \u00b7 behaves as claude-opus-5", "behavesAs": "claude-opus-5"}])
        self.assertNotIn("enableWorkflows", written)
        self.assertTrue(self.profile.restore(require_closed=False)["restored"])
        self.assertFalse(code.exists())
        self.assertFalse(self.profile.journal.exists())

    def test_code_settings_keep_user_rows_and_honor_edits_made_meanwhile(self):
        code = self.profile.code_settings
        atomic_json(code, {"theme": "dark", "modelPicker": {"replaceBuiltInOptions": False, "options": [
            {"model": "my-model", "label": "Mine"}, {"model": "old-hub-row", "description": "Provider Hub \u00b7 stale"}]}})
        settings = self.catalogue_settings()
        settings["claude_workflows"] = True
        self.profile.activate(settings, "local-token", require_closed=False)
        written = read_json(code)
        self.assertEqual([row["model"] for row in written["modelPicker"]["options"]], ["my-model", "claude-opus-5-mis-tral-test-model"])
        self.assertIs(written["modelPicker"]["replaceBuiltInOptions"], False)
        self.assertIs(written["enableWorkflows"], True)
        self.assertEqual(written["theme"], "dark")
        # A row added while the profile is active survives; only the hub's row goes.
        edited = read_json(code)
        edited["modelPicker"]["options"].append({"model": "added-later", "label": "Later"})
        atomic_json(code, edited)
        self.profile.restore(require_closed=False)
        after = read_json(code)
        self.assertEqual([row["model"] for row in after["modelPicker"]["options"]], ["my-model", "added-later"])
        self.assertIs(after["modelPicker"]["replaceBuiltInOptions"], False)
        self.assertNotIn("enableWorkflows", after)
        self.assertEqual(after["theme"], "dark")

    def test_unreadable_code_settings_are_left_alone(self):
        code = self.profile.code_settings
        code.parent.mkdir(parents=True)
        code.write_text("{not json")
        result = self.profile.activate(self.catalogue_settings(), "local-token", require_closed=False)
        self.assertTrue(result["claude_code_settings"].startswith("skipped"))
        self.assertTrue(self.profile.active())
        self.assertEqual(code.read_text(), "{not json")
        self.assertTrue(self.profile.restore(require_closed=False)["restored"])
        self.assertEqual(code.read_text(), "{not json")

    def test_code_settings_switch_off_writes_nothing(self):
        settings = self.catalogue_settings()
        settings["claude_code_settings"] = False
        result = self.profile.activate(settings, "local-token", require_closed=False)
        self.assertNotIn("claude_code_settings", result)
        self.assertFalse(self.profile.code_settings.exists())
        self.profile.restore(require_closed=False)
        self.assertFalse(self.profile.code_settings.exists())

    def test_external_profile_switch_wins(self):
        self.profile.activate(config(), "local-token", require_closed=False)
        meta = read_json(self.profile.meta); meta["appliedId"] = "different-profile"; atomic_json(self.profile.meta, meta)
        result = self.profile.restore(require_closed=False)
        self.assertGreater(result["preserved_external_changes"], 0)
        self.assertEqual(read_json(self.profile.meta)["appliedId"], "different-profile")
        self.assertNotIn(PROFILE_ID, [e["id"] for e in read_json(self.profile.meta)["entries"]])

    def test_running_claude_is_never_switched(self):
        with patch("bridge_core.claude_running", return_value=True):
            with self.assertRaises(BridgeError): self.profile.activate(config(), "token")
        self.assertFalse(self.profile.journal.exists())
        self.assertEqual(read_json(self.profile.meta), self.previous)

    def test_interrupted_write_rolls_back(self):
        import bridge_core
        original = bridge_core.atomic_json
        calls = [0]
        def fail_once(path, value):
            calls[0] += 1
            if calls[0] == 4: raise OSError("Injected interrupted write")
            return original(path, value)
        with patch("bridge_core.atomic_json", side_effect=fail_once):
            with self.assertRaises(OSError): self.profile.activate(config(), "token", require_closed=False)
        self.assertEqual(read_json(self.profile.meta), self.previous)
        self.assertEqual(read_json(self.profile.normal)["deploymentMode"], "1p")
        self.assertFalse(self.profile.profile.exists())

    def test_symlink_and_invalid_journal_are_rejected(self):
        self.profile.profile.symlink_to(self.session)
        with self.assertRaises(BridgeError): self.profile.activate(config(), "token", require_closed=False)
        self.assertEqual(self.session.read_text(), '{"conversation":"leave exactly alone"}')
        self.profile.profile.unlink()
        atomic_json(self.profile.journal, {"version": 1, "operations": [{"path": str(self.session)}]})
        with self.assertRaises(BridgeError): self.profile.restore(require_closed=False)


class CatalogueTests(unittest.TestCase):
    def test_aliases_grouped_but_context_variants_remain_distinct(self):
        def card(identifier, canonical, context, **extra):
            return {"id": identifier, "name": canonical, "max_context_length": context,
                    "capabilities": {"completion_chat": True, "function_calling": True, "reasoning": True}, **extra}
        raw = {"data": [card("model-v1", "model", 262144), card("model-latest", "model", 262144),
                        card("model-1m", "model", 1048576), card("old", "old", 1000, deprecation="2020-01-01")]}
        settings = config(); settings["mappings"] = {key: "model-latest" for key in settings["mappings"]}
        result = build_catalogue(raw, settings, {})
        self.assertEqual(len(result["models"]), 2)
        self.assertEqual(result["retired_ids"], 1)
        specs = route_specs(result)
        self.assertIs(specs["model-v1"], specs["model-latest"])
        self.assertEqual(specs["model-latest"]["id"], "model-latest")
        self.assertEqual(specs["model-1m"]["context"], 1048576)
        self.assertNotEqual(specs["model-1m"]["display_name"], specs["model-v1"]["display_name"])

    def test_quota_failure_does_not_mean_model_retirement(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            events = [{"event": "completed", "model": "model", "time": "a"},
                      {"event": "error", "model": "model", "status": 429, "time": "b"}]
            (root / "activity.jsonl").write_text("\n".join(json.dumps(event) for event in events))
            observed = read_observations(root)
            self.assertEqual(observed["model"]["status"], "quota_limited")
            self.assertEqual(observed["model"]["last_success"], "a")


class MockMistral(BaseHTTPRequestHandler):
    mode = "normal"
    requests = []
    protocol_version = "HTTP/1.1"
    def log_message(self, *_): pass
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).requests.append(body)
        if self.mode == "rate_limit":
            raw = b'{"message":"Test quota exceeded"}'
            self.send_response(429); self.send_header("Content-Length", str(len(raw))); self.end_headers(); self.wfile.write(raw); return
        if not body["stream"]:
            raw = json.dumps({"choices": [{"message": {"content": "Connected", "tool_calls": None}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 5, "completion_tokens": 2}}).encode()
            self.send_response(200); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(raw))); self.end_headers(); self.wfile.write(raw); return
        self.send_response(200); self.send_header("Content-Type", "text/event-stream"); self.send_header("Connection", "close"); self.end_headers()
        self.wfile.write(b'data: {"choices":[{"delta":{"content":"Hello"}}]}\n\n'); self.wfile.flush()
        if self.mode == "slow":
            time.sleep(2)
        if self.mode != "truncated":
            try:
                self.wfile.write(b'data: {"choices":[{"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":5,"completion_tokens":2}}\n\ndata: [DONE]\n\n'); self.wfile.flush()
            except OSError: pass
        self.close_connection = True


class CompactionPlanTests(unittest.TestCase):
    """Server-less Runtime.plan coverage: no sockets, runs anywhere."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        atomic_json(self.root / "settings.json", config())
        atomic_json(self.root / "catalog.json", {"schema_version": 2, "models": list(config()["_model_specs"].values())})
        self.runtime = Runtime(self.root, "http://127.0.0.1:1/v1", key="TEST-SECRET-NEVER-LOG")

    def tearDown(self):
        self.tmp.cleanup()

    def test_plan_compaction_leaves_room_for_requested_output(self):
        for spec in self.runtime.settings["_model_specs"].values():
            spec["context"] = 131072
        body = sized_conversation(60, 2600, max_tokens=32000)
        body["messages"].append({"role": "user", "content": "continue"})
        self.assertGreaterEqual(estimated_tokens(body), 99072)
        plan = self.runtime.plan(body)
        report = plan["compatibility"]["auto_compact"]
        self.assertEqual(report["threshold"], 99072)
        self.assertLessEqual(report["estimated_after"], 99072)
        self.assertGreaterEqual(131072 - report["estimated_after"], 32000)
        # Full requested output survives: no headroom clamp was needed.
        self.assertEqual(plan["body"]["max_tokens"], 32000)
        self.assertNotIn("output_headroom_clamped", plan.get("compatibility", {}))

    def test_plan_records_output_headroom_clamp(self):
        for spec in self.runtime.settings["_model_specs"].values():
            spec["context"] = 8000
        body = prompt(system="S" * 18000, max_tokens=2000)
        estimate = estimated_tokens(body)
        self.assertLess(estimate, 8000)
        plan = self.runtime.plan(body)
        note = plan["compatibility"]["output_headroom_clamped"]
        self.assertEqual(note, {"requested": 2000, "allowed": 8000 - estimate})
        self.assertEqual(plan["body"]["max_tokens"], 8000 - estimate)

    def test_plan_records_compacted_activity_event(self):
        for spec in self.runtime.settings["_model_specs"].values():
            spec["context"] = 12000
        body = sized_conversation(8, 2600)
        body["messages"].append({"role": "user", "content": "continue"})
        self.runtime.plan(body)
        activity = (self.root / "activity.jsonl").read_text()
        self.assertIn('"event": "compacted"', activity)
        self.assertIn('"estimated_before"', activity)
        self.assertIn('"estimated_after"', activity)


class GatewayTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        atomic_json(self.root / "settings.json", config())
        atomic_json(self.root / "catalog.json", {"schema_version": 2, "models": list(config()["_model_specs"].values())})
        MockMistral.mode = "normal"; MockMistral.requests = []
        self.upstream = ThreadingHTTPServer(("127.0.0.1", 0), MockMistral)
        self.upstream.daemon_threads = True
        threading.Thread(target=self.upstream.serve_forever, daemon=True).start()
        self.runtime = Runtime(self.root, f"http://127.0.0.1:{self.upstream.server_port}/v1", key="TEST-SECRET-NEVER-LOG")
        self.server = Server(self.runtime, 0)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.upstream.shutdown(); self.upstream.server_close(); self.tmp.cleanup()

    def request(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=4)
        hs = {"Authorization": "Bearer " + self.runtime.token, "Content-Type": "application/json"}
        hs.update(headers or {})
        connection.request(method, path, json.dumps(body) if body is not None else None, hs)
        response = connection.getresponse(); data = response.read(); status = response.status; info = dict(response.getheaders()); connection.close()
        deadline = time.monotonic() + 1
        while self.runtime.status()["active"] and time.monotonic() < deadline: time.sleep(.01)
        return status, data, info

    def test_auth_origin_and_host_rejections(self):
        self.assertEqual(self.request("GET", "/v1/models", headers={"Authorization": "Bearer bad"})[0], 401)
        self.assertEqual(self.request("GET", "/v1/models", headers={"Host": "untrusted.example"})[0], 403)
        self.assertEqual(self.request("GET", "/v1/models")[0], 200)

    def test_browser_origin_requests_are_cors_enabled(self):
        # The desktop webview sends Origin on its model-discovery fetch; the
        # gateway must answer the preflight and echo ACAO or the picker
        # spins forever. Cross-site pages still hit the token check.
        status, _, headers = self.request("OPTIONS", "/v1/models", headers={
            "Origin": "https://claude.ai", "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "authorization"})
        self.assertEqual(status, 204)
        self.assertEqual(headers.get("Access-Control-Allow-Origin"), "https://claude.ai")
        self.assertEqual(headers.get("Access-Control-Allow-Methods"), "GET, POST, OPTIONS")
        self.assertEqual(headers.get("Access-Control-Allow-Headers"), "authorization")
        status, _, headers = self.request("GET", "/v1/models", headers={"Origin": "https://claude.ai"})
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Access-Control-Allow-Origin"), "https://claude.ai")
        status, data, _ = self.request("GET", "/v1/models", headers={"Origin": "https://untrusted.example",
                                                                      "Authorization": "Bearer bad"})
        self.assertEqual(status, 401)

    def test_text_roundtrip_and_private_logs(self):
        status, data, _ = self.request("POST", "/v1/messages", prompt())
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(data)["content"][0]["text"], "Connected")
        self.assertEqual(MockMistral.requests[0]["model"], "test-model")
        self.assertEqual(self.runtime.completed, 1)
        log = (self.root / "activity.jsonl").read_text()
        for secret in ("TEST-SECRET-NEVER-LOG", "hello", "Connected"):
            self.assertNotIn(secret, log)

    def test_desktop_token_reminders_are_rewritten_before_upstream(self):
        reminder = "<total_tokens>15000000 tokens left</total_tokens>"
        body = prompt(system=reminder, messages=[{"role": "user", "content": "hello"}])
        remaining = 240000 - estimated_tokens(body)
        status, data, _ = self.request("POST", "/v1/messages", body)
        self.assertEqual(status, 200)
        combined = json.dumps(MockMistral.requests[0])
        self.assertNotIn("15000000", combined)
        self.assertIn(f"<total_tokens>{remaining} tokens left</total_tokens>", combined)
        self.assertEqual(self.runtime.plan(body)["compatibility"]["context_reminders"], "catalogue_remaining")

    def test_stream_is_valid_anthropic_sse(self):
        status, data, headers = self.request("POST", "/v1/messages", prompt(stream=True))
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "text/event-stream")
        events = [json.loads(line[6:]) for line in data.decode().splitlines() if line.startswith("data: ")]
        self.assertEqual(events[0]["type"], "message_start")
        self.assertEqual(events[-1]["type"], "message_stop")
        self.assertEqual(events[-2]["usage"]["output_tokens"], 2)

    def test_truncation_is_an_error_not_success(self):
        MockMistral.mode = "truncated"
        status, data, _ = self.request("POST", "/v1/messages", prompt(stream=True))
        self.assertEqual(status, 200)
        self.assertIn(b'"type": "error"', data)
        self.assertNotIn(b'"type": "message_stop"', data)
        self.assertEqual(self.runtime.failed, 1)

    def test_rate_limit_and_token_estimate(self):
        MockMistral.mode = "rate_limit"
        # The gateway absorbs persistent pressure with backoff before
        # surfacing 429; patch the delays so the round-trip fits the
        # harness timeout. The asserted mapping is unchanged.
        with patch("rate_limit.BACKOFF_BASE", 0.01), \
                patch("rate_limit.BACKOFF_CAP", 0.05), \
                patch("rate_limit.random.uniform", return_value=0):
            status, data, _ = self.request("POST", "/v1/messages", prompt())
        self.assertEqual(status, 429)
        self.assertEqual(json.loads(data)["error"]["type"], "rate_limit_error")
        status, data, headers = self.request("POST", "/v1/messages/count_tokens", prompt())
        self.assertEqual(status, 200)
        self.assertEqual(headers["X-Mistral-Bridge-Token-Count"], "estimate")
        self.assertGreater(json.loads(data)["input_tokens"], 0)

    def test_unknown_route_rejection_is_logged_without_counting_failure(self):
        status, data, _ = self.request("POST", "/v1/messages", prompt(model="no/such-model"))
        self.assertEqual(status, 400)
        self.assertIn("not in the current provider catalogue", data.decode())
        events = [json.loads(line) for line in (self.root / "activity.jsonl").read_text().splitlines()]
        rejected = [event for event in events if event.get("event") == "rejected"]
        self.assertEqual(len(rejected), 1)
        self.assertEqual(rejected[0]["model"], "no/such-model")
        self.assertEqual(rejected[0]["status"], 400)
        self.assertEqual(self.runtime.status()["failed"], 0)
        self.assertEqual(self.runtime.status()["completed"], 0)

    def test_context_rejection_is_logged_with_route(self):
        body = prompt(system="S" * 800000, messages=[{"role": "user", "content": "hi"}])
        status, data, _ = self.request("POST", "/v1/messages", body)
        self.assertEqual(status, 400)
        self.assertIn("Provider Hub did not send this request", data.decode())
        self.assertIn("above the 240,000-token context limit", data.decode())
        events = [json.loads(line) for line in (self.root / "activity.jsonl").read_text().splitlines()]
        rejected = [event for event in events if event.get("event") == "rejected"]
        self.assertEqual(len(rejected), 1)
        self.assertEqual(rejected[0]["model"], "claude-fable-5")
        self.assertEqual(rejected[0]["status"], 400)
        self.assertEqual(self.runtime.status()["failed"], 0)

    def test_mapping_options_drop_harness_before_context_preflight(self):
        specs = self.runtime.settings["_model_specs"]
        self.assertTrue(specs)
        for spec in specs.values():
            spec["context"] = 2048
        tools = [{"name": "Read", "description": "T" * 4000,
                  "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}}}]
        body = prompt(system="S" * 4000, tools=tools, tool_choice={"type": "auto"})
        self.assertGreaterEqual(estimated_tokens(body), 2048)
        status, data, _ = self.request("POST", "/v1/messages", body)
        self.assertEqual(status, 400)
        self.assertIn("2,048-token context limit", json.loads(data)["error"]["message"])
        count_status, count_data, _ = self.request("POST", "/v1/messages/count_tokens", body)
        self.assertEqual(count_status, 200)
        self.assertGreaterEqual(json.loads(count_data)["input_tokens"], 2048)
        self.runtime.settings["mapping_options"] = {
            "claude-fable-5": {"omit_system": True, "omit_tools": True},
        }
        status, data, _ = self.request("POST", "/v1/messages", body)
        self.assertEqual(status, 200, data)
        upstream = MockMistral.requests[-1]
        self.assertNotIn("tools", upstream)
        self.assertFalse(any(message.get("role") == "system" for message in upstream["messages"]))
        plan = self.runtime.plan(body)
        self.assertEqual(plan["compatibility"]["omitted_mapping_fields"], ["system", "tools"])
        self.assertNotIn("context_reminders", plan.get("compatibility", {}))
        count_status, count_data, _ = self.request("POST", "/v1/messages/count_tokens", body)
        self.assertEqual(count_status, 200)
        self.assertLess(json.loads(count_data)["input_tokens"], 2048)

    def test_plan_compacts_before_enforcing_context_limit(self):
        for spec in self.runtime.settings["_model_specs"].values():
            spec["context"] = 12000
        body = sized_conversation(8, 2600)
        body["messages"].append({"role": "user", "content": "continue"})
        self.assertGreaterEqual(estimated_tokens(body), 12000)
        plan = self.runtime.plan(body)
        self.assertLess(len(plan["body"]["messages"]), len(body["messages"]))
        report = plan.get("compatibility", {}).get("auto_compact", {})
        self.assertEqual(report.get("threshold"), int(12000 * 0.85))
        self.assertLess(report.get("estimated_after", 0), report.get("estimated_before", 0))

    def test_plan_honors_per_slot_compact_limit(self):
        body = sized_conversation(30, 2600)
        body["messages"].append({"role": "user", "content": "continue"})
        estimate = estimated_tokens(body)
        self.assertGreater(estimate, 40000)
        self.assertLess(estimate, int(240000 * 0.85))
        plain = self.runtime.plan(body)
        self.assertEqual(len(plain["body"]["messages"]), len(body["messages"]))
        self.assertNotIn("auto_compact", plain.get("compatibility", {}))
        self.runtime.settings["mapping_options"] = {
            "claude-fable-5": {"compact_limit": 40000},
        }
        plan = self.runtime.plan(body)
        self.assertLess(len(plan["body"]["messages"]), len(body["messages"]))
        self.assertEqual(plan["compatibility"]["auto_compact"]["threshold"], 40000)
        # An override above the catalogue window clamps to the window instead
        # of disabling protection: an over-limit conversation still compacts.
        # The 64 requested output tokens reserve headroom below the window.
        huge = sized_conversation(150, 2600)
        huge["messages"].append({"role": "user", "content": "continue"})
        self.assertGreaterEqual(estimated_tokens(huge), 240000)
        self.runtime.settings["mapping_options"] = {
            "claude-fable-5": {"compact_limit": 1000000},
        }
        clamped = self.runtime.plan(huge)
        self.assertEqual(clamped["compatibility"]["auto_compact"]["threshold"], 240000 - 64)

    def test_plan_rejects_conversation_that_cannot_compact(self):
        for spec in self.runtime.settings["_model_specs"].values():
            spec["context"] = 12000
        body = prompt(system="S" * 40000)
        self.assertGreaterEqual(estimated_tokens(body), 12000)
        with self.assertRaisesRegex(BridgeError, "12,000-token context limit"):
            self.runtime.plan(body)

    def test_client_cancellation_clears_active_request(self):
        MockMistral.mode = "slow"
        connection = socket.create_connection(("127.0.0.1", self.server.server_port), timeout=3)
        data = json.dumps(prompt(stream=True)).encode()
        request = f"POST /v1/messages HTTP/1.1\r\nHost: 127.0.0.1:{self.server.server_port}\r\nAuthorization: Bearer {self.runtime.token}\r\nContent-Type: application/json\r\nContent-Length: {len(data)}\r\n\r\n".encode() + data
        connection.sendall(request)
        connection.recv(4096)
        connection.shutdown(socket.SHUT_RDWR); connection.close()
        deadline = time.monotonic() + 1.5
        while self.runtime.active and time.monotonic() < deadline: time.sleep(.05)
        self.assertEqual(self.runtime.active, 0)
        self.assertEqual(self.runtime.completed, 0)


class NativeMessageNormalisationTests(unittest.TestCase):
    def test_null_string_and_missing_fields_are_filled_in(self):
        from protocol import normalize_native_message, response_shape
        self.assertEqual(normalize_native_message({"type": "message", "role": "assistant", "content": None})["content"], [])
        text = normalize_native_message({"role": "assistant", "content": "hi"})
        self.assertEqual((text["type"], text["content"], text["stop_reason"]), ("message", [{"type": "text", "text": "hi"}], "end_turn"))
        bare = normalize_native_message({"content": []})
        self.assertEqual((bare["type"], bare["role"], bare["stop_reason"], bare["usage"]),
                         ("message", "assistant", "max_tokens", {"input_tokens": 0, "output_tokens": 0}))
        block = normalize_native_message({"type": "message", "content": {"type": "text", "text": "one"}})
        self.assertEqual(block["content"], [{"type": "text", "text": "one"}])
        untouched = {"error": {"type": "x", "message": "secret"}}
        self.assertEqual(normalize_native_message(untouched), untouched)
        self.assertEqual(normalize_native_message("nope"), "nope")

    def test_response_shape_describes_without_content(self):
        from protocol import response_shape
        self.assertEqual(response_shape({"error": {"type": "x", "message": "secret"}}),
                         {"keys": ["error"], "type": "None", "content": "NoneType", "error_type": "x"})
        self.assertEqual(response_shape({"type": "message", "content": [{"type": "text", "text": "secret"}, {"type": "thinking"}]}),
                         {"keys": ["content", "type"], "type": "message", "content": "list:text,thinking"})
        self.assertEqual(response_shape([1]), {"json": "list"})
        self.assertNotIn("secret", json.dumps(response_shape({"content": "secret", "type": "message"})))


if __name__ == "__main__": unittest.main(verbosity=2)
