"""Socket-free tests for the post-turn keep_alive lease on Ollama routes.

Covers the lease decision itself, the residency guard that keeps the call
from preloading a model it was meant to evict, the settings validation for
the per-connection value, and the gateway wiring that calls it once a
turn's upstream connection is closed.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from bridge_core import SLOTS
from gateway import Runtime
from hub_config import normalize
from ollama_lifecycle import (DEFAULT_LEASE_SECONDS, KEEP_RESIDENT, MAX_LEASE_SECONDS,
                              canonical_model, daemon_url, lease_seconds, release,
                              resident_models)

BASE = "http://127.0.0.1:11434"


def fake_daemon(resident=(), fail=None):
    """A transport over a daemon holding ``resident``, recording its plans."""
    calls = []

    def transport(plan):
        calls.append(plan)
        if fail is not None:
            raise fail
        if plan["url"].endswith("/api/ps"):
            return {"models": [{"name": name, "model": name, "size_vram": 1} for name in resident]}
        return {"model": plan["body"]["model"], "done": True, "done_reason": "load", "response": ""}

    transport.calls = calls
    return transport


def settings_with(connection):
    return normalize({"providers": {"ollama": connection}}, SLOTS, "mistral-vibe-cli-latest")


class CanonicalModelTests(unittest.TestCase):
    def test_bare_name_takes_the_implicit_latest_tag(self):
        self.assertEqual(canonical_model("gemma3"), "gemma3:latest")

    def test_explicit_tag_is_left_alone(self):
        self.assertEqual(canonical_model("qwen3.5:2b"), "qwen3.5:2b")
        self.assertEqual(canonical_model("gpt-oss:120b-cloud"), "gpt-oss:120b-cloud")

    def test_namespace_slashes_do_not_read_as_a_tag(self):
        self.assertEqual(canonical_model("hf.co/user/model"), "hf.co/user/model:latest")
        self.assertEqual(canonical_model("hf.co/user/model:Q4"), "hf.co/user/model:Q4")

    def test_empty_and_non_text_names_are_not_models(self):
        self.assertEqual(canonical_model("  "), "")
        self.assertEqual(canonical_model(None), "")


class DaemonURLTests(unittest.TestCase):
    def test_loopback_v4_and_v6_daemons_are_addressable(self):
        self.assertEqual(daemon_url(BASE, "/api/ps"), "http://127.0.0.1:11434/api/ps")
        self.assertEqual(daemon_url("http://[::1]:11434", "/api/ps"), "http://[::1]:11434/api/ps")

    def test_a_non_loopback_or_non_http_daemon_is_refused(self):
        for base in ("http://10.0.0.5:11434", "https://ollama.example.com", "http://ollama.example.com", ""):
            with self.subTest(base=base), self.assertRaises(ValueError):
                daemon_url(base, "/api/ps")


class ResidentModelsTests(unittest.TestCase):
    def test_resident_names_are_canonical(self):
        transport = fake_daemon(resident=("gemma3", "qwen3.5:2b"))
        self.assertEqual(resident_models(BASE, transport=transport), {"gemma3:latest", "qwen3.5:2b"})
        self.assertEqual(transport.calls[0]["method"], "GET")

    def test_an_empty_or_malformed_answer_holds_nothing(self):
        for answer in ({"models": []}, {"models": None}, {}, None):
            with self.subTest(answer=answer):
                self.assertEqual(resident_models(BASE, transport=lambda plan: answer), set())


class ReleaseTests(unittest.TestCase):
    def test_a_resident_model_is_leased_for_the_configured_seconds(self):
        transport = fake_daemon(resident=("qwen3.5:2b",))
        self.assertEqual(release(BASE, "qwen3.5:2b", 90, transport=transport), "leased")
        ps, lease = transport.calls
        self.assertEqual(ps["url"], BASE + "/api/ps")
        self.assertEqual(lease["method"], "POST")
        self.assertEqual(lease["url"], BASE + "/api/generate")
        # No prompt: the daemon re-arms the runner's expiry without
        # generating, which is the whole reason this call is affordable.
        self.assertEqual(lease["body"], {"model": "qwen3.5:2b", "keep_alive": 90})

    def test_a_model_the_daemon_is_not_holding_is_never_touched(self):
        # The guarantee that makes this safe: the same call against a cold
        # model is a preload, so a cloud route, an already-evicted runner
        # and a turn that never reached the weights all stop at /api/ps.
        transport = fake_daemon(resident=("qwen3.5:2b",))
        self.assertEqual(release(BASE, "gpt-oss:120b-cloud", 90, transport=transport), "absent")
        self.assertEqual([plan["url"] for plan in transport.calls], [BASE + "/api/ps"])

    def test_a_bare_route_matches_its_canonical_resident_name(self):
        transport = fake_daemon(resident=("gemma3:latest",))
        self.assertEqual(release(BASE, "gemma3", 90, transport=transport), "leased")
        self.assertEqual(transport.calls[1]["body"]["model"], "gemma3:latest")

    def test_zero_unloads_the_model_as_the_turn_ends(self):
        transport = fake_daemon(resident=("qwen3.5:2b",))
        self.assertEqual(release(BASE, "qwen3.5:2b", 0, transport=transport), "unloaded")
        self.assertEqual(transport.calls[1]["body"]["keep_alive"], 0)

    def test_minus_one_pins_the_model_in_memory(self):
        transport = fake_daemon(resident=("qwen3.5:2b",))
        self.assertEqual(release(BASE, "qwen3.5:2b", KEEP_RESIDENT, transport=transport), "pinned")
        self.assertEqual(transport.calls[1]["body"]["keep_alive"], -1)

    def test_an_unreachable_daemon_is_reported_not_raised(self):
        self.assertEqual(release(BASE, "qwen3.5:2b", 90, transport=fake_daemon(fail=OSError("refused"))),
                         "unreachable")

    def test_a_daemon_address_that_is_not_loopback_reaches_no_socket(self):
        transport = fake_daemon(resident=("qwen3.5:2b",))
        self.assertEqual(release("http://10.0.0.5:11434", "qwen3.5:2b", 90, transport=transport), "unreachable")
        self.assertEqual(transport.calls, [])

    def test_an_empty_model_is_not_a_release(self):
        transport = fake_daemon(resident=("qwen3.5:2b",))
        self.assertEqual(release(BASE, "", 90, transport=transport), "absent")
        self.assertEqual(transport.calls, [])


class LeaseSettingTests(unittest.TestCase):
    def test_a_connection_without_the_field_takes_the_default(self):
        settings = settings_with({})
        self.assertNotIn("idle_unload_seconds", settings["providers"]["ollama"])
        self.assertEqual(lease_seconds(settings["providers"]["ollama"]), DEFAULT_LEASE_SECONDS)

    def test_a_configured_lease_survives_normalization_and_is_read_back(self):
        for value in (0, 30, KEEP_RESIDENT, MAX_LEASE_SECONDS):
            with self.subTest(value=value):
                settings = settings_with({"idle_unload_seconds": value})
                self.assertEqual(settings["providers"]["ollama"]["idle_unload_seconds"], value)
                self.assertEqual(lease_seconds(settings["providers"]["ollama"]), value)
                # Settings round-trip through the file on every save.
                again = normalize(settings, SLOTS, "mistral-vibe-cli-latest")
                self.assertEqual(again["providers"]["ollama"]["idle_unload_seconds"], value)

    def test_out_of_range_and_non_integer_leases_are_refused(self):
        for value in (MAX_LEASE_SECONDS + 1, -2, "90s", 90.0, True, []):
            with self.subTest(value=value), self.assertRaises(ValueError):
                settings_with({"idle_unload_seconds": value})

    def test_a_hosted_provider_cannot_claim_a_lease_it_would_ignore(self):
        with self.assertRaises(ValueError):
            normalize({"providers": {"mistral": {"idle_unload_seconds": 90}}}, SLOTS, "mistral-vibe-cli-latest")


class GatewayWiringTests(unittest.TestCase):
    """Runtime.release_local_model, without standing a server up."""

    def runtime_for(self, connection):
        return SimpleNamespace(settings=settings_with(connection))

    def call(self, runtime, plan):
        with patch("gateway.release_resident_model") as released, patch("gateway.http_transport") as transport:
            Runtime.release_local_model(runtime, plan)
        return released, transport

    def test_an_ollama_turn_leases_the_model_from_its_route(self):
        runtime = self.runtime_for({"idle_unload_seconds": 30})
        released, _ = self.call(runtime, {"provider_id": "ollama", "route": "ollama/qwen3.5:2b"})
        base, model, seconds = released.call_args.args
        self.assertEqual((base, model, seconds), (BASE, "qwen3.5:2b", 30))

    def test_the_default_lease_applies_when_the_connection_names_none(self):
        released, _ = self.call(self.runtime_for({}), {"provider_id": "ollama", "route": "ollama/gemma3:4b"})
        self.assertEqual(released.call_args.args[2], DEFAULT_LEASE_SECONDS)

    def test_a_hosted_turn_opens_no_daemon_connection(self):
        released, transport = self.call(self.runtime_for({}),
                                        {"provider_id": "mistral", "route": "mistral/mistral-medium-2508"})
        released.assert_not_called()
        transport.assert_not_called()

    def test_a_malformed_plan_is_not_an_error_in_a_finished_turn(self):
        # A plan with no route, or an unparseable one, reaches this only
        # after the client has been answered: it must go quiet, not raise
        # out of the handler's finally and not guess at a model.
        runtime = self.runtime_for({})
        for plan in (None, {}, {"provider_id": "ollama"}, {"provider_id": "ollama", "route": "ollama/"}):
            with self.subTest(plan=plan):
                released, _ = self.call(runtime, plan)
                released.assert_not_called()


class DesktopControlTests(unittest.TestCase):
    """The provider pane and the worker have to agree about this lease.

    The desktop rewrites settings.json wholesale, and the worker reads the
    lease back out of it, so a field the app does not carry is a field the
    app deletes, and a default the app shows but the worker does not use is
    a control that lies.
    """

    def source(self, name):
        return Path(__file__).with_name(name).read_text()

    def test_the_saved_connection_carries_the_lease(self):
        connection = re.search(r"struct ProviderConnection[^}]+}", self.source("HubModels.swift"), re.S)
        self.assertIsNotNone(connection)
        self.assertIn("var idle_unload_seconds: Int?", connection.group(0))

    def test_the_default_the_pane_shows_is_the_one_the_worker_uses(self):
        shown = re.search(r"static let defaultIdleUnloadSeconds = (-?\d+)", self.source("MistralBridge.swift"))
        self.assertIsNotNone(shown, "the desktop no longer names its own default lease")
        self.assertEqual(int(shown.group(1)), DEFAULT_LEASE_SECONDS)

    def test_every_preset_the_pane_offers_is_a_settable_lease(self):
        presets = re.search(r"let presets = \[([^\]]+)\]", self.source("ProviderViews.swift"))
        self.assertIsNotNone(presets, "the provider pane no longer lists lease presets")
        values = [int(value) for value in presets.group(1).split(",")]
        self.assertIn(DEFAULT_LEASE_SECONDS, values)
        for value in values:
            with self.subTest(value=value):
                settings = settings_with({"idle_unload_seconds": value})
                self.assertEqual(settings["providers"]["ollama"]["idle_unload_seconds"], value)


if __name__ == "__main__":
    unittest.main()
