"""Provider accents and logos for the Antigravity desktop app.

Antigravity (Google's Electron app, ``com.google.antigravity``) lists every
model, Google's own and the Hub's gateway routes alike, as plain text rows,
and its tool-call groups stay grey. This module gives them the Hub's
presentation the way ``vibe_accent`` does for Vibe: the app is spawned as a
child of the worker with Chromium's ``--remote-debugging-pipe`` switch (the
pipe loop is ``codex_accent``'s) and a watcher script is installed in its
windows. The watcher only sets attributes, custom properties and one
stylesheet; it never reads cookies, storage or network traffic.

Antigravity's UI is not in its app bundle: the React app is embedded in its
``language_server`` binary and served from ``https://127.0.0.1:<port>/``,
a port that changes per launch (and when the language server restarts, which
reloads the windows). What the watcher keys on, observed in 2.33.1, are the
app's authored ``data-testid`` hooks:

* Picker rows: ``[data-testid=model-selector-item][data-model-label]``, or a
  ``role=menuitem`` whose ``[data-testid=model-selector-effort-group]`` span
  carries ``data-model-base`` for models with an effort submenu. Rows reserve
  a leading-icon slot, so the logo is a ``::before`` box; no node is added.
* The composer pill: ``[data-testid=model-selector-trigger]`` with
  ``aria-label="Select model, current: <label> <effort>"``; its effort word is
  the ``span.opacity-70`` inside the label span, which takes the accent.
* Tool-call groups: ``[data-testid=tool-group-collapsible]`` and
  ``[data-testid=worked-for-collapsible]`` (chevron svg, running label in
  ``.shimmer-text-content``). No turn records its model in the page, so these
  take the accent of the model the composer has selected.

Hub rows are matched by the labels the Hub itself projects into the gateway
catalogue (``antigravity_catalogue``). Google's own rows take the Hub's
presentation for the maker their name gives away (Claude, GPT, Gemini), and
anything else takes Antigravity's.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

from antigravity_catalogue import project_antigravity
from branding import resolve_presentation
from codex_accent import AccentBridge, already_running, child_environment, executable_path, run
from hub_config import split_route
from vibe_accent import _Logos, _entry

#: Antigravity serves its windows from its language server on loopback.
_APP_ORIGIN_PATTERN = r"^https:\/\/127\.0\.0\.1:\d+(?:[\/?#]|$)"
#: The gateway connection the Hub hands Antigravity. The helper forwards
#: exactly these from its own environment and nothing else of the Hub's.
GATEWAY_ENV = ("AGY_LLM_GATEWAY_URL", "AGY_LLM_GATEWAY_API_KEY", "AGY_LLM_GATEWAY_WIRE_PROTOCOL")
#: Google-served rows, by the maker their name gives away. Checked in order;
#: the first match wins and anything else is Antigravity's own.
NATIVE_BRANDS = (
    ("claude", r"\b(claude|opus|sonnet|haiku|fable)\b"),
    ("openai", r"\b(gpt|o\d)\b"),
    ("gemini", r"\bgemini\b"),
)
NATIVE_PROVIDER = "antigravity"


def accent_catalogue(settings: dict, inventory: dict, *, cache_dir: Path | None = None,
                     logo_root: Path | None = None) -> dict:
    """Labels -> entries for the Hub's rows, native brand rules, and logos."""
    logos = _Logos(cache_dir, logo_root or Path(__file__).resolve().parent)
    overrides = settings.get("branding_overrides")
    entries = {entry.get("id"): entry for entry in inventory.get("models", []) if isinstance(entry, dict)}
    labels: dict[str, dict] = {}
    for row in project_antigravity(settings, inventory)["models"]:
        route = row["route"]
        presentation = (entries.get(route) or {}).get("presentation")
        if not isinstance(presentation, dict) or "accent" not in presentation:
            provider, model = split_route(route)
            presentation = resolve_presentation(provider, model, overrides)
        entry = _entry(presentation, logos)
        if entry is not None:
            labels.setdefault(row["display_name"], entry)
    brands = []
    for provider, pattern in NATIVE_BRANDS:
        entry = _entry(resolve_presentation(provider, None, overrides), logos)
        if entry is not None:
            brands.append({"pattern": pattern, "entry": entry})
    native = _entry(resolve_presentation(NATIVE_PROVIDER, None, overrides), logos)
    return {"labels": labels, "brands": brands, "native": native, "logos": logos.uris}


