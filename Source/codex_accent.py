"""Codex power-slider accents.

The ChatGPT desktop app paints its model picker's power slider with one
app-wide design token (``--color-chart-blue``); its model records carry no
colour, and it offers no theming hook. This module gives each hub model its
provider's accent anyway without touching the app bundle: the app is started
as a child of the worker with Chromium's ``--remote-debugging-pipe`` switch,
and a small watcher script is injected into its windows over that pipe.

The pipe is a pair of file descriptors only this helper holds, so nothing
listens on a port. The watcher only reads the picker's own labels and sets
one CSS custom property on the picker; it keeps Ultra's purple untouched.

The pipe is also the app's lifeline: Electron quits when it closes. So the
helper ignores termination signals, never lets a failed status write or a
malformed message end it, and hands the app a launch-services-like
environment and detached stdio so nothing of the hub's reaches it.
"""
from __future__ import annotations

import base64
import fcntl
import json
import os
import plistlib
import re
import select
import signal
import subprocess
import sys
import time
from pathlib import Path

from codex_catalogue import project_codex

PROPERTY = "--color-chart-blue"
_HEX = re.compile(r"^#[0-9A-Fa-f]{6}$")
GLYPH_ATTRIBUTE = "data-provider-hub-glyph"
THEME_ATTRIBUTE = "data-provider-hub-theme"
ACCENT_PROPERTY = "--provider-hub-accent"
# How much of the model's accent goes into the activity shimmer's sweep.
SHIMMER_MIX = "35%"
GLYPH_DIR = Path(__file__).with_name("provider-logos") / "glyphs"
# Brand hue key (or runtime provider id) -> glyph file stem under
# provider-logos/glyphs: 40px marks trimmed from the bundled brand lockups.
GLYPH_KEYS = {
    "mistral": "mistral", "kimi": "kimi", "deepseek": "deepseek", "gemini": "gemini", "antigravity": "gemini",
    "ollama": "ollama", "cerebras": "cerebras", "grok": "grok", "alibaba": "qwen", "qwen": "qwen",
    "qwen-token-plan": "qwen", "meta": "meta", "muse": "meta", "xiaomi": "mimo", "mimo": "mimo",
    "openrouter": "openrouter",
}
_LAUNCH_SWITCH = "--remote-debugging-pipe"
_APP_ORIGIN = "app://-/"
_AUTO_ATTACH = {"autoAttach": True, "waitForDebuggerOnStart": False, "flatten": True}
# What launchd hands a LaunchServices launch, and nothing of the hub's own
# process: no provider keys, no NODE_OPTIONS, no ELECTRON_* or CODEX_* knobs.
_ENVIRONMENT_KEYS = ("HOME", "USER", "LOGNAME", "SHELL", "TMPDIR", "LANG", "LC_ALL", "LC_CTYPE", "TZ",
                     "__CF_USER_TEXT_ENCODING", "XPC_FLAGS", "XPC_SERVICE_NAME", "SSH_AUTH_SOCK", "SECURITYSESSIONID")
_LAUNCHD_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"


def accent_map(settings: dict, inventory: dict) -> dict:
    """Composer label -> provider accent for every model Codex will list.

    Labels are the display names the hub itself projects into the Codex
    catalogue, so the watcher can match them exactly. Accents come from the
    projected catalogue's presentation, which already applies the hub's
    branding overrides and model brand rules (an Ollama-hosted Qwen keeps
    the Qwen hue).
    """
    entries = {entry.get("id"): entry for entry in inventory.get("models", []) if isinstance(entry, dict)}
    accents = {}
    for model in project_codex(settings, inventory)["models"]:
        presentation = (entries.get(model["slug"]) or {}).get("presentation") or {}
        colour = presentation.get("accent")
        if isinstance(colour, str) and _HEX.match(colour):
            accents[model["display_name"]] = colour.upper()
    return accents


