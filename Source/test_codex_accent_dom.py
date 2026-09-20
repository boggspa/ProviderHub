"""Rendered watcher regressions; optional Node + Playwright Chromium.

Expose Playwright through NODE_PATH when it is supplied by a bundled runtime.
The ordinary Python suite skips these checks if Node or Chromium is absent.
"""
import json
import shutil
import subprocess
import unittest

from codex_accent import watcher_script


BROWSER_TESTS = r"""
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
const accent = '--provider-hub-accent';
const hue = '--provider-hub-hue';
const theme = 'data-provider-hub-theme';
const colours = {parent: 'rgb(112, 90, 255)', mistral: 'rgb(212, 68, 4)', kimi: 'rgb(0, 115, 230)', qwen: 'rgb(140, 82, 239)', grey: 'rgb(153, 153, 153)'};
const glyph = (id) => `<div class="group/activity-header"><span class="contents"><svg id="${id}" class="text-text/60"></svg></span><span class="text-text/60" id="${id}-label">Editing files</span><svg class="text-warning" id="${id}-warning"></svg><svg style="color: rgb(10, 20, 30)" id="${id}-identicon"></svg></div><span class="loading-shimmer" id="${id}-shimmer">Working</span>`;
const composer = (id, model, effort='high') => `<button data-codex-intelligence-trigger data-selected-reasoning-effort="${effort}"><span data-tooltip-overflow-target><span><span id="${id}">${model}</span><span>${effort}</span></span></span></button>`;
const tab = (id, tabId, content, controller='right') => `<div id="${id}" role="tabpanel" data-app-shell-tab-panel-controller="${controller}" data-tab-id="${tabId}">${content}</div>`;
const header = (model) => `<div class="flex h-12 shrink-0 items-center gap-2 border-b border-strong px-4"><button>Back</button><span>Agent name</span><span id="agent-model" class="max-w-1/2 min-w-0 truncate text-xs text-tertiary select-none">${model}</span></div>`;
const agent = (model='mistral/mistral-vibe-cli-latest · High') => tab('agent', 'subagents:parent', header(model) + glyph('agent-glyph'));
const side = (model='Kimi for Coding') => tab('side', 'sidechat:child', glyph('side-glyph') + composer('side-model', model));
const main = () => `<main>${glyph('main-glyph')}${composer('main-model', 'GPT-6 Astra')}</main>`;
const styles = `<style>:root{color:#ddd;--color-codex-description:rgb(150,150,150)} button{color:inherit} .text-text\\/60{color:rgb(153,153,153)} .text-warning{color:rgb(255,150,0)}</style>`;
// The origin guard is exercised separately. All other tests use the exact
// production watcher with only its URL input replaced for an about:blank fixture.
const fixtureScript = input.script.replace('String(location.href)', '"app://-/"');
const flush = (page) => page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
const colour = (page, id) => page.locator('#' + id).evaluate(el => getComputedStyle(el).color);
const custom = (page, id, property) => page.locator('#' + id).evaluate((el, prop) => getComputedStyle(el).getPropertyValue(prop).trim(), property);
const mount = async (page, content) => {
  await page.setContent(styles + content);
  const installed = await page.evaluate(fixtureScript);
  assert.equal(installed.installed, true);
  await flush(page);
};
const cases = {
  async separate_panes_even_when_child_composer_comes_first(page) {
    await mount(page, side() + agent() + main());
    assert.equal(await colour(page, 'main-glyph'), colours.parent);
    assert.equal(await colour(page, 'side-glyph'), colours.kimi);
    assert.equal(await colour(page, 'agent-glyph'), colours.mistral);
    for (const id of ['main-glyph', 'side-glyph', 'agent-glyph']) {
      assert.equal(await colour(page, id + '-label'), colours.grey);
      assert.equal(await colour(page, id + '-warning'), 'rgb(255, 150, 0)');
      assert.equal(await colour(page, id + '-identicon'), 'rgb(10, 20, 30)');
    }
    assert.notEqual(await custom(page, 'agent-glyph', hue), await custom(page, 'main-glyph', hue));
    assert.notEqual(await custom(page, 'side-glyph', hue), await custom(page, 'main-glyph', hue));
    await page.locator('#main-model').evaluate(el => { el.textContent = 'Qwen 3'; });
    await flush(page);
    assert.equal(await colour(page, 'main-glyph'), colours.qwen);
    assert.equal(await colour(page, 'agent-glyph'), colours.mistral);
    assert.equal(await colour(page, 'side-glyph'), colours.kimi);
  },
  async selected_agent_switches_routes_native_names_and_unknown_models(page) {
    await mount(page, main() + agent());
    for (const [model, expected] of [
      ['ollama/qwen3:cloud · Ultra', colours.qwen],
      ['codex/gpt-6-astra · Max', colours.parent],
      ['gpt-6-astra · Extra High', colours.parent],
      ['GPT-6 Astra · High', colours.parent],
      ['Custom label · Hosted · High', colours.kimi],
      ['mistral/mistral-vibe-cli-latest-extra · High', colours.grey],
      ['Unknown model', colours.grey],
      ['mistral/mistral-vibe-cli-latest · High', colours.mistral],
    ]) {
      await page.locator('#agent-model').evaluate((el, text) => { el.textContent = text; }, model);
      await flush(page);
      assert.equal(await colour(page, 'agent-glyph'), expected, model);
      assert.equal(await colour(page, 'main-glyph'), colours.parent, model);
    }
    await page.locator('#agent').evaluate((el, html) => el.insertAdjacentHTML('beforeend', html), composer('editable-agent-model', 'Unlisted'));
    await flush(page);
    assert.equal(await colour(page, 'agent-glyph'), colours.grey);
    await page.locator('#editable-agent-model').evaluate(el => { el.textContent = 'Kimi for Coding'; });
    await flush(page);
    assert.equal(await colour(page, 'agent-glyph'), colours.kimi);
    await page.locator('#editable-agent-model').evaluate(el => el.closest('[data-codex-intelligence-trigger]').remove());
    await flush(page);
    assert.equal(await colour(page, 'agent-glyph'), colours.mistral);
    await page.locator('#agent-model').evaluate(el => el.remove());
    await page.locator('#agent').evaluate(el => { el.insertAdjacentHTML('beforeend', '<span>mistral/mistral-vibe-cli-latest</span>'); });
    await flush(page);
    assert.equal(await colour(page, 'agent-glyph'), colours.grey);
  },
  async side_chat_switches_unknown_and_achromatic_without_parent_leakage(page) {
    await mount(page, main() + side());
    await page.locator('#side-model').evaluate(el => { el.textContent = 'Unlisted'; });
    await flush(page);
    assert.equal(await colour(page, 'side-glyph'), colours.grey);
    assert.equal(await custom(page, 'side-glyph', hue), '');
    await page.locator('#side-model').evaluate(el => { el.textContent = 'Plain'; });
    await flush(page);
    assert.equal(await colour(page, 'side-glyph'), 'rgb(128, 128, 128)');
    assert.equal(await custom(page, 'side-glyph', hue), '');
    assert.notEqual(await custom(page, 'main-glyph', hue), '');
    await page.locator('#side-model').evaluate(el => { el.textContent = 'Kimi for Coding'; });
    await page.locator('#main-model').evaluate(el => { el.textContent = 'Unlisted parent'; });
    await flush(page);
    assert.equal(await colour(page, 'main-glyph'), colours.grey);
    assert.equal(await colour(page, 'side-glyph'), colours.kimi);
    assert.equal(await page.locator('html').getAttribute(theme), null);
    assert.equal(await page.locator('#side').getAttribute(theme), 'dark');
  },
  async panel_reuse_detach_and_external_style_changes_restore_only_owned_values(page) {
    await page.setContent(styles + main() + agent());
    await page.locator('#agent').evaluate((el, data) => {
      el.style.setProperty(data.accent, '#112233', 'important');
      el.style.setProperty(data.hue, '42');
      el.setAttribute(data.theme, 'prior');
    }, {accent, hue, theme});
    await page.evaluate(fixtureScript);
    await flush(page);
    assert.equal(await colour(page, 'agent-glyph'), colours.mistral);
    await page.locator('#agent').evaluate(el => el.setAttribute('data-tab-id', 'file:readme'));
    await flush(page);
    const restored = await page.locator('#agent').evaluate((el, data) => ({
      accent: el.style.getPropertyValue(data.accent), priority: el.style.getPropertyPriority(data.accent),
      hue: el.style.getPropertyValue(data.hue), theme: el.getAttribute(data.theme)
    }), {accent, hue, theme});
    assert.deepEqual(restored, {accent:'#112233', priority:'important', hue:'42', theme:'prior'});
    await page.locator('#agent').evaluate(el => el.setAttribute('data-tab-id', 'subagents:other-parent'));
    await flush(page);
    assert.equal(await colour(page, 'agent-glyph'), colours.mistral);
    await page.locator('#agent').evaluate((el, property) => {
      el.style.setProperty(property, '#445566');
      window.detachedPanel = el;
      el.remove();
    }, accent);
    await flush(page);
    assert.equal(await page.evaluate(property => window.detachedPanel.style.getPropertyValue(property), accent), '#445566');
    assert.equal(await page.evaluate(() => window.__providerHubAccent.check().panels.length), 0);
    assert.equal(await colour(page, 'main-glyph'), colours.parent);
  },
  async secondary_panes_and_pending_tabs_cannot_supply_the_main_accent(page) {
    await mount(page, tab('other', 'browser:example', composer('other-model', 'Qwen 3')) +
      tab('loading', 'sidechat-loading:parent:1', glyph('loading-glyph')) + side() + main());
    assert.equal(await colour(page, 'main-glyph'), colours.parent);
    assert.equal(await colour(page, 'loading-glyph'), colours.grey);
    assert.equal(await page.locator('#other').getAttribute(theme), null);
    await page.locator('#side').evaluate(el => el.setAttribute('data-app-shell-tab-panel-controller', 'bottom'));
    await flush(page);
    assert.equal(await colour(page, 'side-glyph'), colours.kimi);
    assert.equal(await colour(page, 'main-glyph'), colours.parent);
  },
  async panel_mount_close_and_header_changes_follow_the_observer(page) {
    await mount(page, main());
    await page.locator('body').evaluate((el, html) => el.insertAdjacentHTML('beforeend', html), agent());
    await flush(page);
    assert.equal(await colour(page, 'agent-glyph'), colours.mistral);
    await page.locator('#agent-model').evaluate(el => { el.firstChild.data = 'ollama/qwen3:cloud · High'; });
    await flush(page);
    assert.equal(await colour(page, 'agent-glyph'), colours.qwen);
    await page.locator('#agent').evaluate(el => { window.detachedPanel = el; el.remove(); });
    await flush(page);
    assert.equal(await page.evaluate(property => window.detachedPanel.style.getPropertyValue(property), accent), '');
    assert.equal(await page.evaluate(attribute => window.detachedPanel.getAttribute(attribute), theme), null);
    assert.equal(await colour(page, 'main-glyph'), colours.parent);
  },
  async pane_theme_and_ultra_are_local(page) {
    await mount(page, main() + tab('side', 'sidechat:child', glyph('side-glyph') + composer('side-model', 'Kimi for Coding', 'ultra')));
    await page.locator('#side').evaluate(el => { el.style.color = '#222'; el.append(document.createElement('span')); });
    await flush(page);
    assert.equal(await page.locator('#side').getAttribute(theme), 'light');
    assert.equal(await page.locator('html').getAttribute(theme), 'dark');
    assert.equal(await colour(page, 'side-glyph'), colours.kimi);
    assert.equal(await colour(page, 'main-glyph'), colours.parent);
    assert.equal(await page.locator('#side [data-provider-hub-ultra]').count(), 1);
    assert.equal(await page.locator('main [data-provider-hub-ultra]').count(), 0);
  },
  async origin_and_frame_guards_remain_intact(page) {
    await page.setContent(styles + main() + agent());
    assert.deepEqual(await page.evaluate(input.script), {skipped:'origin'});
    assert.equal(await colour(page, 'main-glyph'), colours.grey);
    await page.locator('body').evaluate(el => { el.append(document.createElement('iframe')); });
    const child = page.frames().find(frame => frame !== page.mainFrame());
    assert.deepEqual(await child.evaluate(fixtureScript), {skipped:'frame'});
  },
};
(async () => {
  let browser;
  try { browser = await chromium.launch({headless:true}); }
  catch (error) {
    if (!String(error).includes("Executable doesn't exist")) { throw error; }
    process.stdout.write(JSON.stringify({skip:'Playwright Chromium is not installed'}));
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


class PaneAccentBrowserTests(unittest.TestCase):
    def test_rendered_watcher_pane_lifecycle(self):
        node = shutil.which("node")
        if node is None:
            self.skipTest("Node is not installed")
        script = watcher_script({
            "GPT-6 Astra": "#705AFF", "Kimi for Coding": "#0073E6",
            "Custom label · Hosted": "#0073E6", "Qwen 3": "#8C52EF", "Plain": "#808080",
        }, native_labels=["GPT-6-Astra", "gpt-6-astra"], route_accents={
            "mistral/mistral-vibe-cli-latest": "#D44404", "ollama/qwen3:cloud": "#8C52EF",
        })
        result = subprocess.run([node, "-e", BROWSER_TESTS], input=json.dumps({"script": script}),
                                text=True, capture_output=True, timeout=90)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        if report.get("skip"):
            self.skipTest(report["skip"])
        self.assertGreaterEqual(len(report["results"]), 8)
        for name, error in report["results"].items():
            with self.subTest(case=name):
                self.assertIsNone(error, error)


if __name__ == "__main__":
    unittest.main()
