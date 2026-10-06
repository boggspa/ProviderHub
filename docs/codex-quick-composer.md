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
hashes on the small allowlist the helper ships. Currently verified builds:

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
persistence, remounts and keyboard behavior.
No live Desktop send was performed: this task's runtime blocks computer use
of the Codex app. A live in-app acceptance check remains outstanding.
