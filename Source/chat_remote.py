"""Encrypted companion bridge for Provider Hub. Mac executes; the phone does not.

Crypto framing is TaskWraith ``taskwraith-e2ee-v1`` from AGBench
``src/shared/e2ee/{protocol,keys,keyschedule,cipher}.ts``. Golden outputs are
the 2026-06-11 cross-implementation vectors (also locked by TaskWraithKit).
This is that handshake and AES-256-GCM channel, not a new scheme, and it is
not the TaskWraith relay, push, or resolve stack.

Nothing in this module binds a socket on import. ``RemoteServer.start``
requires an explicit loopback, LAN or Tailscale interface IP. Provider
credentials are never written into a frame or a device record.

Root attaches the desktop host. This module does not import it::

    server = RemoteServer(host.handle, state_path, dispatch=enqueue_on_host_queue)
    server.start("192.168.1.10", 8765)
    pairing_text = server.bootstrap("192.168.1.10")
    server.publish(event)          # enqueue from host emit path
    pending = server.status()["pendingPairing"]
    server.confirm_pairing(pending["identity"], pending["confirmCode"], True)
    server.revoke(identity_b64)
    server.stop()

``host.handle`` sees ``send``, ``stop``, ``subscribe``, and ``approve``.
``approve`` binds both the exact pending chat and approval id. Both ends use
the session id from the bootstrap. Compare the six-digit code on both screens
before confirming an unknown phone. Trusted reconnects authenticate signatures.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import json
import os
import re
import socket
import threading
import ipaddress
import tempfile
from pathlib import Path
from collections import OrderedDict

from cryptography.exceptions import InvalidSignature, InvalidTag
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

E2EE_PROTOCOL = "taskwraith-e2ee-v1"
APP_PROTOCOL = "providerhub-remote-app-v1"
HKDF_INFO_MAC_TO_IPHONE = f"{E2EE_PROTOCOL} mac->iphone"
HKDF_INFO_IPHONE_TO_MAC = f"{E2EE_PROTOCOL} iphone->mac"
NONCE_PREFIX = {"mac->iphone": 0x00000001, "iphone->mac": 0x00000002}
CHAT_ID = re.compile(r"[0-9a-f]{32}\Z")
TOKEN = re.compile(r"[0-9a-f]{16,64}\Z")
SESSION_ID = re.compile(r"[A-Za-z0-9._:-]{1,128}\Z")
MAX_TEXT = 100_000
MAX_LOG = 200
MAX_REQUESTS = 64
DISPLAY_TEXT_LIMIT = 8000
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


class RemoteError(Exception):
    pass


def b64e(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def b64d(text, *, limit=200_000) -> bytes:
    if not isinstance(text, str) or not text or len(text) > limit:
        raise RemoteError("bad encoding")
    try:
        return base64.b64decode(text, validate=True)
    except Exception as exc:
        raise RemoteError("bad encoding") from exc


def _raw_public(key) -> bytes:
    return key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def x25519_public(private: X25519PrivateKey) -> bytes:
    return _raw_public(private.public_key())


def ed25519_public(private: Ed25519PrivateKey) -> bytes:
    return _raw_public(private.public_key())


def shared_secret(private: X25519PrivateKey, peer_raw: bytes) -> bytes:
    if len(peer_raw) != 32:
        raise RemoteError("bad peer key")
    secret = private.exchange(X25519PublicKey.from_public_bytes(peer_raw))
    if secret == bytes(32):
        raise RemoteError("invalid peer point")
    return secret


def transcript_hash(session_id, client_eph_b64, server_eph_b64, client_nonce_b64,
                    server_nonce_b64, mac_identity_b64, iphone_identity_b64) -> bytes:
    transcript = "|".join((
        E2EE_PROTOCOL, session_id, client_eph_b64, server_eph_b64,
        client_nonce_b64, server_nonce_b64, mac_identity_b64, iphone_identity_b64,
    ))
    return hashlib.sha256(transcript.encode("utf8")).digest()


def confirm_code(transcript: bytes) -> str:
    return f"{int.from_bytes(transcript[:4], 'big') % 1_000_000:06d}"


def derive_session_keys(private: X25519PrivateKey, peer_raw: bytes,
                        client_nonce: bytes, server_nonce: bytes) -> dict:
    ikm = shared_secret(private, peer_raw)
    salt = hashlib.sha256(client_nonce + server_nonce).digest()
    def expand(info):
        return HKDF(algorithm=hashes.SHA256(), length=32, salt=salt, info=info.encode("utf8")).derive(ikm)
    return {"mac->iphone": expand(HKDF_INFO_MAC_TO_IPHONE), "iphone->mac": expand(HKDF_INFO_IPHONE_TO_MAC)}


def build_nonce(direction: str, seq: int) -> bytes:
    if type(seq) is not int or seq < 0 or seq > 0xFFFFFFFFFFFFFFFF:
        raise RemoteError("bad sequence")
    return NONCE_PREFIX[direction].to_bytes(4, "big") + seq.to_bytes(8, "big")


def build_aad(session_id: str, seq: int) -> bytes:
    return f"{session_id}:{seq}".encode("utf8")


def seal(key: bytes, direction: str, session_id: str, seq: int, plaintext: bytes) -> dict:
    if len(key) != 32:
        raise RemoteError("bad key")
    nonce = build_nonce(direction, seq)
    blob = AESGCM(key).encrypt(nonce, plaintext, build_aad(session_id, seq))
    return {"nonce": nonce, "ct": blob[:-16], "tag": blob[-16:]}


def open_frame(key: bytes, direction: str, session_id: str, seq: int, frame: dict) -> bytes:
    if len(key) != 32:
        raise RemoteError("bad key")
    expected = build_nonce(direction, seq)
    nonce, ct, tag = frame.get("nonce"), frame.get("ct"), frame.get("tag")
    if nonce != expected:
        raise RemoteError("nonce mismatch")
    if not isinstance(tag, (bytes, bytearray)) or len(tag) != 16:
        raise RemoteError("bad tag")
    if not isinstance(ct, (bytes, bytearray)):
        raise RemoteError("bad ciphertext")
    try:
        return AESGCM(key).decrypt(expected, bytes(ct) + bytes(tag), build_aad(session_id, seq))
    except InvalidTag as exc:
        raise RemoteError("tampered frame") from exc


def sign_transcript(private: Ed25519PrivateKey, transcript: bytes) -> bytes:
    return private.sign(transcript)


def verify_transcript(public_raw: bytes, transcript: bytes, signature: bytes) -> bool:
    if len(public_raw) != 32 or len(signature) != 64:
        return False
    try:
        Ed25519PublicKey.from_public_bytes(public_raw).verify(signature, transcript)
        return True
    except InvalidSignature:
        return False


def _session_ok(value) -> str:
    if not isinstance(value, str) or not SESSION_ID.fullmatch(value):
        raise RemoteError("bad session")
    return value


def _obj(value):
    if not isinstance(value, dict):
        raise RemoteError("bad frame")
    return value


class DeviceRegistry:
    """Paired device identities; revoked identities need explicit reapproval."""

    def __init__(self):
        self._devices = {}

    def pair(self, identity_b64: str, label="iPhone", *, reauthorize=False):
        row = self._devices.get(identity_b64, {"label": label, "revoked": False})
        if row["revoked"] and not reauthorize:
            raise RemoteError("device revoked")
        row["label"] = label
        row["revoked"] = False
        self._devices[identity_b64] = row

    def revoke(self, identity_b64: str):
        row = self._devices.setdefault(identity_b64, {"label": "iPhone", "revoked": False})
        row["revoked"] = True

    def trusted(self, identity_b64: str) -> bool:
        row = self._devices.get(identity_b64)
        return bool(row and not row["revoked"])

    def revoked(self, identity_b64: str) -> bool:
        row = self._devices.get(identity_b64)
        return bool(row and row["revoked"])


class Session:
    """One E2EE role. Outbound frames stay in a queue; the caller moves bytes."""

    def __init__(self, role, identity: Ed25519PrivateKey, session_id, trust):
        if role not in {"mac", "iphone"}:
            raise RemoteError("bad role")
        self.role = role
        self.identity = identity
        self.session_id = _session_ok(session_id)
        self.trust = trust
        self.on_app = None
        self.outbox = []
        self.confirm = None
        self.peer_raw = None
        self.failed = None
        self._reset()

    def _reset(self):
        self.established = False
        self.keys = None
        self.send_seq = 0
        self.last_recv = -1
        self.ephemeral = None
        self.client_eph_b64 = ""
        self.server_eph_b64 = ""
        self.client_nonce = b""
        self.server_nonce = b""
        self.client_nonce_b64 = ""
        self.server_nonce_b64 = ""
        self.awaiting_auth = False
        self.transcript = None
        self.pending_auth = None

    def take(self):
        frames, self.outbox = self.outbox, []
        return frames

    def _send(self, frame):
        self.outbox.append(frame)

    def start(self):
        if self.role != "iphone":
            raise RemoteError("only the phone opens")
        self._reset()
        self.ephemeral = X25519PrivateKey.generate()
        nonce = os.urandom(16)
        self.client_eph_b64 = b64e(x25519_public(self.ephemeral))
        self.client_nonce, self.client_nonce_b64 = nonce, b64e(nonce)
        self._send({
            "t": "clientHello", "protocol": E2EE_PROTOCOL, "sessionId": self.session_id,
            "role": "iphone", "ephemeralPubKey": self.client_eph_b64, "nonce": self.client_nonce_b64,
        })

    def close(self):
        self._reset()
        self.keys = None

    def send_app(self, method, params, msg_id):
        if not self.established or not self.keys:
            raise RemoteError("not established")
        direction = "mac->iphone" if self.role == "mac" else "iphone->mac"
        body = {"v": 1, "proto": APP_PROTOCOL, "msgId": msg_id, "method": method, "params": params or {}}
        sealed = seal(self.keys[direction], direction, self.session_id, self.send_seq,
                      json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode("utf8"))
        frame = {
            "t": "enc", "sessionId": self.session_id, "seq": self.send_seq,
            "nonce": b64e(sealed["nonce"]), "ct": b64e(sealed["ct"]), "tag": b64e(sealed["tag"]),
        }
        self.send_seq += 1
        self._send(frame)

    def feed(self, frame):
        frame = _obj(frame)
        kind = frame.get("t")
        try:
            if kind == "clientHello":
                self._client_hello(frame)
            elif kind == "serverHello":
                self._server_hello(frame)
            elif kind == "clientAuth":
                self._client_auth(frame)
            elif kind == "serverAuth":
                self._server_auth(frame)
            elif kind == "enc":
                self._encrypted(frame)
            else:
                raise RemoteError("unknown frame")
        except RemoteError as exc:
            self.failed = str(exc)
            if kind == "enc":
                self.close()
            raise

    def _require_session(self, frame):
        if frame.get("protocol", E2EE_PROTOCOL) != E2EE_PROTOCOL:
            raise RemoteError("protocol mismatch")
        if frame.get("sessionId") != self.session_id:
            raise RemoteError("session mismatch")

    def _client_hello(self, frame):
        if self.role != "mac":
            raise RemoteError("unexpected hello")
        self._require_session(frame)
        if frame.get("role") != "iphone":
            raise RemoteError("unexpected role")
        if self.awaiting_auth and not self.peer_raw:
            raise RemoteError("handshake already in flight")
        pinned = self.peer_raw
        self._reset()
        self.peer_raw = pinned
        self.client_eph_b64 = frame.get("ephemeralPubKey")
        self.client_nonce_b64 = frame.get("nonce")
        self.client_nonce = b64d(self.client_nonce_b64, limit=64)
        if len(self.client_nonce) != 16 or len(b64d(self.client_eph_b64, limit=80)) != 32:
            raise RemoteError("bad hello")
        self.ephemeral = X25519PrivateKey.generate()
        server_nonce = os.urandom(16)
        self.server_eph_b64 = b64e(x25519_public(self.ephemeral))
        self.server_nonce, self.server_nonce_b64 = server_nonce, b64e(server_nonce)
        self.keys = derive_session_keys(
            self.ephemeral, b64d(self.client_eph_b64, limit=80), self.client_nonce, self.server_nonce)
        self.awaiting_auth = True
        self._send({
            "t": "serverHello", "protocol": E2EE_PROTOCOL, "sessionId": self.session_id,
            "ephemeralPubKey": self.server_eph_b64, "nonce": self.server_nonce_b64,
            "macIdentityPubKey": b64e(ed25519_public(self.identity)),
        })

    def _server_hello(self, frame):
        if self.role != "iphone" or not self.ephemeral:
            raise RemoteError("unexpected server hello")
        self._require_session(frame)
        self.server_eph_b64 = frame.get("ephemeralPubKey")
        self.server_nonce_b64 = frame.get("nonce")
        self.server_nonce = b64d(self.server_nonce_b64, limit=64)
        mac_raw = b64d(frame.get("macIdentityPubKey"), limit=80)
        if len(self.server_nonce) != 16 or len(b64d(self.server_eph_b64, limit=80)) != 32 or len(mac_raw) != 32:
            raise RemoteError("bad server hello")
        if self.peer_raw and self.peer_raw != mac_raw:
            raise RemoteError("Mac identity changed")
        self.peer_raw = mac_raw
        self.keys = derive_session_keys(
            self.ephemeral, b64d(self.server_eph_b64, limit=80), self.client_nonce, self.server_nonce)
        self.transcript = transcript_hash(
            self.session_id, self.client_eph_b64, self.server_eph_b64,
            self.client_nonce_b64, self.server_nonce_b64,
            b64e(mac_raw), b64e(ed25519_public(self.identity)))
        self.confirm = confirm_code(self.transcript)
        signature = sign_transcript(self.identity, self.transcript)
        self._send({
            "t": "clientAuth", "sessionId": self.session_id,
            "iphoneIdentityPubKey": b64e(ed25519_public(self.identity)),
            "confirmCode": self.confirm, "transcriptSig": b64e(signature),
        })

    def _client_auth(self, frame):
        if self.role != "mac" or not self.awaiting_auth or not self.keys:
            raise RemoteError("unexpected client auth")
        self.awaiting_auth = False
        if frame.get("sessionId") != self.session_id:
            raise RemoteError("session mismatch")
        phone_raw = b64d(frame.get("iphoneIdentityPubKey"), limit=80)
        signature = b64d(frame.get("transcriptSig"), limit=120)
        if len(phone_raw) != 32:
            raise RemoteError("bad phone identity")
        self.transcript = transcript_hash(
            self.session_id, self.client_eph_b64, self.server_eph_b64,
            self.client_nonce_b64, self.server_nonce_b64,
            b64e(ed25519_public(self.identity)), b64e(phone_raw))
        self.confirm = confirm_code(self.transcript)
        if not verify_transcript(phone_raw, self.transcript, signature):
            raise RemoteError("bad transcript signature")
        if frame.get("confirmCode") != self.confirm:
            raise RemoteError("confirm code mismatch")
        if self.peer_raw and self.peer_raw != phone_raw:
            raise RemoteError("phone identity changed")
        trusted = self.trust(phone_raw, self.confirm)
        if trusted is None:
            self.pending_auth = frame
            return
        if not trusted:
            raise RemoteError("pairing rejected")
        self.peer_raw = phone_raw
        self._send({
            "t": "serverAuth", "sessionId": self.session_id,
            "transcriptSig": b64e(sign_transcript(self.identity, self.transcript)),
        })
        self.established = True

    def _server_auth(self, frame):
        if self.role != "iphone" or not self.transcript or not self.peer_raw:
            raise RemoteError("unexpected server auth")
        if frame.get("sessionId") != self.session_id:
            raise RemoteError("session mismatch")
        signature = b64d(frame.get("transcriptSig"), limit=120)
        if not verify_transcript(self.peer_raw, self.transcript, signature):
            raise RemoteError("bad Mac signature")
        self.established = True

    def _encrypted(self, frame):
        if not self.established or not self.keys:
            raise RemoteError("encrypted frame before session")
        if frame.get("sessionId") != self.session_id:
            raise RemoteError("session mismatch")
        seq = frame.get("seq")
        if type(seq) is not int or seq <= self.last_recv:
            return
        direction = "iphone->mac" if self.role == "mac" else "mac->iphone"
        plaintext = open_frame(self.keys[direction], direction, self.session_id, seq, {
            "nonce": b64d(frame.get("nonce"), limit=64),
            "ct": b64d(frame.get("ct"), limit=300_000),
            "tag": b64d(frame.get("tag"), limit=64),
        })
        self.last_recv = seq
        if len(plaintext) > 196608:
            raise RemoteError("frame too large")
        try:
            message = json.loads(plaintext.decode("utf8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RemoteError("bad app payload") from exc
        if not isinstance(message, dict) or message.get("proto") != APP_PROTOCOL or message.get("v") != 1:
            raise RemoteError("bad app protocol")
        if not isinstance(message.get("method"), str):
            raise RemoteError("bad method")
        if self.on_app:
            self.on_app(message)


def _integral(value):
    if type(value) is int:
        return value
    if type(value) is float and value.is_integer():
        return int(value)
    return None


def mention_chips(raw):
    """Chips the composer drew. A bad record is refused before the host sees it."""
    if not isinstance(raw, list) or len(raw) > 72:
        raise ValueError("invalid mentions")
    chips = []
    for chip in raw:
        if not isinstance(chip, dict):
            raise ValueError("invalid mentions")
        identifier, name, route = chip.get("id"), chip.get("name"), chip.get("route")
        start, length = _integral(chip.get("start")), _integral(chip.get("length"))
        if (not isinstance(identifier, str) or not identifier or len(identifier) > 128
                or not isinstance(name, str) or not name or len(name) > 120
                or not isinstance(route, str) or len(route) > 160
                or start is None or length is None or start < 0 or start > MAX_TEXT * 2
                or length < 2 or length > 121):
            raise ValueError("invalid mentions")
        chips.append({"id": identifier, "name": name, "route": route, "start": start, "length": length})
    return chips


def project_mentions(entry):
    """Keep well-formed chips on the phone transcript and drop the rest."""
    raw = entry.get("mentions") or []
    if not isinstance(raw, list):
        return []
    kept = []
    for chip in raw[:72]:
        try:
            kept.extend(mention_chips([chip]))
        except ValueError:
            continue
    return kept


def project_entry(entry, fallback="legacy"):
    """Visible transcript data only; omit provider reasoning and private metadata."""
    row = {"id": str(entry.get("id") or fallback)[:128],
            "role": str(entry.get("role") or entry.get("kind") or "note")[:40],
            "text": str(entry.get("text") or "")[:DISPLAY_TEXT_LIMIT],
            "summary": str(entry.get("summary") or "")[:500],
            "detail": str(entry.get("detail") or "")[:2000],
            "tool": str(entry.get("tool") or "")[:100],
            "memberName": str(entry.get("memberName") or "")[:100],
            "isError": entry.get("isError") is True,
            "isLive": (entry.get("role") or entry.get("kind")) == "assistant" and entry.get("recorded") is False}
    row.update(route=str(entry.get("route") or "")[:160], memberID=str(entry.get("memberID") or "")[:128],
               contributionID=str(entry.get("contributionID") or "")[:128],
               agentID=str(entry.get("agentID") or "")[:128],
               changedFiles=[str(path)[:300] for path in (entry.get("changedFiles") or [])[:32] if isinstance(path, str)])
    attachments = [{"id": str(attachment["id"])[:128], "name": str(attachment["name"])[:240],
                    "kind": str(attachment.get("kind") or "file")[:20], "size": max(0, attachment.get("size", 0)) if type(attachment.get("size", 0)) is int else 0}
                   for attachment in (entry.get("attachments") or [])[:8] if isinstance(attachment, dict) and attachment.get("id") and attachment.get("name")]
    if attachments: row["attachments"] = attachments
    mentions = project_mentions(entry)
    if mentions: row["mentions"] = mentions
    timestamp = entry.get("time")
    if type(timestamp) in (int, float) and 0 <= timestamp < 10_000_000_000: row["time"] = timestamp
    question = entry.get("question")
    if row["role"] == "question" and isinstance(question, dict):
        from chat_questions import validate
        try:
            projected = {"id": question["id"], "status": question["status"], "mode": question["mode"],
                         "asker": str(question.get("asker", "Assistant"))[:120],
                         "questions": validate("request_user_input", {"questions":question["questions"]}),
                         "answers": question.get("answers", {})}
            if len(json.dumps(projected, ensure_ascii=False).encode("utf8")) <= 40000:
                row.update(question=projected, text="")
        except (ValueError, KeyError, TypeError): pass
    return row


def bound_entries(entries):
    kept, size = [], 0
    for row in reversed(entries[-30:]):
        length = len(json.dumps(row, ensure_ascii=False).encode("utf8"))
        if size + length > 48000: break
        kept.append(row); size += length
    return list(reversed(kept))


def project_event(event):
    """Whitelist presentation events; provider credentials never cross the wire."""
    if not isinstance(event, dict):
        return None
    kind = event.get("event")
    if kind == "chats" and isinstance(event.get("chats"), list):
        rows, size = [], 0
        for row in event["chats"][:256]:
            if not isinstance(row, dict) or not isinstance(row.get("id"), str):
                continue
            projected = {
                "id": row["id"],
                "title": str(row.get("title") or "Chat")[:120],
                "status": str(row.get("status") or "")[:80],
                "busy": bool(row.get("busy")),
                "active": bool(row.get("active")),
                "pendingApproval": bool(row.get("pendingApproval")),
                "modelLabel": str(row.get("modelLabel") or "")[:120],
                "providerLabel": str(row.get("providerLabel") or "")[:60],
                "accent": row.get("accent") if isinstance(row.get("accent"), str) and re.fullmatch(r"#[0-9A-Fa-f]{6}", row["accent"]) else "",
                "approvalMode": row.get("approvalMode") if row.get("approvalMode") in {"manual", "accept_edits", "yolo"} else "manual",
                "route": str(row.get("route") or "")[:160],
            }
            length = len(json.dumps(projected, ensure_ascii=False).encode("utf8"))
            if size + length > 32000: break
            rows.append(projected); size += length
        return {"event": "chats", "chats": rows}
    if kind == "approval":
        approval = event.get("approval")
        if approval is None:
            return {"event": "approval", "approval": None, "chat": event.get("chat")}
        if not isinstance(approval, dict) or not isinstance(approval.get("id"), str):
            return None
        return {"event": "approval", "chat": event.get("chat"), "approval": {
            "id": approval["id"],
            "summary": str(approval.get("summary") or "")[:500],
            "detail": str(approval.get("detail") or "")[:2000],
        }}
    if kind == "delta" and isinstance(event.get("text"), str):
        # Offsets address the bounded prefix shown on the phone. Continuing a
        # long reply must not create a permanent gap or a snapshot request loop.
        offset = max(0, min(DISPLAY_TEXT_LIMIT, event.get("offset", 0))) if type(event.get("offset")) is int else 0
        return {"event": "delta", "chat": event.get("chat"), "id": event.get("id"),
                "text": event["text"][:DISPLAY_TEXT_LIMIT - offset], "offset": offset}
    if kind == "entry" and isinstance(event.get("entry"), dict):
        entry = event["entry"]
        return {"event": "entry", "chat": event.get("chat"), "entry": project_entry(entry)}
    if kind == "state":
        return {"event": "state", "chat": event.get("chat"), "busy": event.get("busy") is True,
                "interrupting": event.get("interrupting") is True, "status": str(event.get("status") or "")[:160]}
    if kind in {"selected", "snapshot"}:
        entries = []
        for index, entry in enumerate((event.get("entries") or [])[-30:]):
            if isinstance(entry, dict):
                entries.append(project_entry(entry, "snapshot-" + str(index)))
        bounded_entries = bound_entries(entries)
        return {"event": kind, "chat": event.get("chat") or event.get("id"), "id": event.get("id"),
                "entries": bounded_entries, "hasMore": len(event.get("entries") or []) > len(bounded_entries),
                "busy": bool(event.get("busy")), "interrupting": event.get("interrupting") is True,
                "status": str(event.get("status") or "")[:160]}
    if kind in {"notice", "rejected", "error"} and isinstance(event.get("message"), str):
        return {"event": kind, "chat": event.get("chat"), "message": event["message"][:500]}
    from chat_remote_controls import project
    return project(event, project_entry)


class MacBridge:
    """Authoritative Mac side. ``execute`` is ``ChatHost.handle`` or a test double."""

    def __init__(self, execute, session_id, *, identity=None, auto_accept=False, retain=MAX_LOG):
        self.execute = execute
        self.identity = identity or Ed25519PrivateKey.generate()
        self.registry = DeviceRegistry()
        self.auto_accept = auto_accept
        self.session = Session("mac", self.identity, session_id, self._trust)
        self.session.on_app = self._on_app
        # Extension 5: the phone may steer a running turn with chat.steer.
        self.view = {"chats": [], "selected": None, "approval": None, "controls": {"version": 1, "extensionVersion": 5}}
        self.pending_pairing = None
        self.hold_pairing = False
        self.selected_chat = None
        self.cursor = 0
        self.log = []
        self.retain = retain
        self._requests = {}
        self._decisions = {}
        self._msg = 0
        self._peer_msg = 0
        self.client_id = "remote"
        self.device_name = "iPhone / iPad"

    @property
    def established(self):
        return self.session.established

    @property
    def confirm(self):
        return self.session.confirm

    @property
    def peer(self):
        return b64e(self.session.peer_raw) if self.session.peer_raw else None

    def identity_b64(self):
        return b64e(ed25519_public(self.identity))

    def take(self):
        return self.session.take()

    def feed(self, frame):
        if isinstance(frame, dict) and frame.get("t") == "clientHello":
            self._peer_msg = 0
            self.pending_pairing = None
        if isinstance(frame, dict) and frame.get("t") == "clientAuth":
            label = frame.get("deviceName")
            if isinstance(label, str) and label.strip():
                self.device_name = label.strip()[:60]
        peer = self.peer
        if peer and self.registry.revoked(peer):
            self.session.close()
            raise RemoteError("device revoked")
        self.session.feed(frame)

    def revoke(self, identity_b64: str):
        self.registry.revoke(identity_b64)
        if self.peer == identity_b64:
            self.session.close()

    def publish(self, event):
        if (self.selected_chat and event.get("chat") and event.get("chat") != self.selected_chat
                and event.get("event") not in {"chats", "catalogue", "plugins"}):
            return None
        if (self.selected_chat and event.get("event") in {"selected", "snapshot"}
                and (event.get("chat") or event.get("id")) != self.selected_chat):
            return None
        projected = project_event(event)
        if projected is None:
            return None
        self._apply_view(projected)
        self.cursor += 1
        self.log.append((self.cursor, projected))
        if len(self.log) > self.retain:
            self.log = self.log[-self.retain:]
        if self.established and not (self.peer and self.registry.revoked(self.peer)):
            self._emit("session.delta", {"cursor": self.cursor, "event": projected})
        return projected

    def _trust(self, peer_raw, code):
        identity = b64e(peer_raw)
        if self.registry.revoked(identity):
            # Removal stops all access immediately. A fresh signed handshake
            # may request reapproval, including from an older companion that
            # still holds its device-only Keychain identity.
            if self.hold_pairing:
                self.pending_pairing = {"identity": identity, "confirmCode": code}
                return None
            return False
        if self.registry.trusted(identity):
            return True
        if not self.auto_accept:
            if self.hold_pairing:
                self.pending_pairing = {"identity": identity, "confirmCode": code}
                return None
            return False
        self.registry.pair(identity)
        return True

    def _emit(self, method, params):
        self._msg += 1
        self.session.send_app(method, params, self._msg)

    def _apply_view(self, projected):
        kind = projected["event"]
        if kind == "control":
            self.view["controls"][projected["panel"]] = projected["value"]
            self.view["controls"]["version"] = 1
            if projected["panel"] == "activity" and self.view.get("selected") and self.view["selected"].get("id") == projected.get("chat"):
                for key in ("busy", "interrupting", "status"):
                    self.view["selected"][key] = projected["value"].get(key)
        elif kind == "state" and self.view.get("selected") and self.view["selected"].get("id") == projected.get("chat"):
            for key in ("busy", "interrupting", "status"):
                self.view["selected"][key] = projected.get(key)
        elif kind == "side_delta":
            side = self.view["controls"].get("side")
            if isinstance(side, dict) and side.get("id") == projected.get("side"):
                rows = side.setdefault("entries", [])
                identifier = projected.get("id") or "side-live"
                row = next((row for row in rows if row.get("id") == identifier), None)
                if row is None:
                    row = {"id": identifier, "role": "assistant", "text": ""}; rows.append(row)
                offset, text = projected["offset"], row.get("text", "")
                if offset <= len(text): row["text"] = (text + projected["text"][max(0, len(text) - offset):])[:4000]
                from chat_remote_controls import bounded
                side["entries"] = bounded(rows[-12:], 7000)
        elif kind == "chats":
            self.view["chats"] = projected["chats"]
        elif kind == "approval":
            self.view["approval"] = projected.get("approval")
            if self.view["approval"]:
                self.view["approval"]["chat"] = projected.get("chat")
        elif kind in {"selected", "snapshot"}:
            self.view["selected"] = {
                "id": projected.get("id") or projected.get("chat"),
                "entries": projected.get("entries") or [],
                "busy": projected.get("busy"),
                "interrupting": projected.get("interrupting", False),
                "status": projected.get("status") or "",
                "live": "",
                "liveID": None,
                "hasMore": projected.get("hasMore", False),
            }
        elif kind == "delta" and self.view.get("selected") and self.view["selected"].get("id") == projected.get("chat"):
            selected = self.view["selected"]
            identifier = projected.get("id") or "live"
            if selected.get("liveID") == identifier:
                live = selected.get("live") or ""
            else:
                live = next((row["text"] for row in selected.get("entries", []) if row.get("id") == identifier), "")
            offset = projected.get("offset", len(live))
            addition = projected.get("text", "")
            overlap = max(0, len(live) - offset)
            selected["live"] = (live + addition[overlap:])[:DISPLAY_TEXT_LIMIT]
            selected["liveID"] = identifier
        elif kind == "entry" and self.view.get("selected") and self.view["selected"].get("id") == projected.get("chat"):
            rows = self.view["selected"].setdefault("entries", [])
            updated = projected["entry"]
            index = next((i for i, row in enumerate(rows) if row.get("id") == updated["id"] and updated["id"] != "legacy"), None)
            if index is None: rows.append(updated)
            else: rows[index] = updated
            kept = bound_entries(rows)
            self.view["selected"]["hasMore"] = self.view["selected"].get("hasMore", False) or len(rows) > len(kept)
            self.view["selected"]["entries"] = kept
            if projected["entry"].get("role") == "assistant":
                self.view["selected"]["live"] = ""
                self.view["selected"]["liveID"] = None

    def _snapshot(self):
        # Refresh images on demand; snapshots prioritize text, control state and
        # chat history within the fixed encrypted frame limit.
        controls = {k: v for k, v in self.view["controls"].items() if k not in {"browserPreview", "blackboard", "options", "turnChanges"}}
        if len(json.dumps(controls, ensure_ascii=False).encode()) > 80000: controls.pop("team", None)
        return {"cursor": self.cursor, "chats": self.view["chats"], "approval": self.view["approval"],
                "selected": self.view["selected"], "controls": controls}

    def _on_app(self, message):
        msg_id = message.get("msgId")
        if type(msg_id) is not int or msg_id < 1:
            raise RemoteError("bad message id")
        if msg_id <= self._peer_msg:
            return
        self._peer_msg = msg_id
        method, params = message.get("method"), message.get("params") or {}
        if not isinstance(params, dict):
            raise RemoteError("bad params")
        peer = self.peer
        if not peer or not self.registry.trusted(peer):
            raise RemoteError("device not trusted")
        if method == "session.resume":
            after = params.get("afterCursor")
            if type(after) is int and self.log and after >= self.log[0][0]:
                for cursor, event in self.log:
                    if cursor > after:
                        self._emit("session.delta", {"cursor": cursor, "event": event})
            else:
                self._emit("session.snapshot", self._snapshot())
            return
        if method not in {"chat.send", "chat.steer", "chat.stop", "chat.approve", "chat.select", "chat.answer_question", "chat.control"}:
            self._ack(params.get("requestId"), False, "unsupported command")
            return
        request_id = params.get("requestId")
        if not isinstance(request_id, str) or not request_id or len(request_id) > 128:
            raise RemoteError("request id required")
        cache = self._requests.setdefault(peer, OrderedDict())
        if request_id in cache:
            self._emit("session.ack", cache[request_id])
            return
        try:
            ack = self._command(peer, method, params, request_id)
        except RemoteError as exc:
            ack = {"requestId": request_id, "ok": False, "error": str(exc)}
        cache[request_id] = ack
        while len(cache) > MAX_REQUESTS:
            cache.popitem(last=False)
        self._emit("session.ack", ack)

    def _ack(self, request_id, ok, error=None):
        if not isinstance(request_id, str):
            return
        body = {"requestId": request_id, "ok": ok}
        if error:
            body["error"] = error
        self._emit("session.ack", body)

    def _command(self, peer, method, params, request_id):
        if method == "chat.control":
            from chat_remote_controls import command
            try: control = command(params, request_id)
            except (ValueError, TypeError) as exc: raise RemoteError(str(exc)) from exc
            action = control["command"]
            if action not in {"remote_create", "remote_options", "remote_models"} and params.get("chat") != self.selected_chat: raise RemoteError("Select this chat before using its controls")
            result = self._execute(control) or {}
            if action == "remote_create":
                created = result.get("chat")
                if not isinstance(created, str) or not CHAT_ID.fullmatch(created): raise RemoteError("Chat creation did not return a chat")
                self.selected_chat = created
                self.view["controls"] = {key: value for key, value in self.view["controls"].items() if key in {"version", "extensionVersion", "models", "browserEnabled", "options"}}
                self.view["approval"] = None
                self._execute({"command": "subscribe", "client": self.client_id, "chats": [created]})
            return {"requestId": request_id, "ok": True, "result": result}
        if method == "chat.approve":
            return self._approve(peer, params, request_id)
        chat = params.get("chat") if method != "chat.select" else params.get("chat")
        if method == "chat.select":
            chat = params.get("chat")
        if not isinstance(chat, str) or not CHAT_ID.fullmatch(chat):
            raise RemoteError("chat id required")
        if method in {"chat.send", "chat.steer"}:
            text = params.get("text")
            if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT:
                raise RemoteError("message required")
            # A steer is the Mac composer's update: it interrupts the running
            # turn with this message, or is an ordinary send if it has ended.
            command = {"command": "steer" if method == "chat.steer" else "send", "chat": chat, "id": chat, "text": text, "request": request_id}
            if "mentions" in params:
                try:
                    command["mentions"] = mention_chips(params.get("mentions"))
                except ValueError as exc:
                    raise RemoteError(str(exc)) from exc
            self._execute(command)
        elif method == "chat.stop":
            self._execute({"command": "stop", "chat": chat, "id": chat})
        elif method == "chat.answer_question":
            identifier = params.get("questionId")
            if not isinstance(identifier, str) or not TOKEN.fullmatch(identifier): raise RemoteError("question id required")
            self._execute({"command": "answer_question", "chat": chat, "question_id": identifier,
                           "answers": params.get("answers"), "dismiss": params.get("dismiss") is True})
        elif method == "chat.select":
            if self.selected_chat != chat:
                self.view["controls"] = {key: value for key, value in self.view["controls"].items() if key in {"version", "extensionVersion", "models", "browserEnabled", "options"}}
                if self.view["approval"] and self.view["approval"].get("chat") != chat:
                    self.view["approval"] = None
            self.selected_chat = chat
            self._execute({"command": "subscribe", "client": self.client_id, "chats": [chat]})
        else:
            raise RemoteError("unsupported command")
        return {"requestId": request_id, "ok": True}

    def _execute(self, command):
        try:
            return self.execute(command)
        except RemoteError:
            raise
        except Exception as exc:
            raise RemoteError(str(exc)[:300]) from exc

    def _approve(self, peer, params, request_id):
        approval_id = params.get("approvalId")
        chat = params.get("chat")
        allow = params.get("allow")
        if not isinstance(approval_id, str) or not TOKEN.fullmatch(approval_id):
            raise RemoteError("approval id required")
        if not isinstance(chat, str) or not CHAT_ID.fullmatch(chat):
            raise RemoteError("chat id required")
        if allow is not True and allow is not False:
            raise RemoteError("approval must be exactly true or false")
        prior = self._decisions.get((chat, approval_id))
        if prior:
            return {"requestId": request_id, "ok": True, "allow": prior["allow"], "duplicate": True}
        pending = self.view.get("approval")
        if not pending or pending.get("id") != approval_id or pending.get("chat") != chat:
            raise RemoteError("This approval is no longer pending.")
        self._execute({"command": "approve", "chat": chat, "id": approval_id, "allow": allow})
        self._decisions[(chat, approval_id)] = {"device": peer, "allow": allow, "requestId": request_id}
        return {"requestId": request_id, "ok": True, "allow": allow}


class PhoneClient:
    """Thin companion state machine. It stores no provider credentials."""

    def __init__(self, session_id, *, identity=None):
        self.identity = identity or Ed25519PrivateKey.generate()
        self.session = Session("iphone", self.identity, session_id, lambda *_: True)
        self.session.on_app = self._on_app
        self.cursor = 0
        self.chats = []
        self.approval = None
        self.selected = None
        self.notices = []
        self.acks = {}
        self.needs_snapshot = False
        self._msg = 0
        self._seen = 0

    @property
    def established(self):
        return self.session.established

    @property
    def confirm(self):
        return self.session.confirm

    def identity_b64(self):
        return b64e(ed25519_public(self.identity))

    def take(self):
        return self.session.take()

    def feed(self, frame):
        before = self.session.established
        self.session.feed(frame)
        if self.session.established and not before:
            self.resume()

    def start(self):
        self._seen = 0
        self.session.start()

    def resume(self):
        self._send("session.resume", {"afterCursor": self.cursor})

    def send_text(self, chat, text, request_id, mentions=None):
        params = {"chat": chat, "text": text, "requestId": request_id}
        if mentions is not None:
            params["mentions"] = mentions
        self._send("chat.send", params)

    def steer_text(self, chat, text, request_id, mentions=None):
        params = {"chat": chat, "text": text, "requestId": request_id}
        if mentions is not None:
            params["mentions"] = mentions
        self._send("chat.steer", params)

    def stop(self, chat, request_id):
        self._send("chat.stop", {"chat": chat, "requestId": request_id})

    def approve(self, chat, approval_id, allow, request_id):
        self._send("chat.approve", {
            "chat": chat, "approvalId": approval_id, "allow": allow, "requestId": request_id,
        })

    def select(self, chat, request_id):
        self._send("chat.select", {"chat": chat, "requestId": request_id})

    def _send(self, method, params):
        self._msg += 1
        self.session.send_app(method, params, self._msg)

    def _on_app(self, message):
        msg_id = message.get("msgId")
        if type(msg_id) is not int or msg_id <= self._seen:
            return
        self._seen = msg_id
        method, params = message["method"], message.get("params") or {}
        if method == "session.snapshot":
            self.cursor = params.get("cursor") or 0
            self.chats = params.get("chats") or []
            self.approval = params.get("approval")
            self.selected = params.get("selected")
            self.needs_snapshot = False
        elif method == "session.delta":
            cursor, event = params.get("cursor"), params.get("event")
            if type(cursor) is not int or not isinstance(event, dict):
                return
            if cursor <= self.cursor:
                return
            if cursor != self.cursor + 1:
                self.needs_snapshot = True
                return
            self.cursor = cursor
            self._apply(event)
        elif method == "session.ack":
            request_id = params.get("requestId")
            if isinstance(request_id, str):
                self.acks[request_id] = params

    def _apply(self, event):
        kind = event.get("event")
        if kind == "chats":
            self.chats = event.get("chats") or []
        elif kind == "approval":
            self.approval = event.get("approval")
        elif kind in {"selected", "snapshot"}:
            self.selected = event
        elif kind == "delta" and isinstance(self.selected, dict):
            live = self.selected.get("live") or ""
            self.selected["live"] = live + (event.get("text") or "")
        elif kind in {"notice", "rejected", "error"}:
            self.notices.append(event.get("message") or "")


def exchange(mac: MacBridge, phone: PhoneClient, rounds=12):
    for _ in range(rounds):
        moved = False
        for frame in phone.take():
            mac.feed(frame)
            moved = True
        for frame in mac.take():
            phone.feed(frame)
            moved = True
        if not moved:
            return


def validate_bind_host(host: str) -> str:
    if host not in LOOPBACK_HOSTS:
        raise ValueError("remote bridge refuses a non-loopback bind")
    return "::1" if host == "::1" else "127.0.0.1"


class LoopbackAdapter:
    """Explicit loopback byte pipe. Construction does not listen."""

    def __init__(self):
        self._server = None
        self.port = None
        self._lock = threading.Lock()

    def start(self, host="127.0.0.1", port=0):
        bound = validate_bind_host(host)
        if type(port) is not int or port < 0 or port > 65535:
            raise ValueError("bad port")
        with self._lock:
            if self._server is not None:
                raise RuntimeError("already listening")
            server = socket.socket(socket.AF_INET6 if bound == "::1" else socket.AF_INET, socket.SOCK_STREAM)
            try:
                if bound == "::1":
                    server.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
                server.bind((bound, port))
                server.listen(1)
                server.settimeout(2)
                self.port = server.getsockname()[1]
                self._server = server
            except Exception:
                server.close()
                raise
        return self.port

    def accept(self, timeout=2):
        if self._server is None:
            raise RuntimeError("not listening")
        self._server.settimeout(timeout)
        conn, addr = self._server.accept()
        peer = addr[0]
        if peer not in {"127.0.0.1", "::1"}:
            conn.close()
            raise RemoteError("refusing non-loopback peer")
        return conn

    def stop(self):
        with self._lock:
            server, self._server = self._server, None
        if server is not None:
            server.close()


def encode_line(frame: dict) -> bytes:
    return json.dumps(frame, separators=(",", ":")).encode("utf8") + b"\n"


class RemoteServer:
    """Explicit private TCP listener; newline-delimited TaskWraith E2EE frames.

    dispatch must enqueue a callable on the desktop's serial command queue.
    Each device has its own encrypted channel, selection and event cursor.
    The listener never owns provider credentials.
    """

    def __init__(self, execute, state_path, session_id="providerhub-phone", *, dispatch, detach=None):
        self.path = Path(state_path)
        self.dispatch = dispatch
        self.detach = detach
        self.lock = threading.RLock()
        self.listener = None
        self.connections = {}
        self._bridges = ()
        self.port = None
        self.error = None
        if self.path.is_symlink():
            raise RemoteError("remote identity state must not be a symlink")
        if self.path.exists():
            fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(fd, "r") as stream:
                state = json.loads(stream.read(65536))
            identity = Ed25519PrivateKey.from_private_bytes(b64d(state["identity"]))
        else:
            state = {"devices": {}}
            identity = Ed25519PrivateKey.generate()
        self.bridge = MacBridge(execute, session_id, identity=identity)
        self.bridge.hold_pairing = True
        self.bridge.registry._devices = state.get("devices", {})
        self._save()

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        raw = self.bridge.identity.private_bytes(serialization.Encoding.Raw,
            serialization.PrivateFormat.Raw, serialization.NoEncryption())
        data = json.dumps({"identity": b64e(raw), "devices": self.bridge.registry._devices})
        fd, temporary_name = tempfile.mkstemp(prefix=".remote-", dir=self.path.parent)
        temporary = Path(temporary_name)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise

    def start(self, host, port=8765):
        address = ipaddress.ip_address(host)
        # Explicit interfaces only. Never wildcard, public, multicast or link-local.
        allowed = address.is_loopback or (address.version == 4 and
            any(address in ipaddress.ip_network(net) for net in
                ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10"))) or (
            address.version == 6 and address in ipaddress.ip_network("fd7a:115c:a1e0::/48"))
        if not allowed:
            raise ValueError("choose an explicit LAN, Tailscale or loopback IP")
        if type(port) is not int or not 0 <= port <= 65535:
            raise ValueError("bad port")
        with self.lock:
            if self.listener:
                raise RemoteError("already listening")
            listener = socket.socket(socket.AF_INET6 if address.version == 6 else socket.AF_INET)
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                listener.bind((host, port))
                listener.listen(8)
                listener.settimeout(0.5)
            except Exception:
                listener.close()
                raise
            self.listener = listener
            self.port = listener.getsockname()[1]
            threading.Thread(target=self._listen, args=(listener,), daemon=True).start()
            return self.port

    def bootstrap(self, host):
        if not self.listener:
            raise RemoteError("not listening")
        return {"host": host, "port": self.port, "sessionId": self.bridge.session.session_id,
                "macIdentityPubKey": self.bridge.identity_b64()}

    def status(self):
        with self.lock:
            connected = {bridge.peer for bridge in self.connections.values() if bridge.established}
            pending = [bridge.pending_pairing for bridge in self.connections.values() if bridge.pending_pairing]
            return {"listening": self.listener is not None, "port": self.port,
                    "connected": bool(connected), "connectedCount": len(connected),
                    "pendingPairing": pending[0] if pending else None, "pendingPairings": pending, "error": self.error,
                    "devices": [{"identity": identity, **row, "connected": identity in connected}
                                for identity, row in self.bridge.registry._devices.items()]}

    @property
    def selections(self):
        # Host event producers read this while holding chat/output locks.
        # Read an immutable membership snapshot without taking a network lock;
        # selections themselves change only on the serial command owner.
        return {bridge.client_id: bridge.selected_chat for bridge in self._bridges if bridge.selected_chat}

    def replace_chat(self, old, new):
        with self.lock:
            for bridge in self.connections.values():
                if bridge.selected_chat == old:
                    bridge.selected_chat = new
            return self.selections

    def _flush(self, connection, bridge):
        payload = b"".join(encode_line(frame) for frame in bridge.take())
        if payload:
            connection.sendall(payload)

    def _receive(self, connection, frame):
        with self.lock:
            bridge = self.connections.get(connection)
            if bridge is None:
                return
            try:
                bridge.feed(frame)
                if bridge.established:
                    # A reconnect replaces only the same authenticated device.
                    # Untrusted sockets can never evict another device.
                    for prior, other in list(self.connections.items()):
                        if prior is not connection and other.peer == bridge.peer:
                            self._disconnect(prior)
                self._flush(connection, bridge)
            except Exception as exc:
                self.error = str(exc)[:300]
                self._disconnect(connection)

    def _disconnect(self, connection):
        bridge = self.connections.pop(connection, None)
        if bridge is None:
            return
        self._bridges = tuple(self.connections.values())
        try:
            connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        connection.close()
        bridge.session.close()
        bridge.session.peer_raw = None
        bridge.session.outbox.clear()
        bridge.pending_pairing = None
        if self.detach is not None:
            self.detach(bridge.client_id)

    def _listen(self, listener):
        while self.listener is listener:
            try:
                conn, _ = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            with self.lock:
                if len(self.connections) >= 8:
                    conn.close()
                    continue
                bridge = MacBridge(self.bridge.execute, self.bridge.session.session_id, identity=self.bridge.identity)
                bridge.registry = self.bridge.registry
                bridge._requests = self.bridge._requests
                bridge._decisions = self.bridge._decisions
                bridge.hold_pairing = True
                bridge.client_id = "remote:" + os.urandom(16).hex()
                bridge.view = copy.deepcopy(self.bridge.view)
                self.connections[conn] = bridge
                self._bridges = tuple(self.connections.values())
                conn.settimeout(2)
                self.error = None
            threading.Thread(target=self._reader, args=(conn,), daemon=True).start()

    def _reader(self, connection):
        try:
            buffer = b""
            while True:
                try:
                    chunk = connection.recv(65536)
                except socket.timeout:
                    continue
                if not chunk:
                    break
                buffer += chunk
                if len(buffer) > 262144:
                    raise RemoteError("frame too large")
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    frame = json.loads(line)
                    self.dispatch(lambda frame=frame: self._receive(connection, frame))
        except (OSError, ValueError, RemoteError) as exc:
            self.error = str(exc)[:300]
        finally:
            # Stop/revocation already cleared this socket on the serial owner.
            # Do not enqueue a late callback after that owner has shut down.
            with self.lock:
                notify_closed = connection in self.connections and self.listener is not None
            def closed():
                with self.lock:
                    self._disconnect(connection)
            if notify_closed: self.dispatch(closed)

    def publish(self, event):
        with self.lock:
            self.bridge.publish(event)
            for connection, bridge in list(self.connections.items()):
                try:
                    bridge.publish(event)
                    self._flush(connection, bridge)
                except OSError:
                    self._disconnect(connection)

    def confirm_pairing(self, identity, code, allow):
        with self.lock:
            match = next(((conn, bridge) for conn, bridge in self.connections.items()
                          if bridge.pending_pairing == {"identity": identity, "confirmCode": code}), None)
            if match is None:
                raise RemoteError("pairing no longer pending")
            connection, bridge = match
            if allow is not True:
                self._disconnect(connection)
                return
            self.bridge.registry.pair(identity, bridge.device_name, reauthorize=True)
            try:
                self._save()
            except Exception:
                self.bridge.registry.revoke(identity)
                self._disconnect(connection)
                raise
            frame = bridge.session.pending_auth
            bridge.session.awaiting_auth = True
            bridge.pending_pairing = None
            bridge.session._client_auth(frame)
            self._flush(connection, bridge)

    def revoke(self, identity):
        with self.lock:
            self.bridge.revoke(identity)
            self._save()
            for connection, bridge in list(self.connections.items()):
                if bridge.peer == identity or (bridge.pending_pairing or {}).get("identity") == identity:
                    self._disconnect(connection)

    def stop(self):
        with self.lock:
            listener, self.listener = self.listener, None
            if listener:
                listener.close()
            for connection in list(self.connections):
                self._disconnect(connection)
