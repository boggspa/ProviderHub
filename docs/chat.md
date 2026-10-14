# Chat in Provider Hub

Choose **Chat with Model…** from the menu bar, or the chat button in the compact
Hub window. Chat runs in its own native window alongside Claude and Codex, using
the provider connections already configured in the Hub.

The header selects a model/account, reasoning level, and workspace folder.
The compact picker follows TaskWraith's provider rail, model list, and reasoning
sidecar. It lists the union of models enabled in the launcher's Claude and
Codex catalogues, deduplicated by route; it does not introduce another catalogue
to maintain. Opening it refreshes the saved launcher selection.
Reasoning choices come from that model's actual ladder. Model and account
choices do not alter either desktop client's configuration. Switch model or
account while idle to continue in the same chat. A marker shows the switch;
visible dialogue, attachments and recorded tool results carry over. The previous
provider history, including returned opaque reasoning, stays intact in a private
archive in the JSONL log. The new provider receives portable text/image context,
never another provider's reasoning signatures, encrypted blocks or pending calls.
Switching back follows the same rule rather than reviving a stale trace. Changed
connections also start a fresh provider context.

A provider that offers both an API key and an installed CLI (Claude, Codex,
Muse, Grok) can list both in Chat at once. Turn on **Also list this provider's
… models in Chat** under its credential source in Providers and refresh its
catalogue. Rows on the active source keep their account ids; rows on the
second source carry `<source>:<account>` (for example `keychain:` for the
default key slot, `cli:work` for a named CLI login), and every row of such a
provider is labelled with its source. The active source still lists the
launcher-enabled routes; the second source has no tick list of its own, so it
lists every model its catalogue holds, and the picker's search narrows it. The
second source's accounts are the ones saved for it (extra API keys or CLI
folders), managed by switching the credential source temporarily. Claude
Desktop and Codex keep using the active source only; a chat on the second
source names it in `_provider_hub_account`, and the gateway reads that source's
cached catalogue whenever the picker refreshes or a request plans on it, so a
refreshed catalogue reaches Chat without restarting the gateway. Changing
workspace opens a new chat, leaving the original transcript in the rail.

The sidebar lists **Projects** first, then **Recents**, then **Workspaces**. A
workspace with attached folders is promoted into Projects; the extra folders
are listed under its header. Attach a folder from the workspace header's
context menu (**Attach folder…**) and disconnect one from its row's context
menu; disconnecting the last folder demotes the workspace back to Workspaces.
Attachments persist across restarts, and a chat's file tools can
read/search/patch the attached folders with absolute paths under the same
approval rules; the system prompt lists them so the model knows they exist.
The Workspaces section groups chats under their single workspace folders.
Collapse a group or use its **+** to start a chat there. Recents shows the ten
newest chats across every workspace, each labelled with its workspace, and
selecting one behaves like selecting it in its group. Folders remain in the
picker and sidebar after restart or after deleting their last chat. Path
aliases share one group; different folders with the same name show their
paths. Disconnected folders stay in the list and their saved chats can still
be read. The selected row carries a thin bar in its model's provider accent.
Each running chat shows a small spinner; a hand marks a chat waiting for
approval, including chats in another folder.
In a narrow window, the saved-chats button opens the same list in a popover.

You can switch chats or start a new one while other chats run. Each chat keeps
its own model or Team, workspace, draft, attachments, elapsed time, and pending
approval. Stop and updates affect the chat you are viewing. Different projects
can work at the same time. Chats sharing a checkout serialize patches and shell
commands, and branch changes wait until all chats using that checkout are idle.

One worker hosts the chats. Idle chats have no execution loop, and only the
visible transcript streams into the window. Selecting a background chat restores
its latest output. Up to sixteen chats can run at once, with at most sixteen
model requests across chats, helpers and Side Chats. The gateway also admits
sixteen requests across its Messages and Responses surfaces. Requests using
the same provider can share all available slots; provider rate-limit responses
still reduce that provider's admission budget and apply backoff. When the chat limit is reached,
the message stays in your draft so you can send it
after a turn finishes.

