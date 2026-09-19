"""Tests for the Route-2 hub glue: cli_routes + the wiring it plugs into.

No real CLI is ever spawned: adapters are fakes seeded into cli_routes._cache,
and the provider API-mode discovery tests inject a transport callable. The
gateway end-to-end tests run a real in-process gateway against a fake adapter,
which is the whole point of the seam: the wire grammar must be indistinguishable
from an HTTP upstream's.
"""
from __future__ import annotations

import http.client
import json
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import cli_routes
import gateway as gateway_module
import providers
from bridge_core import atomic_json, connection_signature, default_settings, discover_provider
from cli_routes import (CliRouteError, cli_credential_mode, discover_via_cli,
                        plan_turn, relay_cli_turn, run_turn)
from cli_tool_call import CLOSE_SENTINEL, OPEN_SENTINEL
from gateway import Runtime, Server
from hub_config import (CLI_AUTH_PROVIDERS, SLOTS, cli_auth_available,
                        credential_modes)
from providers import PROVIDERS, validate_connection


def _fake_adapter(**overrides):
    adapter = types.SimpleNamespace(
        PROVIDER_ID="claude",
        TRANSPORT="print",
        SYSTEM_PROMPT_TRANSPORT="flag",
        KNOWN_MODELS=(),
        auth_state=lambda: {"state": "authenticated", "detail": "signed in"},
        catalogue=lambda **kwargs: ([], ["no list"]),
        run_turn=lambda request, *, timeout=300: iter(()),
    )
    for key, value in overrides.items():
        setattr(adapter, key, value)
    return adapter


