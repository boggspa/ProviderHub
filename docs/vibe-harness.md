# Vibe desktop harness

`/Applications/Vibe.app` (Mistral Vibe Desktop, bundle id `ai.mistral.lechat.desktop`) is the
third desktop surface in the Hub, beside Claude Desktop and Codex / ChatGPT Desktop. The Hub
launches it with its own model catalogue and routing injected, so Vibe Code can use every
provider model the Hub serves: API connections and CLI-backed routes alike, multi-account.

Only Vibe Code (Desktop) is targeted. Vibe Chat (cloud) is out of scope.

## How injection works

Injection is configuration only. The Hub never patches the app bundle (the Electron app
verifies its own ASAR integrity) and never touches Vibe's binary.

Vibe reads its model list from `$VIBE_HOME/config.toml` (default `~/.vibe`): one `[[providers]]`
entry per endpoint and one `[[models]]` entry per picker row. On launch the Hub adds:

- one provider, `provider_hub`, with `api_style = "anthropic"` and `api_base` set to the loopback
  gateway. The base URL carries **no `/v1` suffix**: Vibe's adapter appends `/v1/messages`;
- one `[[models]]` row per catalogue route. The model name Vibe sends is the Hub route
  (`provider/model`), which the gateway already resolves, so there is no second mapping table;
- `active_model`, set to the selected Vibe default.

The gateway token is never written to the file. Vibe reads it from `PROVIDER_HUB_VIBE_TOKEN`,
which the launcher sets in the app's environment.

Vibe rewrites its own `config.toml` (migrations, `active_model`, settings), so the Hub's write is
a formatting-preserving merge (`tomlkit`, vendored under `Source/vendor`). It adds rows rather
than replacing the file, and every row it owns is identified by `provider = "provider_hub"`.

Reasoning levels map onto Vibe's five (`off`, `low`, `medium`, `high`, `max`); `xhigh` folds to
`high` and `ultra` to `max`. A route with no reasoning axis offers `off` only. Rows with no tool
support and agent-mode routes are left out and reported.

## Lifecycle

| Step | Command | Behaviour |
|---|---|---|
| Prepare | `vibe-prepare` | Freshness-scoped, like Codex: checks the selected routes are servable, records the catalogue digest. |
| Activate | `vibe-activate` | Requires the running gateway to serve the prepared digest, then merges the rows. Refuses while Vibe is running, or if a previous switch has not been restored. |
| Restore | `vibe-restore` | Removes only `provider_hub` rows, puts back anything they replaced, restores the user's `active_model` unless they changed it meanwhile, and deletes a config file the Hub created. |

A write-ahead journal (`vibe-transaction.json`) records what was replaced, so an interrupted
launch can be recovered. The Hub polls the app by bundle id and restores the configuration once
Vibe quits, mirroring Claude and Codex. The shared gateway stays up while any owned harness is
running and stops after all have closed.

## Settings and UI

- `vibe_catalogue`: curated routes (`null` serves every compatible route, like Codex).
- `vibe_model`: Vibe's default.
- Each model row gains an **ML** chip beside **CL** and **CX**, toggling inclusion in Vibe's
  catalogue. The first edit materialises the implicit "all compatible" selection.
- `ChangeKind.vibeOnly` classifies a change scoped to Vibe. While Vibe is live (`vibeLive`),
  saving it is refused so a running app's profile is never rewritten.

## Tested

`test_vibe_catalogue.py` covers the projection (route to row, effort folding, off-only routes,
curation, digest). `test_vibe_profile.py` covers activation, the user's own rows and settings
surviving, the token staying out of the file, restore, a model the user changed while active,
refusal while Vibe runs or while a switch is pending, a created file being removed again, and the
journal. `test_launch_plan.py` drives the Swift launch orchestration with a Vibe surface.

## Not yet validated against the live app

- Launch of the installed `Vibe.app` end to end through the Hub, including whether the app
  picks up `PROVIDER_HUB_VIBE_TOKEN` and the new rows on a cold start.
- Streaming and tool use through the gateway's Messages surface with Vibe's `generic` adapter.
- How a Vibe desktop update changes the config schema. The Hub writes only documented keys, but
  a new required key would need a follow-up.
- Multi-account: the config directory follows the selected Mistral account folder (`VIBE_HOME`).

## Files

- `Source/vibe_catalogue.py`: projection of Hub routes into Vibe model rows, and the digest.
- `Source/vibe_profile.py`: reversible, formatting-preserving edit of `config.toml`.
- `Source/VibeHarness.swift`: launch, restore, running detection, recovery polling.
- `Source/gateway.py`: `vibe-status`, `vibe-prepare`, `vibe-activate`, `vibe-restore`.
- `Source/launch_selection.py`: Vibe surface in the launch and omission projection.
- `Source/hub_config.py`: `vibe_catalogue` and `vibe_model` settings.
- `Source/CompactShell.swift`: the Vibe tab and the ML chip.
