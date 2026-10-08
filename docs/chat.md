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

The header's unboxed Git indicator shows changed files, tracked added/deleted
lines, and gold/blue ahead/behind counts against the cached upstream. It never
fetches a remote. With no upstream, only a positive local-commit count appears;
its tooltip labels that distinction. Git inspection runs separately from the
conversation loop so it cannot block Stop.

The small gear at the bottom-left of the chat rail offers Theme, Glass/Solid,
System/Monospaced font, and Small/Default/Large text. Theme and surface reuse the
Hub's existing preferences. Text choices affect the transcript and composer.

## Files and commands

The four tools are read file, search files, apply patch, and run shell. File
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

The first version is a coding chat with attachments and local tools. It has no agent teams,
schedules, connector catalogue, browser automation, or separate IDE.

## Implementation and verification

`ChatWindow.swift`, `ChatModelPicker.swift` and `ChatModel.swift` are the native window, picker and local JSONL
transport. `chat_runtime.py` owns the conversation/tool loop and saved chats;
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
