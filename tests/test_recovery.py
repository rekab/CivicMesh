"""Tests for recovery.py — RecoveryController, liveness_task, recovery_task."""

import asyncio
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from config import RecoveryConfig
from database import DBConfig, init_db
from recovery import (
    DEFAULT_LADDER,
    RecoveryController,
    RecoveryState,
    Rung,
    RungContext,
    _backoff,
    _disconnect_client,
    liveness_task,
    recovery_task,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_controller(
    tmpdir: str,
    cfg: RecoveryConfig | None = None,
) -> RecoveryController:
    cfg = cfg or RecoveryConfig()
    db_cfg = DBConfig(path=f"{tmpdir}/test.db")
    init_db(db_cfg)
    import logging
    log = logging.getLogger("test_recovery")
    return RecoveryController(cfg, db_cfg, "/dev/ttyFAKE", log)


class FakeEvent:
    def __init__(self, etype="ok", payload=None):
        self.type = etype
        self.payload = payload or {}


class FakeCommands:
    def __init__(self):
        self.healthy = True
        self.error = False

    async def get_stats_core(self):
        if not self.healthy:
            await asyncio.sleep(999)  # hangs until cancelled
        if self.error:
            return FakeEvent("ERROR", {"reason": "fake error"})
        return FakeEvent("ok", {"battery_mv": 4200})


class FakeMeshCore:
    def __init__(self):
        self.commands = FakeCommands()
        self.self_info = {"name": "test_node"}
        self._disconnected = False

    async def disconnect(self):
        self._disconnected = True


# ---------------------------------------------------------------------------
# RecoveryController state transitions
# ---------------------------------------------------------------------------

class TestControllerState(unittest.TestCase):
    def test_initial_state_is_disconnected(self):
        with tempfile.TemporaryDirectory() as d:
            c = _make_controller(d)
            assert c.get_state() == RecoveryState.DISCONNECTED
            assert c.get_client() is None

    def test_set_client_does_not_change_state(self):
        with tempfile.TemporaryDirectory() as d:
            c = _make_controller(d)
            mc = FakeMeshCore()
            c.set_client(mc)
            assert c.get_client() is mc
            assert c.get_state() == RecoveryState.DISCONNECTED

    def test_mark_healthy_transitions_state(self):
        with tempfile.TemporaryDirectory() as d:
            c = _make_controller(d)
            c.set_client(FakeMeshCore())
            c.mark_healthy()
            assert c.get_state() == RecoveryState.HEALTHY
            assert c.is_healthy()

    def test_outbox_should_pause_in_recovering(self):
        with tempfile.TemporaryDirectory() as d:
            c = _make_controller(d)
            c._state = RecoveryState.RECOVERING
            assert c.outbox_should_pause() is True

    def test_outbox_should_pause_in_needs_human(self):
        with tempfile.TemporaryDirectory() as d:
            c = _make_controller(d)
            c._state = RecoveryState.NEEDS_HUMAN
            assert c.outbox_should_pause() is True

    def test_outbox_should_not_pause_when_healthy(self):
        with tempfile.TemporaryDirectory() as d:
            c = _make_controller(d)
            c.set_client(FakeMeshCore())
            c.mark_healthy()
            assert c.outbox_should_pause() is False


# ---------------------------------------------------------------------------
# request_recovery idempotency
# ---------------------------------------------------------------------------

class TestRequestRecovery(unittest.TestCase):
    def test_request_recovery_sets_event(self):
        with tempfile.TemporaryDirectory() as d:
            c = _make_controller(d)
            assert not c._request_event.is_set()
            c.request_recovery(source="test", reason="testing")
            assert c._request_event.is_set()

    def test_request_recovery_is_idempotent(self):
        with tempfile.TemporaryDirectory() as d:
            c = _make_controller(d)
            c.request_recovery(source="test", reason="first")
            c.request_recovery(source="test", reason="second")
            assert c._request_event.is_set()
            # Clear once — if it was set multiple times, this still only
            # needs one clear.
            c._request_event.clear()
            assert not c._request_event.is_set()


# ---------------------------------------------------------------------------
# Backoff calculation
# ---------------------------------------------------------------------------

class TestBackoff(unittest.TestCase):
    def test_backoff_attempt_1(self):
        cfg = RecoveryConfig(backoff_base_sec=60.0, backoff_cap_sec=3600.0)
        assert _backoff(1, cfg) == 60.0

    def test_backoff_attempt_2(self):
        cfg = RecoveryConfig(backoff_base_sec=60.0, backoff_cap_sec=3600.0)
        assert _backoff(2, cfg) == 120.0

    def test_backoff_attempt_3(self):
        cfg = RecoveryConfig(backoff_base_sec=60.0, backoff_cap_sec=3600.0)
        assert _backoff(3, cfg) == 240.0

    def test_backoff_caps(self):
        cfg = RecoveryConfig(backoff_base_sec=60.0, backoff_cap_sec=3600.0)
        assert _backoff(100, cfg) == 3600.0


# ---------------------------------------------------------------------------
# Flapping detection
# ---------------------------------------------------------------------------

class TestFlapping(unittest.TestCase):
    def test_within_cap_is_ok(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = RecoveryConfig(flapping_max_recoveries=6, flapping_window_sec=3600)
            c = _make_controller(d, cfg)
            # Simulate 6 recoveries within the window
            now = time.monotonic()
            for i in range(6):
                c._flapping_deque.append(now + i)
            assert len(c._flapping_deque) <= cfg.flapping_max_recoveries

    def test_exceeding_cap_detected(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = RecoveryConfig(flapping_max_recoveries=6, flapping_window_sec=3600)
            c = _make_controller(d, cfg)
            now = time.monotonic()
            for i in range(7):
                c._flapping_deque.append(now + i)
            assert len(c._flapping_deque) > cfg.flapping_max_recoveries

    def test_old_entries_expire(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = RecoveryConfig(flapping_max_recoveries=6, flapping_window_sec=10)
            c = _make_controller(d, cfg)
            now = time.monotonic()
            # Add 7 entries, but make first 4 older than the window
            for i in range(4):
                c._flapping_deque.append(now - 20 + i)
            for i in range(3):
                c._flapping_deque.append(now + i)
            # Prune old entries (same logic as recovery_task)
            while (c._flapping_deque
                   and c._flapping_deque[0] < now - cfg.flapping_window_sec):
                c._flapping_deque.popleft()
            assert len(c._flapping_deque) == 3
            assert len(c._flapping_deque) <= cfg.flapping_max_recoveries


# ---------------------------------------------------------------------------
# Liveness task
# ---------------------------------------------------------------------------

class TestLivenessTask(unittest.IsolatedAsyncioTestCase):
    async def test_consecutive_timeouts_trigger_recovery(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = RecoveryConfig(
                liveness_interval_sec=0.01,
                liveness_timeout_sec=0.01,
                liveness_consecutive_threshold=3,
            )
            c = _make_controller(d, cfg)
            mc = FakeMeshCore()
            mc.commands.healthy = False  # will timeout
            c.set_client(mc)
            c.mark_healthy()

            task = asyncio.create_task(liveness_task(c, c._log))
            try:
                # Wait for the event to be set (3 misses at 0.01s interval)
                await asyncio.wait_for(c._request_event.wait(), timeout=2.0)
                assert c._request_event.is_set()
            finally:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

    async def test_skips_during_recovery(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = RecoveryConfig(
                liveness_interval_sec=0.01,
                liveness_timeout_sec=0.01,
                liveness_consecutive_threshold=1,
            )
            c = _make_controller(d, cfg)
            mc = FakeMeshCore()
            mc.commands.healthy = False
            c.set_client(mc)
            c._state = RecoveryState.RECOVERING

            task = asyncio.create_task(liveness_task(c, c._log))
            try:
                # Give it time to run several iterations
                await asyncio.sleep(0.1)
                # Should NOT have triggered because outbox_should_pause() is True
                assert not c._request_event.is_set()
            finally:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

    async def test_successful_ping_resets_counter(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = RecoveryConfig(
                liveness_interval_sec=0.01,
                liveness_timeout_sec=0.5,
                liveness_consecutive_threshold=3,
            )
            c = _make_controller(d, cfg)
            mc = FakeMeshCore()
            mc.commands.healthy = True
            c.set_client(mc)
            c.mark_healthy()

            task = asyncio.create_task(liveness_task(c, c._log))
            try:
                await asyncio.sleep(0.1)
                # Several successful pings — should not trigger recovery
                assert not c._request_event.is_set()
            finally:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass


# ---------------------------------------------------------------------------
# Recovery task with fake ladder
# ---------------------------------------------------------------------------

class TestRecoveryTask(unittest.IsolatedAsyncioTestCase):
    async def test_successful_recovery(self):
        """Trigger recovery → fake rung succeeds → reconnect → HEALTHY."""
        with tempfile.TemporaryDirectory() as d:
            cfg = RecoveryConfig(post_rts_settle_sec=0.01, verify_timeout_sec=2.0)
            c = _make_controller(d, cfg)
            mc = FakeMeshCore()
            c.set_client(mc)
            c.mark_healthy()

            new_mc = FakeMeshCore()

            async def fake_setup(client):
                pass  # no-op setup

            async def fake_action(ctx):
                pass  # no-op reset

            fake_ladder = [Rung(name="fake_rts", action=fake_action)]

            with patch("recovery._connect_once", new_callable=AsyncMock) as mock_connect:
                mock_connect.return_value = new_mc

                task = asyncio.create_task(
                    recovery_task(c, fake_setup, c._log, ladder=fake_ladder)
                )
                try:
                    c.request_recovery(source="test", reason="test")
                    # Wait for state to return to HEALTHY
                    for _ in range(100):
                        if c.get_state() == RecoveryState.HEALTHY and c.get_client() is new_mc:
                            break
                        await asyncio.sleep(0.05)
                    assert c.get_state() == RecoveryState.HEALTHY
                    assert c.get_client() is new_mc
                finally:
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass

    async def test_pre_recovery_health_check_skips_second_attempt(self):
        """First attempt fails (rung action raises), the client is
        disconnected.  During the NEEDS_HUMAN backoff, a healthy client
        is re-attached (simulating the radio recovering on its own).
        On re-entry, the health check finds the radio alive and skips
        the ladder — the rung should only be invoked once."""
        with tempfile.TemporaryDirectory() as d:
            cfg = RecoveryConfig(
                post_rts_settle_sec=0.01,
                verify_timeout_sec=2.0,
                backoff_base_sec=0.05,
                backoff_cap_sec=0.1,
            )
            c = _make_controller(d, cfg)
            mc = FakeMeshCore()
            mc.commands.healthy = True
            c.set_client(mc)
            c.mark_healthy()

            call_count = 0

            async def counting_action(ctx):
                nonlocal call_count
                call_count += 1
                raise RuntimeError("rung failed")

            fake_ladder = [Rung(name="counting", action=counting_action)]

            task = asyncio.create_task(
                recovery_task(c, AsyncMock(), c._log, ladder=fake_ladder)
            )
            try:
                c.request_recovery(source="test", reason="test")
                # Wait for the first attempt to fail and enter NEEDS_HUMAN
                for _ in range(100):
                    if c.get_state() == RecoveryState.NEEDS_HUMAN:
                        break
                    await asyncio.sleep(0.02)
                assert c.get_state() == RecoveryState.NEEDS_HUMAN
                assert call_count == 1

                # Simulate the radio recovering during backoff: attach a
                # healthy client so the health check can probe it.
                healthy_mc = FakeMeshCore()
                healthy_mc.commands.healthy = True
                c.set_client(healthy_mc)

                # Wait for the health check to detect the healthy radio
                for _ in range(200):
                    if c.get_state() == RecoveryState.HEALTHY:
                        break
                    await asyncio.sleep(0.05)
                # Give extra time for any spurious second rung invocation
                await asyncio.sleep(0.2)
                assert c.get_state() == RecoveryState.HEALTHY
                assert call_count == 1, f"expected 1 rung invocation, got {call_count}"
            finally:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

    async def test_rung_failure_enters_needs_human(self):
        """All rungs fail → enters NEEDS_HUMAN."""
        with tempfile.TemporaryDirectory() as d:
            cfg = RecoveryConfig(
                post_rts_settle_sec=0.01,
                verify_timeout_sec=0.1,
                backoff_base_sec=0.01,
                backoff_cap_sec=0.05,
            )
            c = _make_controller(d, cfg)
            mc = FakeMeshCore()
            c.set_client(mc)
            c.mark_healthy()

            async def failing_action(ctx):
                raise RuntimeError("rung failed")

            fake_ladder = [Rung(name="failing", action=failing_action)]

            task = asyncio.create_task(
                recovery_task(c, AsyncMock(), c._log, ladder=fake_ladder)
            )
            try:
                c.request_recovery(source="test", reason="test")
                for _ in range(100):
                    if c.get_state() == RecoveryState.NEEDS_HUMAN:
                        break
                    await asyncio.sleep(0.05)
                assert c.get_state() == RecoveryState.NEEDS_HUMAN
            finally:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass


# ---------------------------------------------------------------------------
# Disconnect helper
# ---------------------------------------------------------------------------

class TestDisconnect(unittest.IsolatedAsyncioTestCase):
    async def test_disconnect_calls_disconnect(self):
        mc = FakeMeshCore()
        await _disconnect_client(mc, MagicMock())
        assert mc._disconnected

    async def test_disconnect_none_is_noop(self):
        await _disconnect_client(None, MagicMock())

    async def test_disconnect_timeout_force_closes(self):
        mc = FakeMeshCore()

        async def hang():
            await asyncio.sleep(999)

        mc.disconnect = hang
        transport = MagicMock()
        mc.connection_manager = MagicMock()
        mc.connection_manager.connection = MagicMock()
        mc.connection_manager.connection.transport = transport

        await _disconnect_client(mc, MagicMock())
        transport.close.assert_called_once()
