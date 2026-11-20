"""Provider accents and logos for the Vibe desktop app's picker and transcript.

Vibe (Mistral's Electron desktop app, ``ai.mistral.lechat.desktop``) draws
every model in its composer picker with one of its own pixel icons and fills
the selected row's radio with its orange checkbox token. Hub routes therefore
all look alike there, and its transcript's tool-call steps stay grey whatever
model ran them. This module gives them the Hub's presentation without touching
the app bundle, the way ``codex_accent`` does for the ChatGPT desktop app: the
app is spawned as a child of the worker with Chromium's
``--remote-debugging-pipe`` switch and a watcher script is installed in its
``lechat://app`` documents over that pipe. The pipe loop, the launch and the
LaunchServices-like environment are ``codex_accent``'s.

What the watcher keys on, observed in Vibe 1.3.0's renderer bundle
(``composer-model-controls`` and ``enable-connector-task-callback`` chunks):

* Picker rows are ``role="menuitem"`` elements holding the model icon (an
  ``svg.size-4.shrink-0``), the label and an ``<input type="radio">`` whose
  checked fill is its sibling ``<label>``. The radio's ``value`` prop never
  reaches the input (``AuraRadio`` keeps it), so the row's alias is read from
  its React fiber: ``ModelMenuItems`` keys each row by ``model.id``, which is
  the alias. The Hub writes each route as its own alias (``vibe_catalogue``),
  so a Hub row is matched exactly by route; the label is the fallback.
* The composer pill (``button.group/model-picker``) and the menu's "Model"
  sub-trigger show the selected model's label, which is the config row's
  display name, or ``"<display name> (<alias>)"`` when two rows share a name.
* Tool steps render ``div[data-step-state][data-step-has-tool-icon]`` around a
  ``span.text-icon-default-muted`` glyph (or a ``svg`` check mark, or a spinner
  whose top border carries the colour); a running step's title is an
  ``.aura-shimmer`` span. Thread items carry ``data-thread-item-key``, and a
  model switch inserts a divider (``span[data-position]``) naming the new
  model's label, so each stretch of the transcript takes the accent of the
  model that ran it. A notice for a model no longer configured names its raw
  ``provider/model`` route, which resolves through the provider alone.

Vibe-native rows (its own Mistral models, configured or served) take the
Hub's Mistral presentation; rows of another provider in the user's own Vibe
configuration take that provider's presentation when the Hub knows it.
Logos are downscaled to small PNG data URIs (``sips``) and cached in the Hub's
state directory, so the watcher needs nothing from the network or the disk.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

from branding import load_branding, resolve_presentation
from codex_accent import AccentBridge, already_running, child_environment, executable_path, run
from hub_config import split_route
from vibe_catalogue import PROVIDER_ID, project_vibe

#: The only documents the watcher runs in: Vibe's bundled app surfaces.
#: Its other windows (about, alerts, dictation) load ``lechat://./out/...``.
APP_ORIGIN = "lechat://app"
_APP_ORIGIN_PATTERN = r"^lechat:\/\/app(?:[\/?#]|$)"
#: The variable Vibe reads the gateway token from (vibe_profile.TOKEN_ENV).
#: The helper forwards exactly this one from its own environment.
TOKEN_ENV = "PROVIDER_HUB_VIBE_TOKEN"
#: Vibe's own provider name for its first-party models.
NATIVE_PROVIDER = "mistral"
_HEX = re.compile(r"^#[0-9A-Fa-f]{6}$")
#: Picker icons are 16 pt; 64 px covers a 2x display with room for scale.
LOGO_PIXELS = 64
#: A source logo this small is embedded as it is when ``sips`` is missing.
_LOGO_RAW_LIMIT = 48_000


def _logo_uri(relative: str, cache_dir: Path | None, logo_root: Path) -> str | None:
    """A ``data:`` URI for one provider logo, downscaled and cached."""
    source = (logo_root / relative).resolve()
    try:
        if logo_root.resolve() not in source.parents:
            return None
        raw = source.read_bytes()
    except OSError:
        return None
    digest = hashlib.sha256(raw).hexdigest()[:24]
    small = None
    if cache_dir is not None:
        cached = cache_dir / f"{digest}-{LOGO_PIXELS}.png"
        try:
            if cached.is_file():
                small = cached.read_bytes()
            else:
                cache_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
                temporary = cached.with_suffix(".tmp.png")
                subprocess.run(["/usr/bin/sips", "-Z", str(LOGO_PIXELS), "-s", "format", "png", str(source),
                                "--out", str(temporary)], check=True, capture_output=True, timeout=20)
                os.replace(temporary, cached)
                small = cached.read_bytes()
        except (OSError, subprocess.SubprocessError):
            small = None
    if small is None:
        if len(raw) > _LOGO_RAW_LIMIT:
            return None
        small = raw
    return "data:image/png;base64," + base64.b64encode(small).decode("ascii")


class _Logos:
    """Logo URIs keyed by source path, so a logo shared by many rows is
    embedded in the script once."""

    def __init__(self, cache_dir: Path | None, logo_root: Path):
        self.cache_dir = cache_dir
        self.logo_root = logo_root
        self.uris: dict[str, str] = {}
        self.missing: set[str] = set()

    def key(self, relative) -> str | None:
        if not isinstance(relative, str) or not relative or relative in self.missing:
            return None
        if relative not in self.uris:
            uri = _logo_uri(relative, self.cache_dir, self.logo_root)
            if uri is None:
                self.missing.add(relative)
                return None
            self.uris[relative] = uri
        return relative


def _entry(presentation: dict, logos: _Logos) -> dict | None:
    """The watcher's view of one presentation: accent, logo keys, provider."""
    accent = presentation.get("accent")
    if not isinstance(accent, str) or not _HEX.match(accent):
        return None
    entry = {"accent": accent.upper(), "provider": str(presentation.get("displayProvider") or "")[:60]}
    logo = presentation.get("logo") or {}
    light = logos.key(logo.get("light"))
    if light:
        dark = logos.key(logo.get("dark")) or light
        scale = logo.get("scale")
        entry["logo"] = {"light": light, "dark": dark,
                         "scale": float(scale) if isinstance(scale, (int, float)) and 0.5 <= scale <= 2 else 1.0,
                         "template": logo.get("template") is True}
    return entry


