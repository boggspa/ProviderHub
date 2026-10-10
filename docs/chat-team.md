# Team in Chat

Team lets up to four model/account choices contribute to one Chat transcript.
Open **Team** in the inspector, choose **Set up**, and pick names, models,
reasoning efforts and optional responsibilities. The first member takes the
place of the chat's current model. There is no separate coordinator model or
extra inference request to manage the queue. Membership is saved with the chat.

## Contributions and continuation

Each new user message starts one contribution from each member together.
A contribution includes its model replies and tool calls. Finishing
normally means **Done**. A member may call `team_status` with:

| Outcome | Effect |
| --- | --- |
| `done` | Finish this contribution; do not queue another. This is the default. |
| `continue` | Supply a concrete `next_step` and resume independently as soon as this contribution is saved. |
| `waiting` | Supply `next_step` and exactly one `process_id` or `member_id`. Release the model until that dependency finishes; the host then resumes this member with its result. |
| `needs_input` | Supply the blocking question. Running peers finish their contributions, then the Team waits for a real user message. |

New Teams default to **Task** mode. Every 24 model/tool rounds the host saves a
quiet checkpoint in the same contribution. Tools and private history remain
available; no sign-off or renewed continuation choice is required. A normal
final reply still finishes that member's work. A checkpoint is not a spending
limit. There is no fixed lifetime contribution quota.

**One contribution** preserves the earlier behavior for discussions and reviews:
24 rounds, then up to two closing rounds with only `team_status` followed by a
tool-free reply. Explicit continuation resumes that member; without an outcome
the checkpoint pauses for a user message. Saved Teams without execution settings
keep this mode until reconfigured. Solo Chat and delegated helper limits are
unchanged.

Members run and resume independently; there is no wave barrier. Finished peers
remain finished. A real process/member dependency puts a member into **Waiting**.
No model polling requests run during that wait. Missing process records interrupt
the wait for review, and cyclic member waits are rejected. A new user message
supersedes a wait. Stop interrupts waiting members as well as active workers.
Reopening Chat marks old waits interrupted; process IDs are never rebound to
processes from a new worker.

Optional run settings bound elapsed minutes and cumulative reported input,
output and cache tokens across all members. The allowance begins on Send or
Resume; steering an active run does not reset it. Checks run before model
requests and tools, including after a queued request or approval. In-flight
requests may exceed a token limit; these are not prepaid reservations. Missing
usage pauses a configured token limit rather than silently treating it as zero.
Provider spending is not estimated or capped because these routes do not supply
consistent billing telemetry. The inspector reports the specific limit reason.
Once a Team turn ends, one quiet table lists the files its members changed
through patches, with line counts, who touched each file, and its diff on
expansion. Solo Chat turns
also reserve a closing reply instead of reporting the round limit as an error.
In One contribution mode, closing is bounded even if the model ignores the
tool restriction. Output limits still require explicit continuation. Three
identical consecutive contributions pause the Team for review. In Task mode,
three checkpoints without new successful tool evidence also pause for review;
new read/search findings count as progress, as do edits. This bounded comparison
does not judge the meaning of prose or detect every unproductive workflow.

Parallel execution is the default for existing and new Teams, with the same
four-member cap. Model requests, file/search reads and transcript recall can
overlap. Patches and shell commands take a cancellable FIFO gate for the chosen
workspace, including their approval wait. Workspace gates are shared with other
Chat turns in the same worker using that folder. Reads may observe another
member's edits; models are instructed to re-read current files before changing
them. Background commands release the gate after launch, so their later writes
still require the members' normal path coordination.

## Addressing members

Type `@` in the composer to address members by name. A short list above the
composer shows each member's mark, name and model. Arrow keys move through it,
Return or Tab inserts the whole name, Escape closes it, and a click works too.
A tag that will reach a member is drawn in that member's provider accent, on a
chip of the same hue, in the composer and again in the sent message. A tag that
stays plain text addresses nobody.

A message that tags members runs only them, in the order they were first
tagged. The others stand by and read it as context, marked as addressed to
their peers. An untagged message still reaches every member. While the Team
works, a tagged update queues only its members, in tag order. Untagged members
keep working. One that is paused for a question, a stop or a run limit stands
by instead of holding back the tagged members, and so does one that asks a
question after the update; the question stays its next step until a message
addresses it. Tagging a peer in a reply does not schedule it; members use
`team_status(waiting, member_id=...)` for a real dependency. A member standing
by cannot be waited on, and a wait on one that later stands by resumes with
that result.

A tag starts the message or follows a space, an opening bracket or quote, a
comma, semicolon or asterisk, so an email address never addresses anyone. It
matches a member's whole name as written, except that ASCII letters may differ
in case: `@sol` reaches Sol, but other letters, and how an accent is encoded,
must match the name exactly. Choosing from the list always inserts the exact
name. A tag ends before another letter, mark, number or underscore, so
`@Opus 2` and `@Opus` can coexist, and tags inside code spans or fenced blocks
stay text. Member names must differ, ignoring case and how their accents are
encoded; in an older saved Team, names that differ only in the case of ASCII
letters are plain text.

