# Recent-chat quick composer

Provider Hub Preview 0.5.6 build 47 adds a separate **Show the recent-thread
quick composer** preference. Save and launch Codex / ChatGPT from Provider
Hub. It uses the existing private DevTools pipe and works independently of
provider accent colours. It does not modify the installed Desktop bundle.

The launcher sits in the sidebar masthead beside the native search and
notification controls. Its 14px glyph uses the app's secondary text token,
so it tracks native light/dark transitions like its neighbours. Dragging
the window heading moves the floating in-app glass surface, which stays
open until dismissed with its close button, the launcher toggle, or Escape
while focus is inside it; clicking elsewhere or pressing Escape elsewhere in
the app does not close it. It opens below the launcher, and once moved it
reopens where it was left.

The floating surface is a renderer-side DOM overlay (`position: fixed`,
attached to `document.body` inside Codex's Electron `BrowserWindow` content
area). That means it can be dragged close to the visible edge of the Codex
window but it cannot leave it: the renderer is composited inside Codex's
window, so no amount of CSS, drag, or DOM work will let this popover float
above other apps or onto a different display. That is a structural
constraint of being a renderer-side surface, not a clamp the helper
enforces; the helper keeps the heading reachable on next open so the
launcher remains anchorable.

A genuinely independent window is possible, but not from the renderer.
Probed on 26.930.61225 (2026-10-07): `window.open` from the app page
returns null (Desktop's window-open handler refuses popups), while
`Target.createTarget` with `newWindow: true` over the private DevTools pipe
creates a separate page target, which is its own macOS window. A future
version could have the worker own such a window, inject the popover there,
and relay rows, previews and sends to the main session over the pipe. That
is a worker-driven design, not a change to this overlay.

It contains the first ten native recent-chat rows, in sidebar order, each
with a one-line title and a one-line latest assistant response preview.
Rows whose threads are running show a spinning indicator in the thread's
provider accent, or the default gray when accents are unavailable; dormant
rows show none. Rows are updated in place rather than rebuilt, so streaming
updates do not swallow a click. The single-row capsule follows Desktop's
composer proportions and theme tokens. Plain and editing keystrokes,
clipboard events and Escape typed in the window do not reach the app's
document-level shortcuts; other Command/Control shortcuts still do. Each
chat retains its own draft while the window remains mounted.

Only local Codex chats can send in this version. Remote-host and ChatGPT
rows keep their position among the first ten and show as unavailable. The
watcher uses authored sidebar row ID, host, kind and title attributes. The
native `chats` section is identified by its own React component metadata;
the search button is identified by its native action handler. Other search
controls and transcript text cannot select a target. Missing hooks leave
the feature unavailable rather than guessing thread identities.

Only an open popover requests previews. The worker queries the exact
observed local IDs in the newest `state_N.sqlite` database using `mode=ro`,
then reads at most 256 KiB from each saved rollout's tail. Reads accept only
regular JSONL files beneath the configured Codex home's sessions or archived
sessions, reject symlink traversal, and select assistant prose rather than
user input, reasoning or tool output. A response outside that bounded tail
has no preview. Message content is never written to the helper's logs.

Explicit submit calls Desktop's exported native follow-up implementation
through its existing renderer module and live app scope. Desktop resumes
the selected thread and its turn coordinator handles starting, steering or
queueing. No model, reasoning, workspace, collaboration or permission
override is supplied. Success requires Desktop to return the requested
thread identity. Pending sends cannot clear another chat's draft or newer
text typed while sending. Failures preserve the draft.

The adapter verifies the module URL and function shape before invoking it,
and calls the native action's idempotent module initializer. The `Iv`/`Lv`
export mapping is qualified per Desktop build; sending is only enabled for
hashes on the small allowlist the helper ships.

The native action needs Desktop's live app-level dependency scope. Desktop's
shared runtime builds a handle for React components (its `useScope` keeps
one in a ref) that carries the level's descriptor as `scope`, the level's
`node`, the `chain` map from descriptor id to node, and the `get`, `set`,
`watch` and `when` operations the native action calls. The app level is the
descriptor branded `AppScope` with no parent, and every token the native
action reads is declared there. The app module does not export that
descriptor: its `W` export is an unrelated selector, which is why helper
builds 47 to 49 failed every send with the generic reason (they required
`handle.scope === module.W`, which no live handle satisfies). The adapter
now walks the committed React tree from the host root and accepts a handle
only when its links agree with each other: the brand and missing parent on
the descriptor, a symbol id, the node pointing back at the descriptor, and
the chain mapping that id to that node. Thread- and route-level handles, a
handle detached from its chain, or an object that merely has the four method
names are skipped. The search is capped at 60,000 fibers and its extent is
reported.

Every adapter failure code has its own reason in the popover (`scope`,
`module-url`, `root`, `fiber`, `prepare`, `in-flight`, `exports`,
`native-binding`, `native-result`, `native`, the request validators and the
version guard), and an unknown code is shown verbatim. The adapter's
`diagnostics()` adds `scopeSearch` and `lastError`. The popover keeps a
content-free report of the last send (outcome, code, thread id, observed
bundle, module and scope errors, search extent) which the worker takes on
its next two-second read and logs as a `quick-composer` event with stage
`send`; prompts and previews never enter that report.

Currently verified builds:

- `app-initial-f9b16fbf8fc7.js` — ChatGPT Desktop 26.930.51102 (verified 2026-10-05)
- `app-initial-69cd8dbddec5.js` — ChatGPT Desktop 26.930.61225 build 13232 (verified 2026-10-06)

An unapproved bundle fails closed with `app-version` and the message includes
the observed hash; the adapter's `diagnostics()` exposes `observedBundle` and
`approvedBundles` so the operator can tell which build tripped the guard.
Adding a new hash means hand-verifying Lv/Iv semantics, then updating
`codex_desktop_actions.py`, the desktop_actions browser test, this document,
and the Swift preference copy together.

These are observed implementation hooks in the listed Desktop builds, not a
public extension API. App updates can disable the launcher or native
action, and a build the helper has not been verified against will fail
sends with a diagnostic that names the bundle the user is running.

Rendered fixtures cover authored identities, first-ten order,
activity indicators, unsupported hosts, drafts, asynchronous sends, drag and
persistence, remounts, keyboard behavior, every failure reason and the send
report. The adapter fixture models the real handle shape with decoys (a
thread-level handle, a detached handle, a methods-only object) ahead of the
app-scope handle, and a decoy-only tree that must fail with `scope`.

Live check on ChatGPT Desktop 26.930.61225 (2026-10-07, helper scripts from
this tree over the pipe): `ready()` returned true with the module verified
and the app scope found after 26 fibers. No message was sent; a live send
remains to be confirmed by a user from the popover.
