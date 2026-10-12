"""Credential rotation and Anthropic request compatibility, using local mocks."""
import copy
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from bridge_core import BridgeError, atomic_json, default_settings, load_settings
from gateway import Runtime, save_key_metadata
from provider_discovery import discover
from provider_requests import prepare_request
import test_gateway_hub as fixtures
from test_gateway_hub import MockProvider


def prompt(**extra):
    return {"model": "claude-opus-5-5", "max_tokens": 1024,
            "messages": [{"role": "user", "content": "hello"}], **extra}


class ClaudeCompatibilityTests(unittest.TestCase):
    def test_model_api_effort_capabilities_win_over_documentation(self):
        card = {"id": "claude-opus-5-5", "max_input_tokens": 654321, "max_tokens": 4096,
                "capabilities": {"thinking": {"supported": True}, "image_input": {"supported": True},
                                 "effort": {"supported": True, "low": {"supported": True},
                                            "high": {"supported": True}, "max": {"supported": False}}}}
        result = discover("claude", {}, "test-key", transport=lambda _: {"data": [card]})["models"][0]
        self.assertEqual(result["effort_modes"], ["low", "high"])
        self.assertTrue(result["reasoning"])
        self.assertTrue(result["vision"])
        self.assertTrue(result["tools"])
        self.assertEqual(result["context"], 654321)
        self.assertEqual(result["max_output"], 4096)

    def test_legacy_model_cards_use_only_exact_documented_effort_ladders(self):
        ids = ["claude-opus-5-5", "claude-opus-4-6", "claude-opus-4-5", "claude-haiku-4-5", "claude-future"]
        rows = discover("claude", {}, "test-key", transport=lambda _: {"data": [{"id": x} for x in ids]})["models"]
        by_id = {row["id"]: row for row in rows}
        self.assertEqual(by_id[ids[0]]["effort_modes"], ["low", "medium", "high", "xhigh", "max"])
        self.assertEqual(by_id[ids[1]]["effort_modes"], ["low", "medium", "high", "max"])
        self.assertEqual(by_id[ids[2]]["effort_modes"], ["low", "medium", "high"])
        for identifier in ids[3:]: self.assertEqual(by_id[identifier]["effort_modes"], [])
        disabled = discover("claude", {}, "test-key", transport=lambda _: {"data": [{
            "id": ids[0], "capabilities": {"thinking": {"supported": False}, "effort": {"supported": False}}}]})["models"][0]
        self.assertEqual(disabled["effort_modes"], [])
        self.assertFalse(disabled["reasoning"])

    def test_adaptive_thinking_and_effort_are_forwarded_natively(self):
        for level in ["low", "medium", "high", "xhigh", "max"]:
            with self.subTest(level=level):
                original = prompt(thinking={"type": "adaptive"}, output_config={"effort": level})
                body = prepare_request("claude", {}, "test-key", original, "claude-opus-5-5", {
                    "reasoning": True, "effort_modes": ["low", "medium", "high", "xhigh", "max"]})["body"]
                self.assertEqual(body["thinking"], {"type": "adaptive"})
                self.assertEqual(body["output_config"]["effort"], level)
                self.assertEqual(original["thinking"], {"type": "adaptive"})
        body = prepare_request("claude", {}, "test-key", prompt(output_config={"effort": "ultra"}),
                               "claude-opus-4-5", {"effort_modes": ["low", "medium", "high"]})["body"]
        self.assertEqual(body["output_config"]["effort"], "high")

    def test_root_tool_constraints_are_preserved_without_root_combinators(self):
        for kind in ("oneOf", "anyOf", "allOf"):
            with self.subTest(kind=kind):
                constraints = [{"required": ["path"]}, {"required": ["url"]}]
                schema = {"type": "object", "properties": {
                    "path": {"type": "string"}, "url": {"type": "string"},
                    "oneOf": {"anyOf": [{"type": "string"}, {"type": "null"}]}},
                    kind: constraints, "not": {"required": ["blocked"]},
                    "$defs": {"node": {"type": "object"}}}
                original = prompt(tools=[{"name": "fetch", "input_schema": schema}])
                before = copy.deepcopy(original)
                plan = prepare_request("claude", {}, "test-key", original, "claude-opus-5-5", {})
                result = plan["body"]["tools"][0]["input_schema"]
                self.assertFalse(set(result) & {"oneOf", "anyOf", "allOf"})
                self.assertEqual(result["not"]["not"], {kind: constraints, "not": {"required": ["blocked"]}})
                self.assertEqual(result["properties"], schema["properties"])
                self.assertEqual(result["$defs"], schema["$defs"])
                self.assertEqual(original, before)
                # Another Anthropic-compatible provider keeps its original schema.
                other = prepare_request("muse", {}, "test-key", original, "muse-spark-1.3", {})
                self.assertEqual(other["body"]["tools"][0]["input_schema"], schema)

    def test_relocated_schema_references_keep_targets_and_literal_properties(self):
        schema = {"type": "object", "allOf": [{"required": ["path"]}],
                  "properties": {"path": {"type": "string"},
                                 "child": {"$ref": "#/allOf/0"},
                                 "$ref": {"const": "#/allOf/0"},
                                 "other": {"$id": "https://example.test/other", "$ref": "#/allOf/0"}},
                  "not": {"required": ["blocked"]},
                  "$defs": {"originalNot": {"$ref": "#/not"}}}
        plan = prepare_request("claude", {}, "test-key", prompt(tools=[{"name": "read", "input_schema": schema}]),
                               "claude-opus-5-5", {})
        adapted = plan["body"]["tools"][0]["input_schema"]
        self.assertEqual(adapted["properties"]["child"]["$ref"], "#/not/not/allOf/0")
        self.assertEqual(adapted["$defs"]["originalNot"]["$ref"], "#/not/not/not")
        self.assertEqual(adapted["properties"]["$ref"]["const"], "#/allOf/0")
        self.assertEqual(adapted["properties"]["other"]["$ref"], "#/allOf/0")


