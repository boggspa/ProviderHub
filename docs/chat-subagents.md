# Small-scale delegation in Chat — proposal

Status: design and runtime reconnaissance only. Chat does not expose a
delegation tool yet. This proposal combines Fable 5.1's design review with
Astra's runtime review.

## First scope

Start with one blocking `delegate` tool: the parent asks another model to
complete a self-contained task, waits for its result, then continues. One child
at a time, one level deep. The child receives the existing four local tools but
cannot delegate. Use the existing Messages gateway and provider adapters.

This gives a second opinion, focused investigation or bounded implementation
without introducing concurrent edits, mailboxes, queues, separate chat tabs or
an agent dashboard. Parallel children would be a separate runtime change.

## Presentation

Show a compact, expandable row in the parent's transcript: existing provider
mark and accent, child model, short task title and live state. Reuse the tool-row
disclosure and diff treatment. Expansion reveals the child's visible messages
and tools; completion adds its answer and changed-file count. Keep the workspace
sidebar for user chats. The parent turn clock continues through delegation.

Child approvals use the existing approval strip and identify the requesting
child. Only one request is executing, so no additional approval queue is needed.
Git counts already cover the workspace and need no separate child view.

## Execution contract

- Default to the parent's exact route and account. An override must select an
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
- Stop cancels the active request and local command, including a child. A steer
  cancels the child, waits for tool cleanup and a recorded outcome, then restarts
  the parent with the user's update. No queued messages or synthetic tool-result
  injection. A stopped tool may already have changed files.
- Persist child identity, visible entries and tool outcomes within the parent
  log. Keep any returned opaque provider history separately with its exact
  route/account ownership, following existing Chat history rules. Portable
  replay includes the recorded task and result, never the child's reasoning.
- Make interrupted and completed outcomes explicit. Retry carries forward
  recorded results and must not rerun a child whose mutation outcome is unknown.
  Recovery leaves uncertain work stopped for inspection. Launching another
  child is a new, visible invocation, not an implicit resume.

## Why parallel agents are deferred

`ChatService` currently has one active chat, cancel event, approval slot and
worker thread. `GatewayClient` owns one cancellable socket. The repository's
`subagent_catalogue.py` and `spawn_depth.py` adapt desktop-owned agent tools;
they are not a Chat supervisor.

Parallel children require independent clients and cancellation, durable run
identities, parent/child ownership, multiple approval handling and coordinated
workspace mutation. Astra's review estimates roughly 6–9 engineering days for
a polished bounded parallel read-only version, with writable coordination
adding 3–5 days. That estimate does not apply to the smaller serial proposal.

Before shipping serial delegation, exercise mixed-provider results, denial,
tool cancellation, steer during a child approval, restart after an uncertain
patch, expired accounts, catalogue changes and model switching with preserved
opaque traces. Live child usage can remain inside the expanded row; the header's
context figure must continue to describe the parent's context rather than sum
unrelated contexts.
