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
stay in the list and their saved chats can still be read.

Return sends a message; Shift-Return inserts a newline. Stop cancels the current
request and local command execution, keeping partial output and recorded
actions. Closing the window leaves active work running; reopen it from the menu
bar. Quit the Hub after work has stopped.

Use **+** or drop files onto Chat to attach them. Images have small thumbnails
in the composer and transcript; click one to view it. Other files use compact
chips. Text/code files and PDFs with extractable text are sent as text, and PNG,
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

**Turn time** beneath the composer counts the active turn, including tool and
approval waits. Steering and provider-request restarts keep the same clock;
completion, failure or an acknowledged Stop resets it to `00:00`.

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
Creating a worktree leaves
the current selection in place until you choose the new one. Choosing a
worktree keeps this chat, records the workspace change, archives its old provider
context and starts fresh portable context for the new directory. Changes are
available only between turns, including Side Chats using that workspace.

The right-hand **inspector** has three views, each using the whole pane:

- **File Changes** lists file diff counts and expandable patches with three
  context lines. It includes untracked files and reports binary or truncated
  changes. Reads use the local checkout and never fetch a remote.
- **Subagents** lists delegated tasks by model and task. Choose one to read its
  transcript and tool results. There is no child composer; it reports back to
  the parent. Parallel read-only tasks also appear as model/task chips beside
  their originating delegate row in the main transcript. Child approvals for
  serial helpers identify the requesting model in the normal strip.
- **Side Chat** forks the visible conversation at that point with any enabled
  model. It has its own composer and Stop/interrupt controls, with read/search
  tools only. Provider reasoning stays isolated. Side Chats remain in memory
  while you switch parent chats, keep their original workspace and are discarded
  on explicit close, deletion of their parent, or quitting the app. They are not
  written to Chat's saved JSONL history. At most eight can be open at once.

The inspector collapses the left workspace rail on narrower windows to keep the
conversation usable. The main turn clock and context count describe the parent;
Side Chat runs independently.

The small gear at the bottom-left of the sidebar offers Theme, Glass/Solid,
Font, and Text size. Font choices include System, System Mono, Inter, Source
Serif 4 and JetBrains Mono. **Custom…** opens the native macOS font panel for
fonts installed through Font Book. Only the chosen font name is saved; custom
font files are never copied. An unavailable custom font falls back to System.
Theme and surface reuse the Hub's existing preferences. Text choices affect
the transcript and composer; code and diffs keep their monospace treatment.

## Files and commands

The local tools are read file, search files, apply patch, and run shell. The
parent also has **delegate**, which runs one helper at a time and returns its
recorded result. Helpers inherit the workspace and approval mode, get the four
local tools and cannot delegate. A turn can launch at most four helpers, with
twelve model/tool rounds per serial helper. Its alternative `tasks` form runs
two or three read-only lanes together, with eight rounds per lane; all count
toward the same four-helper limit. Read-only lanes have read/search tools only,
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
Tool rows in the transcript expand to show output and textual diffs. Recorded
changed files can be revealed in Finder.

Shell commands use the user's normal macOS permissions, with the workspace as
their working directory. They are not sandboxed. Output is bounded and commands
time out after at most five minutes. Stopping a command cannot undo changes it
already made.

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
trimmed before the limit is reached. A notice records this in the transcript;
the full visible history remains saved. Unknown limits stay unknown. The token
indicator reports the latest request's input usage, rather than a lifetime
token total. A turn pauses after 24 model/tool rounds and can be continued.

Provider accents and labels resolve through `branding.py`,
`provider_branding.json`, and existing user overrides. This is the same
TaskWraith-compatible presentation contract used throughout Provider Hub. Chat
does not maintain its own provider-colour table. Presentation is display data;
routes and account identities continue to govern requests.
Provider marks use the Hub's existing icon assets. The four tool glyphs reuse
TaskWraith's file/search/patch/shell paths as cached native vectors.

Chat is a coding harness with attachments, local tools and bounded delegation.
It has no writable parallel teams, agent mailboxes, schedules, connector
catalogue, browser automation, or separate IDE.

## Implementation and verification

`ChatWindow.swift`, `ChatModelPicker.swift` and `ChatModel.swift` are the native window, picker and local JSONL
transport. `ChatInspector.swift`, `ChatInspectorModel.swift` and
`ChatBranches.swift` provide the inspector and Git controls. `chat_runtime.py`
owns the shared conversation/tool loop and saved chats; `chat_agents.py` provides
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
