"""Bounded, resumable attachment staging for an authenticated remote device.

Uploads are opaque chat-bound handles, never phone-supplied Mac paths. A final
send copies originals through the normal chat attachment pipeline. Temporary
staging expires, is capped, and is removed after acceptance or host shutdown.
"""
import base64
import hashlib
import os
from pathlib import Path
import re
import tempfile
import time
import stat

MAX_FILE = 8 * 1024 * 1024
MAX_TOTAL = 20 * 1024 * 1024
CHUNK = 18 * 1024
LEASE = 15 * 60
HANDLE = re.compile(r"[0-9a-f]{32}\Z")


def read_attachment(chat, root, identifier, offset, *, expected=None, thumbnail=False):
    """Serve a bounded chunk of an original already owned by this chat."""
    from chat_blackboard import attachments
    if not isinstance(identifier, str) or len(identifier) > 128 or type(offset) is not int or offset < 0:
        raise ValueError("Choose an attachment in this chat")
    row = next((r for r in attachments(chat) if r.get("id") == identifier), None)
    if not row or not row.get("path") or row.get("url"): raise ValueError("Attachment not found in this chat")
    root = Path(root)
    parent = root / chat["id"] / ("blackboard" if row.get("source") == "board" else "attachments")
    path = Path(row["path"])
    if path.parent != parent or any(p.is_symlink() for p in (root, root / chat["id"], parent)):
        raise ValueError("Attachment is outside this chat’s private storage")
    directory = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try: fd = os.open(path.name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=directory)
    finally: os.close(directory)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > 64 * 1024 * 1024: raise ValueError("Preview supports regular files up to 64 MiB")
        revision = hashlib.sha256(f"{info.st_ino}:{info.st_size}:{info.st_mtime_ns}".encode()).hexdigest()
        if expected is not None and expected != revision: raise ValueError("Attachment changed while downloading; try again")
        if offset > info.st_size: raise ValueError("Invalid download offset")
        if thumbnail:
            if row.get("kind") != "image" or info.st_size > MAX_FILE: raise ValueError("No thumbnail available")
            import subprocess
            from chat_blackboard import PDF_HELPER
            try: rendered = subprocess.run([str(PDF_HELPER), "--thumbnail"], input=stream.read(MAX_FILE + 1), capture_output=True, timeout=8, check=True).stdout
            except (OSError, subprocess.SubprocessError) as exc: raise ValueError("Thumbnail unavailable; tap to open the original") from exc
            if len(rendered) > 100000: raise ValueError("Thumbnail exceeds size limit")
            return {"id": identifier, "data": base64.b64encode(rendered).decode(), "mime": "image/jpeg"}
        stream.seek(offset)
        raw = stream.read(CHUNK)
    return {"id": identifier, "name": str(row.get("name") or path.name), "kind": row.get("kind", "file"),
            "size": info.st_size, "revision": revision, "offset": offset, "nextOffset": offset + len(raw),
            "data": base64.b64encode(raw).decode(), "done": offset + len(raw) == info.st_size}


