# AntiGravity desktop tool handoff

Provider Hub adapts agy's tool protocol to the tools offered by the calling
desktop. Codex uses Responses function calls; Claude uses Messages tool-use
blocks. Both reuse the hub's existing validated host-call relay.

## Failure observed on 19 September 2026

The Codex attempt called `exec_command` as an agy-native tool, which agy
rejected as unknown. The Claude attempt put a valid host reply inside native
`send_message` to recipient `user`, which agy rejected because that local
recipient did not exist. The old adapter rejected all native tool events with
HTTP 400, so neither host received the requested action.

Live probes also found that agy implements structured completion with its
`finish` tool. `result.response` can contain internal `toolAction` and
`toolSummary` fields; `result.structured_output` contains the clean payload.

## Adapter behavior

Every structured turn gets a private temporary workspace and a scoped
`PreToolUse` hook. User configuration and provider credentials are unchanged.
The hook explicitly allows `finish` and reads of the supplied image copies.
It records and denies other native requests before their execution.

The adapter requires both a matching conversation/step receipt and agy's
confirmation that the hook denied the action. An `ACTIVE` event or a receipt
alone is insufficient. It stops the native process before handing a translated
request to the desktop. A completed native action is never reissued.

Supported translation paths are:

- Exact offered host tool names, with agy's display metadata removed.
- Native `run_command` to offered `exec_command`, `Bash`, or `run_shell_command`.
- Native `view_file` to offered `Read`, `read_file`, or a quoted read command;
  explicit line ranges are preserved.
- Native directory listing to offered `list_directory` or a quoted list command.
- A structured host reply wrapped in native `send_message` to `user`.
- A confirmed unknown-tool rejection for a host tool name, which proves agy
  never dispatched that operation.

All translated calls pass through the existing host-name, tool-choice, batch,
and JSON validation. The desktop applies its tool schema, permissions and
approvals. Captured operations without a supported translation get the existing
single protocol-correction attempt. Missing capture proof, cancellation and
unresolved native activity remain errors. Actual host results retain their
call IDs in the next stateless turn.

## Relationship to TaskWraith

The reference checkout at `/Users/chrisizatt/Documents/AGBench` uses a native
PreToolUse approval bridge and projects provider events into a common activity
format. Some of that projection is display-only, after native execution.
Provider Hub needs a different execution boundary: it captures before execution
and emits the calling desktop's tool request. Post-execution projection cannot
be reused as an executable request without risking duplicate actions.

## Verification

Regression tests exercise the real temporary hook script, confirmed denials,
stale receipts, completed actions, unknown host tools, both desktop tool names,
structured completion, image scoping, cleanup and the single-correction limit.
Live Gemini 3.1 Pro probes completed a host read and final-answer round trip
for both Messages and Responses with one host read each and no replay.

This is a bounded adapter for the observed agy protocol, not a claim that every
future native tool can be translated. Rebuild Provider Hub and reload its worker
to use source changes in the desktop applications.