Return sends a message; Shift-Return inserts a newline. Text an input method is
still composing counts as typed, so a send takes exactly what the composer
shows. Stop cancels the selected chat's request and local command execution,
keeping partial output and recorded actions. Closing the window leaves active
work running; reopen it from the menu bar. Quit the Hub after work has stopped.

Use **+**, drop files onto Chat, or paste them into the composer to attach
them. Draft attachments sit inside the composer above the text. Images have
small thumbnails in the composer and transcript; click one to view it. Other
files use compact chips with a type glyph, name and size. Pasting a copied
image with no file behind it, such as a screenshot, writes a temporary PNG and
attaches that; pasted text still pastes as text. Files can be dropped during a
turn to ride along with an update, while a dropped folder waits for the turn to
end. Text/code files and PDFs with extractable text are sent as text, and PNG,
JPEG, GIF and WebP images use the gateway's existing image support. Up to eight
files can be attached, at 8 MiB per file and 20 MiB per message. Originals are
copied into private Chat storage so saved thumbnails do not depend on the
source staying in place. Removing a draft attachment does not delete its source.

There is no message queue. During a turn, type an update and press Return or
**Interrupt and send update**. Chat cancels the active request, waits for local
tool cleanup, records its real result, then starts a fresh request with the
update as user input. Partial output stays visible. An update is never disguised
as a tool result. A second update cannot queue behind an interruption; keep it in
the composer until the new turn starts. Stop also cancels an impending restart.
If the app closes during interruption, the saved update is recovered without
automatically executing it.

Sending an update while a Team is working keeps the Team running. Members
receive the update after their current recorded work reaches a safe boundary.
Queued continuations keep their place, and members that already finished
receive another contribution afterward. Running tools finish and record their
real results. Use Stop to cancel the Team.

**Turn time** beneath the composer counts the active turn, including tool and
approval waits. Steering and provider-request restarts keep the same clock;
completion, failure or an acknowledged Stop resets it to `00:00`. In a solo
chat the status word sits beside the approval mode. A Team chat leaves it out
and shows what is running only in the live activity strip above the composer,
so the composer does not shift as members' commands wrap. While a turn runs,
the status word, the activity strip and the **Thinking…** label in the
transcript sweep the model's provider accent; with Reduce Motion on they take
the accent without movement.

The header's unboxed Git indicator shows changed files, tracked added/deleted
lines, and gold/blue ahead/behind counts against the cached upstream. It never
fetches a remote. With no upstream, only a positive local-commit count appears;
its tooltip labels that distinction. Git inspection runs separately from the
conversation loop so it cannot block Stop.
Team chats keep the folder, branch and Git totals together on a second header
row, leaving room for every member's context ring.

The **branch/worktree** chip beside the folder picker lists local branches and
worktrees and can create either. Branch switching and creation require a clean
repository and no live work claims; Chat never stashes or forces a checkout.
Manual claims expire under the shared twenty-minute lease contract; runtime
lock projections continue blocking until their owner resolves them.
If a target branch lacks the selected subfolder, choose the worktree root first;
Chat refuses that checkout before it can remove the working directory. The same
check protects open Side Chats rooted in repository subfolders.
Creating a worktree leaves
the current selection in place until you choose the new one. Choosing a
worktree keeps this chat, records the workspace change, archives its old provider
context and starts fresh portable context for the new directory. Changes are
available only between turns, including Side Chats using that workspace.

The right-hand **inspector** has four views, each using the whole pane:

- **File Changes** lists file diff counts and expandable patches with three
  context lines, then the commits made since this turn started. It includes
  untracked files and reports binary or truncated changes. While a turn is
  running it refreshes after a completed patch, shell or other file edit,
  about every few seconds, and again when the turn finishes. A file edit that
  arrives as that refresh decides the list is idle still schedules a trailing
  refresh, without waiting for another edit or the end of the turn. A team member
  who last edited a path is marked on that row. Reads use the local checkout
  and never fetch a remote.
  A separate lower **Repository** viewport shows unpushed commits against the
  cached upstream, or explicitly labelled local commits when no upstream is
  set, and open GitHub pull requests. Both lists load twenty rows at a time,
  up to two hundred, in a lazy scroll view. Git and PR lookups run in worker
  threads with time and output bounds. PR reads use an existing GitHub CLI
  login; an Open GitHub link remains available if that CLI is absent or the
  lookup fails. Refresh reloads repository information without changing Git
  state or fetching a remote.
