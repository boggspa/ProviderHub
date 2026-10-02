# Claude Code provider accents

The first prototype uses Claude Code's official mods API (2.1.287+). It draws
a small provider-coloured dot and a muted model label above the composer in
the terminal and Claude Desktop's Code tab. It preserves other mods' content
in that shared band and yields completely while a survey owns it.

The module registers only `ui.render { component: 'AbovePrompt' }`. It does
not change prompts, requests, tools, permission decisions or model selection.
No app-bundle patch, debug port, external watcher, network request, inference
call or background timer is involved.

## Identity and scope

`Source/claude_accent.py` projects exact `claude_routes()` IDs through the
existing branding resolver, including user colour overrides. Only the schema,
active flag, loopback gateway URL, model labels and hex colours reach the JSON
file. Credentials and the rest of the Hub settings never reach the mod.

The mod's `cataloguePath` option names that file. For a disposable
`--plugin-dir` preview, `PROVIDER_HUB_CLAUDE_ACCENTS` can supply the path instead.
Without either, it draws nothing. The file must be active and the session's
`ANTHROPIC_BASE_URL` must exactly match its loopback gateway URL. Merely having
a similarly named model or the plugin installed does not enable the accent.

Each render reads the current model and catalogue. The lookup strips only
Claude's `[1m]` context suffix; it does not guess a provider from a name or use
the last model as a fallback. An unknown model, another gateway, absent file,
invalid JSON or inactive catalogue leaves native rendering intact. No colour
state is shared between conversations.

## Validation

```sh
claude plugin validate Source/claude-mods --strict
claude plugin validate Source/claude-mods/provider-hub-accents --strict
claude plugin test Source/claude-mods/provider-hub-accents
uv run --python 3.13 python -m pytest Source/test_claude_accent.py -q
```

The native mod test kit validates both terminal and Desktop trees, model
switches, context suffixes, preservation of other mods, survey ownership,
different gateways, missing files and malformed/inactive catalogues. The
Python tests check route identity, shared branding, opt-in refresh and the
absence of credentials. Tree tests cannot verify the app's actual painting.

## Other accents and runtime requirements

`Spinner` exposes wording and an engine-rendered tree, but no documented
colour prop. `ToolUse`, `ToolResult` and `ToolGroup` also expose render hooks.
Replacing those trees could lose native animation, disclosure and status
details. This prototype therefore leaves those sites alone until a visual
experiment demonstrates a worthwhile accent that retains their behaviour.
The native model picker, effort slider and app sidebar have no documented
mod render sites. Chat and Cowork are outside this prototype.

The standalone CLI and Desktop-managed CLI are separate installations.
Updating `~/.local/bin/claude` does not replace Desktop's managed binary.
Desktop's updater reported its app current on 2026-10-02; its latest local
managed binary at that check was 2.1.286. Do not overwrite a managed binary or
enable early-access flags to force mods on. Desktop validation requires its
normal updater to supply a compatible runtime.

References checked 2026-10-02:

- [Mods overview and minimum version](https://code.claude.com/docs/en/plugins/mods/overview)
- [Drawing and preserving engine trees](https://code.claude.com/docs/en/plugins/mods/interface)
- [Render sites and supported props](https://code.claude.com/docs/en/plugins/mods/reference)
- [Native mod test kit](https://code.claude.com/docs/en/plugins/mods/test)
