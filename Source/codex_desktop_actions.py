"""Renderer adapter for native Desktop recent-thread sends and steering.

The adapter never speaks app-server JSON-RPC directly and never drives the
composer UI.  It dynamically imports the already-loaded app module, obtains
the live app scope from React's rendered fibers, and invokes Desktop's own
native follow-up function.  That path resolves the host/thread, resumes the
conversation, and delegates send-versus-steer-versus-queue to the native turn
coordinator while preserving the thread's existing settings.
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
  const state = {
    module: null,
    scope: null,
    moduleError: null,
    scopeError: null,
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

  function looksLikeScope(value, rootScope) {
    return !!value && typeof value === "object"
      && value.scope === rootScope
      && typeof value.get === "function" && typeof value.set === "function"
      && typeof value.watch === "function" && typeof value.when === "function";
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

  function findScope(rootScope) {
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
    let visited = 0;
    while (queue.length) {
      const fiber = queue.shift();
      if (!fiber || seen.has(fiber)) continue;
      seen.add(fiber);
      if (++visited > 20000) break;

      // Search the committed current tree only. An alternate may retain an
      // obsolete scope from the previous render and cannot prove liveness.
      // React hook state is a linked list and refs are `{ current }`.
      for (const node of [fiber]) {
        if (++visited > 20000) break;
        for (const holder of [node.memoizedState, node.memoizedProps, node.stateNode]) {
          let current = holder;
          for (let depth = 0; current && depth < 128; depth += 1, current = current.next) {
            if (!current || typeof current !== "object") break;
            if (looksLikeScope(current, rootScope)) return current;
            if (looksLikeScope(current.current, rootScope)) return current.current;
            if (looksLikeScope(current.value, rootScope)) return current.value;
            if (looksLikeScope(current.memoizedState, rootScope)) return current.memoizedState;
            if (looksLikeScope(current.memoizedState && current.memoizedState.current, rootScope)) {
              return current.memoizedState.current;
            }
            if (current.current && typeof current.current === "object") {
              for (const key of Object.keys(current.current).slice(0, 64)) {
                const value = current.current[key];
                if (looksLikeScope(value, rootScope)) return value;
              }
            }
          }
        }
      }
      if (fiber.return) queue.push(fiber.return);
      if (Array.isArray(fiber.child)) queue.push(...fiber.child);
      else if (fiber.child) queue.push(fiber.child);
      if (fiber.sibling) queue.push(fiber.sibling);
    }
    throw actionError("scope", "The live Desktop app scope could not be found.");
  }

  async function prepare() {
    if (state.module) {
      // Re-resolve the live scope on every operation: React can remount the
      // root under the same exported module and a cached wrapper would then
      // be detached from the current tree.
      try {
        const scope = findScope(state.module.W);
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
      if (new URL(moduleUrl).pathname.split('/').at(-1) !== 'app-initial-f9b16fbf8fc7.js') {
        throw actionError('app-version', 'Native sending has not been verified for this Desktop build.');
      }
      const module = await import(moduleUrl);
      const nativeSend = module.Lv, rootScope = module.W;
      if (typeof nativeSend !== "function" || typeof module.Iv !== 'function' || !rootScope) {
        throw actionError("exports", "The app module no longer exports the native send binding.");
      }
      verifyNativeBinding(nativeSend);
      module.Iv();
      const scope = findScope(rootScope);
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

  async function send(request) {
    validate(request);
    const { module, scope } = await prepare();
    const key = `${request.hostId}:${request.threadId}`;
    if (state.inFlight.has(key)) {
      throw actionError("in-flight", "A send is already in progress for this thread.");
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
      throw error && error.code ? error : actionError("native", "The native Desktop send failed.");
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