- **Team** configures up to eight persistent members, including the current
  chat model. Each contributes once by default and may explicitly continue with
  a next step. Members think and read in parallel through one attributed
  transcript, refreshed before every model round, with separate provider histories.
  Workspace changes and approvals queue one at a time. Stop, unfinished
  work and questions are visible in this pane. See [Team](chat-team.md).
  **Edit** works while the Team runs. Each member changes at its next safe
  boundary: a model request is cancelled, while a running tool finishes and
  records its result first. A replaced member keeps its place and resumes its
  work on the new model.
  Each working member has a small Stop control that cancels only its run;
  healthy peers continue. Done, Ready and stopped members have a Play control
  to contribute again using current Team context. Joining an active run keeps
  its allowance, and questions or safety pauses still need to be resolved.
  Blackboard author and category labels remain plain accented text within
  their cards.
  Earlier delegated tasks remain underneath the roster; choose one to read
  its transcript and tool results. Parallel read-only helper chips remain
  beside their originating delegate row in the main transcript.
- **Side Chat** forks the visible conversation at that point with any enabled
  model. It has its own composer and Stop/interrupt controls, with read/search
  tools only. Provider reasoning stays isolated. Side Chats remain in memory
  while you switch parent chats, keep their original workspace and are discarded
  on explicit close, deletion of their parent, or quitting the app. They are not
  written to Chat's saved JSONL history. At most eight can be open at once.
- **Background Processes** lists the shell commands this chat's model, its Team
  members and its helpers left running, then the last ten that finished. Each
  row names its owner and shows the command, status and running time; expand it
  for the pid, folder and recent output, or stop it. The tab's badge counts
  running processes. See background shells under Files and commands.

