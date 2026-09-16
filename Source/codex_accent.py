"""Codex power-slider accents.

The ChatGPT desktop app paints its model picker's power slider with one
app-wide design token (``--color-chart-blue``); its model records carry no
colour, and it offers no theming hook. This module gives each hub model its
provider's accent anyway without touching the app bundle: the app is started
as a child of the worker with Chromium's ``--remote-debugging-pipe`` switch,
and a small watcher script is injected into its windows over that pipe.

The pipe is a pair of file descriptors only this helper holds, so nothing
listens on a port. The watcher only reads the picker's own labels and sets
CSS custom properties on the picker. Ultra, which the app paints with its
purple token, takes a more saturated cut of the same provider hue instead,
and its word gets a shimmer sweep. With the Codex tab's banner switch on,
the same stylesheet also hides the app's ChatGPT usage banner.

The pipe is also the app's lifeline: Electron quits when it closes. So the
helper ignores termination signals, never lets a failed status write or a
malformed message end it, and hands the app a launch-services-like
environment and detached stdio so nothing of the hub's reaches it.
"""
from __future__ import annotations

import fcntl
import json
import math
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
THEME_ATTRIBUTE = "data-provider-hub-theme"
ACCENT_PROPERTY = "--provider-hub-accent"
# The activity shimmer's gray takes the model's hue: its base colour becomes
# the app's own gray, at its own lightness, with this much OKLCH chroma in
# the accent's hue, which the watcher publishes on the root in degrees. An
# accent with less chroma than the floor is a gray itself and lends none.
HUE_PROPERTY = "--provider-hub-hue"
SHIMMER_CHROMA = "0.07"
SHIMMER_HUE_FLOOR = 0.03
# Ultra: the app paints its top level (the popover's title, the slider's
# fill gradient, the pill's Ultra layer) with one purple token. On the
# picker and the pill that token is given the model's Ultra hue instead,
# and the elements carrying the word are marked for the sweep.
ULTRA_PROPERTY = "--color-chart-purple"
ULTRA_ACCENT_PROPERTY = "--provider-hub-ultra"
ULTRA_MARK = "data-provider-hub-ultra"
# The Ultra hue is the accent with its OKLCH chroma raised by this factor
# (clamped to the sRGB gamut) and its lightness moved away from the surface
# by this much: up on a dark theme, down on a light one. Many accents
# already sit at the gamut edge for their lightness, so the lightness step
# is what keeps "more intense" visible for every provider.
ULTRA_CHROMA_GAIN = 1.5
ULTRA_LIGHTNESS_SHIFT = 0.05
ULTRA_SWEEP = "3.2s"
# The ChatGPT usage banner ("You're out of Codex and Work usage", and the
# per-model "out of usage" variant) is the app's generic banner, an <aside>
# with utility classes only, no role and localised text. What both share is
# the gauge icon, whose path starts with this; nothing else in the app puts
# that icon inside an <aside>.
USAGE_BANNER_ICON = "M10.8343 12.0693"
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


