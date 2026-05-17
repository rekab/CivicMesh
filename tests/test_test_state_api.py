"""HTTP integration tests for /api/_test/state and the read-site hooks.

Three slabs of coverage:

  1. Endpoint shape — GET/POST/DELETE semantics, validation, the diagnostics
     gate, no partial application on bad input.
  2. Override propagation — radio_status / recovery_state / last_seen_ts /
     server_time_skew_seconds actually flip the response payloads they're
     supposed to flip.
  3. **The byte-identical regression guard** — with no overrides set, /api/status
     and /api/external-display/state return exactly the keys they returned
     before this PR's plumbing. The override hook leaking into the no-override
     path is the single most expensive bug this whole PR can ship; this test
     is the trap for it.

Fixture style mirrors tests/test_external_display_api.py.
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
from database import DBConfig, init_db, upsert_status
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


def _base_sections(tmpdir: str, *, diagnostics_enabled: bool) -> dict:
    return {
        "node":     {"site_name": "TestHub", "callsign": "test1"},
        "network":  dict(_BASE_NETWORK),
        "ap":       {"ssid": "CivicMesh-Test", "channel": 6},
        "channels": {"names": ["#civicmesh-test"]},
        "local":    {"names": ["#local"]},
        "web":      {"port": 8080, "portal_aliases": ["civicmesh.internal"]},
        "logging":  {"log_dir": os.path.join(tmpdir, "logs"),
                     "log_level": "WARNING"},
        # The endpoint is gated by [diagnostics].enabled; per-test fixtures
        # flip this. External-display defaults to true here because the
        # propagation tests hit that endpoint to check server_time skew.
        "external_display": {"enabled": True},
        "diagnostics":      {"enabled": diagnostics_enabled},
    }


class _Server:
    """Stand-in for web_server._Server on an ephemeral port. MUST init
    _test_state_overrides because the POST handler writes to it
    unconditionally once the [diagnostics] gate is open."""

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
                # Production _Server initializes this; mirror it so the POST
                # handler can index into it.
                self._test_state_overrides = {}

        def handler(*a, **kw):
            return CivicMeshHandler(*a, directory=static_dir, **kw)

        self.srv = _S(("127.0.0.1", 0), handler)
        self.port = self.srv.server_port
        self.thread = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.thread.start()

    def reset_overrides(self):
        """Clear in-process state between tests within a class. Cheaper than
        spinning up a new server per test."""
        self.srv._test_state_overrides.clear()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()
        self.thread.join(timeout=2)


def _bring_up(cls, *, diagnostics_enabled: bool, log_name: str) -> None:
    cls.tmpdir = tempfile.mkdtemp(prefix="civicmesh_teststate_test_")
    db_path = os.path.join(cls.tmpdir, "test.db")
    cfg_path = os.path.join(cls.tmpdir, "config.toml")
    with open(cfg_path, "w") as f:
        f.write(_render_test_config(
            db_path, _base_sections(cls.tmpdir, diagnostics_enabled=diagnostics_enabled)
        ))
    cls.cfg = load_config(cfg_path)
    cls.db_cfg = DBConfig(path=cls.cfg.db_path)
    assert cls.db_cfg.path.startswith(cls.tmpdir), (
        f"test DB escaped sandbox: {cls.db_cfg.path!r} (expected under {cls.tmpdir!r})"
    )
    init_db(cls.db_cfg, log=logging.getLogger(log_name))
    cls.server = _Server(cls.cfg, cls.db_cfg, cls.tmpdir, log_name)


def _tear_down(cls) -> None:
    cls.server.close()
    shutil.rmtree(cls.tmpdir, ignore_errors=True)


def _request(port: int, method: str, path: str, body=None):
    """Generic helper: returns (status, headers_dict, raw_body_bytes)."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Content-Type": "application/json"} if data is not None else {}
        conn.request(method, path, body=data, headers=headers)
        resp = conn.getresponse()
        return resp.status, dict(resp.getheaders()), resp.read()
    finally:
        conn.close()


