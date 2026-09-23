# `.local-only/` — per-session scratch, not part of the project

Everything in this directory except this README is gitignored. Put throwaway
work here instead of the repo root: one-shot patch scripts, captured test
output, draft commit messages, screenshots, scratch notes.

Why: `git status --porcelain` is the signal AGENTS.md tells every agent to
trust before writing. Scratch dumped in the root becomes permanent `??` noise
in it, and sits one `git add -A` away from being committed.

## It is not a safety net

`scripts/work_guard.py tick` snapshots the working tree with `git add -A`,
which honours `.gitignore`, so — unlike ordinary untracked files — nothing here
is captured into `refs/wip/`. Treat this directory as disposable.

Anything worth keeping: commit it, or move it outside the repo.
