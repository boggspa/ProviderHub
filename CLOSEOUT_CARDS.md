# Close-out cards through Provider Hub

Why the changed-files row (per-file Diff element with `+N -N`) was missing in
both desktop apps, and what Provider Hub now does about it.

Evidence: Codex citations are from `openai/codex` tag `rust-v0.154.0-alpha.6.2` (matching the installed `codex-cli 0.154.0-alpha.6.2` runtime) and the extracted ChatGPT `app.asar`. Claude citations are from the extracted Claude Desktop
bundle (`index.chunk-CrLS8br-.js` and related chunks).

## Codex / ChatGPT Desktop: certain root cause

The card renders exclusively from Codex core's per-turn diff tracker:

1. `core/src/tools/spec_plan.rs` registers the `apply_patch` tool only when the
   model's catalogue entry has `apply_patch_tool_type.is_some()`.
2. `TurnDiffTracker` (`core/src/turn_diff_tracker.rs`: "Tracks the net text
   diff for the current turn from committed apply_patch mutations, without
   rereading the workspace filesystem") is fed only by `AppliedPatchDelta`
   from `ApplyPatch` (`core/src/tools/events.rs`). The shell (`UnifiedExec`)
   branch emits exec events only; shell edits never touch the tracker.
3. At turn end (`core/src/session/turn.rs`), `EventMsg::TurnDiff` is sent only
   when `tracker.get_unified_diff()` is `Some`. Empty tracker: no event.
4. `app-server` forwards it unconditionally as `turn/diff/updated`
   (`bespoke_event_handling.rs`), the webview stores it as `turn.diff`, and
   the card component parses it into per-file rows. Null diff: no card.

Provider Hub projected `"apply_patch_tool_type": None` for every model, so the tool was never offered. All edits went through the shell, the tracker stayed empty, and the card never appeared. Ruled out: auth mode, workspace git state
(the tracker is not git-based), and config switches (none exists for this).

## Codex fix: JSON-wrapped apply_patch projection (per-route opt-in)

Codex speaks apply_patch as a Lark-grammar custom tool (`ApplyPatchToolType`
has exactly one variant, `Freeform`), which no third-party Responses provider
implements. The bridge now adapts it per qualified route:

- `Source/responses_tools.py`: accepts the `apply_patch` custom tool and
  projects it as `apply_patch(patch: string)`, with a reversible mapping
  marker. The freeform "do not wrap in JSON" description is replaced with
  provider-side instructions. History helpers convert `custom_tool_call`
  items to function items at ingress and back at egress.
- `Source/responses_native.py`: accepts the custom history items, restores
  mapped calls to `custom_tool_call` on the way out, and drops the
  JSON-argument deltas for mapped items (core builds the call from
  `output_item.done`; the JSON bytes must not enter the patch buffer).
- `Source/responses_bridge.py`: same treatment on the Messages translation
  path, including a tool-map-aware response adapter.
- `Source/codex_catalogue.py` + `Source/hub_config.py`: advertise
  `"freeform"` for a route when the Codex tab's **Offer apply_patch to
  catalogue models** switch (`codex_apply_patch_all`) is on and the route is
  not in `codex_apply_patch_exclude`, or when the route is listed in
  `codex_apply_patch`. Default is unchanged (`None` everywhere).
- `Source/CodexHarness.swift` + `Source/HubModels.swift`: the switch lives
  beside the ChatGPT-account toggle; the two route lists are carried in the
  Swift settings model so a hand edit survives a Save from the app. Changing
  any of the three counts as a Codex catalogue change (quit Codex first, then
  Save and relaunch: the catalogue file is written at launch).

To qualify a route, turn the switch on (or add the route to
`codex_apply_patch`), relaunch Codex from the hub, exercise real file edits,
and confirm the close-out card appears with correct per-file diffs. A model
that keeps failing the patch format gets the parse error back and usually
falls back to shell edits, which the card does not show; hold such a route
back with `codex_apply_patch_exclude`. JSON-wrapped patch text is easier for
models than raw freeform, but discipline varies per provider, so the
exclusion list stays per-route and explicit. A bridge-side "diff the
workspace around shell calls" fallback is not possible: the desktop renders
the card only from core's `turn/diff/updated`, which the bridge cannot
inject.

## Claude Desktop: two mechanisms

1. **Per-turn file rows / `+N -N` chips.** The desktop computes these from
   completed `Edit`, `Write`, `MultiEdit`, and `NotebookEdit` tool uses
   (bundle matcher `Edit|Write|MultiEdit|NotebookEdit`; `Bash` maps to a
   command, not a file). Upstream models that edit files via shell heredocs
   produce no rows. Fix in this repo: `Source/providers.py` appends a short
   steering instruction preferring the tracked file tools, but only when the
   harness offers both a file-edit tool and `Bash` (otherwise the instruction
   would be noise, or point at tools that do not exist). Chat providers get a
   trailing system message (Cerebras keeps its single leading system message);
   Anthropic-protocol providers get the text appended to top-level system.
   This is best-effort steering: the desktop owns tool choice.
2. **File checkpointing / rewind bar.** Both the spawned
   `enableFileCheckpointing` option and `rewindFiles` are gated on the
   server-side flag `t.Gj("2941281426")`, which evaluates false in the
   third-party (`Claude-3p`) profile path. That gate is Anthropic-controlled;
   the bridge cannot enable it. No bridge change is attempted here.

## Status

- Codex card: restorable via the adapter above; the Codex tab switch turns
  it on for the whole catalogue. Live qualification so far (2026-09-16):
  Kimi for Coding produced correct patches first time and the card, Undo
  and Review appeared. Mistral Medium 3.5 called apply_patch four times
  but wrote SEARCH/REPLACE blocks and unified diffs, which core rejects,
  because the projected description dropped the grammar; the description
  now restates the format (`APPLY_PATCH_FORMAT_GUIDE`) and the base
  instructions prefer apply_patch for edits, which a live probe with the
  same guide showed yields a valid patch on the first attempt.
- Claude per-turn rows: best-effort steering shipped; effectiveness depends
  on model compliance.
- Claude checkpoint/rewind: blocked on the server-side gate; needs
  Anthropic-side enablement for third-party profiles, not bridge code.