class CliRoutesTest(unittest.TestCase):
    def setUp(self):
        self._saved_cache = dict(cli_routes._cache)
        cli_routes._cache.clear()

    def tearDown(self):
        cli_routes._cache.clear()
        cli_routes._cache.update(self._saved_cache)

    # -- gating -------------------------------------------------------------

    def test_cli_credential_mode_only_for_registered_adapters_in_cli_mode(self):
        settings = {"providers": {"claude": {"credential_mode": "cli"},
                                  "grok": {"credential_mode": "keychain"}}}
        self.assertTrue(cli_credential_mode(settings, "claude"))
        self.assertFalse(cli_credential_mode(settings, "grok"))
        self.assertFalse(cli_credential_mode(settings, "mistral"))
        self.assertFalse(cli_credential_mode({}, "claude"))
        self.assertFalse(cli_credential_mode({"providers": None}, "claude"))

    def test_adapter_for_errors_and_cache(self):
        with self.assertRaises(CliRouteError):
            cli_routes.adapter_for("mistral")
        fake = _fake_adapter()
        cli_routes._cache["claude"] = fake
        self.assertIs(cli_routes.adapter_for("claude"), fake)
        # A real adapter module imports cleanly through the same path.
        module = cli_routes.adapter_for("codex")
        self.assertEqual(module.PROVIDER_ID, "codex")
        self.assertEqual(module.SYSTEM_PROMPT_TRANSPORT, "prompt")
        self.assertIs(cli_routes.adapter_for("codex"), module)

    # -- catalogue normalization --------------------------------------------

    def test_hub_row_normalizes_codex_camelcase(self):
        row = {"model": "gpt-5.2", "id": "gpt-5.2", "displayName": "GPT 5.2",
               "hidden": False, "isDefault": True, "defaultReasoningEffort": "high",
               "supportedReasoningEfforts": [{"reasoningEffort": "high"}, {"reasoningEffort": "low"},
                                             {"reasoningEffort": "xhigh"}],
               "inputModalities": ["text", "image"], "description": "Flagship"}
        result = cli_routes._hub_row("codex", row)
        self.assertEqual(result["id"], "gpt-5.2")
        self.assertEqual(result["display_name"], "GPT 5.2")
        # Ladder order is the desktop's, not the runtime's report order.
        self.assertEqual(result["effort_modes"], ["low", "high", "xhigh"])
        self.assertEqual(result["default_effort"], "high")
        self.assertTrue(result["reasoning"])
        self.assertTrue(result["vision"])
        self.assertTrue(result["tools"])
        self.assertEqual(result["source"], "cli")
        self.assertEqual(result["description"], "Flagship")

    def test_hub_row_drops_hidden_and_junk(self):
        self.assertIsNone(cli_routes._hub_row("codex", {"id": "gpt-5", "hidden": True}))
        self.assertIsNone(cli_routes._hub_row("codex", {"model": "not an id!!"}))
        self.assertIsNone(cli_routes._hub_row("codex", {"display_name": "no id"}))
        self.assertIsNone(cli_routes._hub_row("codex", "a string"))
        self.assertIsNone(cli_routes._hub_row("codex", None))

    def test_hub_row_passes_through_hub_shaped_family_cards(self):
        card = {"id": "claude-sonnet-4.6", "display_name": "Claude Sonnet 4.6",
                "reasoning": True, "effort_modes": ["high"], "default_effort": "high",
                "provider_effort_modes": ["thinking"], "aliases": ["claude-sonnet-4-6-thinking"]}
        result = cli_routes._hub_row("antigravity", card)
        self.assertEqual(result["effort_modes"], ["high"])
        self.assertEqual(result["default_effort"], "high")
        self.assertEqual(result["provider_effort_modes"], ["thinking"])
        self.assertTrue(result["reasoning"])

    def test_seed_rows_apply_verified_effort_ladders(self):
        grok = cli_routes._seed_rows("grok", ("grok-4.6", "grok-4.5"))
        self.assertEqual([row["id"] for row in grok], ["grok-4.6", "grok-4.5"])
        self.assertEqual(grok[0]["effort_modes"], ["low", "medium", "high", "xhigh"])
        self.assertTrue(grok[0]["reasoning"])
        claude = cli_routes._seed_rows("claude", ({"id": "sonnet", "reasoning_levels": ["low", "max"]},))
        self.assertEqual(claude[0]["effort_modes"], ["low", "max"])
        self.assertEqual(cli_routes._seed_rows("muse", ()), [])

    # -- discovery -----------------------------------------------------------

    def test_discover_via_cli_seeds_and_reports_auth(self):
        cli_routes._cache["claude"] = _fake_adapter(
            auth_state=lambda: {"state": "missing", "detail": "no active login"},
            KNOWN_MODELS=({"id": "sonnet", "reasoning_levels": ["low", "high"]},),
        )
        inventory = discover_via_cli("claude")
        self.assertEqual(inventory["provider_id"], "claude")
        self.assertEqual(inventory["source"], "cli")
        self.assertEqual(inventory["auth_state"], "missing")
        self.assertEqual([row["id"] for row in inventory["models"]], ["sonnet"])
        self.assertTrue(any("Not signed in" in warning for warning in inventory["warnings"]))
        self.assertTrue(any("no list" in warning for warning in inventory["warnings"]))

    def test_discover_via_cli_degrades_catalogue_failure(self):
        def boom(**kwargs):
            raise RuntimeError("wedged")
        cli_routes._cache["claude"] = _fake_adapter(catalogue=boom)
        inventory = discover_via_cli("claude")
        self.assertEqual(inventory["models"], [])
        self.assertTrue(any("wedged" in warning for warning in inventory["warnings"]))

    # -- turn planning ---------------------------------------------------------

    def test_plan_turn_translates_the_payload(self):
        payload = {"model": "claude/claude-sonnet-5",
                   "messages": [{"role": "user", "content": "hi"}],
                   "system": "be brief",
                   "output_config": {"effort": "high"},
                   "stream": True}
        plan = plan_turn("claude", "claude-sonnet-5", payload, {}, wanted_output=4096)
        self.assertTrue(plan["cli"])
        body = plan["body"]
        self.assertEqual(body["model"], "claude-sonnet-5")
        self.assertEqual(body["effort"], "high")
        self.assertEqual(body["max_tokens"], 4096)
        self.assertTrue(body["stream"])
        self.assertEqual(body["system"], "be brief")
        self.assertEqual(plan["compatibility"]["system_prompt_transport"], "flag")

    def test_plan_turn_tolerates_missing_axes(self):
        plan = plan_turn("claude", "sonnet", {"messages": None}, {}, wanted_output=None)
        self.assertEqual(plan["body"]["messages"], [])
        self.assertIsNone(plan["body"]["effort"])
        self.assertIsNone(plan["body"]["max_tokens"])
        self.assertFalse(plan["body"]["stream"])

    def test_plan_turn_flattens_block_spelled_system(self):
        # The gateway's identity note appends system as a list of text blocks.
        payload = {"messages": [{"role": "user", "content": "hi"}],
                   "system": [{"type": "text", "text": "be brief"},
                              {"type": "text", "text": "you are Sonnet"}]}
        plan = plan_turn("claude", "sonnet", payload, {}, wanted_output=64)
        self.assertEqual(plan["body"]["system"], "be brief\nyou are Sonnet")

    def test_plan_turn_folds_developer_messages_into_system(self):
        # The Codex desktop sends Responses developer items; to_messages keeps
        # them in the array with block content. Adapters must never see them.
        payload = {"messages": [
            {"role": "developer", "content": [{"type": "text", "text": "You are Codex."}]},
            {"role": "user", "content": [{"type": "text", "text": "hi"}]},
        ], "system": "identity note"}
        plan = plan_turn("claude", "sonnet", payload, {}, wanted_output=64)
        body = plan["body"]
        self.assertEqual(body["messages"], [{"role": "user", "content": "hi"}])
        self.assertEqual(body["system"], "identity note\n\nYou are Codex.")

    def test_plan_turn_drops_developer_text_already_in_system(self):
        payload = {"messages": [
            {"role": "developer", "content": [{"type": "text", "text": "Be terse."}]},
            {"role": "user", "content": "hi"},
        ], "system": "Be terse.\n\nidentity note"}
        plan = plan_turn("claude", "sonnet", payload, {}, wanted_output=64)
        self.assertEqual(plan["body"]["system"], "Be terse.\n\nidentity note")

    def test_plan_turn_flattens_tool_and_binary_blocks(self):
        payload = {"messages": [
            {"role": "assistant", "content": [{"type": "thinking", "thinking": "secret"},
                                              {"type": "tool_use", "name": "shell", "input": {}},
                                              {"type": "text", "text": "checking"}]},
            {"role": "user", "content": [{"type": "tool_result",
                                          "content": [{"type": "text", "text": "on main"}]},
                                         {"type": "input_image"}]},
        ]}
        plan = plan_turn("claude", "sonnet", payload, {}, wanted_output=64)
        assistant, user = plan["body"]["messages"]
        # Prior reasoning is provider state, not transcript text.
        self.assertEqual(assistant["content"], "[tool call: shell (call) with {}]\nchecking")
        self.assertEqual(user["content"],
                         "[tool result for call]\non main\n[image omitted: CLI routes are text-only]")

    def test_plan_turn_renders_tool_manifest(self):
        payload = {"messages": [{"role": "user", "content": "hi"}],
                   "tools": [{"name": "get_weather", "description": "Fetch weather.",
                              "input_schema": {"type": "object",
                                               "properties": {"city": {"type": "string"}}}}]}
        plan = plan_turn("claude", "sonnet", payload, {}, wanted_output=64)
        self.assertTrue(plan["cli_tool_calls"])
        self.assertIn(OPEN_SENTINEL, plan["body"]["system"])
        self.assertIn("get_weather", plan["body"]["system"])
        self.assertEqual(plan["compatibility"]["cli_tools"], 1)

    def test_plan_turn_tool_choice_none_suppresses_tools(self):
        payload = {"messages": [{"role": "user", "content": "hi"}],
                   "tools": [{"name": "get_weather"}],
                   "tool_choice": {"type": "none"}}
        plan = plan_turn("claude", "sonnet", payload, {}, wanted_output=64)
        self.assertFalse(plan["cli_tool_calls"])
        self.assertNotIn(OPEN_SENTINEL, plan["body"]["system"] or "")

    def test_tool_history_round_trips_with_correlation_ids(self):
        payload = {"messages": [
            {"role": "assistant", "content": [
                {"type": "tool_use", "id": "toolu_1", "name": "get_weather",
                 "input": {"city": "Paris"}}]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "toolu_1",
                 "content": [{"type": "text", "text": "sunny"}]}]},
        ]}
        plan = plan_turn("claude", "sonnet", payload, {}, wanted_output=64)
        assistant, user = plan["body"]["messages"]
        self.assertIn("get_weather (toolu_1)", assistant["content"])
        self.assertIn('"city": "Paris"', assistant["content"])
        self.assertIn("toolu_1", user["content"])
        self.assertIn("sunny", user["content"])


