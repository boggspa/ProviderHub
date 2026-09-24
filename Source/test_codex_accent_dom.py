"""Rendered watcher regressions; optional Node + Playwright Chromium.

Expose Playwright through NODE_PATH when it is supplied by a bundled runtime.
The ordinary Python suite skips these checks if Node or Chromium is absent.
"""
import json
import os
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
const threadId = n => `00000000-0000-4000-8000-${String(n).padStart(12, '0')}`;
const spinner = id => `<div role="status" aria-label="Working" class="relative flex size-5 shrink-0 items-center justify-center text-text/70" style="color:rgb(153,153,153)"><div class="motion-safe:animate-spin" style="animation-duration:2000ms"><svg id="${id}" class="icon-xs shrink-0" viewBox="0 0 24 24"><circle cx="12" cy="12" r="8" fill="none" stroke="currentColor" stroke-width="2" opacity=".2"/><path d="M12 4a8 8 0 0 1 8 8" fill="none" stroke="currentColor" stroke-width="2"/></svg></div></div>`;
const sidebarRow = (id, n, host='local', kind='local') => `<div id="${id}" data-app-action-sidebar-thread-row data-app-action-sidebar-thread-kind="${kind}" data-app-action-sidebar-thread-host-id="${host}" data-app-action-sidebar-thread-id="local:${threadId(n)}"><span data-thread-title id="${id}-title">Task ${n}</span>${spinner(id + '-spin')}<svg class="text-warning" id="${id}-warning"></svg><span id="${id}-unread" style="color:rgb(0,100,200)">2</span></div>`;
const publishSidebar = async (page, entries) => {
  await page.evaluate(data => window.__providerHubAccent.setSidebarAccents(data), entries);
  await flush(page);
};
const cases = {
  async sidebar_spinners_keep_each_owner_colour_in_both_themes(page) {
    await mount(page, main() + '<nav>' + sidebarRow('a', 1) + sidebarRow('b', 2) + sidebarRow('unknown', 3) + sidebarRow('host', 1, 'other-host') + sidebarRow('cloud', 1, 'local', 'remote') + '</nav>' + spinner('outside-spin'));
    assert.deepEqual(await page.evaluate(() => window.__providerHubAccent.sidebarThreadIds()), [threadId(1), threadId(2), threadId(3)]);
    await publishSidebar(page, {[threadId(1)]:'#D44404', [threadId(2)]:'#0073E6'});
    for (const ink of ['#ddd', '#222']) {
      await page.locator('html').evaluate((el, colour) => { el.style.color = colour; }, ink);
      await flush(page);
      assert.equal(await colour(page, 'a-spin'), colours.mistral);
      assert.equal(await colour(page, 'b-spin'), colours.kimi);
      for (const id of ['unknown-spin', 'host-spin', 'cloud-spin', 'outside-spin']) { assert.equal(await colour(page, id), colours.grey); }
      assert.equal(await colour(page, 'a-warning'), 'rgb(255, 150, 0)');
      assert.equal(await colour(page, 'a-unread'), 'rgb(0, 100, 200)');
      assert.equal(await colour(page, 'a-title'), ink === '#ddd' ? 'rgb(221, 221, 221)' : 'rgb(34, 34, 34)');
    }
    await page.locator('#main-model').evaluate(el => { el.textContent = 'Qwen 3'; });
    await flush(page);
    assert.equal(await colour(page, 'a-spin'), colours.mistral);
    await publishSidebar(page, {[threadId(1)]:'#8C52EF', [threadId(2)]:'#0073E6'});
    assert.equal(await colour(page, 'a-spin'), colours.qwen);
    assert.equal(await page.locator('#a-spin').evaluate(el => el.parentElement.style.animationDuration), '2000ms');
    await page.emulateMedia({reducedMotion:'reduce'});
    assert.equal(await colour(page, 'a-spin'), colours.qwen);
    await publishSidebar(page, {});
    assert.equal(await colour(page, 'a-spin'), colours.grey);
  },
  async sidebar_recycled_rows_mount_close_and_host_switch_restore_grey(page) {
    await mount(page, sidebarRow('a', 1));
    await publishSidebar(page, {[threadId(1)]:'#D44404', [threadId(2)]:'#0073E6'});
    await page.locator('#a').evaluate((el, id) => el.setAttribute('data-app-action-sidebar-thread-id', 'local:' + id), threadId(2));
    await flush(page);
    assert.equal(await colour(page, 'a-spin'), colours.kimi);
    await page.locator('#a').evaluate(el => el.setAttribute('data-app-action-sidebar-thread-host-id', 'another-host'));
    await flush(page);
    assert.equal(await colour(page, 'a-spin'), colours.grey);
    await page.locator('body').evaluate((el, html) => el.insertAdjacentHTML('beforeend', html), sidebarRow('new', 1));
    await flush(page);
    assert.equal(await colour(page, 'new-spin'), colours.mistral);
    await page.locator('#new-spin').evaluate(el => { window.oldSpinner = el; el.closest('[role="status"]').remove(); });
    await flush(page);
    assert.equal(await page.evaluate(() => window.oldSpinner.getAttribute('data-provider-hub-sidebar')), null);
    assert.equal(await page.evaluate(() => window.oldSpinner.style.getPropertyValue('--provider-hub-sidebar-accent')), '');
    await page.locator('#a').evaluate(el => el.setAttribute('data-app-action-sidebar-thread-id', 'local:pending-worktree'));
    await flush(page);
    assert.equal(await page.evaluate(() => window.__providerHubAccent.check().sidebarSpinners), 0);
  },
  async sidebar_restores_prior_style_and_rejects_invalid_colours(page) {
    await mount(page, sidebarRow('a', 1));
    await page.locator('#a-spin').evaluate(el => {
      el.style.setProperty('--provider-hub-sidebar-accent', '#112233', 'important');
      el.setAttribute('data-provider-hub-sidebar', 'prior');
    });
    await publishSidebar(page, {[threadId(1)]:'#D44404'});
    assert.equal(await colour(page, 'a-spin'), colours.mistral);
    await publishSidebar(page, {[threadId(1)]:'red;display:none'});
    assert.equal(await colour(page, 'a-spin'), colours.grey);
    assert.deepEqual(await page.locator('#a-spin').evaluate(el => [el.style.getPropertyValue('--provider-hub-sidebar-accent'), el.style.getPropertyPriority('--provider-hub-sidebar-accent'), el.getAttribute('data-provider-hub-sidebar')]), ['#112233','important','prior']);
    await publishSidebar(page, {[threadId(1)]:'#D44404'});
    await page.locator('#a-spin').evaluate(el => el.style.setProperty('--provider-hub-sidebar-accent', '#445566'));
    await publishSidebar(page, {});
    assert.equal(await page.locator('#a-spin').evaluate(el => el.style.getPropertyValue('--provider-hub-sidebar-accent')), '#445566');
  },
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
  async composer_unlock_frees_only_the_core_limit_of_the_usage_status(page) {
    await page.setContent(styles + main());
    const unlockScript = input.unlockScript.replace('String(location.href)', '"app://-/"');
    const installed = await page.evaluate(unlockScript);
    assert.equal(installed.installed, true);
    assert.equal(installed.unlock, true);
    await flush(page);
    const seen = await page.evaluate(() => {
      const status = JSON.parse('{"user_id":"u1","account_id":"a1","plan_type":"plus","rate_limit":{"allowed":false,"limit_reached":true,"primary_window":{"used_percent":100}},"additional_rate_limits":[{"limit_name":"gpt-reserve","rate_limit":{"allowed":false}}],"credits":{"has_credits":false}}');
      const snapshot = JSON.parse('{"version":1,"stream_id":"s","sequence":2,"usage":{"user_id":"u1","account_id":"a1","plan_type":"plus","rate_limit":{"allowed":false,"limit_reached":true}}}');
      const other = JSON.parse('{"rate_limit":{"allowed":false},"plan_type":"plus"}');
      const allowed = JSON.parse('{"user_id":"u1","account_id":"a1","plan_type":"plus","rate_limit":{"allowed":true}}');
      const revived = JSON.parse('{"a":1}', (key, value) => (key === 'a' ? value + 1 : value));
      let failure = null;
      try { JSON.parse('{nope'); } catch (error) { failure = error.name; }
      return { status, snapshot, other, allowed, revived, failure, list: JSON.parse('[1,2]'), length: JSON.parse.length, name: JSON.parse.name,
               check: window.__providerHubAccent.check().unlock, glyph: getComputedStyle(document.getElementById('main-glyph')).color };
    });
    assert.equal(seen.status.rate_limit.allowed, true);
    assert.equal(seen.status.rate_limit.limit_reached, true);
    assert.equal(seen.status.rate_limit.primary_window.used_percent, 100);
    assert.equal(seen.status.additional_rate_limits[0].rate_limit.allowed, false);
    assert.equal(seen.status.credits.has_credits, false);
    assert.equal(seen.snapshot.usage.rate_limit.allowed, true);
    assert.equal(seen.other.rate_limit.allowed, false);
    assert.equal(seen.allowed.rate_limit.allowed, true);
    assert.equal(seen.revived.a, 2);
    assert.equal(seen.failure, 'SyntaxError');
    assert.deepEqual(seen.list, [1, 2]);
    assert.equal(seen.length, 2);
    assert.equal(seen.name, 'parse');
    assert.deepEqual(seen.check, {seen: 3, unlocked: 2});
    assert.equal(seen.glyph, colours.parent);
    assert.deepEqual(await page.evaluate(unlockScript), {skipped: 'installed'});
  },
  async composer_unlock_stays_off_without_its_switch(page) {
    await mount(page, main());
    const seen = await page.evaluate(() => ({
      native: /\[native code\]/.test(String(JSON.parse)),
      allowed: JSON.parse('{"user_id":"u1","account_id":"a1","plan_type":"plus","rate_limit":{"allowed":false}}').rate_limit.allowed,
      check: window.__providerHubAccent.check().unlock,
    }));
    assert.deepEqual(seen, {native: true, allowed: false, check: null});
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
  try { browser = await chromium.launch({headless:true, ...(input.executablePath ? {executablePath:input.executablePath} : {})}); }
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
        accents = {
            "GPT-6 Astra": "#705AFF", "Kimi for Coding": "#0073E6",
            "Custom label · Hosted": "#0073E6", "Qwen 3": "#8C52EF", "Plain": "#808080",
        }
        options = dict(native_labels=["GPT-6-Astra", "gpt-6-astra"], route_accents={
            "mistral/mistral-vibe-cli-latest": "#D44404", "ollama/qwen3:cloud": "#8C52EF",
        })
        script = watcher_script(accents, **options)
        unlock = watcher_script(accents, unlock_composer=True, **options)
        result = subprocess.run([node, "-e", BROWSER_TESTS], input=json.dumps({"script": script, "unlockScript": unlock,
                                "executablePath": os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH")}),
                                text=True, capture_output=True, timeout=90)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        if report.get("skip"):
            self.skipTest(report["skip"])
        self.assertGreaterEqual(len(report["results"]), 10)
        for name, error in report["results"].items():
            with self.subTest(case=name):
                self.assertIsNone(error, error)


if __name__ == "__main__":
    unittest.main()