def _vibe_rows(config_path: Path) -> list[dict]:
    """The ``[[models]]`` rows of Vibe's own configuration, read-only."""
    try:
        sys.path.insert(0, str(Path(__file__).with_name("vendor")))
        import tomlkit
        document = tomlkit.parse(config_path.read_text(encoding="utf-8")).unwrap()
    except Exception:
        return []
    rows = document.get("models")
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def accent_catalogue(settings: dict, inventory: dict, *, vibe_config: Path | None = None,
                     cache_dir: Path | None = None, logo_root: Path | None = None) -> dict:
    """Everything the watcher needs, keyed the way Vibe's DOM exposes it.

    ``routes`` maps a picker alias to its entry; ``labels`` maps the labels
    Vibe may show for a row (display name, and ``"name (alias)"`` for shared
    names) to that alias; ``native`` is the entry for Vibe's own models that
    no configuration row describes; ``foreign`` lists aliases the watcher
    must leave alone; ``logos`` holds the embedded images.
    """
    logos = _Logos(cache_dir, logo_root or Path(__file__).resolve().parent)
    overrides = settings.get("branding_overrides")
    known_providers = set((load_branding().get("providers") or {}).keys())
    entries = {entry.get("id"): entry for entry in inventory.get("models", []) if isinstance(entry, dict)}
    routes: dict[str, dict] = {}
    labels: dict[str, str] = {}
    foreign: list[str] = []

    def label(text, alias):
        if isinstance(text, str) and text.strip() and alias:
            labels.setdefault(text.strip(), alias)
            labels.setdefault(f"{text.strip()} ({alias})", alias)

    for row in project_vibe(settings, inventory)["models"]:
        route = row["route"]
        presentation = (entries.get(route) or {}).get("presentation")
        if not isinstance(presentation, dict) or "accent" not in presentation:
            provider, model = split_route(route)
            presentation = resolve_presentation(provider, model, overrides)
        entry = _entry(presentation, logos)
        if entry is None:
            continue
        routes[row["alias"]] = entry
        label(row["display_name"], row["alias"])

    if vibe_config is not None:
        for row in _vibe_rows(vibe_config):
            alias, provider = row.get("alias"), row.get("provider")
            if not isinstance(alias, str) or not alias or provider == PROVIDER_ID or alias in routes:
                continue
            if provider not in known_providers:
                foreign.append(alias)
                label(row.get("display_name") or alias, alias)
                continue
            name = row.get("name") if isinstance(row.get("name"), str) else alias
            try:
                presentation = resolve_presentation(provider, name, overrides)
            except Exception:
                foreign.append(alias)
                continue
            entry = _entry(presentation, logos)
            if entry is None:
                foreign.append(alias)
                continue
            routes[alias] = entry
            label(row.get("display_name") or alias, alias)

    native = _entry(resolve_presentation(NATIVE_PROVIDER, None, overrides), logos)
    # Provider-level entries for "provider/model" routes the catalogue no
    # longer lists (old "Model changed to" notices name them raw). Only the
    # providers this Hub is set up for, to keep the script small.
    provider_ids = {split_route(entry["id"])[0] for entry in entries.values()
                    if isinstance(entry.get("id"), str) and "/" in entry["id"]}
    provider_ids |= {key for key in (settings.get("providers") or {}) if isinstance(key, str)}
    providers = {}
    for provider in sorted(provider_ids & known_providers):
        try:
            entry = _entry(resolve_presentation(provider, None, overrides), logos)
        except Exception:
            entry = None
        if entry is not None:
            providers[provider] = entry
    return {"routes": routes, "labels": labels, "native": native, "providers": providers,
            "foreign": sorted(set(foreign)), "logos": logos.uris}


