"""Render production quick-composer JS against the observed sidebar hooks."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import unittest

from codex_quick_composer import quick_composer_script


BROWSER = r"""
const assert=require('node:assert/strict'),fs=require('node:fs');
const {chromium}=require('playwright');const input=JSON.parse(fs.readFileSync(0,'utf8'));
const id=n=>`00000000-0000-7000-8000-${String(n).padStart(12,'0')}`;
const row=(n,host='local',kind='local')=>`<div data-app-action-sidebar-thread-row data-app-action-sidebar-thread-id="local:${id(n)}" data-app-action-sidebar-thread-host-id="${host}" data-app-action-sidebar-thread-kind="${kind}" data-app-action-sidebar-thread-title="Chat ${n}"><span>Chat ${n}</span></div>`;
const fixture=()=>`<style>:root{--color-background:#202022;--color-text:#eee;color:#eee;background:#181819;font-family:system-ui}#app-shell-sidebar{width:260px;padding:10px}#bar{display:flex;align-items:center;gap:8px}button{color:inherit;background:none}#recent>div{padding:10px}</style><div id="app-shell-sidebar"><div id="bar"><b>Codex</b><button id="bell">Bell</button><button id="search">Search</button></div><section id="pinned" data-app-action-sidebar-section data-app-action-sidebar-section-heading="Pinned">${row(50)}</section><section id="recent" data-app-action-sidebar-section data-app-action-sidebar-section-heading="Tasks">${Array.from({length:12},(_,i)=>row(i+1,i===1?'remote':'local')).join('')}</section><section id="projects" data-app-action-sidebar-section data-app-action-sidebar-section-heading="Projects">${row(80)}</section></div><button aria-label="Search transcript">Other search</button>`;
const flush=page=>page.evaluate(()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))));
async function bind(page){await page.evaluate(()=>{
 document.querySelector('#search').__reactFiber$test={memoizedProps:{uniform:true,onClick:function(){return 'chat-search-command-menu';}},return:null};
 for(const [element,sectionKey]of [['recent','chats'],['pinned','pinned'],['projects','threads']])document.getElementById(element).__reactFiber$test={memoizedProps:{sectionKey},return:null};
});}
const shell='[data-provider-hub-quick-composer]';
(async()=>{
 const browser=await chromium.launch({headless:true});const page=await browser.newPage({viewport:{width:1000,height:900}});
 await page.setContent(fixture());await bind(page);
 const script=input.script.replace('String(location.href)','"app://-/"');
 assert.deepEqual(await page.evaluate(input.script),{skipped:'origin'});
 assert.deepEqual(await page.evaluate(script),{installed:true});await flush(page);
 assert.equal(await page.locator('#bar [data-provider-hub-quick-launcher]').count(),1);
 assert.equal(await page.evaluate(()=>window.__providerHubQuickComposer.recentThreadTargets().length),0);
 await page.locator('[data-provider-hub-quick-launcher]').click();
 let targets=await page.evaluate(()=>window.__providerHubQuickComposer.recentThreadTargets());
 assert.equal(targets.length,10);assert.equal(targets[0].threadId,id(1));assert.equal(targets[1].supported,false);assert.equal(targets[9].threadId,id(10));
 assert.equal(await page.locator(shell+' .item').count(),10);
 await page.evaluate(t=>window.__providerHubQuickComposer.setThreadPreviews([{...t,preview:'latest\nassistant response'},{threadId:'wrong',hostId:'local',kind:'local',preview:'intruder'}]),targets[0]);
 assert.equal((await page.locator(shell+' .preview').first().textContent()).trim(),'latest assistant response');
 const capsule=page.locator(shell+' input');await capsule.fill('draft one');
 await page.locator(shell+' .item').nth(2).click();assert.equal(await capsule.inputValue(),'');await capsule.fill('draft three');
 await page.locator(shell+' .item').first().click();assert.equal(await capsule.inputValue(),'draft one');
 // Native rejection keeps the exact draft and identifies the failed target.
 await page.evaluate(()=>window.__providerHubDesktopActions={send:async()=>{throw Error('private failure payload');}});
 await page.locator(shell+' .send').click();assert.equal(await capsule.inputValue(),'draft one');assert.match(await page.locator(shell+' .status').textContent(),/draft is kept/);
 assert.doesNotMatch(await page.locator(shell+' .status').textContent(),/private failure/);
 // Pending send must not clear another selected chat or a newer typed draft.
 await page.evaluate(()=>window.__providerHubDesktopActions={send:request=>new Promise(resolve=>{window.finishQuickSend=()=>resolve({sent:true,mode:'native',threadId:request.threadId});})});
 await page.locator(shell+' .send').click();await page.locator(shell+' .item').nth(2).click();await capsule.fill('newer three');
 await page.evaluate(()=>window.finishQuickSend());await flush(page);assert.equal(await capsule.inputValue(),'newer three');
 await page.locator(shell+' .item').first().click();assert.equal(await capsule.inputValue(),'');await capsule.fill('submit one');
 await page.locator(shell+' .send').click();await capsule.fill('newer one');await page.evaluate(()=>window.finishQuickSend());await flush(page);assert.equal(await capsule.inputValue(),'newer one');
 // Reordering, removal and recycled identity keep native order and exact target.
 await page.evaluate(()=>{const group=document.getElementById('recent');group.prepend(group.children[2]);});await flush(page);
 assert.equal((await page.evaluate(()=>window.__providerHubQuickComposer.recentThreadTargets()))[0].threadId,id(3));
 await page.evaluate(()=>document.querySelector('[data-app-action-sidebar-thread-id="local:00000000-0000-7000-8000-000000000001"]').remove());await flush(page);
 assert.notEqual(await capsule.inputValue(),'newer one');
 // Composer remains same row and both prose lines truncate to the available width.
 const layout=await page.locator(shell+' .composer').evaluate(el=>({height:el.getBoundingClientRect().height,overflow:getComputedStyle(el.getRootNode().querySelector('.title')).textOverflow}));
 assert(layout.height<60);assert.equal(layout.overflow,'ellipsis');
 if(input.screenshot)await page.screenshot({path:input.screenshot});
 await page.emulateMedia({colorScheme:'light'});
 await page.evaluate(()=>{document.documentElement.style.setProperty('--color-text','#222');document.documentElement.style.setProperty('--color-background','#fafafa');});
 assert.equal(await page.locator(shell+' .title').first().evaluate(el=>getComputedStyle(el).color),'rgb(34, 34, 34)');
 // A replaced masthead recovers the launcher without duplicate UI or state loss.
 await page.evaluate(()=>{const old=document.getElementById('bar');old.outerHTML='<div id="bar"><b>Codex</b><button id="bell">Bell</button><button id="search">Search</button></div>';document.querySelector('#search').__reactFiber$test={memoizedProps:{uniform:true,onClick:function(){return 'chat-search-command-menu';}},return:null};});await flush(page);
 assert.equal(await page.locator('[data-provider-hub-quick-launcher]').count(),1);
 await capsule.press('Escape');assert.equal(await page.evaluate(()=>window.__providerHubQuickComposer.isOpen()),false);
 assert.equal(await page.evaluate(()=>window.__providerHubQuickComposer.recentThreadTargets().length),0);
 assert.equal(await page.evaluate(()=>window.__providerHubQuickComposer.setThreadPreviews([{threadId:'a'}]).applied),0);
 await page.evaluate(()=>window.__providerHubQuickComposer.uninstall());await flush(page);
 assert.equal(await page.locator(shell).count(),0);assert.equal(await page.locator('[data-provider-hub-quick-launcher]').count(),0);
 // A custom section named Tasks must not replace the native chats group.
 await page.evaluate(()=>{document.getElementById('recent').__reactFiber$test={memoizedProps:{sectionKey:'custom:tasks'},return:null};});
 await page.evaluate(script);await flush(page);await page.locator('[data-provider-hub-quick-launcher]').click();
 assert.equal(await page.evaluate(()=>window.__providerHubQuickComposer.recentThreadTargets().length),0);
 await page.evaluate(()=>window.__providerHubQuickComposer.uninstall());
 assert.deepEqual(await page.evaluate(script),{installed:true});await flush(page);
 // No native search binding => never attach by the unrelated English search label.
 await page.evaluate(()=>{window.__providerHubQuickComposer.uninstall();delete document.querySelector('#search').__reactFiber$test;});
 await page.evaluate(script);await flush(page);assert.equal(await page.locator('[data-provider-hub-quick-launcher]').count(),0);
 await browser.close();process.stdout.write('quick composer browser checks passed');
})().catch(error=>{process.stderr.write(String(error.stack||error));process.exitCode=1;});
"""


class QuickComposerBrowserTests(unittest.TestCase):
    def test_actual_sidebar_hooks_drafts_native_submissions_and_lifecycle(self):
        node = shutil.which("node")
        if not node or not os.environ.get("NODE_PATH"):
            self.skipTest("Bundled Node and Playwright are required")
        payload = {"script": quick_composer_script()}
        if os.environ.get("PROVIDER_HUB_QUICK_SCREENSHOT"):
            payload["screenshot"] = os.environ["PROVIDER_HUB_QUICK_SCREENSHOT"]
        result = subprocess.run([node, "-e", BROWSER], input=json.dumps(payload), text=True,
                                capture_output=True, timeout=45)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
