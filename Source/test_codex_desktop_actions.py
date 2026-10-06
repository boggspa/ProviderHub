"""Native Desktop action adapter regressions; optional Playwright fixture."""
import json
import os
import shutil
import subprocess
import tempfile
import unittest

from codex_desktop_actions import desktop_actions_script


class DesktopActionScriptTests(unittest.TestCase):
    def test_script_is_idempotent_origin_guarded_and_exposes_exact_contract(self):
        script = desktop_actions_script()
        self.assertIn("__providerHubDesktopActions", script)
        self.assertIn('{ skipped: "installed" }', script)
        self.assertIn("window !== window.top", script)
        self.assertIn("app:\\/\\/-\\/", script)
        self.assertIn("async function send", script)
        self.assertIn("module.Lv({", script)
        self.assertIn('turnTrigger: "composer"', script)
        self.assertIn("hostId: \"local\"", script)

    def test_script_rejects_unverified_modes_and_preserves_settings(self):
        script = desktop_actions_script()
        # The only mode string emitted is native; send/steer/queue classification
        # remains owned by the Desktop turn coordinator and is not guessed here.
        self.assertIn('mode: "native"', script)
        self.assertNotIn("turn/start", script)
        self.assertNotIn("turn/steer", script)
        self.assertNotIn("electronBridge.sendMessageFromView", script)
        # Preserve thread settings: no model/effort/serviceTier/collab overrides.
        self.assertNotIn("model:", script)
        self.assertNotIn("reasoningEffort:", script)
        self.assertNotIn("serviceTier:", script)


class DesktopActionBrowserTests(unittest.TestCase):
    """Exercise the exact production script against a faithful fake app."""

    BROWSER_TESTS = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { chromium } = require('playwright');
