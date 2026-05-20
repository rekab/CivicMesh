"""HTTP integration tests for /api/external-display/state.

The endpoint is always registered. Behavior is gated by
external_display.enabled in config.toml:
  - disabled (or section absent): 404 with JSON {"error": "not found"}
  - enabled: 200 with the v2 payload (api_version=2)

See docs/external-display-api.md for the full contract.
Pure normalization-helper unit tests live in test_external_display_normalize.py.
"""

import http.client
import http.server
import json
import logging
import os
import shutil
import sqlite3
import tempfile
import threading
import time
import unittest

from config import load_config
from database import DBConfig, init_db, insert_message
from web_server import CivicMeshHandler


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
    """Inline TOML render. db_path is emitted as a top-level key BEFORE any
    section header so it cannot get bound to a trailing section."""
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


class _DisplayServer:
    """Minimal stand-in for web_server._Server on an ephemeral port."""

    def __init__(self, cfg, db_cfg, tmpdir, log_name):
        log = logging.getLogger(log_name)
        log.addHandler(logging.NullHandler())

        static_dir = os.path.join(
            os.path.dirname(os.path.abspath(os.path.dirname(__file__))), "static"
        )

        class _S(http.server.ThreadingHTTPServer):
            def __init__(self, *a, **kw):
                super().__init__(*a, **kw)
                self.cfg = cfg
                self.db_cfg = db_cfg
                self.log = log
                self.sec = log
                self.feedback_path = os.path.join(tmpdir, "feedback.jsonl")

        def handler(*a, **kw):
            return CivicMeshHandler(*a, directory=static_dir, **kw)

        self.srv = _S(("127.0.0.1", 0), handler)
        self.port = self.srv.server_port
        self.thread = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()
        self.thread.join(timeout=2)


def _base_sections(tmpdir: str) -> dict:
    return {
        "node":     {"site_name": "TestHub", "callsign": "test1"},
        "network":  dict(_BASE_NETWORK),
        "ap":       {"ssid": "CivicMesh-Test", "channel": 6},
        "channels": {"names": ["#civicmesh-test"]},
        "web":      {"port": 8080, "portal_aliases": ["civicmesh.internal"]},
        "logging":  {"log_dir": os.path.join(tmpdir, "logs"),
                     "log_level": "WARNING"},
    }


def _bring_up(cls, sections: dict, log_name: str) -> None:
    cls.tmpdir = tempfile.mkdtemp(prefix="civicmesh_extdisp_test_")
    db_path = os.path.join(cls.tmpdir, "test.db")
    cfg_path = os.path.join(cls.tmpdir, "config.toml")
    with open(cfg_path, "w") as f:
        f.write(_render_test_config(db_path, sections))

    cls.cfg = load_config(cfg_path)
    cls.db_cfg = DBConfig(path=cls.cfg.db_path)
    assert cls.db_cfg.path.startswith(cls.tmpdir), (
        f"test DB escaped sandbox: {cls.db_cfg.path!r} "
        f"(expected to be under {cls.tmpdir!r})"
    )
    init_db(cls.db_cfg, log=logging.getLogger(log_name))
    cls.server = _DisplayServer(cls.cfg, cls.db_cfg, cls.tmpdir, log_name)


def _tear_down(cls) -> None:
    cls.server.close()
    shutil.rmtree(cls.tmpdir, ignore_errors=True)


def _get(port: int, path: str):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request("GET", path)
        resp = conn.getresponse()
        return resp.status, dict(resp.getheaders()), resp.read()
    finally:
        conn.close()


def _get_json(port: int, path: str):
    status, headers, body = _get(port, path)
    return status, headers, json.loads(body) if body else None


def _pin(db_path: str, msg_id: int, order: int) -> None:
    """Set pinned=1 and pin_order on a message row. insert_message doesn't
    expose these columns; tests need them directly to exercise the
    pinned-first ordering in the v2 payload."""
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE messages SET pinned=1, pin_order=? WHERE id=?",
            (order, msg_id),
        )


