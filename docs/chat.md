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
connections also start a fresh provider context. Changing workspace opens a new
chat, leaving the original transcript in the rail.

The sidebar groups chats under their workspace folders. Collapse a group or use
its **+** to start a chat there. Folders remain in the picker and sidebar after
restart or after deleting their last chat. Path aliases share one group;
different folders with the same name show their paths. Disconnected folders
stay in the list and their saved chats can still be read. The selected row
carries a thin bar in its model's provider accent. Each running chat shows a
small spinner; a hand marks a chat waiting for approval, including chats in
another folder.
In a narrow window, the saved-chats button opens the same list in a popover.

You can switch chats or start a new one while other chats run. Each chat keeps
its own model or Team, workspace, draft, attachments, elapsed time, and pending
approval. Stop and updates affect the chat you are viewing. Different projects
can work at the same time. Chats sharing a checkout serialize patches and shell
commands, and branch changes wait until all chats using that checkout are idle.

One worker hosts the chats. Idle chats have no execution loop, and only the
visible transcript streams into the window. Selecting a background chat restores
its latest output. Up to four chats can run at once, with at most eight model
requests across chats, helpers and Side Chats. When the chat limit is reached,
the message stays in your draft so you can send it
after a turn finishes.

Return sends a message; Shift-Return inserts a newline. Stop cancels the selected chat's
request and local command execution, keeping partial output and recorded
actions. Closing the window leaves active work running; reopen it from the menu
bar. Quit the Hub after work has stopped.

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
completion, failure or an acknowledged Stop resets it to `00:00`. While a turn
runs, the status word beside the approval mode and the **Thinking…** label in
the transcript sweep the model's provider accent; with Reduce Motion on they
take the accent without movement.

The header's unboxed Git indicator shows changed files, tracked added/deleted
lines, and gold/blue ahead/behind counts against the cached upstream. It never
fetches a remote. With no upstream, only a positive local-commit count appears;
its tooltip labels that distinction. Git inspection runs separately from the
conversation loop so it cannot block Stop.

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
  context lines. It includes untracked files and reports binary or truncated
  changes. Reads use the local checkout and never fetch a remote.
- **Team** configures up to three persistent members, including the current
  chat model. Each contributes once by default and may explicitly continue with
  a next step. Members think and read in parallel through one attributed
  transcript, refreshed before every model round, with separate provider histories.
  Workspace changes and approvals queue one at a time. Stop, unfinished
  work and questions are visible in this pane. See [Team](chat-team.md).
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

The inspector collapses the left workspace rail on narrower windows to keep the
conversation usable. The main turn clock and context count describe the parent;
Side Chat runs independently.

The small gear at the bottom-left of the sidebar offers Allow web search, Theme, Glass/Solid,
Font, and Text size. Font choices include System, System Mono, Inter, Source
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
Markdown. The composer stays plain text.

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
and YOLO red, which also tints the composer outline faintly. Choosing YOLO in a
folder for the first time asks once to confirm; that answer is remembered per
folder on this Mac and never sent to the worker.

The transcript reads like Claude and Codex Desktop. Your messages sit in a
bubble on the trailing edge. Each speaker (the chat's model, or a Team member
by name) gets one header above a run of replies and tool calls, rather than a
header per model round; rounds that produced no text draw nothing.
Tool rows lead with what was done ("Ran `swift build`", "Edited Model.swift",
"Read tools/import.py · lines 85–259", "Searched for `water_height`") instead
of the folder every call shares, and their glyph wears the caller's provider
accent, so Team members' calls are recognisable at a glance. A row lifts on
hover and expands to show output and textual diffs; a shell row shows its full
command above the output. A patch row shows its added and removed line counts
in the Git indicator's colours. A call still running shimmers its verb.
Three or more consecutive local tool rows fold into one disclosure such as
**Worked · 4 commands, 2 edits** with the combined file and line counts; the
run at the end of a live turn stays open as **Working** and folds once the
reply begins, and a fold opened or closed by hand keeps that choice. Delegate
rows keep their own lanes and are never folded. Recorded changed files can be
revealed in Finder.

Web search sources collapse into one pill under the reply (**12 sources ·
github.com · …**) that opens into a numbered list; copied and saved text keeps
the original **Sources:** line. Notices are quiet dividers, and a Team
checkpoint reads as a handoff naming the member who paused, with the runtime's
full wording in its tooltip. Older chats saved an "Older model context trimmed"
notice on most rounds of a long turn; those are no longer drawn.

Shell commands use the user's normal macOS permissions, with the workspace as
their working directory. They are not sandboxed. Output is bounded and commands
time out after at most five minutes. Stopping a command cannot undo changes it
already made.

A shell command started with `background` (a dev server, watcher or long
build) keeps running after the call returns and between turns. The call
returns its first second of output and an id such as `p1`; **read process**
returns only output the model has not yet seen, optionally waiting up to 30
seconds for the process to finish, and **stop process** ends it. Starting one
needs the same approval as any shell command; reading or stopping the chat's
own processes does not. Each process runs in its own process group, so Stop
sends SIGTERM to everything it started and SIGKILL three seconds later; when
the command itself exits, anything it left in its group is ended too. A chat
can have four running at once, Chat eight in total. Output is kept as a
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
Team uses the same bounded execution slices with a reserved checkpoint sign-off.
Members run together; explicit continuations join the next wave after current
contributions settle. There is no lifetime Team contribution quota. Usage and
context belong to individual members and are never added together. While a
Team works, the header shows every member's own ring, name and token count,
with working members in bold; members that have not run yet show an empty
ring. Once the Team stops, the header returns to the chat's own context. The
roster shows every member's status and context separately.

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
or removal; it never silently evicts an older decision. Notes share the chat's
atomic save, survive reopening and model/account changes, and are shown only
in the workspace where they were recorded. Switching back restores that view.
They reserve space before context trimming and are projected into each request
without being duplicated in saved model history. Team members share this
notebook and recall the same attributed transcript. Helpers and Side Chats cannot
call the memory tools or mutate the notebook. No background summarizer, extra
provider request or cross-chat memory store is involved.

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
run serially. It has no agent mailboxes, schedules, connector
catalogue, browser automation, or separate IDE.

## Implementation and verification

`ChatWindow.swift`, `ChatModelPicker.swift` and `ChatModel.swift` are the native window, picker and local JSONL
transport. `ChatTranscriptLayout.swift` decides what the transcript draws
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
`chat_tools.py` executes the four local tools. The existing authenticated
gateway serves `/_bridge/chat/models` and accepts Chat requests through
`/v1/messages`, keeping all API and CLI provider adaptation in its current
modules. CLI continuity, signed reasoning, and provider account ownership remain
with those adapters.

Run the repository suite with uv CPython 3.13. Chat tests cover approvals,
cancellation, recovery, account isolation, branding overrides, malformed
streams, and actual Swift model transitions through a fake worker. Local HTTP
fixture tests exercise the real gateway without contacting a provider account.
Build the native app with `bash Source/build.sh`.
