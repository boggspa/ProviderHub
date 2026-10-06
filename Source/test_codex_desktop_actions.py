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

    def test_script_lists_approved_bundles_and_diagnostics(self):
        script = desktop_actions_script()
        # Both bundles the helper is verified against must be named explicitly.
        self.assertIn('app-initial-f9b16fbf8fc7.js', script)
        self.assertIn('app-initial-69cd8dbddec5.js', script)
        # Diagnostics must surface the observed hash so unapproved builds are
        # diagnosable without rerunning the helper under a debugger.
        self.assertIn("observedBundle: state.observedBundle", script)
        self.assertIn("approvedBundles: state.approvedBundles", script)
        # Fail-closed error must include the observed hash so the operator can
        # tell which Desktop build tripped the guard.
        self.assertIn("app-version", script)
        self.assertIn("(observed ${observedBundle})", script)

    def test_script_identifies_the_app_scope_by_its_own_links_not_an_export(self):
        script = desktop_actions_script()
        # The app module's W export is an unrelated selector; the descriptor
        # itself is never exported, so no export identity may be required.
        self.assertNotIn("module.W", script)
        self.assertNotIn("rootScope", script)
        self.assertIn('const APP_SCOPE_BRAND = "AppScope";', script)
        for check in ('descriptor.__scopeBrand !== APP_SCOPE_BRAND', 'descriptor.parent != null',
                      'typeof descriptor.id !== "symbol"', 'node.token !== descriptor',
                      'chain.get(descriptor.id) !== node'):
            self.assertIn(check, script)
        # The search reports how far it looked and the last failure's code.
        self.assertIn("scopeSearch: state.scopeSearch", script)
        self.assertIn("lastError: state.lastError", script)

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
  // Like the real bundle, the fake exports an unrelated selector as W and
  // never exports the app-scope descriptor. The native send accepts only
  // the handle the fixture tree holds, which is shaped like the one
  // Desktop's useScope keeps in a ref.
  const appModule = `export const W = { unrelatedSelector: true };
export const Iv = () => { window.__nativeInitialized = true; };
export const Lv = async ({scope,threadId,sourceThreadId,prompt,turnTrigger}) => { const send_message_to_thread = true;
  if (!window.__nativeInitialized) throw Error('native initializer was omitted');
  if (scope !== window.__testHandle) throw Error('wrong scope');
  if (scope.scope.__scopeBrand !== 'AppScope' || scope.node.token !== scope.scope || !(scope.chain instanceof Map)) throw Error('malformed scope');
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
  const treeSource = String(function makeTree(withHandle = true) {
    const descriptor = { __scopeBrand: 'AppScope', id: Symbol('AppScope'), parent: undefined };
    const node = { token: descriptor, store: {} };
    const chain = new Map([[descriptor.id, node]]);
    const handle = { scope: descriptor, node, chain, get(){}, set(){}, watch(){}, when(){} };
    const thread = { __scopeBrand: 'ThreadScope', id: Symbol('ThreadScope'), parent: descriptor };
    const threadNode = { token: thread };
    const threadHandle = { scope: thread, node: threadNode, chain: new Map([[descriptor.id, node], [thread.id, threadNode]]), get(){}, set(){}, watch(){}, when(){} };
    const detached = { scope: descriptor, node: { token: descriptor }, chain: new Map(), get(){}, set(){}, watch(){}, when(){} };
    const bare = { scope: { __scopeBrand: 'AppScope', id: Symbol('AppScope'), parent: undefined }, get(){}, set(){}, watch(){}, when(){} };
    const decoy = { memoizedState: { memoizedState: { current: threadHandle }, next: { memoizedState: { current: detached }, next: null } },
                    memoizedProps: { value: bare }, stateNode: null, child: null, sibling: null, return: null };
    const holder = { memoizedState: { memoizedState: { current: handle }, next: null }, memoizedProps: null, stateNode: null, child: null, sibling: null, return: null };
    if (withHandle) decoy.sibling = holder;
    const current = { memoizedState: null, memoizedProps: null, stateNode: null, child: decoy, sibling: null, return: null, alternate: null };
    window.__testHandle = withHandle ? handle : null;
    return current;
  });
  const APPROVED = ['app-initial-f9b16fbf8fc7.js', 'app-initial-69cd8dbddec5.js'];
  const UNAPPROVED = 'app-initial-deadbeef0000.js';
  // The adapter needs exactly one app-initial module URL on the page, so each
  // fixture page serves an index that loads only the bundle under test.
  const indexFor = hash => `<!doctype html><html><head><script type="module" src="./assets/${hash}"></script></head><body><div id="root"></div></body></html>`;
  fs.writeFileSync(path.join(assets, APPROVED[0]), appModule);
  fs.writeFileSync(path.join(assets, APPROVED[1]), appModule);
  fs.writeFileSync(path.join(assets, UNAPPROVED), appModule);
  fs.writeFileSync(path.join(root, 'index.html'), indexFor(APPROVED[0]));
  const browser = await chromium.launch();
  const page = await browser.newPage();
  page.on('pageerror', error => console.error('pageerror', error));
  page.on('console', message => console.error('console', message.type(), message.text()));
  await page.route('http://fixture.local/', route => route.fulfill({ path: path.join(root, 'index.html') }));
  await page.route('http://fixture.local/assets/**', route => route.fulfill({ path: path.join(root, route.request().url().replace('http://fixture.local/', '')) }));
  await page.goto('http://fixture.local/');
  const installed = await page.evaluate(async ({ script, hash, treeSource }) => {
    window.__testMakeTree = new Function('return (' + treeSource + ')')();
    const current = window.__testMakeTree();
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
  }, { script, hash: APPROVED[0], treeSource });
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
  assert.equal(diagnostics.ready, true);
  assert.equal(diagnostics.module, true);
  assert.equal(diagnostics.scope, true);
  assert.equal(diagnostics.moduleError, null);
  assert.equal(diagnostics.scopeError, null);
  assert.equal(diagnostics.observedBundle, APPROVED[0]);
  assert.equal(diagnostics.approvedBundles.length, APPROVED.length);
  assert.deepEqual(diagnostics.approvedBundles.map(b => b.hash), APPROVED);
  assert.equal(diagnostics.sends, 1);
  assert.equal(diagnostics.errors, 3);
  // Each approved bundle must import, prepare, and complete a native call.
  for (const hash of APPROVED.slice(1)) {
    const approvedPage = await browser.newPage();
    approvedPage.on('pageerror', error => console.error('pageerror', error));
    approvedPage.on('console', message => console.error('console', message.type(), message.text()));
    await approvedPage.route('http://fixture.local/', route => route.fulfill({ contentType: 'text/html', body: indexFor(hash) }));
    await approvedPage.route('http://fixture.local/assets/**', route => route.fulfill({ path: path.join(root, route.request().url().replace('http://fixture.local/', '')) }));
    await approvedPage.goto('http://fixture.local/');
    await approvedPage.evaluate(async ({ script, hash, treeSource }) => {
      window.__testMakeTree = new Function('return (' + treeSource + ')')();
      const current = window.__testMakeTree();
      const container = { stateNode: { current }, memoizedState: null, child: null, sibling: null, return: null };
      document.getElementById('root')['__reactContainer$test'] = container;
      const originPattern = String.raw`!/^app:\/\/-\//.test(String(location.href))`;
      const expression = script.replace(originPattern, 'false');
      const element = document.createElement('script');
      element.type = 'module';
      element.textContent = expression;
      document.body.append(element);
      return true;
    }, { script, hash, treeSource });
    await approvedPage.waitForFunction(() => window.__providerHubDesktopActions?.send instanceof Function, null, { timeout: 3000 });
    assert.equal(await approvedPage.evaluate(() => window.__providerHubDesktopActions.ready()), true);
    assert.deepEqual(await approvedPage.evaluate(request => window.__providerHubDesktopActions.send(request), request),
                     { sent: true, mode: 'native', threadId: request.threadId });
    const observed = await approvedPage.evaluate(() => window.__providerHubDesktopActions.diagnostics());
    assert.equal(observed.observedBundle, hash);
    await approvedPage.close();
  }
  // An unapproved bundle fail-closes with app-version and the observed hash
  // in both the message and diagnostics.
  const rejectedPage = await browser.newPage();
  rejectedPage.on('pageerror', error => console.error('pageerror', error));
  await rejectedPage.route('http://fixture.local/', route => route.fulfill({ contentType: 'text/html', body: indexFor(UNAPPROVED) }));
  await rejectedPage.route('http://fixture.local/assets/**', route => route.fulfill({ path: path.join(root, route.request().url().replace('http://fixture.local/', '')) }));
  await rejectedPage.goto('http://fixture.local/');
  await rejectedPage.evaluate(async ({ script, hash }) => {
    const originPattern = String.raw`!/^app:\/\/-\//.test(String(location.href))`;
    const expression = script.replace(originPattern, 'false');
    const element = document.createElement('script');
    element.type = 'module';
    element.textContent = expression;
    document.body.append(element);
    return true;
  }, { script, hash: UNAPPROVED });
  await rejectedPage.waitForFunction(() => window.__providerHubDesktopActions?.ready instanceof Function, null, { timeout: 3000 });
  assert.equal(await rejectedPage.evaluate(() => window.__providerHubDesktopActions.ready()), false);
  const rejectedDiagnostics = await rejectedPage.evaluate(() => window.__providerHubDesktopActions.diagnostics());
  assert.equal(rejectedDiagnostics.observedBundle, UNAPPROVED);
  assert.equal(rejectedDiagnostics.scopeError, 'app-version');
  assert.equal(rejectedDiagnostics.module, false);
  assert.equal(rejectedDiagnostics.scope, false);
  await assert.rejects(rejectedPage.evaluate(request => window.__providerHubDesktopActions.send(request), request),
                       /observed app-initial-deadbeef0000.js/);
  assert.equal((await rejectedPage.evaluate(() => window.__providerHubDesktopActions.diagnostics())).lastError.code, 'app-version');
  await rejectedPage.close();
  // A tree holding only look-alikes (a thread-level handle, a handle detached
  // from its chain, a methods-only object) must fail closed with the scope
  // code and report how far the search went, never pass a decoy to Desktop.
  const decoyPage = await browser.newPage();
  decoyPage.on('pageerror', error => console.error('pageerror', error));
  await decoyPage.route('http://fixture.local/', route => route.fulfill({ contentType: 'text/html', body: indexFor(APPROVED[1]) }));
  await decoyPage.route('http://fixture.local/assets/**', route => route.fulfill({ path: path.join(root, route.request().url().replace('http://fixture.local/', '')) }));
  await decoyPage.goto('http://fixture.local/');
  await decoyPage.evaluate(async ({ script, treeSource }) => {
    const makeTree = new Function('return (' + treeSource + ')')();
    const current = makeTree(false);
    const container = { stateNode: { current }, memoizedState: null, child: null, sibling: null, return: null };
    document.getElementById('root')['__reactContainer$test'] = container;
    const originPattern = String.raw`!/^app:\/\/-\//.test(String(location.href))`;
    const element = document.createElement('script');
    element.type = 'module';
    element.textContent = script.replace(originPattern, 'false');
    document.body.append(element);
    return true;
  }, { script, treeSource });
  await decoyPage.waitForFunction(() => window.__providerHubDesktopActions?.send instanceof Function, null, { timeout: 3000 });
  assert.equal(await decoyPage.evaluate(() => window.__providerHubDesktopActions.ready()), false);
  await assert.rejects(decoyPage.evaluate(request => window.__providerHubDesktopActions.send(request), request),
                       /app scope could not be found \(2 fibers inspected\)/);
  const decoyDiagnostics = await decoyPage.evaluate(() => window.__providerHubDesktopActions.diagnostics());
  assert.equal(decoyDiagnostics.scopeError, 'scope');
  assert.equal(decoyDiagnostics.lastError.code, 'scope');
  assert.deepEqual(decoyDiagnostics.scopeSearch, { fibers: 2, truncated: false });
  assert.equal(decoyDiagnostics.errors, 1);
  await decoyPage.close();
  // A root remount replaces HostRoot.current. The next send must resolve the
  // new tree's scope; it cannot reuse the detached wrapper from the old root.
  const remounted = await page.evaluate(() => {
    const root = document.getElementById('root');
    const container = root['__reactContainer$test'];
    const current = window.__testMakeTree();
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
