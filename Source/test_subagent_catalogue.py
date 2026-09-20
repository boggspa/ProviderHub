"""Host catalogue discovery and CLI handoff regression tests; no paid calls."""
import copy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from subagent_catalogue import advertise_subagent_models
from responses_tools import flatten_tools, output_names
from test_codex_catalogue import inventory, model, settings


def spawn_tool():
    return {"type": "function", "name": "spawn_agent", "description":
            "Available model overrides (optional):\n- `old/model`: Old.\n"
            "Spawns an agent. Only call for independent useful work.",
            "parameters": {"type": "object", "properties": {
                "model": {"type": "string"}, "fork_turns": {"type": "string"},
                "message": {"type": "string"}, "task_name": {"type": "string"}},
                "required": ["task_name", "message"]}}


def host_tools():
    return [{"type": "namespace", "name": "collaboration", "tools": [spawn_tool()]}]


class SubagentCatalogueTests(unittest.TestCase):
    def test_nested_transport_disables_every_native_agent_launcher(self):
        import codex_cli_agent
        controls = codex_cli_agent._TRANSPORT_CONFIG
        for setting in ("agents.enabled=false", "features.multi_agent=false",
                        "features.multi_agent_v2=false"):
            with self.subTest(setting=setting):
                self.assertIn(setting, controls)

    def setUp(self):
        self.stock = inventory(*(model(f"mistral/model-{n}") for n in range(7)))
        self.config = settings(codex_model="mistral/model-0")

    def test_every_picker_model_is_advertised_with_its_efforts(self):
        tools = host_tools()
        schema = copy.deepcopy(tools[0]["tools"][0]["parameters"])
        self.assertEqual(advertise_subagent_models(tools, self.config, self.stock), 1)
        tool = tools[0]["tools"][0]
        for n in range(7):
            self.assertIn(f"`mistral/model-{n}`", tool["description"])
        self.assertIn("high (default)", tool["description"])
        self.assertIn('fork_turns="none"', tool["description"])
        self.assertIn("Only call for independent useful work.", tool["description"])
        self.assertNotIn("old/model", tool["description"])
        self.assertEqual(tool["parameters"], schema)
        before = copy.deepcopy(tools)
        advertise_subagent_models(tools, self.config, self.stock)
        self.assertEqual(tools, before)

    def test_only_curated_tool_capable_models_are_advertised(self):
        self.config["codex_catalogue"] = ["mistral/model-6", "gemini/no-tools", "gemini/missing"]
        self.stock["models"].append(model("gemini/no-tools", tools=False))
        tools = host_tools()
        advertise_subagent_models(tools, self.config, self.stock)
        description = tools[0]["tools"][0]["description"]
        self.assertIn("mistral/model-6", description)
        for absent in ("model-0", "gemini/no-tools", "gemini/missing"):
            self.assertNotIn(absent, description)

    def test_priority_orders_recommendations_without_hiding_other_models(self):
        self.config["codex_subagent_rank"] = {"mistral/model-6": 1}
        tools = host_tools()
        advertise_subagent_models(tools, self.config, self.stock)
        description = tools[0]["tools"][0]["description"]
        self.assertLess(description.index("mistral/model-6"), description.index("mistral/model-0"))
        self.assertIn("mistral/model-5", description)

    def test_host_enum_is_respected_without_widening_the_schema(self):
        tools = host_tools()
        tool = tools[0]["tools"][0]
        tool["parameters"]["properties"]["model"]["enum"] = ["mistral/model-6"]
        advertise_subagent_models(tools, self.config, self.stock)
        self.assertIn("mistral/model-6", tool["description"])
        self.assertNotIn("mistral/model-0", tool["description"])
        self.assertEqual(tool["parameters"]["properties"]["model"]["enum"], ["mistral/model-6"])

    def test_namespace_collision_and_reverse_mapping_are_preserved(self):
        unrelated = spawn_tool()
        tools = [copy.deepcopy(unrelated), *host_tools()]
        advertise_subagent_models(tools, self.config, self.stock)
        self.assertEqual(tools[0], unrelated)
        flattened, mapping = flatten_tools(tools)
        host = flattened[1]
        self.assertNotEqual(host["name"], "spawn_agent")
        self.assertIn("mistral/model-6", host["description"])
        restored = output_names({"type": "function_call", "name": host["name"],
                                 "arguments": '{"model":"mistral/model-6"}'}, mapping)
        self.assertEqual((restored["namespace"], restored["name"]), ("collaboration", "spawn_agent"))
        self.assertEqual(json.loads(restored["arguments"])["model"], "mistral/model-6")

    def test_missing_inventory_or_withheld_spawn_is_left_alone(self):
        for tools, stock in ((host_tools(), None), ([], self.stock),
                             ([{"type": "function", "name": "read_file"}], self.stock)):
            with self.subTest(tools=tools):
                before = copy.deepcopy(tools)
                self.assertEqual(advertise_subagent_models(tools, self.config, stock), 0)
                self.assertEqual(tools, before)

    def test_tools_without_model_override_are_left_alone(self):
        tools = host_tools()
        del tools[0]["tools"][0]["parameters"]["properties"]["model"]
        before = copy.deepcopy(tools)
        self.assertEqual(advertise_subagent_models(tools, self.config, self.stock), 0)
        self.assertEqual(tools, before)

    def test_bare_host_spawn_compatibility(self):
        tools = [spawn_tool()]
        self.assertEqual(advertise_subagent_models(tools, self.config, self.stock), 1)
        self.assertIn("mistral/model-6", tools[0]["description"])

    def test_catalogue_reaches_native_and_cli_routes_before_transport_translation(self):
        import responses_native
        import codex_cli_agent
        for route, mode in (("grok/grok-4.6", "keychain"),
                            ("codex/gpt-6-astra", "cli"), ("gemini/gemini-3.8-flash", "cli")):
            with self.subTest(route=route), tempfile.TemporaryDirectory() as directory:
                provider = route.split("/", 1)[0]
                config = copy.deepcopy(self.config)
                config["providers"][provider]["credential_mode"] = mode
                stock = inventory(*self.stock["models"], model(route))
                config["_model_specs"] = {row["id"]: row for row in stock["models"]}
                runtime = SimpleNamespace(settings=config, catalogue=stock, root=Path(directory),
                    replay_key="local-test", token="local-test", upstream_url=None,
                    provider_key=lambda _: "local-test")
                payload = {"model": route, "input": "Test delegation.", "tools": host_tools()}
                original = copy.deepcopy(payload)
                with patch.object(responses_native, "validate_connection", return_value={"base_url": "https://example.invalid"}), \
                        patch.object(responses_native, "_auth_headers", return_value={}), \
                        patch.object(responses_native, "connection_signature", return_value="local-test"):
                    plan = responses_native.prepare_native(runtime, payload)
                self.assertEqual(payload, original)
                forwarded = plan["body"]["tools"][0]
                self.assertIn("mistral/model-6", forwarded["description"])
                if provider == "codex":
                    params = codex_cli_agent._thread_params(
                        {"model": "gpt-6-astra", "effort": "high", "tools": [forwarded]},
                        SimpleNamespace(cwd=directory))
                    dynamic = params["dynamicTools"][0]["tools"][0]
                    self.assertEqual(dynamic["name"], codex_cli_agent._tool_alias("spawn_agent"))
                    self.assertIn("mistral/model-6", dynamic["description"])


if __name__ == "__main__":
    unittest.main()
