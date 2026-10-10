# Team in Chat

Team lets up to three model/account choices contribute to one Chat transcript.
Open **Team** in the inspector, choose **Set up**, and pick names, models,
reasoning efforts and optional responsibilities. The first member takes the
place of the chat's current model. There is no fourth coordinator model or
extra inference request to manage the queue. Membership is saved with the chat.

## Contributions and continuation

Each new user message starts one contribution from each member together.
A contribution includes its model replies and tool calls. Finishing
normally means **Done**. A member may call `team_status` with:

| Outcome | Effect |
| --- | --- |
| `done` | Finish this contribution; do not queue another. This is the default. |
| `continue` | Supply a concrete `next_step` and join the next contribution wave when the current members finish. Renew this choice on every contribution that needs another. |
| `needs_input` | Supply the blocking question. Running peers finish their contributions, then the Team waits for a real user message. |

Only the member requesting continuation is queued again. Peers that have
finished remain finished. There is no fixed lifetime turn quota. Each execution
slice retains Chat's 24 model/tool-round checkpoint. The model receives its
budget at the start and warnings in the last three rounds. At the checkpoint,
workspace tools and hosted search stop. The member has up to two closing rounds:
one can use only `team_status` to choose an outcome, and the final reply has no
tools. Its sign-off explains progress, remaining work and the next step. An
explicit continuation rejoins the queue; an explicit `done` finishes. Without
an outcome it pauses with recorded results and asks for a message to continue.
The checkpoint notice identifies the exhausted tool budget. Solo Chat turns
also reserve a closing reply instead of reporting the round limit as an error.
Closing is bounded even when a model ignores the tool restriction, and no
workspace actions or additional approvals are allowed there. Output limits
still yield only with an explicit continuation. Three identical consecutive
contributions by one member pause the Team for review, preventing a simple
acknowledgement loop.
This detects exact repeats, not every possible unproductive conversation.

Parallel execution is the default for existing and new Teams, with the same
three-member cap. Model requests, file/search reads and transcript recall can
overlap. Patches and shell commands take a cancellable FIFO gate for the chosen
workspace, including their approval wait. Workspace gates are shared with other
Chat turns in the same worker using that folder. Reads may observe another
member's edits; models are instructed to re-read current files before changing
them. Continuing members begin together only after the current wave settles.

## Shared record, private model histories

Replies and tool rows show their member and model in the existing transcript.
The inspector shows status, next step and individual context usage. It uses
the existing native transcript, lazy stacks and text/table renderer.

Each member has its own native provider history, reasoning state and exact
model/account connection. Before every model round, the existing shared-context
logic supplies newly recorded peer replies, tool results and contribution outcomes.
Members coordinate through that attributed transcript and the shared notebook;
there is no additional channel. In-flight text and pending tool results stay out
of model context until their checkpoint. The per-member cursor and deferred
source IDs are persisted together, so an earlier row finishing later is neither
skipped nor repeated. Records that do not fit a shared delta wait for the next
round. New user messages are preserved, while peer excerpts are
bounded to 48,000 UTF-8 bytes per update and 4,000 characters per record. Source
IDs lead back to `search_history` and `read_history` for omitted details. Peer
output is labelled reference material, never new user authorization.

Members share the chat's source-linked decision notebook. Private model context
is trimmed using the provider's reported window with a 200,000-token ceiling;
unknown windows use a 128,000-token working bound. These are local budgeting
estimates, not a promise that a provider accepts every payload. The full visible
transcript and notebook remain on disk when context is trimmed.

Changing a member's model, account connection, effort, responsibility or
workspace archives that member's prior private history and starts a fresh
portable context. Rename-only changes retain it. If a connection or workspace
changes underneath a saved roster, Team asks for reconfiguration before running.
Turning Team off retains the visible transcript and resumes solo mode from its
attributed public record. Normal model switching is available again in solo mode.

## Permissions and interruptions

All members have the four workspace tools plus recall/notebook tools and
`team_status`. They inherit Manual, Accept Edits or YOLO from the chat. Approvals
are queued one at a time and identify the requesting member and approval ID.
Allow or Deny resolves only that request; denial is returned to that member as
a denied tool result. Waiting for an approval holds the workspace write gate,
while other members can continue model work and reads.
Team members cannot recursively delegate or create a fourth member.

**Stop Team** cancels every active request/tool, approval wait and queued write.
**Resume unfinished work** schedules incomplete members only; completed members
are not replayed. A question needs a composer reply, not Resume. Sending a new
message while work is active preserves the run. Every active member receives it
before its next model request, after its current tool outcomes are recorded.
Members that already finished are queued for the next wave. Steering preserves
the original contribution and continuation identities; Stop still cancels all work.

Reopening Chat does not restart Team automatically. Recorded successful
contributions retain their outcome and queue position. Interrupted tool calls
receive an uncertainty result instructing the member to inspect the workspace
before retrying; a host cannot prove whether an external side effect happened
immediately before a crash. A provider failure, including HTTP 429, pauses only
that member; healthy peers can finish their work and explicit continuations.
There is no automatic rate-limit retry. When healthy work finishes, the Team
shows the failed member for review or Resume. Persistence failures stop all
members because their shared journal can no longer be trusted to record work.
For the Codex CLI route, a pending host call can outlive its native turn during
a checkpoint pause. If `turn/steer` confirms there is no active turn, the host
starts a fresh thread and replays the recorded call, result and new user input
in the same request. It does not answer the stale RPC or require a Retry. Other
steering errors still surface normally.

## Persistence and verification

The chat's atomic JSONL snapshot stores a Team record and individual member
records; private histories are excluded from UI state and sidebar headers.
While running, an adjacent `<chat-id>.team-log` appends changed public rows,
one member's private history, all member statuses, queue metadata and decision notes. Every
checkpoint is flushed and synced. A terminal reply and its scheduling outcome
share one checkpoint, with contribution IDs preventing duplicate settlement.
New public rows, including peer rows still streaming, are checkpointed in their
original append order so persisted index cursors retain their meaning after a
crash. Unfinished rows remain unavailable to peer model context until recorded.
The log folds into an atomic snapshot at 8 MiB and when the Team pauses/finishes.
The growing transcript and other members' private histories are not rewritten
for every tool call. Saved attachments remain in the existing chat directory.

Recovery replays sequence-checked records once and ignores an incomplete trailing
append. An invalid complete record fails closed and its source remains available
for recovery. Journals and snapshots use the existing chat identity checks and
reject symlink paths. No new database, web view or service is needed.
The UI receives `activeMemberIDs` for all currently working members, alongside
the compatible `activeMemberID` naming the most recently started one still
working. Every member retains its own `status`, `contributionID`, `usage` and
`context`; histories stay private. Streaming identity uses each working member's
latest row in its live contribution, even when another member appends a row.

`test_chat_team.py` exercises real ChatService scheduling against scripted
providers: all three members, long continuation, fairness, private reasoning and
account isolation, shared recall, serialized approvals/edits, Stop/steer,
failures, context checkpoints and crash recovery. `test_chat_model.py` runs the
actual Swift model to check correlated configuration, cross-chat isolation,
member attribution, contribution streaming identity and resume controls. The
`test_chat_team_parallel.py` uses barriers to prove model/read overlap, FIFO
write and approval ownership, shared-record delivery across cursor holes,
concurrent notebook writes, Stop/steer fan-out, isolated failures and recovery
with two pending tool calls. The full native app is typechecked alongside the repository Python suite. Live
provider behaviour still depends on each model following the Team tool contract.
