"""PeerBroker: durable, workspace-scoped mailboxes with broker-issued principals."""
from contextlib import closing
import os
from pathlib import Path
import sqlite3
import stat
import tempfile
import threading
import unittest

import peer_broker
from peer_broker import PeerAuthError, PeerBroker, PeerBrokerBusy, PeerRequestError, Principal


class Clock:
    def __init__(self, moment=1_800_000_000.0):
        self.moment = moment

    def __call__(self):
        return self.moment

    def advance(self, seconds):
        self.moment += seconds


class BrokerCase(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(os.path.realpath(self.temporary.name))
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.other_workspace = self.root / "elsewhere"
        self.other_workspace.mkdir()
        self.state = self.root / "state" / "peers"
        self.clock = Clock()
        self.broker = self.open()

    def open(self):
        broker = PeerBroker(self.state, clock=self.clock)
        self.addCleanup(broker.close)
        return broker

    def restart(self):
        self.broker.close()
        self.broker = self.open()
        return self.broker

    def external(self, native="codex-1", workspace=None, app="codex", model="gpt", **extra):
        enrolled = self.broker.enroll(app=app, native_thread_id=native,
                                      workspace=workspace or self.workspace, model=model, **extra)
        principal = self.broker.attach(enrolled["thread"]["thread_id"], enrolled["token"])
        return enrolled, principal

    def hub(self, native="hub-1", workspace=None, model="opus"):
        endpoint = self.broker.ensure_internal(native, workspace or self.workspace, "Hub chat", model, "anthropic")
        return endpoint, self.broker.internal(endpoint["thread_id"])

    def send(self, principal, to, body="hello", key="k1", **extra):
        args = {"to": to, "body": body, "idempotency_key": key, **extra}
        return self.broker.dispatch(principal, "hub_send_message", args)

    def read(self, principal, **args):
        return self.broker.dispatch(principal, "hub_read_messages", args)


class EnrolmentTests(BrokerCase):
    def test_enrol_returns_public_endpoint_and_a_token_only_once(self):
        enrolled = self.broker.enroll(app="codex", native_thread_id="n", workspace=self.workspace,
                                      label="Fix", model="gpt", provider="openai")
        thread = enrolled["thread"]
        self.assertTrue(enrolled["token"].startswith(peer_broker.TOKEN_PREFIX))
        self.assertEqual(set(thread), {"thread_id", "native_thread_id", "app", "workspace", "label", "model",
                                       "provider", "identity_assurance", "instance_id", "status", "last_seen",
                                       "capabilities"})
        self.assertEqual(thread["identity_assurance"], "enrolled")
        self.assertEqual(thread["status"], "offline")
        self.assertEqual(thread["capabilities"], {"poll": True, "wake": False})
        self.assertEqual(thread["workspace"], str(self.workspace))
        self.assertNotIn(enrolled["token"], repr(self.broker.thread(thread["thread_id"])))

    def test_token_is_stored_only_as_a_digest(self):
        enrolled, _ = self.external()
        self.broker.close()
        with closing(sqlite3.connect(self.state / peer_broker.DATABASE_NAME)) as db:
            dump = "\n".join(db.iterdump())
        self.assertNotIn(enrolled["token"], dump)
        self.assertNotIn(enrolled["token"][len(peer_broker.TOKEN_PREFIX):], dump)

    def test_enrol_never_rotates_a_live_endpoint(self):
        enrolled, principal = self.external()
        with self.assertRaises(PeerRequestError) as caught:
            self.broker.enroll(app="codex", native_thread_id="codex-1", workspace=self.workspace)
        self.assertEqual(caught.exception.code, "duplicate")
        self.assertEqual(self.broker.dispatch(principal, "hub_self")["thread"]["thread_id"],
                         enrolled["thread"]["thread_id"])

    def test_identity_is_scoped_by_canonical_workspace(self):
        link = self.root / "link"
        link.symlink_to(self.workspace)
        first = self.broker.enroll(app="codex", native_thread_id="n", workspace=self.workspace)
        with self.assertRaises(PeerRequestError):
            self.broker.enroll(app="codex", native_thread_id="n", workspace=str(link) + "/")
        other = self.broker.enroll(app="codex", native_thread_id="n", workspace=self.other_workspace)
        self.assertNotEqual(first["thread"]["thread_id"], other["thread"]["thread_id"])
        with self.assertRaises(PeerRequestError):
            self.broker.enroll(app="codex", native_thread_id="n", workspace="relative/path")

    def test_external_enrolment_can_never_claim_native_identity(self):
        with self.assertRaises(PeerRequestError) as caught:
            self.broker.enroll(app=peer_broker.APP_INTERNAL, native_thread_id="n", workspace=self.workspace)
        self.assertEqual(caught.exception.code, "forbidden")
        with self.assertRaises(PeerRequestError):
            self.broker.enroll(app="codex", native_thread_id="n", workspace=self.workspace, verified=True)
        for bad in ("Codex", "", "a b", "x" * 65):
            with self.assertRaises(PeerRequestError):
                self.broker.enroll(app=bad, native_thread_id="n", workspace=self.workspace)

    def test_app_allowlist_and_capabilities(self):
        self.broker.close()
        broker = PeerBroker(self.state, clock=self.clock, apps=["codex", "claude"])
        self.addCleanup(broker.close)
        with self.assertRaises(PeerRequestError):
            broker.enroll(app="cursor", native_thread_id="n", workspace=self.workspace)
        with self.assertRaises(PeerRequestError):
            broker.enroll(app="codex", native_thread_id="n", workspace=self.workspace, capabilities={"wake": True})
        with self.assertRaises(PeerRequestError):
            broker.enroll(app="codex", native_thread_id="n", workspace=self.workspace, capabilities={"run": True})
        broker.enroll(app="codex", native_thread_id="n", workspace=self.workspace, capabilities={"poll": True})

    def test_ensure_internal_is_stable_and_updates_observed_attributes(self):
        first = self.broker.ensure_internal("hub-1", self.workspace, "Old", "sonnet", "anthropic")
        second = self.broker.ensure_internal("hub-1", self.workspace, "New", "opus", "anthropic")
        self.assertEqual(first["thread_id"], second["thread_id"])
        self.assertEqual((second["label"], second["model"]), ("New", "opus"))
        self.assertEqual(second["app"], peer_broker.APP_INTERNAL)
        self.assertEqual(second["identity_assurance"], "native")
        self.assertNotIn("token", second)

    def test_find_internal_never_creates_or_revives(self):
        self.assertIsNone(self.broker.find_internal("hub-1", self.workspace))
        self.assertIsNone(self.broker.find_internal("hub-1", self.workspace))
        endpoint, _ = self.hub()
        self.assertEqual(self.broker.find_internal("hub-1", self.workspace)["thread_id"], endpoint["thread_id"])
        self.broker.revoke(endpoint["thread_id"])
        self.assertEqual(self.broker.find_internal("hub-1", self.workspace)["status"], "revoked")
        with self.assertRaises(PeerAuthError):
            self.broker.ensure_internal("hub-1", self.workspace, "Hub", "opus", "anthropic")
        restored = self.broker.ensure_internal("hub-1", self.workspace, "Hub", "opus", "anthropic", restore=True)
        self.assertEqual(restored["thread_id"], endpoint["thread_id"])
        self.assertEqual(restored["status"], "offline")


class AuthenticationTests(BrokerCase):
    def test_wrong_token_and_unknown_threads_are_rejected_alike(self):
        enrolled = self.broker.enroll(app="codex", native_thread_id="n", workspace=self.workspace)
        thread_id = enrolled["thread"]["thread_id"]
        for thread, token in ((thread_id, enrolled["token"] + "x"), (thread_id, ""), (thread_id, None),
                              ("00000000-0000-0000-0000-000000000000", enrolled["token"]),
                              ("not-a-uuid", enrolled["token"]), (thread_id, "x" * 1000)):
            with self.assertRaises(PeerAuthError) as caught:
                self.broker.attach(thread, token)
            self.assertEqual(str(caught.exception), "Peer authentication failed.")
        self.assertEqual(self.broker.thread(thread_id)["status"], "offline")

    def test_internal_threads_cannot_attach_and_externals_are_not_internal(self):
        endpoint, _ = self.hub()
        enrolled, _ = self.external()
        with self.assertRaises(PeerAuthError):
            self.broker.attach(endpoint["thread_id"], enrolled["token"])
        with self.assertRaises(PeerAuthError):
            self.broker.internal(enrolled["thread"]["thread_id"])

    def test_forged_principals_are_rejected(self):
        enrolled, principal = self.external()
        forged = Principal(self.broker, principal.thread_id, principal.instance_id)
        for candidate in (forged, {"thread_id": principal.thread_id}, None, "principal"):
            with self.assertRaises(PeerAuthError):
                self.broker.dispatch(candidate, "hub_self")
        other = PeerBroker(self.root / "other", clock=self.clock)
        self.addCleanup(other.close)
        with self.assertRaises(PeerAuthError):
            other.dispatch(principal, "hub_self")

    def test_a_new_attach_makes_the_old_instance_stale(self):
        enrolled, old = self.external()
        new = self.broker.attach(enrolled["thread"]["thread_id"], enrolled["token"])
        self.assertNotEqual(old.instance_id, new.instance_id)
        with self.assertRaises(PeerAuthError) as caught:
            self.broker.dispatch(old, "hub_self")
        self.assertEqual(caught.exception.code, "stale")
        self.assertFalse(self.broker.disconnect(old))
        self.assertEqual(self.broker.dispatch(new, "hub_self")["thread"]["status"], "online")

    def test_disconnect_only_applies_to_the_current_instance(self):
        enrolled, principal = self.external()
        self.assertTrue(self.broker.disconnect(principal))
        self.assertEqual(self.broker.thread(principal.thread_id)["status"], "offline")
        with self.assertRaises(PeerAuthError):
            self.broker.dispatch(principal, "hub_self")
        again = self.broker.attach(enrolled["thread"]["thread_id"], enrolled["token"])
        self.assertEqual(self.broker.dispatch(again, "hub_self")["thread"]["status"], "online")

    def test_internal_principal_is_reused_until_restart(self):
        endpoint, principal = self.hub()
        self.assertIs(self.broker.internal(endpoint["thread_id"]), principal)
        self.restart()
        with self.assertRaises(PeerAuthError):
            self.broker.dispatch(principal, "hub_self")
        fresh = self.broker.internal(endpoint["thread_id"])
        self.assertNotEqual(fresh.instance_id, principal.instance_id)
        self.assertEqual(self.broker.dispatch(fresh, "hub_self")["thread"]["thread_id"], endpoint["thread_id"])

    def test_revoke_invalidates_token_and_principals_immediately(self):
        enrolled, principal = self.external()
        hub, hub_principal = self.hub()
        self.assertTrue(self.broker.revoke(principal.thread_id))
        self.assertFalse(self.broker.revoke(principal.thread_id))
        with self.assertRaises(PeerAuthError):
            self.broker.dispatch(principal, "hub_self")
        with self.assertRaises(PeerAuthError):
            self.broker.attach(principal.thread_id, enrolled["token"])
        self.assertEqual(self.broker.thread(principal.thread_id)["status"], "revoked")
        with self.assertRaises(PeerRequestError) as caught:
            self.send(hub_principal, principal.thread_id)
        self.assertEqual(caught.exception.code, "revoked")
        listed = self.broker.dispatch(hub_principal, "hub_list_threads")["threads"]
        self.assertEqual(listed, [])
        revoked = self.broker.dispatch(hub_principal, "hub_list_threads", {"status": "revoked"})["threads"]
        self.assertEqual([thread["thread_id"] for thread in revoked], [principal.thread_id])
        # Re-enrolment keeps the logical identity and issues a new token.
        again = self.broker.enroll(app="codex", native_thread_id="codex-1", workspace=self.workspace)
        self.assertEqual(again["thread"]["thread_id"], principal.thread_id)
        self.assertNotEqual(again["token"], enrolled["token"])
        with self.assertRaises(PeerAuthError):
            self.broker.attach(principal.thread_id, enrolled["token"])
        self.broker.attach(principal.thread_id, again["token"])

    def test_revoked_internal_endpoint_has_no_principal(self):
        endpoint, principal = self.hub()
        self.broker.revoke(endpoint["thread_id"])
        with self.assertRaises(PeerAuthError):
            self.broker.dispatch(principal, "hub_self")
        with self.assertRaises(PeerAuthError):
            self.broker.internal(endpoint["thread_id"])

    def test_lease_lapses_to_offline_and_activity_renews_it(self):
        _, principal = self.external()
        hub, hub_principal = self.hub()
        self.clock.advance(peer_broker.LEASE_SECONDS - 1)
        self.broker.dispatch(hub_principal, "hub_self")
        self.clock.advance(2)
        status = {t["thread_id"]: t["status"] for t in
                  self.broker.dispatch(hub_principal, "hub_list_threads")["threads"]}
        self.assertEqual(status[principal.thread_id], "offline")
        self.assertEqual(self.broker.dispatch(principal, "hub_self")["thread"]["status"], "online")


class RestartTests(BrokerCase):
    def test_schema_initialises_and_reopens_in_its_own_transaction(self):
        self.broker.close()
        for _ in range(3):
            broker = PeerBroker(self.state, clock=self.clock)
            broker.close()
        with closing(sqlite3.connect(self.state / peer_broker.DATABASE_NAME)) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], peer_broker.SCHEMA_VERSION)
        self.broker = self.open()
        self.assertIsNone(self.broker.find_internal("hub", self.workspace))

    def test_newer_schema_is_refused(self):
        self.broker.close()
        with closing(sqlite3.connect(self.state / peer_broker.DATABASE_NAME)) as db:
            db.execute(f"PRAGMA user_version = {peer_broker.SCHEMA_VERSION + 1}")
        with self.assertRaises(PeerRequestError):
            PeerBroker(self.state, clock=self.clock)

    def test_queued_messages_and_identities_survive_restart(self):
        enrolled, sender = self.external()
        hub, hub_principal = self.hub()
        sent = self.send(sender, hub["thread_id"], body="persist me")["message"]
        self.restart()
        listed = self.broker.thread(sender.thread_id)
        self.assertEqual(listed["status"], "offline")
        self.assertIsNone(listed["instance_id"])
        with self.assertRaises(PeerAuthError):
            self.broker.dispatch(sender, "hub_self")
        reader = self.broker.internal(self.broker.find_internal("hub-1", self.workspace)["thread_id"])
        messages = self.read(reader)["messages"]
        self.assertEqual([(m["message_id"], m["body"]) for m in messages], [(sent["message_id"], "persist me")])
        sender = self.broker.attach(enrolled["thread"]["thread_id"], enrolled["token"])
        retry = self.send(sender, hub["thread_id"], body="persist me")
        self.assertTrue(retry["duplicate"])
        self.assertEqual(retry["message"]["message_id"], sent["message_id"])

    def test_one_owner_per_directory(self):
        with self.assertRaises(PeerBrokerBusy):
            PeerBroker(self.state, clock=self.clock)
        self.broker.close()
        self.open()

    def test_closed_broker_rejects_work(self):
        _, principal = self.external()
        self.broker.close()
        self.broker.close()
        with self.assertRaises(PeerAuthError):
            self.broker.dispatch(principal, "hub_self")


class FilesystemTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(os.path.realpath(self.temporary.name))

    def test_directory_and_database_are_private(self):
        broker = PeerBroker(self.root / "peers")
        self.addCleanup(broker.close)
        self.assertEqual(stat.S_IMODE(os.lstat(self.root / "peers").st_mode), 0o700)
        for name in (peer_broker.DATABASE_NAME, peer_broker.LOCK_NAME):
            self.assertEqual(stat.S_IMODE(os.lstat(self.root / "peers" / name).st_mode) & 0o077, 0)

    def test_symbolic_links_are_refused(self):
        real = self.root / "real"
        real.mkdir(mode=0o700)
        (self.root / "linked").symlink_to(real)
        with self.assertRaises(PermissionError):
            PeerBroker(self.root / "linked")
        target = self.root / "target.sqlite3"
        target.touch(mode=0o600)
        directory = self.root / "peers"
        directory.mkdir(mode=0o700)
        (directory / peer_broker.DATABASE_NAME).symlink_to(target)
        with self.assertRaises(PermissionError):
            PeerBroker(directory)
        (directory / peer_broker.DATABASE_NAME).unlink()
        (directory / (peer_broker.DATABASE_NAME + "-wal")).symlink_to(target)
        with self.assertRaises(PermissionError):
            PeerBroker(directory)

    def test_a_shared_database_file_is_refused(self):
        directory = self.root / "peers"
        directory.mkdir(mode=0o700)
        database = directory / peer_broker.DATABASE_NAME
        database.touch()
        database.chmod(0o644)
        with self.assertRaises(PermissionError):
            PeerBroker(directory)