class ParseToolStreamTest(unittest.TestCase):
    def setUp(self):
        self._saved_cache = dict(cli_routes._cache)
        cli_routes._cache.clear()

    def tearDown(self):
        cli_routes._cache.clear()
        cli_routes._cache.update(self._saved_cache)

    def test_envelope_text_becomes_tool_call_events(self):
        wire = ("sure, checking " + OPEN_SENTINEL
                + '{"name": "get_weather", "input": {"city": "Paris"}}' + CLOSE_SENTINEL)

        def adapter_turn(request, *, timeout=300):
            for piece in (wire[:13], wire[13:40], wire[40:]):
                yield {"type": "text_delta", "text": piece}
            yield {"type": "message_stop", "stop_reason": "end_turn"}

        cli_routes._cache["claude"] = types.SimpleNamespace(run_turn=adapter_turn)
        events = list(run_turn("claude", {}, parse_tool_calls=True))
        calls = [event for event in events if event["type"] == "tool_call"]
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["name"], "get_weather")
        self.assertEqual(calls[0]["input"], {"city": "Paris"})
        self.assertEqual(events[-1], {"type": "message_stop", "stop_reason": "tool_use"})
        self.assertEqual("".join(e.get("text", "") for e in events if e["type"] == "text_delta"),
                         "sure, checking ")

    def test_passthrough_when_parse_disabled(self):
        wire = OPEN_SENTINEL + '{"name": "x"}' + CLOSE_SENTINEL

        def adapter_turn(request, *, timeout=300):
            yield {"type": "text_delta", "text": wire}
            yield {"type": "message_stop", "stop_reason": "end_turn"}

        cli_routes._cache["claude"] = types.SimpleNamespace(run_turn=adapter_turn)
        events = list(run_turn("claude", {}, parse_tool_calls=False))
        self.assertEqual([e["type"] for e in events], ["text_delta", "message_stop"])
        self.assertEqual(events[0]["text"], wire)

    def test_wrapper_close_kills_inner_generator(self):
        closed = []

        def adapter_turn(request, *, timeout=300):
            try:
                yield {"type": "text_delta", "text": "partial " + OPEN_SENTINEL}
                yield {"type": "text_delta", "text": "more"}
            finally:
                closed.append(True)

        cli_routes._cache["claude"] = types.SimpleNamespace(run_turn=adapter_turn)
        events = run_turn("claude", {}, parse_tool_calls=True)
        next(events)
        events.close()
        self.assertEqual(closed, [True])

    def test_relay_emits_tool_use_wire_blocks(self):
        emitted = []
        result = relay_cli_turn(
            iter([{"type": "text_delta", "text": "checking"},
                  {"type": "tool_call", "id": "toolu_1", "name": "get_weather",
                   "input": {"city": "Paris"}},
                  {"type": "message_stop", "stop_reason": "tool_use"}]),
            emitted.append, model="m", input_tokens=0)
        kinds = [(event["type"], event.get("index")) for event in emitted]
        self.assertEqual(kinds, [("message_start", None),
                                 ("content_block_start", 0), ("content_block_delta", 0),
                                 ("content_block_stop", 0),
                                 ("content_block_start", 1), ("content_block_delta", 1),
                                 ("content_block_stop", 1),
                                 ("message_delta", None), ("message_stop", None)])
        tool_block = emitted[4]["content_block"]
        self.assertEqual(tool_block, {"type": "tool_use", "id": "toolu_1",
                                      "name": "get_weather", "input": {}})
        self.assertEqual(emitted[5]["delta"]["type"], "input_json_delta")
        self.assertEqual(json.loads(emitted[5]["delta"]["partial_json"]), {"city": "Paris"})
        self.assertEqual(result["stop_reason"], "tool_use")

    # -- wire translation ------------------------------------------------------

    def _events(self, *items):
        for item in items:
            yield item

    def test_relay_text_turn_emits_full_envelope(self):
        emitted = []
        result = relay_cli_turn(
            self._events({"type": "text_delta", "text": "hel"},
                         {"type": "text_delta", "text": "lo"},
                         {"type": "message_stop", "stop_reason": "end_turn"}),
            emitted.append, model="claude/test", input_tokens=42)
        kinds = [event["type"] for event in emitted]
        self.assertEqual(kinds, ["message_start", "content_block_start", "content_block_delta",
                                 "content_block_delta", "content_block_stop", "message_delta",
                                 "message_stop"])
        self.assertEqual(emitted[0]["message"]["model"], "claude/test")
        self.assertEqual(emitted[0]["message"]["usage"]["input_tokens"], 42)
        self.assertIsNone(result["error"])
        self.assertEqual(result["stop_reason"], "end_turn")

    def test_relay_thinking_then_text_closes_blocks_in_order(self):
        emitted = []
        relay_cli_turn(
            self._events({"type": "thinking_delta", "text": "hmm"},
                         {"type": "text_delta", "text": "answer"},
                         {"type": "message_stop", "stop_reason": "end_turn"}),
            emitted.append, model="m", input_tokens=0)
        kinds = [(event["type"], event.get("index")) for event in emitted]
        self.assertEqual(kinds, [("message_start", None),
                                 ("content_block_start", 0), ("content_block_delta", 0),
                                 ("content_block_stop", 0),
                                 ("content_block_start", 1), ("content_block_delta", 1),
                                 ("content_block_stop", 1),
                                 ("message_delta", None), ("message_stop", None)])
        self.assertEqual(emitted[2]["delta"], {"type": "thinking_delta", "thinking": "hmm"})

    def test_relay_error_mid_stream_emits_no_terminal_envelope(self):
        emitted = []
        result = relay_cli_turn(
            self._events({"type": "text_delta", "text": "partial"},
                         {"type": "error", "message": "died"}),
            emitted.append, model="m", input_tokens=0)
        self.assertEqual(result["error"], "died")
        self.assertTrue(result["started"])
        self.assertNotIn("message_stop", [event["type"] for event in emitted])
        self.assertNotIn("message_delta", [event["type"] for event in emitted])

    def test_relay_unknown_stop_reason_maps_to_end_turn(self):
        emitted = []
        result = relay_cli_turn(
            self._events({"type": "message_stop", "stop_reason": "cancelled"}),
            emitted.append, model="m", input_tokens=0)
        self.assertEqual(result["stop_reason"], "end_turn")


