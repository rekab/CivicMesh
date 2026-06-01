"""CIV-14 contact-add endpoints: POST /api/contacts + GET status.

Covers:
- POST accepts a raw pubkey OR a meshcore:// URL carrying public_key=
- POST validation rejects wrong-length / non-hex inputs
- POST requires a session cookie
- POST idempotency: re-registering an already-'added' pubkey short-circuits
- GET /api/contacts/<pk>/status returns 404 for unknown / malformed pubkeys
- GET status returns the current row state for known pubkeys
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
    create_or_update_session,
    init_db,
    mark_contact_added,
    request_contact_add,
)
from web_server import CivicMeshHandler


_PUBKEY = "1294f888153c0440ed3eedf7752415e7fbd012c4234a89c2bfc04bd215105b89"
_SID = "contacts-session-aaaaaaaaaaaaaaaa"

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


class _ContactsServer:
    """Minimal stand-in for web_server._Server."""

    def __init__(self, cfg, db_cfg, tmpdir):
        log = logging.getLogger("test_contacts_api")
        log.addHandler(logging.NullHandler())
        static_dir = os.path.join(
            os.path.dirname(os.path.abspath(os.path.dirname(__file__))), "static",
        )

        class _S(http.server.ThreadingHTTPServer):
            def __init__(self, *a, **kw):
                super().__init__(*a, **kw)
                self.cfg = cfg
                self.db_cfg = db_cfg
                self.log = log
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


def _make_cfg_and_db(tmpdir):
    db_path = os.path.join(tmpdir, "test.db")
    sections = {
        "node":     {"site_name": "TestNode", "callsign": "test1"},
        "network":  dict(_BASE_NETWORK),
        "ap":       {"ssid": "CivicMesh-Test", "channel": 6},
        "channels": {"names": ["#civicmesh-test"]},
        "web":      {"port": 8080, "portal_aliases": ["civicmesh.internal"]},
        "logging":  {"log_dir": os.path.join(tmpdir, "logs"),
                     "log_level": "WARNING"},
    }
    cfg_path = os.path.join(tmpdir, "config.toml")
    with open(cfg_path, "w") as f:
        f.write(_render_test_config(db_path, sections))
    cfg = load_config(cfg_path)
    db_cfg = DBConfig(path=cfg.db_path)
    assert db_cfg.path.startswith(tmpdir)
    init_db(db_cfg)
    return cfg, db_cfg


def _post(port, body, cookie=_SID):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        headers = {"Content-Type": "application/json"}
        if cookie is not None:
            headers["Cookie"] = f"civicmesh_session={cookie}"
        conn.request(
            "POST", "/api/contacts",
            body=json.dumps(body) if isinstance(body, (dict, list)) else body,
            headers=headers,
        )
        resp = conn.getresponse()
        status = resp.status
        raw = resp.read().decode("utf-8")
    finally:
        conn.close()
    try:
        payload = json.loads(raw) if raw else None
    except json.JSONDecodeError:
        payload = None
    return status, payload


def _get_status(port, pubkey):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request("GET", f"/api/contacts/{pubkey}/status")
        resp = conn.getresponse()
        status = resp.status
        raw = resp.read().decode("utf-8")
    finally:
        conn.close()
    try:
        payload = json.loads(raw) if raw else None
    except json.JSONDecodeError:
        payload = None
    return status, payload


class _ContactsTestBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.mkdtemp(prefix="civicmesh_contacts_api_")
        cls.cfg, cls.db_cfg = _make_cfg_and_db(cls.tmpdir)
        create_or_update_session(
            cls.db_cfg, session_id=_SID,
            name="poster", location="TestNode",
            mac_address=None,
        )
        cls.server = _ContactsServer(cls.cfg, cls.db_cfg, cls.tmpdir)

    @classmethod
    def tearDownClass(cls):
        cls.server.close()
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def _wipe_contacts(self):
        import sqlite3
        conn = sqlite3.connect(self.db_cfg.path)
        try:
            conn.execute("DELETE FROM contacts")
            conn.commit()
        finally:
            conn.close()


class PostContactsValidationTest(_ContactsTestBase):
    def setUp(self):
        self._wipe_contacts()

    def test_accepts_64char_hex_pubkey(self):
        status, body = _post(self.server.port, {"pubkey": _PUBKEY})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["pubkey"], _PUBKEY)
        self.assertEqual(body["status"], "pending")

    def test_accepts_meshcore_url_with_public_key(self):
        url = (
            "meshcore://contact/add?"
            + urllib.parse.urlencode(
                {"name": "Fremonster", "public_key": _PUBKEY, "type": "1"}
            )
        )
        status, body = _post(self.server.port, {"meshcore_url": url})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["pubkey"], _PUBKEY)
        self.assertEqual(body["status"], "pending")

    def test_uppercases_normalize_to_lowercase(self):
        status, body = _post(
            self.server.port, {"pubkey": _PUBKEY.upper()},
        )
        self.assertEqual(status, 200, body)
        self.assertEqual(body["pubkey"], _PUBKEY)

    def test_rejects_wrong_length(self):
        status, body = _post(
            self.server.port, {"pubkey": _PUBKEY[:32]},
        )
        self.assertEqual(status, 400)
        self.assertIn("error", body)

    def test_rejects_non_hex(self):
        status, body = _post(
            self.server.port,
            {"pubkey": "z" * 64},
        )
        self.assertEqual(status, 400)
        self.assertIn("error", body)

    def test_rejects_missing_fields(self):
        status, body = _post(self.server.port, {})
        self.assertEqual(status, 400)
        self.assertIn("error", body)

    def test_rejects_non_meshcore_scheme(self):
        status, body = _post(
            self.server.port,
            {"meshcore_url": "http://example.com/?public_key=" + _PUBKEY},
        )
        self.assertEqual(status, 400)
        self.assertIn("error", body)

    def test_rejects_meshcore_url_without_public_key(self):
        status, body = _post(
            self.server.port,
            {"meshcore_url": "meshcore://contact/add?name=Anon"},
        )
        self.assertEqual(status, 400)
        self.assertIn("error", body)


class PostContactsSessionGuardTest(_ContactsTestBase):
    def setUp(self):
        self._wipe_contacts()

    def test_rejects_request_without_session_cookie(self):
        status, body = _post(
            self.server.port, {"pubkey": _PUBKEY}, cookie=None,
        )
        self.assertEqual(status, 403)

    def test_rejects_unknown_session(self):
        status, body = _post(
            self.server.port, {"pubkey": _PUBKEY},
            cookie="never-created-this-session-aaaaaaaa",
        )
        self.assertEqual(status, 403)


class PostContactsIdempotencyTest(_ContactsTestBase):
    def setUp(self):
        self._wipe_contacts()

    def test_short_circuits_when_already_added(self):
        # First request: row created in 'pending' state.
        status, body = _post(self.server.port, {"pubkey": _PUBKEY})
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "pending")
        # Simulate the worker landing the row.
        mark_contact_added(self.db_cfg, pubkey=_PUBKEY)
        # Second request: server short-circuits with 'added'.
        status, body = _post(self.server.port, {"pubkey": _PUBKEY})
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "added")


class GetContactStatusTest(_ContactsTestBase):
    def setUp(self):
        self._wipe_contacts()

    def test_404_for_unknown_pubkey(self):
        status, body = _get_status(self.server.port, _PUBKEY)
        self.assertEqual(status, 404)

    def test_404_for_malformed_pubkey(self):
        status, body = _get_status(self.server.port, "notahex")
        self.assertEqual(status, 404)
        status, body = _get_status(self.server.port, "g" * 64)
        self.assertEqual(status, 404)

    def test_returns_pending_status(self):
        request_contact_add(self.db_cfg, pubkey=_PUBKEY)
        status, body = _get_status(self.server.port, _PUBKEY)
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "pending")
        self.assertIsNone(body["error_detail"])

    def test_returns_added_status(self):
        request_contact_add(self.db_cfg, pubkey=_PUBKEY)
        mark_contact_added(self.db_cfg, pubkey=_PUBKEY)
        status, body = _get_status(self.server.port, _PUBKEY)
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "added")


if __name__ == "__main__":
    unittest.main()
