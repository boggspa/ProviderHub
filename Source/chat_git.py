"""Bounded, read-only Git status for the Chat header. Never fetches a remote."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess


def git_status(workspace):
    folder = str(Path(workspace).resolve())
    def git(*args, required=True):
        result = subprocess.run(["git", "--no-optional-locks", "-c", "core.fsmonitor=false", "-C", folder, *args],
            capture_output=True, timeout=3, env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_PAGER": "cat"})
        if result.returncode or len(result.stdout) > 2_000_000:
            if required: raise ValueError("Git status unavailable")
            return None
        return result.stdout.decode("utf-8", errors="replace")
    try:
        git("rev-parse", "--show-toplevel")
        raw = git("status", "--porcelain=v1", "-z", "--untracked-files=all")
        records = raw.split("\0"); count = index = 0
        while index < len(records):
            record = records[index]
            if record:
                count += 1
                if "R" in record[:2] or "C" in record[:2]: index += 1
            index += 1
        added = deleted = 0
        stat = git("diff", "--no-ext-diff", "--no-textconv", "--numstat", "-z", "HEAD", required=False)
        if stat is None:
            stat = git("diff", "--cached", "--no-ext-diff", "--no-textconv", "--numstat", "-z")
        for record in stat.split("\0"):
            parts = record.split("\t", 2)
            if len(parts) == 3 and parts[0].isdigit() and parts[1].isdigit():
                added += int(parts[0]); deleted += int(parts[1])
        upstream = git("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}", required=False)
        ahead = behind = 0
        if upstream:
            counts = git("rev-list", "--left-right", "--count", "HEAD...@{upstream}")
            ahead, behind = map(int, counts.split())
        else:
            ahead = int((git("rev-list", "--count", "HEAD", required=False) or "0").strip())
        return {"files": count, "added": added, "deleted": deleted, "ahead": ahead,
                "behind": behind if upstream else None, "upstream": upstream.strip() if upstream else None,
                "branch": (git("branch", "--show-current") or "").strip() or "Detached HEAD"}
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None