def glyph_map(settings: dict, inventory: dict) -> dict:
    """Composer label -> glyph stem: the model's brand hue first, then its
    runtime provider (an OpenRouter-hosted brand without a mark of its own
    wears OpenRouter's)."""
    entries = {entry.get("id"): entry for entry in inventory.get("models", []) if isinstance(entry, dict)}
    glyphs = {}
    for model in project_codex(settings, inventory)["models"]:
        entry = entries.get(model["slug"]) or {}
        presentation = entry.get("presentation") or {}
        for key in (presentation.get("hueKey"), entry.get("provider_id"), str(model["slug"]).split("/", 1)[0]):
            stem = GLYPH_KEYS.get(str(key or "").strip().lower())
            if stem and (GLYPH_DIR / f"{stem}.png").is_file():
                glyphs[model["display_name"]] = stem
                break
    return glyphs


def glyph_assets(stems) -> dict:
    """Glyph stem -> {"light": data URL, "dark": data URL} for the marks that exist."""
    assets = {}
    for stem in sorted(set(stems)):
        variants = {}
        for variant, name in (("light", f"{stem}.png"), ("dark", f"{stem}-on-dark.png")):
            path = GLYPH_DIR / name
            if path.is_file():
                variants[variant] = "data:image/png;base64," + base64.b64encode(path.read_bytes()).decode()
        if variants:
            assets[stem] = variants
    return assets


def glyph_css(assets: dict) -> str:
    """The stylesheet the watcher adopts.

    A 14px brand mark before the pill's model name is drawn by a
    pseudo-element keyed on the attribute the watcher sets, so nothing is
    inserted into the app's own DOM tree. The activity shimmer ("Thinking",
    "Editing files") gets a sweep tinted with the selected model's accent:
    the app resets its ``--loading-shimmer-highlight`` on the element with a
    zero-specificity rule and falls back to a per-theme constant, so a
    zero-specificity rule of ours later in the cascade wins over that reset
    while any component that sets its own highlight still wins over ours;
    without an accent the ``var()`` is invalid and the app's fallback returns.
    """
    shimmer = f"[{THEME_ATTRIBUTE}] :is(.loading-shimmer-pure-text,.loading-shimmer)"
    rules = [f':where({shimmer}){{--loading-shimmer-highlight:color-mix(in srgb,var({ACCENT_PROPERTY}) {SHIMMER_MIX},#ffffffbf)}}',
             f':where([{THEME_ATTRIBUTE}="dark"] :is(.loading-shimmer-pure-text,.loading-shimmer))'
             f'{{--loading-shimmer-highlight:color-mix(in srgb,var({ACCENT_PROPERTY}) {SHIMMER_MIX},#0009)}}',
             f'[{GLYPH_ATTRIBUTE}]::before{{content:"";display:block;flex:none;width:14px;height:14px;'
             'background-position:center;background-repeat:no-repeat;background-size:contain}',
             f'[{GLYPH_ATTRIBUTE}][{GLYPH_ATTRIBUTE}-inline]::before{{display:inline-block;vertical-align:-2px;margin-inline-end:4px}}']
    for stem, variants in assets.items():
        light = variants.get("light") or variants.get("dark")
        rules.append(f'[{GLYPH_ATTRIBUTE}="{stem}"]::before{{background-image:url("{light}")}}')
        if "light" in variants and "dark" in variants:
            rules.append(f'[{GLYPH_ATTRIBUTE}="{stem}"][{GLYPH_ATTRIBUTE}-theme="dark"]::before{{background-image:url("{variants["dark"]}")}}')
    return "".join(rules)


