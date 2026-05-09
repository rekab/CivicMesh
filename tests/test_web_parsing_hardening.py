"""Integration tests for CIV-86 request-parsing hardening.

Pins:
  - JSON body size cap (Content-Length > 4096 → 413 before allocation).
  - Content-Length parse failures (non-int, negative → 400).
  - Malformed JSON → 400 (not 500 with traceback).
  - Numeric query params (limit/offset/message_id) reject non-int and
    out-of-range with stable 400 strings.
  - Realistic infinite-scroll pagination (offset=2400, limit=80) still
    returns 200 — pins that the offset cap doesn't silently break the
    frontend's `loadOlderMessages` reach.
  - Unhandled handler exceptions return 500 with a stable error string,
    NOT a traceback in the response body. The traceback goes to the
    server log only.

Inline TOML render + sandbox assertion match the shape from
tests/test_web_messages_api.py and tests/test_web_post_429.py.
"""

import http.client
import http.server
import io
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
    create_or_update_session,
    init_db,
    queue_outbox_and_message,
)
import web_server
from web_server import CivicMeshHandler, _HTTPError


_CHANNEL = "#civicmesh-test"
_SID = "parse-session-aaaaaaaaaaaaaaaaa"

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


class _ParseServer:
    def __init__(self, cfg, db_cfg, tmpdir, log):
        static_dir = os.path.join(
            os.path.dirname(os.path.abspath(os.path.dirname(__file__))), "static"
        )

        class _S(http.server.ThreadingHTTPServer):
            def __init__(self, *a, **kw):
                super().__init__(*a, **kw)
                self.cfg = cfg
                self.db_cfg = db_cfg
                self.log = log
                # sec=None per the test_web_post_429 pattern: the security
                # logger expects arbitrary kwargs that a stock logging.Logger
                # cannot handle; the if-sec guards in handlers no-op cleanly.
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


def _request(port: int, method: str, path: str, *, body: bytes = b"",
             headers: dict = None) -> tuple[int, bytes]:
    """Low-level request helper. Returns (status, body_bytes). We avoid
    automatic Content-Length insertion so tests can drive the header
    deliberately (oversized, non-int, etc.)."""
    h = dict(headers or {})
    h.setdefault("Cookie", f"civicmesh_session={_SID}")
    if body and "Content-Length" not in h:
        h["Content-Length"] = str(len(body))
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request(method, path, body=body, headers=h)
        resp = conn.getresponse()
        return resp.status, resp.read()
    finally:
        conn.close()


def _json_body(raw: bytes) -> dict:
    try:
        return json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return {}


