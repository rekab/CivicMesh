"""Probe-handler regression tests for CIV-82 (always-captive).

Every OS captive-portal probe URL must 302 to /welcome unconditionally.
There is no longer any "accepted" state that flips probes to vendor-
specific success bodies — see docs/captive-portal-precedent.md §2.
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


_MINIMAL_CONFIG_SRC = os.path.join(
    os.path.dirname(__file__), "apply", "goldens", "minimal-config.toml"
)


PROBE_PATHS = [
    "/generate_204",
    "/gen_204",
    "/hotspot-detect.html",
    "/library/test/success.html",
    "/connecttest.txt",
    "/ncsi.txt",
]


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
        cfg_path = os.path.join(cls.tmpdir, "config.toml")
        with open(_MINIMAL_CONFIG_SRC, "r") as src, open(cfg_path, "w") as dst:
            dst.write(src.read())
            dst.write(f'\ndb_path = "{os.path.join(cls.tmpdir, "test.db")}"\n')

        cls.cfg = load_config(cfg_path)
        cls.db_cfg = DBConfig(path=cls.cfg.db_path)
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
