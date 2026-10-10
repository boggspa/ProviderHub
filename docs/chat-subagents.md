# Subagents in Chat

Chat supports serial helpers and small batches of read-only parallel lanes.
This follows Fable 5.1's design review and Astra's runtime review. It uses the
same gateway, run loop and inspector for both forms.

## First scope

The blocking `delegate` tool lets the parent ask another model to
complete a self-contained task, waits for its result, then continues. One child
at a time, one level deep. The child receives the existing local tools but
cannot delegate. Use the existing Messages gateway and provider adapters.

This gives a second opinion, focused investigation or bounded implementation
without concurrent edits, mailboxes, queues, separate chat tabs or an agent
dashboard. The parent blocks while its child works. Up to four helpers can be
launched in one turn; each has twelve model/tool rounds. The host rejects
recursive delegation even if a child invents a call.

## Presentation

The parent's delegate tool row retains its expandable recorded result, changed
files and error state. Its arrow opens the **Subagents** inspector, whose compact
rows use the existing provider marks and accents. Selecting a child fills the
pane with its task, messages and tools, without a composer. The workspace sidebar
stays for user chats. The parent turn clock continues through delegation.

Child approvals use the existing approval strip and identify the requesting
model. Only one writable task is executing, so no approval queue is needed.
Git counts already cover the workspace and need no separate child view.

## Execution contract

- The default is the parent's exact route and account. An override must select an
  enabled, tool-capable catalogue row. Validate route, account and effort on the
  host; never infer an account from a label. Keep compatible effort or use the
  target model's default, rather than silently picking its lowest setting.
- Start with a fresh task and explicitly bounded portable context. Do not fork
  the parent's provider-native history or forward its signed/encrypted
  reasoning. A nested child loop must own its history and tool-result IDs;
  re-entering the mutable parent `ChatService` is not sufficient.
- Inherit the workspace and the parent's Manual / Accept Edits / YOLO ceiling.
  Delegation cannot raise permissions. The parent blocks while the child acts,
  so they cannot issue simultaneous patches. Keep normal repository claims and
  patch validation. Do not classify shell commands as read-only by their label.
- Enforce depth, launch, round and output limits in the host. Omit delegation
  from the child catalogue and reject attempts anyway; prompts are not limits.
- Stop cancels the active request and foreground local command, including a
  child's. A steer cancels the child, waits for tool cleanup and a recorded
  outcome, then restarts the parent with the user's update. No queued messages
  or synthetic tool-result injection. A stopped tool may already have changed
  files. Background processes a child started belong to the parent chat and
  keep running; see background shells in [Chat](chat.md#files-and-commands).
- Child identity, visible entries, tool outcomes and opaque provider history
  persist as separate `agent` rows in the parent's JSONL file. They are excluded
  from its small metadata header and retain exact route/account ownership. Portable
  replay includes the recorded task and result, never the child's reasoning.
- Make interrupted and completed outcomes explicit. Retry carries forward
  recorded results and must not rerun a child whose mutation outcome is unknown.
  Recovery leaves uncertain work stopped for inspection. Launching another
  child is a new, visible invocation, not an implicit resume.

## Parallel read-only lanes

The parent `ChatService` has one active chat, cancel event, approval slot and
worker thread. A delegate reuses that loop in its own service with isolated
messages and a separate cancellable `GatewayClient`. The repository's
`subagent_catalogue.py` and `spawn_depth.py` adapt desktop-owned agent tools;
they are not a Chat supervisor.

Use the `tasks` form of `delegate` for two or three self-contained tasks. Every
lane is validated before any starts, including the shared four-helper-per-turn
budget. Each defaults to the parent's model/account or selects an exact enabled
catalogue choice. A provider that refuses concurrent requests reports a failure
for that lane; the harness does not retry it silently.

```json
{"tasks": [{"task": "Review the cancellation path"}, {"task": "Check the persistence boundary", "choice": "provider/model|account"}]}
```

Each lane has a separate cancellable client, history and eight-round limit. Only
`read_file` and `search_files` are offered; host dispatch also rejects mutation,
shell and recursive delegation. The parent is blocked until all lanes settle.
Successful results survive a sibling failure. The parent receives bounded,
identified recorded output through normal tool-result continuation, without an
extra synthesis prompt. Native reasoning stays within the owning lane's saved
record and is never forwarded as a parent's or sibling's model history.

Compact provider/task chips in the originating transcript row show live lane
activity and open the existing inspector. There are no lane composers,
mailboxes, queued messages, writable lanes or separate orchestration dashboard.
The parent header's time/context remain its own.

Stop and steering cancel every lane and wait for their cleanup before the next
parent request. Registration is saved before dispatch, and each lane replaces
its own stored snapshot under the parent's persistence lock. Crash recovery
settles incomplete calls without relaunching lanes. Retry continues from the
recorded outcomes; another delegation is a new explicit model tool call.

Fixture tests prove overlapping execution, sibling/account isolation, all-or-none
admission, denied writes, partial failure, eight-round limits and thread-start
failure recovery. They also exercise denial, cancellation, steer during delegation, interrupted
recovery, route/account overrides, depth and round limits, Side Chat generations
and opaque-history isolation. No live-provider concurrency guarantee is implied.
The header's
context figure must continue to describe the parent's context rather than sum
unrelated contexts.
