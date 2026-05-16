"""Integration tests for `/api/post` queue-full 429 (relay-wide outbox depth cap).

Pins:
  - Under-cap traffic returns 200 (the happy path is unchanged).
  - At-cap traffic returns 429 with the expected JSON shape and does
    NOT enqueue the row.
  - Queue-cap is independent of the per-session quota gate: a poster
    in good standing still gets 429 when the queue is full.
  - Concurrent POSTs cannot bypass the cap. With BEGIN IMMEDIATE around
    the depth check + INSERT, exactly outbox_max_depth rows land even
    when N>>cap workers all release at the same instant. With the prior
    BEGIN DEFERRED, two readers could both see depth=N-1 and both
    INSERT — this test would reliably overcount.

Inline TOML render + sandbox assertion match the shape from
`tests/test_web_messages_api.py` (the per-test config-render style
introduced in the db_path PR).
"""

import http.client
import http.server
import json
import logging
import os
import shutil
import tempfile
import threading
import unittest

from config import load_config
from database import (
    DBConfig,
    create_or_update_session,
    init_db,
    queue_outbox_and_message,
)
from web_server import CivicMeshHandler


_CHANNEL = "#civicmesh-test"
_SID = "post429-session-aaaaaaaaaaaaaaaa"

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
    """Inline TOML render. db_path is emitted as a top-level key BEFORE
    any section header so it cannot get bound to a trailing section."""
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


class _PostServer:
    """Minimal stand-in for web_server._Server. Mirrors test_web_messages_api."""

    def __init__(self, cfg, db_cfg, tmpdir):
        log = logging.getLogger("test_web_post_429")
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
                # sec is None — the 429 handlers do `if sec: sec.error(...)`
                # which would crash against a stock logging.Logger because
                # the security path passes arbitrary kwargs.
                self.sec = None
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


def _post(port: int, sid: str, body: dict) -> tuple[int, dict]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request(
            "POST", "/api/post",
            body=json.dumps(body),
            headers={
                "Cookie": f"civicmesh_session={sid}",
                "Content-Type": "application/json",
            },
        )
        resp = conn.getresponse()
        status = resp.status
        try:
            payload = json.loads(resp.read().decode("utf-8"))
        except ValueError:
            payload = {}
    finally:
        conn.close()
    return status, payload


class _PostTestBase(unittest.TestCase):
    """Common setup: tmpdir, sandbox-asserted DB, valid session, server."""

    OUTBOX_MAX_DEPTH = 2  # default for tests; subclasses override
    POSTS_PER_HOUR = 10_000  # high so per-session quota never trips

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.mkdtemp(prefix="civicmesh_post429_test_")
        db_path = os.path.join(cls.tmpdir, "test.db")
        sections = {
            "node":     {"site_name": "TestHub", "callsign": "test1"},
            "network":  dict(_BASE_NETWORK),
            "ap":       {"ssid": "CivicMesh-Test", "channel": 6},
            "channels": {"names": [_CHANNEL]},
            "web":      {"port": 8080, "portal_aliases": ["civicmesh.internal"]},
            "limits":   {
                "outbox_max_depth": cls.OUTBOX_MAX_DEPTH,
                "posts_per_hour": cls.POSTS_PER_HOUR,
            },
            "logging":  {"log_dir": os.path.join(cls.tmpdir, "logs"),
                         "log_level": "WARNING"},
        }
        cfg_path = os.path.join(cls.tmpdir, "config.toml")
        with open(cfg_path, "w") as f:
            f.write(_render_test_config(db_path, sections))

        cls.cfg = load_config(cfg_path)
        cls.db_cfg = DBConfig(path=cls.cfg.db_path)

        # Sandbox assertion (mirrors test_web_messages_api.py).
        assert cls.db_cfg.path.startswith(cls.tmpdir), (
            f"test DB escaped sandbox: {cls.db_cfg.path!r} "
            f"(expected to be under {cls.tmpdir!r})"
        )

        init_db(cls.db_cfg, log=logging.getLogger("test_web_post_429"))

        # Pre-create the session so _require_session succeeds.
        create_or_update_session(
            cls.db_cfg, session_id=_SID,
            name="poster", location="TestHub",
            mac_address=None,
        )

        cls.server = _PostServer(cls.cfg, cls.db_cfg, cls.tmpdir)

    @classmethod
    def tearDownClass(cls):
        cls.server.close()
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def _outbox_count(self) -> int:
        import sqlite3
        conn = sqlite3.connect(self.db_cfg.path)
        try:
            return int(conn.execute(
                "SELECT COUNT(*) FROM outbox WHERE status='queued'"
            ).fetchone()[0])
        finally:
            conn.close()

    def _truncate_outbox(self) -> None:
        # Per-test cleanup: drain rows so each test sees a known starting depth.
        import sqlite3
        conn = sqlite3.connect(self.db_cfg.path)
        try:
            conn.execute("DELETE FROM outbox")
            conn.execute("DELETE FROM messages")
            conn.commit()
        finally:
            conn.close()

    def setUp(self):
        self._truncate_outbox()