_WATCHER = r"""
(() => {
  // The completion value goes back to the helper's log. Only the app's own
  // top-level documents are touched: never sandboxed app frames or
  // browser-panel windows showing outside content.
  try {
    if (window !== window.top) { return { skipped: "frame" }; }
    if (!/^app:\/\/-\//.test(String(location.href))) { return { skipped: "origin" }; }
  } catch (error) { return { skipped: "guard" }; }
  if (window.__providerHubAccent) { return { skipped: "installed" }; }
  try {
    const ACCENTS = __HUB_ACCENTS__;
    const GLYPHS = __HUB_GLYPHS__;
    const GLYPH_CSS = __HUB_GLYPH_CSS__;
    const PROPERTY = "__HUB_PROPERTY__";
    const MARK = "data-provider-hub-tint";
    const GLYPH = "__HUB_GLYPH_ATTRIBUTE__";
    const THEME = "__HUB_THEME_ATTRIBUTE__";
    const ACCENT_PROPERTY = "__HUB_ACCENT_PROPERTY__";
    const state = { targets: [], label: "", colour: "", words: [], glyphs: [], sheet: null, accent: "", theme: "" };
    const norm = (text) => (text || "").replace(/\s+/g, " ").trim().toLowerCase();
    // Labels may carry a leading glyph (a bullet, a tier mark); match the words.
    const lookup = (text) => {
      const key = norm(text).replace(/^[^a-z0-9]+/, "").replace(/[^a-z0-9)\]]+$/, "");
      return key && Object.prototype.hasOwnProperty.call(ACCENTS, key) ? key : "";
    };
    const effortLabel = (container) => container.querySelector("[data-effort-only],[data-accent],[data-maximum]");
    function findModel(container, skip) {
      // The explicit-model layout shows the effort label (data-accent /
      // data-maximum) on one row and the model's display name on the next.
      for (const span of container.querySelectorAll("span")) {
        if (skip && (span === skip || skip.contains(span) || span.contains(skip))) { continue; }
        const key = lookup(span.textContent);
        if (key) { return { key: key, element: span }; }
      }
      return null;
    }
    function modelLabel(container, skip) {
      const found = findModel(container, skip);
      return found ? found.key : "";
    }
    // The innermost span carrying the name: the one whose colour is the text's.
    function textElement(found) {
      let element = found.element;
      for (const span of found.element.querySelectorAll("span")) {
        if (lookup(span.textContent) === found.key) { element = span; }
      }
      return element;
    }
    // Light text means a dark surface: pick the on-dark mark there.
    function themeOf(element) {
      try {
        const match = /rgba?\(\s*([\d.]+)[,\s]+([\d.]+)[,\s]+([\d.]+)/.exec(getComputedStyle(element).color);
        if (!match) { return "light"; }
        return (0.2126 * match[1] + 0.7152 * match[2] + 0.0722 * match[3]) / 255 > 0.5 ? "dark" : "light";
      } catch (error) { return "light"; }
    }
    function installStyles() {
      if (!GLYPH_CSS) { return; }
      try {
        if (!state.sheet) {
          state.sheet = new CSSStyleSheet();
          state.sheet.replaceSync(GLYPH_CSS);
        }
        if (!document.adoptedStyleSheets.includes(state.sheet)) {
          document.adoptedStyleSheets = [...document.adoptedStyleSheets, state.sheet];
        }
      } catch (error) {
        if (!state.sheet && document.head) {
          const style = document.createElement("style");
          style.textContent = GLYPH_CSS;
          document.head.appendChild(style);
          state.sheet = style;
        }
      }
    }
    function clearMenu() {
      // Only undo our own value: leave any inline value the app set itself.
      for (const target of state.targets) {
        try {
          if (norm(target.style.getPropertyValue(PROPERTY)) === norm(state.colour)) { target.style.removeProperty(PROPERTY); }
        } catch (error) {}
      }
      state.targets = []; state.label = ""; state.colour = "";
    }
    function applyMenu() {
      const container = document.querySelector('[data-explicit-model="true"]');
      if (!container) { clearMenu(); return; }
      const label = modelLabel(container, effortLabel(container));
      const colour = label ? ACCENTS[label] : "";
      const host = container.closest("[data-transitions-ready],[data-side]") || container.parentElement;
      if (!host || !colour) { clearMenu(); return; }
      // Themed subtrees re-declare the token, so set it on those too.
      const targets = [host, ...host.querySelectorAll("[data-theme],[data-model-picker-power-slider]")];
      const same = state.colour === colour && targets.length === state.targets.length && targets.every((target, index) => target === state.targets[index]);
      if (!same) {
        clearMenu();
        for (const target of targets) { target.style.setProperty(PROPERTY, colour); }
        state.targets = targets; state.label = label; state.colour = colour;
      }
    }
    // The composer pill: "<model> <effort>". The model picker trigger stacks
    // one span per effort level ([data-reasoning-effort]) and crossfades
    // them inside an effort label that carries the pill's tertiary grey; the
    // Ultra span has its own purple rule, which beats an inherited colour.
    // Give that label the model's accent. Ultra is left to the app, as is
    // anything the app already colours purple. The older two-part pill
    // (model span + effort span) is handled the same way as a fallback.
    function pillWords() {
      const words = [];
      const glyphs = [];
      let accent = "";
      let theme = "";
      for (const trigger of document.querySelectorAll("[data-codex-intelligence-trigger]")) {
        const effort = norm(trigger.getAttribute("data-selected-reasoning-effort"));
        const labels = [];
        for (const layer of trigger.querySelectorAll("[data-reasoning-effort]")) {
          const label = layer.closest("[data-composer-footer-collapse]") || (layer.parentElement && layer.parentElement.parentElement && layer.parentElement.parentElement.parentElement);
          if (label && !labels.includes(label)) { labels.push(label); }
        }
        if (!labels.length) {
          const content = trigger.querySelector("[data-tooltip-overflow-target]") || trigger;
          const wrapper = content.firstElementChild;
          if (wrapper && wrapper.children.length >= 2) { labels.push(wrapper.lastElementChild); }
        }
        const model = findModel(trigger, labels[0] || null);
        if (!model) { continue; }
        const surface = themeOf(textElement(model));
        // The selected model's accent also tints the activity shimmer; the
        // first pill wins if there are several.
        if (!accent) { accent = ACCENTS[model.key]; theme = surface; }
        // The brand mark goes before the model name, as the first flex item
        // of the model group (or inline when the layout is not a flex row).
        const stem = GLYPHS[model.key];
        if (stem) {
          const group = model.element.closest('[class*="ModelPickerTriggerModelGroup"]') || model.element;
          let inline = true;
          try { inline = !/flex|grid/.test(getComputedStyle(group).display); } catch (error) {}
          glyphs.push({ element: group, stem: stem, theme: surface, inline: inline });
        }
        // Ultra is left to the app (its own purple), as is anything it
        // already paints purple.
        if (!effort || effort === "ultra") { continue; }
        for (const label of labels) {
          if (!label || label.classList.contains("text-chart-purple")) { continue; }
          words.push({ element: label, colour: ACCENTS[model.key] });
        }
      }
      return { words: words, glyphs: glyphs, accent: accent, theme: theme };
    }
    function applyShimmer(accent, theme) {
      if (accent === state.accent && theme === state.theme) { return; }
      const root = document.documentElement;
      if (!root) { return; }
      if (accent) {
        installStyles();
        root.style.setProperty(ACCENT_PROPERTY, accent);
        root.setAttribute(THEME, theme);
      } else {
        if (norm(root.style.getPropertyValue(ACCENT_PROPERTY)) === norm(state.accent)) { root.style.removeProperty(ACCENT_PROPERTY); }
        root.removeAttribute(THEME);
      }
      state.accent = accent; state.theme = theme;
    }
    function clearWords() {
      for (const entry of state.words) {
        try {
          if (entry.element.getAttribute(MARK) === "1") { entry.element.style.removeProperty("color"); entry.element.removeAttribute(MARK); }
        } catch (error) {}
      }
      state.words = [];
    }
    function clearGlyphs() {
      for (const entry of state.glyphs) {
        try {
          entry.element.removeAttribute(GLYPH); entry.element.removeAttribute(GLYPH + "-theme"); entry.element.removeAttribute(GLYPH + "-inline");
        } catch (error) {}
      }
      state.glyphs = [];
    }
    function applyPills() {
      const found = pillWords();
      const sameWords = found.words.length === state.words.length && found.words.every((entry, index) => entry.element === state.words[index].element && entry.colour === state.words[index].colour);
      if (!sameWords) {
        clearWords();
        for (const entry of found.words) { entry.element.style.setProperty("color", entry.colour); entry.element.setAttribute(MARK, "1"); }
        state.words = found.words;
      }
      const sameGlyphs = found.glyphs.length === state.glyphs.length && found.glyphs.every((entry, index) => entry.element === state.glyphs[index].element && entry.stem === state.glyphs[index].stem && entry.theme === state.glyphs[index].theme);
      if (!sameGlyphs) {
        clearGlyphs();
        if (found.glyphs.length) { installStyles(); }
        for (const entry of found.glyphs) {
          entry.element.setAttribute(GLYPH, entry.stem);
          entry.element.setAttribute(GLYPH + "-theme", entry.theme);
          if (entry.inline) { entry.element.setAttribute(GLYPH + "-inline", ""); }
        }
        state.glyphs = found.glyphs;
      }
      applyShimmer(found.accent, found.theme);
    }
    function apply() {
      try { applyMenu(); } catch (error) {}
      try { applyPills(); } catch (error) {}
    }
    let scheduled = false;
    function schedule() {
      if (scheduled) { return; }
      scheduled = true;
      requestAnimationFrame(() => { scheduled = false; apply(); });
    }
    // Observe the document node: at document start there is no root element yet.
    const observer = new MutationObserver(schedule);
    observer.observe(document, { childList: true, subtree: true, characterData: true, attributes: true, attributeFilter: ["data-explicit-model", "data-accent", "data-maximum", "data-selected-reasoning-effort"] });
    if (document.readyState === "loading") { document.addEventListener("DOMContentLoaded", schedule, { once: true }); }
    window.__providerHubAccent = {
      version: 4,
      accents: Object.keys(ACCENTS).length,
      glyphs: Object.keys(GLYPHS).length,
      check: () => ({ container: !!document.querySelector('[data-explicit-model="true"]'), label: state.label, colour: state.colour, targets: state.targets.length, pills: state.words.map((entry) => entry.colour), glyphs: state.glyphs.map((entry) => entry.stem + ":" + entry.theme), shimmer: state.accent ? state.accent + ":" + state.theme : "" }),
    };
    schedule();
    return { installed: true, accents: Object.keys(ACCENTS).length, glyphs: Object.keys(GLYPHS).length, ready: document.readyState };
  } catch (error) {
    return { error: String(error && error.message ? error.message : error) };
  }
})();
"""


