"""Recent-thread popover using the Desktop's authored sidebar identities."""
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
  const key=t=>JSON.stringify([t.kind,t.hostId,t.threadId]);
  const state={open:false,targets:[],selected:null,drafts:new Map(),previews:new Map(),statuses:new Map(),pending:new Set(),button:null,host:null,root:null,frame:0,reason:'waiting',disposed:false};
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
    return {threadId,hostId,kind,title:row.getAttribute(TITLE)||'Untitled chat',supported:kind==='local'&&hostId==='local'&&UUID.test(threadId)};
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
  const icon='<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" aria-hidden="true"><path d="M5 4h14a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2h-9l-5 3v-3a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2Z"/><path d="M7 8h10M7 12h7"/></svg>';
  let list,input,send,status,dialog,close;
  function create() {
    const host=document.createElement('div');host.setAttribute('data-provider-hub-quick-composer','');
    host.style.cssText='position:fixed;z-index:2147483000;left:16px;top:72px;display:none;color-scheme:inherit';
    const root=host.attachShadow({mode:'open'});
    root.innerHTML=`<style>
      :host{font-family:var(--font-sans,system-ui,sans-serif);color:var(--color-text,#eee)}
      *{box-sizing:border-box}button,input{font:inherit;color:inherit}button{cursor:pointer}
      .glass{width:min(440px,calc(100vw - 32px));max-height:calc(100vh - 96px);display:flex;flex-direction:column;border:1px solid color-mix(in srgb,currentColor 15%,transparent);border-radius:22px;background:color-mix(in srgb,var(--color-background,var(--color-surface,#202022)) 83%,transparent);backdrop-filter:blur(28px) saturate(1.3);box-shadow:0 18px 70px #0005;overflow:hidden}
      .heading{display:flex;align-items:center;padding:14px 16px 8px;font-size:13px;font-weight:600;gap:12px}.heading span{flex:1}.close{border:0;background:none;border-radius:8px;font-size:18px;padding:2px 7px}
      .list{overflow:auto;padding:4px 8px;min-height:0}.item{display:block;width:100%;text-align:left;padding:8px 10px;border:0;background:none;border-radius:10px;margin:2px 0}.item:hover,.item:focus-visible{background:color-mix(in srgb,currentColor 7%,transparent)}.item[aria-selected=true]{background:color-mix(in srgb,var(--color-chart-blue,#705aff) 16%,transparent)}
      .title,.preview{display:block;white-space:nowrap;text-overflow:ellipsis;overflow:hidden}.title{font-size:13px;line-height:19px}.preview{font-size:12px;line-height:18px;opacity:.6}.item[aria-disabled=true]{opacity:.45;cursor:default}
      .composer{display:flex;align-items:center;gap:8px;margin:10px 12px 4px;border-radius:24px;padding:6px 6px 6px 16px;border:1px solid color-mix(in srgb,currentColor 12%,transparent);background:var(--color-background-secondary, color-mix(in srgb,currentColor 5%,transparent))}
      input{width:0;flex:1;min-width:0;border:0;outline:none;background:none;font-size:14px;line-height:24px}input::placeholder{color:inherit;opacity:.5}.send{width:32px;height:32px;border:0;border-radius:50%;background:var(--color-text,#eee);color:var(--color-background,#222);font-size:20px}.send:disabled{opacity:.3;cursor:default}
      .status{font-size:11px;line-height:16px;min-height:24px;padding:0 18px 8px;opacity:.7}.empty{padding:22px 12px;font-size:13px;opacity:.6}button:focus-visible{outline:2px solid var(--color-chart-blue,#705aff);outline-offset:1px}
      @media(prefers-color-scheme:light){:host{color:var(--color-text,#252525)}.glass{background:color-mix(in srgb,var(--color-background,var(--color-surface,#f7f7f8)) 85%,transparent)}.send{background:var(--color-text,#222);color:var(--color-background,#fff)}}
      @media(prefers-reduced-transparency:reduce){.glass{background:var(--color-background,#202022);backdrop-filter:none}}
    </style><section class="glass" role="dialog" aria-label="Recent chats quick composer"><div class="heading"><span>Recent chats</span><button class="close" aria-label="Close quick composer">×</button></div><div class="list" role="listbox" aria-label="Recent chats"></div><div class="composer"><input aria-label="Message selected chat" placeholder="Message selected chat…" maxlength="65536"><button class="send" aria-label="Send message">↑</button></div><div class="status" role="status" aria-live="polite"></div></section>`;
    state.host=host;state.root=root;document.body.appendChild(host);
    list=root.querySelector('.list');input=root.querySelector('input');send=root.querySelector('.send');status=root.querySelector('.status');dialog=root.querySelector('.glass');close=root.querySelector('.close');
    close.addEventListener('click',()=>setOpen(false));send.addEventListener('click',submit);
    input.addEventListener('input',()=>{if(state.selected)state.drafts.set(state.selected,input.value);updateComposer();});
    input.addEventListener('keydown',e=>{if(e.key==='Enter'&&!e.isComposing){e.preventDefault();submit();}});
    root.addEventListener('keydown',e=>{
      if(e.key==='Escape'){e.preventDefault();setOpen(false);return;}
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
    if(!state.host?.isConnected)create();
    const bar=actionBar();if(!bar){state.reason='masthead-unavailable';return;}
    if(state.button?.parentElement===bar)return;
    state.button?.remove();
    const button=document.createElement('button');button.setAttribute('data-provider-hub-quick-launcher','');
    button.setAttribute('aria-label','Recent chats quick composer');button.setAttribute('title','Recent chats quick composer');button.setAttribute('aria-haspopup','dialog');button.setAttribute('aria-expanded',String(state.open));
    button.style.cssText='display:flex;align-items:center;justify-content:center;width:28px;height:28px;flex-shrink:0;border:0;border-radius:8px;background:transparent;color:inherit;cursor:pointer;-webkit-app-region:no-drag';
    button.innerHTML=icon;button.addEventListener('click',()=>setOpen(!state.open));bar.appendChild(button);state.button=button;
  }
  function updateComposer() {
    const t=state.targets.find(t=>key(t)===state.selected);
    input.disabled=!t?.supported;send.disabled=!t?.supported||!input.value.trim()||state.pending.has(state.selected);
    input.placeholder=t?.supported?'Message '+t.title+'…':'Select a local Codex chat';
    status.textContent=state.statuses.get(state.selected)||(!t?.supported?'Remote and ChatGPT chats are unavailable here.':'');
  }
  function render() {
    if(!list)return;
    const selected=state.targets.find(t=>key(t)===state.selected);
    if(!selected)state.selected=state.targets.find(t=>t.supported)?key(state.targets.find(t=>t.supported)):null;
    const focused=state.root.activeElement?.getAttribute('data-key');
    list.replaceChildren();
    if(!state.targets.length){const empty=document.createElement('div');empty.className='empty';empty.textContent='No recent chats are available in the sidebar.';list.appendChild(empty);}
    for(const t of state.targets){
      const k=key(t),row=document.createElement('button');row.className='item';row.setAttribute('data-key',k);row.setAttribute('role','option');row.setAttribute('aria-selected',String(k===state.selected));row.setAttribute('aria-disabled',String(!t.supported));
      const title=document.createElement('span');title.className='title';title.textContent=t.title;
      const preview=document.createElement('span');preview.className='preview';preview.textContent=t.supported?(state.previews.get(k)||'No response preview available'):'Unavailable in this popover';
      row.append(title,preview);row.addEventListener('click',()=>{if(!t.supported)return;state.selected=k;render();input.focus();});list.appendChild(row);
      if(focused===k)row.focus();
    }
    input.value=state.drafts.get(state.selected)||'';updateComposer();
  }
  function sync() {
    if(state.disposed)return;attach();
    if(!state.open)return;
    state.targets=collect();render();position();
  }
  function position(){if(!state.button||!state.host)return;const box=state.button.getBoundingClientRect();state.host.style.left=Math.max(12,Math.min(box.left,innerWidth-452))+'px';state.host.style.top=Math.min(box.bottom+10,Math.max(12,innerHeight-300))+'px';}
  function setOpen(value){
    state.open=Boolean(value);attach();if(!state.host)return;
    state.host.style.display=state.open?'block':'none';state.button?.setAttribute('aria-expanded',String(state.open));
    if(state.open){sync();input.focus();}else state.button?.focus();
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
    }catch(error){state.statuses.set(k,'Send failed. Your draft is kept.');}
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
  observer.observe(document.documentElement,{childList:true,subtree:true,attributes:true,attributeFilter:[ID,HOST,KIND,TITLE,'data-app-action-sidebar-section-heading','data-app-action-sidebar-section-collapsed']});
  const escape=e=>{if(e.key==='Escape'&&state.open){e.preventDefault();setOpen(false);}};
  const outside=e=>{if(state.open&&!e.composedPath().includes(state.host)&&!e.composedPath().includes(state.button))setOpen(false);};
  document.addEventListener('keydown',escape);document.addEventListener('pointerdown',outside);window.addEventListener('resize',position);
  window.__providerHubQuickComposer={
    recentThreadTargets:()=>state.open?collect():[],setThreadPreviews:previews,isOpen:()=>state.open,
    check:()=>({installed:true,open:state.open,button:Boolean(state.button?.isConnected),reason:state.reason,targets:state.open?collect().length:0}),
    uninstall:()=>{state.disposed=true;observer.disconnect();if(state.frame)cancelAnimationFrame(state.frame);document.removeEventListener('keydown',escape);document.removeEventListener('pointerdown',outside);window.removeEventListener('resize',position);state.button?.remove();state.host?.remove();delete window.__providerHubQuickComposer;}
  };
  attach();return {installed:true};
})()
"""