_WATCHER = r"""
(() => {
  const DATA = __DATA__;
  if (!new RegExp(__ORIGIN__).test(String(location.href))) return { installed: false };
  const existing = window.__providerHubAntigravity;
  if (existing) { existing.update(DATA); return existing.status(); }

  const MARK = 'data-provider-hub-agy';
  const THEME = 'data-provider-hub-agy-theme';
  const STYLE_ID = 'provider-hub-agy-accent';
  const PILL = '[data-testid="model-selector-trigger"]';
  const LOGO_BOX = `content: ''; flex: none; width: 16px; height: 16px; border-radius: 3px; margin-inline-end: 6px;
      background-image: var(--provider-hub-logo-light); background-repeat: no-repeat; background-position: center;
      background-size: var(--provider-hub-logo-size, 100%);`;
  const STYLE = `
    [role="menuitem"][${MARK}="image"]::before, ${PILL}[${MARK}="image"]::before { ${LOGO_BOX} }
    ${PILL}[${MARK}="image"]::before { width: 14px; height: 14px; margin-inline-end: 2px; }
    :is(.dark, .theme-dark) :is([role="menuitem"], ${PILL})[${MARK}="image"]::before { background-image: var(--provider-hub-logo-dark); }
    :is([role="menuitem"], ${PILL})[${MARK}="template"]::before { ${LOGO_BOX} background: var(--provider-hub-accent);
      -webkit-mask: var(--provider-hub-logo-light) center / var(--provider-hub-logo-size, 100%) no-repeat; }
    :is([role="menuitem"], ${PILL})[${MARK}="dot"]::before { content: ''; flex: none; width: 8px; height: 8px; border-radius: 50%;
      margin-inline: 4px 8px; background: var(--provider-hub-accent); }
    ${PILL}[${MARK}] > span > span[class~="opacity-70"] { color: var(--provider-hub-accent) !important; opacity: 1 !important; }
    :root[${THEME}] :is([data-testid="tool-group-collapsible"], [data-testid="worked-for-collapsible"]) > svg {
      color: var(--provider-hub-agy-accent) !important; }
    :root[${THEME}] [data-testid="tool-group-collapsible"] .shimmer-text-content {
      color: color-mix(in oklab, var(--provider-hub-agy-accent) 65%, currentColor) !important; }
  `;

  let data = DATA;
  let brands = [];
  let labels = [];
  const stats = { rows: 0, pill: null, theme: null };

  const clean = (text) => String(text || '').replace(/\s+/g, ' ').trim();
  function rebuild() {
    brands = (data.brands || []).map((rule) => ({ pattern: new RegExp(rule.pattern, 'i'), entry: rule.entry }));
    labels = Object.keys(data.labels || {}).sort((a, b) => b.length - a.length);
  }
  function entryForLabel(label) {
    const value = clean(label);
    if (!value) return null;
    if (data.labels[value]) return data.labels[value];
    for (const rule of brands) if (rule.pattern.test(value)) return rule.entry;
    return data.native || null;
  }
  // The pill's label is "<model label> <effort>": the longest Hub label it
  // starts with wins, otherwise the text before the effort word.
  function entryForPill(pill) {
    const label = clean((pill.getAttribute('aria-label') || '').replace(/^[^:]*:\s*/, ''));
    for (const known of labels) if (label === known || label.startsWith(known + ' ')) return data.labels[known];
    const own = clean(pill.querySelector('span')?.firstChild?.textContent);
    return entryForLabel(own || label);
  }
  function setProperty(element, name, value) {
    if (element.style.getPropertyValue(name) !== value) element.style.setProperty(name, value);
  }
  function setAttribute(element, name, value) {
    if (element.getAttribute(name) !== value) element.setAttribute(name, value);
  }
  function paint(element, entry) {
    if (!entry) {
      if (element.hasAttribute(MARK)) {
        element.removeAttribute(MARK);
        for (const name of ['--provider-hub-accent', '--provider-hub-logo-light', '--provider-hub-logo-dark', '--provider-hub-logo-size'])
          element.style.removeProperty(name);
      }
      return;
    }
    setProperty(element, '--provider-hub-accent', entry.accent);
    const logo = entry.logo && data.logos[entry.logo.light];
    if (!logo) { setAttribute(element, MARK, 'dot'); return; }
    setProperty(element, '--provider-hub-logo-light', `url("${logo}")`);
    setProperty(element, '--provider-hub-logo-dark', `url("${data.logos[entry.logo.dark] || logo}")`);
    setProperty(element, '--provider-hub-logo-size', `${Math.round((entry.logo.scale || 1) * 100)}%`);
    setAttribute(element, MARK, entry.logo.template ? 'template' : 'image');
  }

  function repaint() {
    try {
      if (!document.getElementById(STYLE_ID) && document.head) {
        const style = document.createElement('style');
        style.id = STYLE_ID;
        style.textContent = STYLE;
        document.head.appendChild(style);
      }
      let rows = 0;
      for (const item of document.querySelectorAll('[role="menuitem"]')) {
        const own = item.matches('[data-testid="model-selector-item"]') ? item.getAttribute('data-model-label') : null;
        const base = own === null ? item.querySelector('[data-testid="model-selector-effort-group"][data-model-base]')?.getAttribute('data-model-base') : null;
        const label = own ?? base;
        if (label === null || label === undefined) continue;
        paint(item, entryForLabel(label));
        rows += 1;
      }
      stats.rows = rows;
      const pill = document.querySelector(PILL);
      const entry = pill ? entryForPill(pill) : null;
      if (pill) paint(pill, entry);
      stats.pill = entry ? entry.accent : null;
      const root = document.documentElement;
      if (entry) {
        setAttribute(root, THEME, entry.accent);
        setProperty(root, '--provider-hub-agy-accent', entry.accent);
      } else if (root.hasAttribute(THEME)) {
        root.removeAttribute(THEME);
        root.style.removeProperty('--provider-hub-agy-accent');
      }
      stats.theme = entry ? entry.accent : null;
    } catch (error) {
      stats.error = String(error && error.message || error).slice(0, 200);
    }
  }

  let scheduled = false;
  function schedule() {
    if (scheduled) return;
    scheduled = true;
    requestAnimationFrame(() => { scheduled = false; repaint(); });
  }
  const observer = new MutationObserver(schedule);
  function observe() {
    const root = document.documentElement;
    if (!root) return false;
    observer.observe(root, { childList: true, subtree: true, characterData: true,
                             attributes: true, attributeFilter: ['aria-label', 'data-model-label', 'data-model-base', 'class'] });
    return true;
  }
  if (!observe()) document.addEventListener('DOMContentLoaded', () => { observe(); schedule(); }, { once: true });
  rebuild();
  repaint();

  window.__providerHubAntigravity = {
    update(next) { data = next; rebuild(); repaint(); },
    status() { return { installed: true, labels: labels.length, brands: brands.length, logos: Object.keys(data.logos).length,
                        native: Boolean(data.native), ...stats }; },
  };
  return window.__providerHubAntigravity.status();
})()
"""


