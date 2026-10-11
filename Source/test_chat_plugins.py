from pathlib import Path
import platform
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from chat_plugin_mcp import MCPError
from chat_plugins import PluginConfig, PluginRegistry, peekaboo_config, validate_arguments
from test_chat_plugin_mcp import SERVER


class PluginTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.script = Path(self.temp.name) / "fake.py"
        self.script.write_text(SERVER)
        self.registry = PluginRegistry(self.temp.name)
        self.config = PluginConfig("test", sys.executable, ("-u", str(self.script)), platform.python_version())
        self.registry.configure(self.config)

    def tearDown(self):
        self.registry.close()
        self.temp.cleanup()

    def start(self):
        self.registry.set_enabled("test", True)
        self.registry.start("test")

    def test_configuration_and_health_never_launch(self):
        with patch("chat_plugins.subprocess.Popen", side_effect=AssertionError("unexpected process")):
            self.registry.set_enabled("test", True)
            self.assertFalse(self.registry.health()[0]["running"])
            self.assertEqual(self.registry.tool_definitions(), [])
            restored = PluginRegistry(self.temp.name)
            self.assertTrue(restored.health()[0]["enabled"])
            self.assertEqual(restored.tool_definitions(), [])

    def test_namespaced_normalized_dispatch(self):
        self.start()
        tool = self.registry.tool_definitions()[0]
        self.assertEqual(tool["name"], "plugin__test__echo")
        self.assertTrue(self.registry.describe(tool["name"], {"text": "hello"})["requires_approval"])
        result = self.registry.dispatch(tool["name"], {"text": "hello"})
        self.assertEqual(result["content"][0]["text"], "hello")
        self.assertEqual(result["changed_files"], [])
        tool["input_schema"]["required"] = []
        with self.assertRaises(ValueError):
            self.registry.dispatch(tool["name"], {})

    def test_disable_revokes_advertised_tools(self):
        self.start()
        name = self.registry.tool_definitions()[0]["name"]
        self.registry.set_enabled("test", False)
        self.assertEqual(self.registry.tool_definitions(), [])
        with self.assertRaises(MCPError):
            self.registry.dispatch(name, {"text": "hello"})

    def test_disable_interrupts_inflight_tool(self):
        self.start()
        timer = threading.Timer(.1, lambda: self.registry.set_enabled("test", False))
        timer.start()
        try:
            with self.assertRaises(MCPError):
                self.registry.dispatch("plugin__test__echo", {"text": "wait"})
        finally:
            timer.join()

    def test_version_mismatch_and_hash_mismatch(self):
        self.registry.configure(PluginConfig("test", sys.executable, expected_version="0.0.0"))
        self.registry.set_enabled("test", True)
        with self.assertRaisesRegex(MCPError, "version"):
            self.registry.start("test")
        self.registry.configure(PluginConfig("test", sys.executable, expected_version=platform.python_version(), executable_sha256="0" * 64))
        self.assertEqual(self.registry.health()[0]["availability"], "executable_hash_mismatch")

    def test_macos_availability(self):
        self.registry.configure(PluginConfig("peekaboo", "/not/installed", expected_version="4.9.0", minimum_macos="15.0"))
        with patch("chat_plugins.platform.system", return_value="Darwin"), patch("chat_plugins.platform.mac_ver", return_value=("14.7", (), "")):
            health = self.registry.health()[1]
            self.assertEqual(health["availability"], "unsupported_macos")
            self.assertEqual(health["dependency"]["version"], "4.9.0")

    def test_arguments_validated(self):
        self.start()
        for arguments in ({"text": 1}, {"text": "x", "extra": True}, {}):
            with self.assertRaises(ValueError):
                self.registry.dispatch("plugin__test__echo", arguments)
        with self.assertRaises(ValueError):
            validate_arguments(1, {"type": "integer", "minimum": 2})
        with self.assertRaises(ValueError):
            validate_arguments({}, {"$ref": "evil"})

    def test_exact_configuration_required(self):
        with self.assertRaises(ValueError):
            PluginConfig("test", "python", expected_version="1.0.0")
        with self.assertRaises(ValueError):
            PluginConfig("test", sys.executable, expected_version="latest")

    def test_reviewed_peekaboo_pin(self):
        config = peekaboo_config("/opt/homebrew/bin/peekaboo")
        self.assertEqual(config.args, ("mcp", "serve", "--transport", "stdio"))
        self.assertEqual(config.expected_version, "4.9.0")
        with self.assertRaises(ValueError):
            self.registry.configure(PluginConfig("peekaboo", config.executable, expected_version="4.8.0", minimum_macos="15.0"))

    def test_symlink_configuration_pins_resolved_target(self):
        link = Path(self.temp.name) / "launcher"
        link.symlink_to(sys.executable)
        config = PluginConfig("link", str(link), expected_version=platform.python_version())
        self.assertEqual(config.executable, str(Path(sys.executable).resolve()))
        link.unlink()
        link.symlink_to("/bin/false")
        self.assertEqual(config.executable, str(Path(sys.executable).resolve()))

    def test_cancelled_start_never_probes(self):
        self.registry.set_enabled("test", True)
        cancel = threading.Event()
        cancel.set()
        with patch("chat_plugins.subprocess.Popen", side_effect=AssertionError("spawned")):
            with self.assertRaisesRegex(MCPError, "cancelled"):
                self.registry.start("test", cancel)

    def test_release_verify_state_schema_shape(self):
        # v4.9.0 VerifyStateTool.swift uses oneOf predicates with nested selectors.
        schema = {"type": "object", "properties": {"predicates": {
            "type": "array", "minItems": 1, "maxItems": 8, "items": {"oneOf": [
                {"type": "object", "properties": {"kind": {"type": "string", "enum": ["window_exists"]}, "expected": {"type": "boolean"}}, "required": ["kind", "expected"]},
                {"type": "object", "properties": {"kind": {"type": "string", "enum": ["element_value"]}, "selector": {"type": "object", "properties": {"identifier": {"type": "string"}}}, "expected_value": {"type": "string"}}, "required": ["kind", "selector", "expected_value"]}
            ]}}}, "required": ["predicates"]}
        validate_arguments({"predicates": [{"kind": "window_exists", "expected": True}]}, schema)
        with self.assertRaises(ValueError):
            validate_arguments({"predicates": [{"kind": "window_exists", "expected": "yes"}]}, schema)