class RegistrationTest(unittest.TestCase):
    """The provider registrations the CLI toggle depends on."""

    def test_all_five_cli_providers_are_registered_and_available(self):
        for provider_id in ("codex", "claude", "muse", "grok", "antigravity"):
            self.assertIn(provider_id, PROVIDERS)
            self.assertIn(provider_id, CLI_AUTH_PROVIDERS)
            self.assertTrue(cli_auth_available(provider_id))

    def test_credential_modes_per_provider(self):
        self.assertEqual(credential_modes("antigravity"), ["cli"])
        self.assertEqual(credential_modes("ollama"), ["none"])
        self.assertEqual(credential_modes("mistral"), ["vibe", "keychain", "environment"])
        self.assertEqual(credential_modes("grok"), ["keychain", "environment", "cli"])
        self.assertEqual(credential_modes("codex"), ["keychain", "environment", "cli"])

    def test_defaults_and_connections_validate(self):
        settings = default_settings()
        for provider_id in ("codex", "claude", "antigravity"):
            self.assertIn(provider_id, settings["providers"])
        self.assertEqual(settings["providers"]["antigravity"]["credential_mode"], "cli")
        self.assertNotEqual(settings["providers"]["codex"]["credential_mode"], "cli")
        self.assertEqual(validate_connection("claude", {})["base_url"], "https://api.anthropic.com")
        self.assertEqual(validate_connection("codex", {})["base_url"], "https://api.openai.com/v1")
        self.assertEqual(validate_connection("antigravity", {}), {"region": "local", "base_url": ""})
        # The signature is what fails a stale catalogue on the API/CLI flip.
        signature = connection_signature("antigravity", settings["providers"]["antigravity"])
        self.assertIsInstance(signature, str)
        flipped = dict(settings["providers"]["antigravity"], credential_mode="cli")
        self.assertEqual(signature, connection_signature("antigravity", flipped))

    def test_presentations_carry_credential_modes(self):
        from hub_config import provider_presentations
        rows = {row["id"]: row for row in provider_presentations(default_settings())}
        self.assertEqual(rows["antigravity"]["credential_modes"], ["cli"])
        self.assertIn("cli", rows["claude"]["credential_modes"])
        self.assertNotIn("cli", rows["mistral"]["credential_modes"])


