# Chat capabilities and panes

These are development changes following Provider Hub 0.5.6 build 67. They do
not change the installed desktop clients or their plugin configuration.

## Panes

Drag a chat from the workspace rail over another pane. Blue edge previews show
where it will split; the middle opens the chat in that pane. A chat already
visible receives focus. Pane layouts are recursive and may split along either
axis, without a preset pane-count ceiling.

The native View menu also provides:

| Action | Shortcut |
| --- | --- |
| New Pane Right | Option–Command–D |
| New Pane Below | Shift–Option–Command–D |
| Close Pane | Option–Command–W |
| Focus / Restore Pane | Control–Option–Command–F |

Each new pane starts with a chat picker. Split availability depends on the
target pane's size: two chat columns need 420 points each; stacked panes need
300 points each. Closing a pane collapses its sibling into the space and does
not stop or delete the chat. Focus mode preserves the stored arrangement.
The existing active-chat and provider-request limits still govern execution;
adding a view never starts a model request.

One Python worker owns every chat. Each visible pane subscribes to its chat's
snapshot and subsequent events. Swift shares session state through pane
facades, while sends, approvals and Stop carry explicit chat identities.

## Browser

Choose Browser in the inspector, or choose Browser in an empty workspace pane.
The native WKWebView belongs to the chat and persists while its view moves.
This initial implementation has one page per chat. The browser uses a separate
WebKit data store; it does not import Safari, Chrome or provider sign-ins.

Manual browsing works independently of **Settings → Plugins → Browser Use**.
Enabling Browser Use adds the browser tools to eligible model requests. A tool
operates the same WKWebView displayed in the pane. Turning it off revokes
pending dispatch and cancels outstanding native requests. It cannot undo an
action the page already received.

The initial tool set covers navigation, DOM observation, basic element actions,
screenshots and closing the page. The driver is implemented in this repository
using public WebKit APIs. Fixed scripts execute in an isolated content world;
page text is untrusted observation data. This is not a Playwright backend.
Cross-origin frame traversal, shadow DOM, browser extensions and complete key
chords are not qualified in this version. Re-observe after navigation, user
takeover or a stale-target result. A cancelled action may have already reached
the page; inspect the next observation before retrying a consequential action.

## Computer Use and dependencies

**Settings → Plugins → Computer Use** configures an optional external Peekaboo
executable. Select the native executable from the pinned release, then enable
the plugin. Configuration and status reads do not launch a process; Enable or
Connect starts the MCP server after an exact version check. Provider Hub does
not install Homebrew, npm, Node, or a second agent runtime.

| Component | Source / pin | Runtime requirement |
| --- | --- | --- |
| Pane UI | macOS AppKit and SwiftUI | Existing app deployment target |
| Native browser | macOS WebKit | Existing app deployment target |
| Computer Use | Peekaboo 4.9.0, optional, MIT | macOS 15+; native binary needs no Node |
| MCP client | Repository Python implementation | Existing bundled Python 3.13 |
| Python dependencies | `uv.lock` | Existing project runtime |

The source and distribution contain `Source/plugin-dependencies.lock.json`.
It records an exact release, source revision, upstream archive SHA-256 digests,
licence, platform requirement and qualification notes. The archive digests
describe archives, not extracted executables. An executable selected by the
user is trusted local code; exact version checks alone do not prove its origin
or freedom from vulnerabilities. Pinning makes updates explicit and prevents
an unreviewed version from silently changing tool behaviour. It is not a
vulnerability scanner. Maintenance should review the upstream changes, update
the lock and notices, and rerun compatibility tests together.

The app keeps its older macOS deployment target. UI uses availability checks
and the worker checks the helper's actual minimum macOS version. Unsupported
optional capabilities show their requirement while ordinary Chat remains
available. Screen Recording and Accessibility setup belongs to macOS and the
selected helper. Enabling a plugin does not bypass those permissions.

All plugin tools use the same provider-neutral tool-result envelope and the
existing approval policy. Manual and Accept Edits show bounded action arguments
before external tool calls; YOLO uses the user's existing preauthorization.
Read-only Side Chats and helper lanes do not gain these tools. Screenshots are
saved as transcript attachments and included in model context only when its
route accepts images. Nested screenshot results share the context image budget
and survive a model switch through portable history.

The MCP adapter does not expose server-initiated sampling, host shell access or
elicitation. It rejects unsupported protocol/schema features visibly rather
than treating them as permission. A plugin crash reports a failed action; it
does not restart and replay the action automatically.

## Remote companion

Open **Settings → Devices**, enter the Mac's explicit LAN or Tailscale IP,
and choose Enable. Listening is off by default and never restored automatically
at app launch. Scan the QR code with the iOS companion (or copy pairing text),
compare the code on both devices and confirm on the Mac. Turn off remote access or remove the
device to revoke its connection. Up to eight devices can connect on the private
network, each with its own selected chat. Removing one does not disconnect the
others. A removed device can pair again after a fresh matching-code approval;
compatible Mac and iOS releases do not need matching app version numbers.
The scanner asks for camera access only when
opened; pasted pairing text remains available when camera access is unavailable.

