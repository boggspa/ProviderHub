"""Durable, workspace-scoped peer mailboxes for enrolled threads.

One PeerBroker owns one private SQLite database under its directory, held by
an exclusive flock for the broker's lifetime. Endpoints are logical threads
with a stable UUID per (app, native_thread_id, canonical workspace). External
threads authenticate with an enrolment token (stored only as a SHA-256 digest);
Provider Hub's own threads are trusted in-process through internal().

Every operation runs as a Principal: an opaque, broker-issued object bound to
one endpoint instance. A newer attach, a disconnect, a revoke or a broker
restart makes older principals stale. The sender of a message is always the
principal, never a caller argument, and endpoints only see and message threads
in their own canonical workspace.

Messages are reference-only coordination text. The broker never wakes a
thread, runs anything, or grants a permission: "presented" means a read
returned the message, not that anyone understood or acted on it.
"""
from __future__ import annotations

import contextlib
from datetime import datetime, timezone
import fcntl
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat
import threading
import time
import unicodedata
import uuid

APP_INTERNAL = "provider_hub"
SCHEMA_VERSION = 1
DATABASE_NAME = "peers.sqlite3"
LOCK_NAME = "peers.lock"
LEASE_SECONDS = 120.0
MESSAGE_TTL_SECONDS = 7 * 24 * 3600
# Expired and acknowledged messages (and their idempotency keys) are kept this
# long past their expiry before they are pruned.
RETENTION_SECONDS = 7 * 24 * 3600
BODY_LIMIT = 16 * 1024
QUEUE_LIMIT = 256
READ_DEFAULT = 20
READ_LIMIT = 50
LIST_LIMIT = 128
TOKEN_PREFIX = "pbt_"
KINDS = ("coordination", "delegation", "reply")
THREAD_STATUSES = ("online", "offline", "revoked")
CAPABILITIES = {"poll": True, "wake": False}
APP_PATTERN = re.compile(r"[a-z0-9][a-z0-9_.-]{0,63}")
SEQUENCE_MAX = 2**63 - 1