class RemoteFiles:
    def __init__(self, root, *, clock=time.monotonic):
        self.directory = tempfile.TemporaryDirectory(prefix="remote-uploads-", dir=root)
        self.rows = {}
        self.clock = clock

    def close(self):
        self.rows.clear()
        self.directory.cleanup()

    def prune(self):
        for token, row in list(self.rows.items()):
            if self.clock() - row["updated"] > LEASE: self.drop(row["chat"], token)

    def get(self, chat, token, *, complete=False):
        self.prune()
        if not isinstance(token, str) or not HANDLE.fullmatch(token): raise ValueError("Invalid upload handle")
        row = self.rows.get(token)
        if row is None or row["chat"] != chat: raise ValueError("Upload expired or belongs to another chat. Attach the file again.")
        if complete and not row["complete"]: raise ValueError("Upload has not finished")
        row["updated"] = self.clock()
        return row

    def begin(self, chat, token, name, size, digest):
        self.prune()
        if not isinstance(token, str) or not HANDLE.fullmatch(token): raise ValueError("Invalid upload handle")
        if not isinstance(name, str) or not name or len(name.encode()) > 240 or name in {".", ".."} or any(c in name for c in ("/", "\\", "\x00")):
            raise ValueError("Use a plain attachment filename")
        if type(size) is not int or not 0 < size <= MAX_FILE: raise ValueError("Files may be up to 8 MiB each")
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest): raise ValueError("Attachment checksum required")
        if token in self.rows:
            row = self.get(chat, token)
            if (row["name"], row["size"], row["digest"]) != (name, size, digest): raise ValueError("Upload identity was reused for a different file")
            return {"upload": token, "offset": row["received"]}
        owned = [row for row in self.rows.values() if row["chat"] == chat]
        if len(owned) >= 8 or sum(row["size"] for row in owned) + size > MAX_TOTAL or sum(row["size"] for row in self.rows.values()) + size > 160 * 1024 * 1024:
            raise ValueError("At most eight staged files and 20 MiB total. Remove unused attachments first.")
        # A separate private directory preserves the display filename for the
        # existing intake pipeline without trusting it as a storage path.
        folder = Path(self.directory.name) / token
        folder.mkdir(mode=0o700)
        path = folder / name
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        self.rows[token] = {"chat": chat, "name": name, "size": size, "digest": digest, "path": path,
                            "received": 0, "complete": False, "updated": self.clock()}
        return {"upload": token, "offset": 0}

    def chunk(self, chat, token, offset, encoded):
        row = self.get(chat, token)
        if type(offset) is not int or offset < 0 or not isinstance(encoded, str) or len(encoded) > CHUNK * 4 // 3 + 4:
            raise ValueError("Invalid attachment chunk")
        try: raw = base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError): raise ValueError("Invalid attachment encoding") from None
        if not raw or len(raw) > CHUNK or offset + len(raw) > row["size"]: raise ValueError("Attachment chunk exceeds its declared bounds")
        if offset < row["received"]:
            with row["path"].open("rb") as stream:
                stream.seek(offset)
                if offset + len(raw) <= row["received"] and stream.read(len(raw)) == raw:
                    return {"upload": token, "offset": row["received"]}
            raise ValueError("Attachment replay differs from its original bytes")
        if row["complete"] or offset != row["received"]: raise ValueError("Attachment chunk is out of order")
        with row["path"].open("ab") as stream: stream.write(raw)
        row["received"] += len(raw)
        return {"upload": token, "offset": row["received"]}

    def finish(self, chat, token):
        row = self.get(chat, token)
        if row["received"] != row["size"]: raise ValueError("Attachment is incomplete")
        if hashlib.sha256(row["path"].read_bytes()).hexdigest() != row["digest"]: raise ValueError("Attachment checksum mismatch")
        row["complete"] = True
        return {"upload": token, "name": row["name"], "size": row["size"]}

    def inputs(self, chat, tokens):
        if not isinstance(tokens, list) or len(tokens) > 8 or len(set(tokens)) != len(tokens): raise ValueError("Choose up to eight uploaded attachments")
        rows = [self.get(chat, token, complete=True) for token in tokens]
        if sum(row["size"] for row in rows) > MAX_TOTAL: raise ValueError("Attachments exceed 20 MiB per message")
        inputs = []
        for row in rows:
            item = {"path": str(row["path"])}
            with row["path"].open("rb") as stream: pdf = stream.read(5) == b"%PDF-"
            if pdf:
                from chat_blackboard import PDF_HELPER
                import subprocess
                try:
                    result = subprocess.run([str(PDF_HELPER)], input=row["path"].read_bytes(), capture_output=True, timeout=15, check=True)
                except (OSError, subprocess.SubprocessError) as exc:
                    raise ValueError("PDF text extraction is unavailable. Attach text or images of its pages instead.") from exc
                item["extractedText"] = result.stdout.decode("utf8", errors="ignore")[:200000]
            inputs.append(item)
        return inputs

    def drop(self, chat, token):
        row = self.rows.get(token)
        if row is None: return
        if row["chat"] != chat: raise ValueError("Upload belongs to another chat")
        row["path"].unlink(missing_ok=True)
        row["path"].parent.rmdir()
        del self.rows[token]
