**Provider Hub: Codex / ChatGPT Desktop harness options — 12 September 2026**

Grok PAYG is implemented in 0.3.3. The additional harness described here is
research and an implementation proposal; no Codex/ChatGPT launch control or
Responses endpoint is shipped in this build.

**What is actually installed**

The local desktop bundle is `/Applications/ChatGPT.app`, version
`26.908.40834`, with display name ChatGPT and bundle identifier
`com.openai.codex`. This naming matters: it would be wrong to decide that the
installed ChatGPT-branded app cannot use the Codex provider mechanism merely
because older documentation calls it Codex App.

Installed Ollama `0.33.3` advertises `ollama launch chatgpt` and accepts
`codex-app`, `codex-desktop`, and `codex-gui` as aliases. The distinct `codex`
integration launches the CLI. Its public desktop guide still uses
`ollama launch codex-app` and documents a restore command.

Sources: [Ollama desktop guide](https://docs.ollama.com/integrations/codex-app),
[matching Ollama 0.33.3 implementation](https://github.com/ollama/ollama/blob/v0.33.3/cmd/launch/codex_app.go).
The commands inspected locally were version/help commands only; no launcher was
run and no OpenAI config or credential file was read or changed.

**How Ollama configures it**

The matching launcher source writes a dedicated provider stanza plus selected
root fields in `~/.codex/config.toml`: `model`, `model_provider`, and
`model_catalog_json`. It generates a separate catalogue file and uses
`wire_api = "responses"`. It saves restore state and backups and restarts the
desktop when required. It is a managed configuration switch, not proof of an
independent concurrently running desktop instance.

OpenAI's current configuration docs support custom providers, a custom JSON
model catalogue, bearer-token helpers, and named CLI profile files. The CLI
profile format changed: a selected `name.config.toml` file overlays the base;
the old `[profiles.name]` format and root `profile` selector are no longer the
current mechanism. The inspected desktop launcher therefore edits selected
root values instead of assuming CLI profiles select the desktop configuration.

Sources: [OpenAI custom providers and profiles](https://learn.chatgpt.com/docs/config-file/config-advanced),
[configuration reference](https://learn.chatgpt.com/docs/config-file/config-reference).

**Recommended implementation**

Reuse Provider Hub's accounts, branding and model catalogue, then add a second
client protocol alongside the existing Claude Messages interface:

```text
Claude Desktop          -> /v1/messages  -> Provider Hub -> model API
Codex / ChatGPT Desktop -> /v1/responses -> Provider Hub -> model API
```

Keep these layers separate:

1. **Responses protocol.** Translate messages, function-call items and outputs,
   streaming deltas, usage, terminal signals, cancellation and errors. Preserve
   call IDs across turns. Qualify image inputs, reasoning items and special
   tools separately. A successful text response alone is not sufficient.
2. **Model catalogue.** Publish provider-qualified IDs with friendly names and
   each model's exact known context, supported effort levels and speed tiers.
   Ollama's catalogue schema has per-model `context_window`,
   `max_context_window` and `supported_reasoning_levels`, unlike Claude's slot
   mapping convention. Do not invent context values for unknown models. Verify
   the installed desktop's display and compaction behaviour before promising
   an exact meter for every provider.
3. **Launch and restoration.** Use an owned provider/catalogue and an atomic
   recovery journal. Record the previous values; restore only values still
   owned by the hub so external user edits survive. Leave auth, sessions,
   projects, skills and unrelated provider settings alone. A separate Codex
   home is an option to investigate, not a proven complete GUI isolation
   mechanism.
4. **Authentication.** Codex authenticates to the loopback hub using a separate
   local token. The provider API keys remain in Provider Hub's Keychain. A
   command-backed bearer helper is documented and avoids relying on Finder to
   inherit a terminal environment; its desktop startup behaviour needs a test.
5. **Qualification.** Start with a disposable Codex CLI task to capture the
   real Responses request shape, then test the desktop read/edit/read cycle,
   continuation, compaction, cancellation and restore. Do not switch an active
   task's provider during the investigation.

There are two sensible transport slices. A first slice could qualify direct
Responses forwarding for a provider that already exposes that protocol, such
as xAI or Ollama. A broader slice could translate Responses to the existing
Messages/Chat Completions backends. Shared schemas do not guarantee full
compatibility; free-form tools, hosted tools and reasoning state still need
explicit checks. Any `previous_response_id` support must have session storage
with defined resume behaviour, or the adapter must require full history and
reject unsupported continuations clearly.

The installed app-server schema was generated into a scratch directory without
starting a task. It contains `model`, `modelProvider`, and `config` fields in
thread start/resume requests. This is useful evidence for a custom client, but
does not by itself establish a supported menu-bar control for the existing
desktop's active session.

This proposal targets the installed app's Codex task harness. It does not claim
to replace inference in every ChatGPT cloud chat, voice mode or other product
surface. Those would need their own documented integration contracts.

**Current decision**

Codex/ChatGPT Desktop is a plausible next harness, and Ollama demonstrates the
desktop configuration route. Build a Responses adapter and catalogue projection
as their own tested slices, then add a launcher with restoration. Claude remains
the only launched harness in 0.3.3.
