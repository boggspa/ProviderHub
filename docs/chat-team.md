# Team in Chat

Team lets up to three model/account choices contribute to one Chat transcript.
Open **Team** in the inspector, choose **Set up**, and pick names, models,
reasoning efforts and optional responsibilities. The first member takes the
place of the chat's current model. There is no fourth coordinator model or
extra inference request to manage the queue. Membership is saved with the chat.

## Contributions and continuation

Each new user message schedules one contribution from each member in roster
order. A contribution includes its model replies and tool calls. Finishing
normally means **Done**. A member may call `team_status` with:

| Outcome | Effect |
| --- | --- |
| `done` | Finish this contribution; do not queue another. This is the default. |
| `continue` | Supply a concrete `next_step` and rejoin the back of the queue after finishing. Renew this choice on every contribution that needs another. |
| `needs_input` | Supply the blocking question. Pause the Team after this contribution and wait for a real user message. |

Only the member requesting continuation is queued again. Peers that have
finished remain finished. There is no fixed lifetime turn quota. Each execution
slice retains Chat's 24 model/tool-round checkpoint; an opted-in member yields
there and rejoins the queue. Without continuation it pauses for input. Output
limits behave the same way. Three identical consecutive contributions by one
member pause the Team for review, preventing a simple acknowledgement loop.
This detects exact repeats, not every possible unproductive conversation.

Scheduling is deliberately serial. One member's contribution finishes before
another starts, so there is one workspace writer and one approval slot. The
members are peers, but this version does not provide simultaneous execution or
member-to-member mailboxes. Models are instructed to read current files and
use transcript recall when more context is needed.

## Shared record, private model histories

Replies and tool rows show their member and model in the existing transcript.
The inspector shows status, next step and individual context usage. It uses
the existing native transcript, lazy stacks and text/table renderer.

Each member has its own native provider history, reasoning state and exact
model/account connection. Only attributed visible transcript updates cross
between members. The latest user request is preserved, while peer excerpts are
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
identify the requesting member; denial is returned as a denied tool result.
Team members cannot recursively delegate or create a fourth member.

**Stop Team** cancels the active request/tool and leaves queued work paused.
**Resume unfinished work** schedules incomplete members only; completed members
are not replayed. A question needs a composer reply, not Resume. Sending a new
message while work is active cancels and settles the current action before
queuing the new instruction for all members. No queued member starts while the
previous action or approval is still being cleaned up.

Reopening Chat does not restart Team automatically. Recorded successful
contributions retain their outcome and queue position. Interrupted tool calls
receive an uncertainty result instructing the member to inspect the workspace
before retrying; a host cannot prove whether an external side effect happened
immediately before a crash. Provider and persistence errors pause further work.

## Persistence and verification

The chat's atomic JSONL snapshot stores a Team record and individual member
records; private histories are excluded from UI state and sidebar headers.
While running, an adjacent `<chat-id>.team-log` appends changed public rows,
the active member's private history, queue metadata and decision notes. Every
checkpoint is flushed and synced. A terminal reply and its scheduling outcome
share one checkpoint, with contribution IDs preventing duplicate settlement.
The log folds into an atomic snapshot at 8 MiB and when the Team pauses/finishes.
The growing transcript and other members' private histories are not rewritten
for every tool call. Saved attachments remain in the existing chat directory.

Recovery replays sequence-checked records once and ignores an incomplete trailing
append. An invalid complete record fails closed and its source remains available
for recovery. Journals and snapshots use the existing chat identity checks and
reject symlink paths. No new database, web view or service is needed.

`test_chat_team.py` exercises real ChatService scheduling against scripted
providers: all three members, long continuation, fairness, private reasoning and
account isolation, shared recall, serialized approvals/edits, Stop/steer,
failures, context checkpoints and crash recovery. `test_chat_model.py` runs the
actual Swift model to check correlated configuration, cross-chat isolation,
member attribution, contribution streaming identity and resume controls. The
full native app is typechecked alongside the repository Python suite. Live
provider behaviour still depends on each model following the Team tool contract.
