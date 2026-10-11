# Reconnectable Host — cloud handoff

## Starting point

Continue on **`codex/reconnectable-host`**. Its base is merged `main`,
`d08042706446b5e1dad98b909dbb98ab4c107f35`, released as
[Provider Hub 0.5.6 build 68](https://github.com/boggspa/ProviderHub/releases/tag/v0.5.6-build68).
This handoff commit adds documentation only. **No detached Host, local socket
transport, reconnect journal or new lifecycle wrapper has been implemented.**
The branch originally held the capacity work; that work is already merged.

Completed prerequisites:

- [PR #47](https://github.com/boggspa/ProviderHub/pull/47): manual transcript
  scrolling takes priority over automatic follow. See the
  [profiling evidence and architecture outline](transcript-scroll-profiling.md).
- [PR #48](https://github.com/boggspa/ProviderHub/pull/48): 64 managed background
  processes globally, 8 per chat, 16 active chats, and 16 concurrent provider
  requests in both Chat and the gateway. Saved lower Team limits and provider
  backoff remain respected.
- [PR #49](https://github.com/boggspa/ProviderHub/pull/49): completed public Team
  replies can reference a peer at its next existing model boundary. Preserve
  the bounded delivery receipts and the rule that a tag does not restart work.
- [PR #50](https://github.com/boggspa/ProviderHub/pull/50): build 68 metadata.

All three release prerequisite PRs passed GitHub CI before merging. The combined
release tree passed 2,267 Python tests and 1,791 subtests, with four expected
skips; 14 companion tests; iOS simulator typecheck; and repository/runtime gates.
The release was signed, notarized, stapled and published. Do not change its tag
or rebuild its assets as part of this work.

## Objective and limits

Make existing runs survive a disconnected, reloaded or crashed **UI**. Keep
Python, `ChatHost`, the existing execution loops, storage, Team scheduling,
approvals, questions, resource gates and process shelf. Add the smallest
lifecycle and connection layer needed; no new orchestration framework or
scheduling engine is warranted.

The observed freeze involved main-thread SwiftUI transcript layout while the
Python worker continued updating saved history. It supports separating UI
lifetime and delivery from execution; it does not establish that the execution
engine itself is fragile. A daemon still needs bounded queues, memory, history
projections and concurrency limits.

**UI reconnect and Host crash recovery are different guarantees.** Keeping a
process alive can preserve an in-flight provider/tool request. Restarting a
crashed Host cannot recover its threads or prove an external action completed.
Preserve uncertain outcomes and require inspection or an explicit retry;
never automatically replay an action whose completion is unknown.

Keep the Mac UX minimal: the existing transcript, sidebar, panes and process
shelf should keep their behavior. Use a small connection/reconnect status where
needed. Do not introduce a Host dashboard or a new pane-management system.

## Current code boundaries

| Location | Existing responsibility / change boundary |
| --- | --- |
| [chat_sessions.py](../Source/chat_sessions.py) — `ChatHost` | Owns sessions, subscription state, `ResourceGate`, workspace writer gates, `ProcessRegistry`, optional capabilities and the phone bridge. Reuse this owner. |
| [chat_runtime.py](../Source/chat_runtime.py) — `main` | Owns a serial command queue of 256 items. Network callbacks enter that owner. Stdin EOF exits the loop, and `finally` calls `ChatHost.shutdown`. Its `emit` synchronously writes and flushes JSONL stdout under a mutex. |
| [ChatModel.swift](../Source/ChatModel.swift) — `start`, `shutdown`, `ChatEventReader` | Starts `chat_runtime.py` with `Process` and pipes. `shutdown` sends a Host shutdown and closes stdin. Event parsing is off-main, but each parsed event is enqueued individually onto the main queue. Pane facades share the worker while retaining pane state. |
| [MistralBridge.swift](../Source/MistralBridge.swift) — `startGateway`, `stopGateway`, app termination | Starts `gateway.py serve --parent-pipe`; app termination shuts down Chat and the gateway. These ownership/quit paths need to become attach/detach paths for the persistent Host mode. |
| [gateway.py](../Source/gateway.py) — `serve` | Owns the gateway lock, provider admission and connections. With `parent_pipe=True`, stdin closing requests shutdown. The Host must own its lifetime rather than the UI. |
| [bridge_core.py](../Source/bridge_core.py) | `state_root` uses `MISTRAL_BRIDGE_STATE_DIR`; `private_directory` enforces private directory permissions and rejects symlinks. Respect state-root isolation, including Preview/other configurations. |
| [chat_remote.py](../Source/chat_remote.py) | Existing bounded phone projection, request IDs and duplicate acknowledgements provide useful patterns. Its bounded acknowledgement cache is in memory, not a durable Host command journal. Keep phone permissions/pairing separate from local IPC. |
| [chat_capabilities.py](../Source/chat_capabilities.py), [ChatBrowser.swift](../Source/ChatBrowser.swift) | Native actions have invocation IDs, chat ownership, cancellation generations and a timeout. Browser execution depends on a UI-owned surface. A disconnected UI cannot be treated as an available browser. |
| [chat_questions.py](../Source/chat_questions.py), [ChatQuestions.swift](../Source/ChatQuestions.swift) | Existing async/waiting question cards and persisted identities. Detach must not answer, dismiss or fabricate approval for a question. |
| [chat_processes.py](../Source/chat_processes.py), [chat_execution.py](../Source/chat_execution.py) | Process shelf, resource reservations, execution limits and completion wakeups. Retain the new capacity without loosening ownership, cancellation or workspace mutation gates. |
| [build.sh](../Source/build.sh), [build_provenance.py](../Source/build_provenance.py), [package_macos.py](../Source/package_macos.py) | Any new worker module/native launcher must be packaged and included in provenance. A development checkout is not an adequate release-runtime dependency. |

## Suggested sequence of three focused PRs

### 1. Independent lifetime and local connection

Add a small Python Host entry point/transport, reusing `ChatHost` and its serial
command owner. A standard-library Unix-domain socket is the initial transport
candidate; a small Foundation client/lifecycle helper can attach from Swift.
Keep the existing stdio path usable during migration and for fixtures. Proposed
new module names such as `chat_host.py` are not files that already exist.

- Own one Host per user **and effective state root**, protected by a real process
  lock. Concurrent app starts attach to the same instance; they do not spawn
  duplicate execution owners or guess that a PID file establishes ownership.
- Keep local IPC private and authenticated. Validate endpoint ownership, reject
  unsafe/symlinked paths, bound frames before allocating them, and define a
  versioned handshake. Use available OS peer identity and/or an owner-private
  credential; do not expose the local Host on a public TCP listener.
- Preserve the worker environment, provider/account configuration and bundled
  Python runtime. Let the Host own the gateway and its parent pipe. Do not leave
  the UI's `--parent-pipe` as the gateway's lifetime authority.
- Distinguish `detach` from an explicit `stop`/`stop_all`/Host shutdown. Ordinary
  UI disconnect, reload and quit must not call the execution teardown path.
  Define explicit shutdown, update and last-client behavior, rather than
  introducing an undocumented idle timeout that can cancel work.
- Give each attachment an identity. Scope its subscriptions/view cursor and
  native surfaces so one client cannot steal another pane's selection. Preserve
  the phone's separate subscription/authorization boundary.
- Handle protocol/build mismatch explicitly. A running old Host may reference
  bundled helpers while the app is being updated or moved; establish a stable
  runtime-asset lifetime or a controlled restart boundary before replacing those
  assets. Do not silently restart live runs to match a newly opened UI.
- Keep transport readers/writers and slow-client waits outside execution/state
  locks. Even the first PR needs bounded client queues; a disconnected reader
  must not block `ChatHost.emit` or fill unlimited memory.

Acceptance: with scripted providers and real subprocesses, kill or close the
client, verify the original Host/run/process continues, reconnect and inspect
its state. Test duplicate launches, wrong/incompatible clients, disconnects,
explicit shutdown and gateway ownership. Do not claim transparent reconnection
until the snapshot/receipt work below is complete.

### 2. Authoritative reconnect, bounded updates and command receipts

Reconnect with a Host generation, authoritative snapshot and ordered cursor.
The snapshot must include subscribed chats, current live text/offsets, Team and
process state, pending approvals/questions and relevant request receipts. Make
snapshot construction and its cursor consistent with the same state owner;
updates arriving while it is built cannot create a missing or duplicated tail.

- Coalesce replaceable display state and cap each client's queued bytes/items.
  Streaming chunks are not independently disposable: coalesce to an
  authoritative text/offset update or require a fresh snapshot on a gap.
- Do not silently discard approvals, answers, tool results, command receipts or
  completed entries. Persist essential outcomes before acknowledging them. If
  a client falls behind the bounded replay window, require a resync instead of
  treating dropped events as delivered.
- Also bound Swift delivery to the main queue. An efficient Host socket alone
  does not fix a per-event `DispatchQueue.main.async` backlog or huge transcript
  projections. Preserve existing pane state, drafts, selection and scroll intent.
- Give mutating commands stable IDs and durable receipts. Reconnection queries
  the same ID instead of generating a new send/approval/answer/tool action. A
  duplicate ID with different arguments must be rejected. Specify retention and
  expiry so forgotten IDs are not accidentally treated as new commands.
- Record acceptance, execution and completion with crash-consistent ordering.
  A crash between dispatch and recorded completion creates an **uncertain**
  outcome, not evidence that the action never ran. This is not an external
  exactly-once guarantee.
- Keep the serial command owner; executions already run concurrently behind
  explicit admission gates. Avoid making all mutation commands concurrent to
  expand the shelf. Preserve Stop wakeups and prompt handling for approvals and
  questions under the 16-chat / 64-process workload.

Acceptance: deliberately slow readers and burst output while work progresses;
assert a concrete queue/memory bound, correct replay/resync, intact pending
cards and one dispatch per live accepted command ID. Exercise duplicate sends,
approvals and answers from reconnecting clients and the phone.

### 3. Recovery and lifecycle qualification

Test failures at dispatch, after external execution, before receipt persistence
and after receipt persistence. On Host restart, recover saved state without
provider/tool replay, preserve uncertain invocations and distinguish recovered
interrupted runs from live ones. Do not attach to/restart unrelated processes
based on a stale PID. Test malformed/truncated durable records as well as an
orderly shutdown.

Qualify UI crash/relaunch, Host crash/relaunch, competing clients, slow readers,
workspace writers, provider backoff, cancellation, pending approvals/questions,
Team edits/mentions, background process cleanup, plugin revocation, and app
update/version mismatch. Add launchd supervision only if the tested lifecycle
requires it; do not add a supervisor or login service solely for this roadmap.

## Native browser / CUA policy must be explicit

A UI-backed action needs an attachment and surface identity/generation. If no
eligible surface is attached, return a truthful unavailable result before
dispatch. If the client disappears after dispatch but before a confirmed result,
preserve the original invocation and its uncertain outcome. Do not replay it
against a replacement tab, window or client. Late results must remain scoped to
their original chat/invocation/generation.

Keep optional Browser Use and pinned external Computer Use dependencies under
the existing Plugins settings. Do not install external tools silently or add a
second CUA implementation to solve Host lifetime. The phone is not a substitute
executor for Mac UI-backed actions. Provider envelopes must retain the existing
capability, approval and denial-receipt semantics.

## Verification and working instructions

Read `AGENTS.md` and the
[work-claim lifecycle](agent-doctrine/WORK_CLAIM_LIFECYCLE.md) before editing.
Check actual Git status and live claims; use scoped claims and a private index.
Do not edit a dirty peer-owned path or bulk-stage/stash the shared checkout.
The local `.claude/` directory was unrelated pre-existing work and was left
untouched. Release files, private transcripts and credentials are not handoff
inputs; use temporary fixture state, never the user's running application.

Existing production fixtures worth extending:

- `Source/test_chat_concurrency.py` — subscriptions, parallel chats/Teams,
  scoped approvals, request gates, shutdown and startup without provider replay.
- `Source/test_chat_continuity.py` and `Source/test_chat_processes.py` — process
  completion wakeups, waits, cancellation and non-replay after recovery.
- `Source/test_chat_questions.py` — async/wait cards, stable requester identity,
  duplicate/invalid answers and recovery.
- `Source/test_chat_remote_host.py` and `Source/test_chat_remote.py` — serial
  Host dispatch, phone projections, request IDs and revoked devices.
- `Source/test_chat_model.py`, `Source/test_chat_multiview.py`,
  `Source/test_chat_browser.py`, `Source/test_chat_transcript_scroll.py` and
  `Source/test_chat_team_mentions.py` — native client/pane/scroll invariants and
  peer-reference receipts.
- `Source/test_chat_capacity.py` and gateway/Responses tests — the released
  resource caps, including same-provider admission.

Use the repository's `uv` CPython 3.13 environment:

```sh
uv run python -m pytest Source/ -q
uv run python -m unittest discover -s Source -p 'test_*.py'
```

Add meaningful real-process transport/lifetime tests with bounded waits and
deterministic barriers. Prefer standard-library IPC. A Linux cloud environment
can validate Python lifecycle/protocol work, but macOS AppKit/WebKit tests,
Swift compilation, iOS typecheck and packaging still need the existing macOS CI
or a Mac. Do not describe those checks as passed because they were skipped.

**First continuation task:** implement PR 1 on this branch, preserving the
released engine and capacity. Establish the lifecycle/transport contract and
prove UI detach does not stop a scripted run or its managed background process.
Prepare a focused, reviewable PR and report remaining reconnect/recovery work
explicitly. Follow runtime permissions and the cloud task's human authorization;
this document does not grant tools, credentials or publication authority.
