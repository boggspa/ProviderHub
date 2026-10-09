# Native ChatGPT capabilities in Provider Hub

In Provider Hub settings, turn on **Use native ChatGPT capabilities**, save,
then launch Codex / ChatGPT from the Hub. This uses the existing
`codex_chatgpt_account` setting, so previous account-enabled configurations
remain compatible. The default stays off.

Sign in with your ChatGPT account in Desktop. Provider Hub retains native
account access while model requests use the loopback gateway and its separate
session credential. ChatGPT credentials are not copied into provider requests.
During this launch, the verified `image_generation`, `in_app_voice` and
`realtime_conversation` controls are enabled. Quitting and restoring the
profile returns their previous values; later user edits survive restoration.
Accountless launches leave these native feature preferences untouched.

## Voice

Use **Start voice chat** in Desktop. The intended arrangement is Voice on
ChatGPT's native service, attached to a Codex task whose work uses the selected
Hub model. The installed app contains this attachment path, but the combination
still needs live qualification. Hub does not proxy realtime audio or replace
Voice with dictation. If the desktop helper is enabled for colours or the quick composer,
macOS attributes microphone permission to Provider Hub.

Availability still depends on sign-in, plan, workspace, rollout and the
installed Desktop/host versions. The native account status in Hub reports the
applied launch configuration, not successful authentication or entitlement.
A changed preference needs a save and relaunch. Voice with a custom-provider
task requires the live qualification below before it is claimed as supported.

## Image generation

Codex model routes already enable the native hosted image tool on the
Responses surface and relay its progress, result and failed status. Other
models can call Desktop's image-generation host tool when Desktop offers it.
The normal host-tool bridge retains its schema, execution and approvals.
Enabling account mode does not fabricate a tool or turn a hosted OpenAI wire
tool into a third-party model API tool.

Provider-routed text uses its selected provider account. Native Voice and
images retain OpenAI account limits. A selected extra Codex CLI account can
differ from the Desktop account: nested Codex image generation uses that CLI
account, while Desktop host tools and Voice use the Desktop account.

When native mode and usage-banner hiding are both enabled, account/image-limit
and unrecognized notices remain visible. Only separately identified per-model
text usage notices are hidden. Composer unlock applies to sending
provider text; it cannot grant native usage. Its adjusted core usage meter
is not evidence that Voice or images are available. Desktop updates may change
banner structure; unknown notices stay visible, so some ordinary text usage
banners may reappear. Account/usage pages and submit errors remain the
authoritative diagnostics.

## Qualification

Automated tests use disposable homes, scripted runtime events and local
browser fixtures. They verify configuration ownership/restoration, active
mode status, native image progress/failure forwarding, and image-limit banner
preservation without spending account usage.

Before marking the combination supported, test the built app with an eligible
signed-in account:

1. Enable native mode, launch through Hub and confirm sign-in remains visible.
2. Select a Hub Codex route, generate an image, inspect the result, and edit it
   in the same chat. Check that a failed generation stays a failed tool result.
3. Select a non-OpenAI route and repeat through Desktop's offered image tool.
4. Start Voice on an existing task; issue work, interrupt, and steer the task.
   Confirm work reaches the selected provider and Voice stays on ChatGPT.
5. Repeat with banner hiding and composer unlock. Confirm image-limit notices
   and real native quota failures are visible while provider text can continue.
6. Quit Desktop and restore. Confirm native settings and account files are
   preserved. Repeat with native mode off and with an extra Codex CLI account.

Record Desktop and bundled runtime versions, account/workspace eligibility,
selected route, and observed results. Configuration switches alone do not
establish that this matrix passed.

Official references: [ChatGPT Voice](https://learn.chatgpt.com/codex/features/voice)
and [image generation](https://learn.chatgpt.com/codex/image-generation).
