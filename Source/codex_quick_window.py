"""A worker-owned window for the recent-thread quick composer.

Desktop refuses ``window.open`` from its renderer, so the floating overlay can
never leave Codex's window. The private DevTools pipe can do what the page
cannot: ``Target.createTarget`` with ``newWindow`` opens a separate macOS
window (Desktop's in-app browser panel) whose page is ours. This module owns
that window: it fills it with Provider Hub's own document, positions it where
it was last left, relays the sidebar rows and previews the worker already
reads, and forwards each send to the main session's existing adapter.

The window page never touches Desktop's code. It reaches the worker only
through one CDP binding, and the worker evaluates only two things in it:
``setState`` and ``sendResult``. If the page navigates anywhere other than
``about:blank`` (the address bar is Desktop's), the worker closes the window at
once and ignores its binding, so no outside document can ask the worker to
send. Prompts travel through the worker in memory and never reach its log.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

_UUID = re.compile(r"^[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$")

WINDOW_BINDING = "__providerHubWindowHost"
HOST_BINDING = "__providerHubHost"
DEFAULT_BOUNDS = {"width": 500, "height": 640}
_PROMPT_LIMIT = 65536
_PAYLOAD_LIMIT = 80000


def quick_window_html() -> str:
    return _HTML


_SPIN = ('<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" '
         'aria-hidden="true"><circle cx="12" cy="12" r="8" opacity=".25"/><path d="M12 4a8 8 0 0 1 8 8" stroke-linecap="round"/></svg>')

_HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>Recent chats</title>
<style>
:root{color-scheme:light dark;--bg:#202022;--bg2:#2a2a2d;--text:#ececec;--muted:rgba(236,236,236,.6);--line:rgba(255,255,255,.12);--accent:#705aff;--send-bg:#ececec;--send-fg:#202022}
@media(prefers-color-scheme:light){:root{--bg:#f7f7f8;--bg2:#fff;--text:#222;--muted:rgba(34,34,34,.6);--line:rgba(0,0,0,.12);--send-bg:#222;--send-fg:#fff}}
html,body{height:100%;margin:0}body{font:14px/1.4 system-ui,-apple-system,sans-serif;background:var(--bg);color:var(--text);display:flex;flex-direction:column;overflow:hidden}
*{box-sizing:border-box}button,input{font:inherit;color:inherit}button{cursor:pointer}
.heading{display:flex;align-items:center;gap:8px;padding:12px 16px 6px;font-size:13px;font-weight:600;flex:none}.heading span{flex:1}
.list{flex:1;overflow:auto;padding:4px 8px;min-height:0}
.item{display:block;width:100%;text-align:left;padding:8px 10px;border:0;background:none;border-radius:10px;margin:2px 0;position:relative}
.item:hover,.item:focus-visible{background:color-mix(in srgb,currentColor 7%,transparent)}.item[aria-selected=true]{background:color-mix(in srgb,var(--accent) 16%,transparent)}
.item[aria-disabled=true]{opacity:.45;cursor:default}.item.live .title,.item.live .preview{padding-right:24px}
.spin{position:absolute;right:10px;top:50%;width:14px;height:14px;margin-top:-7px;color:var(--muted);animation:ph-spin 2s steps(60,end) infinite}.spin svg{display:block}@keyframes ph-spin{to{transform:rotate(360deg)}}@media(prefers-reduced-motion:reduce){.spin{animation:none}}
.title,.preview{display:block;white-space:nowrap;text-overflow:ellipsis;overflow:hidden}.title{font-size:13px;line-height:19px}.preview{font-size:12px;line-height:18px;opacity:.6}
.composer{display:flex;align-items:center;gap:8px;margin:10px 12px 4px;border-radius:24px;padding:6px 6px 6px 16px;border:1px solid var(--line);background:var(--bg2);flex:none}
input{width:0;flex:1;min-width:0;border:0;outline:none;background:none;font-size:14px;line-height:24px}input::placeholder{color:inherit;opacity:.5}
.send{width:32px;height:32px;border:0;border-radius:50%;background:var(--send-bg);color:var(--send-fg);font-size:20px}.send:disabled{opacity:.3;cursor:default}
.status{font-size:11px;line-height:16px;min-height:24px;padding:0 18px 10px;opacity:.7;flex:none}.empty{padding:22px 12px;font-size:13px;opacity:.6}
button:focus-visible{outline:2px solid var(--accent);outline-offset:1px}
</style></head><body>
<div class="heading"><span>Recent chats</span></div>
<div class="list" role="listbox" aria-label="Recent chats"></div>
<div class="composer"><input aria-label="Message selected chat" placeholder="Message selected chat…" maxlength="65536"><button class="send" aria-label="Send message">↑</button></div>
<div class="status" role="status" aria-live="polite"></div>
<script>
(()=>{
  const host=payload=>{try{if(typeof window.__providerHubWindowHost==='function')window.__providerHubWindowHost(JSON.stringify(payload));}catch(error){}};
  const key=t=>JSON.stringify([t.kind,t.hostId,t.threadId]);
  const UUID=/^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/i,HEX=/^#[0-9a-f]{6}$/i;
  const state={targets:[],selected:null,shown:undefined,drafts:new Map(),sent:new Map(),statuses:new Map(),pending:new Set()};
  const list=document.querySelector('.list'),input=document.querySelector('input'),send=document.querySelector('.send'),status=document.querySelector('.status');
  const spinGlyph='__SPIN__';
  const rows=new Map();
  const setAttr=(el,name,value)=>{if(el.getAttribute(name)!==value)el.setAttribute(name,value);};
  const setText=(el,value)=>{if(el.textContent!==value)el.textContent=value;};
  function updateComposer(){
    const t=state.targets.find(t=>key(t)===state.selected);
    input.disabled=!t?.supported;send.disabled=!t?.supported||!input.value.trim()||state.pending.has(state.selected);
    input.placeholder=t?.supported?'Message '+t.title+'…':'Select a local Codex chat';
    status.textContent=state.statuses.get(state.selected)||(!t?.supported?'Remote and ChatGPT chats are unavailable here.':'');
  }
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
    setText(row.querySelector('.preview'),t.supported?(t.preview||'No response preview available'):'Unavailable in this window');
    let spin=row.querySelector('.spin');row.classList.toggle('live',t.active);
    if(t.active&&!spin){spin=document.createElement('span');spin.className='spin';spin.setAttribute('aria-hidden','true');spin.innerHTML=spinGlyph;row.prepend(spin);}
    else if(!t.active&&spin){spin.remove();spin=null;}
    if(spin&&spin.__accent!==t.activeAccent){spin.__accent=t.activeAccent;spin.style.color=t.activeAccent||'';}
  }
  function render(){
    if(!state.targets.some(t=>key(t)===state.selected)){const first=state.targets.find(t=>t.supported);state.selected=first?key(first):null;}
    const keys=state.targets.map(key),focused=document.activeElement;
    for(const [k,row] of rows)if(!keys.includes(k)){row.remove();rows.delete(k);}
    const wanted=keys.map(item);state.targets.forEach((t,i)=>paint(wanted[i],t,keys[i]));
    const empty=list.querySelector('.empty');
    if(wanted.length)empty?.remove();
    else if(!empty){const note=document.createElement('div');note.className='empty';note.textContent='No recent chats are available in the sidebar.';list.appendChild(note);}
    const order=Array.from(list.querySelectorAll('.item'));
    if(order.length!==wanted.length||order.some((row,i)=>row!==wanted[i])){list.append(...wanted);if(focused?.isConnected&&document.activeElement!==focused)focused.focus();}
    if(state.shown!==state.selected){state.shown=state.selected;input.value=state.drafts.get(state.selected)||'';}
    updateComposer();
  }
  function valid(t){
    if(!t||typeof t!=='object')return null;
    const threadId=t.threadId,hostId=t.hostId,kind=t.kind;
    if(typeof threadId!=='string'||typeof hostId!=='string'||typeof kind!=='string')return null;
    return {threadId,hostId,kind,title:typeof t.title==='string'&&t.title?t.title:'Untitled chat',
      supported:t.supported===true&&kind==='local'&&hostId==='local'&&UUID.test(threadId),
      active:t.active===true,activeAccent:typeof t.activeAccent==='string'&&HEX.test(t.activeAccent)?t.activeAccent.toUpperCase():null,
      preview:typeof t.preview==='string'?t.preview.trim().replace(/\s+/g,' ').slice(0,512):null};
  }
  function setState(payload){
    const seen=new Set(),targets=[];
    for(const raw of Array.isArray(payload?.targets)?payload.targets.slice(0,10):[]){const t=valid(raw);if(t&&!seen.has(key(t))){seen.add(key(t));targets.push(t);}}
    state.targets=targets;render();return {applied:targets.length};
  }
  function submit(){
    const t=state.targets.find(t=>key(t)===state.selected);
    if(!t?.supported||state.pending.has(state.selected))return;
    const prompt=input.value;if(!prompt.trim())return;
    const k=key(t);state.drafts.set(k,prompt);state.sent.set(k,prompt);state.pending.add(k);state.statuses.set(k,'Sending…');updateComposer();
    host({type:'send',threadId:t.threadId,prompt});
  }
  function sendResult(result){
    const threadId=result?.threadId;if(typeof threadId!=='string')return {applied:false};
    const k=Array.from(state.pending).find(k=>JSON.parse(k)[2]===threadId);if(!k)return {applied:false};
    state.pending.delete(k);
    if(result.ok===true&&state.drafts.get(k)===state.sent.get(k))state.drafts.set(k,'');
    state.sent.delete(k);
    state.statuses.set(k,typeof result.reason==='string'&&result.reason?result.reason:(result.ok===true?'Sent.':'Send failed. Your draft is kept.'));
    if(state.selected===k)input.value=state.drafts.get(k)||'';updateComposer();return {applied:true};
  }
  send.addEventListener('click',submit);
  input.addEventListener('input',()=>{if(state.selected)state.drafts.set(state.selected,input.value);updateComposer();});
  input.addEventListener('keydown',e=>{if(e.key==='Enter'&&!e.isComposing){e.preventDefault();submit();}});
  document.addEventListener('keydown',e=>{
    if(e.key==='Escape'&&!e.isComposing){e.preventDefault();host({type:'close'});return;}
    if((e.key==='ArrowDown'||e.key==='ArrowUp')&&document.activeElement?.classList.contains('item')){
      e.preventDefault();const items=Array.from(list.querySelectorAll('.item'));const n=items.indexOf(document.activeElement);
      items[(n+(e.key==='ArrowDown'?1:-1)+items.length)%items.length]?.focus();
    }
  });
  // Where the window sits is the user's choice; report it when it changes so
  // the worker can reopen the window in the same place next time.
  let lastBounds='';
  const report=()=>{const b={type:'bounds',left:screenX,top:screenY,width:outerWidth,height:outerHeight};const s=JSON.stringify(b);if(s!==lastBounds){lastBounds=s;host(b);}};
  window.addEventListener('resize',report);setInterval(report,1000);
  window.__providerHubQuickWindow={setState,sendResult,check:()=>({targets:state.targets.length,selected:state.selected,pending:state.pending.size})};
  render();host({type:'ready'});
})();
</script></body></html>
""".replace("__SPIN__", _SPIN)