class _ParseTestBase(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.mkdtemp(prefix="civicmesh_parse_test_")
        db_path = os.path.join(cls.tmpdir, "test.db")
        sections = {
            "node":     {"site_name": "TestHub", "callsign": "test1"},
            "network":  dict(_BASE_NETWORK),
            "ap":       {"ssid": "CivicMesh-Test", "channel": 6},
            "channels": {"names": [_CHANNEL]},
            "web":      {"port": 8080, "portal_aliases": ["civicmesh.internal"]},
            "limits":   {"posts_per_hour": 10_000},
            "logging":  {"log_dir": os.path.join(cls.tmpdir, "logs"),
                         "log_level": "WARNING"},
        }
        cfg_path = os.path.join(cls.tmpdir, "config.toml")
        with open(cfg_path, "w") as f:
            f.write(_render_test_config(db_path, sections))

        cls.cfg = load_config(cfg_path)
        cls.db_cfg = DBConfig(path=cls.cfg.db_path)
        assert cls.db_cfg.path.startswith(cls.tmpdir), (
            f"test DB escaped sandbox: {cls.db_cfg.path!r}"
        )

        cls.log = logging.getLogger("test_web_parsing_hardening")
        cls.log.setLevel(logging.ERROR)
        cls.log.propagate = False
        # Capture log records so the traceback-leak test can assert the
        # traceback IS present in the log even when absent from the body.
        cls.log_buffer = io.StringIO()
        cls.log_handler = logging.StreamHandler(cls.log_buffer)
        cls.log.addHandler(cls.log_handler)

        init_db(cls.db_cfg, log=cls.log)
        create_or_update_session(
            cls.db_cfg, session_id=_SID,
            name="parser", location="TestHub", mac_address=None,
        )

        cls.server = _ParseServer(cls.cfg, cls.db_cfg, cls.tmpdir, cls.log)

    @classmethod
    def tearDownClass(cls):
        cls.server.close()
        cls.log.removeHandler(cls.log_handler)
        shutil.rmtree(cls.tmpdir, ignore_errors=True)


class TestContentLengthGuards(_ParseTestBase):

    def test_oversized_content_length_returns_413(self):
        # Body matches declared length (5000 bytes), well over 4096 cap.
        # Server must respond 413 before reading the body into memory.
        body = b"x" * 5000
        status, raw = _request(
            self.server.port, "POST", "/api/post",
            body=body, headers={"Content-Type": "application/json"},
        )
        self.assertEqual(status, 413)
        self.assertEqual(_json_body(raw), {"error": "request too large"})

    def test_negative_content_length_returns_400(self):
        # Cannot send raw body with negative Content-Length, but the
        # header alone is enough to trigger the 400.
        status, raw = _request(
            self.server.port, "POST", "/api/post",
            body=b"",
            headers={"Content-Type": "application/json", "Content-Length": "-1"},
        )
        self.assertEqual(status, 400)
        self.assertEqual(_json_body(raw), {"error": "invalid content-length"})

    def test_non_integer_content_length_returns_400(self):
        status, raw = _request(
            self.server.port, "POST", "/api/post",
            body=b"",
            headers={"Content-Type": "application/json", "Content-Length": "foo"},
        )
        self.assertEqual(status, 400)
        self.assertEqual(_json_body(raw), {"error": "invalid content-length"})


class TestMalformedJson(_ParseTestBase):

    def test_malformed_json_returns_400(self):
        body = b"{not-valid-json"
        status, raw = _request(
            self.server.port, "POST", "/api/post",
            body=body, headers={"Content-Type": "application/json"},
        )
        self.assertEqual(status, 400)
        self.assertEqual(_json_body(raw), {"error": "invalid json"})
        # Stable error string only — no parser exception detail leaks.
        body_text = raw.decode("utf-8", errors="replace").lower()
        for token in ("traceback", "jsondecodeerror", "expecting value",
                      "unicodedecodeerror"):
            self.assertNotIn(token, body_text,
                             f"response body must not leak {token!r}: {body_text!r}")


class TestMessagesNumeric(_ParseTestBase):

    def test_messages_non_integer_limit_returns_400(self):
        qs = urllib.parse.urlencode({"channel": _CHANNEL, "limit": "abc"})
        status, raw = _request(self.server.port, "GET", f"/api/messages?{qs}")
        self.assertEqual(status, 400)
        self.assertEqual(_json_body(raw),
                         {"error": "limit must be an integer"})

    def test_messages_out_of_range_limit_returns_400(self):
        for v in ("0", "-1", str(web_server._MAX_MESSAGE_LIMIT + 1), "99999999"):
            qs = urllib.parse.urlencode({"channel": _CHANNEL, "limit": v})
            status, raw = _request(self.server.port, "GET", f"/api/messages?{qs}")
            self.assertEqual(status, 400, f"limit={v} should 400")
            self.assertEqual(_json_body(raw), {"error": "limit out of range"},
                             f"limit={v}")

    def test_messages_out_of_range_offset_returns_400(self):
        for v in ("-1", str(web_server._MAX_MESSAGE_OFFSET + 1)):
            qs = urllib.parse.urlencode({"channel": _CHANNEL, "offset": v})
            status, raw = _request(self.server.port, "GET", f"/api/messages?{qs}")
            self.assertEqual(status, 400, f"offset={v} should 400")
            self.assertEqual(_json_body(raw), {"error": "offset out of range"},
                             f"offset={v}")

    def test_messages_offset_at_cap_returns_200(self):
        # Boundary case: offset == _MAX_MESSAGE_OFFSET is in-range.
        qs = urllib.parse.urlencode({
            "channel": _CHANNEL,
            "offset": str(web_server._MAX_MESSAGE_OFFSET),
            "limit": "10",
        })
        status, _ = _request(self.server.port, "GET", f"/api/messages?{qs}")
        self.assertEqual(status, 200)

    def test_messages_realistic_pagination_returns_200(self):
        # Mimics ~30 scroll-ups in static/app.js loadOlderMessages
        # (pageSize=80). Pins that the offset cap accommodates real
        # infinite-scroll. If a future change tightens
        # _MAX_MESSAGE_OFFSET below 2400, this test fires.
        qs = urllib.parse.urlencode({
            "channel": _CHANNEL, "offset": "2400", "limit": "80",
        })
        status, raw = _request(self.server.port, "GET", f"/api/messages?{qs}")
        self.assertEqual(status, 200)
        self.assertIn("messages", _json_body(raw))

    def test_messages_valid_request_returns_200(self):
        qs = urllib.parse.urlencode({"channel": _CHANNEL, "limit": "10",
                                     "offset": "0"})
        status, raw = _request(self.server.port, "GET", f"/api/messages?{qs}")
        self.assertEqual(status, 200)
        self.assertIn("messages", _json_body(raw))


class TestVotesNumeric(_ParseTestBase):

    def test_votes_non_integer_message_id_returns_400(self):
        status, raw = _request(self.server.port, "GET",
                               "/api/votes?message_id=xyz")
        self.assertEqual(status, 400)
        self.assertEqual(_json_body(raw),
                         {"error": "message_id must be an integer"})


class TestVoteBody(_ParseTestBase):

    def test_vote_body_non_integer_message_id_returns_400(self):
        body = json.dumps({"message_id": "abc", "vote_type": 1}).encode("utf-8")
        status, raw = _request(
            self.server.port, "POST", "/api/vote",
            body=body, headers={"Content-Type": "application/json"},
        )
        self.assertEqual(status, 400)
        self.assertEqual(_json_body(raw),
                         {"error": "message_id must be an integer"})

    def test_vote_body_out_of_range_vote_type_returns_400(self):
        # Prime the DB with a real message so message_id passes; vote_type
        # garbage is what we're checking.
        oid, mid = queue_outbox_and_message(
            self.db_cfg, ts=1_700_000_010,
            channel=_CHANNEL, sender="seed", content="vote-target",
            session_id=_SID, fingerprint=None,
            max_queue_depth=100_000,
        )
        body = json.dumps({"message_id": mid, "vote_type": 99}).encode("utf-8")
        status, raw = _request(
            self.server.port, "POST", "/api/vote",
            body=body, headers={"Content-Type": "application/json"},
        )
        self.assertEqual(status, 400)
        self.assertEqual(_json_body(raw), {"error": "vote_type out of range"})


class TestTracebackDoesNotLeak(_ParseTestBase):
    """Pin the structural property that an unhandled exception inside
    a handler returns 500 with a stable error string, NOT a traceback
    in the response body."""

    def setUp(self):
        # Save the real method so we can restore after each test.
        self._orig_status_handler = CivicMeshHandler._do_GET_inner

    def tearDown(self):
        CivicMeshHandler._do_GET_inner = self._orig_status_handler

    def test_handler_exception_does_not_leak_traceback(self):
        # Replace _do_GET_inner with a raiser. The wrapper's except-Exception
        # branch should produce 500 + stable JSON, with the traceback
        # going to the server log only.
        marker = "synthetic-boom-civ86"

        def _raiser(self):
            raise RuntimeError(marker)

        CivicMeshHandler._do_GET_inner = _raiser
        # Truncate any prior log records so our assertion is clean.
        self.log_buffer.truncate(0)
        self.log_buffer.seek(0)

        status, raw = _request(self.server.port, "GET", "/anything")

        self.assertEqual(status, 500)
        self.assertEqual(_json_body(raw), {"error": "internal error"})

        body_text = raw.decode("utf-8", errors="replace").lower()
        for token in ("traceback", "runtimeerror", marker.lower()):
            self.assertNotIn(token, body_text,
                             f"response body must not contain {token!r}: "
                             f"{body_text!r}")

        # The traceback should be in the log, however — that's where
        # operators look. This pins the asymmetry: client sees stable
        # error, log sees full detail.
        log_text = self.log_buffer.getvalue()
        self.assertIn("Traceback", log_text,
                      "server log MUST contain the traceback")
        self.assertIn(marker, log_text,
                      "server log MUST contain the original exception message")


if __name__ == "__main__":
    unittest.main()
