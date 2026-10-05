# Codex Desktop Side Chat with external providers

Investigation: 2026-10-05, ChatGPT Desktop 26.930.51102 (build 13100),
bundled `codex-cli 0.160.0`.

## Finding

There is no native-model-only check in the inspected local Desktop Side Chat
creation path. An isolated local fake-provider probe successfully forks a
persistent chat into an ephemeral side chat, injects a reference boundary,
and completes a side turn using provider-qualified external model IDs.
Equivalent simple Side Chat requests also pass Provider Hub's offline
Responses-to-Messages preparation for the Mistral and Gemini routes.

This demonstrates protocol compatibility in the installed runtime. It does
not reproduce the user's reported failure or prove that every real provider,
saved history, attachment, or tool cycle works. No production configuration
or Desktop patch is justified by these results alone.

The retained probe passes five routing cases and seven offline Hub request
translations, preserving the parent's transcript. A separate temporary
probe also completed a side turn before releasing a parent whose fake
provider response was deliberately held open. That concurrency check used
simple text history; unfinished tool calls and reasoning are not qualified
by it.

[Official OpenAI documentation](https://learn.chatgpt.com/docs/developer-commands#start-a-side-chat-with-side)
describes `/side` as an ephemeral fork that keeps a separate transcript.
The documentation does not establish third-party provider compatibility.

## Installed Desktop trace

The following paths are entries inside
`/Applications/ChatGPT.app/Contents/Resources/app.asar`, not editable
repository source. Generated names and line numbers can change on update.

- `webview/assets/app-initial-f9b16fbf8fc7.js`, line 4585: `ems` starts a
  normal fork with `ephemeral: true`, `sideConversation: true`, the parent's
  collaboration mode, workspace roots and Side Chat developer instructions.
- `.vite/build/bootstrap-CXJAEjVI.js`, line 615: the fork sends
  `thread/fork` with the source thread, `excludeTurns: true`,
  `ephemeral: true`, capability overrides and developer instructions.
  It then calls `thread/inject_items` to mark inherited history as reference
  context. The GUI omits an explicit fork model.
- The capability helper called `readCodexConfig` resolves to
  `buildMcpCodexConfig` in `.vite/build/main-C7cfj__D.js`, lines 490 and 709.
  It supplies Desktop MCP/capability overrides rather than copying the whole
  root model/provider TOML into the request.
- Both inline `/side question` submission and the side composer preserve the
  parent's collaboration mode. Existing-thread model settings come from
  that mode. See `app-initial`, lines 1679 and 1854, and
  `webview/assets/app-primary-c0280d43ce72.js`, line 161.

The runtime's fork defaults deserve care: with configured model A, switching
a parent's turn to B and forking without an explicit model returns A in the
fork response. A side turn with the parent's collaboration mode requests B.
Desktop carries that mode through both Side Chat entry paths, so the
root-default observation by itself is not a demonstrated GUI inheritance
failure or a reason to pin another global TOML setting.

The GUI does not wait for an active parent before requesting its fork.
Snapshot consistency of unfinished history belongs to the backend runtime.

## Repeatable offline probe

```sh
uv run --python 3.13 python scripts/probe_codex_side_chat.py
```

The probe uses a disposable `CODEX_HOME` and a deterministic loopback
Responses server. The external-looking model IDs identify synthetic models;
neither Mistral nor Gemini receives a request. It does not read credentials,
submit real workspace tools, change the user's configuration, or open the
desktop UI. Its JSON output distinguishes GUI-equivalent inherited-mode
turns from the API control that omits that mode. The injected reference
boundary is synthetic, not a byte-for-byte copy of the Desktop prompt.

The launch check in `Source/codex_runtime.py` still qualifies only
`model/list`. A successful prepared catalogue is not a Side Chat guarantee.
This probe is a separate diagnostic, not a new prerequisite for launching.

## Locating a real failure

Capture the exact error, model route, whether the parent was still running,
and whether the failure occurred while opening the side panel or sending its
first prompt. In the Provider Hub state directory, `last-responses-shape.json`
records model IDs, top-level fields, history item types and tool names without
message bodies. `activity.jsonl` records request outcomes. These are shared
across requests: another parent/background turn can overwrite the latest
shape, so correlate timing and do not assume it necessarily belongs to the
side chat. Desktop logs under `~/Library/Logs/com.openai.codex/` can locate
failed fork or injection RPCs.

If the failure occurs before an inference request reaches Hub, investigate
the Desktop/app-server fork, configuration and history initialization. If the
request reaches Hub, compare its route and history shape with a successful
main turn, then reproduce the specific rejected payload in an offline test
before changing translation. Inherited tool cycles, reasoning state,
attachments and provider-specific effort/tier validation remain relevant
areas that the simple text probe cannot fully qualify.

There is no separate Side Chat HTTP route in Hub. Its ordinary Responses
handler and `Source/responses_native.py:prepare_native` handle these turns.
The previously documented ephemeral-parent subagent failure in
`docs/codex-provider-subagents.md` concerns a nested CLI collaboration fork;
it is not evidence that GUI Side Chat has the same failure.