(async () => {
  const source = fs.readFileSync(process.argv[2], 'utf8');
  const quotes = String.fromCharCode(34, 34, 34);
  const newline = String.fromCharCode(10);
  const marker = "return r" + quotes + newline;
  const end = newline + quotes;
  const start = source.indexOf(marker);
  const finish = source.indexOf(end, start + marker.length);
  const script = source.slice(start + marker.length, finish);
  const appModule = `export const W = { root: true };
export const Iv = () => { window.__nativeInitialized = true; };
export const Lv = async ({scope,threadId,sourceThreadId,prompt,turnTrigger}) => { const send_message_to_thread = true;
  if (!window.__nativeInitialized) throw Error('native initializer was omitted');
  if (scope.scope !== W) throw Error('wrong scope ' + JSON.stringify(scope));
  if (typeof threadId !== 'string' || prompt !== prompt.trim() || prompt.length === 0) throw Error('wrong native request');
  if (turnTrigger !== 'composer') throw Error('wrong turn trigger');
  if (window.__testReject) throw Error('native private failure text');
  if (window.__nativeResult !== undefined) return window.__nativeResult;
  return { threadId };
};
`;
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'provider-hub-actions-'));
  const assets = path.join(root, 'assets');
  fs.mkdirSync(assets);
  fs.writeFileSync(path.join(assets, 'app-initial-f9b16fbf8fc7.js'), appModule);
  fs.writeFileSync(path.join(root, 'index.html'), `<!doctype html><html><head><script type="module" src="./assets/app-initial-f9b16fbf8fc7.js"></script></head><body><div id="root"></div></body></html>`);
  const browser = await chromium.launch();
  const page = await browser.newPage();
  page.on('pageerror', error => console.error('pageerror', error));
  page.on('console', message => console.error('console', message.type(), message.text()));
  await page.route('http://fixture.local/', route => route.fulfill({ path: path.join(root, 'index.html') }));
  await page.route('http://fixture.local/assets/**', route => route.fulfill({ path: path.join(root, route.request().url().replace('http://fixture.local/', '')) }));
  await page.goto('http://fixture.local/');
  const installed = await page.evaluate(async ({ script }) => {
    window.__testRootScope = await import('./assets/app-initial-f9b16fbf8fc7.js').then(module => module.W);
    const scope = { scope: window.__testRootScope, get(){}, set(){}, watch(){}, when(){} };
    scope.scope = window.__testRootScope;
    const current = { memoizedState: { memoizedState: { current: scope }, next: null }, child: null, sibling: null, return: null, alternate: null };
    const alternate = { ...current, memoizedState: null };
    current.alternate = alternate;
    alternate.alternate = current;
    const container = { stateNode: { current }, memoizedState: null, child: null, sibling: null, return: null };
    document.getElementById('root')['__reactContainer$test'] = container;
    // The production origin guard is tested by the source assertions. This
    // fixture only replaces the URL test because file:// is not app://-/.
    const originPattern = String.raw`!/^app:\/\/-\//.test(String(location.href))`;
    const expression = script.replace(originPattern, "false");
    window.__providerHubActionsScript = expression;
    if (window.__fixtureDebug) console.log('expression-start', expression.slice(0, 200));
    const element = document.createElement('script');
    element.type = 'module';
    element.textContent = expression;
    document.body.append(element);
    return { queued: true };
  }, { script });
  await page.waitForFunction(() => window.__providerHubDesktopActions?.send instanceof Function, null, { timeout: 3000 });
  await page.waitForTimeout(100);
  assert.deepEqual(installed, { queued: true });
  assert.equal(await page.evaluate(() => window.__providerHubDesktopActions.ready()), true);
  const request = { threadId: '00000000-0000-4000-8000-000000000001', hostId: 'local', kind: 'local', prompt: 'hello' };
  assert.deepEqual(await page.evaluate(request => window.__providerHubDesktopActions.send(request), request),
                   { sent: true, mode: 'native', threadId: request.threadId });
  await assert.rejects(page.evaluate(() => window.__providerHubDesktopActions.send({threadId:'bad',hostId:'local',kind:'local',prompt:'x'})), /valid local threadId/);
  await assert.rejects(page.evaluate(() => window.__providerHubDesktopActions.send({threadId:'00000000-0000-4000-8000-000000000001',hostId:'remote',kind:'local',prompt:'x'})), /Only local Codex threads/);
  // Native result contract: undefined, a different thread, and rejection are
  // all failures. The adapter reports its generic code and never the native
  // error text, which can quote user content.
  await page.evaluate(() => { window.__nativeResult = null; });
  await assert.rejects(page.evaluate(request => window.__providerHubDesktopActions.send(request), request), /unexpected result/);
  await page.evaluate(() => { window.__nativeResult = { threadId: '00000000-0000-4000-8000-000000000002' }; });
  await assert.rejects(page.evaluate(request => window.__providerHubDesktopActions.send(request), request), /unexpected result/);
  await page.evaluate(() => {
    window.__nativeResult = undefined;
    window.__testReject = true;
  });
  await assert.rejects(page.evaluate(request => window.__providerHubDesktopActions.send(request), request), /native Desktop send failed/);
  await page.evaluate(() => { window.__testReject = false; });
  const diagnostics = await page.evaluate(() => window.__providerHubDesktopActions.diagnostics());
  assert.deepEqual(diagnostics, { ready: true, module: true, scope: true, moduleError: null, scopeError: null, sends: 1, errors: 3 });
  // A root remount replaces HostRoot.current. The next send must resolve the
  // new tree's scope; it cannot reuse the detached wrapper from the old root.
  const remounted = await page.evaluate(() => {
    const root = document.getElementById('root');
    const container = root['__reactContainer$test'];
    const scope = { scope: window.__testRootScope, get(){}, set(){}, watch(){}, when(){} };
    const current = { memoizedState: { memoizedState: { current: scope }, next: null }, child: null, sibling: null, return: null };
    container.stateNode = { current };
    return true;
  });
  assert.equal(remounted, true);
  assert.equal(await page.evaluate(request => window.__providerHubDesktopActions.send(request), request).then(() => true), true);
  await browser.close();
  fs.rmSync(root, { recursive: true, force: true });
})().catch(error => { console.error(error); process.exit(1); });
"""

    def test_browser_contract_with_faithful_fake_app(self):
        node = shutil.which("node")
        if node is None:
            self.skipTest("Node is not installed")
        node_modules = os.environ.get("NODE_PATH", "")
        if not node_modules or not os.path.isdir(node_modules):
            self.skipTest("Playwright runtime is not configured")
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as handle:
            handle.write(self.BROWSER_TESTS)
            fixture = handle.name
        try:
            environment = dict(os.environ, NODE_PATH=node_modules)
            source = os.path.join(os.path.dirname(__file__), "codex_desktop_actions.py")
            result = subprocess.run([node, fixture, source], capture_output=True, text=True,
                                    env=environment, timeout=20)
        finally:
            os.unlink(fixture)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
