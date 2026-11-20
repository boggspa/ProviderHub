"""Antigravity uses an ephemeral process environment, not user config edits."""
import tempfile
import unittest
from pathlib import Path

from antigravity_catalogue import route_for_slug
from antigravity_profile import AntigravityProfile
from bridge_core import BridgeError


class ProfileTests(unittest.TestCase):
    def test_activation_returns_discoverable_routes_without_writing_config(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            profile = AntigravityProfile(root, running=lambda: False)
            settings = {"port": 11436, "antigravity_model": "mistral/large-3"}
            inventory = {"models": [{"id": "mistral/large-3", "display_name": "Large",
                                     "context": 200000, "tools": True}]}
            result = profile.activate(settings, inventory)
            self.assertEqual(result["antigravity_base_url"], "http://127.0.0.1:11436")
            self.assertEqual(route_for_slug(result["antigravity_model"]), "mistral/large-3")
            self.assertEqual(list(root.iterdir()), [])

    def test_running_app_is_not_reconfigured(self):
        with tempfile.TemporaryDirectory() as directory:
            profile = AntigravityProfile(directory, running=lambda: True)
            with self.assertRaises(BridgeError):
                profile.activate({"port": 11436}, {"models": []})

    def test_unknown_selection_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            profile = AntigravityProfile(directory, running=lambda: False)
            with self.assertRaises(BridgeError):
                profile.activate({"port": 11436, "antigravity_model": "mistral/nope"},
                                 {"models": [{"id": "mistral/large-3", "tools": True}]})
