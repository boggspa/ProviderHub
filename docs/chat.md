# Chat in Provider Hub

Choose **Chat with Model…** from the menu bar, or the chat button in the compact
Hub window. Chat runs in its own native window alongside Claude and Codex, using
the provider connections already configured in the Hub.

The header selects a model/account, reasoning level, and workspace folder.
Reasoning choices come from that model's actual ladder. Model and account
choices do not alter either desktop client's configuration. Changing the model,
account, or folder after a conversation begins opens a fresh chat; the earlier
transcript stays in the saved-chat rail. A changed connection requires a fresh
chat as well, so provider reasoning never crosses accounts or routes.

Return sends a message; Shift-Return inserts a newline. Stop cancels the current
request and local command execution, keeping partial output and recorded
actions. Closing the window leaves active work running; reopen it from the menu
bar. Quit the Hub after work has stopped.

## Files and commands

The four tools are read file, search files, apply patch, and run shell. File
tools stay inside the chosen workspace and reject symlink paths. Patch and
shell requests display the proposed action above the composer and wait for
**Allow once** or **Deny**. Expand a proposed patch to review it before allowing.
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

The first version is a text coding chat with local tools. It has no agent teams,
schedules, connector catalogue, browser automation, or separate IDE.

## Implementation and verification

`ChatWindow.swift` and `ChatModel.swift` are the native window and local JSONL
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
