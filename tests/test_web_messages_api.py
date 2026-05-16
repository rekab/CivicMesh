"""Regression tests for the /api/messages JSON contract.

Pins the fix for the session-cookie-disclosure bug: the messages
endpoint must not expose `session_id` (which IS the poster's cookie
value) or `fingerprint` to other portal viewers, and must instead
project a server-computed `is_own` boolean against the requesting
viewer's session id.

If a future change reintroduces `messages.*` in the projection or adds
session_id/fingerprint back to the response shape,
`test_session_id_absent_from_response` fires immediately.
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
import urllib.parse

from config import load_config
from database import (
    DBConfig,
    init_db,
    insert_message,
    queue_outbox_and_message,
)
from web_server import CivicMeshHandler


_CHANNEL = "#civicmesh-test"

_POSTER_SID = "poster-session-aaaaaaaaaaaaaa"
_OTHER_SID = "other-session-bbbbbbbbbbbbbbbb"


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
    section header so it cannot get bound to a trailing section — that
    binding is the bug class this whole change is closing."""
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


class _MessagesServer:
    """Minimal stand-in for web_server._Server. Mirrors test_web_probe.py."""

    def __init__(self, cfg, db_cfg, tmpdir):
        log = logging.getLogger("test_web_messages_api")
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


class TestApiMessagesShape(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.mkdtemp(prefix="civicmesh_messages_test_")
        db_path = os.path.join(cls.tmpdir, "test.db")
        sections = {
            "node":     {"site_name": "TestHub", "callsign": "test1"},
            "network":  dict(_BASE_NETWORK),
            "ap":       {"ssid": "CivicMesh-Test", "channel": 6},
            "channels": {"names": [_CHANNEL]},
            "web":      {"port": 8080, "portal_aliases": ["civicmesh.internal"]},
            "logging":  {"log_dir": os.path.join(cls.tmpdir, "logs"),
                         "log_level": "WARNING"},
        }
        cfg_path = os.path.join(cls.tmpdir, "config.toml")
        with open(cfg_path, "w") as f:
            f.write(_render_test_config(db_path, sections))

        cls.cfg = load_config(cfg_path)
        cls.db_cfg = DBConfig(path=cls.cfg.db_path)

        # Defence in depth against the bug class that motivated this whole
        # change: if the test ever ends up pointing at a path outside its
        # tmpdir (because the inline render breaks, because somebody adds
        # a fallback to the loader, because cwd resolution sneaks back in),
        # fail loud BEFORE init_db opens any connection.
        assert cls.db_cfg.path.startswith(cls.tmpdir), (
            f"test DB escaped sandbox: {cls.db_cfg.path!r} "
            f"(expected to be under {cls.tmpdir!r})"
        )

        init_db(cls.db_cfg, log=logging.getLogger("test_web_messages_api"))

        # Two portal-origin posts (one per session) and one mesh-origin post.
        # The mesh row carries a NULL session_id, mirroring how mesh_bot.py
        # calls insert_message without passing session_id.
        # max_queue_depth=100_000 — generous fixture cap, never trips.
        # The parameter became required when the relay-wide outbox cap
        # landed; this is fixture data.
        cls.poster_oid, cls.poster_mid = queue_outbox_and_message(
            cls.db_cfg,
            ts=1_700_000_000,
            channel=_CHANNEL,
            sender="poster",
            content="hello from poster",
            session_id=_POSTER_SID,
            fingerprint="ffffffffffffffffffffffffffffffffffffffff",
            max_queue_depth=100_000,
        )
        cls.other_oid, cls.other_mid = queue_outbox_and_message(
            cls.db_cfg,
            ts=1_700_000_001,
            channel=_CHANNEL,
            sender="other",
            content="hello from other",
            session_id=_OTHER_SID,
            fingerprint="eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
            max_queue_depth=100_000,
        )
        cls.mesh_mid = insert_message(
            cls.db_cfg,
            ts=1_700_000_002,
            channel=_CHANNEL,
            sender="mesh-peer",
            content="hello from mesh",
            source="mesh",
        )

        cls.server = _MessagesServer(cls.cfg, cls.db_cfg, cls.tmpdir)

    @classmethod
    def tearDownClass(cls):
        cls.server.close()
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def _get_messages(self, cookie=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.port, timeout=5)
        try:
            headers = {}
            if cookie is not None:
                headers["Cookie"] = f"civicmesh_session={cookie}"
            qs = urllib.parse.urlencode({"channel": _CHANNEL})
            conn.request("GET", f"/api/messages?{qs}", headers=headers)
            resp = conn.getresponse()
            self.assertEqual(resp.status, 200)
            body = json.loads(resp.read().decode("utf-8"))
        finally:
            conn.close()
        self.assertIn("messages", body)
        return body["messages"]

    def _by_id(self, rows, mid):
        for r in rows:
            if r["id"] == mid:
                return r
        self.fail(f"no row with id={mid} in response")

    # ---- the leak fix itself --------------------------------------------

    def test_session_id_absent_from_response(self):
        """No row in the response may carry session_id or fingerprint, and
        every row must carry is_own. Pins the session-cookie-disclosure
        fix; if a future change reverts to `SELECT messages.*`, this
        test fires before the leak ships."""
        rows = self._get_messages(cookie=_POSTER_SID)
        self.assertGreater(len(rows), 0)
        for r in rows:
            self.assertNotIn(
                "session_id", r,
                f"row id={r.get('id')} leaks session_id (session-cookie-disclosure regression)",
            )
            self.assertNotIn(
                "fingerprint", r,
                f"row id={r.get('id')} leaks fingerprint (session-cookie-disclosure regression)",
            )
            self.assertIn("is_own", r, f"row id={r.get('id')} missing is_own")

    # ---- is_own semantics -----------------------------------------------

    def test_is_own_true_for_viewer_session(self):
        rows = self._get_messages(cookie=_POSTER_SID)
        self.assertIs(self._by_id(rows, self.poster_mid)["is_own"], True)

    def test_is_own_false_for_other_session(self):
        rows = self._get_messages(cookie=_POSTER_SID)
        self.assertIs(self._by_id(rows, self.other_mid)["is_own"], False)

    def test_is_own_false_for_anonymous(self):
        rows = self._get_messages(cookie=None)
        for r in rows:
            self.assertIs(
                r["is_own"], False,
                f"row id={r['id']} should be is_own=False for anonymous viewer",
            )

    def test_is_own_false_for_mesh_origin(self):
        # Mesh-origin rows have NULL session_id; the SQL guard
        # (session_id IS NOT NULL AND session_id = ?) must keep them
        # is_own=False even when the viewer cookie is set.
        rows = self._get_messages(cookie=_POSTER_SID)
        self.assertIs(self._by_id(rows, self.mesh_mid)["is_own"], False)
        rows_anon = self._get_messages(cookie=None)
        self.assertIs(self._by_id(rows_anon, self.mesh_mid)["is_own"], False)


if __name__ == "__main__":
    unittest.main()