def _label_key(label: str) -> str:
    return label.replace("\u00a0", " ").strip().lower()


def watcher_script(accents: dict, property_name: str = PROPERTY, glyphs: dict | None = None,
                   assets: dict | None = None) -> str:
    table = {_label_key(label): colour for label, colour in accents.items()}
    marks = {_label_key(label): stem for label, stem in (glyphs or {}).items()}
    return (_WATCHER.replace("__HUB_ACCENTS__", json.dumps(table, ensure_ascii=False))
            .replace("__HUB_GLYPHS__", json.dumps(marks, ensure_ascii=False))
            .replace("__HUB_GLYPH_CSS__", json.dumps(glyph_css(assets or {})))
            .replace("__HUB_GLYPH_ATTRIBUTE__", GLYPH_ATTRIBUTE)
            .replace("__HUB_THEME_ATTRIBUTE__", THEME_ATTRIBUTE)
            .replace("__HUB_ACCENT_PROPERTY__", ACCENT_PROPERTY)
            .replace("__HUB_PROPERTY__", property_name))


def executable_path(app_path: Path) -> Path:
    """The binary inside an .app bundle, from its Info.plist."""
    info = app_path / "Contents" / "Info.plist"
    try:
        with info.open("rb") as stream:
            name = plistlib.load(stream).get("CFBundleExecutable")
    except (OSError, ValueError) as exc:
        raise ValueError(f"Cannot read {info.name}; is this an app bundle?") from exc
    if not isinstance(name, str) or not name:
        raise ValueError("The app bundle does not name its executable.")
    binary = app_path / "Contents" / "MacOS" / name
    if not os.access(binary, os.X_OK):
        raise ValueError("The app's executable is missing or not runnable.")
    return binary