class CredentialSaveTests(unittest.TestCase):
    def test_metadata_save_preserves_routes_and_rejects_unrelated_edits(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = default_settings()
            settings["port"] = 12567
            atomic_json(root / "settings.json", settings)
            settings = load_settings(root)
            saved = save_key_metadata(root, "grok", {})
            self.assertEqual(saved["port"], 12567)
            self.assertEqual(saved["mappings"], settings["mappings"])
            self.assertEqual(saved["providers"]["muse"], settings["providers"]["muse"])
            self.assertEqual(saved["providers"]["grok"]["credential_revision"], 1)
            self.assertEqual(saved["providers"]["grok"]["credential_mode"], "keychain")
            for bad in ({"base_url": "https://other.example"}, {"secret": "must-not-be-saved"}):
                with self.assertRaises(BridgeError): save_key_metadata(root, "grok", bad)
            with self.assertRaises(BridgeError): save_key_metadata(root, "ollama", {})
            self.assertEqual(load_settings(root), saved)

    def test_revision_reload_keeps_models_and_sources_fixed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = default_settings()
            settings["providers"]["claude"]["credential_mode"] = "keychain"
            runtime = Runtime.__new__(Runtime)
            runtime.root = root; runtime.settings = copy.deepcopy(settings)
            runtime.settings["_model_specs"] = {"retained": {"context": 100000}}
            runtime.lock = threading.Lock(); runtime.key = None; runtime.credential_cache = {}
            atomic_json(root / "settings.json", settings)
            with patch("gateway.credentials", side_effect=lambda value, _: (
                    "key-" + str(value["providers"]["claude"]["credential_revision"]), "test")) as resolve:
                captured = runtime.provider_key("claude")
                self.assertEqual(runtime.provider_key("claude"), captured)
                changed = save_key_metadata(root, "claude", {})
                changed["mappings"] = {slot: "grok/other" for slot in changed["mappings"]}
                changed["providers"]["claude"]["credential_mode"] = "cli"
                atomic_json(root / "settings.json", changed)
                self.assertEqual(runtime.provider_key("claude"), "key-1")
                self.assertEqual(captured, "key-0")
                self.assertEqual(resolve.call_count, 2)
                live = runtime.credential_settings()
                self.assertEqual(live["mappings"], runtime.settings["mappings"])
                self.assertEqual(live["providers"]["claude"]["credential_mode"], "keychain")
                self.assertEqual(live["_model_specs"], runtime.settings["_model_specs"])

    def test_key_save_ui_does_not_stop_gateway_or_require_idle_surfaces(self):
        source = Path(__file__).with_name("MistralBridge.swift").read_text()
        save = source[source.index("    func saveKey() async {"):source.index("    func openVibe() {")]
        for forbidden in ("stopGateway()", "checkIdleGateway()", "anyOwnedHarnessRunning", "try await save()"):
            self.assertNotIn(forbidden, save)
        self.assertIn('command("save-key-metadata"', save)

    def test_saving_default_key_preserves_extra_slots_and_api_source_can_reload(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = default_settings()
            settings["providers"]["grok"].update(credential_mode="environment",
                key_accounts=[{"id": "work", "label": "Work"}], key_account="work")
            atomic_json(root / "settings.json", settings)
            runtime = Runtime.__new__(Runtime)
            runtime.root = root; runtime.settings = load_settings(root)
            runtime.lock = threading.Lock()
            saved = save_key_metadata(root, "grok", {"key_account": None})
            self.assertNotIn("key_account", saved["providers"]["grok"])
            self.assertEqual(saved["providers"]["grok"]["key_accounts"], [{"id": "work", "label": "Work"}])
            self.assertEqual(runtime.credential_settings()["providers"]["grok"]["credential_mode"], "keychain")
            runtime.settings["providers"]["grok"]["credential_mode"] = "cli"
            del runtime._credential_settings
            self.assertEqual(runtime.credential_settings()["providers"]["grok"]["credential_mode"], "cli")


class LiveCredentialRotationTests(unittest.TestCase):
    setUp = fixtures.GatewayHubHTTPTests.setUp
    tearDown = fixtures.GatewayHubHTTPTests.tearDown
    start_gateway = fixtures.GatewayHubHTTPTests.start_gateway
    request = fixtures.GatewayHubHTTPTests.request

    def test_save_key_while_streaming_preserves_socket_and_next_call_uses_new_key(self):
        self.start_gateway("claude", "claude-opus-5-5", {"reasoning": True})
        self.runtime.key = None
        self.runtime.settings["providers"]["claude"]["credential_mode"] = "keychain"
        saved = load_settings(self.root)
        saved["providers"]["claude"]["credential_mode"] = "keychain"
        atomic_json(self.root / "settings.json", saved)
        MockProvider.reset("slow_native")
        results = []
        with patch("gateway.credentials", side_effect=lambda value, _: (
                "mock-key-" + str(value["providers"]["claude"]["credential_revision"]), "test")):
            thread = threading.Thread(target=lambda: results.append(self.request(prompt(stream=True))))
            thread.start()
            deadline = time.monotonic() + 3
            while not MockProvider.request_headers and time.monotonic() < deadline: time.sleep(.01)
            self.assertTrue(MockProvider.request_headers)
            self.assertEqual(self.runtime.status()["active"], 1)
            save_key_metadata(self.root, "claude", {})
            self.assertEqual(self.runtime.status()["active"], 1)
            self.assertEqual(MockProvider.request_headers[0]["x-api-key"], "mock-key-0")
            MockProvider.release.set(); thread.join(6)
            self.assertFalse(thread.is_alive())
            self.assertEqual(results[0][0], 200)
            status, _, _ = self.request(prompt(stream=True))
            self.assertEqual(status, 200)
            self.assertEqual(MockProvider.request_headers[-1]["x-api-key"], "mock-key-1")
