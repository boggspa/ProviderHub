import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import vibe_accent
from vibe_accent import (APP_ORIGIN, TOKEN_ENV, VibeBridge, accent_catalogue, bridge_command,
                         vibe_environment, watcher_script)

LOGO_ROOT = Path(__file__).resolve().parent


def _inventory():
    return {"models": [
        {"id": "kimi/kimi-k2", "display_name": "Kimi K2", "tools": True,
         "presentation": {"runtimeProvider": "kimi", "displayProvider": "Kimi", "accent": "#1f6feb",
                          "logo": {"light": "provider-logos/provider-logo-kimi.png"}}},
        {"id": "mistral/glm-5-2", "display_name": "GLM 5.2", "tools": True,
         "presentation": {"runtimeProvider": "mistral", "displayProvider": "Mistral", "accent": "#D44404",
                          "logo": {"light": "provider-logos/provider-logo-mistral.png", "scale": 1.08,
                                   "dark": "provider-logos/provider-logo-mistral.png"}}},
        {"id": "kimi/no-tools", "display_name": "No tools", "tools": False,
         "presentation": {"accent": "#123456"}},
    ]}


class AccentCatalogueTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.directory)
        self.config = self.directory / "config.toml"
        self.config.write_text(
            'active_model = "mistral-large-4"\n\n'
            '[[models]]\nname = "mistral-large-4"\nalias = "mistral-large-4"\ndisplay_name = "Mistral Large 4"\nprovider = "mistral"\n\n'
            '[[models]]\nname = "my-local"\nalias = "local-llama"\nprovider = "llamacpp"\n\n'
            '[[models]]\nname = "kimi/kimi-k2"\nalias = "kimi/kimi-k2"\ndisplay_name = "stale"\nprovider = "provider_hub"\n')

    def catalogue(self, **settings):
        return accent_catalogue({"vibe_catalogue": None, **settings}, _inventory(), vibe_config=self.config,
                                cache_dir=self.directory / "logos", logo_root=LOGO_ROOT)

    def test_hub_routes_are_keyed_by_their_alias_with_accent_and_logo(self):
        catalogue = self.catalogue()
        kimi = catalogue["routes"]["kimi/kimi-k2"]
        self.assertEqual(kimi["accent"], "#1F6FEB")
        self.assertEqual(kimi["logo"]["light"], "provider-logos/provider-logo-kimi.png")
        self.assertTrue(catalogue["logos"][kimi["logo"]["light"]].startswith("data:image/png;base64,"))
        self.assertEqual(catalogue["routes"]["mistral/glm-5-2"]["logo"]["scale"], 1.08)
        self.assertNotIn("kimi/no-tools", catalogue["routes"])

    def test_labels_cover_display_names_and_the_shared_name_spelling(self):
        labels = self.catalogue()["labels"]
        # The provider is left out when the model name already carries it.
        self.assertEqual(labels["Kimi K2"], "kimi/kimi-k2")
        self.assertEqual(labels["Kimi K2 (kimi/kimi-k2)"], "kimi/kimi-k2")
        self.assertEqual(labels["GLM 5.2 · Mistral"], "mistral/glm-5-2")
        self.assertEqual(labels["Mistral Large 4"], "mistral-large-4")

    def test_native_and_foreign_rows(self):
        catalogue = self.catalogue()
        self.assertIn("mistral-large-4", catalogue["routes"])
        self.assertEqual(catalogue["foreign"], ["local-llama"])
        self.assertIsNotNone(catalogue["native"])
        # The Hub's own rows in Vibe's file come from the projection, not the
        # file, so a stale display name there is never used.
        self.assertNotIn("stale", catalogue["labels"])

    def test_providers_cover_raw_routes_in_old_notices(self):
        catalogue = self.catalogue(providers={"grok": {}})
        self.assertEqual(sorted(catalogue["providers"]), ["grok", "kimi", "mistral"])
        self.assertTrue(catalogue["providers"]["kimi"]["accent"].startswith("#"))
        # A foreign row's label is indexed so the watcher can leave it alone.
        self.assertEqual(catalogue["labels"]["local-llama"], "local-llama")

    def test_logos_are_downscaled_and_cached(self):
        catalogue = self.catalogue()
        cached = list((self.directory / "logos").glob("*.png"))
        self.assertTrue(cached)
        for path in cached:
            self.assertLess(path.stat().st_size, 40_000)
        self.assertLess(len(json.dumps(catalogue)), 200_000)

    def test_a_missing_logo_leaves_the_accent(self):
        inventory = {"models": [{"id": "kimi/kimi-k2", "display_name": "Kimi K2", "presentation": {
            "accent": "#1F6FEB", "logo": {"light": "provider-logos/does-not-exist.png"}}}]}
        catalogue = accent_catalogue({"vibe_catalogue": None}, inventory, vibe_config=self.config,
                                     cache_dir=None, logo_root=LOGO_ROOT)
        self.assertEqual(catalogue["routes"]["kimi/kimi-k2"], {"accent": "#1F6FEB", "provider": ""})

    def test_logo_paths_cannot_leave_the_logo_root(self):
        inventory = {"models": [{"id": "kimi/kimi-k2", "display_name": "Kimi K2", "presentation": {
            "accent": "#1F6FEB", "logo": {"light": "../../etc/passwd"}}}]}
        catalogue = accent_catalogue({"vibe_catalogue": None}, inventory, vibe_config=None,
                                     cache_dir=None, logo_root=LOGO_ROOT)
        self.assertNotIn("logo", catalogue["routes"]["kimi/kimi-k2"])
        self.assertNotIn("../../etc/passwd", catalogue["logos"])