_WATCHER = r"""
(() => {
  const DATA = __DATA__;
  if (!new RegExp(__ORIGIN__).test(String(location.href))) return { installed: false };
  const existing = window.__providerHubVibe;
  if (existing) { existing.update(DATA); return existing.status(); }

  const LOGO = 'data-provider-hub-logo';
  const ITEM = 'data-provider-hub-item';
  const TURN = 'data-provider-hub-turn';
  const EFFORT = 'data-provider-hub-effort';
  const STYLE_ID = 'provider-hub-vibe-accent';
  const ICON = 'svg[class~="size-4"][class~="shrink-0"]:not([data-icon-asset="thinking"]):not([class~="absolute"])';
  const TRIGGER = 'button[class~="group/model-picker"]';
  const SUB_TRIGGER = '[role="menuitem"][aria-haspopup]';
  const STYLE = `
    svg[${LOGO}] { background-image: var(--provider-hub-logo-light) !important; background-repeat: no-repeat !important;
      background-position: center !important; background-size: var(--provider-hub-logo-size, 100%) !important; border-radius: 3px; }
    :is(.dark, [data-theme="dark"]) svg[${LOGO}] { background-image: var(--provider-hub-logo-dark) !important; }
    svg[${LOGO}] > * { opacity: 0 !important; }
    svg[${LOGO}="template"] { background-image: none !important; background-color: var(--provider-hub-accent) !important;
      -webkit-mask: var(--provider-hub-logo-light) center / var(--provider-hub-logo-size, 100%) no-repeat;
      mask: var(--provider-hub-logo-light) center / var(--provider-hub-logo-size, 100%) no-repeat; border-radius: 0; }
    svg[${LOGO}="tint"] { background: none !important; color: var(--provider-hub-accent) !important; }
    svg[${LOGO}="tint"] > * { opacity: 1 !important; }
    [${ITEM}] :has(> input[type="radio"]:checked) > label,
    [${ITEM}] input[type="radio"]:checked ~ label { background-color: var(--provider-hub-accent) !important; }
    [${EFFORT}] { color: var(--provider-hub-accent) !important; }
    [${TURN}] [data-step-state] > span[class~="text-icon-default-muted"],
    [${TURN}] [data-step-state] > svg[class~="text-icon-default-muted"] { color: var(--provider-hub-turn-accent) !important; }
    [${TURN}] [data-step-state] > span[class~="animate-spin"] { border-top-color: var(--provider-hub-turn-accent) !important; }
    [${TURN}] .aura-shimmer { color: color-mix(in oklab, var(--provider-hub-turn-accent) 62%, var(--text-default, currentColor)) !important; }
  `;

  let data = DATA;
  let index = [];
  const learned = new Map();
  const stats = { items: 0, icons: 0, turns: 0, composer: null };

  const clean = (text) => String(text || '').replace(/\s+/g, ' ').trim();
  function reindex() {
    const pairs = new Map(Object.entries(data.labels || {}));
    for (const [label, alias] of learned) if (!pairs.has(label)) pairs.set(label, alias);
    index = [...pairs].filter(([label]) => label.length > 1).sort((a, b) => b[0].length - a[0].length);
  }
  const NATIVE = '__native__';
  const ROUTE = /(?:^|[\s(])([a-z0-9][a-z0-9-]*)\/[A-Za-z0-9][\w.:-]*/;
  function entryFor(alias) {
    if (!alias) return null;
    if (data.routes[alias]) return data.routes[alias];
    if (alias.startsWith('provider:')) return (data.providers || {})[alias.slice(9)] || null;
    if ((data.foreign || []).includes(alias)) return null;
    return data.native || null;
  }
  function aliasInText(text) {
    const value = clean(text);
    if (!value) return null;
    for (const [label, alias] of index) if (value.includes(label)) return alias;
    // A route the catalogue no longer lists still names its provider.
    const route = value.match(ROUTE);
    if (route && (data.providers || {})[route[1]]) return 'provider:' + route[1];
    return null;
  }
  // ModelMenuItems keys each row by model.id, the alias; the radio's value
  // prop never reaches the DOM. Walk up from the row's host fiber to the
  // first keyed fiber: in Vibe 1.3.0 that is the DropdownMenuItem fiber,
  // 15 levels above the row's div (Radix wraps it in slots and providers).
  function fiberKey(element) {
    const name = Object.keys(element).find((key) => key.startsWith('__reactFiber$'));
    let fiber = name ? element[name] : null;
    for (let depth = 0; fiber && depth < 24; depth += 1, fiber = fiber.return) {
      if (typeof fiber.key === 'string' && fiber.key) return fiber.key;
    }
    return null;
  }
  function setProperty(element, name, value) {
    if (element.style.getPropertyValue(name) !== value) element.style.setProperty(name, value);
  }
  function setAttribute(element, name, value) {
    if (element.getAttribute(name) !== value) element.setAttribute(name, value);
  }
  function unpaint(element, attribute, properties) {
    if (!element.hasAttribute(attribute)) return;
    element.removeAttribute(attribute);
    for (const name of properties) element.style.removeProperty(name);
  }
  const LOGO_PROPERTIES = ['--provider-hub-logo-light', '--provider-hub-logo-dark', '--provider-hub-logo-size', '--provider-hub-accent'];
  function paintIcon(svg, entry) {
    if (!svg) return;
    if (!entry) { unpaint(svg, LOGO, LOGO_PROPERTIES); return; }
    setProperty(svg, '--provider-hub-accent', entry.accent);
    const logo = entry.logo && data.logos[entry.logo.light];
    if (!logo) { setAttribute(svg, LOGO, 'tint'); return; }
    const dark = data.logos[entry.logo.dark] || logo;
    setProperty(svg, '--provider-hub-logo-light', `url("${logo}")`);
    setProperty(svg, '--provider-hub-logo-dark', `url("${dark}")`);
    setProperty(svg, '--provider-hub-logo-size', `${Math.round((entry.logo.scale || 1) * 100)}%`);
    setAttribute(svg, LOGO, entry.logo.template ? 'template' : 'image');
    stats.icons += 1;
  }

  function paintMenu() {
    for (const input of document.querySelectorAll('[role^="menuitem"] input[type="radio"]')) {
      const item = input.closest('[role^="menuitem"]');
      const icon = item && item.querySelector(ICON);
      // Thinking-level rows carry a radio too, but no model icon.
      if (!item || !icon) continue;
      const label = clean(item.querySelector('[class~="truncate"]')?.textContent);
      const key = fiberKey(item);
      const known = (alias) => alias && (data.routes[alias] || (data.foreign || []).includes(alias));
      // A row nobody configured is one of Vibe's own served models.
      const alias = known(key) ? key : (aliasInText(label) || NATIVE);
      const entry = entryFor(alias);
      if (label && learned.get(label) !== alias) { learned.set(label, alias); reindex(); }
      paintIcon(icon, entry);
      if (entry) { setAttribute(item, ITEM, alias); setProperty(item, '--provider-hub-accent', entry.accent); stats.items += 1; }
      else unpaint(item, ITEM, ['--provider-hub-accent']);
    }
  }

  function paintSelected() {
    let composer = null;
    for (const host of document.querySelectorAll(`${TRIGGER}, ${SUB_TRIGGER}`)) {
      const icon = host.querySelector(ICON);
      if (!icon) continue;
      const alias = aliasInText(host.textContent);
      if (host.matches(TRIGGER) && alias && composer === null) composer = alias;
      paintIcon(icon, alias ? entryFor(alias) : null);
    }
    stats.composer = composer;
    return composer;
  }

  // The effort level ("Low", "High") is an AuraTypography span (class
  // text-subtle) in the pill's label span, after the model label's text and
  // an optional "Preview" span of the same kind: [model, preview?, " · Low"].
  // Only the span that opens with the " · " separator (or stands alone when
  // the pill shows no model) is the level. The menu's "Thinking" sub-trigger
  // holds it as the last span of a flex-col span (text-xs, text-muted); the
  // sub-menu's rows carry a radio but no model icon. The menu is portalled,
  // so all of these take the composer model's accent.
  function paintEffort(composer) {
    const entry = composer ? entryFor(composer) : null;
    const targets = new Set();
    if (entry) {
      for (const host of document.querySelectorAll(TRIGGER)) {
        const label = host.querySelector('span[class~="hidden"]');
        const level = label && label.lastElementChild;
        if (level && level.tagName === 'SPAN' && level.className.includes('text-subtle')
            && (label.childNodes.length === 1 || clean(level.textContent).startsWith('\u00b7'))) targets.add(level);
      }
      for (const host of document.querySelectorAll(SUB_TRIGGER)) {
        if (!host.querySelector('svg[data-icon-asset="thinking"]')) continue;
        const level = host.querySelector('span[class~="flex-col"] > span:last-child');
        if (level && level.children.length === 0) targets.add(level);
      }
    }
    for (const span of targets) { setAttribute(span, EFFORT, composer); setProperty(span, '--provider-hub-accent', entry.accent); }
    for (const span of document.querySelectorAll(`[${EFFORT}]`)) if (!targets.has(span)) unpaint(span, EFFORT, ['--provider-hub-accent']);
    // The thinking sub-menu's checked radio. Other composer menus (approval
    // policy) also hold icon-less radio rows, so only while the Thinking
    // sub-trigger reports its sub-menu open (Radix data-state="open").
    const thinkingOpen = [...document.querySelectorAll(SUB_TRIGGER)].some((host) =>
      host.getAttribute('data-state') === 'open' && host.querySelector('svg[data-icon-asset="thinking"]'));
    for (const input of document.querySelectorAll('[role^="menuitem"] input[type="radio"]')) {
      const item = input.closest('[role^="menuitem"]');
      if (!item || item.querySelector(ICON)) continue;
      if (entry && thinkingOpen) { setAttribute(item, ITEM, composer); setProperty(item, '--provider-hub-accent', entry.accent); }
      else unpaint(item, ITEM, ['--provider-hub-accent']);
    }
  }

  function paintTurns(composer) {
    const items = [...document.querySelectorAll('[data-thread-item-key]')];
    const notices = items.map((item) => {
      for (const divider of item.querySelectorAll('span[data-position]')) {
        const alias = aliasInText(divider.textContent);
        if (alias) return alias;
      }
      return null;
    });
    // Before the first "Model changed to" notice the model is the one the
    // session started on, which the page no longer names; with no notice at
    // all it is the composer's.
    let current = notices.some(Boolean) ? null : composer;
    let painted = 0;
    items.forEach((item, position) => {
      if (notices[position]) current = notices[position];
      const entry = entryFor(current);
      if (entry) {
        setAttribute(item, TURN, current);
        setProperty(item, '--provider-hub-turn-accent', entry.accent);
        painted += 1;
      } else unpaint(item, TURN, ['--provider-hub-turn-accent']);
    });
    stats.turns = painted;
  }

  function paint() {
    stats.items = 0; stats.icons = 0;
    try {
      if (!document.getElementById(STYLE_ID) && document.head) {
        const style = document.createElement('style');
        style.id = STYLE_ID;
        style.textContent = STYLE;
        document.head.appendChild(style);
      }
      paintMenu();
      const composer = paintSelected();
      paintEffort(composer);
      paintTurns(composer);
    } catch (error) {
      stats.error = String(error && error.message || error).slice(0, 200);
    }
  }

  let scheduled = false;
  function schedule() {
    if (scheduled) return;
    scheduled = true;
    requestAnimationFrame(() => { scheduled = false; paint(); });
  }
  const observer = new MutationObserver(schedule);
  function observe() {
    const root = document.documentElement;
    if (!root) return false;
    observer.observe(root, { childList: true, subtree: true, characterData: true,
                             attributes: true, attributeFilter: ['aria-checked', 'data-state', 'value', 'class'] });
    return true;
  }
  if (!observe()) document.addEventListener('DOMContentLoaded', () => { observe(); schedule(); }, { once: true });
  reindex();
  paint();

  window.__providerHubVibe = {
    update(next) { data = next; reindex(); paint(); },
    status() { return { installed: true, routes: Object.keys(data.routes).length, logos: Object.keys(data.logos).length,
                        providers: Object.keys(data.providers || {}).length, native: Boolean(data.native), ...stats }; },
  };
  return window.__providerHubVibe.status();
})()
"""


