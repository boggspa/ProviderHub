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
  async visible_child_model_wins_during_reuse_and_late_worker_replies(page) {
    await mount(page, main() + agent('codex/gpt-6-astra · High'));
    await page.locator('#agent-model').evaluate((element, id) => {
      // React can keep the DOM node's original fiber after an update. Its
      // owner props still name the previously selected Codex child.
      element.__reactFiber$fixture = { memoizedProps: {}, return: {
        memoizedProps: { seed: id, onBack(){} }, return: {
          memoizedProps: { conversationId: id, hostId: 'local', onBack(){} }, return: null
        }
      }};
    }, threadId(2));
    await page.evaluate(data => window.__providerHubAccent.setChildAccents(data), {[threadId(2)]:'#705AFF'});
    assert.equal(await colour(page, 'agent-glyph'), colours.parent);
    await page.locator('#agent-model').evaluate(element => { element.firstChild.data = 'claude/fable · High'; });
    await flush(page);
    assert.equal(await colour(page, 'agent-glyph'), 'rgb(217, 119, 87)');
    // A reply already in flight must not undo the displayed model's accent.
    await page.evaluate(data => window.__providerHubAccent.setChildAccents(data), {[threadId(2)]:'#705AFF'});
    assert.equal(await colour(page, 'agent-glyph'), 'rgb(217, 119, 87)');
    assert.equal(await colour(page, 'main-glyph'), colours.parent);
    assert.notEqual(await custom(page, 'agent-glyph', hue), await custom(page, 'main-glyph', hue));
    await page.locator('#agent-model').evaluate(element => { element.firstChild.data = 'codex/gpt-6-astra · High'; });
    await flush(page);
    await page.evaluate(data => window.__providerHubAccent.setChildAccents(data), {[threadId(2)]:'#D97757'});
    assert.equal(await colour(page, 'agent-glyph'), colours.parent);
  },
  async ambiguous_reused_child_identity_cannot_borrow_previous_metadata(page) {
    await mount(page, main() + agent('Unlisted display label'));
    await page.locator('#agent-model').evaluate((element, ids) => {
      const branch = (id, hostId='local') => ({ memoizedProps: {}, return: {
        memoizedProps: { seed: id, onBack(){} }, return: {
          memoizedProps: { conversationId: id, hostId, onBack(){} }, return: null
        }
      }});
      const previous = branch(ids[0]);
      const next = branch(ids[1]);
      previous.alternate = next;
      next.alternate = previous;
      element.__reactFiber$fixture = previous;
    }, [threadId(2), threadId(3)]);
    assert.deepEqual(await page.evaluate(() => window.__providerHubAccent.childThreadIds()), []);
    await page.evaluate(data => window.__providerHubAccent.setChildAccents(data), {[threadId(2)]:'#705AFF'});
    assert.equal(await colour(page, 'agent-glyph'), colours.grey);
    await page.locator('#agent-model').evaluate(element => {
      const original = element.__reactFiber$fixture;
      original.return.memoizedProps.seed = original.alternate.return.memoizedProps.seed;
      original.return.return.memoizedProps.conversationId = original.alternate.return.return.memoizedProps.conversationId;
    });
    assert.deepEqual(await page.evaluate(() => window.__providerHubAccent.childThreadIds()), [threadId(3)]);
    await page.evaluate(data => window.__providerHubAccent.setChildAccents(data), {[threadId(3)]:'#D97757'});
    assert.equal(await colour(page, 'agent-glyph'), 'rgb(217, 119, 87)');
    await page.locator('#agent-model').evaluate(element => {
      element.__reactFiber$fixture.alternate.return.return.memoizedProps.hostId = 'remote-host';
    });
    assert.deepEqual(await page.evaluate(() => window.__providerHubAccent.childThreadIds()), []);
    await page.evaluate(data => window.__providerHubAccent.setChildAccents(data), {[threadId(3)]:'#D97757'});
    assert.equal(await colour(page, 'agent-glyph'), colours.grey);
  },
  async current_shell_header_rows_resolve_identity_and_survive_dock_switches(page) {
    const currentHeader = `<div class="flex h-full min-w-0 items-center gap-2 px-4"><button>Back</button><span>Agent name</span><span id="agent-model" class="max-w-1/2 min-w-0 truncate text-xs text-tertiary select-none">Unlisted display label</span></div>`;
    await mount(page, main() + tab('agent', 'subagents:parent', currentHeader + glyph('agent-glyph')));
    const bind = async (n, host='local') => page.locator('#agent-model').evaluate((element, value) => {
      element.__reactFiber$fixture = { memoizedProps: { children: element.textContent }, return: {
        memoizedProps: { seed: value.id, label: 'Agent', onBack(){} }, return: {
          memoizedProps: { conversationId: value.id, hostId: value.host, onBack(){} }, return: null
        }
      }};
    }, {id:threadId(n), host});
    await bind(2);
    assert.deepEqual(await page.evaluate(() => window.__providerHubAccent.childThreadIds()), [threadId(2)]);
    await page.evaluate(data => window.__providerHubAccent.setChildAccents(data), {[threadId(2)]:'#D44404'});
    assert.equal(await colour(page, 'agent-glyph'), colours.mistral);
    assert.equal(await colour(page, 'main-glyph'), colours.parent);
    await page.locator('#agent').evaluate(element => element.setAttribute('data-app-shell-tab-panel-controller', 'bottom'));
    await flush(page);
    assert.equal(await colour(page, 'agent-glyph'), colours.mistral);
    await bind(3);
    await page.evaluate(data => window.__providerHubAccent.setChildAccents(data), {[threadId(2)]:'#D44404'});
    assert.equal(await colour(page, 'agent-glyph'), colours.grey);
    await page.evaluate(data => window.__providerHubAccent.setChildAccents(data), {[threadId(3)]:'#0073E6'});
    assert.equal(await colour(page, 'agent-glyph'), colours.kimi);
    await bind(3, 'remote-host');
    assert.deepEqual(await page.evaluate(() => window.__providerHubAccent.childThreadIds()), []);
    await page.evaluate(data => window.__providerHubAccent.setChildAccents(data), {[threadId(3)]:'#0073E6'});
    assert.equal(await colour(page, 'agent-glyph'), colours.grey);
    await page.locator('#agent-model').evaluate(element => element.textContent='ollama/qwen3:cloud · Ultra');
    await flush(page);
    assert.equal(await colour(page, 'agent-glyph'), colours.qwen);
    for (const id of ['agent-glyph-label','agent-glyph-warning','agent-glyph-identicon']) {
      assert.equal(await colour(page, id), id.endsWith('label') ? colours.grey : id.endsWith('warning') ? 'rgb(255, 150, 0)' : 'rgb(10, 20, 30)');
    }
    await bind(3);
    await page.locator('#agent').evaluate(element => {element.hidden=true;});
    assert.deepEqual(await page.evaluate(() => window.__providerHubAccent.childThreadIds()), []);
  },
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
  async sidebar_status_reports_rows_spinners_and_paint(page) {
    await mount(page, '<nav>' + sidebarRow('a', 1) + sidebarRow('b', 2) + sidebarRow('host', 3, 'other-host') + '</nav>' + spinner('outside-spin'));
    const status = await page.evaluate(data => window.__providerHubAccent.setSidebarAccents(data), {[threadId(1)]: '#D44404'});
    assert.deepEqual(status, {colours: 1, rows: 3, local: 2, matched: 1, spinners: 4, rowSpinners: 3, painted: 1, accent: '', composers: [0, 0],
      sample: [['local:' + threadId(1), 'local', 'local'], ['local:' + threadId(2), 'local', 'local'], ['local:' + threadId(3), 'other-host', 'local']]});
    assert.equal(await colour(page, 'a-spin'), colours.mistral);
  },
  async sidebar_client_keyed_rows_resolve_through_their_own_row_props(page) {
    // The window that started a thread keys its row by the client id; only
    // the row component's props (same dataAttributes) carry the thread id.
    const client = id => `<div id="${id}" data-app-action-sidebar-thread-row data-app-action-sidebar-thread-kind="local" data-app-action-sidebar-thread-host-id="local" data-app-action-sidebar-thread-id="local:client-new-thread:${threadId(90)}">${spinner(id + '-spin')}</div>`;
    await mount(page, '<nav>' + client('own') + client('borrowed') + client('bare') + sidebarRow('plain', 1) + '</nav>');
    await page.evaluate(() => {
      const key = el => el.getAttribute('data-app-action-sidebar-thread-id');
      // Layout wrappers above the row carry its dataAttributes without the
      // thread id (26.924, 26 Sep rebuild); the id sits two components up.
      const own = document.getElementById('own');
      const naming = {dataAttributes: {'data-app-action-sidebar-thread-id': key(own)}, isActive: false};
      own.__reactFiber$test = {memoizedProps: {className: 'row'}, return: {memoizedProps: naming, return: {memoizedProps: {...naming, hostId: 'local'},
        return: {memoizedProps: {...naming, conversationId: '00000000-0000-4000-8000-000000000007'}, return: null}}}};
      // A parent for another row must never lend its thread id.
      const borrowed = document.getElementById('borrowed');
      borrowed.__reactFiber$test = {memoizedProps: {}, return: {memoizedProps: {dataAttributes: {'data-app-action-sidebar-thread-id': 'local:someone-else'}, conversationId: '00000000-0000-4000-8000-000000000008'}, return: null}};
    });
    assert.deepEqual(await page.evaluate(() => window.__providerHubAccent.sidebarThreadIds()), [threadId(7), threadId(1)]);
    await publishSidebar(page, {[threadId(7)]: '#D44404', [threadId(8)]: '#0073E6', [threadId(1)]: '#8C52EF'});
    assert.equal(await colour(page, 'own-spin'), colours.mistral);
    assert.equal(await colour(page, 'borrowed-spin'), colours.grey);
    assert.equal(await colour(page, 'bare-spin'), colours.grey);
    assert.equal(await colour(page, 'plain-spin'), colours.qwen);
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
  async retained_hidden_pages_cannot_supply_the_main_accent(page) {
    // The app keeps the pages it has left mounted, hidden by a React
    // Activity (display:none on the page's hosts) under a wrapper naming
    // the page inactive (26.928). One opened earlier precedes the page on
    // screen in DOM order, composer and all.
    const retained = (id, active, model) => `<div id="${id}" class="contents" data-app-shell-active-page="${active}"><section${active ? '' : ' style="display:none !important"'}>${glyph(id + '-glyph')}${composer(id + '-model', model)}</section></div>`;
    await mount(page, retained('first', false, 'GPT-6 Astra') + retained('second', true, 'Kimi for Coding'));
    assert.equal(await colour(page, 'second-glyph'), colours.kimi);
    const status = await page.evaluate(() => window.__providerHubAccent.sidebarStatus());
    assert.deepEqual([status.accent, status.composers], ['#0073E6', [2, 1]]);
    // Going back shows the retained page again: no node is added or removed.
    await page.evaluate(() => {
      for (const [id, active] of [['first', true], ['second', false]]) {
        const wrapper = document.getElementById(id);
        wrapper.setAttribute('data-app-shell-active-page', String(active));
        if (active) { wrapper.firstElementChild.style.removeProperty('display'); }
        else { wrapper.firstElementChild.style.setProperty('display', 'none', 'important'); }
      }
    });
    await flush(page);
    assert.equal(await colour(page, 'first-glyph'), colours.parent);
  },
  async retained_destinations_hidden_without_a_wrapper_cannot_supply_it_either(page) {
    // A retained sidebar destination has no wrapper, only the hidden hosts.
    await mount(page, `<section style="display:none !important">${composer('hidden-model', 'Qwen 3')}</section>` + main());
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
  async usage_banner_switch_hides_recovery_layout_banners_by_their_renderer(page) {
    // The recovery layout (sending blocked) has no icon and server copy; the
    // component that rendered the aside is what names it a usage banner.
    const aside = id => `<aside id="${id}" role="status" aria-live="polite"><h3>Banner ${id}</h3><button>Add Credits</button></aside>`;
    const gauge = '<aside id="gauge"><svg><path d="M10.8343 12.0693C10 12.5 9 12 8.5 11.4Z"></path></svg>Out of usage</aside>';
    await page.setContent(styles + main() + ['referral', 'exhausted', 'own', 'model', 'fallback', 'notice', 'bare'].map(aside).join('') + gauge);
    await page.evaluate(() => {
      const layout = parent => ({memoizedProps: {Icon: null, variant: 'recovery'}, return: parent});
      const server = actions => layout({memoizedProps: {banner: {banner_type: 'free_or_go_rate_limit_reached', ctas: actions.map(action => ({action, label: action}))}, behavior: {actions: {}}}, return: null});
      const host = (id, fiber) => { document.getElementById(id).__reactFiber$test = {memoizedProps: {role: 'status'}, return: fiber}; };
      host('referral', server(['add_credits', 'refer']));
      host('exhausted', server(['add_credits']));
      host('own', layout({memoizedProps: {rateLimitStatus: {plan_type: 'free'}, imageGenerationLimit: null, lastImageGenerationImpressionKeyRef: {current: null}}, return: null}));
      host('model', layout({memoizedProps: {modelName: 'gpt-6-astra', resetAt: null}, return: null}));
      // Other content under the provider must not borrow its banner, and a
      // chat hard-block notice in the same layout stays.
      host('fallback', {memoizedProps: {title: 'x'}, return: {memoizedProps: {banner: {ctas: [{action: 'refer'}]}, fallbackContent: null}, return: server(['refer'])}});
      host('notice', layout({memoizedProps: {bannerInfo: {call_to_action: ['new_chat']}, sendBlocked: true}, return: null}));
    });
    const installed = await page.evaluate(input.bannerScript.replace('String(location.href)', '"app://-/"'));
    assert.equal(installed.usageBanner, true);
    await flush(page);
    const display = id => page.locator('#' + id).evaluate(el => getComputedStyle(el).display);
    for (const id of ['referral', 'exhausted', 'own', 'model', 'gauge']) { assert.equal(await display(id), 'none', id); }
    for (const id of ['fallback', 'notice', 'bare']) { assert.equal(await display(id), 'block', id); }
    assert.equal(await page.evaluate(() => window.__providerHubAccent.check().banners), 5);
    // A reused aside that stops being a usage banner comes back.
    await page.evaluate(() => {
      const el = document.getElementById('referral');
      el.__reactFiber$test.return = {memoizedProps: {bannerInfo: {}}, return: null};
      el.querySelector('h3').textContent = 'Something else';
    });
    await flush(page);
    assert.equal(await display('referral'), 'block');
    assert.equal(await page.locator('#referral').getAttribute('data-provider-hub-usage-banner'), null);
  },
  async recovery_banners_stay_without_the_usage_banner_switch(page) {
    await page.setContent(styles + main() + '<aside id="off" role="status">Out of usage</aside>');
    await page.evaluate(() => { document.getElementById('off').__reactFiber$test = {memoizedProps: {}, return: {memoizedProps: {banner: {ctas: [{action: 'refer'}]}, behavior: {}}, return: null}}; });
    await page.evaluate(fixtureScript);
    await flush(page);
    assert.equal(await page.locator('#off').evaluate(el => getComputedStyle(el).display), 'block');
    assert.equal(await page.locator('#off').getAttribute('data-provider-hub-usage-banner'), null);
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
            "claude/fable": "#D97757",
        })
        script = watcher_script(accents, **options)
        unlock = watcher_script(accents, unlock_composer=True, **options)
        banner = watcher_script(accents, hide_usage_banner=True, **options)
        result = subprocess.run([node, "-e", BROWSER_TESTS], input=json.dumps({"script": script, "unlockScript": unlock, "bannerScript": banner,
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
