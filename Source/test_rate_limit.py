"""Deterministic unit tests for the client-side rate limiter.

All timing runs on a fake clock/sleeper: no wall-clock waits, no sockets.
HTTP-level behavior (absorbed retries, slot queueing) is covered in
test_gateway_hub.py against the mock upstream.
"""
from __future__ import annotations

import threading
import time
import unittest
from email.utils import formatdate

from rate_limit import (BACKOFF_BASE, BACKOFF_CAP, THROTTLE_CAP, ProviderAdmission, ProviderThrottle,
                        backoff_delay, parse_retry_after)


class FakeClock:
    def __init__(self):
        self.now = 1000.0
        self.sleeps = []

    def clock(self):
        return self.now

    def sleeper(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def cancel_after(limit):
    """A cancel callable that fires after ``limit`` checks."""
    state = {"checks": 0}

    def cancel():
        state["checks"] += 1
        return state["checks"] > limit

    return cancel


class ParseRetryAfterTests(unittest.TestCase):
    def test_delta_seconds(self):
        self.assertEqual(parse_retry_after("2"), 2.0)
        self.assertEqual(parse_retry_after("  30 "), 30.0)
        self.assertEqual(parse_retry_after("0"), 0.0)

    def test_unusable_values_yield_none(self):
        for value in (None, "", "   ", "soon", "-3", "1.2.3"):
            with self.subTest(value=value):
                self.assertIsNone(parse_retry_after(value))

    def test_http_date_is_relative_to_now(self):
        moment = 1_700_000_000.0
        header = formatdate(moment, usegmt=True)
        self.assertAlmostEqual(parse_retry_after(header, now=moment - 30), 30.0)
        self.assertEqual(parse_retry_after(header, now=moment + 10), 0.0)

    def test_garbage_date_yields_none(self):
        self.assertIsNone(parse_retry_after("Wed, 99 Foo 9999 99:99:99 GMT"))


class BackoffDelayTests(unittest.TestCase):
    def test_exponential_growth_within_cap(self):
        for attempt in range(8):
            with self.subTest(attempt=attempt):
                delay = backoff_delay(attempt)
                floor = min(BACKOFF_BASE * (2 ** attempt), THROTTLE_CAP)
                self.assertGreaterEqual(delay, floor)
                self.assertLessEqual(delay, floor + 1.0)

    def test_retry_after_is_a_floor(self):
        delay = backoff_delay(0, retry_after=10.0)
        self.assertGreaterEqual(delay, 10.0)
        self.assertLessEqual(delay, 11.0)

    def test_huge_hint_is_capped(self):
        delay = backoff_delay(0, retry_after=9999.0)
        self.assertGreaterEqual(delay, THROTTLE_CAP)
        self.assertLessEqual(delay, THROTTLE_CAP + 1.0)

    def test_cap_bounds_high_attempts(self):
        delay = backoff_delay(100)
        self.assertGreaterEqual(delay, min(BACKOFF_CAP, THROTTLE_CAP))
        self.assertLessEqual(delay, THROTTLE_CAP + 1.0)


class ProviderThrottleTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeClock()
        self.throttle = ProviderThrottle(clock=self.fake.clock, sleeper=self.fake.sleeper)

    def test_unset_window_passes_at_once(self):
        self.assertTrue(self.throttle.wait("mistral"))
        self.assertEqual(self.fake.sleeps, [])

    def test_limit_parks_key_until_window_passes(self):
        delay = self.throttle.note_limit("mistral", attempt=1)
        self.assertTrue(self.throttle.wait("mistral"))
        self.assertAlmostEqual(sum(self.fake.sleeps), delay, places=6)
        # Window consumed: the next wait passes at once.
        before = len(self.fake.sleeps)
        self.assertTrue(self.throttle.wait("mistral"))
        self.assertEqual(len(self.fake.sleeps), before)

    def test_keys_are_independent(self):
        self.throttle.note_limit("mistral", attempt=3)
        self.assertTrue(self.throttle.wait("deepseek"))
        self.assertEqual(self.fake.sleeps, [])

    def test_success_clears_window(self):
        self.throttle.note_limit("mistral", attempt=4)
        self.throttle.note_success("mistral")
        self.assertTrue(self.throttle.wait("mistral"))
        self.assertEqual(self.fake.sleeps, [])

    def test_late_sibling_success_does_not_clear_newer_limit(self):
        started_at = self.fake.now
        self.fake.now += 1
        self.throttle.note_limit("mistral", attempt=4)
        self.assertFalse(self.throttle.note_success("mistral", started_at=started_at))
        self.assertGreater(self.throttle.remaining("mistral"), 0)
        self.assertTrue(self.throttle.note_success("mistral", started_at=self.fake.now))
        self.assertEqual(self.throttle.remaining("mistral"), 0)

    def test_later_limit_does_not_shorten_existing_cooldown(self):
        self.throttle.note_limit("mistral", retry_after=30)
        first = self.throttle.remaining("mistral")
        self.throttle.note_limit("mistral", retry_after=1)
        self.assertGreaterEqual(self.throttle.remaining("mistral"), first)

    def test_cancel_aborts_wait(self):
        self.throttle.note_limit("mistral", attempt=4)
        self.assertFalse(self.throttle.wait("mistral", cancel=cancel_after(2)))
        # Still gated afterwards: cancellation is not consent.
        self.assertFalse(self.throttle.wait("mistral", cancel=lambda: True, timeout=0))

    def test_timeout_aborts_wait(self):
        self.throttle.note_limit("mistral", attempt=4)
        self.assertFalse(self.throttle.wait("mistral", timeout=0.1))
        self.assertLessEqual(sum(self.fake.sleeps), 0.1 + 1e-6)

    def test_concurrent_limits_do_not_corrupt(self):
        errors = []

        def hammer(index):
            try:
                for attempt in range(25):
                    self.throttle.note_limit(f"key-{index % 4}", attempt=attempt % 3)
                    self.throttle.note_success(f"key-{(index + 1) % 4}")
            except Exception as exc:  # pragma: no cover - fails the test below
                errors.append(exc)

        threads = [threading.Thread(target=hammer, args=(index,)) for index in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])


class ProviderAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.semaphore = threading.BoundedSemaphore(3)
        self.admission = ProviderAdmission(self.semaphore, capacity=3)

    def test_parallel_admission_and_provider_backoff(self):
        for attempt in range(3):
            self.assertTrue(self.admission.acquire("kimi"))
        self.assertFalse(self.admission.acquire("qwen", timeout=0.01))
        self.admission.release("kimi")
        self.assertTrue(self.admission.acquire("qwen"))
        self.admission.release("qwen")
        for attempt in range(2):
            self.admission.release("kimi")
        self.admission.note_limit("kimi")
        self.assertTrue(self.admission.acquire("kimi"))
        self.assertFalse(self.admission.acquire("kimi", timeout=0.01))
        self.assertTrue(self.admission.acquire("qwen"))
        self.admission.release("kimi")
        self.admission.release("qwen")

    def test_recovery_is_gradual(self):
        self.admission.note_limit("cerebras")
        for success in range(7):
            self.admission.note_success("cerebras")
        self.assertTrue(self.admission.acquire("cerebras"))
        self.assertFalse(self.admission.acquire("cerebras", timeout=0.01))
        self.admission.note_success("cerebras")
        self.assertTrue(self.admission.acquire("cerebras"))
        self.admission.release("cerebras")
        self.admission.release("cerebras")

    def test_cancelled_waiter_does_not_block_next_provider(self):
        self.assertFalse(self.admission.acquire("kimi", cancel=lambda: True))
        self.assertTrue(self.admission.acquire("qwen"))
        self.admission.release("qwen")

    def test_waiters_rotate_across_providers(self):
        self.assertTrue(self.semaphore.acquire(blocking=False))
        self.assertTrue(self.semaphore.acquire(blocking=False))
        self.assertTrue(self.semaphore.acquire(blocking=False))
        order = []
        permits = {name: threading.Event() for name in ("kimi-1", "kimi-2", "qwen")}

        def worker(name, provider):
            if self.admission.acquire(provider, timeout=3):
                order.append(name)
                permits[name].wait(3)
                self.admission.release(provider)

        threads = [threading.Thread(target=worker, args=(name, provider)) for name, provider in
                   (("kimi-1", "kimi"), ("kimi-2", "kimi"), ("qwen", "qwen"))]
        for index, thread in enumerate(threads):
            thread.start()
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                with self.admission._condition:
                    if sum(map(len, self.admission._waiting.values())) >= index + 1:
                        break
                time.sleep(0.01)
        for position in range(3):
            self.semaphore.release()
            deadline = time.monotonic() + 2
            while len(order) < position + 1 and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertEqual(len(order), position + 1)
        self.assertEqual(order, ["kimi-1", "qwen", "kimi-2"])
        for permit in permits.values():
            permit.set()
        for thread in threads:
            thread.join(timeout=3)
            self.assertFalse(thread.is_alive())


if __name__ == "__main__":
    unittest.main()