def send_from_host_expression(thread_id: str, prompt: str) -> str:
    """The app-page call that performs one send for a window or the Hub."""
    argument = json.dumps({"threadId": thread_id, "prompt": prompt})
    return ("(async () => { if (window !== window.top || !/^app:\\/\\/-\\//.test(String(location.href))) return null; "
            "const composer = window.__providerHubQuickComposer; if (!composer?.sendFromHost) return null; "
            f"return await composer.sendFromHost({argument}); }})()")


def _load_bounds(path: Path | None) -> dict | None:
    if path is None:
        return None
    try:
        raw = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return _bounds(raw)


def _bounds(raw) -> dict | None:
    if not isinstance(raw, dict):
        return None
    result = {}
    for name in ("left", "top", "width", "height"):
        value = raw.get(name)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        result[name] = int(value)
    if result["width"] < 240 or result["height"] < 200 or result["width"] > 10000 or result["height"] > 10000:
        return None
    if abs(result["left"]) > 100000 or abs(result["top"]) > 100000:
        return None
    return result


class QuickWindowBridge:
    """Own the separate quick-composer window over the DevTools pipe."""

    def __init__(self, pipe=None, emit=None, state_path: Path | None = None, host=None):
        self.pipe = pipe
        self.emit = emit or (lambda event: None)
        self.state_path = state_path
        # The Hub's own panel (codex_quick_host) takes precedence when the
        # Hub is listening; this window is the fallback once it is gone.
        self.host = host
        self.bounds = _load_bounds(state_path)
        self.pending: dict[int, tuple[str, object]] = {}
        self.main_sessions: set[str] = set()
        self.composer_session: str | None = None
        self.target_id: str | None = None
        self.session_id: str | None = None
        self.window_id = None
        self.opening = False
        self.ready = False
        self.navigated = False
        self.last_state = None

    # -- wiring from the accent bridge -------------------------------------
    def attach_main(self, session_id: str) -> None:
        """A Desktop app window: let its launcher ask for this window."""
        self.main_sessions.add(session_id)
        self.pipe.send("Runtime.enable", session_id=session_id)
        self.pipe.send("Runtime.addBinding", {"name": HOST_BINDING}, session_id=session_id)

    def wants_target(self, info: dict) -> bool:
        if self.target_id is not None:
            return info.get("targetId") == self.target_id
        # Desktop attaches the new target before it answers createTarget, so
        # while a create is in flight the blank page that appears is ours.
        return (self.opening and self.session_id is None and info.get("type") == "page"
                and info.get("url") in ("about:blank", "") and isinstance(info.get("targetId"), str))

    def attached(self, session_id: str, info: dict) -> None:
        """Our created target has a session: fill it and show it."""
        if self.target_id is None:
            self.target_id = info.get("targetId")
        self.session_id = session_id
        self.ready = False
        self.navigated = False
        self.pipe.send("Page.enable", session_id=session_id)
        self.pipe.send("Runtime.enable", session_id=session_id)
        self.pipe.send("Runtime.addBinding", {"name": WINDOW_BINDING}, session_id=session_id)
        identifier = self.pipe.send("Page.setDocumentContent", {"frameId": self.target_id, "html": quick_window_html()},
                                    session_id=session_id)
        self.pending[identifier] = ("document", None)
        identifier = self.pipe.send("Browser.getWindowForTarget", {"targetId": self.target_id})
        self.pending[identifier] = ("window", None)

    def detach(self, session_id: str) -> None:
        self.main_sessions.discard(session_id)
        if session_id == self.composer_session:
            self.composer_session = None
        if session_id == self.session_id:
            self._closed("detached")

    def is_open(self) -> bool:
        return self.session_id is not None

    def open(self) -> None:
        if self.session_id is not None:
            self.pipe.send("Target.activateTarget", {"targetId": self.target_id})
            return
        if self.opening or self.target_id is not None:
            return  # being created, or created and about to attach
        self.opening = True
        params = {"url": "about:blank", "newWindow": True,
                  "width": (self.bounds or DEFAULT_BOUNDS)["width"], "height": (self.bounds or DEFAULT_BOUNDS)["height"]}
        self.pending[self.pipe.send("Target.createTarget", params)] = ("create", None)

    def close(self) -> None:
        if self.target_id is not None:
            self.pipe.send("Target.closeTarget", {"targetId": self.target_id})

    def update(self, session_id: str, payload: list) -> None:
        """The rows and previews the worker just read for the sidebar."""
        self.composer_session = session_id
        self.last_state = {"targets": payload}
        self._push()

    # -- messages ----------------------------------------------------------
    def handle(self, message: dict) -> bool:
        identifier = message.get("id")
        if identifier in self.pending:
            kind, detail = self.pending.pop(identifier)
            self._reply(kind, detail, message)
            return True
        method = message.get("method")
        params = message.get("params") or {}
        session_id = message.get("sessionId")
        if method == "Runtime.bindingCalled":
            name = params.get("name")
            if name == HOST_BINDING and session_id in self.main_sessions:
                self._host_request(params.get("payload"))
                return True
            if name == WINDOW_BINDING and session_id == self.session_id and not self.navigated:
                self._window_request(params.get("payload"))
                return True
            return name in (HOST_BINDING, WINDOW_BINDING)
        if method == "Page.frameNavigated" and session_id == self.session_id:
            frame = params.get("frame") or {}
            if frame.get("parentId") is None and frame.get("url") != "about:blank":
                # The address bar is Desktop's. Anything but our blank page
                # must not keep a binding into the worker.
                self.navigated = True
                self.emit({"event": "quick-window", "stage": "navigated"})
                self.close()
            return True
        if method in ("Target.detachedFromTarget", "Target.targetDestroyed"):
            if (params.get("sessionId") and params.get("sessionId") == self.session_id) \
                    or (params.get("targetId") and params.get("targetId") == self.target_id):
                self._closed("closed")
                return True
        return False

    def _reply(self, kind, detail, message):
        error = message.get("error")
        result = message.get("result") or {}
        if kind == "create":
            self.opening = False
            if error or not isinstance(result.get("targetId"), str):
                self.emit({"event": "quick-window", "stage": "create", "available": False})
                return
            created = result["targetId"]
            if self.target_id is not None and self.target_id != created:
                # A blank page that was not ours attached first. Keep the one
                # Desktop says it created and let go of the other.
                stray = self.target_id
                self.target_id, self.session_id, self.ready = created, None, False
                self.emit({"event": "quick-window", "stage": "mismatch"})
                self.pipe.send("Target.closeTarget", {"targetId": stray})
            self.target_id = created
            self.emit({"event": "quick-window", "stage": "created"})
        elif kind == "window":
            if error:
                self.emit({"event": "quick-window", "stage": "window", "available": False})
            else:
                self.window_id = result.get("windowId")
                if self.window_id is not None and self.bounds:
                    self.pipe.send("Browser.setWindowBounds", {"windowId": self.window_id, "bounds": dict(self.bounds)})
            if self.target_id is not None:
                self.pipe.send("Target.activateTarget", {"targetId": self.target_id})
        elif kind == "document":
            if error:
                self.emit({"event": "quick-window", "stage": "document", "available": False})
        elif kind == "send":
            thread_id = detail
            value = ((result.get("result") or {}).get("value")) if not error else None
            ok = isinstance(value, dict) and value.get("ok") is True
            reason = value.get("reason") if isinstance(value, dict) and isinstance(value.get("reason"), str) else None
            if not ok and not reason:
                reason = "Send failed. Your draft is kept." if value is not None or error else "Sending is unavailable in this Desktop version. Draft kept."
            self._evaluate_window("sendResult", {"threadId": thread_id, "ok": ok, "reason": reason}, "push")
            self.emit({"event": "quick-window", "stage": "send", "ok": ok, "threadId": thread_id})

    def _host_request(self, payload):
        request = _parse(payload)
        if request.get("type") == "open-window":
            if self.host is not None and self.host.request_open():
                return
            self.open()

    def _window_request(self, payload):
        request = _parse(payload)
        kind = request.get("type")
        if kind == "ready":
            self.ready = True
            self._push()
        elif kind == "close":
            self.close()
        elif kind == "bounds":
            bounds = _bounds(request)
            if bounds:
                self.bounds = bounds
                self._save_bounds()
        elif kind == "send":
            self._send(request)

    def _send(self, request):
        thread_id = request.get("threadId")
        prompt = request.get("prompt")
        if not isinstance(thread_id, str) or not _UUID.fullmatch(thread_id):
            return
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > _PROMPT_LIMIT:
            self._evaluate_window("sendResult", {"threadId": thread_id, "ok": False,
                                                 "reason": "A non-empty prompt of at most 64 KiB is required."}, "push")
            return
        session = self.composer_session or next(iter(sorted(self.main_sessions)), None)
        if session is None:
            self._evaluate_window("sendResult", {"threadId": thread_id, "ok": False,
                                                 "reason": "Codex's window is not available. Your draft is kept."}, "push")
            return
        identifier = self.pipe.send("Runtime.evaluate", {"expression": send_from_host_expression(thread_id, prompt),
                                                         "returnByValue": True, "awaitPromise": True, "timeout": 60000},
                                    session_id=session)
        self.pending[identifier] = ("send", thread_id)

    def _push(self):
        if self.ready and self.last_state is not None and self.session_id is not None:
            self._evaluate_window("setState", self.last_state, "push")

    def _evaluate_window(self, method, argument, kind):
        if self.session_id is None or self.navigated:
            return
        expression = f"window.__providerHubQuickWindow?.{method}?.({json.dumps(argument)})"
        identifier = self.pipe.send("Runtime.evaluate", {"expression": expression, "returnByValue": True, "timeout": 2000},
                                    session_id=self.session_id)
        self.pending[identifier] = (kind, None)

    def _save_bounds(self):
        if self.state_path is None:
            return
        try:
            self.state_path.write_text(json.dumps(self.bounds))
        except OSError:
            pass

    def _closed(self, how):
        was_open = self.session_id is not None or self.target_id is not None
        self.session_id = None
        self.target_id = None
        self.window_id = None
        self.ready = False
        self.opening = False
        self.navigated = False
        # Replies still due for the old window must not act on the next one.
        self.pending = {identifier: entry for identifier, entry in self.pending.items() if entry[0] == "send"}
        if was_open:
            self.emit({"event": "quick-window", "stage": how})


def _parse(payload) -> dict:
    if not isinstance(payload, str) or len(payload) > _PAYLOAD_LIMIT:
        return {}
    try:
        value = json.loads(payload)
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


__all__ = ["QuickWindowBridge", "quick_window_html", "send_from_host_expression", "WINDOW_BINDING", "HOST_BINDING", "DEFAULT_BOUNDS"]