class ScopeTests(BrokerCase):
    def test_discovery_and_sending_stay_inside_the_workspace(self):
        _, here = self.external("a")
        _, also_here = self.external("b", app="claude")
        _, there = self.external("c", workspace=self.other_workspace)
        listed = self.broker.dispatch(here, "hub_list_threads")["threads"]
        self.assertEqual([t["thread_id"] for t in listed], [also_here.thread_id])
        self.assertEqual(self.broker.dispatch(here, "hub_list_threads", {"app": "codex"})["threads"], [])
        with self.assertRaises(PeerRequestError) as caught:
            self.send(here, there.thread_id)
        self.assertEqual(caught.exception.code, "not_found")
        with self.assertRaises(PeerRequestError):
            self.send(here, here.thread_id)

    def test_list_is_bounded(self):
        _, viewer = self.external("viewer")
        for index in range(peer_broker.LIST_LIMIT + 1):
            self.broker.enroll(app="codex", native_thread_id=f"n{index}", workspace=self.workspace)
        result = self.broker.dispatch(viewer, "hub_list_threads")
        self.assertEqual(len(result["threads"]), peer_broker.LIST_LIMIT)
        self.assertTrue(result["truncated"])

    def test_sender_comes_from_the_principal_never_arguments(self):
        _, alice = self.external("alice")
        _, bob = self.external("bob")
        hub, reader = self.hub()
        for field in ("from", "sender", "sender_thread_id", "workspace", "instance_id"):
            with self.assertRaises(PeerRequestError) as caught:
                self.send(alice, hub["thread_id"], **{field: bob.thread_id})
            self.assertIn("Unexpected argument", str(caught.exception))
        self.send(alice, hub["thread_id"])
        message = self.read(reader)["messages"][0]
        self.assertEqual(message["sender"]["thread_id"], alice.thread_id)
        self.assertEqual(message["sender_instance_id"], alice.instance_id)

    def test_arguments_are_validated(self):
        hub, principal = self.hub()
        _, other = self.external()
        cases = [
            ("hub_nope", {}),
            ("hub_self", {"extra": 1}),
            ("hub_self", ["list"]),
            ("hub_send_message", {"to": other.thread_id, "body": "x"}),
            ("hub_send_message", {"to": "nope", "body": "x", "idempotency_key": "k"}),
            ("hub_send_message", {"to": other.thread_id, "body": "", "idempotency_key": "k"}),
            ("hub_send_message", {"to": other.thread_id, "body": 5, "idempotency_key": "k"}),
            ("hub_send_message", {"to": other.thread_id, "body": "\x00", "idempotency_key": "k"}),
            ("hub_send_message", {"to": other.thread_id, "body": "\ud800", "idempotency_key": "k"}),
            ("hub_send_message", {"to": other.thread_id, "body": "x", "idempotency_key": "k", "kind": "run"}),
            ("hub_send_message", {"to": other.thread_id, "body": "x", "idempotency_key": "k", "kind": "reply"}),
            ("hub_send_message", {"to": other.thread_id, "body": "x", "idempotency_key": "k" * 129}),
            ("hub_read_messages", {"limit": 0}),
            ("hub_read_messages", {"limit": peer_broker.READ_LIMIT + 1}),
            ("hub_read_messages", {"limit": True}),
            ("hub_read_messages", {"after": -1}),
            ("hub_read_messages", {"after": 1.5}),
            ("hub_list_threads", {"status": "busy"}),
            ("hub_ack_message", {"message_id": "nope"}),
        ]
        for name, args in cases:
            with self.subTest(name=name, args=args), self.assertRaises(PeerRequestError):
                self.broker.dispatch(principal, name, args)

    def test_body_is_bounded_in_utf8_bytes(self):
        hub, _ = self.hub()
        _, sender = self.external()
        self.send(sender, hub["thread_id"], body="é" * (peer_broker.BODY_LIMIT // 2), key="fits")
        with self.assertRaises(PeerRequestError) as caught:
            self.send(sender, hub["thread_id"], body="é" * (peer_broker.BODY_LIMIT // 2 + 1), key="big")
        self.assertEqual(caught.exception.code, "too_large")
        self.send(sender, hub["thread_id"], body="line one\nline two\ttab", key="lines")


class MessageTests(BrokerCase):
    def test_message_carries_provenance_and_immutable_snapshots(self):
        hub, reader = self.hub(model="opus")
        enrolled, sender = self.external(model="gpt-1", provider="openai", label="Worker")
        sent = self.send(sender, hub["thread_id"], body="status?")["message"]
        self.assertEqual(sent["origin"], "peer_agent")
        self.assertEqual(sent["authority"], "reference_only")
        self.assertEqual(sent["status"], "queued")
        self.assertEqual(sent["kind"], "coordination")
        self.assertEqual(sent["sender"], {"thread_id": sender.thread_id, "app": "codex", "native_thread_id": "codex-1",
                                          "label": "Worker", "model": "gpt-1", "provider": "openai",
                                          "identity_assurance": "enrolled"})
        self.assertEqual(sent["recipient"]["identity_assurance"], "native")
        self.assertEqual(sent["recipient"]["model"], "opus")
        self.assertEqual(sent["created_at"], peer_broker.iso(self.clock.moment))
        self.assertEqual(sent["expires_at"], peer_broker.iso(self.clock.moment + peer_broker.MESSAGE_TTL_SECONDS))
        self.assertNotIn("token", repr(sent).lower())
        # A later model change does not rewrite the recorded snapshot.
        self.broker.ensure_internal("hub-1", self.workspace, "Hub chat", "sonnet", "anthropic")
        delivered = self.read(reader)["messages"][0]
        self.assertEqual(delivered["recipient"]["model"], "opus")
        self.assertEqual(delivered["sender"]["model"], "gpt-1")
        self.assertNotIn("idempotency_key", delivered)
        self.assertEqual(self.broker.dispatch(sender, "hub_message_status",
                                              {"message_id": sent["message_id"]})["message"]["idempotency_key"], "k1")

    def test_sequence_is_monotonic_and_read_pages_by_cursor(self):
        hub, reader = self.hub()
        _, sender = self.external()
        ids = [self.send(sender, hub["thread_id"], body=str(i), key=f"k{i}")["message"]["message_id"]
               for i in range(5)]
        page = self.read(reader, limit=2)
        self.assertEqual([m["message_id"] for m in page["messages"]], ids[:2])
        self.assertTrue(page["more"])
        sequences = [m["sequence"] for m in page["messages"]]
        self.assertEqual(sequences, sorted(sequences))
        rest = self.read(reader, after=page["next_after"], limit=50)
        self.assertEqual([m["message_id"] for m in rest["messages"]], ids[2:])
        self.assertFalse(rest["more"])
        empty = self.read(reader, after=rest["next_after"])
        self.assertEqual((empty["messages"], empty["next_after"]), ([], rest["next_after"]))

    def test_read_marks_presented_idempotently(self):
        hub, reader = self.hub()
        _, sender = self.external()
        sent = self.send(sender, hub["thread_id"])["message"]
        first = self.read(reader)["messages"][0]
        self.clock.advance(5)
        second = self.read(reader)["messages"][0]
        self.assertEqual(first["status"], "presented")
        self.assertEqual(second["presented_at"], first["presented_at"])
        status = self.broker.dispatch(sender, "hub_message_status", {"message_id": sent["message_id"]})
        self.assertEqual(status["message"]["status"], "presented")

    def test_ack_requires_presentation_and_only_the_recipient(self):
        hub, reader = self.hub()
        _, sender = self.external("sender")
        _, bystander = self.external("bystander")
        message_id = self.send(sender, hub["thread_id"])["message"]["message_id"]
        with self.assertRaises(PeerRequestError) as caught:
            self.broker.dispatch(reader, "hub_ack_message", {"message_id": message_id})
        self.assertEqual(caught.exception.code, "not_presented")
        self.read(reader)
        for principal in (sender, bystander):
            with self.assertRaises(PeerRequestError) as caught:
                self.broker.dispatch(principal, "hub_ack_message", {"message_id": message_id})
            self.assertEqual(caught.exception.code, "not_found")
        acked = self.broker.dispatch(reader, "hub_ack_message", {"message_id": message_id})["message"]
        self.assertEqual(acked["status"], "acknowledged")
        again = self.broker.dispatch(reader, "hub_ack_message", {"message_id": message_id})["message"]
        self.assertEqual(again["acknowledged_at"], acked["acknowledged_at"])
        self.assertEqual(self.read(reader)["messages"], [])

    def test_status_is_visible_only_to_sender_and_recipient(self):
        hub, reader = self.hub()
        _, sender = self.external("sender")
        _, bystander = self.external("bystander")
        message_id = self.send(sender, hub["thread_id"])["message"]["message_id"]
        for principal in (sender, reader):
            self.broker.dispatch(principal, "hub_message_status", {"message_id": message_id})
        with self.assertRaises(PeerRequestError) as caught:
            self.broker.dispatch(bystander, "hub_message_status", {"message_id": message_id})
        self.assertEqual(caught.exception.code, "not_found")
        self.assertEqual(self.read(bystander)["messages"], [])

    def test_idempotency_is_scoped_to_sender_and_content(self):
        hub, reader = self.hub()
        _, alice = self.external("alice")
        _, bob = self.external("bob")
        first = self.send(alice, hub["thread_id"], body="same", key="shared")
        retry = self.send(alice, hub["thread_id"], body="same", key="shared")
        self.assertFalse(first["duplicate"])
        self.assertTrue(retry["duplicate"])
        self.assertEqual(retry["message"]["message_id"], first["message"]["message_id"])
        with self.assertRaises(PeerRequestError) as caught:
            self.send(alice, hub["thread_id"], body="different", key="shared")
        self.assertEqual(caught.exception.code, "conflict")
        with self.assertRaises(PeerRequestError):
            self.send(alice, bob.thread_id, body="same", key="shared")
        other = self.send(bob, hub["thread_id"], body="same", key="shared")
        self.assertFalse(other["duplicate"])
        self.assertEqual(len(self.read(reader)["messages"]), 2)

    def test_replies_must_reference_a_visible_message(self):
        hub, reader = self.hub()
        _, alice = self.external("alice")
        _, bob = self.external("bob")
        original = self.send(alice, hub["thread_id"])["message"]["message_id"]
        reply = self.send(reader, alice.thread_id, body="ack", key="r", kind="reply", reply_to=original)
        self.assertEqual(reply["message"]["reply_to"], original)
        with self.assertRaises(PeerRequestError) as caught:
            self.send(bob, hub["thread_id"], key="spoof", kind="reply", reply_to=original)
        self.assertEqual(caught.exception.code, "not_found")
        with self.assertRaises(PeerRequestError):
            self.send(alice, hub["thread_id"], key="x", reply_to=original)

    def test_replies_stay_within_the_original_pair(self):
        hub, reader = self.hub()
        _, alice = self.external("alice")
        _, bob = self.external("bob")
        original = self.send(alice, hub["thread_id"])["message"]["message_id"]
        # Either party may reply within the pair, in either direction.
        back = self.send(reader, alice.thread_id, body="back", key="r1", kind="reply", reply_to=original)
        self.assertEqual(back["message"]["reply_to"], original)
        onward = self.send(alice, hub["thread_id"], body="more", key="r2", kind="reply", reply_to=original)
        self.assertEqual(onward["message"]["reply_to"], original)
        # Neither party may cite the alice<->hub conversation in a message to bob.
        for sender, key in ((reader, "x1"), (alice, "x2")):
            with self.subTest(key=key), self.assertRaises(PeerRequestError) as caught:
                self.send(sender, bob.thread_id, key=key, kind="reply", reply_to=original)
            self.assertEqual(caught.exception.code, "not_found")
        # Nor may bob cite it to either of them.
        for recipient, key in ((hub["thread_id"], "x3"), (alice.thread_id, "x4")):
            with self.subTest(key=key), self.assertRaises(PeerRequestError) as caught:
                self.send(bob, recipient, key=key, kind="reply", reply_to=original)
            self.assertEqual(caught.exception.code, "not_found")

    def test_pending_queue_is_bounded_per_recipient(self):
        hub, reader = self.hub()
        _, sender = self.external()
        _, other = self.external("other")
        for index in range(peer_broker.QUEUE_LIMIT):
            self.send(sender, hub["thread_id"], body="m", key=f"k{index}")
        with self.assertRaises(PeerRequestError) as caught:
            self.send(sender, hub["thread_id"], body="m", key="overflow")
        self.assertEqual(caught.exception.code, "queue_full")
        self.send(sender, other.thread_id, body="m", key="elsewhere")
        # Presented messages still occupy the queue until acknowledged.
        page = self.read(reader, limit=1)
        with self.assertRaises(PeerRequestError):
            self.send(sender, hub["thread_id"], body="m", key="overflow")
        self.broker.dispatch(reader, "hub_ack_message", {"message_id": page["messages"][0]["message_id"]})
        self.send(sender, hub["thread_id"], body="m", key="overflow")

    def test_messages_expire_and_are_eventually_pruned(self):
        hub, reader = self.hub()
        _, sender = self.external()
        message_id = self.send(sender, hub["thread_id"])["message"]["message_id"]
        self.clock.advance(peer_broker.MESSAGE_TTL_SECONDS)
        self.assertEqual(self.read(reader)["messages"], [])
        status = self.broker.dispatch(sender, "hub_message_status", {"message_id": message_id})
        self.assertEqual(status["message"]["status"], "expired")
        with self.assertRaises(PeerRequestError) as caught:
            self.broker.dispatch(reader, "hub_ack_message", {"message_id": message_id})
        self.assertEqual(caught.exception.code, "expired")
        self.clock.advance(peer_broker.RETENTION_SECONDS + 1)
        with self.assertRaises(PeerRequestError):
            self.broker.dispatch(sender, "hub_message_status", {"message_id": message_id})

    def test_concurrent_sends_keep_unique_ordered_sequences(self):
        hub, reader = self.hub()
        _, sender = self.external()
        errors = []

        def worker(offset):
            try:
                for index in range(20):
                    self.send(sender, hub["thread_id"], body="m", key=f"{offset}-{index}")
            except Exception as error:  # pragma: no cover - reported below
                errors.append(error)

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(5)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        self.assertEqual(errors, [])
        sequences, after = [], 0
        while True:
            page = self.read(reader, after=after, limit=50)
            sequences += [m["sequence"] for m in page["messages"]]
            after = page["next_after"]
            if not page["more"]:
                break
        self.assertEqual(len(sequences), 100)
        self.assertEqual(sequences, sorted(set(sequences)))


if __name__ == "__main__":
    unittest.main()
