# Provider Hub Preview 0.5.0

Use your model accounts inside Claude Desktop or the Codex coding workspace in Codex / ChatGPT Desktop.

## Install

1. Unzip the Apple Silicon download.
2. Drag **Provider Hub Preview.app** into **Applications**, then open it.
3. Open **Models & Settings…** from its menu bar icon whenever you need it.

Requires an Apple Silicon Mac (M1 or newer) with macOS 14 or newer. This distribution is Developer ID signed and Apple-notarized, and includes its own Python runtime. You do not need to install Python or run terminal commands. Install the desktop app you want to use separately.

## Connect a provider

In **Providers**, choose an account and configure its credential source. API
keys are stored in your Mac's Keychain. The download contains no account keys,
settings, or conversations from its developer.

- **Mistral:** use the API key already saved by Vibe, or enter your Mistral API
  key directly. Starting Vibe is optional when using an API key directly.
- **Kimi, MiMo, DeepSeek, Cerebras, Muse, Grok, Qwen Token Plan, OpenRouter, or Gemini:** configure the corresponding account/key in its card.
- **Ollama:** run your existing Ollama app or daemon. It manages its own models,
  downloads, and cloud sign-in.

The app refreshes configured catalogues on startup, after credential changes, and when preparing a desktop launch. **Refresh catalogue** and **Refresh all**
are available when you want to refresh them manually. A listed model is
advertised by its provider; listing alone does not prove inference access.

Usage is charged or counted by the selected provider or account. Provider Hub does not change a key's billing arrangement. The tested Muse Model API key
uses pay-as-you-go billing; the Muse Code subscription is a separate agent
integration. Grok here also uses its pay-as-you-go API.

## Claude Desktop

1. Open **Models** and select the provider model behind each Claude model slot. Optional **Omit system** / **Omit tools** checkboxes drop those Claude-sent fields at this gateway (own risk; tool loops break).
2. Open **Claude** and choose **Launch Claude**.
3. If Claude is already open, the app asks before restarting it.

Claude uses its native third-party profile system. A model whose effective
context is at least 1M is advertised under a single `[1m]`-suffixed slot id, so
the picker shows one row metered at 1M instead of separate standard and 1M
choices. Routes below 1M are advertised bare at their catalogued window.
Provider Hub retains the provider's model identity and context metadata; it
does not expose an editable context-size guess.

**Claude features in this profile** (all off by default) switch on Claude
Desktop capabilities that a third-party profile hides unless the profile asks
for them: dictation, the built-in browser, Claude in Chrome, scheduled tasks,
and the Cowork tab. Each runs locally inside Claude and still sends its model
calls through the gateway. Save, then launch Claude again for them to apply.
Whether dictation and scheduled tasks appear depends on the installed Claude
build honouring the field.

Quitting Claude restores the previous Claude profile. Keep Provider Hub running
while using this session. Your existing Claude and Ollama conversations remain
in their respective profiles.

## Codex / ChatGPT Desktop

1. Open **Codex** in Provider Hub.
2. By default **All compatible models** from every configured provider appear
   in the Codex picker. Switch to **Custom selection** to curate which models
   are published.
3. Select a **Default model** from the provider-grouped menu.
4. Choose **Launch Codex / ChatGPT**, then confirm the restart when ready.

The default sets the starting model. Codex's picker receives the models you
selected: either all compatible models, or your curated list. OpenRouter supplies
a curated shortlist with distinct context choices.

If you switch to **Custom selection**, only the routes you add are offered in
Codex. Missing curated routes (e.g., after a provider refresh) are shown on the
page and must be refreshed or removed before launching.

Provider Hub uses the same installed desktop app. Its catalogue temporarily
replaces the normal model catalogue. This is not a second simultaneous app or
a new login. Existing tasks, credentials, projects, and unrelated settings are
retained. Quit the desktop app to restore its previous model configuration.
The **Restore previous setup** button is available for recovery after it closes.

**Show your ChatGPT account in Codex** (off by default) keeps your ChatGPT
sign-in visible while Provider Hub is active. The composer then uses the
native model pill (white label, chevron, Ultra colour) for hub models, and
the account chrome and usage banners reflect your ChatGPT plan even though
hub traffic does not spend it. A ChatGPT sign-in is required to launch in
this mode, and the gateway credential is written into the Codex config for
the session instead of being fetched by the helper command; it is removed
when the previous setup is restored.

**Offer apply_patch to catalogue models** (off by default) advertises Codex's
own `apply_patch` editing tool for every model in the catalogue. Codex draws
its close-out diff card (the "Edited N files" card with per-file `+N -N`
rows, Undo and Review) only from edits made with that tool, so hub models,
which otherwise edit through the shell, never produce one. With the switch
on, Provider Hub projects the tool as a plain JSON function for the provider
and converts the calls back, and the card returns. A model that keeps
failing the patch format gets the error back and usually falls back to shell
edits; hold such a route back by listing it under `codex_apply_patch_exclude`
in the settings file. Quit Codex, save, then launch again: the catalogue is
written at launch.

Claude and Codex can run on the same gateway. While one desktop harness is
live, you can change the other's model selection and save; launching briefly
restarts the gateway so both share the new snapshot, and the running app
reconnects automatically. Shared provider settings (keys, regions, port) still
require quitting both desktop apps.

This integration applies to the Codex coding workspace. It does not reroute
ordinary ChatGPT cloud chats, voice, or every other product feature. File
editing and terminal tools are supported; hosted web search is off in this
profile. Start a new task when switching provider/account boundaries that carry
provider-specific reasoning history.

## What has been checked

The 0.5.0 release passes 226 automated tests under the bundled runtime. The
installed Codex engine completed live read/edit/read cycles using Ollama's
DeepSeek V4 Flash cloud route and Cerebras GPT OSS 120B. Their reported context
limits were preserved. Other provider translations have deterministic protocol
tests; not every advertised model has been live-tested.

Live 0.5.0 Messages and streaming Responses read/edit/read cycles also passed
for OpenRouter North Mini Code Free and Gemini 3.8 Flash. Qwen Token Plan
returned its exhausted weekly quota response, so its live tool cycle remains
for verification after quota returns.

The active development Codex app was not restarted during those checks. A full
GUI launch/picker/context-meter/quit/restore cycle remains a final user check.
The preview label reflects these remaining account and desktop qualifications.

Gemini replies currently appear after generation finishes so its continuation
signatures can be preserved. The connection stays active while it waits.

If something fails, **Activity** shows request status and usage without logging
prompts or keys. A provider can reject a model because of account access,
quota, or unsupported controls. Choose another available model or check that
provider's account page.
