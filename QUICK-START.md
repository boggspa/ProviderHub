# Provider Hub Preview 0.5.6

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
- **Several keys for one provider** (say, work and personal): under **Paste a
  key**, **Add account** makes another slot, each with its own Keychain item.
  The slot chosen in **Account** is the one a pasted key lands in and the one
  the gateway uses; Save applies a switch. Subscriptions, Devin and Muse keep a
  single login.
- **Ollama:** run your existing Ollama app or daemon. It manages its own models,
  downloads, and cloud sign-in.

The app refreshes configured catalogues on startup, after credential changes, and when preparing a desktop launch. **Refresh catalogue** and **Refresh all**
are available when you want to refresh them manually. A listed model is
advertised by its provider; listing alone does not prove inference access.

Usage is charged or counted by the selected provider or account. Provider Hub does not change a key's billing arrangement. The tested Muse Model API key
uses pay-as-you-go billing; the Muse Code subscription is a separate agent
integration. Grok here also uses its pay-as-you-go API.

## Claude Desktop

1. Open **Models**. With **Five Claude slots** selected, pick the provider
   model behind each Claude model slot. Optional **Omit system** /
   **Omit tools** checkboxes drop those Claude-sent fields at this gateway
   (own risk; tool loops break). Switch to **Curated catalogue** to list any
   number of provider models in Claude's picker instead; each row carries the
   Claude family tier it plays (Fable, Opus, Sonnet or Haiku) and one row per
   tier is that tier's default.
2. Open **Claude** and choose **Launch Claude**.
3. If Claude is already open, the app asks before restarting it.

**Curated catalogue.** Claude lists each row under a generated id that starts
with the tier's Claude model (for example `claude-sonnet-5-kimi-k3`), keeps
the row's own display name, and starts on the Fable tier's default (Opus if
there is no Fable row). Claude Code's own family requests (its sonnet, opus,
haiku and fable aliases, dated family ids, older Sonnet fallbacks) go to that
tier's default, with the nearest tier standing in for a missing one. The
five slot mappings stay saved for switching back. **Teach Claude Code the
catalogue ids** (Claude tab, on by default) writes a `modelPicker` row with
`behavesAs` into `~/.claude/settings.json` for each catalogue model while the
profile is active, which is Claude Code's own remedy for a model id it does
not know: the row inherits the effort ladder, capabilities and context
handling of its tier's Claude model. The rows are removed when the previous
setup is restored; a terminal Claude Code sees them meanwhile.

**Ultracode.** Claude's Ultracode effort level (xhigh effort plus standing
dynamic-workflow orchestration) works with catalogue models on the Fable,
Opus or Sonnet tier once Claude Code's dynamic workflows are enabled. Turn on
**Enable dynamic workflows for Ultracode** (Claude tab) to add
`enableWorkflows` to the same settings file while the profile is active.
With Ultracode on, the gateway sends the provider its highest reasoning
setting and adds an orchestration note so the external model reaches for the
Workflow tool the way Claude's own reminders ask.

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

Claude Desktop checks the connection with a one-token test message to the
Haiku tier's default model. A provider that spends that token before saying
anything is answered with an empty reply so the check passes; if a provider
returns something that is not a message at all, the gateway reports the
reply's shape (never its content) in the error and in
`last-upstream-shape.json` under its state folder.

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
Codex. A curated route that a provider no longer advertises, or whose provider
could not be prepared, is left out of that launch and named in the launch
notice; your selection keeps it for when it returns. Only a selection with
nothing left to serve still blocks the launch.

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

**Use provider accent colours** (off by default) also colours running-task
spinners in the sidebar. Each spinner uses its task's saved model accent;
subagents use their root parent task's accent, including your branding
overrides. Colours refresh about every two seconds. Unknown tasks and tasks
on remote hosts keep their original spinner colour. See the
[sidebar accent notes](docs/codex-sidebar-accents.md) for scope and verification.

The same switch makes Provider Hub
start Codex / ChatGPT itself with a Chromium DevTools pipe and install a small
watcher in its windows that colours the model picker's power slider, and the
effort word in the composer's model pill, with the selected model's provider
accent, the same hues as the Providers page (Gemini wears Google's
Antigravity green). The activity text ("Thinking", "Editing files") turns
a slightly cooler gray: the app's own gray at its own lightness with a hint
of the provider hue, while the shimmer's sweep keeps the app's highlight,
and the icon leading such a row wears the accent itself. At
Ultra the slider, the picker's
title and the pill's word take a deeper, more saturated cut of the same hue
in place of the app's purple, and the word shimmers. Only Provider Hub holds
the pipe; nothing listens on a port. The route is unsupported by OpenAI: the slider's colour is an app-wide
design token, so if an update changes the picker the colour falls back to
blue and nothing else changes, and a Codex self-relaunch after an update runs
without the colour until the next launch from Provider Hub. Because Codex is
then a child of Provider Hub, macOS attributes its privacy prompts
(microphone, camera, calendars, reminders, location, folders, automation)
to Provider Hub, and the pipe is a full control channel into Codex that only
the helper holds. The helper stays until Codex quits and outlives Provider
Hub; if the helper itself is killed, Codex treats the closed pipe as a
request to quit.

**Hide the ChatGPT usage banner** (off by default; needs the power-slider
switch) makes the same watcher hide the "You're out of Codex and Work
usage" banner, and its per-model "out of usage" variant, above the
composer. The banner appears only with **Show your ChatGPT account in
Codex** on, reports that account's plan usage, and hub traffic does not
spend it. The same switch hides the server-sent versions, including the
"Get 250 credits" referral card (Add Credits, Refer). Banners are
recognised by their gauge icon, or, in the icon-free layout used while
sending is blocked, by the component that drew them, so an app update that
changes either brings the banner back and changes nothing else; the account and usage pages, and the rate-limit
prompt Codex may open on submit, are untouched. Save, then launch.

**Keep sending when ChatGPT usage runs out** (off by default; needs the
power-slider switch) keeps the composer's send button usable for hub models
after the ChatGPT plan's usage is exhausted. Codex otherwise disables the
button for every model, provider routes included. The watcher reads the
plan's usage status as the app receives it and reports the core limit as
still allowing sends; usage windows, per-model limits and credits are left
alone, native OpenAI models remain limited by the server and show the app's
own usage-limit message, and while it is on the reset-credit prompt and
reserve-model offers stay off and usage meters show at least 1% remaining.
Unsupported by OpenAI. Save, then launch.

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
editing and terminal tools are supported. Hosted web search is on when any
route in the catalogue can run it (Codex, Claude and Grok CLI routes, where
OpenAI, Anthropic or xAI searches under that CLI's own sign-in, and OpenRouter
models with native search); other routes run without it, and the Codex tab
lists which routes search. Start a new
task when switching provider/account boundaries that carry
provider-specific reasoning history.

## What has been checked

The 0.5.6 release passes 1,623 automated tests under the bundled runtime
(`unittest discover -s Source -p 'test_*.py'`). The
installed Codex engine completed live read/edit/read cycles using Ollama's
DeepSeek V4 Flash cloud route and Cerebras GPT OSS 120B. Their reported context
limits were preserved. Other provider translations have deterministic protocol
tests; not every advertised model has been live-tested.

Live Messages and streaming Responses read/edit/read cycles also passed on the
0.5.0 build for OpenRouter North Mini Code Free and Gemini 3.8 Flash; they have
not been re-run against 0.5.6. Qwen Token Plan
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