def watcher_script(catalogue: dict) -> str:
    """The watcher, with the catalogue embedded as a JSON literal."""
    data = json.dumps(catalogue, separators=(",", ":"), sort_keys=True).replace("</", "<\\/")
    return (_WATCHER.replace("__ORIGIN__", json.dumps(_APP_ORIGIN_PATTERN))
            .replace("__DATA__", data))


class AntigravityBridge(AccentBridge):
    """``AccentBridge`` admitting Antigravity's loopback windows.

    Built-in browser tabs are ``webview`` targets and are detached at once;
    the script checks the origin again on every document a kept page loads.
    """

    @staticmethod
    def wanted(info: dict) -> bool:
        url = info.get("url") or ""
        return info.get("type") == "page" and (url in ("", "about:blank") or bool(re.match(_APP_ORIGIN_PATTERN, url)))


def antigravity_environment(bundle: Path) -> dict:
    """A LaunchServices-like environment plus the gateway connection only."""
    environment = child_environment(bundle)
    for key in GATEWAY_ENV:
        value = os.environ.get(key)
        if value:
            environment[key] = value
    return environment


def _print_event(event: dict) -> None:
    try:
        print(json.dumps(event), flush=True)
    except (OSError, ValueError):
        pass  # stdout is gone with the hub; the app must not follow


def bridge_command(app_path: str, settings: dict, inventory: dict, emit=None, log_path: Path | None = None,
                   state_root: Path | None = None, **run_options) -> int:
    """Worker entry point: ``gateway.py antigravity-accent --app <bundle>``.

    Long-lived like ``vibe-accent``: the exit status is the app's, and events
    go to stdout as JSON lines and to ``log_path`` when given.
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
            emit({"event": "error", "stage": "launch", "message": "Antigravity is already running; quit it first."})
            return 2
        cache_dir = state_root / "accent-logos" if state_root is not None else None
        catalogue = accent_catalogue(settings, inventory, cache_dir=cache_dir)
        emit({"event": "accents", "labels": len(catalogue["labels"]), "logos": len(catalogue["logos"]),
              "native": catalogue["native"] is not None})
        run_options.setdefault("environment", antigravity_environment(bundle))
        run_options.setdefault("bridge_factory", AntigravityBridge)
        # Antigravity starts its language server before its first window.
        run_options.setdefault("launch_timeout", 90.0)
        return run(binary, watcher_script(catalogue), emit=emit, **run_options)
    finally:
        if stream is not None:
            stream.close()