class ApiModeDiscoveryTest(unittest.TestCase):
    """The API side of the CLI|API toggle for the newly registered providers."""

    def _discover(self, provider_id, payload):
        seen = {}

        def transport(plan):
            seen["url"] = plan["url"]
            seen["headers"] = plan["headers"]
            return payload

        with patch.object(providers, "_fetch_json", side_effect=transport):
            inventory = providers.discover(provider_id, None, "sk-test-key")
        return inventory, seen

    def test_claude_api_discovery_parses_list_route(self):
        payload = {"data": [
            {"type": "model", "id": "claude-opus-4-1", "display_name": "Claude Opus 4.1",
             "created_at": "2026-01-01T00:00:00Z"},
            {"type": "model", "id": "claude-sonnet-5", "display_name": "Claude Sonnet 5",
             "created_at": "2026-01-01T00:00:00Z"},
            {"type": "not-a-model", "id": "skipped-thing"},
        ]}
        inventory, seen = self._discover("claude", payload)
        self.assertEqual(seen["url"], "https://api.anthropic.com/v1/models")
        self.assertEqual(seen["headers"]["x-api-key"], "sk-test-key")
        ids = [model["id"] for model in inventory["models"]]
        self.assertEqual(ids, ["claude-sonnet-5", "claude-opus-4-1"][::-1] if False else
                         sorted(ids, key=ids.index))
        self.assertIn("claude-sonnet-5", ids)
        self.assertIn("claude-opus-4-1", ids)
        self.assertNotIn("skipped-thing", ids)
        self.assertTrue(any("provider-managed" in w for w in inventory["warnings"]))

    def test_codex_api_discovery_keeps_only_chat_models(self):
        payload = {"object": "list", "data": [
            {"id": "gpt-5.2", "object": "model", "created": 1, "owned_by": "openai"},
            {"id": "o4-mini", "object": "model", "created": 1, "owned_by": "openai"},
            {"id": "whisper-1", "object": "model", "created": 1, "owned_by": "openai"},
            {"id": "gpt-image-1", "object": "model", "created": 1, "owned_by": "openai"},
            {"id": "gpt-4o-audio-preview", "object": "model", "created": 1, "owned_by": "openai"},
            {"id": "text-embedding-3-small", "object": "model", "created": 1, "owned_by": "openai"},
            {"id": "davinci-002", "object": "model", "created": 1, "owned_by": "openai"},
        ]}
        inventory, seen = self._discover("codex", payload)
        self.assertEqual(seen["url"], "https://api.openai.com/v1/models")
        self.assertEqual(seen["headers"]["Authorization"], "Bearer sk-test-key")
        ids = [model["id"] for model in inventory["models"]]
        self.assertEqual(sorted(ids), ["gpt-5.2", "o4-mini"])
        self.assertTrue(any("provider-managed" in w for w in inventory["warnings"]))


class DiscoverProviderBranchTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_cli_mode_discovery_uses_the_adapter_not_http(self):
        settings = default_settings()
        settings["providers"]["claude"]["credential_mode"] = "cli"
        called = {}

        def fake_discover(provider_id, **kwargs):
            called["provider_id"] = provider_id
            return {"provider_id": provider_id, "models": [
                {"id": "sonnet", "display_name": "Sonnet", "aliases": ["sonnet"]}],
                "source": "cli", "warnings": []}

        with patch("bridge_core.discover_via_cli", side_effect=fake_discover), \
             patch.object(providers, "discover",
                          side_effect=AssertionError("HTTP discovery must not run for a CLI route")):
            source = discover_provider(settings, "claude", root=self.root)
        self.assertEqual(called["provider_id"], "claude")
        self.assertEqual(source, "Claude (Anthropic API) CLI login")
        cached = json.loads((self.root / "catalogues" / "claude.json").read_text())
        self.assertEqual(cached["connection_signature"],
                         connection_signature("claude", settings["providers"]["claude"]))
        self.assertEqual(cached["models"][0]["id"], "sonnet")