Drag the hairline at either side of the conversation to resize the workspace
rail (150–360 points, default 190) or inspector (at least 260 points, at most
everything but the conversation's 420). The inspector and the conversation
share width equally: without a drag override the inspector takes half of what
is left beside the rail, giving ground only so the rail stays available, so a
wide window gives the browser, Team and other tabs real room instead of a
fixed 360. Width preferences persist across sessions. Double-click a divider
to reset it (the inspector returns to the even split); accessibility
adjustment and a Reset width action are also available. Resizing the window
clamps displayed widths to leave at least 420 points for the conversation. A
wide inspector collapses the left workspace rail on narrower windows; growing
the window restores the preferred widths.
The main turn clock and context count describe the parent;
Side Chat runs independently.

The small gear at the bottom-left of the sidebar offers Allow web search, Theme, Glass/Solid,
Font, and Text size. Glass lays a 75% scrim over the window material (black in
Dark, white in Light) so text holds on a busy wallpaper; the Hub window's Glass
surface lays the same scrim beneath its chroma wash, so the two windows read as
one. Font choices include
System, System Mono, Inter, Source
Serif 4 and JetBrains Mono. **Custom…** opens the native macOS font panel for
fonts installed through Font Book. Only the chosen font name is saved; custom
font files are never copied. An unavailable custom font falls back to System.
Theme and surface reuse the Hub's existing preferences. Text choices affect
the transcript and composer; code and diffs keep their monospace treatment.

**Zoom** enlarges the conversation without rescaling the app. Use the header's
**− 100% +** control (click the percentage for actual size) or View › Zoom In
(⌘+), Zoom Out (⌘−) and Actual Size (⌘0) while the Chat window is in front.
Steps run from 85% to 300% on top of the Text size choice. Transcript text,
tool rows, glyphs, tables and the composer scale, and the reading column
widens with them; the header, rail, footer and buttons keep their size. The
zoom is saved across launches.

The send and stop buttons stay monochrome and invert with the theme: a light
disc on a dark window, a dark disc on a light one.

**Allow web search** is on by default and saved across app launches. It enables
the provider's native search on routes that advertise support: eligible Codex,
Claude and Grok CLI models, and eligible OpenRouter API routes. Models without
native search continue to work; Settings shows that search is unavailable for
the selected model. OpenRouter uses its native engine without a third-party
search fallback. No separate search account or API key is needed.

The preference also applies to Team members, helpers, read-only lanes and Side Chats, using
each agent's own model capabilities. A change takes effect on the next model
request; an already-running request may finish its search. Turning it off omits
native search and instructs agents not to search through other tools. This
setting does not sandbox network access by shell commands. Provider search
records appear in expandable transcript rows, and returned source links appear
with the answer. Source data and citations survive reopening a chat.

### Markdown and tables in replies

Assistant replies render common Markdown natively. Headings (`#` to `######`)
drop their hashes and appear in semibold. Bullet (`-`, `*`, `+`) and numbered
(`1.`, `1)`) lists use a hanging indent, so wrapped lines align under the
item's text rather than its marker. Nesting follows leading indentation, and
numbers stay exactly as the model wrote them. Inline code sits in a small
rounded chip. A fenced code block (` ``` ` or `~~~`) becomes a contained box with its
language label. Long lines scroll sideways instead of wrapping, and a **Copy**
button appears on hover. Quotes (`>`) show a quiet bar, and `---` draws a rule.
Only the display changes: **Copy Message** and the saved chat keep the original
Markdown. The composer stays plain text, except that in a Team an `@Name` tag
is tinted in the accent of the member it will reach; see
[addressing members](chat-team.md#addressing-members).

Markdown pipe tables get a small native layout. A header followed by a valid
`---` delimiter row becomes a table with wrapped cells and the requested column alignment.
Wide tables scroll horizontally within the transcript. Right-click a table
to **Copy table (TSV)** for a spreadsheet or **Copy table as Markdown**.

Transcript text behaves like text in any Mac app: click and drag to select
within a message, double-click a word, ⌘A to select a whole message, and ⌘C
to copy. Each message is selected on its own.
Right-click offers the native menu (Copy, Look Up, Translate, Share and
Services) plus **Copy Message**, which copies the whole message with its
original Markdown, whether or not anything is selected.
Hover over a user or assistant message to reveal **Copy message** beside it.
The button copies the whole saved text, including Markdown and source links,
and shows a checkmark after copying. Tool calls and notices have no message
copy button. The reserved action space keeps text still when the button appears;
keyboard focus also reveals it.
This applies to the main transcript, Side Chat and agent inspector.

During streaming, only complete table lines become cells. The line currently
arriving stays visible as text until its newline or the end of the turn.
Fenced/indented code and malformed table syntax keep the existing text layout.
The parser does not interpret HTML or change saved transcripts or model input.

The renderer adds no dependencies or WebView. The transcripts already use
SwiftUI `LazyVStack`: rows are created lazily, while saved entries still stay
in memory. Formatting state belongs to each reply and reuses unchanged
blocks/cells. Native tables are bounded to 12 columns, 128 body rows and 64 KiB;
one reply can render up to 16 tables and 2,048 cells. Larger content falls back
to complete text, and messages above 256 KiB skip structured parsing. These
limits cap view/parse work without truncating history. Full transcript
windowing is a separate optimization to consider after measuring long chats.

## Files and commands

The local tools are read file, search files, apply patch, and run shell, which
can also start a background process followed with read process and stop
process. In solo
mode the parent also has **delegate**, which runs one helper at a time and returns its
recorded result. Helpers inherit the workspace and approval mode, get the
local tools and cannot delegate. A turn can launch at most four helpers, with
twelve model/tool rounds per serial helper. Its alternative `tasks` form runs
two or three read-only lanes together, with eight rounds per lane; all count
toward the same four-helper limit. Read-only lanes have read/search local tools only,
even in YOLO. Stop and steering cancel every lane and wait for cleanup before
the parent continues. See [Subagents](chat-subagents.md). File
tools stay inside the chosen workspace and reject symlink paths. A small menu
below the composer selects one of three modes, saved per chat:

- **Manual** asks for patches and shell commands with **Allow once / Deny**.
- **Accept Edits** allows validated patches inside the selected Git repository
  automatically. Shell commands, non-repository patches and Git metadata changes
  still ask. Shell commands are never guessed to be safe file edits.
- **YOLO** runs the available tools without approval prompts.

New chats default to Manual. Modes can be changed between turns. Expand a
proposed patch to review it before allowing.
The mode menu shows its state in colour: Manual is blue, Accept Edits neutral,
and YOLO red, which also tints the composer outline faintly. While a turn runs
the mode cannot change, but it stays in full colour beneath the composer.
Choosing YOLO in a
folder for the first time asks once to confirm; that answer is remembered per
folder on this Mac and never sent to the worker.

A CLI-backed route (Claude Code, Codex or another CLI running as the model
transport) may describe its own sandbox, plan or read-only mode and a temporary
working directory. Those describe only the nested process. The host tools act
on the workspace named in the system text under the approval mode above, and
the note the host sends with its tools says so explicitly, because seats have
read the nested CLI's posture as the host's and stopped editing.

The transcript reads like Claude and Codex Desktop. Your messages sit in a
bubble on the trailing edge and open with a bold **User:** label inside the
bubble, so that in a Team transcript, where members' replies and tool calls
interleave with yours, an instruction is never read as the preceding member's
words; **Copy Message** still copies the message without the label. A helper's
inspector transcript labels the parent's task the same way. Your messages and
replies can carry emoji reactions from Team members or from you (right-click
**React**, or click an existing chip); see [Reactions](chat-team.md#reactions). Each speaker (the chat's model, or a Team member
by name) gets a section header; in a Team, every text reply starts a new section
and later tools join that member's current section even when peers interleave.
User messages and notices close all member sections; solo chats retain their
continuous speaker blocks, and finished rounds that produced no text draw nothing.
Section headers show the opening entry's recorded time in local time (with a date for earlier days); older chats without recorded times show none.
Tool rows lead with what was done ("Ran `swift build`", "Edited Model.swift",
"Read tools/import.py · lines 85–259", "Searched for `water_height`") instead
of the folder every call shares, and their glyph wears the caller's provider
accent, so Team members' calls are recognisable at a glance. A row lifts on
hover and expands to show output and textual diffs; a shell row shows its full
command above the output. A patch row shows its added and removed line counts
in the Git indicator's colours. A call still running shimmers its verb.
Three or more consecutive local tool rows fold into one disclosure such as
**Worked · 4 commands, 2 edits** with the combined file and line counts. A
fold opens only when you open it, jump to a row inside it, or had already
expanded one of its rows; while its speaker works it stays one line reading
**Working**, its counts and latest step updating in place, so a busy Team
transcript does not jump as calls start and finish. A fold opened or closed by
hand keeps that choice. Delegate
rows keep their own lanes and are never folded. Recorded changed files can be
revealed in Finder. A link in a reply that names a file or folder (a path
relative to the chat's workspace, an absolute or `~` path, or a `file://` URL)
opens the file in its default app or reveals a folder in Finder; a path that no
longer exists reveals the nearest existing folder instead. Web links open in
the browser as before. Previously every such link went to the system as a
schemeless URL and failed with Finder's "The application can't be opened"
(error -50). The newest row keeps a little headroom above the composer:
the transcript follows a spacer below the last row, so auto-follow lands with
breathing room, scrolling up still disengages it, and scrolling back to the
end or the **Latest** button re-engages it.

While a turn runs, a **live activity strip** sits between the transcript and
the composer with one line per speaker that is working right now, in the
rows' own words: "Sol Running `swift build -c release`", "Luna Editing
main.swift", "MiniMax Thinking…", or "M3 Waiting" with the reason. A Team
shows every working, continuing or waiting member in roster order; a solo
chat shows its model. The line reads the speaker's newest row (a running tool
in its live phrasing, a streaming reply as "Replying…", a model request in
flight as "Thinking…"), so nothing is invented, and it disappears when the
turn ends. After Stop, or while an update interrupts the turn, every line reads
the turn's own status ("Stopping…", "Interrupting for your update…") until the
cancelled work has finished. Clicking a line scrolls to that speaker's current
row (or the fold holding it, which opens) and pauses following, like a manual
scroll; the **Latest** button brings the view back.

Web search sources collapse into one pill under the reply (**12 sources ·
github.com · …**) that opens into a numbered list; copied and saved text keeps
the original **Sources:** line. Notices are quiet dividers. A Team checkpoint
that only continues later or finishes is not drawn; a pause or question reads
as a handoff naming the member, with the runtime's full wording in its
tooltip. A finished Team turn ends with a table of the files it changed.
Older chats saved an "Older model context trimmed"
notice on most rounds of a long turn; those are no longer drawn.

Patches use Codex's `*** Begin Patch` format, which the tool description now
spells out: `*** Add File:` lists every new line with a `+` prefix and no `@@`
line, `*** Update File:` carries `@@` hunks whose ` `, `-` and `+` lines copy
the file verbatim, and `*** Delete File:` removes a file. A rejection names what
went wrong in the patch's own terms: the literal marker lines expected and
found, the offending line by number, a hunk that matched zero or several times
with the count, or the header to use instead (an Update of a missing file
points to Add; an Add of an existing file points to Update). Blank lines around
a patch or between its sections, trailing whitespace on marker lines and a
stray `@@ -0,0 +1,N @@` line in an Add section are tolerated, since none can be
content. Read file and patch handle UTF-8 text only; a binary or differently
encoded file is refused with the offending byte and offset, and shell commands
such as `file`, `xxd` or `od -c` inspect it.

Shell commands use the user's normal macOS permissions, with the workspace as
their working directory. They are not sandboxed. Output is bounded and commands
time out after at most five minutes. Stopping a command cannot undo changes it
already made. A foreground command's whole process group ends when the call
returns, so a trailing `&`, `nohup` or `setsid` keeps nothing running; the
result says so when a command tried one, and points to `background`. Exit code
127 adds a note that the command was not found on `PATH`: `rg`, `7z`,
`ffprobe`, `bsdtar` with RAR support and similar tools are not bundled with
Provider Hub, so they must already be installed on the Mac, and search files
covers literal text searches without `rg`. Several Pythons can be on `PATH`,
so a package one command installs may not be importable by the interpreter
another command runs; create a virtual environment in the workspace and run
both the install and the script through it.

A shell command started with `background` (a dev server, watcher or long
build) keeps running after the call returns and between turns. The call
returns its first second of output and an id such as `p1`; **read process**
returns only output the model has not yet seen, optionally waiting up to 30
seconds for the process to finish, and **stop process** ends it. Every report
states how long the process has run and how many bytes it has written in
total, so a quiet build still shows progress, and a finished one says who
ended it: `stop_process`, the user from the inspector, or its chat being
deleted or Chat shutting down. Starting one
needs the same approval as any shell command; reading or stopping the chat's
own processes does not. Each process runs in its own process group, so Stop
sends SIGTERM to everything it started and SIGKILL three seconds later; when
the command itself exits, anything it left in its group is ended too. A chat
can have eight running at once by default, with sixty-four across all chats.
Saved Team settings retain their configured process count. Teams can choose
one to eight slots in their run settings; extra launches reserve capacity for
active peers. Each member has an independent output cursor. Output is kept as a
bounded tail in memory with no separate log file; output a tool call reports
to the model is saved with the transcript like any other tool result.
Stopping a turn does not
stop its background processes: stop them in the inspector, or they end when
their chat is deleted or Chat quits. Read-only Side Chats and lanes cannot
start them. If the worker is killed outright, macOS does not end these
process groups for it; when Chat disconnects with processes running, the tab
says they may still be running.
Background processes need a Python runtime that can see a process exit
without reaping it (`os.waitid` with `WNOWAIT`; Python 3.13 or later on
macOS, as Provider Hub bundles). Without it, Chat refuses background launches
rather than risk signalling a process group whose id has been reused.

After an interruption, Chat keeps recorded results and marks unanswered tools
as interrupted. Retry resumes from those results instead of replaying completed
actions. If an action began but its result could not be saved, inspect the
workspace before retrying; Chat does not guess whether it completed.

## History, context and accents

Private JSONL files live in the Hub's state directory under `chats/`, with one
file per chat. They contain the visible transcript and a separate model history,
including opaque provider reasoning needed for continuation. They contain no
provider credentials. Writes are atomic and the directory is held by one Chat
worker at a time. Saved transcripts remain available if the gateway is offline.

For models with a known context limit, older complete conversation units are
trimmed before the limit is reached. This is routine on long turns, so it adds
no transcript notice; the full visible history remains saved and recoverable
with search_history. Unknown limits stay unknown. The token
indicator reports the latest request's input usage, rather than a lifetime
token total. With a known limit, a small ring beside it fills in the provider
accent, turning amber at 80% and red at 95% of the window; with an unknown
limit no ring is shown. A solo turn pauses after 24 model/tool rounds and can
be continued.
New Teams use Task mode: the 24-round boundary saves progress and keeps working
without a sign-off. One contribution mode keeps the earlier closing phase;
existing Teams retain that mode until changed. Members resume independently and
can wait for a process or peer without model polling. There is no lifetime Team
contribution quota. Run settings optionally limit elapsed minutes and cumulative
reported tokens; these are separate from the per-member context indicators.
Context indicators use the effective configured window. While a
Team works, the header shows every member's own ring, name and token count,
with working members in bold; members that have not run yet show an empty
ring. On narrow headers, counts and then names yield to keep every ring
visible; tooltips and accessibility labels retain each name and count.
Once the Team stops, the header returns to the chat's own context. The roster
shows every member's status and context separately.

### Recall and decision notes

The main chat can recover older visible messages with `search_history` and
`read_history`, even after they leave the model's context. Search is literal,
case-insensitive and newest-first, with bounded snippets and source entry IDs.
Reading an ID returns the exact saved text in pages, including extracted
attachment text and recorded tool results. It searches this chat only, never
other chats, helper logs or provider reasoning. Earlier messages can refer to a
previous workspace; recalled actions are records, not instructions to run again.

The model can use `record_decision` to keep a concise decision, correction,
verified finding or unresolved question, citing one to four source entry IDs.
Replacing the same key updates a note; `forget_decision` removes it without
deleting the original messages. Ask Chat to remember a decision, correct a note
or forget one. These actions appear as expandable tool rows in the transcript.
Notes are model-written reference material, not verified facts or permission;
the model is instructed to read the original sources before relying on details.
Workspace instructions and tool approvals continue to govern actions.

The notebook holds at most sixteen notes, 800 UTF-8 bytes per note and 6,000
bytes of serialized notes overall. A full notebook requires explicit replacement
or removal; it never silently evicts an older decision. Each `record_decision`
and `forget_decision` result reports the capacity used (notes and bytes, across
every workspace scope that counts toward the caps), and a full notebook's
refusal names those counts and how many bytes the note would have added.
Rejections distinguish an empty text from one over the byte limit, naming its
UTF-8 size, and a repeated source ID from one that is not a visible entry,
since multi-byte characters and tool call IDs were the usual causes. Notes share the chat's
atomic save, survive reopening and model/account changes, and are shown only
in the workspace where they were recorded. Switching back restores that view.
They reserve space before context trimming and are projected into each request
without being duplicated in saved model history. Team members share this
notebook and recall the same attributed transcript. Helpers and Side Chats cannot
call the memory tools or mutate the notebook. No background summarizer, extra
provider request or cross-chat memory store is involved.
Team chats also have a Blackboard of short pinned posts and reference files,
shown in the Team tab; like notes, its posts are model-written (or yours) reference
material, not verified facts or permission. See [Team in Chat](chat-team.md#blackboard).

If the notebook would consume more than a quarter of a model's context, Chat
omits it for that request with a notice to the model; the notes stay saved and
transcript recall remains available. Invalid notebook metadata is likewise
excluded without hiding the transcript. New messages record their workspace;
legacy messages may have no workspace attribution. Explicitly different
workspace sources cannot be used for a new note without a current clarification.

Provider accents and labels resolve through `branding.py`,
`provider_branding.json`, and existing user overrides. This is the same
TaskWraith-compatible presentation contract used throughout Provider Hub. Chat
does not maintain its own provider-colour table. Presentation is display data;
routes and account identities continue to govern requests.
Provider marks use the Hub's existing icon assets. The four tool glyphs reuse
Tool glyphs reuse TaskWraith's tool-call icon catalogue (file, shell, search,
patch, browser, memory, delegate, status and handoff) as cached native vectors.

Chat is a coding harness with attachments, local tools, concurrent chats,
persistent parallel Teams and bounded delegation. Mutating tools in one checkout
run serially. Development follow-ups add recursive panes, a native shared
browser and optional Browser Use / Computer Use plugins; see
[Chat capabilities and panes](chat-capabilities.md) for dependencies,
interaction details and qualification limits. It has no agent mailboxes,
schedules, connector catalogue or separate IDE.

## Implementation and verification

`ChatWindow.swift`, `ChatComposerText.swift`, `ChatModelPicker.swift` and `ChatModel.swift` are the native window,
composer text view, picker and local JSONL transport. `ChatTranscriptLayout.swift` decides what the transcript draws
(speaker blocks, folds, tool phrasing, source links and notices) without views,
and `ChatTranscriptRows.swift` draws it. `chat_memory.py` provides transcript recall and source-linked decision
notes. `ChatInspector.swift`, `ChatInspectorModel.swift` and
`ChatBranches.swift` provide the inspector and Git controls. `chat_runtime.py`
owns each conversation/tool loop and saved chats; `chat_sessions.py` hosts
independent chat sessions in one process and routes events by chat identity.
`chat_team.py` and
`ChatTeam.swift` provide persistent membership, parallel contributions, queued
workspace writes/approvals and roster UI.
`chat_agents.py` provides
isolated delegates and memory-only Side Chats, and `chat_inspector.py` handles
bounded Git inspection and explicit branch/worktree actions;
`chat_git_overview.py` and `ChatGitOverview.swift` provide paged repository
metadata and the independently scrolling Git pane;
`chat_tools.py` executes the four local tools. The existing authenticated
gateway serves `/_bridge/chat/models` and accepts Chat requests through
`/v1/messages`, keeping all API and CLI provider adaptation in its current
modules. CLI continuity, signed reasoning, and provider account ownership remain
with those adapters.

Run the repository suite with uv CPython 3.13. Chat tests cover approvals,
cancellation, recovery, account isolation, branding overrides, malformed
streams, actual Swift model transitions through a fake worker, and the real
composer text view with input-method compositions. Local HTTP
fixture tests exercise the real gateway without contacting a provider account.
Build the native app with `bash Source/build.sh`.

## Transcript loading

Worker events retain the existing JSONL `selected` snapshot semantics. Swift
also accepts `snapshot` events for each chat subscribed by a visible pane,
without changing the selected chat. Selection failures include the originating
request identity so a deleted chat cannot leave later selections blocked. Swift
frames and parses events and decodes transcript, catalogue, chat-list and Team
payloads on a serial background queue, then applies them in FIFO order on the
main actor. Each worker gets a new reader; queued events from an older process
are ignored after reconnecting. Streaming offsets and inspector request IDs
therefore retain their original ordering.

A transcript travels as one line, which the pipe delivers in 16 KB reads.
Framing searches each byte once; rescanning the buffer on every read made a
35 MB transcript take minutes to connect. The worker sends a transcript once
before `ready`, and once per chat in a subscription: the `snapshot`, followed
by that chat's helper, Side Chat and Team state.

The transcript initially renders the last 100 layout segments. “Load earlier
messages” reveals 100 more at a time, preserving the full transcript and its
speaker/fold grouping while bounding layout during a chat switch. Workspace
history is persisted only when its folder order changes.
