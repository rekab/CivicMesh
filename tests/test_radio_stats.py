"""Companion-radio telemetry: radio_samples round-trip + rts_pulse counter.

Covers the radio_samples schema, insert_radio_sample, and how compute_stats
(the /api/stats source) and compute_dm_stats (the DM `stats` source) surface
the latest radio scalars, the 1h RSSI series, and the windowed RTS-reset count.
"""

import os
import sqlite3
import tempfile
import unittest

from database import (
    DBConfig,
    compute_dm_stats,
    compute_stats,
    init_db,
    insert_radio_sample,
    insert_telemetry_event,
)


_NOW = 1_000_000


class _DBTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.cfg = DBConfig(path=self.tmp.name)
        init_db(self.cfg)

    def tearDown(self) -> None:
        os.unlink(self.tmp.name)

    def _radio(self, *, ts, **overrides) -> None:
        fields = dict(
            battery_mv=4100, radio_uptime_s=5 * 86400, err_bitmask=0,
            tx_queue_len=0, noise_floor=-115, last_rssi=-92, last_snr=6.0,
            tx_air_secs=120, rx_air_secs=340,
        )
        fields.update(overrides)
        insert_radio_sample(self.cfg, _ts_for_test=ts, **fields)


class SchemaTest(_DBTestBase):
    def test_radio_samples_columns(self) -> None:
        conn = sqlite3.connect(self.cfg.path)
        try:
            cols = [r[1] for r in conn.execute("PRAGMA table_info(radio_samples)").fetchall()]
        finally:
            conn.close()
        self.assertEqual(
            cols,
            ["ts", "battery_mv", "radio_uptime_s", "err_bitmask", "tx_queue_len",
             "noise_floor", "last_rssi", "last_snr", "tx_air_secs", "rx_air_secs"],
        )


class ComputeStatsRadioTest(_DBTestBase):
    def test_system_radio_latest_and_series(self) -> None:
        # Two samples in the last hour; the latest (highest ts) drives scalars.
        self._radio(ts=_NOW - 120, last_rssi=-100, last_snr=2.0, tx_queue_len=1)
        self._radio(ts=_NOW - 60, last_rssi=-92, last_snr=6.0, tx_queue_len=0)

        radio = compute_stats(self.cfg, _NOW)["system"]["radio"]
        self.assertEqual(radio["last_rssi"], -92)
        self.assertEqual(radio["last_snr"], 6.0)
        self.assertEqual(radio["tx_queue_len"], 0)
        self.assertEqual(radio["noise_floor"], -115)
        # Series are in ts order, oldest first.
        self.assertEqual(radio["rssi_1h_series"]["values"], [-100, -92])
        self.assertEqual(radio["rssi_1h_series"]["sample_sec"], 60)

    def test_radio_empty_when_no_samples(self) -> None:
        radio = compute_stats(self.cfg, _NOW)["system"]["radio"]
        self.assertIsNone(radio["last_rssi"])
        self.assertEqual(radio["rssi_1h_series"]["values"], [])


class RtsResetCounterTest(_DBTestBase):
    def _seed_pulses(self) -> None:
        # 1h:1, 24h:2, 7d:3 — one in each widening window.
        insert_telemetry_event(self.cfg, kind="rts_pulse", _ts_for_test=_NOW - 10)
        insert_telemetry_event(self.cfg, kind="rts_pulse", _ts_for_test=_NOW - 7200)
        insert_telemetry_event(self.cfg, kind="rts_pulse", _ts_for_test=_NOW - 200_000)
        # A different kind must not be counted.
        insert_telemetry_event(self.cfg, kind="recovery_succeeded", _ts_for_test=_NOW - 10)

    def test_compute_stats_rts_resets(self) -> None:
        self._seed_pulses()
        rts = compute_stats(self.cfg, _NOW)["rts_resets"]
        self.assertEqual(rts, {"hour": 1, "day": 2, "week": 3})

    def test_compute_dm_stats_rts_and_radio(self) -> None:
        self._seed_pulses()
        self._radio(ts=_NOW - 60, last_rssi=-92, last_snr=6.0, tx_queue_len=0)

        dm = compute_dm_stats(self.cfg, _NOW)
        self.assertEqual(dm["rts_resets"], {"1h": 1, "24h": 2, "7d": 3})
        self.assertEqual(dm["radio"]["last_rssi"], -92)
        self.assertEqual(dm["radio"]["last_snr"], 6.0)
        self.assertEqual(dm["radio"]["tx_queue_len"], 0)

    def test_dm_stats_radio_empty_when_no_samples(self) -> None:
        dm = compute_dm_stats(self.cfg, _NOW)
        self.assertEqual(dm["rts_resets"], {"1h": 0, "24h": 0, "7d": 0})
        self.assertIsNone(dm["radio"]["last_rssi"])


if __name__ == "__main__":
    unittest.main()