The transport and Swift companion live in `Source/chat_remote.py`,
`Packages/ProviderHubRemote/` and `ios/ProviderHubCompanion/`. The phone can list
chats, view the latest transcript entries, send text, stop work and answer an
exact pending approval. Phone selection subscribes to a chat independently of
the desktop's focused pane. Commands enter the same serial host queue; the Mac
owns credentials, execution, approvals and recorded results. A paused/closed
phone does not execute work. Keep the Mac running and connected.

The companion follows the Mac's semantic surfaces, provider accents and
compact conversation layout. iPhone opens the chat list as a sheet; wide iPad
layouts keep a chat rail alongside the transcript. Assistant, user and tool
rows retain their identities as results arrive. Tool details expand in place;
code blocks are selectable and scroll horizontally. Approvals sit above the
composer. Settings offers System/Light/Dark and Glass/Solid appearances.
Both apps use darker card fills with a subtle 0.5-point accent rim, keeping
the existing control geometry and hover treatments.

The application uses the TaskWraith E2EE primitives with a pinned Mac identity
and human-confirmed phone trust. The phone identity uses device-only Keychain
storage. Mac identity/trust use a private 0600 file. Reconnect retrieves a
snapshot; unacknowledged messages remain drafts and are not automatically
resent. Duplicate request IDs are retained for the latest 64 requests per
device within the Mac process lifetime; this does not promise exactly-once
execution after a process restart. Full history, media preview, push, background
phone connections and a cloud relay remain separate follow-ups.
The visible projection is limited to the latest 30 entries within a 48 KB
serialized entry budget, with an 8,000-character prefix per message/live reply.
Chat summaries have a separate 32 KB budget and a maximum of 256 recent chats.
JSON text uses UTF-8 on the encrypted channel so these budgets also hold for
non-ASCII transcripts and stay within the transport frame limit.
Long-stream offsets stay within that prefix so reconnects do not repeatedly
request the same snapshot. Provider/account identifiers, reasoning and local
attachment records are excluded; visible message and tool text can still
contain paths or other information already shown in the chat.

See [the companion guide](../ios/ProviderHubCompanion/README.md) for iOS build
and pairing details. A signed Simulator app completed pairing, transcript
subscription and a message round trip with fixture data against production
ChatHost. Real Swift/Python encrypted TCP tests and a QR render/decode round
trip pass. Physical-camera scanning and an on-device network trial remain unverified.

## Verification

### Questions from agents

Every tool-capable Chat, helper and Team member can use `request_user_input`
to ask and wait, or `request_user_input_async` to post a card and continue
independent work. `wait_for_user_input` waits for a previously posted request;
its bounded timeout leaves the question open and returns `pending`.

The transcript card names the asker, offers choices or free text, and supports
multiple selections when requested. No answer is preselected. Send answer
records the result; Dismiss records that no answer was given. The card stays
in history after settlement. Draft answers survive pane changes and row
remounts for the running app session. The paired iOS companion can answer the
same question; the first durable reply wins across devices.

Waiting affects only the asking worker and does not hold a workspace or
provider-request slot. Async answers enter the asking agent's context at a
model boundary, or resume an idle chat; Team replies target the named member
without cancelling peers. Finished helper answers return to the owning chat
with attribution. Stop cancels that worker's questions; stopping the whole
chat cancels all its questions. Restart cancels unanswered saved cards rather
than reattaching them to a new worker. Answered cards remain the record if a
connection or restart interrupts delivery. These tools cannot grant tool
permissions or replace Allow once.

The native layout follows TaskWraith's `AgentQuestionCard`/settled-card pattern
in AGBench, implemented with the harness's SwiftUI card surfaces and existing
host routing. It adds no package dependency. At most 16 questions can remain
open in a chat, with up to four prompts and six choices per card.

Use the repository's uv CPython 3.13 test command. Targeted suites include
`test_chat_model`, `test_chat_concurrency`, `test_chat_capabilities`,
`test_chat_plugins`, `test_chat_plugin_mcp`, `test_chat_browser`,
`test_chat_multiview`, `test_chat_attachments` and `test_chat_history`.
These cover state routing, cancellation/revocation, approvals, executable pin
checks, malformed MCP replies, native split layout and screenshot retention.
The pinned arm64 Peekaboo archive was independently hash-checked, its executable
signature verified, and its actual MCP initialize/tools-list completed with 26
upstream tools. The initial CUA adapter excludes upstream `agent` and `analyze`,
which run a separate model pipeline; model execution remains in Provider Hub.
The admitted 24-tool catalogue also completed a read-only permissions-status
call and exact-PID inspection/image capture of a temporary native fixture.
Semantic `set_value` returned success, changed the fixture's own field receipt
and was confirmed by a fresh accessibility read. The adapter accepts the pinned
catalogue's `uniqueItems` array declarations, enforcing uniqueness when requested.
A pointer-button click incremented that fixture's counter but the helper reported an
indeterminate final target-generation validation error. The adapter preserves
the error and did not retry the action. Pointer actuation remains
qualification-limited; a positive counter change is not a clean success receipt.
Executable WKWebView loopback tests cover observation, action targets,
navigation and cancellation. Native split tests exercise both axes and stable
hosting controllers. The encrypted phone test routes through production
ChatHost as well as the transport-specific cross-language checks.
Live external desktop automation requires separate qualification on the pinned
helper and target apps; unit-test success must not be represented as that
qualification.
