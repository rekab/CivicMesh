"""Unit + integration tests for diagnostics/mesh-sim/inject.py.

Covers:
  - Duration parser accept/reject lists from the README.
  - Scenario validation: required fields, unknown fields, source allowlist,
    channel-not-in-config warning, length caps.
  - End-to-end injection: rows land in the messages table with the right
    timestamps; sidecar tracks ids.
  - --replace-injected deletes only sidecar-recorded rows; a separately
    inserted real-radio row survives.
  - The script refuses to run when [diagnostics] enabled = false.
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path

# The injector lives at diagnostics/mesh-sim/inject.py — the hyphen in the
# directory name means we can't `import diagnostics.mesh_sim.inject` the
# normal way. Load the module by path, once, for the whole test module.
_REPO_ROOT = Path(__file__).resolve().parents[1]
_INJECT_PATH = _REPO_ROOT / "diagnostics" / "mesh-sim" / "inject.py"


def _load_inject():
    spec = importlib.util.spec_from_file_location("mesh_sim_inject", _INJECT_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


inject = _load_inject()


# ---------------------------------------------------------------------------
# Shared config/db fixture helpers (mirrors test_external_display_api.py).
# ---------------------------------------------------------------------------


_BASE_NETWORK = {
    "ip": "10.0.0.1",
    "subnet_cidr": "10.0.0.0/24",
    "iface": "wlan0",
    "country_code": "US",
    "dhcp_range_start": "10.0.0.10",
    "dhcp_range_end": "10.0.0.250",
    "dhcp_lease": "15m",
}


def _render_test_config(db_path: str, sections: dict) -> str:
    lines = [f'db_path = "{db_path}"', ""]
    for section, fields in sections.items():
        lines.append(f"[{section}]")
        for k, v in fields.items():
            if isinstance(v, str):
                lines.append(f'{k} = "{v}"')
            elif isinstance(v, bool):
                lines.append(f"{k} = {'true' if v else 'false'}")
            elif isinstance(v, list):
                inner = ", ".join(f'"{x}"' for x in v)
                lines.append(f"{k} = [{inner}]")
            else:
                lines.append(f"{k} = {v}")
        lines.append("")
    return "\n".join(lines)


def _base_sections(tmpdir: str, *, diagnostics_enabled: bool) -> dict:
    return {
        "node":     {"site_name": "TestHub", "callsign": "test1"},
        "network":  dict(_BASE_NETWORK),
        "ap":       {"ssid": "CivicMesh-Test", "channel": 6},
        "channels": {"names": ["#civicmesh", "#testing"]},
        "local":    {"names": ["#local"]},
        "web":      {"port": 8080, "portal_aliases": []},
        "logging":  {"log_dir": os.path.join(tmpdir, "logs"),
                     "log_level": "WARNING"},
        "diagnostics": {"enabled": diagnostics_enabled},
    }


def _make_test_env(diagnostics_enabled: bool = True):
    """Create a temp dir with config.toml + an initialized DB. Returns the
    tmpdir path so the caller is responsible for cleanup."""
    from config import load_config
    from database import DBConfig, init_db

    tmpdir = tempfile.mkdtemp(prefix="civicmesh_meshsim_test_")
    db_path = os.path.join(tmpdir, "test.db")
    cfg_path = os.path.join(tmpdir, "config.toml")
    with open(cfg_path, "w") as f:
        f.write(_render_test_config(
            db_path, _base_sections(tmpdir, diagnostics_enabled=diagnostics_enabled)
        ))
    cfg = load_config(cfg_path)
    db_cfg = DBConfig(path=cfg.db_path)
    init_db(db_cfg)
    return tmpdir, cfg_path, cfg, db_cfg


def _write_scenario(tmpdir: str, doc: dict) -> str:
    p = os.path.join(tmpdir, "scenario.json")
    with open(p, "w") as f:
        json.dump(doc, f)
    return p


def _reset_sidecar():
    """Clear the sidecar so tests don't pollute each other. The sidecar is
    a single file in the tool's directory, so it's shared across all
    inject.py invocations in the test process — including across tests in
    this file."""
    p = inject._sidecar_path()
    if p.exists():
        p.unlink()


# ---------------------------------------------------------------------------
# Duration parser
# ---------------------------------------------------------------------------


class TestDurationParser(unittest.TestCase):
    def test_accepts_listed_forms(self):
        cases = {
            "-1h": -3600,
            "-90m": -5400,
            "-1h30m": -5400,
            "+5m": 300,
            "5m": 300,
            "0s": 0,
            "0": 0,
            "-0": 0,
            "+0": 0,
            "-0s": 0,
            "-1h30m45s": -(3600 + 30 * 60 + 45),
            "+2h": 7200,
        }
        for s, expected in cases.items():
            with self.subTest(s=s):
                self.assertEqual(inject.parse_duration(s), expected)

    def test_rejects_listed_forms(self):
        bad = [
            "1.5h",
            "1h 30m",
            "30",
            "-1m30h",
            "-h",
            "",
            " ",
            "1d",
            "h",
            "abc",
            "-",
            "+",
        ]
        for s in bad:
            with self.subTest(s=s):
                with self.assertRaises(inject.BadDuration):
                    inject.parse_duration(s)

    def test_non_string_rejected(self):
        with self.assertRaises(inject.BadDuration):
            inject.parse_duration(30)  # type: ignore[arg-type]
        with self.assertRaises(inject.BadDuration):
            inject.parse_duration(None)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Scenario validation (no DB writes; just exercises validate_scenario)
# ---------------------------------------------------------------------------


class TestScenarioValidation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmpdir, cls.cfg_path, cls.cfg, cls.db_cfg = _make_test_env()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmpdir, ignore_errors=True)
        _reset_sidecar()

    def _doc(self, **msg_overrides):
        msg = {
            "channel": "#civicmesh",
            "sender": "alice",
            "body": "hi",
            "ts_offset": "0",
        }
        msg.update(msg_overrides)
        return {"name": "t", "messages": [msg]}

    def test_unknown_top_level_field_rejected(self):
        doc = self._doc()
        doc["extra"] = "nope"
        with self.assertRaises(inject.ScenarioError) as cm:
            inject.validate_scenario(doc, self.cfg)
        self.assertIn("extra", str(cm.exception))

    def test_unknown_per_message_field_rejected(self):
        doc = self._doc(timezone="PST")
        with self.assertRaises(inject.ScenarioError) as cm:
            inject.validate_scenario(doc, self.cfg)
        # The error must name the offending field by name.
        self.assertIn("timezone", str(cm.exception))

    def test_missing_required_field_rejected(self):
        doc = {"name": "t", "messages": [{"channel": "#civicmesh"}]}
        with self.assertRaises(inject.ScenarioError) as cm:
            inject.validate_scenario(doc, self.cfg)
        msg = str(cm.exception)
        self.assertIn("sender", msg)
        self.assertIn("body", msg)
        self.assertIn("ts_offset", msg)

    def test_source_not_in_allowlist_rejected(self):
        doc = self._doc(source="lorawan")
        with self.assertRaises(inject.ScenarioError) as cm:
            inject.validate_scenario(doc, self.cfg)
        self.assertIn("lorawan", str(cm.exception))

    def test_channel_not_in_config_warns_but_passes(self):
        doc = self._doc(channel="#not-configured")
        buf = io.StringIO()
        with redirect_stderr(buf):
            result = inject.validate_scenario(doc, self.cfg)
        self.assertEqual(len(result), 1)
        self.assertIn("#not-configured", buf.getvalue())
        self.assertIn("warning", buf.getvalue().lower())

    def test_oversized_sender_rejected(self):
        # cfg.limits.name_max_chars defaults to 12 in load_config.
        doc = self._doc(sender="a" * 13)
        with self.assertRaises(inject.ScenarioError) as cm:
            inject.validate_scenario(doc, self.cfg)
        self.assertIn("name_max_chars", str(cm.exception))

    def test_oversized_body_rejected(self):
        # Default message_max_chars is 200 per the loader; cap our scenario
        # against whatever the loaded cfg says rather than hardcoding 100.
        doc = self._doc(body="x" * (self.cfg.limits.message_max_chars + 1))
        with self.assertRaises(inject.ScenarioError) as cm:
            inject.validate_scenario(doc, self.cfg)
        self.assertIn("message_max_chars", str(cm.exception))

    def test_pinned_must_be_bool(self):
        doc = self._doc(pinned="yes")
        with self.assertRaises(inject.ScenarioError) as cm:
            inject.validate_scenario(doc, self.cfg)
        self.assertIn("pinned", str(cm.exception))


# ---------------------------------------------------------------------------
# End-to-end injection through main()
# ---------------------------------------------------------------------------


def _count_messages(db_path: str) -> int:
    # sqlite3's `with` is a transaction context, not a connection one — it
    # doesn't close the connection on exit. Explicit close to keep
    # ResourceWarnings out of the test output.
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    finally:
        conn.close()


def _row(db_path: str, msg_id: int) -> dict:
    conn = sqlite3.connect(db_path)
    try:
        conn.row_factory = sqlite3.Row
        return dict(conn.execute(
            "SELECT * FROM messages WHERE id=?", (msg_id,)
        ).fetchone())
    finally:
        conn.close()


class TestInjectMain(unittest.TestCase):
    def setUp(self):
        _reset_sidecar()
        self.tmpdir, self.cfg_path, self.cfg, self.db_cfg = _make_test_env()

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        _reset_sidecar()

    def test_three_message_scenario_inserts_three_rows(self):
        scenario = {
            "name": "three-msg",
            "messages": [
                {"channel": "#civicmesh", "sender": "a", "body": "x",
                 "ts_offset": "-1h"},
                {"channel": "#civicmesh", "sender": "b", "body": "y",
                 "ts_offset": "-30m"},
                {"channel": "#testing",   "sender": "c", "body": "z",
                 "ts_offset": "0"},
            ],
        }
        path = _write_scenario(self.tmpdir, scenario)
        # Fixed anchor in UTC so we can assert exact timestamps.
        anchor = "2026-05-16T12:00:00+00:00"
        anchor_epoch = inject.parse_anchor(anchor)
        rc = inject.main(["--config", self.cfg_path, "--anchor", anchor, path])
        self.assertEqual(rc, 0)
        self.assertEqual(_count_messages(self.db_cfg.path), 3)

        # Pull the rows and verify timestamps land at anchor + offset.
        conn = sqlite3.connect(self.db_cfg.path)
        try:
            conn.row_factory = sqlite3.Row
            rows = list(conn.execute(
                "SELECT ts, channel, sender, content FROM messages ORDER BY id"
            ))
        finally:
            conn.close()
        self.assertEqual(rows[0]["ts"], anchor_epoch - 3600)
        self.assertEqual(rows[1]["ts"], anchor_epoch - 1800)
        self.assertEqual(rows[2]["ts"], anchor_epoch)
        self.assertEqual(rows[0]["channel"], "#civicmesh")
        self.assertEqual(rows[2]["channel"], "#testing")
        self.assertEqual(rows[0]["sender"], "a")

    def test_unknown_channel_warns_but_inserts(self):
        scenario = {
            "name": "ch-warn",
            "messages": [
                {"channel": "#nope", "sender": "a", "body": "x",
                 "ts_offset": "0"},
            ],
        }
        path = _write_scenario(self.tmpdir, scenario)
        buf = io.StringIO()
        with redirect_stderr(buf):
            rc = inject.main(["--config", self.cfg_path, path])
        self.assertEqual(rc, 0)
        self.assertEqual(_count_messages(self.db_cfg.path), 1)
        self.assertIn("#nope", buf.getvalue())

    def test_bad_source_hard_error_no_rows_inserted(self):
        scenario = {
            "name": "bad-src",
            "messages": [
                {"channel": "#civicmesh", "sender": "a", "body": "x",
                 "ts_offset": "0", "source": "lorawan"},
            ],
        }
        path = _write_scenario(self.tmpdir, scenario)
        buf = io.StringIO()
        with redirect_stderr(buf):
            rc = inject.main(["--config", self.cfg_path, path])
        self.assertNotEqual(rc, 0)
        self.assertEqual(_count_messages(self.db_cfg.path), 0)
        self.assertIn("lorawan", buf.getvalue())

    def test_unknown_field_names_offender(self):
        scenario = {
            "name": "bad-field",
            "messages": [
                {"channel": "#civicmesh", "sender": "a", "body": "x",
                 "ts_offset": "0", "priority": "high"},
            ],
        }
        path = _write_scenario(self.tmpdir, scenario)
        buf = io.StringIO()
        with redirect_stderr(buf):
            rc = inject.main(["--config", self.cfg_path, path])
        self.assertNotEqual(rc, 0)
        self.assertEqual(_count_messages(self.db_cfg.path), 0)
        self.assertIn("priority", buf.getvalue())

    def test_pinned_message_gets_pin_columns_set(self):
        scenario = {
            "name": "pin-test",
            "messages": [
                {"channel": "#civicmesh", "sender": "a", "body": "x",
                 "ts_offset": "0", "pinned": True},
                {"channel": "#civicmesh", "sender": "b", "body": "y",
                 "ts_offset": "-1m", "pinned": True},
                {"channel": "#civicmesh", "sender": "c", "body": "z",
                 "ts_offset": "-2m"},  # unpinned
            ],
        }
        path = _write_scenario(self.tmpdir, scenario)
        rc = inject.main(["--config", self.cfg_path, path])
        self.assertEqual(rc, 0)
        conn = sqlite3.connect(self.db_cfg.path)
        try:
            conn.row_factory = sqlite3.Row
            rows = list(conn.execute(
                "SELECT sender, pinned, pin_order FROM messages ORDER BY id"
            ))
        finally:
            conn.close()
        # First two pinned, third not.
        self.assertEqual(rows[0]["pinned"], 1)
        self.assertEqual(rows[1]["pinned"], 1)
        self.assertEqual(rows[2]["pinned"], 0)
        # pin_order increments per channel.
        self.assertEqual(rows[0]["pin_order"], 1)
        self.assertEqual(rows[1]["pin_order"], 2)


# ---------------------------------------------------------------------------
# --replace-injected: must delete only what's in the sidecar.
# ---------------------------------------------------------------------------


class TestReplaceInjected(unittest.TestCase):
    def setUp(self):
        _reset_sidecar()
        self.tmpdir, self.cfg_path, self.cfg, self.db_cfg = _make_test_env()

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        _reset_sidecar()

    def test_replace_injected_preserves_real_radio_row(self):
        # 1. Insert a "real radio" row directly via database.insert_message,
        #    so it's NOT recorded in the sidecar.
        from database import insert_message
        real_id = insert_message(
            self.db_cfg,
            ts=1700000000,
            channel="#civicmesh",
            sender="radio",
            content="from-real-radio",
            source="mesh",
        )

        # 2. Run inject — appends sidecar-tracked rows.
        scenario = {
            "name": "first",
            "messages": [
                {"channel": "#civicmesh", "sender": "a", "body": "x",
                 "ts_offset": "0"},
                {"channel": "#civicmesh", "sender": "b", "body": "y",
                 "ts_offset": "-1m"},
            ],
        }
        path = _write_scenario(self.tmpdir, scenario)
        rc = inject.main(["--config", self.cfg_path, path])
        self.assertEqual(rc, 0)
        self.assertEqual(_count_messages(self.db_cfg.path), 3)  # 1 real + 2 injected

        # 3. Run --replace-injected with a different scenario. Sidecar rows
        #    should be deleted; the real-radio row must survive.
        scenario2 = {
            "name": "second",
            "messages": [
                {"channel": "#civicmesh", "sender": "c", "body": "z",
                 "ts_offset": "0"},
            ],
        }
        path2 = _write_scenario(self.tmpdir, scenario2)
        rc = inject.main(["--config", self.cfg_path, "--replace-injected", path2])
        self.assertEqual(rc, 0)

        # 4. Real row still there, plus 1 new injected row.
        self.assertEqual(_count_messages(self.db_cfg.path), 2)
        # The real-radio row must still be queryable by id.
        self.assertEqual(_row(self.db_cfg.path, real_id)["content"], "from-real-radio")

    def test_replace_injected_is_idempotent(self):
        scenario = {
            "name": "idem",
            "messages": [
                {"channel": "#civicmesh", "sender": "a", "body": "x",
                 "ts_offset": "0"},
            ],
        }
        path = _write_scenario(self.tmpdir, scenario)
        inject.main(["--config", self.cfg_path, path])
        first_count = _count_messages(self.db_cfg.path)
        # Running --replace-injected twice in succession with the same
        # scenario should land on the same row count both times.
        inject.main(["--config", self.cfg_path, "--replace-injected", path])
        after_first = _count_messages(self.db_cfg.path)
        inject.main(["--config", self.cfg_path, "--replace-injected", path])
        after_second = _count_messages(self.db_cfg.path)
        self.assertEqual(after_first, first_count)
        self.assertEqual(after_second, first_count)


# ---------------------------------------------------------------------------
# Refusal when [diagnostics] enabled = false
# ---------------------------------------------------------------------------


class TestDiagnosticsGate(unittest.TestCase):
    def setUp(self):
        _reset_sidecar()
        self.tmpdir, self.cfg_path, self.cfg, self.db_cfg = _make_test_env(
            diagnostics_enabled=False
        )

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        _reset_sidecar()

    def test_script_refuses_when_disabled(self):
        scenario = {
            "name": "gated",
            "messages": [
                {"channel": "#civicmesh", "sender": "a", "body": "x",
                 "ts_offset": "0"},
            ],
        }
        path = _write_scenario(self.tmpdir, scenario)
        buf = io.StringIO()
        with redirect_stderr(buf):
            rc = inject.main(["--config", self.cfg_path, path])
        self.assertNotEqual(rc, 0)
        self.assertEqual(_count_messages(self.db_cfg.path), 0)
        self.assertIn("diagnostics", buf.getvalue().lower())

    def test_script_refuses_wipe_when_disabled(self):
        buf = io.StringIO()
        with redirect_stderr(buf):
            rc = inject.main(["--config", self.cfg_path, "--wipe-all", "--yes"])
        self.assertNotEqual(rc, 0)


# ---------------------------------------------------------------------------
# --wipe-all
# ---------------------------------------------------------------------------


class TestWipeAll(unittest.TestCase):
    def setUp(self):
        _reset_sidecar()
        self.tmpdir, self.cfg_path, self.cfg, self.db_cfg = _make_test_env()

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        _reset_sidecar()

    def test_wipe_all_without_yes_refuses(self):
        buf = io.StringIO()
        with redirect_stderr(buf):
            rc = inject.main(["--config", self.cfg_path, "--wipe-all"])
        self.assertNotEqual(rc, 0)
        self.assertIn("--yes", buf.getvalue())

    def test_wipe_all_with_yes_clears_messages(self):
        from database import insert_message
        insert_message(
            self.db_cfg,
            ts=1700000000, channel="#civicmesh", sender="a",
            content="x", source="mesh",
        )
        insert_message(
            self.db_cfg,
            ts=1700000001, channel="#civicmesh", sender="b",
            content="y", source="mesh",
        )
        self.assertEqual(_count_messages(self.db_cfg.path), 2)

        buf = io.StringIO()
        with redirect_stderr(buf):
            rc = inject.main(["--config", self.cfg_path, "--wipe-all", "--yes"])
        self.assertEqual(rc, 0)
        self.assertEqual(_count_messages(self.db_cfg.path), 0)


if __name__ == "__main__":
    unittest.main()
