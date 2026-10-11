"""Paged Git/PR metadata for the inspector. Runs only in background workers.

Local inspection never fetches or writes Git state. GitHub reads use the user's
existing gh login, with strict time/output limits and no interactive prompts.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import selectors
import shutil
import subprocess
import threading
import time
from urllib.parse import urlsplit

from chat_inspector import _git

PAGE_SIZE = 20
MAX_ROWS = 200
MAX_OUTPUT = 256_000
SHA = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?\Z")
PR_QUERY = """query($owner:String!, $name:String!, $first:Int!, $after:String) {
  repository(owner:$owner, name:$name) {
    pullRequests(first:$first, after:$after, states:OPEN,
                 orderBy:{field:UPDATED_AT,direction:DESC}) {
      nodes { number title url headRefName isDraft author { login } }
      pageInfo { hasNextPage endCursor }
    }
  }
}"""


def _text(workspace, *args, optional=False):
    raw, truncated, code = _git(workspace, *args, limit=MAX_OUTPUT, timeout=3, allow_failure=optional)
    if truncated:
        raise ValueError("Git metadata exceeded the inspection limit.")
    return None if code and optional else raw.decode("utf-8", "replace").rstrip("\n")


def _repository(remote):
    if not remote:
        return None
    if re.match(r"^[^/]+@[^:]+:", remote):
        remote = "ssh://" + remote.replace(":", "/", 1)
    value = urlsplit(remote)
    if value.scheme not in {"https", "ssh"} or not value.hostname:
        return None
    parts = value.path.strip("/").removesuffix(".git").split("/")
    if len(parts) != 2 or not all(re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", item) for item in parts):
        return None
    host = value.hostname
    if host != "github.com" and not re.fullmatch(r"[A-Za-z0-9.-]+", host):
        return None
    return {"host": host, "owner": parts[0], "name": parts[1],
            "url": "https://" + host + "/" + "/".join(parts)}


def _remote(workspace, branch):
    remote = _text(workspace, "config", "--get", "branch." + branch + ".remote", optional=True) or "origin"
    if remote == "." or not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", remote):
        remote = "origin"
    return _repository(_text(workspace, "config", "--get", "remote." + remote + ".url", optional=True))


def git_overview(workspace, cursor=None):
    root = _text(workspace, "rev-parse", "--show-toplevel")
    head = _text(root, "rev-parse", "--verify", "HEAD", optional=True)
    branch = _text(root, "branch", "--show-current") or "Detached HEAD"
    upstream = _text(root, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}", optional=True)
    upstream_head = _text(root, "rev-parse", "--verify", "@{upstream}", optional=True) if upstream else None
    repository = _remote(root, branch)
    repository_url = repository["url"] if repository else None
    offset = 0
    if cursor is not None:
        if (not isinstance(cursor, dict) or cursor.get("head") != head
                or cursor.get("upstream") != upstream_head or cursor.get("root") != root
                or cursor.get("repository") != repository_url):
            raise ValueError("Git history changed. Refresh before loading more commits.")
        offset = cursor.get("offset")
        if type(offset) is not int or not 0 < offset < MAX_ROWS or offset % PAGE_SIZE:
            raise ValueError("Invalid commit page.")
    commits = []
    more = False
    if head:
        if not SHA.fullmatch(head) or upstream_head and not SHA.fullmatch(upstream_head):
            raise ValueError("Invalid Git revision.")
        revision = upstream_head + ".." + head if upstream_head else head
        # Subjects/authors cannot contain NUL; the five fields stay lossless
        # even with tabs, newlines, or record separator characters in a name.
        rows = _text(root, "log", "--no-show-signature", "--format=%H%x00%s%x00%an%x00%aI%x00",
                     "--max-count=" + str(PAGE_SIZE + 1), "--skip=" + str(offset), revision, "--").split("\0")
        for index in range(0, len(rows) - 3, 4):
            commit = rows[index].strip()
            if SHA.fullmatch(commit):
                commits.append({"hash": commit, "subject": rows[index + 1][:500],
                                "author": rows[index + 2][:160], "time": rows[index + 3]})
        more = len(commits) > PAGE_SIZE
        commits = commits[:PAGE_SIZE]
    return {"root": root, "head": head, "branch": branch, "upstream": upstream,
            "repository": repository, "commits": commits,
            "nextCursor": {"head": head, "upstream": upstream_head, "root": root,
                           "repository": repository_url, "offset": offset + PAGE_SIZE}
                if more and offset + PAGE_SIZE < MAX_ROWS else None,
            "truncated": more and offset + PAGE_SIZE >= MAX_ROWS}


def _gh(command, *, timeout=6):
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(GH_PROMPT_DISABLED="1", GH_PAGER="cat", NO_COLOR="1")
    process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, env=env)
    chunks = {process.stdout: bytearray(), process.stderr: bytearray()}
    deadline = time.monotonic() + timeout
    total = 0
    try:
        with selectors.DefaultSelector() as selector:
            for stream in chunks:
                selector.register(stream, selectors.EVENT_READ)
            while selector.get_map():
                if time.monotonic() >= deadline:
                    raise ValueError("Pull request lookup timed out. Refresh to try again.")
                for key, _ in selector.select(min(0.1, max(0, deadline - time.monotonic()))):
                    data = os.read(key.fileobj.fileno(), 65536)
                    if not data:
                        selector.unregister(key.fileobj)
                        continue
                    total += len(data)
                    if total > MAX_OUTPUT:
                        raise ValueError("Pull request response exceeded the inspection limit.")
                    chunks[key.fileobj].extend(data)
        process.wait(timeout=max(0.1, deadline - time.monotonic()))
        if process.returncode:
            raise ValueError("Pull requests unavailable. Check this repository's access in GitHub CLI.")
        return json.loads(bytes(chunks[process.stdout]))
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()
        for stream in chunks:
            stream.close()


def pull_requests(workspace, cursor=None):
    branch = _text(workspace, "branch", "--show-current") or "Detached HEAD"
    repository = _remote(workspace, branch)
    if repository is None:
        return {"items": [], "repository": None, "nextCursor": None,
                "notice": "No GitHub remote is configured."}
    gh = shutil.which("gh") or next((path for path in ("/opt/homebrew/bin/gh", "/usr/local/bin/gh")
                                    if os.path.isfile(path) and os.access(path, os.X_OK)), None)
    if gh is None:
        return {"items": [], "repository": repository, "nextCursor": None,
                "notice": "Open pull requests on GitHub, or install GitHub CLI to list them here."}
    after = None
    if cursor is not None:
        if (not isinstance(cursor, dict) or cursor.get("repository") != repository["url"]
                or not isinstance(cursor.get("after"), str) or not 1 <= len(cursor["after"]) <= 512):
            raise ValueError("Repository changed. Refresh pull requests.")
        after = cursor["after"]
    command = [gh, "api", "graphql", "--hostname", repository["host"],
               "-f", "query=" + PR_QUERY, "-f", "owner=" + repository["owner"],
               "-f", "name=" + repository["name"], "-F", "first=" + str(PAGE_SIZE)]
    if after:
        command.extend(["-f", "after=" + after])
    result = _gh(command)
    if result.get("errors"):
        raise ValueError("Pull requests unavailable for this repository.")
    page = result["data"]["repository"]["pullRequests"]
    items = []
    for item in page["nodes"][:PAGE_SIZE]:
        link = urlsplit(str(item.get("url", "")))
        if link.scheme != "https" or link.hostname != repository["host"]:
            continue
        items.append({"number": int(item["number"]), "title": str(item["title"])[:500],
                      "url": item["url"], "branch": str(item["headRefName"])[:160],
                      "author": str((item.get("author") or {}).get("login", ""))[:160],
                      "draft": bool(item.get("isDraft"))})
    next_cursor = ({"repository": repository["url"], "after": page["pageInfo"]["endCursor"]}
                   if page["pageInfo"]["hasNextPage"] else None)
    return {"items": items, "repository": repository, "nextCursor": next_cursor, "notice": ""}


def handle(parent, command):
    action = command.get("command")
    if action not in {"git_overview", "git_pull_requests"}:
        return False
    identifier = command.get("id")
    with parent._mutex:
        if not parent.chat or identifier != parent.chat["id"]:
            raise ValueError("Select the current chat.")
        workspace = parent.chat["workspace"]
        pending = getattr(parent, "_git_overview_pending", None)
        if pending is None:
            pending = parent._git_overview_pending = set()
        if action in pending:
            parent.emit({"event": action, "chat": identifier, "workspace": workspace,
                         "request": command.get("request"), "notice": "A lookup is already running."})
            return True
        pending.add(action)
    def report():
        event = {"event": action, "chat": identifier, "workspace": workspace, "request": command.get("request")}
        try:
            event["overview" if action == "git_overview" else "pullRequests"] = (
                git_overview if action == "git_overview" else pull_requests)(workspace, command.get("cursor"))
        except ValueError as error:
            event["notice"] = str(error)[:300]
        except (OSError, KeyError, TypeError, subprocess.SubprocessError):
            event["notice"] = "Git information could not refresh. Check the repository and try again."
        finally:
            with parent._mutex:
                pending.discard(action)
            parent.emit(event)
    threading.Thread(target=report, name="chat-" + action, daemon=True).start()
    return True
