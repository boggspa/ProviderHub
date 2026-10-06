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
 const launcher=page.locator('[data-provider-hub-quick-launcher]');
 assert.equal(await launcher.evaluate(el=>el.querySelector('svg').getAttribute('width')),'14');
 assert.equal(await launcher.evaluate(el=>el.querySelector('svg').getAttribute('height')),'14');
 await page.evaluate(()=>document.documentElement.style.setProperty('--color-text-secondary','rgb(150, 151, 152)'));
 assert.equal(await launcher.evaluate(el=>getComputedStyle(el).color),'rgb(150, 151, 152)');
 await page.evaluate(()=>document.documentElement.style.removeProperty('--color-text-secondary'));
 // Running rows carry the sidebar's own spinner shape: accented, bare, an
 // invalid accent, and one nested under another row element.
 await page.evaluate(()=>{const rows=Array.from(document.querySelectorAll('#recent [data-app-action-sidebar-thread-row]'));const spin=inner=>`<span role="status" class="text-text/70"><span class="motion-safe:animate-spin">${inner}</span></span>`;rows[0].insertAdjacentHTML('beforeend',spin('<svg data-provider-hub-sidebar="1" style="--provider-hub-sidebar-accent:#D44404"></svg>'));rows[2].insertAdjacentHTML('beforeend',spin('<svg></svg>'));rows[3].insertAdjacentHTML('beforeend',spin('<svg data-provider-hub-sidebar="1" style="--provider-hub-sidebar-accent:not-a-colour"></svg>'));rows[9].insertAdjacentHTML('beforeend','<div data-app-action-sidebar-thread-row>'+spin('<svg data-provider-hub-sidebar="1" style="--provider-hub-sidebar-accent:#0073E6"></svg>')+'</div>');});
 assert.equal(await page.evaluate(()=>window.__providerHubQuickComposer.recentThreadTargets().length),0);
 await page.locator('[data-provider-hub-quick-launcher]').click();
 let targets=await page.evaluate(()=>window.__providerHubQuickComposer.recentThreadTargets());
 assert.equal(targets.length,10);assert.equal(targets[0].threadId,id(1));assert.equal(targets[1].supported,false);assert.equal(targets[9].threadId,id(10));
 assert.equal(await page.locator(shell+' .item').count(),10);
 assert.equal(targets[0].active,true);assert.equal(targets[0].activeAccent,'#D44404');
 assert.equal(targets[2].active,true);assert.equal(targets[2].activeAccent,null);
 assert.equal(targets[3].active,true);assert.equal(targets[3].activeAccent,null);
 assert.equal(targets[9].active,false);
 assert.equal(await page.locator(shell+' .item.live').count(),3);
 assert.equal(await page.locator(shell+' .item.live .spin').count(),3);
 assert.equal(await page.locator(shell+' .item.live .spin').first().evaluate(el=>getComputedStyle(el).color),'rgb(212, 68, 4)');
 assert.equal(await page.locator(shell+' .item.live .spin svg').count(),3);
 // Outside interaction no longer dismisses the floating window.
 await page.mouse.click(900,850);await flush(page);
 assert.equal(await page.evaluate(()=>window.__providerHubQuickComposer.isOpen()),true);
 // Escape elsewhere in the app is neither consumed nor a dismissal.
 await page.evaluate(()=>{window.__escapes=[];window.addEventListener('keydown',e=>{if(e.key==='Escape')window.__escapes.push(e.defaultPrevented);});});
 await page.locator('#bell').focus();await page.keyboard.press('Escape');
 assert.equal(await page.evaluate(()=>window.__providerHubQuickComposer.isOpen()),true);
 assert.deepEqual(await page.evaluate(()=>window.__escapes),[false]);
 // The heading drags the window under pointer capture (the pointer leaves the
 // heading mid-drag); later syncs must not re-anchor it.
 const place=()=>page.locator(shell).evaluate(el=>({left:parseFloat(el.style.left),top:parseFloat(el.style.top)}));
 const beforeDrag=await place();assert.equal(await page.evaluate(()=>window.__providerHubQuickComposer.check().dragged),false);
 const grip=await page.locator(shell+' .heading span').boundingBox();
 await page.mouse.move(grip.x+20,grip.y+grip.height/2);await page.mouse.down();
 await page.mouse.move(grip.x+140,grip.y+grip.height/2+80,{steps:4});await page.mouse.up();
 const afterDrag=await place();
 assert(Math.abs(afterDrag.left-beforeDrag.left-120)<2,JSON.stringify([beforeDrag,afterDrag]));assert(Math.abs(afterDrag.top-beforeDrag.top-80)<2,JSON.stringify([beforeDrag,afterDrag]));
 assert.equal(await page.evaluate(()=>window.__providerHubQuickComposer.check().dragged),true);
 await page.evaluate(()=>document.getElementById('recent').setAttribute('data-app-action-sidebar-section-collapsed','false'));await flush(page);
 assert.deepEqual(await place(),afterDrag);
 // A finished spinner clears its indicator without touching drafts.
 await page.evaluate(()=>document.querySelector('[data-app-action-sidebar-thread-id="local:00000000-0000-7000-8000-000000000001"] [role="status"]').remove());await flush(page);
 assert.equal(await page.locator(shell+' .item.live').count(),2);
 // The close button and launcher toggle still dismiss and reopen, and the
 // window reopens where it was left.
 await page.locator(shell+' .close').click();
 assert.equal(await page.evaluate(()=>window.__providerHubQuickComposer.isOpen()),false);
 await page.locator('[data-provider-hub-quick-launcher]').click();
 assert.equal(await page.evaluate(()=>window.__providerHubQuickComposer.isOpen()),true);
 assert.deepEqual(await place(),afterDrag);
 // A narrower app window keeps the whole floating window reachable.
 await page.setViewportSize({width:700,height:600});await flush(page);
 const squeezed=await place();assert(squeezed.left<=700-440-8&&squeezed.top<=600-160,JSON.stringify(squeezed));
 await page.setViewportSize({width:1000,height:900});await flush(page);
 // Unrelated page churn and preview updates patch rows in place.
 await page.locator(shell+' .item').first().evaluate(el=>{el.__kept=true;});
 await page.evaluate(()=>document.body.appendChild(document.createElement('div')));await flush(page);
 assert.equal(await page.locator(shell+' .item').first().evaluate(el=>el.__kept===true),true);
 await page.evaluate(t=>window.__providerHubQuickComposer.setThreadPreviews([{...t,preview:'latest\nassistant response'},{threadId:'wrong',hostId:'local',kind:'local',preview:'intruder'}]),targets[0]);
 assert.equal((await page.locator(shell+' .preview').first().textContent()).trim(),'latest assistant response');
 assert.equal(await page.locator(shell+' .item').first().evaluate(el=>el.__kept===true),true);
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
 // Plain keystrokes stay in the window; other Command shortcuts reach the app.
 await page.evaluate(()=>{window.__keys=[];window.addEventListener('keydown',e=>window.__keys.push(e.key));});
 await capsule.press('q');await capsule.press('Meta+a');await capsule.press('Meta+k');
 assert.deepEqual(await page.evaluate(()=>window.__keys.filter(k=>k!=='Meta')),['k']);
 await capsule.press('Escape');assert.equal(await page.evaluate(()=>window.__providerHubQuickComposer.isOpen()),false);
 assert.deepEqual(await page.evaluate(()=>window.__escapes),[false]);
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
})().catch(error=>{process.stderr.write(String(error.stack||error),()=>process.exit(1));});
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
