"""Recent-thread floating window using the Desktop's authored sidebar identities."""
from __future__ import annotations


def quick_composer_script() -> str:
    return _SCRIPT


_SCRIPT = r"""
(() => {
  if (window !== window.top || !/^app:\/\/-\//.test(String(location.href))) return {skipped:'origin'};
  if (window.__providerHubQuickComposer) return {skipped:'installed'};
  const ROW='data-app-action-sidebar-thread-row', ID='data-app-action-sidebar-thread-id';
  const HOST='data-app-action-sidebar-thread-host-id', KIND='data-app-action-sidebar-thread-kind';
  const TITLE='data-app-action-sidebar-thread-title', SECTION='data-app-action-sidebar-section';
  const UUID=/^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/i;
  const SPINNER='[role="status"][class~="text-text/70"] > [class~="motion-safe:animate-spin"] > svg';
  const MARK='data-provider-hub-sidebar', ACCENT='--provider-hub-sidebar-accent';
  const HEX=/^#[0-9a-f]{6}$/i;
  const EDIT=/^(?:[acvxyz]|Enter|Backspace|Delete|Arrow(?:Left|Right|Up|Down)|Home|End)$/i;
  const key=t=>JSON.stringify([t.kind,t.hostId,t.threadId]);
  const state={open:false,dragged:false,targets:[],selected:null,shown:undefined,drafts:new Map(),previews:new Map(),statuses:new Map(),pending:new Set(),button:null,host:null,root:null,frame:0,reason:'waiting',disposed:false};
  const fibres=element=>{
    const name=Object.keys(element).find(k=>k.startsWith('__reactFiber$'));
    let node=name?element[name]:null; const result=[];
    for(let depth=0;node&&depth<24;depth++,node=node.return) result.push(node.memoizedProps);
    return result.filter(p=>p&&typeof p==='object');
  };
  function group() {
    const sidebar=document.getElementById('app-shell-sidebar');
    if(!sidebar) return null;
    const groups=Array.from(sidebar.querySelectorAll('['+SECTION+']')).slice(0,128);
    return groups.find(el=>fibres(el).some(p=>p.sectionKey==='chats')) || null;
  }
  function identity(row) {
    const raw=row.getAttribute(ID), hostId=row.getAttribute(HOST), kind=row.getAttribute(KIND);
    if(!raw||!hostId||!kind) return null;
    let threadId=raw.startsWith('local:')?raw.slice(6):raw;
    if(threadId.startsWith('client-new-thread:')) {
      threadId=null;
      for(const props of fibres(row)) {
        if(props.dataAttributes?.[ID]===raw && UUID.test(props.conversationId||'')) {threadId=props.conversationId;break;}
      }
      if(!threadId) return null;
    }
    let active=false,activeAccent=null;
    try{
      for(const glyph of row.querySelectorAll(SPINNER)){
        if(glyph.closest('['+ROW+']')!==row)continue;
        active=true;
        if(glyph.getAttribute(MARK)==='1'){
          const paint=glyph.style.getPropertyValue(ACCENT).trim();
          if(HEX.test(paint))activeAccent=paint.toUpperCase();
        }
        break;
      }
    }catch(error){}
    return {threadId,hostId,kind,title:row.getAttribute(TITLE)||'Untitled chat',supported:kind==='local'&&hostId==='local'&&UUID.test(threadId),active,activeAccent};
  }
  function collect() {
    const section=group(); if(!section) {state.reason='recents-unavailable';return [];}
    const rows=Array.from(section.querySelectorAll('['+ROW+']')).filter(row=>row.closest('['+SECTION+']')===section).slice(0,10);
    const targets=[],seen=new Set();
    for(const row of rows) {const t=identity(row);if(t&&!seen.has(key(t))){seen.add(key(t));targets.push(t);}}
    state.reason='ready';return targets;
  }
  function actionBar() {
    const sidebar=document.getElementById('app-shell-sidebar');if(!sidebar)return null;
    // The masthead's icon button invokes the authored search action. Never
    // select another search button by translated labels or transcript text.
    for(const button of Array.from(sidebar.querySelectorAll('button')).slice(0,256)) {
      if(button.closest('['+SECTION+']'))continue;
      const verified=fibres(button).some(p=>p.uniform===true&&typeof p.onClick==='function'
        && Function.prototype.toString.call(p.onClick).includes('chat-search-command-menu'));
      if(!verified)continue;
      for(let bar=button.parentElement,depth=0;bar&&bar!==sidebar&&depth<4;bar=bar.parentElement,depth++) {
        if(!bar.querySelector('['+SECTION+']')&&bar.querySelectorAll('button').length>=2)return bar;
      }
      return button.parentElement;
    }
    return null;
  }
  const icon='<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="M5 4h14a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2h-9l-5 3v-3a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2Z"/><path d="M7 8h10M7 12h7"/></svg>';
  // Same 25% arc over a faint track as the sidebar's running indicator.
  const spinGlyph='<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" aria-hidden="true"><circle cx="12" cy="12" r="8" opacity=".25"/><path d="M12 4a8 8 0 0 1 8 8" stroke-linecap="round"/></svg>';
  let list,input,send,status,dialog,close,heading;
  function create() {
    const host=document.createElement('div');host.setAttribute('data-provider-hub-quick-composer','');
    // A host the page discarded is rebuilt where, and as open as, it was.
    host.style.cssText=state.host?state.host.style.cssText:'position:fixed;z-index:2147483000;left:16px;top:72px;display:none;color-scheme:inherit';
    const root=host.attachShadow({mode:'open'});
    root.innerHTML=`<style>
      :host{font-family:var(--font-sans,system-ui,sans-serif);color:var(--color-text,#eee)}
      *{box-sizing:border-box}button,input{font:inherit;color:inherit}button{cursor:pointer}
      .glass{width:min(440px,calc(100vw - 32px));max-height:calc(100vh - 96px);display:flex;flex-direction:column;border:1px solid color-mix(in srgb,currentColor 15%,transparent);border-radius:22px;background:color-mix(in srgb,var(--color-background,var(--color-surface,#202022)) 83%,transparent);backdrop-filter:blur(28px) saturate(1.3);box-shadow:0 18px 70px #0005;overflow:hidden}
      .heading{display:flex;align-items:center;padding:14px 16px 8px;font-size:13px;font-weight:600;gap:12px;cursor:move;touch-action:none;user-select:none}.heading span{flex:1}.close{border:0;background:none;border-radius:8px;font-size:18px;padding:2px 7px;cursor:pointer}
      .list{overflow:auto;padding:4px 8px;min-height:0}.item{display:block;width:100%;text-align:left;padding:8px 10px;border:0;background:none;border-radius:10px;margin:2px 0}.item:hover,.item:focus-visible{background:color-mix(in srgb,currentColor 7%,transparent)}.item[aria-selected=true]{background:color-mix(in srgb,var(--color-chart-blue,#705aff) 16%,transparent)}
      .item{position:relative}.item.live .title,.item.live .preview{padding-right:24px}.spin{position:absolute;right:10px;top:50%;width:14px;height:14px;margin-top:-7px;color:var(--color-text-secondary,currentColor);animation:ph-qc-spin 2s steps(60,end) infinite}.spin svg{display:block}@keyframes ph-qc-spin{to{transform:rotate(360deg)}}@media(prefers-reduced-motion:reduce){.spin{animation:none}}
      .title,.preview{display:block;white-space:nowrap;text-overflow:ellipsis;overflow:hidden}.title{font-size:13px;line-height:19px}.preview{font-size:12px;line-height:18px;opacity:.6}.item[aria-disabled=true]{opacity:.45;cursor:default}
      .composer{display:flex;align-items:center;gap:8px;margin:10px 12px 4px;border-radius:24px;padding:6px 6px 6px 16px;border:1px solid color-mix(in srgb,currentColor 12%,transparent);background:var(--color-background-secondary, color-mix(in srgb,currentColor 5%,transparent))}
      input{width:0;flex:1;min-width:0;border:0;outline:none;background:none;font-size:14px;line-height:24px}input::placeholder{color:inherit;opacity:.5}.send{width:32px;height:32px;border:0;border-radius:50%;background:var(--color-text,#eee);color:var(--color-background,#222);font-size:20px}.send:disabled{opacity:.3;cursor:default}
      .status{font-size:11px;line-height:16px;min-height:24px;padding:0 18px 8px;opacity:.7}.empty{padding:22px 12px;font-size:13px;opacity:.6}button:focus-visible{outline:2px solid var(--color-chart-blue,#705aff);outline-offset:1px}
      @media(prefers-color-scheme:light){:host{color:var(--color-text,#252525)}.glass{background:color-mix(in srgb,var(--color-background,var(--color-surface,#f7f7f8)) 85%,transparent)}.send{background:var(--color-text,#222);color:var(--color-background,#fff)}}
      @media(prefers-reduced-transparency:reduce){.glass{background:var(--color-background,#202022);backdrop-filter:none}}
    </style><section class="glass" role="dialog" aria-label="Recent chats quick composer"><div class="heading"><span>Recent chats</span><button class="close" aria-label="Close quick composer">×</button></div><div class="list" role="listbox" aria-label="Recent chats"></div><div class="composer"><input aria-label="Message selected chat" placeholder="Message selected chat…" maxlength="65536"><button class="send" aria-label="Send message">↑</button></div><div class="status" role="status" aria-live="polite"></div></section>`;
    state.host=host;state.root=root;state.shown=undefined;document.body.appendChild(host);
    list=root.querySelector('.list');input=root.querySelector('input');send=root.querySelector('.send');status=root.querySelector('.status');dialog=root.querySelector('.glass');close=root.querySelector('.close');heading=root.querySelector('.heading');
    close.addEventListener('click',()=>setOpen(false));send.addEventListener('click',submit);
    heading.addEventListener('pointerdown',startDrag);heading.addEventListener('pointermove',dragMove);
    for(const type of ['pointerup','pointercancel','lostpointercapture'])heading.addEventListener(type,dragEnd);
    input.addEventListener('input',()=>{if(state.selected)state.drafts.set(state.selected,input.value);updateComposer();});
    input.addEventListener('keydown',e=>{if(e.key==='Enter'&&!e.isComposing){e.preventDefault();submit();}});
    // Keys typed here belong to this window. Document-level handlers only see
    // the host as the target, so plain and editing keystrokes, clipboard
    // events and Escape stop at the shadow root; other Command/Control
    // shortcuts still reach the app.
    const isolate=e=>{if(!(e.metaKey||e.ctrlKey)||EDIT.test(e.key))e.stopPropagation();};
    for(const type of ['keyup','keypress'])root.addEventListener(type,isolate);
    for(const type of ['copy','cut','paste'])root.addEventListener(type,e=>e.stopPropagation());
    root.addEventListener('keydown',e=>{
      isolate(e);
      if(e.key==='Escape'&&!e.isComposing){e.preventDefault();setOpen(false);return;}
      if(e.key==='Tab'){
        const controls=Array.from(root.querySelectorAll('button:not(:disabled),input:not(:disabled)')).filter(el=>!el.hidden);
        const pos=controls.indexOf(root.activeElement);
        if(e.shiftKey&&pos===0){e.preventDefault();controls.at(-1)?.focus();}
        else if(!e.shiftKey&&pos===controls.length-1){e.preventDefault();controls[0]?.focus();}
      }
      if((e.key==='ArrowDown'||e.key==='ArrowUp')&&root.activeElement?.classList.contains('item')){
        e.preventDefault();const items=Array.from(list.querySelectorAll('.item'));const n=items.indexOf(root.activeElement);
        items[(n+(e.key==='ArrowDown'?1:-1)+items.length)%items.length]?.focus();
      }
    });
  }
  function attach() {
    if(state.disposed||!document.body)return;
    if(!state.host?.isConnected){create();if(state.open)position();}
    const bar=actionBar();if(!bar){state.reason='masthead-unavailable';return;}
    if(state.button?.parentElement===bar)return;
    state.button?.remove();
    const button=document.createElement('button');button.setAttribute('data-provider-hub-quick-launcher','');
    button.setAttribute('aria-label','Recent chats quick composer');button.setAttribute('title','Recent chats quick composer');button.setAttribute('aria-haspopup','dialog');button.setAttribute('aria-expanded',String(state.open));
    button.style.cssText='display:flex;align-items:center;justify-content:center;width:28px;height:28px;flex-shrink:0;border:0;border-radius:8px;background:transparent;color:var(--color-text-secondary,inherit);cursor:pointer;-webkit-app-region:no-drag';
    button.innerHTML=icon;button.addEventListener('click',()=>setOpen(!state.open));bar.appendChild(button);state.button=button;
  }
  function updateComposer() {
    const t=state.targets.find(t=>key(t)===state.selected);
    input.disabled=!t?.supported;send.disabled=!t?.supported||!input.value.trim()||state.pending.has(state.selected);
    input.placeholder=t?.supported?'Message '+t.title+'…':'Select a local Codex chat';
    status.textContent=state.statuses.get(state.selected)||(!t?.supported?'Remote and ChatGPT chats are unavailable here.':'');
  }
  const rows=new Map();
  const setAttr=(el,name,value)=>{if(el.getAttribute(name)!==value)el.setAttribute(name,value);};
  const setText=(el,value)=>{if(el.textContent!==value)el.textContent=value;};
  function choose(k){if(!state.targets.find(t=>key(t)===k)?.supported)return;state.selected=k;render();input.focus();}
  function item(k){
    let row=rows.get(k);if(row)return row;
    row=document.createElement('button');row.className='item';row.setAttribute('data-key',k);row.setAttribute('role','option');
    const title=document.createElement('span');title.className='title';const preview=document.createElement('span');preview.className='preview';
    row.append(title,preview);row.addEventListener('click',()=>choose(k));rows.set(k,row);return row;
  }
  function paint(row,t,k){
    setAttr(row,'aria-selected',String(k===state.selected));setAttr(row,'aria-disabled',String(!t.supported));
    setText(row.querySelector('.title'),t.title);
    setText(row.querySelector('.preview'),t.supported?(state.previews.get(k)||'No response preview available'):'Unavailable in this popover');
    let spin=row.querySelector('.spin');row.classList.toggle('live',t.active);
    if(t.active&&!spin){spin=document.createElement('span');spin.className='spin';spin.setAttribute('aria-hidden','true');spin.innerHTML=spinGlyph;row.prepend(spin);}
    else if(!t.active&&spin){spin.remove();spin=null;}
    if(spin&&spin.__accent!==t.activeAccent){spin.__accent=t.activeAccent;spin.style.color=t.activeAccent||'';}
  }
  function render() {
    if(!list)return;
    if(!state.targets.some(t=>key(t)===state.selected)){const first=state.targets.find(t=>t.supported);state.selected=first?key(first):null;}
    // Rows are keyed and patched in place: streaming updates never replace
    // the row under a press, and unrelated sidebar churn writes nothing.
    const keys=state.targets.map(key),focused=state.root.activeElement;
    for(const [k,row] of rows)if(!keys.includes(k)){row.remove();rows.delete(k);}
    const wanted=keys.map(item);state.targets.forEach((t,i)=>paint(wanted[i],t,keys[i]));
    const empty=list.querySelector('.empty');
    if(wanted.length)empty?.remove();
    else if(!empty){const note=document.createElement('div');note.className='empty';note.textContent='No recent chats are available in the sidebar.';list.appendChild(note);}
    const order=Array.from(list.querySelectorAll('.item'));
    if(order.length!==wanted.length||order.some((row,i)=>row!==wanted[i])){list.append(...wanted);if(focused?.isConnected&&state.root.activeElement!==focused)focused.focus();}
    if(state.shown!==state.selected){state.shown=state.selected;input.value=state.drafts.get(state.selected)||'';}
    updateComposer();
  }
  let drag=null;
  // Pointer capture keeps a fast drag attached to the heading after the
  // pointer leaves it; a press without movement leaves the anchor alone.
  function startDrag(e){if(!state.host||e.button!==0||e.target.closest('.close'))return;e.preventDefault();const box=state.host.getBoundingClientRect();drag={id:e.pointerId,cx:e.clientX,cy:e.clientY,x:box.left,y:box.top,moved:false};try{heading.setPointerCapture(e.pointerId);}catch(error){}}
  function dragMove(e){if(!drag||e.pointerId!==drag.id)return;const dx=e.clientX-drag.cx,dy=e.clientY-drag.cy;if(!drag.moved&&Math.hypot(dx,dy)<3)return;drag.moved=true;state.dragged=true;place(drag.x+dx,drag.y+dy);}
  function dragEnd(e){if(drag&&e.pointerId===drag.id)drag=null;}
  function sync() {
    if(state.disposed)return;attach();
    if(!state.open)return;
    state.targets=collect();render();
  }
  // The window is a renderer-side DOM surface, so position:fixed is clipped
  // by Codex's BrowserWindow content area. We can only relax how close the
  // user can drag it to the visible edge; we cannot make it leave the app.
  // Keep 80px on the right and 160px on the bottom so the heading stays
  // grabbable and the launcher remains anchorable on next open.
  function place(x,y){
    const width=state.host.offsetWidth||440;x=Math.max(8,Math.min(x,innerWidth-80));y=Math.max(8,Math.min(y,innerHeight-160));
    state.host.style.left=Math.round(x)+'px';state.host.style.top=Math.round(y)+'px';dialog.style.maxHeight=Math.max(120,innerHeight-y-8)+'px';
  }
  // Anchor below the launcher only until the user moves the window; after
  // that it reopens where it was left. The bounds are Codex's BrowserWindow
  // content rect, not our choice: position:fixed in the renderer cannot
  // composite outside Codex's window, so we keep the user's last x/y rather
  // than re-snapping to the launcher.
  function position(){
    if(!state.host)return;
    const box=state.button?.isConnected?state.button.getBoundingClientRect():null;
    if(!state.dragged&&box&&(box.width||box.height))return place(box.left,box.bottom+10);
    const x=parseFloat(state.host.style.left),y=parseFloat(state.host.style.top),own=state.host.getBoundingClientRect();
    place(Number.isFinite(x)?x:own.left,Number.isFinite(y)?y:own.top);
  }
  function setOpen(value){
    state.open=Boolean(value);attach();if(!state.host)return;
    state.host.style.display=state.open?'block':'none';state.button?.setAttribute('aria-expanded',String(state.open));
    if(state.open){position();sync();input.focus();}else{drag=null;state.button?.focus();}
  }
  async function submit(){
    state.targets=collect();const t=state.targets.find(t=>key(t)===state.selected);
    if(!t?.supported||state.pending.has(state.selected))return;
    const k=key(t),prompt=input.value;if(!prompt.trim())return;state.drafts.set(k,prompt);
    const adapter=window.__providerHubDesktopActions;
    if(typeof adapter?.send!=='function'){state.statuses.set(k,'Sending is unavailable in this Desktop version. Draft kept.');updateComposer();return;}
    state.pending.add(k);state.statuses.set(k,'Sending…');updateComposer();
    try{
      const result=await adapter.send({threadId:t.threadId,hostId:t.hostId,kind:t.kind,prompt});
      if(result?.sent!==true||result.threadId!==t.threadId)throw new Error('unverified-result');
      if(state.drafts.get(k)===prompt)state.drafts.set(k,'');
      state.statuses.set(k,'Sent. Desktop handles steering or starting the next turn.');
    }catch(error){
      // An unverified Desktop build is the most common cause and is otherwise
      // indistinguishable from a generic native failure. Surface the native
      // adapter's diagnostics so the user can tell the difference and tell
      // us which bundle they are on. Errors reading diagnostics stay silent:
      // the user still sees the generic reason.
      let reason='Send failed. Your draft is kept.';
      try{
        const adapterDiagnostics=adapter.diagnostics?.();
        const observedBundle=adapterDiagnostics?.observedBundle;
        const approvedBundles=adapterDiagnostics?.approvedBundles;
        if(error?.code==='app-version'&&observedBundle){
          const known=Array.isArray(approvedBundles)?approvedBundles.map(b=>b.hash).join(', '):'';
          reason=`Send failed: this Desktop build (${observedBundle}) is not in the verified list${known?' (verified: '+known+')':''}. Update the helper to send again.`;
        }else if(error?.code==='exports'){
          reason='Send failed: Desktop\'s native send binding is no longer exported. Update the helper to send again.';
        }else if(error?.code==='native-binding'){
          reason='Send failed: Desktop\'s native send binding did not match the expected shape. Update the helper to send again.';
        }else if(error?.code==='native-result'){
          reason='Send failed: Desktop did not confirm the sent thread. Your draft is kept.';
        }else if(error?.code==='native'){
          reason='Send failed: Desktop rejected the send. Your draft is kept.';
        }
      }catch(diagnosticError){}
      state.statuses.set(k,reason);
    }
    finally{state.pending.delete(k);if(state.selected===k)input.value=state.drafts.get(k)||'';updateComposer();}
  }
  function previews(records){
    if(!state.open||!Array.isArray(records))return {applied:0};
    const wanted=new Set(collect().filter(t=>t.supported).map(key));let applied=0;
    for(const record of records.slice(0,10)){
      if(!record||typeof record!=='object'||!wanted.has(key(record)))continue;
      state.previews.set(key(record),typeof record.preview==='string'?record.preview.trim().replace(/\s+/g,' ').slice(0,512):null);applied++;
    }
    for(const k of state.previews.keys())if(!wanted.has(k))state.previews.delete(k);
    render();return {applied};
  }
  const schedule=()=>{if(!state.frame&&!state.disposed)state.frame=requestAnimationFrame(()=>{state.frame=0;sync();});};
  const observer=new MutationObserver(mutations=>{
    if(mutations.every(m=>m.target===state.host||m.target===state.button||state.host?.contains(m.target)||state.button?.contains(m.target)))return;
    schedule();
  });
  observer.observe(document.documentElement,{childList:true,subtree:true,attributes:true,attributeFilter:[ID,HOST,KIND,TITLE,MARK,'data-app-action-sidebar-section-heading','data-app-action-sidebar-section-collapsed']});
  const resize=()=>{if(state.open)position();};
  window.addEventListener('resize',resize);
  window.__providerHubQuickComposer={
    recentThreadTargets:()=>state.open?collect():[],setThreadPreviews:previews,isOpen:()=>state.open,
    check:()=>{const live=state.open?collect():[];return {installed:true,open:state.open,button:Boolean(state.button?.isConnected),reason:state.reason,targets:live.length,active:live.filter(t=>t.active).length,dragged:state.dragged};},
    uninstall:()=>{state.disposed=true;drag=null;observer.disconnect();if(state.frame)cancelAnimationFrame(state.frame);window.removeEventListener('resize',resize);state.button?.remove();state.host?.remove();delete window.__providerHubQuickComposer;}
  };
  attach();return {installed:true};
})()
"""