def child_environment(app_path: Path | None = None) -> dict:
    """The environment a LaunchServices launch would give the app.

    Only the login session's basics pass through, with launchd's PATH, plus
    the bundle's own ``LSEnvironment`` entries (which a direct spawn would
    otherwise drop). Nothing from the hub's process reaches the app.
    """
    environment = {key: os.environ[key] for key in _ENVIRONMENT_KEYS if key in os.environ}
    environment["PATH"] = _LAUNCHD_PATH
    if app_path is not None:
        try:
            with (app_path / "Contents" / "Info.plist").open("rb") as stream:
                extra = plistlib.load(stream).get("LSEnvironment") or {}
        except (OSError, ValueError):
            extra = {}
        if isinstance(extra, dict):
            environment.update({key: str(value) for key, value in extra.items() if isinstance(key, str)})
    return environment


class DevToolsPipe:
    """NUL-delimited JSON over the two descriptors Chromium's pipe uses.

    The browser reads commands from its fd 3 and writes responses and
    events to its fd 4; this end holds the other side of each pipe.
    """

    def __init__(self, read_fd: int, write_fd: int):
        self.read_fd = read_fd
        self.write_fd = write_fd
        self.buffer = b""
        self.next_id = 0

    def send(self, method: str, params: dict | None = None, session_id: str | None = None) -> int:
        self.next_id += 1
        message = {"id": self.next_id, "method": method, "params": params or {}}
        if session_id:
            message["sessionId"] = session_id
        data = json.dumps(message).encode() + b"\0"
        while data:
            written = os.write(self.write_fd, data)
            data = data[written:]
        return self.next_id

    def poll(self, timeout: float) -> list[dict]:
        ready, _, _ = select.select([self.read_fd], [], [], timeout)
        if not ready:
            return []
        chunk = os.read(self.read_fd, 65536)
        if not chunk:
            raise EOFError("The DevTools pipe closed.")
        self.buffer += chunk
        messages = []
        while b"\0" in self.buffer:
            raw, self.buffer = self.buffer.split(b"\0", 1)
            try:
                message = json.loads(raw)
            except ValueError:
                continue
            if isinstance(message, dict):
                messages.append(message)
        return messages

    def close(self) -> None:
        for fd in (self.read_fd, self.write_fd):
            try:
                os.close(fd)
            except OSError:
                pass