def _json_request(port: int, method: str, path: str, body=None):
    status, headers, raw = _request(port, method, path, body=body)
    payload = json.loads(raw) if raw else None
    return status, headers, payload


# ---------------------------------------------------------------------------
# Disabled gate — every method must 404
# ---------------------------------------------------------------------------


class TestDiagnosticsGateDisabled(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _bring_up(cls, diagnostics_enabled=False, log_name="teststate_off")

    @classmethod
    def tearDownClass(cls):
        _tear_down(cls)

    def test_get_returns_404(self):
        status, _, payload = _json_request(self.server.port, "GET", "/api/_test/state")
        self.assertEqual(status, 404)
        self.assertEqual(payload, {"error": "not found"})

    def test_post_returns_404(self):
        status, _, payload = _json_request(
            self.server.port, "POST", "/api/_test/state",
            body={"radio_status": "offline"},
        )
        self.assertEqual(status, 404)
        self.assertEqual(payload, {"error": "not found"})

    def test_delete_returns_404(self):
        status, _, payload = _json_request(self.server.port, "DELETE", "/api/_test/state")
        self.assertEqual(status, 404)
        self.assertEqual(payload, {"error": "not found"})

    def test_disabled_does_not_change_status_endpoint_shape(self):
        # Belt-and-suspenders: even with the gate off, /api/status must
        # behave exactly as before. The hooks are wired unconditionally
        # (they're cheap), but with no override store population they
        # must be no-ops.
        status, _, payload = _json_request(self.server.port, "GET", "/api/status")
        self.assertEqual(status, 200)
        # Empty branch (no mesh_bot row yet):
        self.assertEqual(payload["radio_status"], "needs_human")
        self.assertIsNone(payload["recovery_state"])
        self.assertFalse(payload["mesh_bot_seen"])


# ---------------------------------------------------------------------------
# Endpoint shape — GET / POST / DELETE semantics with the gate open
# ---------------------------------------------------------------------------


class TestEndpointShape(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _bring_up(cls, diagnostics_enabled=True, log_name="teststate_shape")

    @classmethod
    def tearDownClass(cls):
        _tear_down(cls)

    def setUp(self):
        # Each test starts from a clean override store.
        self.server.reset_overrides()

    def test_get_empty_returns_empty_dict(self):
        status, _, payload = _json_request(self.server.port, "GET", "/api/_test/state")
        self.assertEqual(status, 200)
        self.assertEqual(payload, {})

    def test_post_merges_across_calls(self):
        status, _, p1 = _json_request(
            self.server.port, "POST", "/api/_test/state",
            body={"radio_status": "offline"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(p1, {"radio_status": "offline"})

        status, _, p2 = _json_request(
            self.server.port, "POST", "/api/_test/state",
            body={"recovery_state": "recovering"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(p2, {"radio_status": "offline", "recovery_state": "recovering"})

        # GET confirms the merged store.
        _, _, get_payload = _json_request(self.server.port, "GET", "/api/_test/state")
        self.assertEqual(
            get_payload,
            {"radio_status": "offline", "recovery_state": "recovering"},
        )

    def test_post_null_clears_one_field(self):
        _json_request(
            self.server.port, "POST", "/api/_test/state",
            body={"radio_status": "offline", "recovery_state": "recovering"},
        )
        status, _, payload = _json_request(
            self.server.port, "POST", "/api/_test/state",
            body={"radio_status": None},
        )
        self.assertEqual(status, 200)
        # radio_status removed; recovery_state preserved.
        self.assertEqual(payload, {"recovery_state": "recovering"})

    def test_delete_clears_all_and_returns_204(self):
        _json_request(
            self.server.port, "POST", "/api/_test/state",
            body={"radio_status": "offline", "recovery_state": "recovering"},
        )
        status, _, raw = _request(self.server.port, "DELETE", "/api/_test/state")
        self.assertEqual(status, 204)
        self.assertEqual(raw, b"")  # 204 must have no body
        _, _, payload = _json_request(self.server.port, "GET", "/api/_test/state")
        self.assertEqual(payload, {})

    def test_unknown_field_400_no_partial_apply(self):
        status, _, payload = _json_request(
            self.server.port, "POST", "/api/_test/state",
            body={"radio_status": "offline", "bogus": "x"},
        )
        self.assertEqual(status, 400)
        self.assertIn("bogus", payload["error"])
        # The valid sibling field MUST NOT have been applied.
        _, _, get_payload = _json_request(self.server.port, "GET", "/api/_test/state")
        self.assertEqual(get_payload, {})

    def test_type_mismatch_radio_status(self):
        status, _, payload = _json_request(
            self.server.port, "POST", "/api/_test/state",
            body={"radio_status": 42},
        )
        self.assertEqual(status, 400)
        self.assertIn("radio_status", payload["error"])

    def test_type_mismatch_radio_status_unknown_enum_value(self):
        status, _, payload = _json_request(
            self.server.port, "POST", "/api/_test/state",
            body={"radio_status": "purple"},
        )
        self.assertEqual(status, 400)
        self.assertIn("radio_status", payload["error"])

    def test_type_mismatch_skew_string(self):
        status, _, payload = _json_request(
            self.server.port, "POST", "/api/_test/state",
            body={"server_time_skew_seconds": "1h"},
        )
        self.assertEqual(status, 400)
        self.assertIn("server_time_skew_seconds", payload["error"])

    def test_type_mismatch_last_seen_ts_bool_rejected(self):
        # bool is a subclass of int — must be rejected explicitly so
        # {"last_seen_ts": true} doesn't silently land as 1.
        status, _, payload = _json_request(
            self.server.port, "POST", "/api/_test/state",
            body={"last_seen_ts": True},
        )
        self.assertEqual(status, 400)
        self.assertIn("last_seen_ts", payload["error"])

    def test_post_non_object_body_400(self):
        status, _, payload = _json_request(
            self.server.port, "POST", "/api/_test/state",
            body=["radio_status", "offline"],
        )
        self.assertEqual(status, 400)
        self.assertIn("object", payload["error"].lower())


# ---------------------------------------------------------------------------
# Propagation — overrides actually shift the downstream response payloads
# ---------------------------------------------------------------------------


def _insert_status_row(db_path: str, *, last_seen_ts: int, state: str, connected: bool):
    """Wrapper around upsert_status using the test DB path. The /api/status
    handler's NORMAL branch only fires when there's a row with a non-null
    last_seen_ts; tests that exercise the normal branch call this first."""
    conn = sqlite3.connect(db_path, isolation_level=None)
    try:
        conn.execute(
            """
            INSERT INTO status(process, last_seen_ts, radio_connected, state)
            VALUES ('mesh_bot', ?, ?, ?)
            ON CONFLICT(process) DO UPDATE SET
                last_seen_ts=excluded.last_seen_ts,
                radio_connected=excluded.radio_connected,
                state=excluded.state
            """,
            (last_seen_ts, 1 if connected else 0, state),
        )
    finally:
        conn.close()


class TestPropagation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _bring_up(cls, diagnostics_enabled=True, log_name="teststate_prop")
        # Seed a fresh, healthy status row so /api/status hits the normal
        # branch by default. Individual tests adjust as needed.
        _insert_status_row(
            cls.db_cfg.path,
            last_seen_ts=int(time.time()),
            state="healthy",
            connected=True,
        )

    @classmethod
    def tearDownClass(cls):
        _tear_down(cls)

    def setUp(self):
        self.server.reset_overrides()
        # Re-seed the row each test so age computations are predictable.
        _insert_status_row(
            self.db_cfg.path,
            last_seen_ts=int(time.time()),
            state="healthy",
            connected=True,
        )

    def test_radio_status_override_wins_over_derivation(self):
        # Real row says "healthy + connected" -> would derive "online".
        # Override to "offline" -> response says "offline" regardless.
        _json_request(
            self.server.port, "POST", "/api/_test/state",
            body={"radio_status": "offline"},
        )
        _, _, payload = _json_request(self.server.port, "GET", "/api/status")
        self.assertEqual(payload["radio_status"], "offline")
        # Deprecated `radio` field derives from radio_status, so it follows
        # the override too.
        self.assertEqual(payload["radio"], "offline")

    def test_recovery_state_override_visible_and_flows_to_derivation(self):
        # Override recovery_state to "recovering" without touching radio_status.
        # The derivation block sees state="recovering" and connected=True;
        # falls into the "recovering" branch.
        _json_request(
            self.server.port, "POST", "/api/_test/state",
            body={"recovery_state": "recovering"},
        )
        _, _, payload = _json_request(self.server.port, "GET", "/api/status")
        self.assertEqual(payload["recovery_state"], "recovering")
        self.assertEqual(payload["radio_status"], "recovering")

    def test_last_seen_ts_override_reshapes_age_sec(self):
        now = int(time.time())
        # Override last_seen_ts to 600 seconds ago. age_sec must reflect that.
        _json_request(
            self.server.port, "POST", "/api/_test/state",
            body={"last_seen_ts": now - 600},
        )
        _, _, payload = _json_request(self.server.port, "GET", "/api/status")
        self.assertEqual(payload["last_seen_ts"], now - 600)
        # age_sec ~= 600 (within a few seconds for the wall-clock between
        # the override POST and the GET).
        self.assertGreaterEqual(payload["age_sec"], 600)
        self.assertLessEqual(payload["age_sec"], 605)
        # age > 30 with state="healthy" hits the "age > 30 -> needs_human"
        # branch of the derivation, so without overriding radio_status
        # directly the response reflects the staleness.
        self.assertEqual(payload["radio_status"], "needs_human")

    def test_server_time_skew_shifts_external_display_server_time(self):
        _json_request(
            self.server.port, "POST", "/api/_test/state",
            body={"server_time_skew_seconds": 3600},
        )
        before = int(time.time())
        _, _, payload = _json_request(
            self.server.port, "GET", "/api/external-display/state"
        )
        after = int(time.time())
        # server_time = _now_ts() + 3600. _now_ts() is captured between
        # `before` and `after` on the server side.
        self.assertGreaterEqual(payload["server_time"], before + 3600)
        self.assertLessEqual(payload["server_time"], after + 3600)

    def test_skew_shifts_status_age_consistently(self):
        # Two entry points must use the same skewed clock. If /api/status's
        # `now` is skewed but /api/external-display/state's `server_time`
        # isn't (or vice versa), this test catches the inconsistency.
        now = int(time.time())
        # Force last_seen_ts to a known anchor, override skew to a big number.
        _insert_status_row(
            self.db_cfg.path, last_seen_ts=now, state="healthy", connected=True,
        )
        _json_request(
            self.server.port, "POST", "/api/_test/state",
            body={"server_time_skew_seconds": 7200},
        )
        _, _, payload = _json_request(self.server.port, "GET", "/api/status")
        # Skew is 2h; the row is fresh; age_sec should be ~7200, not ~0.
        self.assertGreaterEqual(payload["age_sec"], 7195)
        self.assertLessEqual(payload["age_sec"], 7210)

    def test_negative_skew_accepted(self):
        # Spec allows any sign on server_time_skew_seconds.
        _json_request(
            self.server.port, "POST", "/api/_test/state",
            body={"server_time_skew_seconds": -3600},
        )
        before = int(time.time())
        _, _, payload = _json_request(
            self.server.port, "GET", "/api/external-display/state"
        )
        after = int(time.time())
        self.assertGreaterEqual(payload["server_time"], before - 3600)
        self.assertLessEqual(payload["server_time"], after - 3600)


# ---------------------------------------------------------------------------
# The regression guard — no override path is byte-identical to pre-PR
# ---------------------------------------------------------------------------


# These key sets are the contract: /api/status and /api/external-display/state
# returned exactly these keys before this PR's plumbing was added. The
# guard test asserts both:
#   1. Every expected key is present.
#   2. No unexpected key has been introduced.
# If (1) fails the override hook removed a key. If (2) fails the override
# hook leaked a new key into the no-override response. Both are bugs the
# spec called out by name.

_STATUS_EMPTY_BRANCH_KEYS = frozenset({
    "radio_status", "recovery_state", "radio", "mesh_bot_seen",
    "node_name", "hub_name", "outbox_queue_depth",
})
_STATUS_NORMAL_BRANCH_KEYS = _STATUS_EMPTY_BRANCH_KEYS | {"last_seen_ts", "age_sec"}
_EXTDISP_TOP_KEYS = frozenset({"api_version", "server_time", "hub", "channels"})


class TestNoOverrideRegressionGuard(unittest.TestCase):
    """With overrides empty, both endpoints MUST return exactly the keys
    they returned before this PR. This is the single most important test
    in the file."""

    @classmethod
    def setUpClass(cls):
        _bring_up(cls, diagnostics_enabled=True, log_name="teststate_regress")

    @classmethod
    def tearDownClass(cls):
        _tear_down(cls)

    def setUp(self):
        self.server.reset_overrides()

    def test_status_empty_branch_byte_identical_keys(self):
        # No status row -> empty branch.
        _, _, payload = _json_request(self.server.port, "GET", "/api/status")
        self.assertEqual(
            set(payload.keys()), _STATUS_EMPTY_BRANCH_KEYS,
            f"empty-branch key set drifted: got {sorted(payload.keys())}",
        )
        self.assertEqual(payload["radio_status"], "needs_human")
        self.assertIsNone(payload["recovery_state"])
        self.assertFalse(payload["mesh_bot_seen"])
        self.assertEqual(payload["radio"], "offline")

    def test_status_normal_branch_byte_identical_keys(self):
        _insert_status_row(
            self.db_cfg.path,
            last_seen_ts=int(time.time()),
            state="healthy",
            connected=True,
        )
        _, _, payload = _json_request(self.server.port, "GET", "/api/status")
        self.assertEqual(
            set(payload.keys()), _STATUS_NORMAL_BRANCH_KEYS,
            f"normal-branch key set drifted: got {sorted(payload.keys())}",
        )
        # And the derivation still produces "online" for a fresh healthy row,
        # exactly as before this PR.
        self.assertEqual(payload["radio_status"], "online")
        self.assertEqual(payload["recovery_state"], "healthy")
        self.assertTrue(payload["mesh_bot_seen"])
        self.assertEqual(payload["radio"], "online")
        self.assertIsInstance(payload["last_seen_ts"], int)
        self.assertIsInstance(payload["age_sec"], int)
        self.assertGreaterEqual(payload["age_sec"], 0)
        self.assertLessEqual(payload["age_sec"], 5)

    def test_external_display_byte_identical_keys(self):
        _, _, payload = _json_request(
            self.server.port, "GET", "/api/external-display/state"
        )
        self.assertEqual(
            set(payload.keys()), _EXTDISP_TOP_KEYS,
            f"external-display top-level key set drifted: got {sorted(payload.keys())}",
        )
        # server_time must equal real wall-clock (no skew override set).
        now = int(time.time())
        self.assertGreaterEqual(payload["server_time"], now - 2)
        self.assertLessEqual(payload["server_time"], now + 2)


if __name__ == "__main__":
    unittest.main()
