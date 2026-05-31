"""CIV-14 onboarding: /api/identity endpoint contract.

Covers the 503-before-mesh_bot-connects path, the 200 success shape, and
the contact_url construction (URL encoding + type=1 companion).
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
from database import DBConfig, init_db, upsert_node_identity
from web_server import CivicMeshHandler


_PUBKEY = "6aa0bd72e35732e05483ec9c3f57c27e54587152cdd287dff520c0bfc46d8531"
_NAME = "6AA0BD72"

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


class _IdentityServer:
    """Minimal stand-in for web_server._Server. Mirrors test_web_messages_api."""

    def __init__(self, cfg, db_cfg, tmpdir):
        log = logging.getLogger("test_api_identity")
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


def _make_cfg_and_db(tmpdir, site_name="TestHub"):
    db_path = os.path.join(tmpdir, "test.db")
    sections = {
        "node":     {"site_name": site_name, "callsign": "test1"},
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


def _get(server, path):
    conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=5)
    try:
        conn.request("GET", path)
        resp = conn.getresponse()
        status = resp.status
        body_raw = resp.read().decode("utf-8")
    finally:
        conn.close()
    try:
        body = json.loads(body_raw) if body_raw else None
    except json.JSONDecodeError:
        body = None
    return status, body


class TestApiIdentity503BeforeConnect(unittest.TestCase):
    """Pre-mesh_bot-connect: /api/identity returns 503 and the SPA hides
    the card. The 503 path is load-bearing — if it ever leaks 200 with
    empty/stale fields, the QR card displays a broken or wrong QR."""

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.mkdtemp(prefix="civicmesh_id_503_")
        cls.cfg, cls.db_cfg = _make_cfg_and_db(cls.tmpdir)
        # NO upsert_node_identity call — that's the point.
        cls.server = _IdentityServer(cls.cfg, cls.db_cfg, cls.tmpdir)

    @classmethod
    def tearDownClass(cls):
        cls.server.close()
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def test_returns_503_when_identity_row_missing(self):
        status, body = _get(self.server, "/api/identity")
        self.assertEqual(status, 503)
        self.assertIsInstance(body, dict)
        self.assertIn("reason", body)


class TestApiIdentitySuccess(unittest.TestCase):
    """Post-mesh_bot-connect: /api/identity returns the contact_url the
    SPA will render as the QR."""

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.mkdtemp(prefix="civicmesh_id_ok_")
        cls.cfg, cls.db_cfg = _make_cfg_and_db(cls.tmpdir)
        upsert_node_identity(
            cls.db_cfg,
            public_key=_PUBKEY,
            name=_NAME,
        )
        cls.server = _IdentityServer(cls.cfg, cls.db_cfg, cls.tmpdir)

    @classmethod
    def tearDownClass(cls):
        cls.server.close()
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def test_returns_200_with_full_shape(self):
        status, body = _get(self.server, "/api/identity")
        self.assertEqual(status, 200)
        self.assertEqual(body["name"], _NAME)
        self.assertEqual(body["public_key"], _PUBKEY)
        self.assertIn("contact_url", body)

    def test_contact_url_is_meshcore_companion_link(self):
        _, body = _get(self.server, "/api/identity")
        url = body["contact_url"]
        self.assertTrue(
            url.startswith("meshcore://contact/add?"),
            f"unexpected URL prefix: {url!r}",
        )
        parsed = urllib.parse.urlparse(url)
        # urlparse treats the scheme:// part fine for non-http schemes;
        # the query is what matters.
        q = urllib.parse.parse_qs(parsed.query)
        self.assertEqual(q["name"], [_NAME])
        self.assertEqual(q["public_key"], [_PUBKEY])
        # type=1 = companion per docs/meshcore-protocol-reference.md §14.2.
        # If the hub ever becomes a room server, this assertion will fire
        # and the QR onboarding flow needs re-validation.
        self.assertEqual(q["type"], ["1"])


class TestApiIdentityUrlEncoding(unittest.TestCase):
    """Non-ASCII / space-containing on-air names must still produce a
    well-formed URL — the server urlencodes the name."""

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.mkdtemp(prefix="civicmesh_id_enc_")
        cls.cfg, cls.db_cfg = _make_cfg_and_db(cls.tmpdir)
        upsert_node_identity(
            cls.db_cfg,
            public_key=_PUBKEY,
            name="Hub One — Capitol",  # spaces + em-dash
        )
        cls.server = _IdentityServer(cls.cfg, cls.db_cfg, cls.tmpdir)

    @classmethod
    def tearDownClass(cls):
        cls.server.close()
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def test_contact_url_round_trips_unicode_name(self):
        _, body = _get(self.server, "/api/identity")
        # The raw response field carries the unicode name; the URL
        # contains the URL-encoded form. Both must round-trip.
        self.assertEqual(body["name"], "Hub One — Capitol")
        parsed = urllib.parse.urlparse(body["contact_url"])
        q = urllib.parse.parse_qs(parsed.query)
        self.assertEqual(q["name"], ["Hub One — Capitol"])


class TestNodeIdentityRoundTrip(unittest.TestCase):
    """DB-level: upsert_node_identity / get_node_identity round-trip plus
    the change-detection return value the WARNING log keys on."""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db_cfg = DBConfig(path=self.tmp.name)
        init_db(self.db_cfg)

    def tearDown(self):
        try:
            os.unlink(self.tmp.name)
        except FileNotFoundError:
            pass

    def test_first_upsert_returns_none(self):
        from database import get_node_identity
        prior = upsert_node_identity(
            self.db_cfg, public_key=_PUBKEY, name=_NAME,
        )
        self.assertIsNone(prior)
        row = get_node_identity(self.db_cfg)
        self.assertEqual(row["public_key"], _PUBKEY)
        self.assertEqual(row["name"], _NAME)

    def test_same_key_upsert_returns_none(self):
        upsert_node_identity(self.db_cfg, public_key=_PUBKEY, name=_NAME)
        prior = upsert_node_identity(
            self.db_cfg, public_key=_PUBKEY, name="renamed",
        )
        self.assertIsNone(prior)

    def test_changed_key_returns_prior(self):
        upsert_node_identity(self.db_cfg, public_key=_PUBKEY, name=_NAME)
        new_key = "ff" * 32
        prior = upsert_node_identity(
            self.db_cfg, public_key=new_key, name="FFFFFFFF",
        )
        self.assertEqual(prior, _PUBKEY)

    def test_singleton_enforced(self):
        """The id=1 CHECK constraint pins this to one row; an upsert always
        overwrites, never appends. Guards against a refactor that drops the
        CHECK and silently grows a multi-row history table."""
        import sqlite3
        upsert_node_identity(self.db_cfg, public_key=_PUBKEY, name=_NAME)
        upsert_node_identity(self.db_cfg, public_key="aa" * 32, name="AAAAAAAA")
        upsert_node_identity(self.db_cfg, public_key="bb" * 32, name="BBBBBBBB")
        conn = sqlite3.connect(self.db_cfg.path)
        try:
            count = conn.execute("SELECT COUNT(*) FROM node_identity").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(count, 1)


if __name__ == "__main__":
    unittest.main()
