"""Probe-handler regression tests for CIV-82 (always-captive).

Every OS captive-portal probe URL must 302 to /welcome unconditionally.
There is no longer any "accepted" state that flips probes to vendor-
specific success bodies — see docs/captive-portal-precedent.md §2
"The two-state trap".
"""

import http.client
import http.server
import logging
import os
import shutil
import tempfile
import threading
import unittest

from config import load_config
from database import DBConfig, init_db
from web_server import CivicMeshHandler


PROBE_PATHS = [
    "/generate_204",
    "/gen_204",
    "/hotspot-detect.html",
    "/library/test/success.html",
    "/connecttest.txt",
    "/ncsi.txt",
]


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


class _ProbeServer:
    """Minimal stand-in for web_server._Server, runs on an ephemeral port."""

    def __init__(self, cfg, db_cfg, tmpdir):
        log = logging.getLogger("test_web_probe")
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


class TestAlwaysCaptive(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.mkdtemp(prefix="civicmesh_probe_test_")
        db_path = os.path.join(cls.tmpdir, "test.db")
        sections = {
            "node":     {"site_name": "TestHub", "callsign": "test1"},
            "network":  dict(_BASE_NETWORK),
            "ap":       {"ssid": "CivicMesh-Test", "channel": 6},
            "channels": {"names": ["#civicmesh-test"]},
            "web":      {"port": 8080, "portal_aliases": ["civicmesh.internal"]},
            "logging":  {"log_dir": os.path.join(cls.tmpdir, "logs"),
                         "log_level": "WARNING"},
        }
        cfg_path = os.path.join(cls.tmpdir, "config.toml")
        with open(cfg_path, "w") as f:
            f.write(_render_test_config(db_path, sections))

        cls.cfg = load_config(cfg_path)
        cls.db_cfg = DBConfig(path=cls.cfg.db_path)
        # Sandbox assertion: see test_web_messages_api.py for the rationale.
        # This file previously had the prepend-after-sections bug, so it had
        # been silently opening the dev DB on every run.
        assert cls.db_cfg.path.startswith(cls.tmpdir), (
            f"test DB escaped sandbox: {cls.db_cfg.path!r} "
            f"(expected to be under {cls.tmpdir!r})"
        )
        init_db(cls.db_cfg, log=logging.getLogger("test_web_probe"))
        cls.server = _ProbeServer(cls.cfg, cls.db_cfg, cls.tmpdir)

    @classmethod
    def tearDownClass(cls):
        cls.server.close()
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def _get(self, path):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.port, timeout=5)
        try:
            conn.request("GET", path)
            resp = conn.getresponse()
            return resp.status, dict(resp.getheaders()), resp.read()
        finally:
            conn.close()

    def test_every_probe_url_redirects_to_welcome(self):
        expected_location_suffix = "/welcome"
        for path in PROBE_PATHS:
            with self.subTest(path=path):
                status, headers, body = self._get(path)
                self.assertEqual(status, 302, f"{path} did not 302")
                location = headers.get("Location", "")
                self.assertTrue(
                    location.endswith(expected_location_suffix),
                    f"{path} -> Location={location!r}, expected to end with {expected_location_suffix!r}",
                )
                self.assertEqual(body, b"", f"{path} body should be empty for 302")

    def test_portal_accept_redirects_home(self):
        status, headers, _ = self._get("/portal-accept")
        self.assertEqual(status, 302)
        location = headers.get("Location", "")
        self.assertTrue(
            location.endswith("/"),
            f"/portal-accept -> Location={location!r}, expected to end with '/'",
        )

    def test_portal_accept_does_not_mutate_server_state(self):
        # The portal_accepted dict was removed in CIV-82; this test documents
        # the invariant by asserting the attribute is absent on the server.
        self.assertFalse(
            hasattr(self.server.srv, "portal_accepted"),
            "_Server should not have a portal_accepted attribute post-CIV-82",
        )
        self._get("/portal-accept")
        self.assertFalse(
            hasattr(self.server.srv, "portal_accepted"),
            "/portal-accept must not introduce portal_accepted state",
        )


if __name__ == "__main__":
    unittest.main()
