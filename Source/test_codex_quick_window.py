"""The separate quick-composer window: lifecycle, relay, bounds and safety."""
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from codex_quick_window import HOST_BINDING, WINDOW_BINDING, QuickWindowBridge, quick_window_html
from test_codex_accent import FakeTransport

TID = "00000000-0000-4000-8000-000000000001"
ROW = {"threadId": TID, "hostId": "local", "kind": "local", "title": "Fix the tests", "supported": True,
       "active": True, "activeAccent": "#705AFF", "preview": "latest assistant reply"}


def sent(pipe, method):
    return [m for m in pipe.sent if m["method"] == method]


def reply(ident, value=None, error=None, raw=None):
    if error:
        return {"id": ident, "error": {"message": error}}
    return {"id": ident, "result": raw if raw is not None else {"result": {"value": value}}}


def binding(session, name, payload):
    return {"method": "Runtime.bindingCalled", "sessionId": session,
            "params": {"name": name, "payload": payload if isinstance(payload, str) else json.dumps(payload),
                       "executionContextId": 1}}


class QuickWindowBridgeTests(unittest.TestCase):
    def setUp(self):
        self.pipe, self.events = FakeTransport(), []
        self.directory = tempfile.mkdtemp()
        self.path = Path(self.directory) / "quick-window.json"
        self.bridge = QuickWindowBridge(self.pipe, self.events.append, self.path)

    def tearDown(self):
        shutil.rmtree(self.directory, ignore_errors=True)

    def open_window(self, session="W1", target="T1"):
        self.bridge.open()
        self.assertTrue(self.bridge.handle(reply(sent(self.pipe, "Target.createTarget")[-1]["id"], raw={"targetId": target})))
        self.assertTrue(self.bridge.wants_target({"targetId": target, "type": "page", "url": "about:blank"}))
        self.bridge.attached(session, {"targetId": target})
        window = sent(self.pipe, "Browser.getWindowForTarget")[-1]
        self.assertTrue(self.bridge.handle(reply(window["id"], raw={"windowId": 7, "bounds": {}})))

    def test_main_window_gets_runtime_and_the_launcher_binding(self):
        self.bridge.attach_main("main")
        self.assertEqual([(m["method"], m["sessionId"]) for m in self.pipe.sent],
                         [("Runtime.enable", "main"), ("Runtime.addBinding", "main")])
        self.assertEqual(self.pipe.sent[1]["params"], {"name": HOST_BINDING})

    def test_open_creates_one_window_fills_it_and_focuses_it(self):
        self.bridge.open()
        self.bridge.open()
        creates = sent(self.pipe, "Target.createTarget")
        self.assertEqual(len(creates), 1)
        self.assertEqual(creates[0]["params"], {"url": "about:blank", "newWindow": True, "width": 500, "height": 640})
        self.assertFalse(self.bridge.is_open())
        self.open_window()
        self.assertTrue(self.bridge.is_open())
        methods = [(m["method"], m["sessionId"]) for m in self.pipe.sent[1:]]
        self.assertEqual(methods[:4], [("Page.enable", "W1"), ("Runtime.enable", "W1"), ("Runtime.addBinding", "W1"),
                                       ("Page.setDocumentContent", "W1")])
        self.assertEqual(self.pipe.sent[3]["params"], {"name": WINDOW_BINDING})
        document = self.pipe.sent[4]["params"]
        self.assertEqual(document["frameId"], "T1")
        self.assertEqual(document["html"], quick_window_html())
        self.assertEqual(sent(self.pipe, "Browser.getWindowForTarget")[0]["params"], {"targetId": "T1"})
        self.assertEqual(sent(self.pipe, "Browser.setWindowBounds"), [])
        self.assertEqual(sent(self.pipe, "Target.activateTarget")[-1]["params"], {"targetId": "T1"})
        # Opening again only focuses the window that exists.
        self.bridge.open()
        self.assertEqual(len(sent(self.pipe, "Target.createTarget")), 1)
        self.assertEqual(len(sent(self.pipe, "Target.activateTarget")), 2)
        self.assertEqual([e["stage"] for e in self.events], ["created"])

    def test_a_blank_page_attaching_before_the_create_reply_is_our_window(self):
        # Desktop attaches the new target before answering createTarget, and
        # the launcher's binding and the poll's flag may both ask to open.
        self.bridge.attach_main("main")
        self.bridge.handle(binding("main", HOST_BINDING, {"type": "open-window"}))
        create = sent(self.pipe, "Target.createTarget")[-1]["id"]
        self.bridge.open()  # the poll's windowRequest, while the create is in flight
        self.assertEqual(len(sent(self.pipe, "Target.createTarget")), 1)
        self.assertFalse(self.bridge.wants_target({"targetId": "X", "type": "page", "url": "app://-/index.html"}))
        self.assertFalse(self.bridge.wants_target({"targetId": "X", "type": "iframe", "url": "about:blank"}))
        info = {"targetId": "T1", "type": "page", "url": "about:blank"}
        self.assertTrue(self.bridge.wants_target(info))
        self.bridge.attached("W1", info)
        self.assertEqual(self.bridge.target_id, "T1")
        self.assertEqual(self.pipe.sent[-2]["params"]["frameId"], "T1")
        self.assertTrue(self.bridge.is_open())
        self.bridge.open()  # created and attached: only focus, never a second window
        self.assertEqual(len(sent(self.pipe, "Target.createTarget")), 1)
        self.assertEqual(sent(self.pipe, "Target.activateTarget")[-1]["params"], {"targetId": "T1"})
        self.assertTrue(self.bridge.handle(reply(create, raw={"targetId": "T1"})))
        self.assertEqual(self.bridge.target_id, "T1")
        self.assertEqual(sent(self.pipe, "Target.closeTarget"), [])
        self.assertFalse(self.bridge.wants_target({"targetId": "T2", "type": "page", "url": "about:blank"}))
        # Once created and before attach, a repeat request is also absorbed.
        self.bridge.handle({"method": "Target.detachedFromTarget", "params": {"sessionId": "W1", "targetId": "T1"}})
        self.bridge.open()
        self.bridge.handle(reply(sent(self.pipe, "Target.createTarget")[-1]["id"], raw={"targetId": "T2"}))
        self.bridge.open()
        self.assertEqual(len(sent(self.pipe, "Target.createTarget")), 2)

    def test_a_stray_blank_page_is_released_when_the_create_reply_names_another(self):
        self.bridge.open()
        create = sent(self.pipe, "Target.createTarget")[-1]["id"]
        self.bridge.attached("S1", {"targetId": "STRAY", "type": "page", "url": "about:blank"})
        self.assertTrue(self.bridge.handle(reply(create, raw={"targetId": "T9"})))
        self.assertEqual(self.bridge.target_id, "T9")
        self.assertFalse(self.bridge.is_open())
        self.assertEqual(sent(self.pipe, "Target.closeTarget")[-1]["params"], {"targetId": "STRAY"})
        self.assertIn({"event": "quick-window", "stage": "mismatch"}, self.events)
        self.assertTrue(self.bridge.wants_target({"targetId": "T9", "type": "page", "url": "about:blank"}))

    def test_saved_bounds_size_the_window_and_reports_are_persisted(self):
        self.path.write_text(json.dumps({"left": 60, "top": 80, "width": 520, "height": 700}))
        self.bridge = QuickWindowBridge(self.pipe, self.events.append, self.path)
        self.open_window()
        self.assertEqual(sent(self.pipe, "Target.createTarget")[0]["params"]["width"], 520)
        self.assertEqual(sent(self.pipe, "Browser.setWindowBounds")[0]["params"],
                         {"windowId": 7, "bounds": {"left": 60, "top": 80, "width": 520, "height": 700}})
        self.assertTrue(self.bridge.handle(binding("W1", WINDOW_BINDING, {"type": "bounds", "left": 100, "top": 120, "width": 500, "height": 640})))
        self.assertEqual(json.loads(self.path.read_text()), {"left": 100, "top": 120, "width": 500, "height": 640})
        for bad in ({"type": "bounds", "left": 1, "top": 1, "width": 10, "height": 640},
                    {"type": "bounds", "left": "1", "top": 1, "width": 500, "height": 640},
                    {"type": "bounds", "left": True, "top": 1, "width": 500, "height": 640}):
            self.bridge.handle(binding("W1", WINDOW_BINDING, bad))
        self.assertEqual(json.loads(self.path.read_text()), {"left": 100, "top": 120, "width": 500, "height": 640})

    def test_state_is_pushed_once_the_page_is_ready_and_on_every_update(self):
        self.open_window()
        self.bridge.update("main", [ROW])
        self.assertEqual(sent(self.pipe, "Runtime.evaluate"), [])
        self.assertTrue(self.bridge.handle(binding("W1", WINDOW_BINDING, {"type": "ready"})))
        pushes = sent(self.pipe, "Runtime.evaluate")
        self.assertEqual(len(pushes), 1)
        self.assertEqual(pushes[0]["sessionId"], "W1")
        self.assertIn("__providerHubQuickWindow?.setState?.(", pushes[0]["params"]["expression"])
        self.assertIn("latest assistant reply", pushes[0]["params"]["expression"])
        self.bridge.update("main", [])
        self.assertEqual(len(sent(self.pipe, "Runtime.evaluate")), 2)
        self.assertNotIn("latest assistant", json.dumps(self.events))

    def test_send_goes_to_the_composer_session_and_the_outcome_back_without_logging_the_prompt(self):
        self.bridge.attach_main("main")
        self.open_window()
        self.bridge.update("main", [ROW])
        self.assertTrue(self.bridge.handle(binding("W1", WINDOW_BINDING, {"type": "send", "threadId": TID, "prompt": "secret prompt"})))
        call = sent(self.pipe, "Runtime.evaluate")[-1]
        self.assertEqual(call["sessionId"], "main")
        self.assertTrue(call["params"]["awaitPromise"])
        self.assertIn("sendFromHost(", call["params"]["expression"])
        self.assertIn("secret prompt", call["params"]["expression"])
        self.assertIn("app:", call["params"]["expression"])
        self.assertTrue(self.bridge.handle(reply(call["id"], {"ok": True, "reason": "Sent."})))
        result = sent(self.pipe, "Runtime.evaluate")[-1]
        self.assertEqual(result["sessionId"], "W1")
        self.assertEqual(result["params"]["expression"],
                         'window.__providerHubQuickWindow?.sendResult?.(%s)' % json.dumps({"threadId": TID, "ok": True, "reason": "Sent."}))
        self.assertEqual(self.events[-1], {"event": "quick-window", "stage": "send", "ok": True, "threadId": TID})
        self.assertNotIn("secret", json.dumps(self.events))
        # A rejected evaluate, a composer without the method, and a failure
        # with its own reason each come back as a kept draft.
        for outcome, expected in ((("error", None), "Send failed. Your draft is kept."),
                                  ((None, None), "Sending is unavailable in this Desktop version. Draft kept."),
                                  (({"ok": False, "reason": "Send failed: Desktop rejected the send. Your draft is kept."}, None),
                                   "Send failed: Desktop rejected the send. Your draft is kept.")):
            self.bridge.handle(binding("W1", WINDOW_BINDING, {"type": "send", "threadId": TID, "prompt": "again"}))
            call = sent(self.pipe, "Runtime.evaluate")[-1]
            self.bridge.handle(reply(call["id"], error="boom") if outcome[0] == "error" else reply(call["id"], outcome[0]))
            self.assertIn(json.dumps(expected), sent(self.pipe, "Runtime.evaluate")[-1]["params"]["expression"])
            self.assertFalse(self.events[-1]["ok"])

    def test_send_requests_are_validated_before_anything_reaches_codex(self):
        self.open_window()
        before = len(self.pipe.sent)
        self.bridge.handle(binding("W1", WINDOW_BINDING, {"type": "send", "threadId": "not-a-uuid", "prompt": "hi"}))
        self.assertEqual(len(self.pipe.sent), before)
        self.bridge.handle(binding("W1", WINDOW_BINDING, {"type": "send", "threadId": TID, "prompt": "   "}))
        self.assertIn("A non-empty prompt of at most 64 KiB is required.", self.pipe.sent[-1]["params"]["expression"])
        self.assertEqual(self.pipe.sent[-1]["sessionId"], "W1")
        self.bridge.handle(binding("W1", WINDOW_BINDING, {"type": "send", "threadId": TID, "prompt": "x" * 65537}))
        self.assertIn("64 KiB", self.pipe.sent[-1]["params"]["expression"])
        # No Codex window at all: the draft is kept with a reason.
        self.bridge.handle(binding("W1", WINDOW_BINDING, {"type": "send", "threadId": TID, "prompt": "hello"}))
        self.assertIn("not available", self.pipe.sent[-1]["params"]["expression"])
        self.assertEqual(sent(self.pipe, "Runtime.evaluate")[-1]["sessionId"], "W1")
        self.assertEqual(self.events, [{"event": "quick-window", "stage": "created"}])

    def test_launcher_binding_opens_only_from_an_attached_codex_window(self):
        self.bridge.attach_main("main")
        self.assertTrue(self.bridge.handle(binding("stranger", HOST_BINDING, {"type": "open-window"})))
        self.assertEqual(sent(self.pipe, "Target.createTarget"), [])
        self.assertTrue(self.bridge.handle(binding("main", HOST_BINDING, {"type": "open-window"})))
        self.assertEqual(len(sent(self.pipe, "Target.createTarget")), 1)
        # Other bindings and other methods are left to the other bridges.
        self.assertFalse(self.bridge.handle(binding("main", "__somethingElse", "{}")))
        self.assertFalse(self.bridge.handle({"method": "Target.attachedToTarget", "params": {"sessionId": "x", "targetInfo": {}}}))

    def test_navigating_away_closes_the_window_and_ignores_its_binding(self):
        self.bridge.attach_main("main")
        self.open_window()
        self.bridge.handle(binding("W1", WINDOW_BINDING, {"type": "ready"}))
        self.assertTrue(self.bridge.handle({"method": "Page.frameNavigated", "sessionId": "W1",
                                            "params": {"frame": {"id": "T1", "url": "about:blank"}}}))
        self.assertEqual(sent(self.pipe, "Target.closeTarget"), [])
        self.assertTrue(self.bridge.handle({"method": "Page.frameNavigated", "sessionId": "W1",
                                            "params": {"frame": {"id": "T1", "url": "https://example.test/"}}}))
        self.assertEqual(sent(self.pipe, "Target.closeTarget")[-1]["params"], {"targetId": "T1"})
        self.assertEqual(self.events[-1], {"event": "quick-window", "stage": "navigated"})
        before = len(self.pipe.sent)
        self.bridge.handle(binding("W1", WINDOW_BINDING, {"type": "send", "threadId": TID, "prompt": "from a foreign page"}))
        self.bridge.update("main", [ROW])
        self.assertEqual(len(self.pipe.sent), before)
        self.assertNotIn("foreign", json.dumps(self.pipe.sent))

    def test_closing_forgets_the_window_and_a_late_reply_cannot_act_on_the_next_one(self):
        self.open_window()
        late_window = sent(self.pipe, "Browser.getWindowForTarget")[-1]["id"]
        self.bridge.handle(binding("W1", WINDOW_BINDING, {"type": "close"}))
        self.assertEqual(sent(self.pipe, "Target.closeTarget")[-1]["params"], {"targetId": "T1"})
        self.assertTrue(self.bridge.handle({"method": "Target.detachedFromTarget", "params": {"sessionId": "W1", "targetId": "T1"}}))
        self.assertFalse(self.bridge.is_open())
        self.assertEqual(self.events[-1], {"event": "quick-window", "stage": "closed"})
        activations = len(sent(self.pipe, "Target.activateTarget"))
        self.assertFalse(self.bridge.handle(reply(late_window, raw={"windowId": 9, "bounds": {}})))
        self.assertEqual(len(sent(self.pipe, "Target.activateTarget")), activations)
        self.open_window(session="W2", target="T2")
        self.assertEqual(len(sent(self.pipe, "Target.createTarget")), 2)
        self.bridge.detach("W2")
        self.assertFalse(self.bridge.is_open())

    def test_malformed_payloads_are_ignored(self):
        self.bridge.attach_main("main")
        self.open_window()
        before = len(self.pipe.sent)
        for payload in (None, 42, "not json", "[1,2]", "x" * 90000, json.dumps({"type": "unknown"})):
            self.bridge.handle(binding("W1", WINDOW_BINDING, payload))
            self.bridge.handle(binding("main", HOST_BINDING, payload))
        self.assertEqual(len(self.pipe.sent), before)

    def test_page_reaches_the_worker_only_through_its_binding(self):
        html = quick_window_html()
        self.assertIn("<title>Recent chats</title>", html)
        self.assertIn(WINDOW_BINDING, html)
        self.assertNotIn("app://", html)
        self.assertNotIn("fetch(", html)
        self.assertNotIn("<script src", html)
        self.assertIn("prefers-color-scheme", html)


