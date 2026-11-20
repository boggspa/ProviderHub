import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import antigravity_accent
from antigravity_accent import (GATEWAY_ENV, AntigravityBridge, accent_catalogue, antigravity_environment,
                                bridge_command, watcher_script)

LOGO_ROOT = Path(__file__).resolve().parent


def _inventory():
    return {"models": [
        {"id": "kimi/kimi-k2", "display_name": "Kimi K2", "tools": True,
         "presentation": {"runtimeProvider": "kimi", "displayProvider": "Kimi", "accent": "#1F6FEB",
                          "logo": {"light": "provider-logos/provider-logo-kimi.png"}}},
        {"id": "mistral/devstral-2", "display_name": "Devstral 2", "tools": True,
         "presentation": {"runtimeProvider": "mistral", "displayProvider": "Mistral", "accent": "#D44404",
                          "logo": {"light": "provider-logos/provider-logo-mistral.png"}}},
    ]}


class CatalogueTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.directory)
        self.catalogue = accent_catalogue({"antigravity_catalogue": None}, _inventory(),
                                          cache_dir=self.directory, logo_root=LOGO_ROOT)

    def test_hub_rows_are_keyed_by_the_label_the_gateway_serves(self):
        self.assertEqual(self.catalogue["labels"]["Kimi K2"]["accent"], "#1F6FEB")
        self.assertEqual(self.catalogue["labels"]["Devstral 2 · Mistral"]["accent"], "#D44404")

    def test_native_rows_have_brand_rules_and_a_fallback(self):
        providers = [rule["pattern"] for rule in self.catalogue["brands"]]
        self.assertEqual(len(providers), 3)
        self.assertIsNotNone(self.catalogue["native"])
        self.assertTrue(all(uri.startswith("data:image/png;base64,") for uri in self.catalogue["logos"].values()))


class ScriptTests(unittest.TestCase):
    def test_script_embeds_data_and_guards_the_origin(self):
        script = watcher_script({"labels": {"</script>": {"accent": "#FFFFFF"}}, "brands": [], "native": None, "logos": {}})
        self.assertNotIn("__DATA__", script)
        self.assertNotIn("</script>", script)
        self.assertIn("127", script)
        self.assertIn('[data-testid="model-selector-trigger"]', script)
        self.assertIn('[data-testid="tool-group-collapsible"]', script)


class BridgeTests(unittest.TestCase):
    def test_only_loopback_pages_are_kept(self):
        self.assertTrue(AntigravityBridge.wanted({"type": "page", "url": "https://127.0.0.1:60264/"}))
        self.assertTrue(AntigravityBridge.wanted({"type": "page", "url": "https://127.0.0.1:60264/c/abc"}))
        self.assertTrue(AntigravityBridge.wanted({"type": "page", "url": "about:blank"}))
        self.assertFalse(AntigravityBridge.wanted({"type": "webview", "url": "https://127.0.0.1:3000/"}))
        self.assertFalse(AntigravityBridge.wanted({"type": "page", "url": "https://127.0.0.1.evil.com:1/"}))
        self.assertFalse(AntigravityBridge.wanted({"type": "page", "url": "https://accounts.google.com/"}))

    def test_environment_forwards_only_the_gateway_connection(self):
        values = {key: "v-" + key for key in GATEWAY_ENV}
        with mock.patch.dict(os.environ, {**values, "GEMINI_API_KEY": "nope", "HOME": "/Users/x"}):
            environment = antigravity_environment(Path("/nonexistent/Antigravity.app"))
        for key in GATEWAY_ENV:
            self.assertEqual(environment[key], "v-" + key)
        self.assertNotIn("GEMINI_API_KEY", environment)

    def test_a_running_antigravity_is_never_spawned_twice(self):
        events = []
        with mock.patch.object(antigravity_accent, "executable_path", return_value=Path("/x/Antigravity")), \
             mock.patch.object(antigravity_accent, "already_running", return_value=True), \
             mock.patch.object(antigravity_accent, "run") as run:
            self.assertEqual(bridge_command("/x.app", {}, {"models": []}, emit=events.append), 2)
        run.assert_not_called()

    def test_launch_uses_the_antigravity_bridge(self):
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.object(antigravity_accent, "executable_path", return_value=Path("/x/Antigravity")), \
             mock.patch.object(antigravity_accent, "already_running", return_value=False), \
             mock.patch.object(antigravity_accent, "run", return_value=0) as run:
            bridge_command("/x.app", {"antigravity_catalogue": None}, _inventory(), emit=lambda event: None,
                           state_root=Path(directory))
        self.assertIs(run.call_args.kwargs["bridge_factory"], AntigravityBridge)
        self.assertEqual(run.call_args.kwargs["launch_timeout"], 90.0)