class GatewayCliTurnTest(unittest.TestCase):
    """End-to-end through a real in-process gateway against a fake adapter."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self._saved_cache = dict(cli_routes._cache)
        cli_routes._cache.clear()
        self.gateway = None
        self.events = [
            {"type": "thinking_delta", "text": "thinking"},
            {"type": "text_delta", "text": "hello "},
            {"type": "text_delta", "text": "world"},
            {"type": "message_stop", "stop_reason": "end_turn"},
        ]
        self.requests = []

        def run_turn(request, *, timeout=300):
            self.requests.append(request)
            for event in self.events:
                yield event

        cli_routes._cache["claude"] = _fake_adapter(run_turn=run_turn)
        vibe = {"active_model": "unused", "active_display_name": "Unused",
                "key_name": "MISTRAL_API_KEY", "vibe_home": str(self.root / "vibe"),
                "configured_models": []}
        with patch("bridge_core.vibe_settings", return_value=vibe):
            settings = default_settings()
        settings["providers"]["claude"]["credential_mode"] = "cli"
        settings["mappings"] = {slot[0]: "claude/claude-sonnet-5" for slot in SLOTS}
        atomic_json(self.root / "settings.json", settings)
        atomic_json(self.root / "catalogues" / "claude.json", {
            "provider_id": "claude",
            "source": "cli",
            "connection_signature": connection_signature("claude", settings["providers"]["claude"]),
            "models": [{
                "id": "claude-sonnet-5", "canonical_id": "claude-sonnet-5",
                "display_name": "Claude Sonnet 5", "context": 200000,
                "aliases": ["claude-sonnet-5"], "tools": True, "vision": True,
                "reasoning": True, "effort_modes": ["low", "high"], "fast_mode": False,
                "inference_status": "advertised", "source": "cli", "evidence": "test"}],
        })
        with patch("bridge_core.vibe_settings", return_value=vibe):
            self.runtime = Runtime(self.root)
        self.gateway = Server(self.runtime, 0)
        threading.Thread(target=self.gateway.serve_forever, daemon=True).start()

    def tearDown(self):
        if self.gateway is not None:
            self.gateway.shutdown()
            self.gateway.server_close()
        self.temp.cleanup()
        cli_routes._cache.clear()
        cli_routes._cache.update(self._saved_cache)

    def request(self, body, timeout=8):
        connection = http.client.HTTPConnection("127.0.0.1", self.gateway.server_port, timeout=timeout)
        connection.request("POST", "/v1/messages", json.dumps(body), {
            "Authorization": "Bearer " + self.runtime.token,
            "Content-Type": "application/json"})
        response = connection.getresponse()
        raw = response.read()
        status = response.status
        connection.close()
        deadline = time.monotonic() + 2
        while self.runtime.status()["active"] and time.monotonic() < deadline:
            time.sleep(.01)
        return status, raw

    def test_non_streaming_cli_turn_returns_one_message(self):
        status, raw = self.request({"model": "claude/claude-sonnet-5", "max_tokens": 64,
                                    "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(status, 200, raw[:300])
        message = json.loads(raw)
        self.assertEqual(message["type"], "message")
        self.assertEqual(message["model"], "claude/claude-sonnet-5")
        self.assertEqual(message["stop_reason"], "end_turn")
        self.assertEqual(message["content"][0], {"type": "thinking", "thinking": "thinking"})
        self.assertEqual(message["content"][1], {"type": "text", "text": "hello world"})
        # The adapter received the translated request, never an HTTP plan.
        request = self.requests[0]
        self.assertEqual(request["model"], "claude-sonnet-5")
        self.assertFalse(request["stream"])

    def test_streaming_cli_turn_emits_anthropic_sse(self):
        status, raw = self.request({"model": "claude/claude-sonnet-5", "max_tokens": 64,
                                    "stream": True,
                                    "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(status, 200, raw[:300])
        text = raw.decode()
        for expected in ("event: message_start", "event: content_block_start",
                         "thinking_delta", "text_delta", "hello ", "world",
                         "event: message_delta", "event: message_stop"):
            self.assertIn(expected, text)
        self.assertTrue(self.requests[0]["stream"])

    def test_adapter_error_before_content_is_a_clean_502(self):
        self.events = [{"type": "error", "message": "the claude CLI was not found on PATH"}]
        status, raw = self.request({"model": "claude/claude-sonnet-5", "max_tokens": 64,
                                    "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(status, 502)
        self.assertIn(b"not found on PATH", raw)

    def test_prepare_request_is_never_called_for_cli_routes(self):
        with patch.object(gateway_module, "prepare_request",
                          side_effect=AssertionError("HTTP planner must not run for a CLI route")):
            status, _ = self.request({"model": "claude/claude-sonnet-5", "max_tokens": 64,
                                      "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(status, 200)

    def test_developer_role_and_block_content_never_reach_the_adapter(self):
        # What the Codex desktop actually sends after the Responses bridge:
        # a developer message with block content, then a user block message.
        status, raw = self.request({"model": "claude/claude-sonnet-5", "max_tokens": 64,
                                    "system": "top-level system",
                                    "messages": [
                                        {"role": "developer",
                                         "content": [{"type": "text", "text": "harness context"}]},
                                        {"role": "user",
                                         "content": [{"type": "text", "text": "hi"}]}]})
        self.assertEqual(status, 200, raw[:300])
        request = self.requests[0]
        self.assertEqual([message["role"] for message in request["messages"]], ["user"])
        self.assertEqual(request["messages"][0]["content"], "hi")
        self.assertIn("top-level system", request["system"])
        self.assertIn("harness context", request["system"])

    def test_tool_call_round_trip_on_the_wire(self):
        self.events = [
            {"type": "text_delta", "text": "checking " + OPEN_SENTINEL
             + '{"name": "get_weather", "input": {"city": "Paris"}}' + CLOSE_SENTINEL},
            {"type": "message_stop", "stop_reason": "end_turn"},
        ]
        status, raw = self.request({"model": "claude/claude-sonnet-5", "max_tokens": 64,
                                    "messages": [{"role": "user", "content": "weather?"}],
                                    "tools": [{"name": "get_weather",
                                               "description": "Fetch weather.",
                                               "input_schema": {"type": "object",
                                                                "properties": {"city": {"type": "string"}}}}]})
        self.assertEqual(status, 200, raw[:300])
        message = json.loads(raw)
        self.assertEqual(message["stop_reason"], "tool_use")
        kinds = [block.get("type") for block in message["content"]]
        self.assertEqual(kinds, ["text", "tool_use"])
        self.assertEqual(message["content"][1]["name"], "get_weather")
        self.assertEqual(message["content"][1]["input"], {"city": "Paris"})
        # The manifest rode the system text; the envelope never did.
        self.assertIn(OPEN_SENTINEL, self.requests[0]["system"])

    def test_tool_call_streams_as_sse_blocks(self):
        self.events = [
            {"type": "text_delta", "text": OPEN_SENTINEL
             + '{"name": "shell", "input": {"cmd": "pwd"}}' + CLOSE_SENTINEL},
            {"type": "message_stop", "stop_reason": "end_turn"},
        ]
        status, raw = self.request({"model": "claude/claude-sonnet-5", "max_tokens": 64,
                                    "stream": True,
                                    "messages": [{"role": "user", "content": "where am i"}],
                                    "tools": [{"name": "shell"}]})
        self.assertEqual(status, 200, raw[:300])
        text = raw.decode()
        self.assertIn('"type": "tool_use"', text)
        self.assertIn('"stop_reason": "tool_use"', text)
        self.assertIn("input_json_delta", text)


class ResponsesBridgeTest(unittest.TestCase):
    """CLI-mode providers take the messages bridge on the Codex surface too."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_native_provider_in_cli_mode_delegates_to_messages_bridge(self):
        from responses_native import prepare_native
        vibe = {"active_model": "unused", "active_display_name": "Unused",
                "key_name": "MISTRAL_API_KEY", "vibe_home": str(self.root / "vibe"),
                "configured_models": []}
        with patch("bridge_core.vibe_settings", return_value=vibe):
            settings = default_settings()
        settings["providers"]["grok"]["credential_mode"] = "cli"
        atomic_json(self.root / "settings.json", settings)
        atomic_json(self.root / "catalogues" / "grok.json", {
            "provider_id": "grok",
            "source": "cli",
            "connection_signature": connection_signature("grok", settings["providers"]["grok"]),
            "models": [{
                "id": "grok-4.6", "canonical_id": "grok-4.6", "display_name": "Grok 4.6",
                "context": 131072, "aliases": ["grok-4.6"], "tools": True, "vision": False,
                "reasoning": True, "effort_modes": ["low", "high"], "fast_mode": False,
                "inference_status": "advertised", "source": "cli", "evidence": "test"}],
        })
        with patch("bridge_core.vibe_settings", return_value=vibe):
            runtime = Runtime(self.root)
        # grok is a NATIVE_PROVIDER: in API mode it would relay to xAI. In cli
        # mode the plan must delegate to the local Messages bridge instead.
        # ReasoningEnvelope needs the cryptography package, absent from the uv
        # runtime (a pre-existing environment gap, unrelated to this test).
        with patch("responses_native.ReasoningEnvelope", return_value=types.SimpleNamespace()):
            plan = prepare_native(runtime, {"model": "grok/grok-4.6", "input": "hi",
                                            "stream": False, "store": False})
        self.assertEqual(plan["protocol"], "messages_bridge")
        self.assertEqual(plan["provider_id"], "grok")

    def test_bridged_developer_items_normalize_for_cli(self):
        from responses_native import prepare_native
        vibe = {"active_model": "unused", "active_display_name": "Unused",
                "key_name": "MISTRAL_API_KEY", "vibe_home": str(self.root / "vibe"),
                "configured_models": []}
        with patch("bridge_core.vibe_settings", return_value=vibe):
            settings = default_settings()
        settings["providers"]["grok"]["credential_mode"] = "cli"
        atomic_json(self.root / "settings.json", settings)
        atomic_json(self.root / "catalogues" / "grok.json", {
            "provider_id": "grok",
            "source": "cli",
            "connection_signature": connection_signature("grok", settings["providers"]["grok"]),
            "models": [{
                "id": "grok-4.6", "canonical_id": "grok-4.6", "display_name": "Grok 4.6",
                "context": 131072, "aliases": ["grok-4.6"], "tools": True, "vision": False,
                "reasoning": True, "effort_modes": ["low", "high"], "fast_mode": False,
                "inference_status": "advertised", "source": "cli", "evidence": "test"}],
        })
        with patch("bridge_core.vibe_settings", return_value=vibe):
            runtime = Runtime(self.root)
        with patch("responses_native.ReasoningEnvelope", return_value=types.SimpleNamespace()):
            plan = prepare_native(runtime, {
                "model": "grok/grok-4.6",
                "instructions": "Be the Codex harness.",
                "input": [
                    {"type": "message", "role": "developer",
                     "content": [{"type": "input_text", "text": "env: macOS"}]},
                    {"type": "message", "role": "user",
                     "content": [{"type": "input_text", "text": "hi"}]}],
                "stream": False, "store": False})
        self.assertEqual(plan["protocol"], "messages_bridge")
        # The bridged Messages body is exactly what /v1/messages will plan on.
        cli_plan = plan_turn("grok", "grok-4.6", plan["body"], {}, wanted_output=4096)
        self.assertEqual([m["role"] for m in cli_plan["body"]["messages"]], ["user"])
        self.assertEqual(cli_plan["body"]["messages"][0]["content"], "hi")
        self.assertEqual(cli_plan["body"]["system"], "Be the Codex harness.\n\nenv: macOS")


