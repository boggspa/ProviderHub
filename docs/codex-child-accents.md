# Subagent pane provider accents

Provider Hub Preview 0.5.6 build 47 recognises the current Desktop shell's
`subagent-detail` header rows as well as the older embedded header. This was
inspected in ChatGPT Desktop 26.930.51102 (13100). The old resolver required
the header's `h-12 border-b border-strong` classes, which the shell now owns
outside the shared header component; missing that hook restored gray.

The watcher scopes the tool glyph accent and shimmer hue to each subagent
tabpanel. Right and bottom controllers share the same authored hooks. The
current header's React components carry `seed` and `conversationId` for the
selected child plus its host identity. The watcher accepts that identity
only when both component values agree. `subagents:<UUID>` is the parent's
tab key and is never used as the child's identity.

Every two seconds the worker reads only the selected local children's `id`
and `model` from the newest local state database using SQLite `mode=ro`,
with an input limit of 32 and a short query deadline. The returned palette
uses each child's own provider branding. The main sidebar's existing palette
continues to follow spawn edges to the parent. Remote child panes use only
their own recognised displayed model; they cannot borrow a local thread's
metadata. No prompts, transcripts or credentials enter the accent lookup.

The child composer, when present, remains authoritative for its selected
model. Otherwise a recognised header model takes precedence over the worker's
metadata. React can reuse a header DOM node while its original fiber still
names the previous child; an in-flight metadata reply can also arrive after
the visible model changes. Neither may overwrite the displayed model's accent.
When a header has alternate React owner chains, both must agree on the child
and host before the watcher requests or uses local metadata. Ambiguous
identities with unrecognised labels retain the app's gray until resolved.
Labels, warning colours, identicons, disclosure controls and provider logos
keep their original styling.

These are observed implementation hooks, so a Desktop update can disable
the override. The browser regression covers the current shell header,
verified child identity, child switching, right-to-bottom movement, hidden
panes, stale React identities, late metadata replies, host separation,
warnings and independent parent colouring. SQLite tests verify own-model
resolution even when a parent is different or absent, model refresh, bounded
requests and an unavailable newer database.

Run the tests with CPython 3.13 and Playwright available to Node:

```sh
uv run --python 3.13 python -m pytest Source/test_codex_accent.py Source/test_codex_sidebar.py Source/test_codex_accent_dom.py -q
```

Rendered fixtures and installed-source inspection do not prove live Desktop
placement or sending. Live acceptance requires rebuilding and installing the
updated Hub, then quitting Desktop and launching it through Hub. The helper
and injected watcher load at launch; an existing session does not hot-reload.