class TestExternalDisplayDisabled(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Deliberately omit [external_display] entirely — absent section
        # must default to disabled (the "most hubs" path).
        _bring_up(cls, _base_sections(tempfile.gettempdir()), "test_extdisp_off")

    @classmethod
    def tearDownClass(cls):
        _tear_down(cls)

    def test_section_absent_defaults_to_disabled(self):
        self.assertFalse(self.cfg.external_display.enabled)

    def test_returns_404_with_json_when_disabled(self):
        status, headers, body = _get(self.server.port, "/api/external-display/state")
        self.assertEqual(status, 404)
        self.assertIn("application/json", headers.get("Content-Type", ""))
        payload = json.loads(body)
        self.assertEqual(payload, {"error": "not found"})


class TestExternalDisplayEnabled(unittest.TestCase):
    """v2 payload assertions. Sets up a config with two local channels,
    two mesh channels, and a deliberate fixture mix per channel:
      - #local        — 2 pinned + 4 unpinned (tests pinned-first + 5-cap)
      - #hub-board    — 1 message (tests "channel has data")
      - #civicmesh-test — 6 unpinned (tests 5-cap + ts DESC)
      - #fremont      — empty (tests messages: [] not omitted)
    """

    @classmethod
    def setUpClass(cls):
        sections = _base_sections(tempfile.gettempdir())
        sections["node"] = {"site_name": "Greenwood Library", "callsign": "GWD"}
        sections["channels"] = {"names": ["#civicmesh-test", "#fremont"]}
        sections["local"] = {"names": ["#local", "#hub-board"]}
        sections["external_display"] = {"enabled": True}
        _bring_up(cls, sections, "test_extdisp_on")

        # --- #hub-board: one recent local message ---
        insert_message(
            cls.db_cfg,
            ts=1_700_000_500,
            channel="#hub-board",
            sender="alice",
            content="bulletin notice",
            source="local",
        )

        # --- #civicmesh-test: 20 mesh messages, varying ts ---
        # Insert in ts-ascending order; expect newest-first in payload.
        # Sized to exceed _PER_CHANNEL_LIMIT (15) so the cap-behavior
        # tests below actually exercise the slice.
        for i in range(20):
            insert_message(
                cls.db_cfg,
                ts=1_700_000_001 + i,
                channel="#civicmesh-test",
                sender=f"mesh-peer-{i}",
                content=f"mesh message {i}",
                source="mesh",
            )

        # --- #fremont: empty (no inserts) ---

        # --- #local: 4 unpinned, then 2 pinned (with pin_order 1, 2) ---
        unpinned_ids = []
        for i, ts in enumerate(
            [1_700_001_001, 1_700_001_002, 1_700_001_003, 1_700_001_004]
        ):
            mid = insert_message(
                cls.db_cfg,
                ts=ts,
                channel="#local",
                sender=f"local-user-{i}",
                content=f"local unpinned {i}",
                source="local",
            )
            unpinned_ids.append(mid)

        pin1 = insert_message(
            cls.db_cfg,
            ts=1_700_000_500,  # deliberately older than the unpinned ones
            channel="#local",
            sender="pinned-author-A",
            content="pinned first",
            source="local",
        )
        pin2 = insert_message(
            cls.db_cfg,
            ts=1_700_000_600,
            channel="#local",
            sender="pinned-author-B",
            content="pinned second",
            source="local",
        )
        _pin(cls.db_cfg.path, pin1, order=1)
        _pin(cls.db_cfg.path, pin2, order=2)
        cls.pin_ids = (pin1, pin2)
        cls.unpinned_ids = unpinned_ids

        # --- one message with text that exercises normalization ---
        cls.norm_msg_id = insert_message(
            cls.db_cfg,
            ts=1_700_000_700,
            channel="#hub-board",
            sender="ali\x00ce",
            content="Hello\n🚀\nworld",
            source="local",
        )

    @classmethod
    def tearDownClass(cls):
        _tear_down(cls)

    def _payload(self):
        status, headers, payload = _get_json(self.server.port, "/api/external-display/state")
        self.assertEqual(status, 200, f"unexpected status: {status}")
        self.assertIn("application/json", headers.get("Content-Type", ""))
        return payload

    def _channel(self, payload, name):
        for ch in payload["channels"]:
            if ch["name"] == name:
                return ch
        self.fail(f"channel {name!r} not in payload (have: {[c['name'] for c in payload['channels']]})")

    # --- top-level shape -------------------------------------------------

    def test_api_version_is_2(self):
        self.assertEqual(self._payload()["api_version"], 2)

    def test_server_time_is_int_and_recent(self):
        payload = self._payload()
        self.assertIsInstance(payload["server_time"], int)
        # Within a generous 60 s window of test-runner wall clock.
        self.assertAlmostEqual(payload["server_time"], int(time.time()), delta=60)

    def test_hub_mirrors_config(self):
        # callsign gets lowercased on load (config._validate_callsign).
        self.assertEqual(
            self._payload()["hub"],
            {"site_name": "Greenwood Library", "callsign": "gwd"},
        )

    # --- channels list ---------------------------------------------------

    def test_channels_ordered_local_then_mesh(self):
        names = [c["name"] for c in self._payload()["channels"]]
        self.assertEqual(
            names,
            ["#local", "#hub-board", "#civicmesh-test", "#fremont"],
        )

    def test_scope_values(self):
        channels = self._payload()["channels"]
        scopes = {c["name"]: c["scope"] for c in channels}
        self.assertEqual(scopes["#local"], "local")
        self.assertEqual(scopes["#hub-board"], "local")
        self.assertEqual(scopes["#civicmesh-test"], "mesh")
        self.assertEqual(scopes["#fremont"], "mesh")

    def test_empty_channel_has_messages_list(self):
        ch = self._channel(self._payload(), "#fremont")
        self.assertEqual(ch["messages"], [])

    # --- per-channel selection ------------------------------------------

    def test_per_channel_cap_at_15(self):
        ch = self._channel(self._payload(), "#civicmesh-test")
        self.assertEqual(len(ch["messages"]), 15)

    def test_messages_newest_first_by_ts(self):
        ch = self._channel(self._payload(), "#civicmesh-test")
        timestamps = [m["ts"] for m in ch["messages"]]
        self.assertEqual(timestamps, sorted(timestamps, reverse=True))
        # 20 inserted, cap 15 → the 5 oldest (ts=1_700_000_001..005) drop.
        for old_ts in range(1_700_000_001, 1_700_000_006):
            self.assertNotIn(old_ts, timestamps)
        # And the newest (ts=1_700_000_020) is present.
        self.assertIn(1_700_000_020, timestamps)

    def test_pinned_first_then_newest_unpinned(self):
        ch = self._channel(self._payload(), "#local")
        # 2 pinned + 4 unpinned, all under the per-channel cap of 15.
        self.assertEqual(len(ch["messages"]), 6)
        pin1, pin2 = self.pin_ids
        # Pinned come first, in pin_order ASC (1, 2).
        self.assertEqual(ch["messages"][0]["id"], pin1)
        self.assertEqual(ch["messages"][1]["id"], pin2)
        # Remaining slots are the unpinned, newest first.
        unpinned_in_payload = [m["id"] for m in ch["messages"][2:]]
        self.assertEqual(unpinned_in_payload, list(reversed(self.unpinned_ids)))

    # --- message field projection ---------------------------------------

    def test_message_keys_are_id_ts_ts_str_sender_body_only(self):
        ch = self._channel(self._payload(), "#hub-board")
        self.assertTrue(ch["messages"], "expected at least one message in #hub-board")
        for m in ch["messages"]:
            self.assertEqual(
                set(m.keys()), {"id", "ts", "ts_str", "sender", "body"}
            )

    def test_ts_str_formatted_in_configured_timezone(self):
        # ts=1_700_000_700 is 2023-11-14 22:25 UTC = 14:25 in America/
        # Los_Angeles (PST, UTC-8 — outside DST on that date). The test
        # config doesn't set node.timezone, so the LA default applies.
        ch = self._channel(self._payload(), "#hub-board")
        norm_row = next(m for m in ch["messages"] if m["id"] == self.norm_msg_id)
        self.assertEqual(norm_row["ts"], 1_700_000_700)
        self.assertEqual(norm_row["ts_str"], "14:25")

    def test_normalization_applied_to_payload(self):
        ch = self._channel(self._payload(), "#hub-board")
        norm_row = next(m for m in ch["messages"] if m["id"] == self.norm_msg_id)
        # NFKD+ASCII drops the rocket; the embedded newlines collapse to a
        # single space; the control char in the sender gets stripped.
        self.assertEqual(norm_row["body"], "Hello world")
        self.assertEqual(norm_row["sender"], "alice")


if __name__ == "__main__":
    unittest.main()