# The markup below is copied (trimmed) from Antigravity 2.33.1's live DOM.
BROWSER_TEST = r"""
const fs = require('node:fs');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
let chromium;
try { chromium = require('playwright').chromium; }
catch (error) { if (error.code !== 'MODULE_NOT_FOUND') throw error; process.stdout.write(JSON.stringify({skip: 'Playwright is not installed'})); process.exit(0); }
const html = `<div class="h-full w-full dark">
 <div role="menuitem" id="hub" data-testid="model-selector-item" data-model-label="Kimi K2" style="display:flex"><span><span>Kimi K2</span></span></div>
 <div role="menuitem" id="claude" aria-haspopup="menu" style="display:flex"><span><span data-testid="model-selector-effort-group" data-model-base="Claude Opus 5.5">Claude Opus 5.5</span></span></div>
 <div role="menuitem" id="gemini" aria-haspopup="menu" style="display:flex"><span><span data-testid="model-selector-effort-group" data-model-base="Gemini 4 Neon">Gemini 4 Neon</span></span></div>
 <div role="menuitem" id="plain"><span>Not a model</span></div>
 <button id="pill" data-testid="model-selector-trigger" aria-label="Select model, current: Kimi K2 High" style="display:flex"><span id="pill-label" style="color:rgb(200,200,200)">Kimi K2<span id="effort" class="opacity-70"> High</span></span><svg width="14" height="14"></svg></button>
 <button id="tool" data-testid="tool-group-collapsible" style="color:rgb(150,150,150)"><span><span class="shimmer-text-mask"><span class="shimmer-text-content" id="shimmer">Edited file</span></span></span><svg id="chevron" width="16" height="16" style="color:rgb(150,150,150)"></svg></button>
</div>`;
(async () => {
  const browser = await chromium.launch();
  const page = await browser.newPage();
  await page.setContent(html);
  await page.evaluate(input.script);
  await page.waitForTimeout(100);
  const result = await page.evaluate(() => {
    const before = (id) => getComputedStyle(document.getElementById(id), '::before');
    const colour = (id) => getComputedStyle(document.getElementById(id)).color;
    return {
      hub: [document.getElementById('hub').getAttribute('data-provider-hub-agy'), before('hub').backgroundImage.slice(0, 20), before('hub').width],
      claude: document.getElementById('claude').style.getPropertyValue('--provider-hub-accent'),
      gemini: document.getElementById('gemini').style.getPropertyValue('--provider-hub-accent'),
      plain: document.getElementById('plain').hasAttribute('data-provider-hub-agy'),
      pill: document.getElementById('pill').getAttribute('data-provider-hub-agy'),
      effort: colour('effort'), label: colour('pill-label'), chevron: colour('chevron'), shimmer: colour('shimmer'),
      status: window.__providerHubAntigravity.status(),
    };
  });
  await browser.close();
  process.stdout.write(JSON.stringify(result));
})().catch((error) => { process.stdout.write(JSON.stringify({error: String(error)})); process.exit(1); });
"""


class RenderedWatcherTests(unittest.TestCase):
    def test_rows_pill_effort_and_tool_groups_take_the_presentation(self):
        node = shutil.which("node")
        if node is None:
            self.skipTest("node is not installed")
        with tempfile.TemporaryDirectory() as directory:
            catalogue = accent_catalogue({"antigravity_catalogue": None}, _inventory(), cache_dir=Path(directory),
                                         logo_root=LOGO_ROOT)
        script = watcher_script(catalogue).replace(json.dumps(antigravity_accent._APP_ORIGIN_PATTERN), json.dumps("^about:blank"))
        result = subprocess.run([node, "-e", BROWSER_TEST], input=json.dumps({"script": script}),
                                capture_output=True, text=True, timeout=120)
        output = json.loads(result.stdout or "{}")
        if "skip" in output:
            self.skipTest(output["skip"])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        kimi = "rgb(31, 111, 235)"
        self.assertEqual(output["hub"][0], "image")
        self.assertTrue(output["hub"][1].startswith('url("data:image'))
        self.assertEqual(output["hub"][2], "16px")
        claude = next(rule["entry"]["accent"] for rule in catalogue["brands"] if "claude" in rule["pattern"])
        gemini = next(rule["entry"]["accent"] for rule in catalogue["brands"] if "gemini" in rule["pattern"])
        self.assertEqual(output["claude"], claude)
        self.assertEqual(output["gemini"], gemini)
        self.assertFalse(output["plain"])
        self.assertEqual(output["pill"], "image")
        self.assertEqual(output["effort"], kimi)
        self.assertEqual(output["label"], "rgb(200, 200, 200)")
        self.assertEqual(output["chevron"], kimi)
        self.assertNotEqual(output["shimmer"], "rgb(150, 150, 150)")


if __name__ == "__main__":
    unittest.main()
