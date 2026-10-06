"""Bounded assistant-only previews for observed local Codex threads."""
from __future__ import annotations
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import time

_UUID = re.compile(r"^[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$")
TAIL_BYTES = 256 * 1024


def _text(record):
    if not isinstance(record, dict) or not isinstance(record.get("payload"), dict):
        return None
    payload = record["payload"]
    if record.get("type") == "response_item":
        if payload.get("type") != "message" or payload.get("role") != "assistant":
            return None
        parts = payload.get("content")
        if not isinstance(parts, list):
            return None
        texts = [part["text"] for part in parts if isinstance(part, dict)
                 and part.get("type") == "output_text" and isinstance(part.get("text"), str)]
        return " ".join(texts) if texts else None
    if record.get("type") == "event_msg":
        if payload.get("type") == "agent_message":
            return payload.get("message") if isinstance(payload.get("message"), str) else None
        if payload.get("type") == "item_completed":
            item = payload.get("item")
            if isinstance(item, dict) and item.get("type") in ("AgentMessage", "agent_message"):
                return item.get("text") if isinstance(item.get("text"), str) else None
    return None


class RecentThreadPreviews:
    def __init__(self, config_home: Path | None = None):
        self.home = Path(config_home if config_home is not None else
                         os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))).expanduser().absolute()
        self.cache = {}

    def __call__(self, targets):
        if not isinstance(targets, list):
            return []
        result, requested, seen = [], [], set()
        for target in targets[:10]:
            if not isinstance(target, dict):
                continue
            tid, host, kind = target.get("threadId"), target.get("hostId"), target.get("kind")
            if not all(isinstance(value, str) for value in (tid, host, kind)) or not _UUID.fullmatch(tid):
                continue
            identity = (kind, host, tid)
            if identity in seen:
                continue
            seen.add(identity)
            result.append({"threadId": tid, "hostId": host, "kind": kind, "preview": None})
            if host == "local" and kind == "local":
                requested.append(tid)
        self.cache = {tid: value for tid, value in self.cache.items() if tid in requested}
        if not requested:
            return result
        deadline = time.monotonic() + 0.5
        connection = None
        try:
            candidates = [(int(match.group(1)), path) for path in self.home.glob("state_*.sqlite")
                          if (match := re.fullmatch(r"state_(\d+)\.sqlite", path.name))]
            if not candidates:
                return result
            database = max(candidates)[1]
            if database.is_symlink() or not database.is_file():
                return result
            connection = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=0.05)
            query_deadline = min(deadline, time.monotonic() + 0.2)
            connection.set_progress_handler(lambda: int(time.monotonic() > query_deadline), 1000)
            placeholders = ",".join("?" for _ in requested)
            rows = connection.execute(f"SELECT id, rollout_path FROM threads WHERE id IN ({placeholders})",
                                      tuple(requested)).fetchall()
            connection.close()
            connection = None
            paths = dict(rows)
            for item in result:
                if item["hostId"] != "local" or item["kind"] != "local" or time.monotonic() > deadline:
                    continue
                path = paths.get(item["threadId"])
                if isinstance(path, str):
                    item["preview"] = self._preview(item["threadId"], path)
            return result
        except (OSError, ValueError, sqlite3.Error):
            self.cache.clear()
            return result
        finally:
            if connection is not None:
                connection.close()

    def _open_rollout(self, path):
        candidate = Path(path)
        if not candidate.is_absolute():
            candidate = self.home / candidate
        try:
            relative = candidate.relative_to(self.home)
        except ValueError:
            return None
        parts = relative.parts
        if (len(parts) < 2 or parts[0] not in ("sessions", "archived_sessions")
                or ".." in parts or candidate.suffix != ".jsonl"):
            return None
        descriptors = []
        try:
            flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            descriptor = os.open(self.home, flags)
            descriptors.append(descriptor)
            for component in parts[:-1]:
                descriptor = os.open(component, flags, dir_fd=descriptor)
                descriptors.append(descriptor)
            fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor)
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                os.close(fd)
                return None
            return fd
        except (OSError, ValueError):
            return None
        finally:
            for descriptor in reversed(descriptors):
                os.close(descriptor)

    def _preview(self, thread_id, path):
        descriptor = self._open_rollout(path)
        if descriptor is None:
            self.cache.pop(thread_id, None)
            return None
        try:
            facts = os.fstat(descriptor)
            signature = (path, facts.st_dev, facts.st_ino, facts.st_size, facts.st_mtime_ns)
            cached = self.cache.get(thread_id)
            if cached and cached[0] == signature:
                return cached[1]
            offset = max(0, facts.st_size - TAIL_BYTES)
            os.lseek(descriptor, offset, os.SEEK_SET)
            tail = os.read(descriptor, min(TAIL_BYTES, facts.st_size))
            lines = tail.split(b"\n")
            if offset:
                lines = lines[1:]
            if tail and not tail.endswith(b"\n"):
                lines = lines[:-1]
            preview = None
            for line in reversed(lines):
                try:
                    text = _text(json.loads(line))
                except (ValueError, UnicodeError):
                    continue
                if isinstance(text, str) and text.strip():
                    preview = " ".join(text.split())[:512]
                    break
            self.cache[thread_id] = (signature, preview)
            return preview
        except (OSError, ValueError):
            self.cache.pop(thread_id, None)
            return None
        finally:
            os.close(descriptor)