# Tool name -> (allowed argument names, required argument names).
TOOLS = {
    "hub_self": (frozenset(), frozenset()),
    "hub_list_threads": (frozenset({"status", "app"}), frozenset()),
    "hub_send_message": (
        frozenset({"to", "body", "kind", "reply_to", "idempotency_key"}),
        frozenset({"to", "body", "idempotency_key"}),
    ),
    "hub_read_messages": (frozenset({"after", "limit"}), frozenset()),
    "hub_ack_message": (frozenset({"message_id"}), frozenset({"message_id"})),
    "hub_message_status": (frozenset({"message_id"}), frozenset({"message_id"})),
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS threads (
    thread_id TEXT PRIMARY KEY,
    app TEXT NOT NULL,
    native_thread_id TEXT NOT NULL,
    workspace TEXT NOT NULL,
    label TEXT NOT NULL,
    model TEXT NOT NULL,
    provider TEXT NOT NULL,
    assurance TEXT NOT NULL,
    token_hash BLOB,
    instance_id TEXT,
    status TEXT NOT NULL,
    last_seen REAL,
    created_at REAL NOT NULL,
    revoked_at REAL,
    UNIQUE (app, native_thread_id, workspace)
);
CREATE INDEX IF NOT EXISTS threads_workspace ON threads (workspace, status);
CREATE TABLE IF NOT EXISTS messages (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id TEXT NOT NULL UNIQUE,
    workspace TEXT NOT NULL,
    sender_thread_id TEXT NOT NULL,
    recipient_thread_id TEXT NOT NULL,
    sender_snapshot TEXT NOT NULL,
    recipient_snapshot TEXT NOT NULL,
    sender_instance_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    body TEXT NOT NULL,
    reply_to TEXT,
    idempotency_key TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    status TEXT NOT NULL,
    presented_at REAL,
    acknowledged_at REAL,
    UNIQUE (sender_thread_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS messages_inbox ON messages (recipient_thread_id, status, sequence);
"""


class PeerAuthError(PermissionError):
    """Authentication failed or the principal is no longer the live instance."""

    def __init__(self, message="Peer authentication failed.", code="unauthenticated"):
        super().__init__(message)
        self.code = code


class PeerRequestError(ValueError):
    """A rejected request. `code` is a stable machine-readable reason."""

    def __init__(self, message, code="invalid_argument"):
        super().__init__(message)
        self.code = code


class PeerBrokerBusy(RuntimeError):
    """Another broker already owns this directory."""


class Principal:
    """An authenticated endpoint instance. Opaque: only its broker can validate it."""

    __slots__ = ("_broker", "_thread_id", "_instance_id", "__weakref__")

    def __init__(self, broker, thread_id, instance_id):
        self._broker = broker
        self._thread_id = thread_id
        self._instance_id = instance_id

    @property
    def thread_id(self):
        return self._thread_id

    @property
    def instance_id(self):
        return self._instance_id

    def __repr__(self):
        return f"<Principal thread={self._thread_id} instance={self._instance_id}>"


def iso(moment):
    if moment is None:
        return None
    stamp = datetime.fromtimestamp(moment, timezone.utc).isoformat(timespec="milliseconds")
    return stamp.replace("+00:00", "Z")


def _text(value, name, limit, *, required=True, multiline=False):
    if value is None and not required:
        return ""
    if not isinstance(value, str):
        raise PeerRequestError(f"{name} must be a string.")
    if required and not value.strip():
        raise PeerRequestError(f"{name} must not be empty.")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError:
        raise PeerRequestError(f"{name} must be valid UTF-8 text.") from None
    if size > limit:
        raise PeerRequestError(f"{name} exceeds {limit} bytes.", "too_large")
    for character in value:
        if multiline and character in "\n\r\t":
            continue
        if unicodedata.category(character) in ("Cc", "Cs"):
            raise PeerRequestError(f"{name} must not contain control characters.")
    return value


def _uuid(value, name):
    if not isinstance(value, str) or len(value) > 64:
        raise PeerRequestError(f"{name} must be a UUID string.")
    try:
        return str(uuid.UUID(value))
    except ValueError:
        raise PeerRequestError(f"{name} must be a UUID string.") from None


def canonical_workspace(path):
    """The workspace identity endpoints are scoped by: an absolute, resolved path."""
    if isinstance(path, os.PathLike):
        path = os.fspath(path)
    text = _text(path, "workspace", 4096)
    if not os.path.isabs(text):
        raise PeerRequestError("workspace must be an absolute path.")
    return os.path.realpath(text)


def _app(value):
    text = _text(value, "app", 64)
    if not APP_PATTERN.fullmatch(text):
        raise PeerRequestError("app must be a short lowercase identifier.")
    return text


def _capabilities(value):
    if value is None:
        return
    if not isinstance(value, dict):
        raise PeerRequestError("capabilities must be an object.")
    for key, flag in value.items():
        if key not in CAPABILITIES or not isinstance(flag, bool):
            raise PeerRequestError(f"Unsupported capability: {key!r}.")
        if flag != CAPABILITIES[key]:
            raise PeerRequestError(f"The broker does not support {key}={flag}.", "unsupported")


def _digest(token):
    return hashlib.sha256(token.encode("utf-8")).digest()


def _private_file(path):
    """Open or create a regular file private to this user, never through a link."""
    try:
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    except OSError as error:
        raise PermissionError(f"Refusing to open {path.name}: {error.strerror}.") from None
    info = os.fstat(descriptor)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
        os.close(descriptor)
        raise PermissionError(f"{path.name} must be a regular file private to this user.")
    return descriptor


def _private_directory(directory):
    if directory.is_symlink():
        raise PermissionError("The peer broker directory must not be a symbolic link.")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = os.lstat(directory)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise PermissionError("The peer broker directory must be a directory owned by this user.")
    os.chmod(directory, 0o700)
    if stat.S_IMODE(os.lstat(directory).st_mode) & 0o077:
        raise PermissionError("The peer broker directory must be private to this user.")


class PeerBroker:
    """The single owner of one peer database. Thread-safe; every call is one transaction."""

    def __init__(self, directory, clock=time.time, apps=None):
        self.directory = Path(directory)
        self._clock = clock
        self._apps = None if apps is None else frozenset(_app(app) for app in apps)
        if self._apps is not None and APP_INTERNAL in self._apps:
            raise PeerRequestError("provider_hub is not an external app.")
        self._lock = threading.RLock()
        self._live = {}  # thread_id -> the one Principal currently valid for it
        self._closed = False
        self._lock_descriptor = None
        self._db = None
        _private_directory(self.directory)
        lock_path = self.directory / LOCK_NAME
        descriptor = _private_file(lock_path)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(descriptor)
            raise PeerBrokerBusy("Another peer broker owns this directory.") from None
        self._lock_descriptor = descriptor
        try:
            self._open_database()
        except BaseException:
            self.close()
            raise

    # -- storage ---------------------------------------------------------

    def _open_database(self):
        path = self.directory / DATABASE_NAME
        for suffix in ("-journal", "-wal", "-shm"):
            if os.path.islink(str(path) + suffix):
                raise PermissionError("Refusing a symbolic link beside the peer database.")
        descriptor = _private_file(path)
        try:
            expected = os.fstat(descriptor)
            self._db = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
            actual = os.lstat(path)
            if (actual.st_dev, actual.st_ino) != (expected.st_dev, expected.st_ino):
                raise PermissionError("The peer database changed while it was being opened.")
        finally:
            os.close(descriptor)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA foreign_keys = ON")
        self._db.execute("PRAGMA synchronous = FULL")
        self._db.execute("PRAGMA secure_delete = ON")
        version = self._db.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise PeerRequestError("The peer database is from a newer version.", "unsupported")
        # executescript commits any open transaction itself, so it runs its own.
        self._db.executescript("BEGIN IMMEDIATE;" + SCHEMA + f"PRAGMA user_version = {SCHEMA_VERSION}; COMMIT;")
        with self._transaction():
            # A new broker is the only owner, so no earlier instance is still attached.
            self._db.execute(
                "UPDATE threads SET status = 'offline', instance_id = NULL WHERE status = 'online'"
            )

    @contextlib.contextmanager
    def _transaction(self):
        with self._lock:
            if self._closed or self._db is None:
                raise PeerAuthError("The peer broker is closed.", "closed")
            self._db.execute("BEGIN IMMEDIATE")
            try:
                yield self._db
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
            else:
                self._db.execute("COMMIT")

    def _now(self):
        moment = float(self._clock())
        if moment != moment or moment in (float("inf"), float("-inf")):
            raise RuntimeError("The broker clock returned a non-finite time.")
        return moment

    def _refresh(self, db, now):
        """Lazily apply leases, expiry and retention."""
        db.execute(
            "UPDATE threads SET status = 'offline' WHERE status = 'online' AND last_seen < ?",
            (now - LEASE_SECONDS,),
        )
        db.execute(
            "UPDATE messages SET status = 'expired' WHERE status IN ('queued', 'presented') AND expires_at <= ?",
            (now,),
        )
        db.execute(
            "DELETE FROM messages WHERE status IN ('expired', 'acknowledged') AND expires_at <= ?",
            (now - RETENTION_SECONDS,),
        )

    def close(self):
        with self._lock:
            self._closed = True
            self._live.clear()
            if self._db is not None:
                self._db.close()
                self._db = None
            if self._lock_descriptor is not None:
                os.close(self._lock_descriptor)
                self._lock_descriptor = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # -- views -----------------------------------------------------------

    @staticmethod
    def _endpoint(row):
        return {
            "thread_id": row["thread_id"],
            "native_thread_id": row["native_thread_id"],
            "app": row["app"],
            "workspace": row["workspace"],
            "label": row["label"],
            "model": row["model"],
            "provider": row["provider"],
            "identity_assurance": row["assurance"],
            "instance_id": row["instance_id"],
            "status": row["status"],
            "last_seen": iso(row["last_seen"]),
            "capabilities": dict(CAPABILITIES),
        }

    @staticmethod
    def _snapshot(row):
        """The immutable identity a message records for its sender or recipient."""
        return {
            "thread_id": row["thread_id"],
            "app": row["app"],
            "native_thread_id": row["native_thread_id"],
            "label": row["label"],
            "model": row["model"],
            "provider": row["provider"],
            "identity_assurance": row["assurance"],
        }

    @staticmethod
    def _message(row, viewer):
        view = {
            "message_id": row["message_id"],
            "sequence": row["sequence"],
            "kind": row["kind"],
            "body": row["body"],
            "reply_to": row["reply_to"],
            "sender": json.loads(row["sender_snapshot"]),
            "recipient": json.loads(row["recipient_snapshot"]),
            "sender_instance_id": row["sender_instance_id"],
            "origin": "peer_agent",
            "authority": "reference_only",
            "created_at": iso(row["created_at"]),
            "expires_at": iso(row["expires_at"]),
            "status": row["status"],
            "presented_at": iso(row["presented_at"]),
            "acknowledged_at": iso(row["acknowledged_at"]),
        }
        if viewer == row["sender_thread_id"]:
            view["idempotency_key"] = row["idempotency_key"]
        return view

    def _row(self, db, thread_id):
        return db.execute("SELECT * FROM threads WHERE thread_id = ?", (thread_id,)).fetchone()

    # -- enrolment -------------------------------------------------------

    def enroll(self, *, app, native_thread_id, workspace, label="", model="", provider="",
               capabilities=None, verified=False):
        """Enrol an external thread. Returns {thread, token}; the token is shown only here.

        Never rotates a live endpoint: enrolling an existing, unrevoked identity
        raises PeerRequestError(code='duplicate'). A revoked identity may be
        re-enrolled; it keeps its thread_id and gets a new token.
        """
        if verified is not False:
            raise PeerRequestError("External enrolment can never be native-verified.", "forbidden")
        app = _app(app)
        if app == APP_INTERNAL:
            raise PeerRequestError("provider_hub threads are registered internally.", "forbidden")
        if self._apps is not None and app not in self._apps:
            raise PeerRequestError(f"Unknown app: {app}.", "forbidden")
        native = _text(native_thread_id, "native_thread_id", 256)
        workspace = canonical_workspace(workspace)
        label = _text(label, "label", 200, required=False)
        model = _text(model, "model", 200, required=False)
        provider = _text(provider, "provider", 64, required=False)
        _capabilities(capabilities)
        token = TOKEN_PREFIX + secrets.token_urlsafe(32)
        with self._transaction() as db:
            now = self._now()
            self._refresh(db, now)
            existing = db.execute(
                "SELECT * FROM threads WHERE app = ? AND native_thread_id = ? AND workspace = ?",
                (app, native, workspace),
            ).fetchone()
            if existing is not None and existing["status"] != "revoked":
                raise PeerRequestError("This thread is already enrolled.", "duplicate")
            if existing is None:
                thread_id = str(uuid.uuid4())
                db.execute(
                    "INSERT INTO threads (thread_id, app, native_thread_id, workspace, label, model,"
                    " provider, assurance, token_hash, instance_id, status, last_seen, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, 'enrolled', ?, NULL, 'offline', NULL, ?)",
                    (thread_id, app, native, workspace, label, model, provider, _digest(token), now),
                )
            else:
                thread_id = existing["thread_id"]
                db.execute(
                    "UPDATE threads SET label = ?, model = ?, provider = ?, token_hash = ?,"
                    " instance_id = NULL, status = 'offline', revoked_at = NULL WHERE thread_id = ?",
                    (label, model, provider, _digest(token), thread_id),
                )
            return {"thread": self._endpoint(self._row(db, thread_id)), "token": token}

    def ensure_internal(self, native_thread_id, workspace, label="", model="", provider="",
                        *, restore=False):
        """Register or refresh a Provider Hub thread; the only native-verified path.

        Updates the observed label/model/provider on the same identity. A revoked
        endpoint stays revoked unless restore=True.
        """
        native = _text(native_thread_id, "native_thread_id", 256)
        workspace = canonical_workspace(workspace)
        label = _text(label, "label", 200, required=False)
        model = _text(model, "model", 200, required=False)
        provider = _text(provider, "provider", 64, required=False)
        with self._transaction() as db:
            now = self._now()
            self._refresh(db, now)
            existing = db.execute(
                "SELECT * FROM threads WHERE app = ? AND native_thread_id = ? AND workspace = ?",
                (APP_INTERNAL, native, workspace),
            ).fetchone()
            if existing is None:
                thread_id = str(uuid.uuid4())
                db.execute(
                    "INSERT INTO threads (thread_id, app, native_thread_id, workspace, label, model,"
                    " provider, assurance, token_hash, instance_id, status, last_seen, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, 'native', NULL, NULL, 'offline', NULL, ?)",
                    (thread_id, APP_INTERNAL, native, workspace, label, model, provider, now),
                )
            else:
                thread_id = existing["thread_id"]
                if existing["status"] == "revoked" and not restore:
                    raise PeerAuthError("This thread's peer endpoint is revoked.", "revoked")
                restoring = existing["status"] == "revoked"
                db.execute(
                    "UPDATE threads SET label = ?, model = ?, provider = ?, status = ?,"
                    " revoked_at = ? WHERE thread_id = ?",
                    (label, model, provider, "offline" if restoring else existing["status"],
                     None if restoring else existing["revoked_at"], thread_id),
                )
            return self._endpoint(self._row(db, thread_id))

    def find_internal(self, native_thread_id, workspace):
        """Look up a Provider Hub thread's endpoint without creating or reviving it."""
        native = _text(native_thread_id, "native_thread_id", 256)
        workspace = canonical_workspace(workspace)
        with self._transaction() as db:
            self._refresh(db, self._now())
            row = db.execute(
                "SELECT * FROM threads WHERE app = ? AND native_thread_id = ? AND workspace = ?",
                (APP_INTERNAL, native, workspace),
            ).fetchone()
            return None if row is None else self._endpoint(row)

    # -- principals ------------------------------------------------------

    def _issue(self, db, row, now):
        principal = Principal(self, row["thread_id"], str(uuid.uuid4()))
        db.execute(
            "UPDATE threads SET instance_id = ?, status = 'online', last_seen = ? WHERE thread_id = ?",
            (principal.instance_id, now, row["thread_id"]),
        )
        self._live[row["thread_id"]] = principal
        return principal

    def attach(self, thread_id, token):
        """Authenticate an external endpoint and start a new instance of it.

        Any earlier principal for the thread becomes stale.
        """
        if not isinstance(token, str) or len(token) > 256:
            raise PeerAuthError()
        try:
            thread_id = _uuid(thread_id, "thread_id")
        except PeerRequestError:
            raise PeerAuthError() from None
        presented = _digest(token)
        with self._transaction() as db:
            now = self._now()
            self._refresh(db, now)
            row = self._row(db, thread_id)
            stored = row["token_hash"] if row is not None and row["token_hash"] is not None else b"\0" * 32
            matched = hmac.compare_digest(bytes(stored), presented)
            if row is None or row["token_hash"] is None or not matched:
                raise PeerAuthError()
            if row["status"] == "revoked" or row["app"] == APP_INTERNAL:
                raise PeerAuthError()
            return self._issue(db, row, now)

    def internal(self, thread_id):
        """The trusted in-process principal for a Provider Hub thread.

        Reused until it is disconnected, revoked or the broker restarts.
        """
        thread_id = _uuid(thread_id, "thread_id")
        with self._transaction() as db:
            now = self._now()
            self._refresh(db, now)
            row = self._row(db, thread_id)
            if row is None or row["app"] != APP_INTERNAL or row["assurance"] != "native":
                raise PeerAuthError("Not a Provider Hub thread.", "forbidden")
            if row["status"] == "revoked":
                raise PeerAuthError("This thread's peer endpoint is revoked.", "revoked")
            principal = self._live.get(thread_id)
            if principal is not None and principal.instance_id == row["instance_id"]:
                db.execute(
                    "UPDATE threads SET status = 'online', last_seen = ? WHERE thread_id = ?",
                    (now, thread_id),
                )
                return principal
            return self._issue(db, row, now)

    def _authenticate(self, db, principal, now):
        """The principal's current endpoint row, or PeerAuthError. Marks activity."""
        if not isinstance(principal, Principal) or principal._broker is not self:
            raise PeerAuthError()
        if self._live.get(principal.thread_id) is not principal:
            raise PeerAuthError("This peer session is no longer current.", "stale")
        row = self._row(db, principal.thread_id)
        if row is None or row["status"] == "revoked" or row["instance_id"] != principal.instance_id:
            self._live.pop(principal.thread_id, None)
            raise PeerAuthError("This peer session is no longer current.", "stale")
        db.execute(
            "UPDATE threads SET status = 'online', last_seen = ? WHERE thread_id = ?",
            (now, principal.thread_id),
        )
        return self._row(db, principal.thread_id)

    def disconnect(self, principal):
        """Mark the endpoint offline if principal is still its current instance."""
        with self._lock:
            if not isinstance(principal, Principal) or principal._broker is not self:
                return False
            if self._live.get(principal.thread_id) is not principal:
                return False
            with self._transaction() as db:
                self._live.pop(principal.thread_id, None)
                db.execute(
                    "UPDATE threads SET status = 'offline', instance_id = NULL"
                    " WHERE thread_id = ? AND instance_id = ? AND status != 'revoked'",
                    (principal.thread_id, principal.instance_id),
                )
                return True

    def revoke(self, thread_id):
        """Invalidate the endpoint's token and principals immediately."""
        thread_id = _uuid(thread_id, "thread_id")
        with self._transaction() as db:
            now = self._now()
            self._live.pop(thread_id, None)
            cursor = db.execute(
                "UPDATE threads SET status = 'revoked', token_hash = NULL, instance_id = NULL,"
                " revoked_at = ? WHERE thread_id = ? AND status != 'revoked'",
                (now, thread_id),
            )
            return cursor.rowcount == 1

    def thread(self, thread_id):
        """Public metadata for one endpoint (trusted callers), or None."""
        thread_id = _uuid(thread_id, "thread_id")
        with self._transaction() as db:
            self._refresh(db, self._now())
            row = self._row(db, thread_id)
            return None if row is None else self._endpoint(row)

    # -- tools -----------------------------------------------------------

    def dispatch(self, principal, name, args=None):
        """Run one peer tool as principal. The sender is always the principal."""
        if not isinstance(name, str) or name not in TOOLS:
            raise PeerRequestError(f"Unknown peer tool: {name!r}.", "unknown_tool")
        if args is None:
            args = {}
        if not isinstance(args, dict) or not all(isinstance(key, str) for key in args):
            raise PeerRequestError("Arguments must be an object.")
        allowed, required = TOOLS[name]
        unknown = sorted(set(args) - allowed)
        if unknown:
            raise PeerRequestError(f"Unexpected argument: {unknown[0]}.")
        missing = sorted(required - set(args))
        if missing:
            raise PeerRequestError(f"Missing argument: {missing[0]}.")
        handler = getattr(self, "_tool_" + name)
        with self._transaction() as db:
            now = self._now()
            self._refresh(db, now)
            me = self._authenticate(db, principal, now)
            return handler(db, me, now, args)

    def _tool_hub_self(self, db, me, now, args):
        return {"thread": self._endpoint(me)}

    def _tool_hub_list_threads(self, db, me, now, args):
        query = "SELECT * FROM threads WHERE workspace = ? AND thread_id != ?"
        params = [me["workspace"], me["thread_id"]]
        status = args.get("status")
        if status is None:
            query += " AND status != 'revoked'"
        else:
            if status not in THREAD_STATUSES:
                raise PeerRequestError("status must be online, offline or revoked.")
            query += " AND status = ?"
            params.append(status)
        if "app" in args:
            query += " AND app = ?"
            params.append(_app(args["app"]))
        query += " ORDER BY status = 'online' DESC, last_seen DESC, thread_id LIMIT ?"
        params.append(LIST_LIMIT + 1)
        rows = db.execute(query, params).fetchall()
        return {
            "threads": [self._endpoint(row) for row in rows[:LIST_LIMIT]],
            "truncated": len(rows) > LIST_LIMIT,
        }

    def _tool_hub_send_message(self, db, me, now, args):
        to = _uuid(args["to"], "to")
        body = _text(args["body"], "body", BODY_LIMIT, multiline=True)
        kind = args.get("kind", "coordination")
        if kind not in KINDS:
            raise PeerRequestError("kind must be coordination, delegation or reply.")
        reply_to = args.get("reply_to")
        if reply_to is not None:
            reply_to = _uuid(reply_to, "reply_to")
        if (kind == "reply") != (reply_to is not None):
            raise PeerRequestError("reply_to is required for, and only for, kind=reply.")
        key = _text(args["idempotency_key"], "idempotency_key", 128)
        fingerprint = hashlib.sha256(json.dumps(
            {"to": to, "body": body, "kind": kind, "reply_to": reply_to},
            sort_keys=True, ensure_ascii=False,
        ).encode("utf-8")).hexdigest()
        previous = db.execute(
            "SELECT * FROM messages WHERE sender_thread_id = ? AND idempotency_key = ?",
            (me["thread_id"], key),
        ).fetchone()
        if previous is not None:
            if not hmac.compare_digest(previous["fingerprint"], fingerprint):
                raise PeerRequestError("idempotency_key was already used for a different message.", "conflict")
            return {"message": self._message(previous, me["thread_id"]), "duplicate": True}
        if to == me["thread_id"]:
            raise PeerRequestError("A thread cannot message itself.")
        recipient = self._row(db, to)
        if recipient is None or recipient["workspace"] != me["workspace"]:
            raise PeerRequestError("No such thread in this workspace.", "not_found")
        if recipient["status"] == "revoked":
            raise PeerRequestError("That thread's endpoint is revoked.", "revoked")
        if reply_to is not None:
            # A reply stays inside its own conversation: the original must be
            # between exactly this sender and this recipient, in either direction.
            original = db.execute(
                "SELECT 1 FROM messages WHERE message_id = ? AND ("
                "(sender_thread_id = ? AND recipient_thread_id = ?)"
                " OR (sender_thread_id = ? AND recipient_thread_id = ?))",
                (reply_to, me["thread_id"], to, to, me["thread_id"]),
            ).fetchone()
            if original is None:
                raise PeerRequestError(
                    "reply_to must be a message between this thread and the recipient.", "not_found"
                )
        pending = db.execute(
            "SELECT COUNT(*) FROM messages WHERE recipient_thread_id = ? AND status IN ('queued', 'presented')",
            (to,),
        ).fetchone()[0]
        if pending >= QUEUE_LIMIT:
            raise PeerRequestError("That thread's mailbox is full.", "queue_full")
        message_id = str(uuid.uuid4())
        db.execute(
            "INSERT INTO messages (message_id, workspace, sender_thread_id, recipient_thread_id,"
            " sender_snapshot, recipient_snapshot, sender_instance_id, kind, body, reply_to,"
            " idempotency_key, fingerprint, created_at, expires_at, status)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued')",
            (message_id, me["workspace"], me["thread_id"], to,
             json.dumps(self._snapshot(me), ensure_ascii=False),
             json.dumps(self._snapshot(recipient), ensure_ascii=False),
             me["instance_id"], kind, body, reply_to, key, fingerprint,
             now, now + MESSAGE_TTL_SECONDS),
        )
        row = db.execute("SELECT * FROM messages WHERE message_id = ?", (message_id,)).fetchone()
        return {"message": self._message(row, me["thread_id"]), "duplicate": False}

    def _tool_hub_read_messages(self, db, me, now, args):
        after = args.get("after", 0)
        if isinstance(after, bool) or not isinstance(after, int) or not 0 <= after <= SEQUENCE_MAX:
            raise PeerRequestError("after must be a non-negative integer.")
        limit = args.get("limit", READ_DEFAULT)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= READ_LIMIT:
            raise PeerRequestError(f"limit must be an integer from 1 to {READ_LIMIT}.")
        rows = db.execute(
            "SELECT * FROM messages WHERE recipient_thread_id = ? AND sequence > ?"
            " AND status IN ('queued', 'presented') ORDER BY sequence LIMIT ?",
            (me["thread_id"], after, limit + 1),
        ).fetchall()
        more, rows = len(rows) > limit, rows[:limit]
        ids = [row["message_id"] for row in rows if row["status"] == "queued"]
        db.executemany(
            "UPDATE messages SET status = 'presented', presented_at = ? WHERE message_id = ? AND status = 'queued'",
            [(now, message_id) for message_id in ids],
        )
        if ids:
            marks = ",".join("?" * len(rows))
            rows = db.execute(
                f"SELECT * FROM messages WHERE message_id IN ({marks}) ORDER BY sequence",
                [row["message_id"] for row in rows],
            ).fetchall()
        return {
            "messages": [self._message(row, me["thread_id"]) for row in rows],
            "next_after": rows[-1]["sequence"] if rows else after,
            "more": more,
        }

    def _visible(self, db, me, args):
        message_id = _uuid(args["message_id"], "message_id")
        row = db.execute("SELECT * FROM messages WHERE message_id = ?", (message_id,)).fetchone()
        if row is None or me["thread_id"] not in (row["sender_thread_id"], row["recipient_thread_id"]):
            raise PeerRequestError("No such message.", "not_found")
        return row

    def _tool_hub_ack_message(self, db, me, now, args):
        row = self._visible(db, me, args)
        if row["recipient_thread_id"] != me["thread_id"]:
            raise PeerRequestError("No such message.", "not_found")
        if row["status"] == "queued":
            raise PeerRequestError("Read a message before acknowledging it.", "not_presented")
        if row["status"] == "expired":
            raise PeerRequestError("That message has expired.", "expired")
        if row["status"] == "presented":
            db.execute(
                "UPDATE messages SET status = 'acknowledged', acknowledged_at = ? WHERE message_id = ?",
                (now, row["message_id"]),
            )
            row = db.execute("SELECT * FROM messages WHERE message_id = ?", (row["message_id"],)).fetchone()
        return {"message": self._message(row, me["thread_id"])}

    def _tool_hub_message_status(self, db, me, now, args):
        return {"message": self._message(self._visible(db, me, args), me["thread_id"])}