class AccentBridge:
    """Attach to the app's own windows and install the watcher in each.

    Auto-attach covers windows that exist and windows still to come. Only
    page targets showing the app's own ``app://-/`` documents keep their
    session; anything else (sandboxed app frames, browser-panel windows on
    outside sites, workers) is detached again at once.
    """

    def __init__(self, pipe: DevToolsPipe, script: str, emit=None):
        self.pipe = pipe
        self.script = script
        self.emit = emit or (lambda event: None)
        self.pending: dict[int, tuple[str, str | None]] = {}
        self.injected: set[str] = set()

    def start(self) -> None:
        params = dict(_AUTO_ATTACH, filter=[{"type": "page"}])
        self.pending[self.pipe.send("Target.setAutoAttach", params)] = ("autoattach", None)

    @staticmethod
    def wanted(info: dict) -> bool:
        url = info.get("url") or ""
        return info.get("type") == "page" and (url in ("", "about:blank") or url.startswith(_APP_ORIGIN))

    def handle(self, message: dict) -> None:
        method = message.get("method")
        params = message.get("params") or {}
        if method == "Target.attachedToTarget":
            self._attached(params.get("sessionId"), params.get("targetInfo") or {})
            return
        if method == "Target.detachedFromTarget":
            self.injected.discard(params.get("sessionId") or "")
            return
        if method in ("Page.domContentEventFired", "Page.loadEventFired"):
            # The evaluate sent at attach time can land before the first
            # document exists; the script is idempotent, so run it again once
            # the document is there.
            session_id = message.get("sessionId")
            if session_id in self.injected:
                self._evaluate(session_id)
            return
        identifier = message.get("id")
        if identifier not in self.pending:
            return
        kind, session_id = self.pending.pop(identifier)
        if message.get("error"):
            if kind == "autoattach":
                # A protocol without the target filter: attach to everything
                # and sort the targets out as they arrive.
                self.pending[self.pipe.send("Target.setAutoAttach", dict(_AUTO_ATTACH))] = ("autoattach-unfiltered", None)
                return
            self.emit({"event": "error", "stage": kind, "message": message["error"].get("message", "")})
            return
        if kind == "evaluate":
            result = message.get("result") or {}
            exception = result.get("exceptionDetails")
            if exception:
                detail = (exception.get("exception") or {}).get("description") or exception.get("text", "")
                self.emit({"event": "error", "stage": "evaluate", "session": session_id, "message": str(detail)[:300],
                           "line": exception.get("lineNumber"), "column": exception.get("columnNumber")})
            else:
                self.emit({"event": "injected", "session": session_id, "result": (result.get("result") or {}).get("value")})

    def _evaluate(self, session_id: str) -> None:
        self.pending[self.pipe.send("Runtime.evaluate", {"expression": self.script, "returnByValue": True}, session_id=session_id)] = ("evaluate", session_id)

    def _attached(self, session_id, info: dict) -> None:
        if not session_id or session_id in self.injected:
            return
        if not self.wanted(info):
            self.pipe.send("Target.detachFromTarget", {"sessionId": session_id})
            return
        self.injected.add(session_id)
        self.pipe.send("Page.enable", session_id=session_id)
        self.pipe.send("Page.addScriptToEvaluateOnNewDocument", {"source": self.script, "runImmediately": True}, session_id=session_id)
        self._evaluate(session_id)


