#!/usr/bin/env bash
# One-command install for the concurrent-work pre-commit hook.
# Runs `git config core.hooksPath .githooks` so every git commit in this
# checkout is guarded. Re-run after a fresh checkout (hooks are not cloned).
set -euo pipefail

cd "$(git rev-parse --show-toplevel)"

chmod +x .githooks/pre-commit 2>/dev/null || true

git config core.hooksPath .githooks
echo "[hooks] core.hooksPath -> .githooks"
echo ""
echo "Pre-commit guard is active. Read AGENTS.md before your first edit."
echo "Between-commits clock:  python3 scripts/work_guard.py status"
