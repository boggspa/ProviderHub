# Provider accents for sidebar spinners

Added in Provider Hub 0.5.5 build 26, using the existing **Use provider accent
colours** switch. Launch Codex / ChatGPT from Provider Hub after installing
the new build to load the updated helper. An already running helper keeps the
code it launched with; building a new app does not update that session.

Each running task's sidebar spinner takes its saved model's resolved branding
accent. If the row represents a spawned subagent, follow the spawn edges to
the root parent task and use that model's accent. A child running on a different
provider cannot recolour its parent's sidebar spinner. The separate accents
inside child conversation panels continue to reflect each child's own model.

The helper asks each installed watcher for up to 256 local sidebar thread IDs
every two seconds. It opens the highest-version `state_*.sqlite` database in
the configured Codex home using SQLite `mode=ro`, queries only those thread
IDs and their ancestors, and closes the connection. Only `threads.id`,
`threads.model` and the corresponding `thread_spawn_edges` fields are read;
titles, prompts, transcripts and credentials are not needed. Ancestor walks
are capped at 32 levels and database operations have a short time budget.
Only thread IDs and validated hex colours travel through the existing private
DevTools pipe. No additional network service is started.

The DOM hooks were inspected in ChatGPT Desktop 26.917.62051 (10789):
`data-app-action-sidebar-thread-row`, `-id`, `-kind` and `-host-id`. Local
keys have the form `local:<thread UUID>`. The running indicator is the grey
`role="status"` wrapper containing the app's motion-safe spinning container
and its SVG. Only that SVG is marked and coloured. Task labels, unread badges,
warnings, animation timing and reduced-motion behaviour are unchanged.

Unknown models, incomplete parent chains, cycles, missing or unsupported
databases, and unavailable read snapshots remove the override. Remote hosts,
cloud tasks, ChatGPT conversations and pending worktree rows keep their
original colours. No selected-conversation accent is used as a fallback.
Branding is loaded when the helper starts, matching the other accent overrides;
model changes in saved task metadata are picked up at the next poll.

Regression coverage uses temporary SQLite databases and the production watcher
in Chromium. It checks parent inheritance, model switches, native models,
custom branding, cycles, unavailable databases, origin guards, polling bounds,
row recycling, new/removed spinners, remote-host isolation, theme changes and
restoration of pre-existing styles. Run with CPython 3.13:

```sh
uv run --python 3.13 python -m pytest Source/test_codex_accent.py Source/test_codex_sidebar.py Source/test_codex_accent_dom.py -q
```

The browser tests use Node and Playwright. If the browser executable is supplied
separately, set `PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH`; expose Playwright through
`NODE_PATH` when needed. Without those dependencies the rendered test skips.
This is an observed app implementation hook; an app update may stop the
override, leaving the stock spinner.
