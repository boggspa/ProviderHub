"""Renderer adapter for native Desktop recent-thread sends and steering.

The adapter never speaks app-server JSON-RPC directly and never drives the
composer UI.  It dynamically imports the already-loaded app module, obtains
the live app scope from React's rendered fibers, and invokes Desktop's own
native follow-up function.  That path resolves the host/thread, resumes the
conversation, and delegates send-versus-steer-versus-queue to the native turn
coordinator while preserving the thread's existing settings.

The live scope is a handle Desktop's shared runtime builds for React
components (its ``useScope`` keeps one in a ref).  A handle carries the
level's descriptor as ``scope``, the level's ``node``, the ``chain`` Map
from descriptor id to node, and the ``get``/``set``/``watch``/``when``
operations the native send calls.  The app level is the descriptor branded
``AppScope`` with no parent; every token the native send reads is declared
there.  The app module does not export that descriptor (its ``W`` export is
an unrelated selector), so the adapter recognises the handle by the
agreement of its own links rather than by an export identity.
"""
from __future__ import annotations

import json


def desktop_actions_script() -> str:
    """Return an idempotent, self-diagnosing renderer action adapter."""
    return r"""
(() => {
  if (window !== window.top) return { skipped: "frame" };
  if (!/^app:\/\/-\//.test(String(location.href))) return { skipped: "origin" };
  if (window.__providerHubDesktopActions) return { skipped: "installed" };

  const UUID = /^[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$/;
  // Each Desktop build we have verified the native send binding against.
  // The pinned set is the contract: every approved hash has been hand-
  // checked for Lv/Iv shape and source signature; an unknown bundle
  // fail-closes with an app-version code that names the observed hash.
  // Adding a new hash means verifying Lv/Iv semantics, then updating
  // this allowlist, the desktop_actions browser test, docs/codex-quick-
  // composer.md and the Swift preference copy together. The verified
  // date lives next to the hash so reviewers can see what was checked.
  const APPROVED_BUNDLES = Object.freeze([
    { hash: 'app-initial-f9b16fbf8fc7.js', version: '26.930.51102', verified: '2026-10-05' },
    { hash: 'app-initial-69cd8dbddec5.js', version: '26.930.61225 build 13232', verified: '2026-10-06' },
  ]);
  const APPROVED_HASHES = Object.freeze(APPROVED_BUNDLES.map(entry => entry.hash));
  // The brand Desktop's shared runtime gives its root dependency scope
  // (verified in app-shared-122c56612a72.js, ChatGPT 26.930.61225). The
  // thread and route levels are branded ThreadScope and RouteScope and
  // carry a parent; neither qualifies.
  const APP_SCOPE_BRAND = "AppScope";
  // Fibers inspected before the scope search gives up. The sidebar, the
  // active page and up to five retained pages fit comfortably; the limit
  // only bounds a runaway walk.
  const FIBER_LIMIT = 60000;
  const state = {
    module: null,
    scope: null,
    moduleError: null,
    scopeError: null,
    observedBundle: null,
    approvedBundles: APPROVED_BUNDLES,
    scopeSearch: null,
    lastError: null,
    sends: 0,
    errors: 0,
    inFlight: new Set(),
  };

  function actionError(code, message, details = {}) {
    const error = new Error(message);
    error.name = "DesktopActionError";
    error.code = code;
    error.details = details;
    return error;
  }

  function findModuleUrl() {
    const urls = new Set();
    const add = (value) => {
      if (typeof value !== "string" || value.length > 4096) return;
      try {
        const url = new URL(value, location.href);
        if (url.protocol === location.protocol && url.host === location.host
            && /\/app-initial-[0-9a-f]+\.js$/.test(url.pathname)) urls.add(url.href);
      } catch (error) {}
    };
    // The loaded index does not preload app-initial: app-main dynamically
    // imports it. Resource timings therefore provide the observed module URL.
    for (const element of document.querySelectorAll("script[src], link[rel=\"modulepreload\"]")) {
      add(element.getAttribute("src") || element.getAttribute("href"));
    }
    if (typeof performance !== "undefined" && performance.getEntriesByType) {
      for (const entry of performance.getEntriesByType("resource").slice(-2000)) add(entry.name);
    }
    if (urls.size !== 1) {
      throw actionError("module-url", "The app module could not be identified uniquely.", { count: urls.size });
    }
    return urls.values().next().value;
  }

  function looksLikeScope(value) {
    // A handle qualifies only when all of its links agree: the descriptor is
    // the parentless app level, the node points back at that descriptor, and
    // the chain maps the descriptor's id to that very node. A stray object
    // with the four method names, a thread- or route-level handle, or a
    // handle detached from its chain cannot pass.
    if (!value || typeof value !== "object") return false;
    for (const name of ["get", "set", "watch", "when"]) {
      if (typeof value[name] !== "function") return false;
    }
    const descriptor = value.scope, node = value.node, chain = value.chain;
    if (!descriptor || typeof descriptor !== "object") return false;
    if (descriptor.__scopeBrand !== APP_SCOPE_BRAND || descriptor.parent != null) return false;
    if (typeof descriptor.id !== "symbol") return false;
    if (!node || typeof node !== "object" || node.token !== descriptor) return false;
    if (!(chain instanceof Map) || chain.get(descriptor.id) !== node) return false;
    return true;
  }

  function inspect(current) {
    // Hook state is a linked list whose entries hold the hook's value in
    // memoizedState; a useRef hook keeps `{ current }` there, which is where
    // Desktop's useScope stores its handle. Props and class instances are
    // checked for a direct or `.value` / `.current` handle as well.
    if (looksLikeScope(current)) return current;
    if (looksLikeScope(current.current)) return current.current;
    if (looksLikeScope(current.value)) return current.value;
    const hook = current.memoizedState;
    if (looksLikeScope(hook)) return hook;
    if (hook && typeof hook === "object" && looksLikeScope(hook.current)) return hook.current;
    if (current.current && typeof current.current === "object") {
      for (const key of Object.keys(current.current).slice(0, 64)) {
        if (looksLikeScope(current.current[key])) return current.current[key];
      }
    }
    return null;
  }

  function verifyNativeBinding(nativeSend) {
    // Export aliases rotate between builds. A function at Lv is not enough:
    // require source evidence of the exact native action. The installed
    // 26.928 bundle destructures the documented parameter list and retains
    // its send_message_to_thread ancestry guard. A future alias or compiler
    // rewrite must fail closed rather than be called by shape alone.
    let source = "";
    try { source = Function.prototype.toString.call(nativeSend); } catch (error) {}
    const required = [
      "scope", "threadId", "sourceThreadId", "prompt", "turnTrigger",
      "send_message_to_thread",
    ];
    if (!required.every(token => source.includes(token))) {
      throw actionError("native-binding", "The app module's native send binding was not verified.",
                       { verified: false });
    }
  }

  function findScope() {
    const root = document.getElementById("root");
    if (!root) throw actionError("root", "The app root element is missing.");
    const fiberKey = Object.keys(root).find(name => name.startsWith("__reactContainer$"));
    const container = fiberKey ? root[fiberKey] : null;
    if (!container) throw actionError("fiber", "The app React container is unavailable.");

    // HostRoot's stateNode owns the current tree. Start there, not with the
    // long-lived container, so a root remount cannot supply a stale scope.
    const current = container.stateNode && container.stateNode.current
      ? container.stateNode.current : container;
    const seen = new Set();
    const queue = [current];
    const search = { fibers: 0, truncated: false };
    state.scopeSearch = search;
    while (queue.length) {
      const fiber = queue.shift();
      if (!fiber || seen.has(fiber)) continue;
      seen.add(fiber);
      if (search.fibers >= FIBER_LIMIT) { search.truncated = true; break; }
      search.fibers += 1;

      // Search the committed current tree only. An alternate may retain an
      // obsolete scope from the previous render and cannot prove liveness.
      for (const holder of [fiber.memoizedState, fiber.memoizedProps, fiber.stateNode]) {
        let entry = holder;
        for (let depth = 0; entry && depth < 128; depth += 1, entry = entry.next) {
          if (typeof entry !== "object") break;
          const found = inspect(entry);
          if (found) return found;
        }
      }
      if (Array.isArray(fiber.child)) queue.push(...fiber.child);
      else if (fiber.child) queue.push(fiber.child);
      if (fiber.sibling) queue.push(fiber.sibling);
    }
    throw actionError("scope", search.truncated
      ? `The live Desktop app scope was not found within ${FIBER_LIMIT} fibers.`
      : `The live Desktop app scope could not be found (${search.fibers} fibers inspected).`,
      { ...search });
  }

  async function prepare() {
    if (state.module) {
      // Re-resolve the live scope on every operation: React can remount the
      // root under the same exported module and a cached wrapper would then
      // be detached from the current tree.
      try {
        const scope = findScope();
        state.scope = scope;
        state.scopeError = null;
        return state;
      } catch (error) {
        state.scope = null;
        state.scopeError = error && error.code ? error.code : "scope";
        throw error;
      }
    }
    try {
      const moduleUrl = findModuleUrl();
      // Rolldown exposes both the action and its idempotent initializer.
      // The verified execution module calls Iv (M3s) before using Lv (k3s).
      // Calling the action alone can leave its lazy dependencies unset.
      // Aliases are build-specific, so require the inspected module mapping.
      const observedBundle = new URL(moduleUrl).pathname.split('/').at(-1);
      state.observedBundle = observedBundle;
      if (!APPROVED_HASHES.includes(observedBundle)) {
        throw actionError('app-version',
          `Native sending has not been verified for this Desktop build (observed ${observedBundle}).`,
          { observedBundle, approvedBundles: APPROVED_BUNDLES });
      }
      const module = await import(moduleUrl);
      const nativeSend = module.Lv;
      if (typeof nativeSend !== "function" || typeof module.Iv !== 'function') {
        throw actionError("exports", "The app module no longer exports the native send binding.");
      }
      verifyNativeBinding(nativeSend);
      module.Iv();
      const scope = findScope();
      state.module = module;
      state.scope = scope;
      state.moduleError = null;
      state.scopeError = null;
    } catch (error) {
      state.module = null;
      state.scope = null;
      state.moduleError = error instanceof Error ? error.name : String(error);
      state.scopeError = error && error.code ? error.code : null;
      throw error && error.code ? error : actionError("prepare", "Desktop native sending is unavailable.");
    }
    return state;
  }

  function validate(request) {
    if (!request || typeof request !== "object") throw actionError("arguments", "A send request object is required.");
    const { threadId, hostId, kind, prompt } = request;
    if (typeof threadId !== "string" || !UUID.test(threadId)) throw actionError("thread", "A valid local threadId is required.");
    if (hostId !== "local" || kind !== "local") throw actionError("unsupported", "Only local Codex threads are supported.");
    if (typeof prompt !== "string" || !prompt.trim() || prompt.length > 65536) {
      throw actionError("prompt", "A non-empty prompt of at most 64 KiB is required.");
    }
  }

  function remember(error) {
    const code = error && error.code ? String(error.code) : "unknown";
    const message = error && error.message ? String(error.message).slice(0, 300) : "";
    state.lastError = { code, message, at: Date.now() };
    return error;
  }

  async function send(request) {
    try {
      validate(request);
    } catch (error) {
      throw remember(error);
    }
    let prepared;
    try {
      prepared = await prepare();
    } catch (error) {
      state.errors += 1;
      throw remember(error);
    }
    const { module, scope } = prepared;
    const key = `${request.hostId}:${request.threadId}`;
    if (state.inFlight.has(key)) {
      throw remember(actionError("in-flight", "A send is already in progress for this thread."));
    }
    state.inFlight.add(key);
    try {
      const result = await module.Lv({
        scope,
        threadId: request.threadId,
        hostId: "local",
        prompt: request.prompt,
        turnTrigger: "composer",
      });
      if (!result || typeof result !== "object" || typeof result.threadId !== "string"
          || result.threadId !== request.threadId) {
        throw actionError("native-result", "The native Desktop send returned an unexpected result.");
      }
      if (typeof result.threadId !== "string" || !UUID.test(result.threadId)) {
        throw actionError("native-result", "The native Desktop send returned an unexpected result.");
      }
      state.sends += 1;
      return { sent: true, mode: "native", threadId: result.threadId };
    } catch (error) {
      state.errors += 1;
      throw remember(error && error.code ? error : actionError("native", "The native Desktop send failed."));
    } finally {
      state.inFlight.delete(key);
    }
  }

  window.__providerHubDesktopActions = {
    ready: async () => {
      try { await prepare(); return true; } catch (error) { return false; }
    },
    send,
    diagnostics: () => ({
      ready: !!(state.module && state.scope),
      module: !!state.module,
      scope: !!state.scope,
      moduleError: state.moduleError,
      scopeError: state.scopeError,
      observedBundle: state.observedBundle,
      approvedBundles: state.approvedBundles,
      scopeSearch: state.scopeSearch,
      lastError: state.lastError,
      sends: state.sends,
      errors: state.errors,
    }),
  };
  return { installed: true };
})();
"""


__all__ = ["desktop_actions_script", "desktop_actions_script_json"]


def desktop_actions_script_json() -> str:
    """Serialization helper for callers embedding the script in JSON."""
    return json.dumps(desktop_actions_script())
