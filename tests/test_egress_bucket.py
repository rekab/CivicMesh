"""Unit tests for `mesh_bot._GlobalEgressBucket`, the relay-wide
hourly egress cap.

The bucket is a sliding-hour token bucket consulted by `_outbox_task` to
cap relay-wide mesh egress. Tests pass `now` explicitly to every method
so no fake-clock fixture or `freezegun` dependency is needed — the
production caller reads `time.monotonic()` once per check and passes it
in, mirrored here.

Scenario (h) from the implementation plan — bucket-not-consulted-during-
RECOVERING — is not tested here. It is enforced structurally: the
`controller.outbox_should_pause()` gate at mesh_bot.py:134-140 happens
BEFORE the bucket check site, and the bucket is never consulted while
the outbox loop is paused. Verified by code-read; an async-task-level
test would be fragile and would not add coverage beyond that structural
property.
"""

import unittest

from mesh_bot import _GlobalEgressBucket


class TestGlobalEgressBucket(unittest.TestCase):

    def test_under_cap_allows_immediately(self):
        # (a) Capacity=10; ten consecutive try_consume return (True, 0.0).
        b = _GlobalEgressBucket(capacity_per_hour=10)
        for i in range(10):
            allowed, wait = b.try_consume(now=float(i))
            self.assertTrue(allowed, f"call {i} should be allowed")
            self.assertEqual(wait, 0.0)

    def test_at_cap_pauses_with_wait(self):
        # (b) Capacity=2; consume twice at t=0; third at t=10 returns
        # (False, ~3590); advance to t=3601; consume returns True.
        b = _GlobalEgressBucket(capacity_per_hour=2)
        self.assertEqual(b.try_consume(now=0.0), (True, 0.0))
        self.assertEqual(b.try_consume(now=0.0), (True, 0.0))
        allowed, wait = b.try_consume(now=10.0)
        self.assertFalse(allowed)
        # Oldest send is at t=0; cutoff slides to t-3600. wait_sec until
        # the oldest send falls out of the window is 3600 - (10 - 0) = 3590.
        self.assertAlmostEqual(wait, 3590.0, places=1)
        # After 3601s: the t=0 entry is older than the 1-hour cutoff and
        # gets evicted, freeing a slot.
        allowed, wait = b.try_consume(now=3601.0)
        self.assertTrue(allowed)
        self.assertEqual(wait, 0.0)

    def test_sliding_window_evicts_old(self):
        # (c) Consume at t=0; advance to t=3601; consume returns True with
        # the deque size still 1 (old entry was evicted, not preserved).
        b = _GlobalEgressBucket(capacity_per_hour=1)
        self.assertEqual(b.try_consume(now=0.0), (True, 0.0))
        # Eviction happens lazily at consume time, so probing the deque
        # size after a successful consume confirms one entry, not two.
        allowed, _ = b.try_consume(now=3601.0)
        self.assertTrue(allowed)
        self.assertEqual(len(b._sends), 1,
                         "old entry should have been evicted, not retained")

    def test_cold_start_grants_full_budget(self):
        # (d) Fresh bucket, capacity=N; N consecutive try_consume succeed
        # at t=0 with no wait. Documents the "no persistence across
        # restart" property — on Pi boot the deque is empty and the
        # first N sends go through immediately.
        for cap in (1, 5, 200):
            b = _GlobalEgressBucket(capacity_per_hour=cap)
            for i in range(cap):
                allowed, wait = b.try_consume(now=0.0)
                self.assertTrue(allowed, f"capacity={cap} call {i} should be allowed at cold start")
                self.assertEqual(wait, 0.0)
            # The next one fails.
            allowed, _ = b.try_consume(now=0.0)
            self.assertFalse(allowed, f"capacity={cap}: post-fill consume must fail")

    def test_consume_on_attempt_not_success(self):
        # (i) Structural test: try_consume is the only API. There is no
        # record_success / record_failure path. Every successful claim
        # spends a token regardless of whether the eventual radio call
        # succeeded — bytes hit the air either way, so the airtime budget
        # is debited the same.
        b = _GlobalEgressBucket(capacity_per_hour=1)
        api = {name for name in dir(b) if not name.startswith("_")}
        self.assertEqual(api, {"try_consume"},
                         "bucket must expose ONLY try_consume; any success-only "
                         "path would let a flaky radio bypass the cap")

    def test_failed_consume_does_not_grow_deque(self):
        # At-cap try_consume returns (False, wait) and does NOT append.
        # If a denied claim grew the deque, the eviction logic would
        # eventually let attackers "queue up" tokens against the future.
        b = _GlobalEgressBucket(capacity_per_hour=1)
        self.assertEqual(b.try_consume(now=0.0), (True, 0.0))
        size_before = len(b._sends)
        for i in range(20):
            allowed, _ = b.try_consume(now=10.0 + i)
            self.assertFalse(allowed)
        self.assertEqual(len(b._sends), size_before,
                         "denied claims must not grow the deque")


if __name__ == "__main__":
    unittest.main()
