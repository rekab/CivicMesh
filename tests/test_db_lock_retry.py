"""Tests for the DB lock-retry decorator, executor offload, and event-loop
responsiveness changes from commits 46bbe08 and 93b2993.

Three claims under test:

A. _retry_on_locked retries on "database is locked", gives up after N
   attempts, and does not retry on other errors.

B. The event loop stays responsive during DB lock contention — i.e.,
   DB writes running in worker threads (asyncio.to_thread / executor)
   do not block other coroutines.

   We test this WITHOUT a full mesh_bot subprocess.  Spawning mesh_bot
   requires a real serial radio or an elaborate mock of the meshcore
   library, which is out of scope.  Instead we run insert_message (a
   @_retry_on_locked-decorated function) via asyncio.to_thread against
   a real SQLite file where another connection holds the write lock.
   A parallel asyncio.sleep(0.1) task must complete on time, proving
   the event loop is not blocked by the held lock.

C. _executor_db logs exceptions via done-callback instead of silently
   swallowing them.
"""

import asyncio
import logging
import os
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

from database import DBConfig, _retry_on_locked, init_db, insert_message
from mesh_bot import _executor_db


# ---------------------------------------------------------------------------
# Claim A: _retry_on_locked decorator behavior
# ---------------------------------------------------------------------------

class TestRetryOnLockedSuccess(unittest.TestCase):
    """A1: Two lock failures then success."""

    @patch("database.time.sleep")
    def test_retries_then_succeeds(self, mock_sleep):
        call_count = 0

        @_retry_on_locked(attempts=3, base_delay=0.05)
        def flaky():
            nonlocal call_count
            call_count += 1
            if call_count <= 2:
                raise sqlite3.OperationalError("database is locked")
            return 42

        result = flaky()
        self.assertEqual(result, 42)
        self.assertEqual(call_count, 3)
        self.assertEqual(mock_sleep.call_count, 2)
        # Verify backoff delays: 0.05 * 3^0, 0.05 * 3^1
        self.assertAlmostEqual(mock_sleep.call_args_list[0][0][0], 0.05)
        self.assertAlmostEqual(mock_sleep.call_args_list[1][0][0], 0.15)


class TestRetryOnLockedExhausted(unittest.TestCase):
    """A2: All attempts fail — re-raises the last OperationalError."""

    @patch("database.time.sleep")
    def test_gives_up_after_max_attempts(self, mock_sleep):
        call_count = 0

        @_retry_on_locked(attempts=3, base_delay=0.05)
        def always_locked():
            nonlocal call_count
            call_count += 1
            raise sqlite3.OperationalError("database is locked")

        with self.assertRaises(sqlite3.OperationalError) as ctx:
            always_locked()
        self.assertIn("database is locked", str(ctx.exception))
        self.assertEqual(call_count, 3)
        # Sleep called on attempts 1 and 2, NOT on the final attempt
        self.assertEqual(mock_sleep.call_count, 2)


class TestRetryOnLockedOtherOperationalError(unittest.TestCase):
    """A3: Non-lock OperationalError is not retried."""

    @patch("database.time.sleep")
    def test_no_retry_on_other_operational_error(self, mock_sleep):
        @_retry_on_locked(attempts=3, base_delay=0.05)
        def bad_table():
            raise sqlite3.OperationalError("no such table: foo")

        with self.assertRaises(sqlite3.OperationalError) as ctx:
            bad_table()
        self.assertIn("no such table", str(ctx.exception))
        mock_sleep.assert_not_called()


class TestRetryOnLockedNonSqliteError(unittest.TestCase):
    """A4: Non-sqlite exception passes through unchanged."""

    @patch("database.time.sleep")
    def test_no_retry_on_value_error(self, mock_sleep):
        @_retry_on_locked(attempts=3, base_delay=0.05)
        def bad_value():
            raise ValueError("nope")

        with self.assertRaises(ValueError):
            bad_value()
        mock_sleep.assert_not_called()


class TestRetryOnLockedLogging(unittest.TestCase):
    """A5: Retries emit db:retry_on_locked log warnings."""

    @patch("database.time.sleep")
    def test_retry_emits_warning(self, mock_sleep):
        call_count = 0

        @_retry_on_locked(attempts=3, base_delay=0.05)
        def flaky_logged():
            nonlocal call_count
            call_count += 1
            if call_count <= 1:
                raise sqlite3.OperationalError("database is locked")
            return "ok"

        with self.assertLogs("civicmesh.db", level="WARNING") as cm:
            flaky_logged()
        # Should have exactly one retry warning (attempt 1)
        self.assertEqual(len(cm.output), 1)
        self.assertIn("db:retry_on_locked", cm.output[0])
        self.assertIn("flaky_logged", cm.output[0])
        self.assertIn("attempt=1", cm.output[0])


# ---------------------------------------------------------------------------
# Claim B: Event loop stays responsive during lock contention
# ---------------------------------------------------------------------------

