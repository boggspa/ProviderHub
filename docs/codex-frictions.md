# Codex route friction repairs

## Failures reproduced from the 9 October 2026 reports

`Responses request is too large or empty` was Provider Hub's local 32 MiB
request gate. It ran before model selection or image-history reduction, so
the same Luna model could work in a native thread and fail behind the hub.
Image base64, tool schemas and history all count toward this byte limit;
it is separate from the model's token context window.

Ordinary Responses requests now have a 64 MiB working budget. Between that
budget and the 128 MiB hard ingress limit, the gateway returns the client's
`context_length_exceeded` signal so it can compact instead of reconnecting.
Both `/responses/compact` and older clients' checkpoint prompts through
`/responses` can receive the same history up to the hard limit. The Messages
hop also accepts 128 MiB. Provider response/event limits stay at 32 MiB.
Empty requests are malformed requests (400), while requests beyond the hard
limit still fail explicitly (413). This does not promise recovery from an
arbitrarily large single attachment or an indivisible pending tool batch.

`Codex attempted a native CLI tool (imageGeneration)` was a bridge policy
failure, not an image service connection failure. The nested runtime's image
tool was enabled by default while its events were rejected. It is now off in
the default/fallback configuration and enabled per thread for the Codex
Responses surface, which can represent it. Progress and results become
`image_generation_call` transcript items with stable identities. A failed
image stays a failed tool result; the model can continue and use a host tool.
Native shell, file modification, image viewing, plugins and agent spawning
remain controlled by the existing host-tool path.

An invalid host-tool alias or non-object arguments receive a `success:false`
RPC response before anything executes, allowing correction in the same turn.
Three rejected calls are allowed before a terminal protocol error. This
does not authorize an unoffered tool or change approval handling.

Unsupported, remote or oversized media blocks returned by host tools become
explicit placeholders for the Codex model. Other blocks, call/result pairing,
and the host's success/error flag survive. The original host transcript is
unchanged; no remote image is fetched and no pixels are resized or invented.
User attachment validation stays strict. Generated image history uses the
same honest fallback and can be compacted without feeding base64 to the
summarizer. Switching to a text-only route retains an image outcome note.

## Inline visualizations from non-GPT models (10 October 2026)

A Claude Fable thread ended with its `visualize{"path":…}` line printed as
text instead of the inline chart. The installed skill and desktop parser
both require U+E200 before `visualize`, U+E202 before the JSON and U+E201
after it. The recorded reply lacked those delimiters. A previous `codex/`
reply preserved them, so this was not a universal visualization failure.
The available evidence does not establish why the model omitted them.

Provider Hub now wraps a bare reference line in those sentinels on both
output paths: the Messages adapter behind translated and CLI routes, and the
native Responses relay. Deltas are rewritten line by line, holding back only
a line that could still become a reference, so the reference does not flash
as raw text while streaming, and every completed shape (`output_text.done`,
`content_part.done`, `output_item.done`, the terminal response) carries the
same wrapped text. Lines already wrapped, lines inside fenced code and
objects without a non-empty `path` string are left alone. This fallback
handles the observed malformed reference without assuming which provider
will produce it. The model still writes the fragment itself into the
thread's visualization directory; the hub changes no paths and adds nothing
to the request.

Regression coverage uses synthetic replies and local HTTP fixtures, including
split keywords, non-empty opening blocks, fenced examples, interleaved text
parts and completion without a trailing newline. An installed Desktop render
with this patch is still a manual qualification step.

## Qualification

### Build 59 native-command regression

The running v0.5.6 build 59 (`c49b85d`) included the original repairs but
still failed with `Codex attempted a native CLI tool (commandExecution)`.
An offline probe of the installed Codex 0.162.0-alpha.2 runtime reproduced
the cause: `thread/start` replaces the `features` table. The image repair's
`{"features":{"image_generation":true}}` override discarded the argv's
`features.shell_tool=false` and the other feature restrictions. Native
commands became available again, and the existing guard terminated the run.

