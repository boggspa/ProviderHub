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
 const squeezed=await place();assert(squeezed.left<=700-80&&squeezed.top<=600-160,JSON.stringify(squeezed));
 // The relaxed clamp lets the window reach within ~80px of the right edge
 // (so the heading stays grabbable on a near-fullscreen Codex window) but
 // not be lost off-screen. Drag the heading off the right and verify.
 await page.setViewportSize({width:1000,height:900});await flush(page);
 const grip2=await page.locator(shell+' .heading span').boundingBox();
 await page.mouse.move(grip2.x+20,grip2.y+grip2.height/2);await page.mouse.down();
 await page.mouse.move(960,grip2.y+grip2.height/2+80,{steps:4});await page.mouse.up();
 await flush(page);
 const afterRightDrag=await place();
 assert(afterRightDrag.left<=1000-80,JSON.stringify(afterRightDrag));
 // A window left off-screen is pulled back inside the viewport when it next
 // opens. Its close button is out of reach then, as it would be for a user,
 // so the launcher toggle closes it.
 const beforeLost=await place();
 await page.locator(shell).evaluate(el=>el.style.left='-10000px');
 await page.locator('[data-provider-hub-quick-launcher]').click();await flush(page);
 assert.equal(await page.evaluate(()=>window.__providerHubQuickComposer.isOpen()),false);
 await page.locator('[data-provider-hub-quick-launcher]').click();await flush(page);
 const afterLost=await place();
 assert.deepEqual(afterLost,{left:8,top:beforeLost.top},JSON.stringify({beforeLost,afterLost}));
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
 // Every adapter code is named. The scope code is the one a wrong export
 // pin produced in the field; the report keeps the code and the adapter's
 // diagnostics for the worker's log, never the draft or the error text.
 await page.evaluate(()=>window.__providerHubDesktopActions={send:async()=>{throw Object.assign(Error('secret scope text'),{code:'scope'});},diagnostics:()=>({observedBundle:'app-initial-69cd8dbddec5.js',moduleError:'DesktopActionError',scopeError:'scope',scopeSearch:{fibers:1234,truncated:false},approvedBundles:[]})});
 await page.locator(shell+' .send').click();assert.equal(await capsule.inputValue(),'draft one');
 const scopeStatus=await page.locator(shell+' .status').textContent();
 assert.ok(scopeStatus.includes('the live Desktop app scope was not found'),scopeStatus);assert.doesNotMatch(scopeStatus,/secret scope/);
 const selected=(await page.evaluate(()=>window.__providerHubQuickComposer.recentThreadTargets()))[0].threadId;
 const report=await page.evaluate(()=>window.__providerHubQuickComposer.takeSendReport());
 assert.deepEqual({...report,at:typeof report.at},{outcome:'failed',code:'scope',at:'number',threadId:selected,observedBundle:'app-initial-69cd8dbddec5.js',moduleError:'DesktopActionError',scopeError:'scope',scopeSearch:{fibers:1234,truncated:false}});
 assert.doesNotMatch(JSON.stringify(report),/draft one|secret/);
 assert.equal(await page.evaluate(()=>window.__providerHubQuickComposer.takeSendReport()),null);
 await page.evaluate(()=>window.__providerHubDesktopActions={send:async()=>{throw Object.assign(Error('x'),{code:'in-flight'});}});
 await page.locator(shell+' .send').click();assert.match(await page.locator(shell+' .status').textContent(),/already in progress/);
 await page.evaluate(()=>window.__providerHubDesktopActions={send:async()=>{throw Object.assign(Error('x'),{code:'something-new'});}});
 await page.locator(shell+' .send').click();assert.match(await page.locator(shell+' .status').textContent(),/^Send failed \(something-new\)\. Your draft is kept\.$/);
 await page.evaluate(()=>window.__providerHubDesktopActions={send:async()=>({sent:true,mode:'native',threadId:'wrong'})});
 await page.locator(shell+' .send').click();assert.match(await page.locator(shell+' .status').textContent(),/did not confirm the sent thread/);
 assert.equal((await page.evaluate(()=>window.__providerHubQuickComposer.takeSendReport())).code,'native-result');
 // An unverified Desktop build is named, so a refused send can be diagnosed.
 await page.evaluate(()=>window.__providerHubDesktopActions={send:async()=>{throw Object.assign(Error('unverified'),{code:'app-version'});},diagnostics:()=>({observedBundle:'app-initial-0123abcd.js',approvedBundles:[{hash:'app-initial-f9b16fbf8fc7.js'}]})});
 await page.locator(shell+' .send').click();assert.equal(await capsule.inputValue(),'draft one');
 const unverified=await page.locator(shell+' .status').textContent();
 assert.ok(unverified.includes('this Desktop build (app-initial-0123abcd.js) is not in the verified list (verified: app-initial-f9b16fbf8fc7.js)'),unverified);
 // Pending send must not clear another selected chat or a newer typed draft.
 await page.evaluate(()=>window.__providerHubDesktopActions={send:request=>new Promise(resolve=>{window.finishQuickSend=()=>resolve({sent:true,mode:'native',threadId:request.threadId});})});
 await page.locator(shell+' .send').click();await page.locator(shell+' .item').nth(2).click();await capsule.fill('newer three');
 await page.evaluate(()=>window.finishQuickSend());await flush(page);assert.equal(await capsule.inputValue(),'newer three');
 assert.deepEqual((({outcome,code})=>({outcome,code}))(await page.evaluate(()=>window.__providerHubQuickComposer.takeSendReport())),{outcome:'sent',code:null});
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
 // Window mode: the launcher asks the worker for the separate window instead
 // of opening the overlay, the worker's poll collects rows while that window
 // is open, and sends on the window's behalf share the overlay's outcomes.
 await page.evaluate(()=>{window.__providerHubQuickComposer?.uninstall?.();document.querySelector('#search').__reactFiber$test={memoizedProps:{uniform:true,onClick:function(){return 'chat-search-command-menu';}},return:null};document.getElementById('recent').__reactFiber$test={memoizedProps:{sectionKey:'chats'},return:null};window.__hostCalls=[];window.__providerHubHost=p=>window.__hostCalls.push(JSON.parse(p));});
 const windowScript=input.windowScript.replace('String(location.href)','"app://-/"');
 assert.deepEqual(await page.evaluate(windowScript),{installed:true});await flush(page);
 await page.locator('[data-provider-hub-quick-launcher]').click();await flush(page);
 assert.equal(await page.evaluate(()=>window.__providerHubQuickComposer.isOpen()),false);
 assert.deepEqual(await page.evaluate(()=>window.__hostCalls),[{type:'open-window'}]);
 assert.equal(await page.evaluate(()=>window.__providerHubQuickComposer.recentThreadTargets().length),0);
 let poll=await page.evaluate(()=>window.__providerHubQuickComposer.pollHost({windowOpen:false}));
 assert.equal(poll.windowRequest,true);assert.equal(poll.windowMode,true);assert.equal(poll.targets.length,0);assert.equal(poll.report,null);
 poll=await page.evaluate(()=>window.__providerHubQuickComposer.pollHost({windowOpen:true}));
 assert.equal(poll.windowRequest,false);assert.equal(poll.targets.length,9);assert.equal(poll.targets[0].threadId,id(3));assert.equal(poll.targets[0].title,'Chat 3');
 assert.equal(await page.evaluate(t=>window.__providerHubQuickComposer.setThreadPreviews([{...t,preview:'window preview'}]).applied,poll.targets[0]),1);
 // A missing adapter, an unknown target, a rejected send and a confirmed send.
 await page.evaluate(()=>{delete window.__providerHubDesktopActions;});
 let outcome=await page.evaluate(t=>window.__providerHubQuickComposer.sendFromHost({threadId:t,prompt:'hi'}),id(3));
 assert.deepEqual(outcome,{ok:false,reason:'Sending is unavailable in this Desktop version. Draft kept.'});
 outcome=await page.evaluate(()=>window.__providerHubQuickComposer.sendFromHost({threadId:'00000000-0000-7000-8000-000000000099',prompt:'hi'}));
 assert.equal(outcome.ok,false);assert.match(outcome.reason,/no longer among the first ten/);
 outcome=await page.evaluate(t=>window.__providerHubQuickComposer.sendFromHost({threadId:t,prompt:'   '}),id(3));
 assert.deepEqual(outcome,{ok:false,reason:'Type a message first.'});
 await page.evaluate(()=>window.__providerHubDesktopActions={send:async()=>{throw Object.assign(Error('secret'),{code:'native'});},diagnostics:()=>({observedBundle:'app-initial-69cd8dbddec5.js'})});
 outcome=await page.evaluate(t=>window.__providerHubQuickComposer.sendFromHost({threadId:t,prompt:'from the window'}),id(3));
 assert.deepEqual(outcome,{ok:false,reason:'Send failed: Desktop rejected the send. Your draft is kept.'});
 const hostReport=await page.evaluate(()=>window.__providerHubQuickComposer.pollHost({windowOpen:true}).report);
 assert.equal(hostReport.code,'native');assert.equal(hostReport.threadId,id(3));assert.doesNotMatch(JSON.stringify(hostReport),/from the window|secret/);
 await page.evaluate(()=>window.__providerHubDesktopActions={send:r=>new Promise(resolve=>{window.finishHostSend=()=>resolve({sent:true,mode:'native',threadId:r.threadId});})});
 const pendingSend=page.evaluate(t=>window.__providerHubQuickComposer.sendFromHost({threadId:t,prompt:'from the window'}),id(3));
 await flush(page);
 outcome=await page.evaluate(t=>window.__providerHubQuickComposer.sendFromHost({threadId:t,prompt:'again'}),id(3));
 assert.match(outcome.reason,/already in progress/);
 await page.evaluate(()=>window.finishHostSend());
 assert.deepEqual(await pendingSend,{ok:true,reason:'Sent. Desktop handles steering or starting the next turn.'});
 assert.equal((await page.evaluate(()=>window.__providerHubQuickComposer.pollHost({windowOpen:true}).report)).outcome,'sent');
 await page.evaluate(()=>window.__providerHubQuickComposer.uninstall());
 await browser.close();process.stdout.write('quick composer browser checks passed');
})().catch(error=>{process.stderr.write(String(error.stack||error),()=>process.exit(1));});
"""


class QuickComposerBrowserTests(unittest.TestCase):
    def test_actual_sidebar_hooks_drafts_native_submissions_and_lifecycle(self):
        node = shutil.which("node")
        if not node or not os.environ.get("NODE_PATH"):
            self.skipTest("Bundled Node and Playwright are required")
        payload = {"script": quick_composer_script(), "windowScript": quick_composer_script(window_mode=True)}
        if os.environ.get("PROVIDER_HUB_QUICK_SCREENSHOT"):
            payload["screenshot"] = os.environ["PROVIDER_HUB_QUICK_SCREENSHOT"]
        result = subprocess.run([node, "-e", BROWSER], input=json.dumps(payload), text=True,
                                capture_output=True, timeout=45)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