class TestEventLoopResponsiveDuringLock(unittest.IsolatedAsyncioTestCase):
    """B: A DB write blocked on a held lock (running in a worker thread)
    does not prevent other coroutines from executing.

    Setup: create a real SQLite DB, hold the write lock from a second
    connection (BEGIN IMMEDIATE without COMMIT), then run insert_message
    via asyncio.to_thread.  A parallel canary task (asyncio.sleep +
    flag set) must complete promptly, proving the event loop is free.
    """

    async def test_event_loop_not_blocked_by_locked_db(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            db_cfg = DBConfig(path=db_path, timeout_sec=4.0)
            init_db(db_cfg)

            # Hold the write lock from a separate connection
            blocker = sqlite3.connect(db_path, timeout=0)
            self.addCleanup(blocker.close)
            blocker.execute("BEGIN IMMEDIATE")
            blocker.execute(
                "INSERT INTO messages (ts, channel, sender, content, source) "
                "VALUES (1, 'ch', 's', 'block', 'test')"
            )
            # Do NOT commit — lock is held

            canary_done = asyncio.Event()

            async def canary():
                """Must complete quickly if the event loop is free."""
                await asyncio.sleep(0.1)
                canary_done.set()

            async def blocked_write():
                """insert_message will block in the worker thread until
                the lock is released or timeout_sec expires."""
                try:
                    await asyncio.to_thread(
                        insert_message,
                        db_cfg,
                        ts=999,
                        channel="test",
                        sender="test",
                        content="hello",
                        source="test",
                    )
                except sqlite3.OperationalError:
                    pass  # expected — lock held past timeout

            # Start both concurrently
            write_task = asyncio.create_task(blocked_write())
            canary_task = asyncio.create_task(canary())

            # The canary should complete within 1 second even though
            # the DB write is blocked.  If insert_message were running
            # on the event loop thread, canary would be starved.
            try:
                await asyncio.wait_for(canary_task, timeout=1.0)
            except asyncio.TimeoutError:
                self.fail(
                    "Canary task did not complete within 1s — event loop "
                    "was blocked by the DB write"
                )

            self.assertTrue(canary_done.is_set())

            # Clean up: release lock so the write task can finish
            blocker.execute("ROLLBACK")
            blocker.close()

            # Let the write task finish (might succeed now or might
            # have already timed out — either is fine)
            try:
                await asyncio.wait_for(write_task, timeout=6.0)
            except Exception:
                pass


class TestRetryFiresDuringLockContention(unittest.IsolatedAsyncioTestCase):
    """B (supplementary): When a lock is held then released, the retry
    decorator fires and the write eventually succeeds.

    This validates the full path: to_thread + decorator retry + real
    SQLite lock contention.  Uses a threading.Event to release the lock
    after the first retry fires, avoiding timing-fragile fixed delays.
    """

    async def test_retry_succeeds_after_lock_released(self):
        import threading

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            # Short timeout so the first attempt gives up fast, but
            # long enough that after the lock is released during the
            # retry sleep, subsequent attempts can acquire it.
            db_cfg = DBConfig(path=db_path, timeout_sec=0.2)
            init_db(db_cfg)

            # Hold the write lock.  check_same_thread=False because
            # the ROLLBACK will be issued from the worker thread
            # inside the patched sleep.
            blocker = sqlite3.connect(
                db_path, timeout=0, check_same_thread=False,
            )
            self.addCleanup(blocker.close)
            blocker.execute("BEGIN IMMEDIATE")
            blocker.execute(
                "INSERT INTO messages (ts, channel, sender, content, source) "
                "VALUES (1, 'ch', 's', 'block', 'test')"
            )

            # Release the lock when the first retry fires.  The patched
            # sleep runs in the worker thread (via to_thread), so we
            # issue ROLLBACK there — the lock is released before the
            # retry sleep finishes, and the next attempt succeeds.
            released = threading.Event()
            _real_sleep = time.sleep

            def _releasing_sleep(secs):
                if not released.is_set():
                    blocker.execute("ROLLBACK")
                    released.set()
                _real_sleep(secs)

            with patch("database.time.sleep", side_effect=_releasing_sleep):
                msg_id = await asyncio.to_thread(
                    insert_message,
                    db_cfg,
                    ts=100,
                    channel="test",
                    sender="tester",
                    content="retry works",
                    source="test",
                )

            self.assertTrue(released.is_set())
            self.assertIsInstance(msg_id, int)
            self.assertGreater(msg_id, 0)

            # Verify the row actually landed
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM messages WHERE id=?", (msg_id,)
            ).fetchone()
            conn.close()
            self.assertIsNotNone(row)
            self.assertEqual(row["content"], "retry works")


# ---------------------------------------------------------------------------
# Claim C: _executor_db logs exceptions instead of swallowing them
# ---------------------------------------------------------------------------

class TestExecutorDbLogsErrors(unittest.IsolatedAsyncioTestCase):
    """C: _executor_db surfaces exceptions via done-callback logging."""

    async def test_executor_db_logs_exception(self):
        def bad_fn():
            raise RuntimeError("test explosion")

        with self.assertLogs("mesh_bot", level="ERROR") as cm:
            _executor_db(bad_fn)
            # Give the executor thread time to run and the done-callback
            # to fire on the event loop.
            await asyncio.sleep(0.2)

        matched = [line for line in cm.output if "executor:db_error" in line]
        self.assertTrue(matched, f"Expected executor:db_error in logs, got: {cm.output}")
        self.assertIn("test explosion", matched[0])


class TestExecutorDbSuccessNoLog(unittest.IsolatedAsyncioTestCase):
    """C (supplementary): Successful calls do not emit error logs."""

    async def test_executor_db_no_log_on_success(self):
        results = []

        def good_fn():
            results.append("ok")

        with self.assertNoLogs("mesh_bot", level="ERROR"):
            _executor_db(good_fn)
            await asyncio.sleep(0.2)
        self.assertEqual(results, ["ok"])


if __name__ == "__main__":
    unittest.main()