Only the composer decides what is a tag. Each send carries the chips it drew,
and those chips are the routing record: a tinted tag always reaches its member,
an untinted one stays plain text, and a message sent without chips reaches the
whole Team. The worker never reads the text for tags, and its checks use no
Unicode case or normalization tables, so neither side's Unicode version can
change a tag. It keeps a chip only while its member is still in the Team under
exactly that name and model, over text that reads `@` and the name by the rule
above. Otherwise the composer drew from an outdated roster, so the worker
refuses the message before storing anything and resends the current roster;
Chat restores the draft and the tints correct themselves. Sent messages record
each tag's member, name, route and position, so a later rename or model change
never recolours history. Chat decodes rosters and transcripts character for
character, so a member's name and a recorded tag's position are exactly what
the worker compared. Text an input method is still composing is part of the
draft, so a send takes it as shown; a tag it completes is tinted once the
composition is committed. The composer's cases live in
`Source/test_chat_mentions_cases.json`; the tests also generate thousands of
messages full of awkward characters and check that the worker keeps every chip
the composer draws for them.

## Failed requests

Transient member request failures retry quietly, for up to five attempts total,
with cancellable waits of roughly 2, 5, 10 and 20 seconds. Only the provider
request is retried: completed tools and their recorded results are kept.
Authentication and invalid requests fail immediately. After exhaustion, that
member is parked while peers continue. One red warning line names the member;
expand it for error codes, raw errors and attempt timings. The inspector shows
the short reason. A new user message or Resume unfinished work can resume it.
Shell timeout accepts seconds; clearly millisecond values (1000–600000) are
converted and capped at five minutes. Invalid tool arguments remain ordinary
tool errors that the model can correct.

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
round, keeping only the newest 32 so a busy Team cannot build an unbounded
backlog. New user messages are preserved, while peer excerpts are bounded to
48,000 UTF-8 bytes per update, 4,000 characters per reply and 1,500 per tool
result, since a member can re-read files itself. Source IDs lead back to
`search_history` and `read_history` for omitted details. Peer output is
labelled reference material, never new user authorization.

Members share the chat's source-linked decision notebook. `team_status` also
accepts bounded `objective`, `findings` and `owned_paths`; these are stored with the next step
and dependency as a per-member work record, and reintroduced after compaction.
Follow-up messages preserve the findings and record the latest request; members
can replace outdated notes. The record is reference data, not a path lock or permission.
Private model context is trimmed using the smaller of the provider's reported
window and the configured Team context ceiling (default 200,000 tokens);
unknown windows use at most 128,000. The inspector shows that effective ceiling.
Input budgeting reserves 15% plus output and saved notes. These are local budgeting
estimates, not a promise that a provider accepts every payload. The full visible
transcript and notebook remain on disk when context is trimmed.

Changing a member's model, account connection, effort, responsibility or
workspace archives that member's prior private history and starts a fresh
portable context. Rename-only changes retain it. If a connection or workspace
changes underneath a saved roster, Team asks for reconfiguration before running.
Turning Team off retains the visible transcript and resumes solo mode from its
attributed public record. Normal model switching is available again in solo mode.

## Permissions and interruptions

All members have workspace tools, background process tools, recall/notebook tools and
`team_status`. They inherit Manual, Accept Edits or YOLO from the chat. Approvals
are queued one at a time and identify the requesting member and approval ID.
Allow or Deny resolves only that request; denial is returned to that member as
a denied tool result. Waiting for an approval holds the workspace write gate,
while other members can continue model work and reads.
Team members cannot recursively delegate or add members.

Background processes retain member attribution in the shared Processes tab.
The Team's running-process ceiling is configurable from one to eight (default
four), within the worker-wide maximum of eight. Extra launches preserve a first
slot for active peers where capacity permits. Each member has its own output
cursor, so one member reading a process cannot consume another's unread output.
Process completion wakes registered waits. Stopping Team leaves background
processes running; stop them from Processes, delete the chat, or quit Chat.

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
providers: all four members, long continuation, fairness, private reasoning and
account isolation, shared recall, serialized approvals/edits, Stop/steer,
failures, context checkpoints and crash recovery. `test_chat_model.py` runs the
actual Swift model to check correlated configuration, cross-chat isolation,
member attribution, contribution streaming identity and resume controls. The
`test_chat_team_parallel.py` uses barriers to prove model/read overlap, FIFO
write and approval ownership, shared-record delivery across cursor holes,
concurrent notebook writes, Stop/steer fan-out, isolated failures and recovery
with two pending tool calls. The full native app is typechecked alongside the repository Python suite. Live
provider behaviour still depends on each model following the Team tool contract.