class QuickWindowPageTests(unittest.TestCase):
    """Render the window page in a browser and drive it through a fake host."""

    BROWSER_TEST = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const { chromium } = require('playwright');
(async () => {
  const html = fs.readFileSync(process.argv[2], 'utf8');
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 500, height: 640 } });
  page.on('pageerror', error => console.error('pageerror', error));
  // The worker's binding is faked ahead of the page script so the ready message is seen.
  const fake = '<script>window.__messages = []; window.__providerHubWindowHost = payload => window.__messages.push(JSON.parse(payload));</script>';
  await page.setContent(html.replace('<script>', fake + '<script>'));
  await page.waitForFunction(() => window.__providerHubQuickWindow && window.__messages.some(m => m.type === 'ready'));
  assert.equal(await page.title(), 'Recent chats');
  const id = n => `00000000-0000-7000-8000-${String(n).padStart(12, '0')}`;
  const rows = [
    { threadId: id(1), hostId: 'local', kind: 'local', title: 'First chat', supported: true, active: true, activeAccent: '#705aff', preview: 'latest\n reply' },
    { threadId: 'remote-1', hostId: 'ssh-box', kind: 'remote', title: 'Remote chat', supported: false, active: false, activeAccent: null, preview: null },
    { threadId: id(3), hostId: 'local', kind: 'local', title: 'Third chat', supported: true, active: false, activeAccent: null, preview: null },
    { threadId: id(1), hostId: 'local', kind: 'local', title: 'Duplicate', supported: true },
    { threadId: 7 },
  ];
  assert.deepEqual(await page.evaluate(rows => window.__providerHubQuickWindow.setState({ targets: rows }), rows), { applied: 3 });
  const titles = await page.locator('.item .title').allTextContents();
  assert.deepEqual(titles, ['First chat', 'Remote chat', 'Third chat']);
  assert.equal(await page.locator('.item').first().getAttribute('aria-selected'), 'true');
  assert.equal(await page.locator('.item').nth(1).getAttribute('aria-disabled'), 'true');
  assert.equal((await page.locator('.item .preview').first().textContent()).trim(), 'latest reply');
  assert.equal(await page.locator('.item.live .spin').count(), 1);
  assert.equal(await page.locator('.item.live .spin').evaluate(el => el.style.color), 'rgb(112, 90, 255)');
  // Drafts stay with their chat; Enter sends through the binding and keeps the draft until the outcome.
  const input = page.locator('input');
  await input.fill('draft one');
  await page.locator('.item').nth(2).click();
  assert.equal(await input.inputValue(), '');
  await input.fill('draft three');
  await page.locator('.item').first().click();
  assert.equal(await input.inputValue(), 'draft one');
  await input.press('Enter');
  const sendMessage = (await page.evaluate(() => window.__messages)).find(m => m.type === 'send');
  assert.deepEqual(sendMessage, { type: 'send', threadId: id(1), prompt: 'draft one' });
  assert.equal(await page.locator('.send').isDisabled(), true);
  assert.equal((await page.locator('.status').textContent()).trim(), 'Sending…');
  await input.fill('draft one edited');
  assert.deepEqual(await page.evaluate(r => window.__providerHubQuickWindow.sendResult(r), { threadId: id(1), ok: true, reason: 'Sent.' }), { applied: true });
  assert.equal(await input.inputValue(), 'draft one edited');
  assert.equal((await page.locator('.status').textContent()).trim(), 'Sent.');
  await input.fill('second');
  await page.locator('.send').click();
  assert.deepEqual(await page.evaluate(r => window.__providerHubQuickWindow.sendResult(r), { threadId: id(1), ok: false, reason: 'Send failed: Desktop rejected the send. Your draft is kept.' }), { applied: true });
  assert.equal(await input.inputValue(), 'second');
  assert.match(await page.locator('.status').textContent(), /rejected the send/);
  await input.fill('third');
  await page.locator('.send').click();
  assert.deepEqual(await page.evaluate(r => window.__providerHubQuickWindow.sendResult(r), { threadId: id(1), ok: true, reason: 'Sent.' }), { applied: true });
  assert.equal(await input.inputValue(), '');
  assert.deepEqual(await page.evaluate(r => window.__providerHubQuickWindow.sendResult(r), { threadId: id(3), ok: true }), { applied: false });
  // Unsupported rows cannot be messaged; Escape asks the worker to close.
  await page.locator('.item').nth(1).click({ force: true });
  assert.equal(await page.locator('.item').first().getAttribute('aria-selected'), 'true');
  await page.keyboard.press('Escape');
  assert.ok((await page.evaluate(() => window.__messages)).some(m => m.type === 'close'));
  // The page reports its bounds so the worker can reopen it where it was.
  await page.waitForFunction(() => window.__messages.some(m => m.type === 'bounds' && typeof m.width === 'number'), null, { timeout: 3000 });
  await page.evaluate(() => window.__providerHubQuickWindow.setState({ targets: [] }));
  assert.equal((await page.locator('.empty').textContent()).trim(), 'No recent chats are available in the sidebar.');
  await browser.close();
})().catch(error => { console.error(error); process.exit(1); });
"""

    def test_page_renders_rows_drafts_sends_and_outcomes(self):
        node = shutil.which("node")
        if node is None:
            self.skipTest("Node is not installed")
        node_modules = os.environ.get("NODE_PATH", "")
        if not node_modules or not os.path.isdir(node_modules):
            self.skipTest("Playwright runtime is not configured")
        with tempfile.TemporaryDirectory() as directory:
            fixture = os.path.join(directory, "fixture.js")
            html = os.path.join(directory, "window.html")
            with open(fixture, "w") as handle:
                handle.write(self.BROWSER_TEST)
            with open(html, "w") as handle:
                handle.write(quick_window_html())
            result = subprocess.run([node, fixture, html], capture_output=True, text=True,
                                    env=dict(os.environ, NODE_PATH=node_modules), timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