class WatcherScriptTests(unittest.TestCase):
    def test_script_embeds_the_catalogue_and_guards_the_origin(self):
        script = watcher_script({"routes": {"a/b": {"accent": "#FFFFFF", "provider": "</script>"}},
                                 "labels": {}, "native": None, "foreign": [], "logos": {}})
        self.assertNotIn("__DATA__", script)
        self.assertNotIn("__ORIGIN__", script)
        self.assertNotIn("</script>", script)
        self.assertIn('"a/b"', script)
        self.assertIn("lechat", script)
        self.assertIn("__reactFiber$", script)
        self.assertIn("data-thread-item-key", script)
        self.assertIn("[data-step-state]", script)

    def test_script_parses_as_javascript_when_node_is_available(self):
        node = shutil.which("node")
        if node is None:
            self.skipTest("node is not installed")
        import subprocess
        script = watcher_script({"routes": {}, "labels": {}, "native": None, "foreign": [], "logos": {}})
        result = subprocess.run([node, "--check", "-"], input=script, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)


class BridgeTests(unittest.TestCase):
    def test_only_the_app_documents_are_kept(self):
        self.assertTrue(VibeBridge.wanted({"type": "page", "url": APP_ORIGIN + "/code"}))
        self.assertTrue(VibeBridge.wanted({"type": "page", "url": "lechat://app"}))
        self.assertTrue(VibeBridge.wanted({"type": "page", "url": "about:blank"}))
        self.assertFalse(VibeBridge.wanted({"type": "page", "url": "lechat://./out/renderer/about.html"}))
        self.assertFalse(VibeBridge.wanted({"type": "page", "url": "lechat://application.evil/"}))
        self.assertFalse(VibeBridge.wanted({"type": "page", "url": "https://chat.mistral.ai/"}))
        self.assertFalse(VibeBridge.wanted({"type": "worker", "url": APP_ORIGIN + "/"}))

    def test_environment_forwards_only_the_gateway_token(self):
        with mock.patch.dict(os.environ, {TOKEN_ENV: "secret", "MISTRAL_API_KEY": "nope", "HOME": "/Users/x"}):
            environment = vibe_environment(Path("/nonexistent/Vibe.app"))
        self.assertEqual(environment[TOKEN_ENV], "secret")
        self.assertNotIn("MISTRAL_API_KEY", environment)
        self.assertEqual(environment["PATH"], "/usr/bin:/bin:/usr/sbin:/sbin")

    def test_a_running_vibe_is_never_spawned_twice(self):
        events = []
        with mock.patch.object(vibe_accent, "executable_path", return_value=Path("/Applications/Vibe.app/Contents/MacOS/Vibe")), \
             mock.patch.object(vibe_accent, "already_running", return_value=True), \
             mock.patch.object(vibe_accent, "run") as run:
            code = bridge_command("/Applications/Vibe.app", {}, {"models": []}, emit=events.append)
        self.assertEqual(code, 2)
        run.assert_not_called()
        self.assertEqual(events[0]["stage"], "launch")

    def test_launch_uses_the_vibe_bridge(self):
        events = []
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.object(vibe_accent, "executable_path", return_value=Path("/Applications/Vibe.app/Contents/MacOS/Vibe")), \
             mock.patch.object(vibe_accent, "already_running", return_value=False), \
             mock.patch.object(vibe_accent, "run", return_value=0) as run:
            code = bridge_command("/Applications/Vibe.app", {"vibe_catalogue": None}, _inventory(), emit=events.append,
                                  state_root=Path(directory), vibe_config=Path(directory) / "missing.toml")
        self.assertEqual(code, 0)
        self.assertIs(run.call_args.kwargs["bridge_factory"], VibeBridge)
        self.assertEqual(events[0]["event"], "accents")
        self.assertEqual(events[0]["routes"], 2)


if __name__ == "__main__":
    unittest.main()
