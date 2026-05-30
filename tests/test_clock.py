"""CIV-99 clock-consensus tests.

Coverage map vs the plan's "Verification" section:

  Pure consensus math (clock.evaluate_consensus):
    - test_no_double_application_regression  (THE bug the prior plan would have shipped)
    - test_quorum_below_threshold / _at_threshold / _above
    - test_mac_dedupe_collapses_votes
    - test_median_robust_to_one_outlier
    - test_sanity_floor_rejection / _ceiling_rejection
    - test_first_correction_forward_only_allows_large_jump
    - test_first_correction_rejects_backward
    - test_post_first_correction_bidirectional_nudge
    - test_post_first_correction_rejects_beyond_cap

  /api/clock storage (record_clock_report):
    - test_record_clock_report_stores_raw_delta
    - test_record_clock_report_stamps_boot_id_and_epoch

  Eligibility (database.get_eligible_clock_reports):
    - test_eligibility_boot_id_excludes_prior_boot_via_equality
    - test_eligibility_vote_epoch_invalidation_after_external_step
    - test_eligibility_max_report_age_via_monotonic

  Cross-boot:
    - test_cross_boot_storage_hygiene_clears_old_boot_rows

  External step + first-correction-done:
    - test_external_step_writes_audit_and_bumps_epoch
    - test_first_correction_done_only_consensus_rows
    - test_first_correction_done_only_current_boot_epoch

  Admin command:
    - test_admin_happy_path_commits_offset_zero_and_bumps_epoch
    - test_admin_date_failure_rolls_back_db
    - test_admin_fake_hwclock_failure_still_commits_db

  Concurrency / busy_timeout:
    - test_busy_timeout_waits_under_contention

  Centralization invariant:
    - test_no_production_caller_passes_ts_to_wall_writers

  Sanity bound (server-side at /api/clock):
    - test_sanity_check_wall_accepts_stale_raw_clock_scenario
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

# Make repo importable when run via `python -m unittest tests.test_clock`.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import clock
from clock import (
    ConsensusConfig,
    Report,
    _reset_boot_id_cache_for_tests,
    evaluate_consensus,
    sanity_check_wall,
)
from database import (
    DBConfig,
    _connect,
    cross_boot_storage_hygiene,
    evaluate_and_maybe_apply_consensus,
    get_eligible_clock_reports,
    get_latest_clock_correction,
    has_consensus_correction_in_boot,
    init_db,
    record_clock_report,
    write_consensus_correction,
    write_external_step_correction,
)


# Defaults used by most consensus tests. Stays generous so tests are
# concerned with rule behavior, not threshold tuning.
_CFG = ConsensusConfig(
    quorum_min_cookies=3,
    sanity_floor_epoch=1_700_000_000,
    sanity_ceiling_epoch=2_000_000_000,
    max_nudge_sec=120,
)
# A `raw_now` value comfortably inside the sanity bounds for typical test
# offsets (~864_000 = 10 days). 1_750_000_000 ≈ June 2025; +/- 10 days
# stays well inside the configured floor/ceiling above.
_RAW_NOW = 1_750_000_000


def _votes_at(n: int, offset: int) -> list[Report]:
    """N distinct-cookie reports voting the same absolute offset."""
    return [
        Report(session_id=f"s{i}", mac_address=None, offset_vote_sec=offset)
        for i in range(n)
    ]


# ---------------------------------------------------------------------------
# Pure consensus math
# ---------------------------------------------------------------------------


class TestNoDoubleApplicationRegression(unittest.TestCase):
    """THE bug the prior plan would have shipped.

    Prior formula: candidate = current_offset + median(votes). Stored
    votes were already absolute offsets, so the second tick would
    double-apply.  Current formula: candidate = median(votes).
    """

    def test_tick_two_with_same_population_produces_zero_nudge(self):
        votes = _votes_at(3, 864_000)
        d1 = evaluate_consensus(
            votes,
            current_offset=0,
            raw_now=_RAW_NOW,
            first_correction_done=False,
            cfg=_CFG,
        )
        assert d1 is not None
        self.assertEqual(d1.new_offset, 864_000)
        self.assertEqual(d1.nudge, 864_000)

        # Tick two: same votes (phones still on the network), current_offset
        # now reflects the accepted decision.
        d2 = evaluate_consensus(
            votes,
            current_offset=d1.new_offset,
            raw_now=_RAW_NOW,
            first_correction_done=True,
            cfg=_CFG,
        )
        assert d2 is not None
        self.assertEqual(d2.new_offset, 864_000)
        self.assertEqual(d2.nudge, 0, "nudge must be 0 — no double-application")


class TestQuorum(unittest.TestCase):
    def test_below_threshold_rejects(self):
        self.assertIsNone(
            evaluate_consensus(
                _votes_at(2, 60),
                current_offset=0, raw_now=_RAW_NOW,
                first_correction_done=False, cfg=_CFG,
            )
        )

    def test_at_threshold_accepts(self):
        d = evaluate_consensus(
            _votes_at(3, 60),
            current_offset=0, raw_now=_RAW_NOW,
            first_correction_done=False, cfg=_CFG,
        )
        self.assertIsNotNone(d)


class TestMacDedupe(unittest.TestCase):
    def test_same_mac_collapses_to_one_vote(self):
        # Three sessions but two share a MAC: effective vote count = 2.
        reports = [
            Report(session_id="a", mac_address="aa:bb:cc:dd:ee:01", offset_vote_sec=60),
            Report(session_id="b", mac_address="aa:bb:cc:dd:ee:01", offset_vote_sec=60),
            Report(session_id="c", mac_address=None, offset_vote_sec=60),
        ]
        d = evaluate_consensus(
            reports,
            current_offset=0, raw_now=_RAW_NOW,
            first_correction_done=False, cfg=_CFG,
        )
        # 2 distinct effective voters < quorum_min_cookies=3 → reject.
        self.assertIsNone(d)

    def test_distinct_macs_count_independently(self):
        reports = [
            Report(session_id="a", mac_address="aa:bb:cc:dd:ee:01", offset_vote_sec=60),
            Report(session_id="b", mac_address="aa:bb:cc:dd:ee:02", offset_vote_sec=60),
            Report(session_id="c", mac_address="aa:bb:cc:dd:ee:03", offset_vote_sec=60),
        ]
        d = evaluate_consensus(
            reports,
            current_offset=0, raw_now=_RAW_NOW,
            first_correction_done=False, cfg=_CFG,
        )
        self.assertIsNotNone(d)


class TestMedianRobustness(unittest.TestCase):
    def test_one_outlier_doesnt_swing_the_median(self):
        reports = [
            Report(session_id=f"s{i}", mac_address=None, offset_vote_sec=60)
            for i in range(3)
        ]
        # Add a wildly-different fourth vote.
        reports.append(Report(session_id="liar", mac_address=None, offset_vote_sec=1_000_000))
        d = evaluate_consensus(
            reports,
            current_offset=0, raw_now=_RAW_NOW,
            first_correction_done=False, cfg=_CFG,
        )
        assert d is not None
        # Median of [60,60,60,1000000] = 60 (low half of pair = 60).
        self.assertEqual(d.new_offset, 60)


class TestSanityBound(unittest.TestCase):
    def test_below_floor_rejects(self):
        # candidate puts wall before sanity_floor_epoch.
        d = evaluate_consensus(
            _votes_at(3, _CFG.sanity_floor_epoch - _RAW_NOW - 1),
            current_offset=0, raw_now=_RAW_NOW,
            first_correction_done=False, cfg=_CFG,
        )
        self.assertIsNone(d)

    def test_above_ceiling_rejects(self):
        d = evaluate_consensus(
            _votes_at(3, _CFG.sanity_ceiling_epoch - _RAW_NOW + 1),
            current_offset=0, raw_now=_RAW_NOW,
            first_correction_done=False, cfg=_CFG,
        )
        self.assertIsNone(d)


class TestFirstCorrectionForwardOnly(unittest.TestCase):
    def test_allows_large_forward_jump(self):
        d = evaluate_consensus(
            _votes_at(3, 864_000),  # +10 days
            current_offset=0, raw_now=_RAW_NOW,
            first_correction_done=False, cfg=_CFG,
        )
        assert d is not None
        self.assertEqual(d.accept_reason, "first_correction_forward")
        self.assertEqual(d.new_offset, 864_000)

    def test_rejects_backward_jump(self):
        # current_offset positive; candidate smaller would move wall back.
        d = evaluate_consensus(
            _votes_at(3, 100),
            current_offset=200, raw_now=_RAW_NOW,
            first_correction_done=False, cfg=_CFG,
        )
        self.assertIsNone(d)


class TestPostFirstCorrectionBidirectional(unittest.TestCase):
    def test_small_backward_nudge_accepted_within_cap(self):
        d = evaluate_consensus(
            _votes_at(3, 864_000 - 60),  # candidate = 60 less than current
            current_offset=864_000, raw_now=_RAW_NOW,
            first_correction_done=True, cfg=_CFG,
        )
        assert d is not None
        self.assertEqual(d.nudge, -60)
        self.assertEqual(d.accept_reason, "nudge_within_cap")

    def test_small_forward_nudge_accepted_within_cap(self):
        d = evaluate_consensus(
            _votes_at(3, 864_000 + 60),
            current_offset=864_000, raw_now=_RAW_NOW,
            first_correction_done=True, cfg=_CFG,
        )
        assert d is not None
        self.assertEqual(d.nudge, 60)

    def test_nudge_beyond_cap_rejected(self):
        d = evaluate_consensus(
            _votes_at(3, 864_000 + _CFG.max_nudge_sec + 1),
            current_offset=864_000, raw_now=_RAW_NOW,
            first_correction_done=True, cfg=_CFG,
        )
        self.assertIsNone(d)


class TestSanityCheckWall(unittest.TestCase):
    def test_stale_raw_clock_scenario_accepts_correct_client_time(self):
        """The scenario the absolute ceiling exists for.

        If we used `raw_now + sanity_future_years` as the ceiling, a Pi
        whose raw clock is stuck in 2016 would reject any client
        reporting today's date — exactly the case the feature targets.
        """
        client_time = 1_750_000_000  # ~2025
        err = sanity_check_wall(
            client_time,
            floor_epoch=1_704_067_200,   # 2024-01-01
            ceiling_epoch=1_862_006_400, # 2029-01-01
        )
        self.assertIsNone(err)


# ---------------------------------------------------------------------------
# DB-level tests: per-tempfile fixture
# ---------------------------------------------------------------------------


class _TempDBTest(unittest.TestCase):
    def setUp(self):
        # Each test gets its own SQLite file so failures don't leak state.
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.db_cfg = DBConfig(path=self.path)
        init_db(self.db_cfg)
        # Seed one sessions row we'll attach reports to.
        with _connect(self.db_cfg) as conn:
            conn.execute(
                "INSERT INTO sessions(session_id, name, location, mac_address, "
                "fingerprint, created_ts, last_post_ts, post_count_hour) "
                "VALUES (?, ?, ?, ?, ?, ?, NULL, 0)",
                ("session-A", "tester", "loc", None, None, 0),
            )
            conn.execute(
                "INSERT INTO sessions(session_id, name, location, mac_address, "
                "fingerprint, created_ts, last_post_ts, post_count_hour) "
                "VALUES (?, ?, ?, ?, ?, ?, NULL, 0)",
                ("session-B", "tester", "loc", "aa:bb:cc:dd:ee:02", None, 0),
            )

    def tearDown(self):
        os.unlink(self.path)


# ---------------------------------------------------------------------------
# record_clock_report storage
# ---------------------------------------------------------------------------


class TestRecordClockReport(_TempDBTest):
    def test_stores_raw_delta_and_boot_id(self):
        raw = 1_700_000_000
        client = raw + 864_000
        vote = record_clock_report(
            self.db_cfg,
            session_id="session-A",
            client_time=client,
            boot_id="boot-xyz",
            _ts_for_test=raw,
            _mono_for_test=12.5,
        )
        self.assertEqual(vote, 864_000)
        with _connect(self.db_cfg) as conn:
            row = conn.execute(
                "SELECT clock_offset_vote_sec, clock_reported_system_ts, "
                "clock_report_mono, clock_report_boot_id, clock_vote_epoch "
                "FROM sessions WHERE session_id='session-A'"
            ).fetchone()
        self.assertEqual(row["clock_offset_vote_sec"], 864_000)
        self.assertEqual(row["clock_reported_system_ts"], raw)
        self.assertAlmostEqual(row["clock_report_mono"], 12.5)
        self.assertEqual(row["clock_report_boot_id"], "boot-xyz")
        self.assertEqual(row["clock_vote_epoch"], 0)  # initial epoch


# ---------------------------------------------------------------------------
# Eligibility — boot id, vote epoch, age
# ---------------------------------------------------------------------------


class TestEligibilityBootId(_TempDBTest):
    def test_prior_boot_id_excluded_via_pure_equality(self):
        """The monotonic counterexample (CIV-99 review item 1)."""
        # Stamp a report from an OLD boot at monotonic=5 (prior boot's
        # CLOCK_MONOTONIC value).
        record_clock_report(
            self.db_cfg, session_id="session-A",
            client_time=1_700_000_000, boot_id="old-boot",
            _ts_for_test=1_700_000_000, _mono_for_test=5.0,
        )
        # And a fresh-boot report at monotonic=10 — which under a
        # naive "max_mono > current_mono" filter would pass at
        # current_mono=10.
        record_clock_report(
            self.db_cfg, session_id="session-B",
            client_time=1_700_000_000, boot_id="new-boot",
            _ts_for_test=1_700_000_000, _mono_for_test=10.0,
        )
        # Touch sessions.created_ts so both pass the cookie-age gate.
        with _connect(self.db_cfg) as conn:
            conn.execute("UPDATE sessions SET created_ts = 0")
        eligible = get_eligible_clock_reports(
            self.db_cfg,
            boot_id="new-boot",
            vote_epoch=0,
            mono_now=20.0,
            max_report_age_sec=3600,
            wall_now_ts=1_700_001_000,
            min_cookie_age_sec=300,
        )
        ids = {r["session_id"] for r in eligible}
        self.assertEqual(ids, {"session-B"})


class TestEligibilityVoteEpoch(_TempDBTest):
    def test_external_step_bumps_epoch_and_excludes_old_reports(self):
        # Seed a report in the current epoch.
        record_clock_report(
            self.db_cfg, session_id="session-A",
            client_time=1_700_000_000, boot_id="boot-1",
            _ts_for_test=1_700_000_000, _mono_for_test=10.0,
        )
        with _connect(self.db_cfg) as conn:
            conn.execute("UPDATE sessions SET created_ts = 0")
        # Run external-step path: bumps epoch + NULLs session columns.
        # (NULL alone would exclude — but the test that follows then
        # re-seeds the report; the post-bump epoch is what excludes it.)
        write_external_step_correction(
            self.db_cfg, source_summary_json="{}",
        )
        # Re-seed using the SAME stale epoch via raw SQL to exercise
        # the epoch gate independently of the NULL sweep (belt-and-
        # suspenders verification per the plan).
        with _connect(self.db_cfg) as conn:
            conn.execute(
                "UPDATE sessions SET "
                "  clock_offset_vote_sec=864000, "
                "  clock_reported_system_ts=1700000000, "
                "  clock_report_mono=10.0, "
                "  clock_report_boot_id='boot-1', "
                "  clock_vote_epoch=0 "  # stale epoch
                "WHERE session_id='session-A'"
            )
        # Eligibility check at the new vote_epoch:
        eligible = get_eligible_clock_reports(
            self.db_cfg,
            boot_id="boot-1",
            vote_epoch=1,  # new
            mono_now=20.0,
            max_report_age_sec=3600,
            wall_now_ts=1_700_001_000,
            min_cookie_age_sec=300,
        )
        self.assertEqual(
            eligible, [],
            "epoch alone (not the NULL sweep) must filter the stale report",
        )


class TestEligibilityAgeViaMonotonic(_TempDBTest):
    def test_old_monotonic_report_excluded(self):
        record_clock_report(
            self.db_cfg, session_id="session-A",
            client_time=1_700_000_000, boot_id="boot-1",
            _ts_for_test=1_700_000_000, _mono_for_test=10.0,
        )
        with _connect(self.db_cfg) as conn:
            conn.execute("UPDATE sessions SET created_ts = 0")
        eligible = get_eligible_clock_reports(
            self.db_cfg,
            boot_id="boot-1",
            vote_epoch=0,
            mono_now=10.0 + 3600,  # 1h later in monotonic terms
            max_report_age_sec=1800,  # 30 min
            wall_now_ts=1_700_010_000,
            min_cookie_age_sec=300,
        )
        self.assertEqual(eligible, [])


# ---------------------------------------------------------------------------
# Cross-boot hygiene
# ---------------------------------------------------------------------------


class TestCrossBootHygiene(_TempDBTest):
    def test_cleans_prior_boot_rows_without_bumping_epoch(self):
        record_clock_report(
            self.db_cfg, session_id="session-A",
            client_time=1_700_000_000, boot_id="old-boot",
            _ts_for_test=1_700_000_000, _mono_for_test=5.0,
        )
        record_clock_report(
            self.db_cfg, session_id="session-B",
            client_time=1_700_000_000, boot_id="new-boot",
            _ts_for_test=1_700_000_000, _mono_for_test=6.0,
        )
        n = cross_boot_storage_hygiene(self.db_cfg, current_boot_id="new-boot")
        self.assertEqual(n, 1)
        with _connect(self.db_cfg) as conn:
            row_old = conn.execute(
                "SELECT clock_offset_vote_sec, clock_report_boot_id "
                "FROM sessions WHERE session_id='session-A'"
            ).fetchone()
            row_new = conn.execute(
                "SELECT clock_offset_vote_sec, clock_report_boot_id "
                "FROM sessions WHERE session_id='session-B'"
            ).fetchone()
            ve = conn.execute(
                "SELECT value FROM clock_state WHERE key='vote_epoch'"
            ).fetchone()
        self.assertIsNone(row_old["clock_offset_vote_sec"])
        self.assertIsNone(row_old["clock_report_boot_id"])
        self.assertEqual(row_new["clock_report_boot_id"], "new-boot")
        # boot-id alone gates eligibility; vote_epoch must NOT be bumped.
        self.assertEqual(ve["value"], "0")


# ---------------------------------------------------------------------------
# External-step writer
# ---------------------------------------------------------------------------


class TestExternalStepWriter(_TempDBTest):
    def test_offset_zeroed_epoch_bumped_audit_row_appended(self):
        # Pre-set an offset and a session report.
        with _connect(self.db_cfg) as conn:
            conn.execute(
                "UPDATE clock_state SET value='864000' WHERE key='offset_seconds'"
            )
        record_clock_report(
            self.db_cfg, session_id="session-A",
            client_time=1_700_000_000, boot_id="boot-1",
            _ts_for_test=1_700_000_000, _mono_for_test=5.0,
        )
        new_id = write_external_step_correction(
            self.db_cfg, source_summary_json='{"reason":"test"}',
        )
        self.assertGreater(new_id, 0)
        with _connect(self.db_cfg) as conn:
            os_val = conn.execute(
                "SELECT value FROM clock_state WHERE key='offset_seconds'"
            ).fetchone()["value"]
            ve = conn.execute(
                "SELECT value FROM clock_state WHERE key='vote_epoch'"
            ).fetchone()["value"]
            row_sess = conn.execute(
                "SELECT clock_offset_vote_sec FROM sessions WHERE session_id='session-A'"
            ).fetchone()
            audit = conn.execute(
                "SELECT trigger, offset_before_sec, offset_after_sec "
                "FROM clock_corrections WHERE id=?", (new_id,),
            ).fetchone()
        self.assertEqual(os_val, "0")
        self.assertEqual(ve, "1")  # bumped from 0
        self.assertIsNone(row_sess["clock_offset_vote_sec"])
        self.assertEqual(audit["trigger"], "external_step")
        self.assertEqual(audit["offset_before_sec"], 864000)
        self.assertEqual(audit["offset_after_sec"], 0)


# ---------------------------------------------------------------------------
# first_correction_done semantics
# ---------------------------------------------------------------------------


class TestFirstCorrectionDone(_TempDBTest):
    def test_consensus_row_counts(self):
        # Seed a consensus row in the current boot epoch.
        write_consensus_correction(
            self.db_cfg,
            new_offset=864_000,
            voter_count=3,
            median_offset_vote_sec=864_000,
            source_summary_json="{}",
        )
        # mono_now well above the applied_at_monotonic of the seed.
        # (Use a very large value so the row's mono is guaranteed <=.)
        big = time.monotonic() + 100
        self.assertTrue(has_consensus_correction_in_boot(self.db_cfg, mono_now=big))

    def test_external_step_row_does_not_count(self):
        write_external_step_correction(
            self.db_cfg, source_summary_json="{}",
        )
        big = time.monotonic() + 100
        self.assertFalse(has_consensus_correction_in_boot(self.db_cfg, mono_now=big))

    def test_consensus_row_from_prior_boot_does_not_count(self):
        # Seed a consensus row whose applied_at_monotonic is FAR in the
        # future relative to "now" — simulating a prior boot whose
        # CLOCK_MONOTONIC reached a high value before OS reboot reset it.
        write_consensus_correction(
            self.db_cfg, new_offset=864_000, voter_count=3,
            median_offset_vote_sec=864_000, source_summary_json="{}",
        )
        with _connect(self.db_cfg) as conn:
            conn.execute(
                "UPDATE clock_corrections SET applied_at_monotonic = ?",
                (time.monotonic() + 10_000,),  # "this boot was earlier"
            )
        # Current mono is much smaller than the persisted one ⇒ different boot.
        self.assertFalse(
            has_consensus_correction_in_boot(self.db_cfg, mono_now=time.monotonic())
        )


# ---------------------------------------------------------------------------
# Consensus-writer round trip + no-op suppression
# ---------------------------------------------------------------------------


class TestConsensusWriter(_TempDBTest):
    def test_does_not_bump_vote_epoch(self):
        # Initial vote_epoch=0.
        write_consensus_correction(
            self.db_cfg, new_offset=60, voter_count=3,
            median_offset_vote_sec=60, source_summary_json="{}",
        )
        with _connect(self.db_cfg) as conn:
            ve = conn.execute(
                "SELECT value FROM clock_state WHERE key='vote_epoch'"
            ).fetchone()["value"]
        self.assertEqual(ve, "0", "consensus must NOT bump vote_epoch")


# ---------------------------------------------------------------------------
# Atomicity regression: consensus tick + concurrent admin command.
# ---------------------------------------------------------------------------


class TestConsensusTickAtomicity(_TempDBTest):
    """The race that prompted evaluate_and_maybe_apply_consensus.

    The naive implementation read offset / vote_epoch / reports through
    SEPARATE connections, evaluated, then called write_consensus_correction
    on a FOURTH connection. The admin command's BEGIN EXCLUSIVE could
    commit between the read phase and the write phase — invalidating
    the votes — but the bot's write proceeded anyway with stale data
    and silently undid the admin correction.

    With the atomic helper, the bot's BEGIN IMMEDIATE blocks the
    admin's BEGIN EXCLUSIVE until the bot commits. Either:
      - Bot commits first: writes offset=864000, admin then overwrites
        with offset=0.
      - Admin commits first: bot sees NULL reports + new vote_epoch ⇒
        no eligible reports ⇒ no decision ⇒ no write.
    Either way, the final offset is 0 (admin's intent preserved).

    The test forces interleave with threading + a patch on
    clock.evaluate_consensus that signals an event AFTER the reads
    have happened but BEFORE the helper commits. While the helper
    holds the lock, the admin thread tries BEGIN EXCLUSIVE — that
    must block, not race ahead.
    """

    def _seed_eligible_reports(self):
        import threading as _t  # noqa: F401 (suppress flake if unused)
        for sid in ("sA", "sB", "sC"):
            with _connect(self.db_cfg) as conn:
                conn.execute(
                    "INSERT INTO sessions(session_id, name, location, mac_address, "
                    "fingerprint, created_ts, last_post_ts, post_count_hour) "
                    "VALUES (?, 'x', 'x', NULL, NULL, 0, NULL, 0)",
                    (sid,),
                )
            # Each vote is +864000 (raw_now=1_700_000_000, client=2_564_000_000)
            record_clock_report(
                self.db_cfg, session_id=sid,
                client_time=1_700_000_000 + 864_000, boot_id="boot-T",
                _ts_for_test=1_700_000_000, _mono_for_test=5.0,
            )

    def test_admin_landing_between_read_and_commit_does_not_lose(self):
        import threading
        import clock as _clock_mod
        self._seed_eligible_reports()

        # Synchronization. consensus_in_txn fires once the helper has
        # read all of offset/vote_epoch/reports and is about to commit;
        # the admin thread waits for it before attempting BEGIN EXCLUSIVE.
        consensus_in_txn = threading.Event()
        bot_result = {}
        admin_done = threading.Event()

        original_eval = _clock_mod.evaluate_consensus

        def signaling_eval(*a, **kw):
            decision = original_eval(*a, **kw)
            consensus_in_txn.set()
            # Hold here briefly so admin gets to attempt BEGIN EXCLUSIVE
            # WHILE the helper still holds the lock. Without atomicity,
            # admin would race ahead.
            time.sleep(0.4)
            return decision

        consensus_cfg = ConsensusConfig(
            quorum_min_cookies=3,
            sanity_floor_epoch=1_000_000_000,
            sanity_ceiling_epoch=3_000_000_000,
            max_nudge_sec=120,
        )

        def bot_thread():
            with patch("clock.evaluate_consensus", side_effect=signaling_eval):
                bot_result["applied"] = evaluate_and_maybe_apply_consensus(
                    self.db_cfg,
                    boot_id="boot-T",
                    max_report_age_sec=3600,
                    min_cookie_age_sec=0,
                    consensus_cfg=consensus_cfg,
                )

        def admin_thread():
            consensus_in_txn.wait(timeout=5)
            # Run an admin-equivalent under BEGIN EXCLUSIVE. With the
            # atomic helper holding BEGIN IMMEDIATE, this MUST block
            # until the helper commits.
            conn = _connect(self.db_cfg)
            try:
                conn.execute("BEGIN EXCLUSIVE")
                conn.execute(
                    "UPDATE clock_state SET value='0' WHERE key='offset_seconds'"
                )
                conn.execute(
                    "UPDATE clock_state SET value=CAST(CAST(value AS INTEGER)+1 AS TEXT) "
                    "WHERE key='vote_epoch'"
                )
                conn.execute(
                    "UPDATE sessions SET "
                    " clock_offset_vote_sec=NULL, clock_reported_system_ts=NULL, "
                    " clock_report_mono=NULL, clock_report_boot_id=NULL, "
                    " clock_vote_epoch=NULL"
                )
                conn.execute(
                    "INSERT INTO clock_corrections "
                    "(applied_at_monotonic, system_time_before, system_time_after, "
                    " offset_before_sec, offset_after_sec, trigger, "
                    " voter_count, median_offset_vote_sec, source_summary) "
                    "VALUES (?, ?, ?, ?, 0, 'admin', NULL, NULL, '{}')",
                    (time.monotonic(), 1_700_000_100, 1_700_000_100 + 864_000, 864_000),
                )
                conn.execute("COMMIT")
            finally:
                conn.close()
                admin_done.set()

        t_bot = threading.Thread(target=bot_thread)
        t_admin = threading.Thread(target=admin_thread)
        t_bot.start()
        t_admin.start()
        t_bot.join(timeout=10)
        t_admin.join(timeout=10)
        self.assertFalse(t_bot.is_alive(), "bot thread hung")
        self.assertFalse(t_admin.is_alive(), "admin thread hung")

        # Admin's intent must be preserved: final offset is 0, and
        # vote_epoch reflects the admin bump regardless of which thread
        # committed first.
        with _connect(self.db_cfg) as conn:
            offset = conn.execute(
                "SELECT value FROM clock_state WHERE key='offset_seconds'"
            ).fetchone()["value"]
            ve = conn.execute(
                "SELECT value FROM clock_state WHERE key='vote_epoch'"
            ).fetchone()["value"]
            triggers = [
                r["trigger"] for r in conn.execute(
                    "SELECT trigger FROM clock_corrections ORDER BY id"
                ).fetchall()
            ]

        self.assertEqual(offset, "0", "admin's correction must persist")
        self.assertGreaterEqual(int(ve), 1, "admin must have bumped vote_epoch")
        self.assertIn("admin", triggers, "admin row must be present")
        # Either the bot committed first (and admin overwrote) or admin
        # committed first (and the bot saw an empty eligibility set).
        # Both outcomes are acceptable; the invariant is offset == 0.

    def test_admin_first_makes_bot_a_noop(self):
        """If admin commits BEFORE the consensus tick starts, the tick
        sees the post-admin state (NULL reports, new vote_epoch) and
        writes nothing."""
        self._seed_eligible_reports()
        # Apply an admin-equivalent first.
        with _connect(self.db_cfg) as conn:
            conn.execute("BEGIN EXCLUSIVE")
            conn.execute(
                "UPDATE clock_state SET value='0' WHERE key='offset_seconds'"
            )
            conn.execute(
                "UPDATE clock_state SET value=CAST(CAST(value AS INTEGER)+1 AS TEXT) "
                "WHERE key='vote_epoch'"
            )
            conn.execute(
                "UPDATE sessions SET "
                " clock_offset_vote_sec=NULL, clock_reported_system_ts=NULL, "
                " clock_report_mono=NULL, clock_report_boot_id=NULL, "
                " clock_vote_epoch=NULL"
            )
            conn.execute(
                "INSERT INTO clock_corrections "
                "(applied_at_monotonic, system_time_before, system_time_after, "
                " offset_before_sec, offset_after_sec, trigger, "
                " voter_count, median_offset_vote_sec, source_summary) "
                "VALUES (?, ?, ?, ?, 0, 'admin', NULL, NULL, '{}')",
                (time.monotonic(), 1_700_000_100, 1_700_000_100 + 864_000, 864_000),
            )
            conn.execute("COMMIT")

        consensus_cfg = ConsensusConfig(
            quorum_min_cookies=3,
            sanity_floor_epoch=1_000_000_000,
            sanity_ceiling_epoch=3_000_000_000,
            max_nudge_sec=120,
        )
        applied = evaluate_and_maybe_apply_consensus(
            self.db_cfg,
            boot_id="boot-T",
            max_report_age_sec=3600,
            min_cookie_age_sec=0,
            consensus_cfg=consensus_cfg,
        )
        self.assertIsNone(
            applied,
            "consensus must see no eligible reports after admin's NULL+epoch bump",
        )
        # And the offset must remain 0.
        with _connect(self.db_cfg) as conn:
            offset = conn.execute(
                "SELECT value FROM clock_state WHERE key='offset_seconds'"
            ).fetchone()["value"]
        self.assertEqual(offset, "0")


# ---------------------------------------------------------------------------
# civicmesh-set-clock end-to-end (subprocess-stubbed `date` and
# `fake-hwclock`).
# ---------------------------------------------------------------------------


class _AdminCommandTest(_TempDBTest):
    """Drives civicmesh._cmd_set_clock with stubbed subprocess calls."""

    def _seed_offset(self, offset: int) -> None:
        with _connect(self.db_cfg) as conn:
            conn.execute(
                "UPDATE clock_state SET value=? WHERE key='offset_seconds'",
                (str(offset),),
            )

    def _run(self, *, date_ok: bool, fake_hwclock_ok: bool):
        # Make civicmesh importable and inject stubs for `date -s` and
        # `fake-hwclock save`. We patch subprocess.run inside civicmesh.
        import civicmesh
        from logger import LoggingConfig

        def fake_subprocess_run(args, check=False, capture_output=False, **kw):
            if args[0] == "date":
                rc = 0 if date_ok else 1
                if check and rc != 0:
                    raise subprocess.CalledProcessError(rc, args, b"", b"date stub fail")
                return subprocess.CompletedProcess(args, rc, b"", b"")
            if args[0] == "fake-hwclock":
                rc = 0 if fake_hwclock_ok else 1
                if check and rc != 0:
                    raise subprocess.CalledProcessError(
                        rc, args, b"", b"fake-hwclock stub fail",
                    )
                return subprocess.CompletedProcess(args, rc, b"", b"")
            raise AssertionError(f"unexpected subprocess: {args!r}")

        # Build a minimal Namespace the command consumes. The command
        # calls _resolve_config_path and load_config, so we feed it a real
        # toml on disk.
        cfg_path = tempfile.NamedTemporaryFile(
            mode="w", suffix=".toml", delete=False,
        )
        cfg_path.write(f"""\
db_path = "{self.path}"
[node]
site_name = "test"
callsign = "test"
[network]
ip = "10.0.0.1"
subnet_cidr = "10.0.0.0/24"
iface = "wlan0"
country_code = "US"
dhcp_range_start = "10.0.0.10"
dhcp_range_end = "10.0.0.250"
dhcp_lease = "15m"
[ap]
ssid = "test"
channel = 6
""")
        cfg_path.close()
        try:
            args = type("A", (), {})()
            args.config = cfg_path.name
            # Stub geteuid so the command thinks we're root.
            with patch("civicmesh._sub.run", side_effect=fake_subprocess_run) \
                    if False else patch.multiple(
                        "civicmesh",
                        load_config=civicmesh.load_config,
                    ):
                pass
            # Patch the actual things we need.
            with patch("subprocess.run", side_effect=fake_subprocess_run), \
                 patch("os.geteuid", return_value=0):
                try:
                    civicmesh._cmd_set_clock(args)
                except SystemExit as e:
                    return int(e.code) if isinstance(e.code, int) else 0
        finally:
            os.unlink(cfg_path.name)
        return 0

    def _read_offset_and_epoch(self):
        with _connect(self.db_cfg) as conn:
            ofs = conn.execute(
                "SELECT value FROM clock_state WHERE key='offset_seconds'"
            ).fetchone()["value"]
            ve = conn.execute(
                "SELECT value FROM clock_state WHERE key='vote_epoch'"
            ).fetchone()["value"]
            audits = conn.execute(
                "SELECT trigger, source_summary FROM clock_corrections ORDER BY id"
            ).fetchall()
        return ofs, ve, [dict(a) for a in audits]


class TestAdminHappyPath(_AdminCommandTest):
    def test_commits_offset_zero_bumps_epoch_writes_admin_row(self):
        self._seed_offset(864_000)
        # Mark a stale clock report — it must be NULLed on success.
        record_clock_report(
            self.db_cfg, session_id="session-A",
            client_time=1_700_000_000, boot_id="boot-1",
            _ts_for_test=1_700_000_000, _mono_for_test=5.0,
        )
        rc = self._run(date_ok=True, fake_hwclock_ok=True)
        self.assertEqual(rc, 0)
        ofs, ve, audits = self._read_offset_and_epoch()
        self.assertEqual(ofs, "0")
        self.assertEqual(ve, "1")
        self.assertEqual(len(audits), 1)
        self.assertEqual(audits[0]["trigger"], "admin")
        self.assertIn("fake_hwclock_save_failed", audits[0]["source_summary"])
        # NULL sweep verification.
        with _connect(self.db_cfg) as conn:
            row = conn.execute(
                "SELECT clock_offset_vote_sec FROM sessions WHERE session_id='session-A'"
            ).fetchone()
        self.assertIsNone(row["clock_offset_vote_sec"])


class TestAdminDateFails(_AdminCommandTest):
    def test_date_failure_rolls_back_db(self):
        self._seed_offset(864_000)
        rc = self._run(date_ok=False, fake_hwclock_ok=True)
        self.assertNotEqual(rc, 0)
        ofs, ve, audits = self._read_offset_and_epoch()
        self.assertEqual(ofs, "864000", "offset must be unchanged after date failure")
        self.assertEqual(ve, "0")
        self.assertEqual(audits, [])


class TestAdminFakeHwclockFails(_AdminCommandTest):
    def test_db_still_commits_with_flag(self):
        """The critical correctness point of the fake-hwclock spec.

        If `date -s` succeeded but `fake-hwclock save` failed, the live
        system clock is now correct. Rolling back the DB would leave
        wall_now = jumped_clock + old_offset (double-corrected) until
        reboot. We commit the DB anyway and flag the failure.
        """
        self._seed_offset(864_000)
        rc = self._run(date_ok=True, fake_hwclock_ok=False)
        self.assertNotEqual(rc, 0, "must exit non-zero on fake-hwclock fail")
        ofs, ve, audits = self._read_offset_and_epoch()
        self.assertEqual(ofs, "0", "offset must be committed despite fake-hwclock fail")
        self.assertEqual(ve, "1", "vote_epoch must be bumped despite fake-hwclock fail")
        self.assertEqual(len(audits), 1)
        self.assertEqual(audits[0]["trigger"], "admin")
        self.assertIn(
            '"fake_hwclock_save_failed":true',
            audits[0]["source_summary"],
            "audit row must flag the failure for operator triage",
        )


# ---------------------------------------------------------------------------
# busy_timeout — concurrent writer waits instead of failing
# ---------------------------------------------------------------------------


class TestBusyTimeout(_TempDBTest):
    def test_second_writer_waits_rather_than_failing(self):
        # Hold a write lock from connection A.
        conn_a = sqlite3.connect(self.path, isolation_level=None, timeout=10.0)
        conn_a.execute("BEGIN IMMEDIATE")
        conn_a.execute(
            "INSERT INTO sessions(session_id, name, location, mac_address, fingerprint, "
            "created_ts, last_post_ts, post_count_hour) "
            "VALUES ('lock-test', 'x', 'x', NULL, NULL, 0, NULL, 0)"
        )
        # From connection B, try BEGIN IMMEDIATE — it should wait, then
        # succeed once A commits. We measure that it waits at LEAST a
        # moderate amount; the test uses a short A-side hold to keep
        # runtime fast.
        import threading
        results = {}

        def b_thread():
            # _connect installs PRAGMA busy_timeout=10000 (10s).
            conn_b = _connect(self.db_cfg)
            t0 = time.monotonic()
            try:
                conn_b.execute("BEGIN IMMEDIATE")
                conn_b.execute("COMMIT")
                results["elapsed"] = time.monotonic() - t0
                results["ok"] = True
            except sqlite3.OperationalError as e:
                results["error"] = str(e)
                results["ok"] = False
            finally:
                conn_b.close()

        t = threading.Thread(target=b_thread)
        t.start()
        time.sleep(0.3)  # B is now waiting on the busy_timeout
        conn_a.execute("COMMIT")
        conn_a.close()
        t.join(timeout=5)
        self.assertTrue(results.get("ok"), f"B failed: {results.get('error')}")
        # The waited time should be >= our sleep above.
        self.assertGreaterEqual(results["elapsed"], 0.25)


# ---------------------------------------------------------------------------
# Centralization invariant — production source must not pass `ts=` to the
# wall-writer helpers.
# ---------------------------------------------------------------------------


class TestCentralizationInvariant(unittest.TestCase):
    """Catches a future change that re-threads a precomputed `ts` through a
    production caller. The wall writers don't accept `ts=` at all; the
    test scans the production .py files for ts= against any *_wall name."""

    _WALL_FUNCS = (
        "insert_message_wall",
        # queue_outbox_and_message accepts _ts_for_test (test escape hatch)
        # but production callers must never pass it.
    )
    _PRODUCTION_FILES = (
        "web_server.py",
        "mesh_bot.py",
        "recovery.py",
        "telemetry.py",
    )

    def test_no_ts_kwarg_to_wall_writers(self):
        for fname in self._PRODUCTION_FILES:
            path = _REPO_ROOT / fname
            text = path.read_text()
            for wall in self._WALL_FUNCS:
                # Find `wall(...) and look for ts= within its call. Simple
                # textual scan good enough; production calls fit on a few
                # lines.
                idx = 0
                while True:
                    pos = text.find(f"{wall}(", idx)
                    if pos < 0:
                        break
                    # Read until matching close paren (simple counter).
                    depth = 0
                    end = pos + len(wall) + 1
                    while end < len(text):
                        c = text[end]
                        if c == "(":
                            depth += 1
                        elif c == ")":
                            if depth == 0:
                                break
                            depth -= 1
                        end += 1
                    call_text = text[pos:end + 1]
                    self.assertNotIn(
                        "ts=", call_text,
                        f"{fname}: {wall} call passes ts=. The wall writers "
                        "stamp ts inside their own write txn; passing a "
                        "precomputed ts reintroduces the admin-command race.",
                    )
                    idx = end + 1

    def test_no_ts_for_test_in_production(self):
        for fname in self._PRODUCTION_FILES:
            path = _REPO_ROOT / fname
            text = path.read_text()
            self.assertNotIn(
                "_ts_for_test=", text,
                f"{fname}: production code uses _ts_for_test=, which is a "
                "test-only escape hatch on the wall writers. Remove it.",
            )


if __name__ == "__main__":
    unittest.main()