def _parked(fd: int) -> int:
    """Move a pipe end above the numbers the child's pipe ends will take.

    A fresh process hands out 3 and 4 first, and dup2(3, 3) is a no-op that
    keeps the close-on-exec flag, so the child would find fd 3 closed.
    """
    if fd >= 10:
        return fd
    moved = fcntl.fcntl(fd, fcntl.F_DUPFD_CLOEXEC, 10)
    os.close(fd)
    return moved


def launch(binary: Path, arguments=(), environment: dict | None = None) -> tuple[int, DevToolsPipe]:
    """Spawn the app in its own session with the DevTools pipe on its fds 3
    and 4 and its stdio detached, the way a launch-services launch has it."""
    command_read, command_write = (_parked(fd) for fd in os.pipe())
    event_read, event_write = (_parked(fd) for fd in os.pipe())
    argv = [str(binary), _LAUNCH_SWITCH, *arguments]
    actions = [(os.POSIX_SPAWN_OPEN, 0, os.devnull, os.O_RDONLY, 0),
               (os.POSIX_SPAWN_OPEN, 1, os.devnull, os.O_WRONLY, 0),
               (os.POSIX_SPAWN_OPEN, 2, os.devnull, os.O_WRONLY, 0),
               (os.POSIX_SPAWN_DUP2, command_read, 3),
               (os.POSIX_SPAWN_DUP2, event_write, 4)]
    env = environment if environment is not None else child_environment()
    try:
        try:
            pid = os.posix_spawn(str(binary), argv, env, file_actions=actions, setsid=True)
        except NotImplementedError:
            pid = os.posix_spawn(str(binary), argv, env, file_actions=actions, setpgroup=0)
    finally:
        os.close(command_read)
        os.close(event_write)
    return pid, DevToolsPipe(event_read, command_write)


