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

from rate_limit import (BACKOFF_BASE, BACKOFF_CAP, THROTTLE_CAP, ProviderThrottle,
                        backoff_delay, parse_retry_after, wait_for_slot)


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


class WaitForSlotTests(unittest.TestCase):
    def test_free_slot_acquires_at_once(self):
        fake = FakeClock()
        semaphore = threading.Semaphore(2)
        self.assertTrue(wait_for_slot(semaphore, clock=fake.clock, sleeper=fake.sleeper))
        self.assertEqual(fake.sleeps, [])
        semaphore.release()

    def test_held_slot_times_out(self):
        fake = FakeClock()
        semaphore = threading.Semaphore(1)
        self.assertTrue(semaphore.acquire(blocking=False))
        try:
            self.assertFalse(wait_for_slot(semaphore, timeout=1.0, clock=fake.clock, sleeper=fake.sleeper))
            self.assertAlmostEqual(sum(fake.sleeps), 1.0, places=6)
        finally:
            semaphore.release()

    def test_cancel_aborts_wait(self):
        fake = FakeClock()
        semaphore = threading.Semaphore(1)
        self.assertTrue(semaphore.acquire(blocking=False))
        try:
            self.assertFalse(wait_for_slot(semaphore, cancel=cancel_after(3),
                                           clock=fake.clock, sleeper=fake.sleeper))
        finally:
            semaphore.release()

    def test_release_unblocks_waiter(self):
        semaphore = threading.Semaphore(1)
        self.assertTrue(semaphore.acquire(blocking=False))
        outcome = {}

        def waiter():
            outcome["acquired"] = wait_for_slot(semaphore, timeout=5)

        thread = threading.Thread(target=waiter)
        thread.start()
        time.sleep(0.2)
        self.assertNotIn("acquired", outcome)
        semaphore.release()
        thread.join(timeout=5)
        self.assertTrue(outcome.get("acquired"))
        semaphore.release()


if __name__ == "__main__":
    unittest.main()