def _srgb_to_oklch(colour: str) -> tuple[float, float, float]:
    channels = [int(colour[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    r, g, b = (c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in channels)
    l = (0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b) ** (1 / 3)
    m = (0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b) ** (1 / 3)
    s = (0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b) ** (1 / 3)
    lightness = 0.2104542553 * l + 0.7936177850 * m - 0.0040720468 * s
    a = 1.9779984951 * l - 2.4285922050 * m + 0.4505937099 * s
    b2 = 0.0259040371 * l + 0.7827717662 * m - 0.8086757660 * s
    return lightness, math.hypot(a, b2), math.atan2(b2, a)


def _oklch_to_srgb(lightness: float, chroma: float, hue: float) -> tuple[float, float, float]:
    a, b = chroma * math.cos(hue), chroma * math.sin(hue)
    l = (lightness + 0.3963377774 * a + 0.2158037573 * b) ** 3
    m = (lightness - 0.1055613458 * a - 0.0638541728 * b) ** 3
    s = (lightness - 0.0894841775 * a - 1.2914855480 * b) ** 3
    linear = (4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s,
              -1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s,
              -0.0041960863 * l - 0.7034186147 * m + 1.7076147010 * s)
    return tuple(12.92 * c if c <= 0.0031308 else 1.055 * c ** (1 / 2.4) - 0.055 if c > 0 else 0.0 for c in linear)


def _in_gamut(channels) -> bool:
    return all(-1e-6 <= c <= 1 + 1e-6 for c in channels)


def _hex(channels) -> str:
    return "#%02X%02X%02X" % tuple(max(0, min(255, round(c * 255))) for c in channels)


def ultra_accents(colour: str) -> dict:
    """The Ultra hue of an accent, per surface: ``{"dark": hex, "light": hex}``.

    Lightness moves away from the surface (so contrast can only improve)
    and chroma is raised, then cut back to the largest value still inside
    the sRGB gamut at that lightness. A grey (no chroma) stays grey.
    """
    lightness, chroma, hue = _srgb_to_oklch(colour)
    variants = {}
    for theme, shift in (("dark", ULTRA_LIGHTNESS_SHIFT), ("light", -ULTRA_LIGHTNESS_SHIFT)):
        level = min(1.0, max(0.0, lightness + shift))
        low, high = 0.0, chroma * ULTRA_CHROMA_GAIN
        if _in_gamut(_oklch_to_srgb(level, high, hue)):
            low = high
        else:
            for _ in range(32):
                middle = (low + high) / 2
                if _in_gamut(_oklch_to_srgb(level, middle, hue)):
                    low = middle
                else:
                    high = middle
        variants[theme] = _hex(_oklch_to_srgb(level, low, hue))
    return variants


def ultra_map(accents: dict) -> dict:
    """Composer label -> per-surface Ultra hue, for every accent."""
    return {label: ultra_accents(colour) for label, colour in accents.items()}


def hue_map(accents: dict) -> dict:
    """Each accent's OKLCH hue in degrees, for the shimmer's gray; an accent
    without enough chroma to carry a hue is left out."""
    hues = {}
    for key, colour in accents.items():
        _, chroma, hue = _srgb_to_oklch(colour)
        if chroma >= SHIMMER_HUE_FLOOR:
            hues[key] = round(math.degrees(hue) % 360, 1)
    return hues


def ultra_css() -> str:
    """The Ultra word's sweep: the marked element's text is filled with a
    gradient of the Ultra hue carrying one lighter highlight, slid across it.

    Only the text fill goes transparent, so ``color`` (and anything drawn
    with it) keeps the hue; the hue falls back to ``currentColor`` so a
    marked element never loses its text, and reduced motion gets a still
    fill in the plain hue.
    """
    hue = f"var({ULTRA_ACCENT_PROPERTY},currentColor)"
    marked = f':where([{ULTRA_MARK}="1"])'
    stops = lambda mix: (f"{hue} 0%,{hue} 28%,color-mix(in srgb,{hue} {mix},#fff) 50%,{hue} 72%,{hue} 100%")
    return (f"@keyframes provider-hub-ultra-sweep{{from{{background-position:100% 0}}to{{background-position:0% 0}}}}"
            f"{marked}{{background-image:linear-gradient(100deg,{stops('55%')});background-size:240% 100%;"
            f"-webkit-background-clip:text;background-clip:text;-webkit-text-fill-color:transparent;"
            f"animation:provider-hub-ultra-sweep {ULTRA_SWEEP} linear infinite}}"
            f':where([{THEME_ATTRIBUTE}="light"] [{ULTRA_MARK}="1"]){{background-image:linear-gradient(100deg,{stops("72%")})}}'
            f"@media (prefers-reduced-motion:reduce){{{marked}{{animation:none;background-image:none;-webkit-text-fill-color:{hue}}}}}")


def shimmer_css() -> str:
    """The stylesheet the watcher adopts: the activity shimmer ("Thinking",
    "Editing files") keeps the app's sweep, but its gray takes the model's hue.

    The shimmer text derives every tone from ``--loading-shimmer-foreground``
    and falls back to the app's description gray; the app resets that knob on
    the element with a zero-specificity rule, so a zero-specificity rule of
    ours later in the cascade wins over the reset while any component that
    sets its own foreground still wins over ours. The value is the app's own
    gray at its own lightness and alpha with a fixed chroma in the accent's
    hue, so legibility does not move; without a hue the ``var()`` is invalid
    and the app's fallback returns.
    """
    shimmer = f"[{THEME_ATTRIBUTE}] :is(.loading-shimmer-pure-text,.loading-shimmer)"
    return (f":where({shimmer}){{--loading-shimmer-foreground:"
            f"oklch(from var(--color-codex-description) l {SHIMMER_CHROMA} var({HUE_PROPERTY}))}}")


def usage_banner_selector() -> str:
    """The ChatGPT usage banners: the app's generic banner (an ``aside``)
    carrying the gauge icon. The account-wide banner and the per-model one
    both draw it; the icon's other uses are slash-command rows, not banners.
    """
    return f'aside:has(svg path[d^="{USAGE_BANNER_ICON}"])'


def usage_banner_css() -> str:
    """Hide the usage banners. The selector outranks the app's utility
    classes on specificity alone, so no ``!important`` is needed; the state
    behind the banner (the account's rate-limit status, the modal it may
    open on submit, the account and usage pages) is untouched.
    """
    return f"{usage_banner_selector()}{{display:none}}"


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
    const ULTRA = __HUB_ULTRA__;
    const STYLE_CSS = __HUB_STYLE_CSS__;
    const PROPERTY = "__HUB_PROPERTY__";
    const MARK = "data-provider-hub-tint";
    const THEME = "__HUB_THEME_ATTRIBUTE__";
    const ACCENT_PROPERTY = "__HUB_ACCENT_PROPERTY__";
    const ULTRA_PROPERTY = "__HUB_ULTRA_PROPERTY__";
    const ULTRA_ACCENT_PROPERTY = "__HUB_ULTRA_ACCENT_PROPERTY__";
    const ULTRA_MARK = "__HUB_ULTRA_MARK__";
    const USAGE_SELECTOR = __HUB_USAGE_SELECTOR__;
    const HUES = __HUB_HUES__;
    const HUE_PROPERTY = "__HUB_HUE_PROPERTY__";
    const state = { targets: [], label: "", colour: "", ultra: "", title: null, words: [], pills: [], marks: [], sheet: null, accent: "", theme: "", hue: "" };
    const norm = (text) => (text || "").replace(/\s+/g, " ").trim().toLowerCase();
    // Labels may carry a leading glyph (a bullet, a tier mark); match the words.
    const lookup = (text) => {
      const key = norm(text).replace(/^[^a-z0-9]+/, "").replace(/[^a-z0-9)\]]+$/, "");
      return key && Object.prototype.hasOwnProperty.call(ACCENTS, key) ? key : "";
    };
    const effortLabel = (container) => container.querySelector("[data-effort-only],[data-accent],[data-maximum]");
    // Light text means a dark surface, and the Ultra hue is cut per surface.
    const ultraColour = (key, surface) => (key && ULTRA[key] && ULTRA[key][surface]) || "";
    // The pill names the selected level by id; the popover's title is
    // localised text, so it is only read when there is no pill to ask.
    function ultraSelected(container) {
      const triggers = document.querySelectorAll("[data-codex-intelligence-trigger][data-selected-reasoning-effort]");
      if (triggers.length) { return Array.prototype.some.call(triggers, (trigger) => norm(trigger.getAttribute("data-selected-reasoning-effort")) === "ultra"); }
      const title = container.querySelector('[data-maximum="true"]');
      return !!title && norm(title.textContent) === "ultra";
    }
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
      if (!STYLE_CSS) { return; }
      try {
        if (!state.sheet) {
          state.sheet = new CSSStyleSheet();
          state.sheet.replaceSync(STYLE_CSS);
        }
        if (!document.adoptedStyleSheets.includes(state.sheet)) {
          document.adoptedStyleSheets = [...document.adoptedStyleSheets, state.sheet];
        }
      } catch (error) {
        if (!state.sheet && document.head) {
          const style = document.createElement("style");
          style.textContent = STYLE_CSS;
          document.head.appendChild(style);
          state.sheet = style;
        }
      }
    }
    function unmark(element) {
      try { if (element && element.getAttribute(ULTRA_MARK) === "1") { element.removeAttribute(ULTRA_MARK); } } catch (error) {}
    }
    function clearMenu() {
      // Only undo our own values: leave any inline value the app set itself.
      for (const target of state.targets) {
        try {
          if (norm(target.style.getPropertyValue(PROPERTY)) === norm(state.colour)) { target.style.removeProperty(PROPERTY); }
          if (state.ultra && norm(target.style.getPropertyValue(ULTRA_PROPERTY)) === norm(state.ultra)) { target.style.removeProperty(ULTRA_PROPERTY); }
          if (state.ultra && norm(target.style.getPropertyValue(ULTRA_ACCENT_PROPERTY)) === norm(state.ultra)) { target.style.removeProperty(ULTRA_ACCENT_PROPERTY); }
        } catch (error) {}
      }
      unmark(state.title);
      state.targets = []; state.label = ""; state.colour = ""; state.ultra = ""; state.title = null;
    }
    function applyMenu() {
      const container = document.querySelector('[data-explicit-model="true"]');
      if (!container) { clearMenu(); return; }
      const found = findModel(container, effortLabel(container));
      const label = found ? found.key : "";
      const colour = label ? ACCENTS[label] : "";
      const host = container.closest("[data-transitions-ready],[data-side]") || container.parentElement;
      if (!host || !colour) { clearMenu(); return; }
      // At Ultra the title, and the slider's fill gradient beneath it, read
      // the app's purple token: give them the model's Ultra hue instead,
      // and the title its sweep. Max (the same title attribute) keeps the
      // app's purple.
      const title = container.querySelector('[data-maximum="true"]');
      const ultra = title && ultraSelected(container) ? ultraColour(label, themeOf(textElement(found))) : "";
      // Themed subtrees re-declare the token, so set it on those too.
      const targets = [host, ...host.querySelectorAll("[data-theme],[data-model-picker-power-slider]")];
      const same = state.colour === colour && state.ultra === ultra && state.title === (ultra ? title : null)
        && targets.length === state.targets.length && targets.every((target, index) => target === state.targets[index]);
      if (!same) {
        clearMenu();
        for (const target of targets) {
          target.style.setProperty(PROPERTY, colour);
          if (ultra) { target.style.setProperty(ULTRA_PROPERTY, ultra); target.style.setProperty(ULTRA_ACCENT_PROPERTY, ultra); }
        }
        if (ultra) { installStyles(); title.setAttribute(ULTRA_MARK, "1"); }
        state.targets = targets; state.label = label; state.colour = colour; state.ultra = ultra; state.title = ultra ? title : null;
      }
    }
    // The composer pill: "<model> <effort>". The model picker trigger stacks
    // one span per effort level ([data-reasoning-effort]) and crossfades
    // them inside an effort label that carries the pill's tertiary grey; the
    // Ultra span paints itself with the app's purple token, which beats an
    // inherited colour. Give that label the model's accent below Ultra. At
    // Ultra, hand the trigger the model's Ultra hue under the app's own
    // token name and mark the Ultra span for the sweep; anything else the
    // app already colours purple is left alone. The older two-part pill
    // (model span + effort span) is handled the same way as a fallback.
    function pillWords() {
      const words = [];
      const pills = [];
      const marks = [];
      let accent = "";
      let theme = "";
      let hue = "";
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
        // The selected model's hue also tints the activity shimmer's gray;
        // the first pill wins if there are several.
        if (!accent) { accent = ACCENTS[model.key]; theme = surface; hue = Object.prototype.hasOwnProperty.call(HUES, model.key) ? String(HUES[model.key]) : ""; }
        if (!effort) { continue; }
        if (effort === "ultra") {
          const ultra = ultraColour(model.key, surface);
          if (!ultra) { continue; }
          pills.push({ element: trigger, colour: ultra });
          const layers = trigger.querySelectorAll('[data-reasoning-effort="ultra"]');
          for (const layer of layers) { marks.push(layer); }
          // The two-part pill has no layers: its effort span is the word.
          if (!layers.length) { for (const label of labels) { if (label) { marks.push(label); } } }
          continue;
        }
        for (const label of labels) {
          if (!label || label.classList.contains("text-chart-purple")) { continue; }
          words.push({ element: label, colour: ACCENTS[model.key] });
        }
      }
      return { words: words, pills: pills, marks: marks, accent: accent, theme: theme, hue: hue };
    }
    function applyShimmer(accent, theme, hue) {
      if (accent === state.accent && theme === state.theme && hue === state.hue) { return; }
      const root = document.documentElement;
      if (!root) { return; }
      const ours = (property, value) => norm(root.style.getPropertyValue(property)) === norm(value);
      if (accent) {
        installStyles();
        root.style.setProperty(ACCENT_PROPERTY, accent);
        if (hue) { root.style.setProperty(HUE_PROPERTY, hue); } else if (ours(HUE_PROPERTY, state.hue)) { root.style.removeProperty(HUE_PROPERTY); }
        root.setAttribute(THEME, theme);
      } else {
        if (ours(ACCENT_PROPERTY, state.accent)) { root.style.removeProperty(ACCENT_PROPERTY); }
        if (ours(HUE_PROPERTY, state.hue)) { root.style.removeProperty(HUE_PROPERTY); }
        root.removeAttribute(THEME);
      }
      state.accent = accent; state.theme = theme; state.hue = hue;
    }
    function clearWords() {
      for (const entry of state.words) {
        try {
          if (entry.element.getAttribute(MARK) === "1") { entry.element.style.removeProperty("color"); entry.element.removeAttribute(MARK); }
        } catch (error) {}
      }
      state.words = [];
    }
    function clearUltraPills() {
      for (const entry of state.pills) {
        try {
          for (const property of [ULTRA_PROPERTY, ULTRA_ACCENT_PROPERTY]) {
            if (norm(entry.element.style.getPropertyValue(property)) === norm(entry.colour)) { entry.element.style.removeProperty(property); }
          }
        } catch (error) {}
      }
      for (const element of state.marks) { unmark(element); }
      state.pills = []; state.marks = [];
    }
    const sameEntries = (next, previous) => next.length === previous.length && next.every((entry, index) => entry.element === previous[index].element && entry.colour === previous[index].colour);
    function applyPills() {
      const found = pillWords();
      if (!sameEntries(found.words, state.words)) {
        clearWords();
        for (const entry of found.words) { entry.element.style.setProperty("color", entry.colour); entry.element.setAttribute(MARK, "1"); }
        state.words = found.words;
      }
      const sameMarks = found.marks.length === state.marks.length && found.marks.every((element, index) => element === state.marks[index]);
      if (!sameEntries(found.pills, state.pills) || !sameMarks) {
        clearUltraPills();
        if (found.pills.length) { installStyles(); }
        for (const entry of found.pills) { entry.element.style.setProperty(ULTRA_PROPERTY, entry.colour); entry.element.style.setProperty(ULTRA_ACCENT_PROPERTY, entry.colour); }
        for (const element of found.marks) { element.setAttribute(ULTRA_MARK, "1"); }
        state.pills = found.pills; state.marks = found.marks;
      }
      applyShimmer(found.accent, found.theme, found.hue);
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
      version: 8,
      accents: Object.keys(ACCENTS).length,
      check: () => ({ container: !!document.querySelector('[data-explicit-model="true"]'), label: state.label, colour: state.colour, ultra: state.ultra, targets: state.targets.length,
                      pills: state.words.map((entry) => entry.colour), ultraPills: state.pills.map((entry) => entry.colour), marks: state.marks.length,
                      shimmer: state.accent ? state.accent + ":" + state.theme : "", hue: state.hue,
                      banners: USAGE_SELECTOR ? document.querySelectorAll(USAGE_SELECTOR).length : null }),
    };
    schedule();
    return { installed: true, accents: Object.keys(ACCENTS).length, usageBanner: !!USAGE_SELECTOR, ready: document.readyState };
  } catch (error) {
    return { error: String(error && error.message ? error.message : error) };
  }
})();
"""


def _label_key(label: str) -> str:
    return label.replace("\u00a0", " ").strip().lower()


def watcher_script(accents: dict, property_name: str = PROPERTY, hide_usage_banner: bool = False) -> str:
    table = {_label_key(label): colour for label, colour in accents.items()}
    css = shimmer_css() + ultra_css() + (usage_banner_css() if hide_usage_banner else "")
    return (_WATCHER.replace("__HUB_ACCENTS__", json.dumps(table, ensure_ascii=False))
            .replace("__HUB_ULTRA__", json.dumps(ultra_map(table), ensure_ascii=False))
            .replace("__HUB_STYLE_CSS__", json.dumps(css))
            .replace("__HUB_USAGE_SELECTOR__", json.dumps(usage_banner_selector() if hide_usage_banner else ""))
            .replace("__HUB_HUES__", json.dumps(hue_map(table)))
            .replace("__HUB_HUE_PROPERTY__", HUE_PROPERTY)
            .replace("__HUB_THEME_ATTRIBUTE__", THEME_ATTRIBUTE)
            .replace("__HUB_ACCENT_PROPERTY__", ACCENT_PROPERTY)
            .replace("__HUB_ULTRA_ACCENT_PROPERTY__", ULTRA_ACCENT_PROPERTY)
            .replace("__HUB_ULTRA_PROPERTY__", ULTRA_PROPERTY)
            .replace("__HUB_ULTRA_MARK__", ULTRA_MARK)
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
        hide_banner = settings.get("codex_hide_usage_banner") is True
        emit({"event": "accents", "count": len(accents), "usage_banner": "hidden" if hide_banner else "shown"})
        run_options.setdefault("environment", child_environment(bundle))
        return run(binary, watcher_script(accents, hide_usage_banner=hide_banner), emit=emit, **run_options)
    finally:
        if stream is not None:
            stream.close()


if __name__ == "__main__":  # pragma: no cover - manual smoke run
    sys.exit(run(executable_path(Path(sys.argv[1])), watcher_script({}), emit=_print_event,
                 environment=child_environment(Path(sys.argv[1]))))
