# Provider models and Codex host subagents

Investigated on 2026-09-19 with the installed ChatGPT Desktop runtime,
`codex-cli 0.155.0-alpha.9.2`. The reproducible probes use a loopback fake
Responses provider and a disposable Codex home; they do not call real models,
read credentials, change user configuration, or run workspace tools.

## Findings

There is no five-model execution limit in the tested runtime. Its native
`collaboration.spawn_agent` description lists the first five catalogue rows
by priority. A seventh model, qualified with another provider's name, is
accepted and receives the child request through the configured provider.
The five rows are recommendations in the description, not an enum on the
`model` argument. Treating omission from that list as unavailability explains
the reported failure to choose Gemini from a larger Hub catalogue.

CLI-backed Codex had a separate ownership problem. Its nested runtime is
pinned to the OpenAI provider and its native model catalogue, while the
desktop owns the Hub provider routes and the actual subagent sessions.
The bridge passed the desktop tools under `host.bridge_*` aliases but only
disabled `features.multi_agent`. The nested runtime still advertised its own
`collaboration` tools and native model list alongside those host aliases.
A model choosing that local tool could not route the child through the Hub.

In this runtime, setting all three of `agents.enabled=false`,
`features.multi_agent=false`, and `features.multi_agent_v2=false` removes the
nested collaboration tools. The host dynamic tool remains callable: the
installed runtime emitted `item/tool/call` with the host alias and preserved
the requested cross-provider model and `fork_turns` arguments.

## Changes

### Exact missing-thread failure (2026-09-20)

The installed app still carried only `features.multi_agent=false`, whereas
the checkout already had all three controls. An isolated runtime probe
reproduced `collab spawn failed: no thread with id: <id>` with an ephemeral
parent and `fork_turns="all"`. The same probe with a persistent parent
succeeded, as did an ephemeral parent with `fork_turns="none"`. This locates
the reproduced failure in the nested native launcher's history-fork path.
Disabling all three native controls removed native spawn and produced the
expected host tool handoff instead.

Reproduce without paid inference or real tasks:

```sh
uv run --python 3.13 python scripts/probe_codex_subagents.py --control features.multi_agent=false --ephemeral --fork-turns all
uv run --python 3.13 python scripts/probe_codex_subagents.py --transport-controls --forward-host --fork-turns all
```

Changing fork arguments is not a routing fix: nested agents would still use
the wrong runtime. Install the corrected transport and restart the gateway
when active sessions can be interrupted safely. A source checkout change
alone does not update the worker inside the installed application bundle.

### Transport and catalogue changes

- The CLI transport applies all three controls. Actual delegation remains
  owned by the desktop and is returned through the existing host tool bridge.
- Before translating a Responses request, the Hub replaces the host spawn
  tool's abbreviated model list with the full tool-capable Codex catalogue.
  Curated selections and model priority order are preserved. This applies
  to native Responses, translated API, and CLI routes.
- The description retains the host's task guidance. It changes no tool
  schema or permissions, never restores a withheld spawn tool, and respects
  an explicit model enum if a future host supplies one.
- The Hub UI describes priorities as ordering preferences rather than a
  five-model availability limit. The build includes the new helper module.

## Reproducing the runtime checks

From the repository root, with CPython 3.13:

```sh
uv run --python 3.13 python scripts/probe_codex_subagents.py
uv run --python 3.13 python scripts/probe_codex_subagents.py --control features.multi_agent=false
uv run --python 3.13 python scripts/probe_codex_subagents.py --transport-controls --forward-host
```

The first check should request `gemini/probe-flash` even though that route is
absent from the native spawn description. The second demonstrates the old
flag leaving native spawn exposed. The last uses the adapter's actual
configuration: `spawn_tools` should be empty and `host_calls` should contain
the preserved Gemini target under the host alias.

Unit and integration regressions cover catalogue curation, more than five
models, priority order, absent spawn tools, host enums, namespace collisions,
native/CLI translation, and cross-provider host argument preservation.

## Limits and activation

These checks verify desktop routing and the CLI handoff, not a paid Gemini,
Claude, or other vendor inference call. Provider login, account availability,
tool support, concurrency and depth controls still apply. The host's own
instructions determine when delegation is appropriate; prefer a
self-contained task with `fork_turns="none"` when requesting another model.

The gateway holds its imported code and catalogue for its lifetime. Rebuild
the Hub and restart its gateway to activate these changes; reconnect or
relaunch the desktop through the Hub if its catalogue was also changed.
Existing running sessions were not restarted by this investigation.

Official configuration documentation describes custom provider catalogues,
agent enablement and default subagent model settings, but does not document
the five-description-row behavior or the combined controls observed here:
<https://learn.chatgpt.com/docs/config-file/config-reference>.

## Collaboration tools "lost" mid-session (2026-10-10)

A Codex Desktop seat on `codex/gpt-6.1-sol` (the CLI route) that had spawned
sixteen helpers earlier in the thread reported, right after the desktop
restarted and re-injected a 450K-token history cold: "This turn exposes agent
metadata but no direct collaboration controls". The desktop had sent every
host tool on that turn: the nested runtime logged
`thread_start.dynamic_tool_count=168`, the same count as the thread's healthy
root turns, where a depth-1 child gets 167. The Hub recorded no
`spawn_stripped` event for it, so no layer removed the tool. What the model
saw was `host.bridge_<16 hex>` with a description that opened with the Hub's
42-row model catalogue, while the briefing told it to call `spawn_agent`. That
name matched nothing in its tool list: the failure class the native route's
rename (`responses_tools.tool_name`) had already fixed, still present here.

Changes:

- Host tool aliases on the CLI route keep the tool's own name:
  `bridge_spawn_agent_<8 hex>` rather than `bridge_<16 hex>`. The prefix and
  digest stay, so built-in runtime tools are never shadowed and distinct host
  names never merge.
- The subagent catalogue block is appended after the host's own description
  instead of in front of it.
- A request whose spawn tool the depth limit removes carries a note in
  `instructions` naming the withheld tool and why, so a tasked child, or any
  route with delegation switched off, does not read the absence as a broken
  session.
- `responses-shapes.jsonl` in the state directory keeps a bounded
  per-request record: tool names when the set changes, counts, item types and
  the spawn-depth strip count. The next report of a missing tool can be
  attributed to the desktop or to the middleware from that file alone.