The image thread override now copies **all** transport feature restrictions
before enabling image generation. A synthetic native-command attempt is
therefore answered by the runtime as an unavailable tool, and a corrected
host-tool request continues in the same turn without the bridge's fatal
native-command event. The guard remains for unexpected native activity.

Run `uv run --python 3.13 python scripts/probe_codex_native_commands.py` to
verify the installed runtime against a local scripted provider. The optional
`--reproduce-legacy` case restores the faulty table in an isolated scratch
thread and attempts only a fixed `printf`, demonstrating the old failure.
No user credentials or real model calls are involved. Unit tests also require
new transport feature restrictions to survive future image-thread overrides.

Deterministic tests exercise the real local HTTP gateway with scripted Codex
app-server events: streamed and buffered images, image failure followed by
host work, interleaved outputs, replay, compaction, request budgets, invalid
tool-call correction, and per-block media recovery. No external model calls
or generated images are used by those tests. Wire fields were checked against
the installed ChatGPT-bundled Codex app-server JSON schema. Actual image
rendering in a running desktop app still needs a tester pass after installing
a build containing these changes; editing source does not update a running
Provider Hub installation.

## Voice with a ChatGPT account

[OpenAI's voice documentation](https://learn.chatgpt.com/docs/features/voice)
describes GPT-Live voice directing work through an existing task's selected
model. Account, rollout and workspace availability still apply. In the
installed desktop bundle inspected on 9 October, voice has a direct ChatGPT
`/realtime/status` path and can attach an existing voice call through
`thread/realtime/start`. Provider Hub itself implements neither endpoint.

This makes voice alongside a hub-backed work model plausible; it does not
establish a tested custom-provider combination. Provider Hub's **Show your
ChatGPT account** setting preserves the sign-in UI. It is not a guarantee of
voice availability or a replacement for ChatGPT plan access. With provider
accent colours enabled, macOS attributes microphone permission to Provider
Hub, as the preference already explains. This repair does not change voice
authentication, endpoint routing, permissions, or feature gates.

## "Select a project to continue" after one message

A tester's hub-launched Desktop refused follow-ups with "Unable to send
message / Select a project to continue". A new chat sent one message and then
locked again. Their ChatGPT usage was fine; this is not the usage wall, and
**Keep sending when usage runs out** cannot clear it.

The dialog is Desktop's `missing-workspace` submit block, read from the
ChatGPT 26.1007.21159 bundle. The composer blocks a local chat whose workspace
roots contain nothing but `/`, unless Desktop counts the chat as projectless.
A chat in a project starts in the project's primary folder. The tester's
project had been created on the whole disk, so its chats started in `/`.
Before a chat exists, the composer falls back to the home folder, so the
first message goes through. Every later message is checked against `/` and
refused. One locked chat used the native `openai` provider, so hub routing
is not involved. Desktop allows creating a project on `/` but then won't let
its chats continue; that inconsistency is Desktop's.

Provider Hub now reports it. `codex_projects.py` reads both of Desktop's
project records read-only and with bounded time: `local-projects` in
`~/.codex/.codex-global-state.json`, and the app-server's
`projects`/`project_roots` tables in the newest `state_*.sqlite`. It names
every local project whose primary folder is `/`. The worker's
`codex-projects` command and `codex-prepare` return those names. The hub
then shows a warning in the Codex preferences and appends it to the launch
notice. It reads no chats or titles, and missing files or schema drift give
no warning rather than an error.

The hub does not fake a folder to get past the block: that would run a
full-access agent in a folder the user never chose. The fix is in Desktop.
Choose **Edit project**, add a specific folder (the home folder works, and a
full-access agent can still read the rest of the disk from there), make it
**primary**, save, and start a new chat. Chats started before the change keep
`/` as their working directory and may stay blocked.

For a composer that refuses to send for another reason, run
`scripts/diagnose_codex_composer.sh` while the dialog is showing, before
starting a new chat or relaunching. It is read-only and needs nothing
installed. It prints app versions and the hub's Codex switches. It also
prints the global-state inputs the composer reads, projects rooted at `/`,
each recent chat's working directory and projectless status, and each turn's
working directory. Message text, titles, tokens and keys are left out;
project names and folder paths are included.
