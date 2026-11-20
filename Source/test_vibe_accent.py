import json
import os
import shutil
import subprocess
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
        self.assertIn("data-provider-hub-effort", script)
        self.assertIn("function paintEffort", script)

    def test_script_parses_as_javascript_when_node_is_available(self):
        node = shutil.which("node")
        if node is None:
            self.skipTest("node is not installed")
        import subprocess
        script = watcher_script({"routes": {}, "labels": {}, "native": None, "foreign": [], "logos": {}})
        result = subprocess.run([node, "--check", "-"], input=script, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)


EFFORT_BROWSER_TESTS = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
let chromium;
try { chromium = require('playwright').chromium; }
catch (error) {
  if (error.code !== 'MODULE_NOT_FOUND') { throw error; }
  process.stdout.write(JSON.stringify({skip: 'Playwright is not installed'}));
  process.exit(0);
}
const kimi = 'rgb(31, 111, 235)', mistral = 'rgb(212, 68, 4)', plain = 'rgb(120, 120, 120)';
const styles = `<style>:root{color:rgb(10,20,30)} button{color:inherit} .text-subtle,.text-muted{color:rgb(120,120,120)} .hidden{display:none}</style>`;
const fixtureScript = input.script.replace('String(location.href)', '"lechat://app/code/p/s"');
const flush = (page) => page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
const colour = (page, id) => page.locator('#' + id).evaluate(el => getComputedStyle(el).color);
const icon = '<svg class="size-4 shrink-0"></svg>';
// button.group/model-picker > span.hidden @lg/chat-input-row:inline > [model text, preview span?, " · Low" span]
const pill = (model, effort, preview = '') =>
  `<button class="group/model-picker gap-2 px-2">${icon}<span class="hidden @lg/chat-input-row:inline" id="label">${model}${preview ? `<span class="transition-colors text-subtle" id="preview"> ${preview}</span>` : ''}${effort ? `<span class="transition-colors text-subtle" id="effort">${effort}</span>` : ''}</span></button>`;
const menu = () => `<div role="menu"><div role="menuitem" aria-haspopup="menu" data-state="open"><svg data-icon-asset="thinking" class="mt-0.5 size-4 shrink-0"></svg><span class="flex min-w-0 flex-1 flex-col gap-0.5"><span class="text-sm text-default" id="thinking-title">Thinking</span><span class="text-xs text-muted" id="thinking-level">Low</span></span></div>
  <div role="menu"><div role="menuitem" id="low-row"><span class="truncate">Low</span><input type="radio" checked><label id="low-radio" style="background-color:rgb(1,2,3)"></label></div></div></div>`;
const mount = async (page, content) => {
  await page.setContent(styles + content);
  const installed = await page.evaluate(fixtureScript);
  assert.equal(installed.installed, true);
  await flush(page);
};
const cases = {
  async effort_takes_the_models_accent_and_the_label_does_not(page) {
    await mount(page, pill('Kimi K2', ' · Low'));
    assert.equal(await colour(page, 'effort'), kimi);
    assert.equal(await page.locator('#label').evaluate(el => getComputedStyle(el).color), 'rgb(10, 20, 30)');
    // The model switches: the level follows without a loop of mutations.
    await page.locator('#label').evaluate(el => { el.firstChild.data = 'GLM 5.2'; });
    await flush(page);
    assert.equal(await colour(page, 'effort'), mistral);
    await page.locator('#effort').evaluate(el => { el.textContent = ' · High'; });
    await flush(page);
    assert.equal(await colour(page, 'effort'), mistral);
    // An unknown model takes the accent away again.
    await page.locator('#label').evaluate(el => { el.firstChild.data = 'Mystery'; });
    await flush(page);
    assert.equal(await colour(page, 'effort'), plain);
    assert.equal(await page.locator('#effort').evaluate(el => el.hasAttribute('data-provider-hub-effort') || el.style.length > 0), false);
  },
  async preview_span_is_not_mistaken_for_the_effort_level(page) {
    await mount(page, pill('Kimi K2', ' · Low', 'Preview'));
    assert.equal(await colour(page, 'effort'), kimi);
    assert.equal(await colour(page, 'preview'), plain);
    // No thinking level: the trailing span is the model's Preview badge.
    await page.evaluate(() => document.getElementById('effort').remove());
    await flush(page);
    assert.equal(await colour(page, 'preview'), plain);
  },
  async level_alone_in_a_pill_without_model(page) {
    await mount(page, pill('', 'Low'));
    // No model text to resolve: nothing to colour, nothing breaks.
    assert.equal(await colour(page, 'effort'), plain);
  },
  async thinking_submenu_level_and_checked_radio(page) {
    await mount(page, pill('Kimi K2', ' · Low') + menu());
    assert.equal(await colour(page, 'thinking-level'), kimi);
    assert.equal(await colour(page, 'thinking-title'), 'rgb(10, 20, 30)');
    assert.equal(await page.locator('#low-radio').evaluate(el => getComputedStyle(el).backgroundColor), 'rgb(31, 111, 235)');
  },
  async painting_settles_without_a_mutation_loop(page) {
    await mount(page, pill('Kimi K2', ' · Low') + menu());
    const writes = await page.evaluate(() => new Promise(resolve => {
      let count = 0;
      const observer = new MutationObserver(records => { count += records.length; });
      observer.observe(document.documentElement, { subtree: true, attributes: true, childList: true, characterData: true });
      setTimeout(() => resolve(count), 400);
    }));
    assert.equal(writes, 0);
  },
};
(async () => {
  let browser;
  try { browser = await chromium.launch({headless: true, ...(input.executablePath ? {executablePath: input.executablePath} : {})}); }
  catch (error) {
    if (!String(error).includes("Executable doesn't exist")) { throw error; }
    process.stdout.write(JSON.stringify({skip: 'Playwright Chromium is not installed'}));
    return;
  }
  const results = {};
  try {
    for (const [name, run] of Object.entries(cases)) {
      const page = await browser.newPage();
      try { await run(page); results[name] = null; }
      catch (error) { results[name] = error.stack; }
      finally { await page.close(); }
    }
  } finally { await browser.close(); }
  process.stdout.write(JSON.stringify({results}));
})().catch(error => { console.error(error); process.exitCode = 1; });
"""


class EffortAccentBrowserTests(unittest.TestCase):
    def test_rendered_effort_level_accent(self):
        node = shutil.which("node")
        if node is None:
            self.skipTest("Node is not installed")
        catalogue = {"routes": {"kimi/kimi-k2": {"accent": "#1F6FEB", "provider": "Kimi"},
                                "mistral/glm-5-2": {"accent": "#D44404", "provider": "Mistral"}},
                     "labels": {"Kimi K2": "kimi/kimi-k2", "GLM 5.2": "mistral/glm-5-2"},
                     "native": None, "providers": {}, "foreign": [], "logos": {}}
        result = subprocess.run([node, "-e", EFFORT_BROWSER_TESTS], input=json.dumps({
            "script": watcher_script(catalogue),
            "executablePath": os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH")}),
            text=True, capture_output=True, timeout=90)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        if report.get("skip"):
            self.skipTest(report["skip"])
        self.assertGreaterEqual(len(report["results"]), 5)
        for name, error in report["results"].items():
            with self.subTest(case=name):
                self.assertIsNone(error, error)


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