def watcher_script(catalogue: dict) -> str:
    """The watcher, with the catalogue embedded as a JSON literal."""
    data = json.dumps(catalogue, separators=(",", ":"), sort_keys=True).replace("</", "<\\/")
    return (_WATCHER.replace("__ORIGIN__", json.dumps(_APP_ORIGIN_PATTERN))
            .replace("__DATA__", data))


class VibeBridge(AccentBridge):
    """``AccentBridge`` admitting Vibe's ``lechat://app`` documents.

    Every other target (its about, alert and dictation windows, embedded
    sites, workers) is detached at once; the script checks the origin again
    on every document a kept target loads.
    """

    @staticmethod
    def wanted(info: dict) -> bool:
        url = info.get("url") or ""
        return info.get("type") == "page" and (url in ("", "about:blank") or bool(re.match(_APP_ORIGIN_PATTERN, url)))


def vibe_environment(bundle: Path) -> dict:
    """A LaunchServices-like environment plus the gateway token, nothing else."""
    environment = child_environment(bundle)
    token = os.environ.get(TOKEN_ENV)
    if token:
        environment[TOKEN_ENV] = token
    return environment


def _print_event(event: dict) -> None:
    try:
        print(json.dumps(event), flush=True)
    except (OSError, ValueError):
        pass  # stdout is gone with the hub; the app must not follow


def bridge_command(app_path: str, settings: dict, inventory: dict, emit=None, log_path: Path | None = None,
                   state_root: Path | None = None, vibe_config: Path | None = None, **run_options) -> int:
    """Worker entry point: ``gateway.py vibe-accent --app <bundle>``.

    Long-lived like ``codex-accent``: the exit status is the app's, and events
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
            emit({"event": "error", "stage": "launch", "message": "Vibe is already running; quit it first."})
            return 2
        if vibe_config is None:
            from vibe_profile import vibe_home
            vibe_config = vibe_home() / "config.toml"
        cache_dir = state_root / "vibe-logos" if state_root is not None else None
        catalogue = accent_catalogue(settings, inventory, vibe_config=vibe_config, cache_dir=cache_dir)
        emit({"event": "accents", "routes": len(catalogue["routes"]), "logos": len(catalogue["logos"]),
              "native": catalogue["native"] is not None})
        run_options.setdefault("environment", vibe_environment(bundle))
        run_options.setdefault("bridge_factory", VibeBridge)
        return run(binary, watcher_script(catalogue), emit=emit, **run_options)
    finally:
        if stream is not None:
            stream.close()