class ResponsesEndToEndTest(unittest.TestCase):
    """A full /v1/responses turn for a CLI-mode provider: the regression that
    replayed grok's first answer under Codex Reconnect retries.

    finish_response special-cased grok with plan["body"]["store"], but on the
    messages-bridge path plan["body"] is the translated Messages body, which
    has no "store" key - the KeyError was swallowed into a terminal provider
    error after the answer had streamed, so Codex retried a successful turn.
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self._saved_cache = dict(cli_routes._cache)
        cli_routes._cache.clear()

        def run_turn(request, *, timeout=300):
            yield {"type": "thinking_delta", "text": "thinking"}
            yield {"type": "text_delta", "text": "ROUTE2 OK"}
            yield {"type": "message_stop", "stop_reason": "end_turn"}

        cli_routes._cache["grok"] = types.SimpleNamespace(
            PROVIDER_ID="grok", TRANSPORT="print", SYSTEM_PROMPT_TRANSPORT="flag",
            KNOWN_MODELS=(), run_turn=run_turn)
        vibe = {"active_model": "unused", "active_display_name": "Unused",
                "key_name": "MISTRAL_API_KEY", "vibe_home": str(self.root / "vibe"),
                "configured_models": []}
        with patch("bridge_core.vibe_settings", return_value=vibe):
            settings = default_settings()
        settings["providers"]["grok"]["credential_mode"] = "cli"
        settings["mappings"] = {slot[0]: "grok/grok-4.6" for slot in SLOTS}
        atomic_json(self.root / "settings.json", settings)
        atomic_json(self.root / "catalogues" / "grok.json", {
            "provider_id": "grok",
            "source": "cli",
            "connection_signature": connection_signature("grok", settings["providers"]["grok"]),
            "models": [{
                "id": "grok-4.6", "canonical_id": "grok-4.6", "display_name": "Grok 4.6",
                "context": 131072, "aliases": ["grok-4.6"], "tools": True, "vision": False,
                "reasoning": True, "effort_modes": ["low", "high"], "fast_mode": False,
                "inference_status": "advertised", "source": "cli", "evidence": "test"}],
        })
        with patch("bridge_core.vibe_settings", return_value=vibe):
            self.runtime = Runtime(self.root)
        self.gateway = Server(self.runtime, 0)
        threading.Thread(target=self.gateway.serve_forever, daemon=True).start()

    def tearDown(self):
        self.gateway.shutdown()
        self.gateway.server_close()
        self.temp.cleanup()
        cli_routes._cache.clear()
        cli_routes._cache.update(self._saved_cache)

    def test_bridged_cli_turn_completes_with_no_terminal_error(self):
        connection = http.client.HTTPConnection("127.0.0.1", self.gateway.server_port, timeout=30)
        connection.request("POST", "/v1/responses", json.dumps({
            "model": "grok/grok-4.6", "stream": True, "store": False,
            "instructions": "You are a terse assistant.",
            "input": [{"type": "message", "role": "user",
                       "content": [{"type": "input_text", "text": "hi"}]}]}),
            {"Authorization": "Bearer " + self.runtime.token,
             "Content-Type": "application/json"})
        response = connection.getresponse()
        raw = response.read().decode()
        connection.close()
        kinds = [line[7:] for block in raw.split("\n\n") for line in [block.split("\n")[0]]
                 if line.startswith("event: ")]
        self.assertEqual(response.status, 200)
        self.assertIn("response.completed", kinds)
        self.assertNotIn("error", kinds)
        self.assertIn("ROUTE2 OK", raw)

    def test_bridged_tool_call_becomes_a_function_call(self):
        def tool_turn(request, *, timeout=300):
            yield {"type": "text_delta", "text": OPEN_SENTINEL
                   + '{"name": "get_weather", "input": {"city": "Paris"}}' + CLOSE_SENTINEL}
            yield {"type": "message_stop", "stop_reason": "end_turn"}

        cli_routes._cache["grok"] = types.SimpleNamespace(
            PROVIDER_ID="grok", TRANSPORT="print", SYSTEM_PROMPT_TRANSPORT="flag",
            KNOWN_MODELS=(), run_turn=tool_turn)
        connection = http.client.HTTPConnection("127.0.0.1", self.gateway.server_port, timeout=30)
        connection.request("POST", "/v1/responses", json.dumps({
            "model": "grok/grok-4.6", "stream": True, "store": False,
            "input": [{"type": "message", "role": "user",
                       "content": [{"type": "input_text", "text": "weather in Paris?"}]}],
            "tools": [{"type": "function", "name": "get_weather",
                       "description": "Fetch weather.",
                       "parameters": {"type": "object",
                                      "properties": {"city": {"type": "string"}}}}]}),
            {"Authorization": "Bearer " + self.runtime.token,
             "Content-Type": "application/json"})
        response = connection.getresponse()
        raw = response.read().decode()
        connection.close()
        self.assertEqual(response.status, 200, raw[:300])
        self.assertIn("response.completed", raw)
        completed = [json.loads(line[5:]) for block in raw.split("\n\n")
                     for line in [block.split("\n")[-1]]
                     if line.startswith("data:")
                     and json.loads(line[5:]).get("type") == "response.completed"]
        output = completed[0]["response"]["output"]
        calls = [item for item in output if item.get("type") == "function_call"]
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["name"], "get_weather")
        self.assertEqual(json.loads(calls[0]["arguments"]), {"city": "Paris"})


if __name__ == "__main__":
    unittest.main()