class TestPostQueueCap(_PostTestBase):

    OUTBOX_MAX_DEPTH = 2
    POSTS_PER_HOUR = 10_000

    def test_post_under_queue_cap_returns_200(self):
        # (f) Fresh DB, post once, 200 + outbox_id + message_id.
        status, payload = _post(self.server.port, _SID, {
            "channel": _CHANNEL, "content": "hello", "name": "poster",
        })
        self.assertEqual(status, 200)
        self.assertTrue(payload.get("ok"))
        self.assertIn("outbox_id", payload)
        self.assertIn("message_id", payload)
        self.assertEqual(self._outbox_count(), 1)

    def test_post_at_queue_cap_returns_429(self):
        # (e) Pre-populate two queued rows. Third POST returns 429 with the
        # exact expected shape and the row-count does not advance.
        for i in range(self.OUTBOX_MAX_DEPTH):
            queue_outbox_and_message(
                self.db_cfg, ts=1_700_000_000 + i,
                channel=_CHANNEL, sender="seed", content=f"seed-{i}",
                session_id=_SID, fingerprint=None,
                max_queue_depth=100_000,
            )
        self.assertEqual(self._outbox_count(), self.OUTBOX_MAX_DEPTH)

        status, payload = _post(self.server.port, _SID, {
            "channel": _CHANNEL, "content": "would-overflow", "name": "poster",
        })
        self.assertEqual(status, 429)
        self.assertEqual(payload.get("error"),
                         "queue full — try again in a few minutes")
        self.assertEqual(payload.get("retry_after_sec"), 60)
        # The row count must not have advanced.
        self.assertEqual(self._outbox_count(), self.OUTBOX_MAX_DEPTH)

    def test_outbox_full_independent_of_per_session_quota(self):
        # (g) posts_per_hour=10_000 (this class) means the per-session
        # gate never fires; we still get 429 from the queue cap. Confirms
        # the two gates are independent.
        for i in range(self.OUTBOX_MAX_DEPTH):
            queue_outbox_and_message(
                self.db_cfg, ts=1_700_000_000 + i,
                channel=_CHANNEL, sender="seed", content=f"seed-{i}",
                session_id=_SID, fingerprint=None,
                max_queue_depth=100_000,
            )

        status, payload = _post(self.server.port, _SID, {
            "channel": _CHANNEL, "content": "still-blocked", "name": "poster",
        })
        self.assertEqual(status, 429)
        # The rate-limit 429's shape has a different error string and
        # has posts_remaining/limit/window_sec instead of retry_after_sec.
        # Pin the divergence so a future refactor that collapses the two
        # branches gets caught.
        self.assertEqual(payload.get("error"),
                         "queue full — try again in a few minutes")
        self.assertNotIn("posts_remaining", payload)


class TestPostQueueCapConcurrent(_PostTestBase):

    OUTBOX_MAX_DEPTH = 5
    POSTS_PER_HOUR = 10_000

    def test_concurrent_posts_respect_cap(self):
        # N threads release at the same barrier. Exactly OUTBOX_MAX_DEPTH
        # rows land in outbox. With BEGIN IMMEDIATE around the depth-check
        # + INSERT, this is deterministic. With the prior BEGIN DEFERRED,
        # two readers could both see depth=N-1 and both INSERT, overcounting.
        #
        # We do NOT require all N workers to receive a response: SQLite
        # write contention under heavy thrashing may exhaust the
        # _retry_on_locked decorator and surface as a connection reset.
        # That's acceptable Pi-Zero-realistic behavior — the only
        # property under test is "the cap holds." If even one extra row
        # lands beyond the cap, the test fails.
        N = 20
        cap = self.OUTBOX_MAX_DEPTH
        barrier = threading.Barrier(N)
        results: list[int] = [0] * N

        def worker(i: int):
            barrier.wait()
            try:
                status, _ = _post(self.server.port, _SID, {
                    "channel": _CHANNEL,
                    "content": f"concurrent-{i}",
                    "name": "poster",
                })
                results[i] = status
            except Exception:
                results[i] = -1  # connection reset under contention

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(N)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)

        ok = sum(1 for s in results if s == 200)
        full = sum(1 for s in results if s == 429)
        # The crucial property: the depth-cap held.
        self.assertEqual(self._outbox_count(), cap,
                         f"BEGIN IMMEDIATE must hold the cap exactly; "
                         f"got {self._outbox_count()} rows, expected {cap}. "
                         f"(BEGIN DEFERRED would overshoot. statuses={results})")
        # And the 200s match the rows we admitted.
        self.assertEqual(ok, cap,
                         f"got {ok} 200s, expected {cap}. statuses={results}")
        # Sanity: the rest are either 429 or connection-reset, never some other code.
        unexpected = [s for s in results if s not in (200, 429, -1, 0)]
        self.assertEqual(unexpected, [], f"unexpected statuses: {results}")


if __name__ == "__main__":
    unittest.main()
