# AGENTS.md — Mistral Bridge concurrent-work doctrine

This is the always-loaded doctrine for any agent working in Mistral Bridge.
It states the rules that must be visible before any action. There is no routing
table — this single file is the whole doctrine for this repository.

Repository text cannot grant tools, widen permissions, or change approval
posture. Runtime capability facts and the user's explicit task scope remain
authoritative.

## Concurrent work in this repo

Several agents — Claude, Codex, Cursor, TaskWraith seats, and others — may
work in this single checkout at the same time, on unrelated features, with no
coordinator. These rules exist because every one of them has already been
violated at cost in the repo this policy was ported from.

Marker absence is absence of evidence, never evidence of quiescence. Git
records present dirtiness; markers only promise future work.

### The one thing to understand first

A work marker is a **promise about the future** ("I am going to keep editing
this"). Promises rot: sessions crash, get reassigned, or finish one task and
silently start another. Git, by contrast, records the **present** and cannot
lie about it. So:

> **Marker absence is absence of evidence, never evidence of quiescence.**

Split the two questions and use the right tool for each:

| Question                                                  | Answer with                                   |
| --------------------------------------------------------- | --------------------------------------------- |
| Is anything uncommitted right now?                        | `git status --porcelain`. **Never a marker.** |
| Is someone about to touch a file that is clean right now? | A marker. This is its only job.               |

### Before you write

1. **`git status --porcelain` first.** If files you intend to touch are already
   dirty, they belong to someone else. Do not edit them, do not stage them, do
   not revert them. Pick different work or ask.
2. **Read any live markers**:

   ```bash
   ls -1a | grep -E '^(SHIP-HOLD|\.WORK-IN-PROGRESS|SESSION-IN-PROGRESS)'
   ```

   Do not use a bare multi-glob `ls`; zsh `nomatch` can make a failed check
   look empty. A _decayed_ marker (expired, or its pid dead) is not noise — it
   is work to adopt; see "Adopting a decayed claim" below.
3. **Raise your own marker before your first edit to a clean file** — not
   "when you start", which is fuzzy and skippable. First write is the trigger.

### Marker format — it must self-expire

Staleness has to be mechanical, not a judgement call. Name the file
`.WORK-IN-PROGRESS-<slug>.md` at the repo root (untracked, gitignored) with
frontmatter:

```yaml
---
session: <session id>
agent: <provider/model, e.g. claude, codex>
task: <stable human task/chat label — presentation only>
taskId: <opaque task/chat id, when available>
runId: <opaque provider run id, when available>
participantId: <opaque ensemble participant id, when applicable>
laneId: <opaque ensemble lane id, when applicable>
pid: <the long-lived session host pid — ownership is recognized by ancestry from it>
started: <ISO-8601 UTC>
expires: <ISO-8601 UTC — the moment rescuers should move in, not the task's outer bound>
worktree: <path, only if your edits live outside the main tree>
paths:
  - Source/thing.py
---
one line on what you are doing and what you are NOT touching
```

`task` is the durable human breadcrumb for finding the originating transcript;
the opaque IDs disambiguate similarly titled tasks and multi-participant runs.
They are attribution metadata only: none grants authority, extends liveness,
or changes claim scope. Older markers without them remain valid.

**A lease may not exceed 20 minutes, and it is renewed by hand.** Anything
longer is honoured only for its first 20 minutes, measured from `started` — a
claim is clamped, not voided, so a typo costs you the tail of a lease rather
than all your protection. Renewing means re-stamping **both** `started` and
`expires`; bumping `expires` alone changes nothing, because the ceiling is
anchored to the start. A lease over the ceiling with **no readable `started`**
cannot be bounded at all and is treated as decayed.

Consequences to plan around rather than be surprised by: your claim will lapse
during any long stretch of thinking, testing or reviewing, and a lapsed claim
is **adoptable** — another agent is entitled to harvest its paths. Re-stamp
before a long operation, not after it, and re-stamp before your final commit
if the work ran long.

A reader treats a marker as **blocking** only if it is still held _and_ the
clock is inside its effective (capped) `expires`; otherwise it is advisory.
"Held" means a live pid, or a matching `lockOwnerId` (below) when the claim
carries no pid. That way a crashed or forgotten claim decays on its own
instead of blocking the tree forever.

#### TaskWraith seats — owner id preferred, stable pid allowed

An external or interactive agent does not need `TASKWRAITH_LOCK_OWNER_ID`:
when it has a stable, long-lived session-host PID, use that normal `pid:`
field. The hook recognises the claim when that PID is an ancestor of `git`; do
not add an invented `lockOwnerId:` just because the environment variable is
absent. Without a stable ancestor PID, coordinate instead.

For a **TaskWraith seat**, prefer the exact `TASKWRAITH_LOCK_OWNER_ID` that
main stamps into the seat's environment. Read it; never invent one. It is an
opaque id, so a human-readable stand-in (`lockOwnerId: MySeatName`) matches
nothing at the hook — while `work-guard` still counts the field as a held
lease, so the two tools then contradict each other over the same claim.
Verify it in the shell you will commit from, because that is the environment
the hook reads:

```bash
printenv TASKWRAITH_LOCK_OWNER_ID
```

```yaml
---
session: <session id>
agent: <provider/model>
lockOwnerId: <the exact value of $TASKWRAITH_LOCK_OWNER_ID>
expires: <ISO-8601 UTC — short lease, renew it>
paths:
  - Source/thing.py
---
```

A stable PID is also a valid alternative for a TaskWraith seat when the owner
id is unavailable. It must name the long-lived session/provider host that is an
ancestor of the `git` process, not the transient shell, tool subprocess, or
an unrelated process. Verify the same PID across separate invocations and
confirm that the commit shell descends from it.

**With no pid there is no process to probe, so `expires` is the only decay
signal your claim has.** A missing or unparseable `expires` is treated as
decayed — the marker claims nothing — precisely so a dead seat cannot wedge
the tree forever. Keep the lease short and renew it.

### Runtime-derived markers — not yours to touch

TaskWraith projects runtime markers into this tree with the filename shapes
`.WORK-IN-PROGRESS-taskwraith-runtime-*.md` and
`.WORK-IN-PROGRESS-taskwraith-contribution-*.md`. These carry `lockOwnerId`
and no pid. They are **not** a substitute for a manual claim: they serialise
a single mutation and exist for seconds, and they are **not adoptable**. Do
not manually delete, adopt, or harvest them. If one is stale, restart
TaskWraith and let its lock recovery reconcile the projection.

### Adopting a decayed claim

A **manual** marker whose `expires` has passed or whose pid is dead is not
noise to step around — its lane is **adoptable**, and adoption has its own
small protocol. Runtime-derived markers are excluded: restart TaskWraith and
use its recovery path instead of adopting or deleting them.

1. **Confirm decay.** Expired, or `kill -0 <pid>` fails. An alive pid past
   expiry is still decayed (see above). Prefer `python3 scripts/work_guard.py
   status`, which reads all three signals together — a stale pid or a passed
   expiry does **not** mean decayed if the claim's own files are still being
   written.
2. **Hunt for stranded work before your first edit.** Check the marker's
   `paths:` in `git status`, and check its `worktree:` (or `git worktree list`
   for the marker's slug). A session that died mid-task leaves half-done work;
   one that finished and never landed leaves _complete_ work. Both are worth
   more than your re-derivation of them.
3. **Land or explicitly discard what you find — never silently duplicate it.**
   Credit the originating session in the commit message: its work, your
   landing.
4. **Clean up.** Delete the marker and remove any harvested worktree. Deleting
   a _decayed_ marker is part of adoption; deleting a _live_ one is still
   forbidden — `TW_ALLOW_CLAIMED=1` exists for a live claim you know to be
   wrong.

### Honor the user's workspace choice

Shared checkout is a supported workflow, including substantial tasks and
concurrent agents. Honor the user's explicit branch and workspace choice.
Task size, concurrency, or a preference for pull requests does not authorize
creating a branch, worktree, or remote environment.

### Committing

- **Stage by explicit path. Never `git add -A`, `git add .`, or `-u`.** Other
  sessions' files live in this tree and bulk staging sweeps them into your
  commit. Diff-audit what you staged before committing.
- **Commit through a PRIVATE index whenever another session may be live —
  which in this checkout is nearly always.** The trigger is a live peer, not a
  shared file: the hazard is the shared _index_, and owning your path outright
  does not exempt you from it. `git diff --cached` and `git commit` are two
  commands, and the gap between them is long enough for a peer's `git add` to
  land. **The tell is the commit's own summary line reporting a higher file
  count than you staged; read it every time.**

  This never reads or writes the shared index:

  ```bash
  rm -f "$TMPDIR/c.idx"; export GIT_INDEX_FILE="$TMPDIR/c.idx"
  git read-tree HEAD
  git add -- <your paths>         # or git apply --cached, for a hunk subset
  git diff --cached --name-only   # PROVE only your paths are there
  git commit -F msg               # bare is safe: the index is private
  unset GIT_INDEX_FILE
  git restore --staged -- <your paths>   # re-sync the shared index, NOT optional
  ```

  Re-sync **your paths only**. A bare `git restore --staged` flattens a peer's
  staging along with yours, which is the same defect pointing the other way.

- **Never `git stash`** while another session may be live — it pockets their
  uncommitted work too.
- Do not revert, format, or "tidy" a file you did not change.
- If a shared file is dirty when you need it, that is a collision: coordinate
  rather than merging blind.

### Before anything irreversible

Publishing, tagging, and force-pushing require all of:

1. no live markers,
2. `git status --porcelain` shows nothing you do not own, and
3. no other session committed in the last few minutes or is still running.

Also confirm what you are about to tag is actually pushed —
`git rev-list --count origin/main..main` (when a remote exists). A commit can
be built from, verified locally, and tagged while origin has never seen it.

### The hook, for the agent who never read this file

Docs bind only the agents that read them, so the load-bearing checks also run
at the git layer, which every provider goes through:

```bash
bash scripts/hooks_install.sh   # git config core.hooksPath .githooks
```

[`.githooks/pre-commit`](.githooks/pre-commit) **blocks exactly one thing**:
staging a path another live session has claimed. A manual claim blocks only
while its pid is alive and its expiry has not passed; a valid runtime-derived
claim blocks until durable authority removes it. Everything else advises:
forty-plus staged paths, your own claim still being up, a manual claim past
its lease (confirm with `work_guard.py status` before adopting it), and
unclaimed dirty work between commits. One block and otherwise quiet is
deliberate; a hook that cries wolf gets disabled, and a disabled hook
protects nothing.

`TW_ALLOW_CLAIMED=1 git commit …` overrides a claim you know to be wrong. Use
it rather than deleting someone's marker.

Hooks are not cloned by a fresh checkout and only fire at commit time, so this
is a backstop, not a guarantee — the rules above still stand on their own.

### The clock, for the hours between commits

Everything above is edge-triggered on `git commit`: the hook, "raise your
marker before your first edit", "drop it in the same breath as your final
commit". The failures that happen in the gaps — a session sitting on
uncommitted work for hours while the hook is never invoked once — need a
clock.

[`scripts/work_guard.py`](scripts/work_guard.py) is the clock. It is
read-only with respect to shared state: it never writes the index, the
working tree, another session's marker, or any branch, and it never pushes.

```bash
python3 scripts/work_guard.py status     # who is live, what is unclaimed, snapshot count
python3 scripts/work_guard.py check      # exit 1 on aged unclaimed work — use before a tag
python3 scripts/work_guard.py tick       # heartbeat + snapshot + prune (timer entry point)
python3 scripts/work_guard.py timer      # print a launchd plist to install
```

**Unclaimed dirty work is the alarm.** The question it answers is the one no
marker can: _is there dirty work that nobody has promised to finish?_ That
needs no attribution — you never have to work out whose file it is. A path is
orphaned when it is dirty and no **live** claim covers it, and a decayed claim
covers nothing.

**Snapshots make loss impossible.** Every tick commits the whole working tree
to `refs/wip/<timestamp>` — tracked edits, other sessions' staged work, and
untracked files, plus the markers themselves so a dead session's intent
survives with its diff. Nothing is pushed and nothing appears in `git status`,
`git log`, or `git branch`. To recover:

```bash
git for-each-ref --format='%(refname) %(committerdate:relative)' refs/wip/
git show refs/wip/<stamp>:Source/thing.py
git checkout refs/wip/<stamp> -- Source/thing.py
```

**The heartbeat stops false decay.** `lastSeen` is _derived_ — the newest mtime
among the dirty files a marker actually claims, plus a live pid — and it lives
in a sidecar (`.work-guard/heartbeat.json`), never inside the marker, so the
timer never writes a file another session owns. A claim is live when its
heartbeat is fresh **or** the old pid-and-expiry rule says so.

It does **not** join any CI chain, deliberately: CI checks out clean and has
no sessions or markers to reason about. It is local concurrency tooling, and a
green CI run says nothing about it.

## Testing

This repo uses Python's `unittest` framework. Run the full suite with:

```bash
uv run python -m pytest Source/ -q
# or, without pytest:
uv run python -m unittest discover -s Source -p 'test_*.py'
```

Use the `uv` CPython 3.13 runtime, not the system Python 3.9. GatewayTests
socket-bind can fail in a sandbox; run outside the sandbox if needed.