def already_running(binary: Path) -> bool:
    """The app on macOS takes no single-instance lock: a second copy on the
    same profile must never be spawned.

    A LaunchServices launch runs the binary by its full path, which ps
    reports as the command; the full argument list is checked as well.
    """
    target = str(binary)
    try:
        listing = subprocess.run(["/bin/ps", "-axo", "pid=,comm=,command="], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return False
    own = str(os.getpid())
    for line in listing.stdout.splitlines():
        pid, _, rest = line.strip().partition(" ")
        if pid == own:
            continue
        rest = rest.strip()
        if rest.startswith(target + " ") or rest == target:
            return True
    return False


def run(binary: Path, script: str, *, emit=None, arguments=(), environment=None, poll_interval=0.25,
        launch_timeout=45.0) -> int:
    """Launch the app, install the watcher, then stay attached until it exits.

    Returns the app's exit status. SIGTERM and SIGHUP are ignored once the
    app runs, a status sink that fails (the hub quit and took the helper's
    stdout with it) is dropped, and a message the bridge cannot handle is
    reported rather than raised: this helper's exit would close the pipe,
    which is the app's cue to quit, so only the app's own exit ends it.
    """
    sink = emit or (lambda event: None)

    def emit(event):
        try:
            sink(event)
        except Exception:
            pass

    pid, pipe = launch(binary, arguments, environment)
    emit({"event": "launched", "pid": pid})
    for signum in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(signum, signal.SIG_IGN)
    bridge = AccentBridge(pipe, script, emit)
    started = time.monotonic()
    status = None
    try:
        try:
            bridge.start()
        except OSError as exc:
            # An app that exits at once (a running copy took the launch over,
            # or the switch was refused) closes the pipe before we can talk.
            emit({"event": "error", "stage": "start", "message": str(exc)})
        while True:
            try:
                messages = pipe.poll(poll_interval)
            except EOFError:
                break
            except OSError as exc:
                emit({"event": "error", "stage": "session", "message": str(exc)})
                break
            for message in messages:
                try:
                    bridge.handle(message)
                except Exception as exc:  # a surprise in the protocol must not end the helper
                    emit({"event": "error", "stage": "handle", "message": f"{type(exc).__name__}: {exc}"})
            done, raw = os.waitpid(pid, os.WNOHANG)
            if done:
                status = raw
                break
            if not bridge.injected and time.monotonic() - started > launch_timeout:
                emit({"event": "error", "stage": "attach", "message": "No window accepted the watcher in time."})
                started = float("inf")
        if status is None:
            _, status = os.waitpid(pid, 0)
    finally:
        pipe.close()
    code = os.waitstatus_to_exitcode(status) if status is not None else 0
    emit({"event": "exited", "status": code})
    return code


def _print_event(event: dict) -> None:
    try:
        print(json.dumps(event), flush=True)
    except (OSError, ValueError):
        pass  # stdout is gone with the hub; the app must not follow


def bridge_command(app_path: str, settings: dict, inventory: dict, emit=None, log_path: Path | None = None,
                   **run_options) -> int:
    """Worker entry point: `gateway.py codex-accent --app <bundle>`.

    Events go to stdout as JSON lines and, when a log path is given, to that
    file as well, so a session can be inspected after the hub drained stdout.
    """
    stream = None
    if log_path is not None:
        try:
            stream = log_path.open("w")
        except OSError:
            stream = None
    base_emit = emit or _print_event

    def emit(event):  # noqa: F811 - the caller's sink plus the log file
        try:
            base_emit(event)
        except Exception:
            pass
        if stream is not None:
            try:
                stream.write(json.dumps(event) + "\n")
                stream.flush()
            except (OSError, ValueError):
                pass

    try:
        bundle = Path(app_path)
        binary = executable_path(bundle)
        if already_running(binary):
            emit({"event": "error", "stage": "launch", "message": "The app is already running; quit it first."})
            return 2
        accents = accent_map(settings, inventory)
        glyphs = glyph_map(settings, inventory)
        emit({"event": "accents", "count": len(accents), "glyphs": len(glyphs)})
        run_options.setdefault("environment", child_environment(bundle))
        return run(binary, watcher_script(accents, glyphs=glyphs, assets=glyph_assets(glyphs.values())), emit=emit, **run_options)
    finally:
        if stream is not None:
            stream.close()


if __name__ == "__main__":  # pragma: no cover - manual smoke run
    sys.exit(run(executable_path(Path(sys.argv[1])), watcher_script({}), emit=_print_event,
                 environment=child_environment(Path(sys.argv[1]))))
