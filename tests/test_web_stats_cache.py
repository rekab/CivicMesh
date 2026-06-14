"""Cache-Control contract for JSON endpoints.

/api/stats is live telemetry the SPA polls; it MUST send Cache-Control:
no-store so a browser never serves a pre-deploy payload whose shape the
freshly-deployed app.js no longer matches (the "blank radio tiles after
deploy" bug). Other JSON endpoints are intentionally left untouched —
this test pins both halves so neither silently regresses.
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


def _render_config(db_path: str, log_dir: str) -> str:
    return "\n".join([
        f'db_path = "{db_path}"',
        "",
        "[node]", 'site_name = "TestHub"', 'callsign = "test1"', "",
        "[network]", 'ip = "10.0.0.1"', 'subnet_cidr = "10.0.0.0/24"',
        'iface = "wlan0"', 'country_code = "US"',
        'dhcp_range_start = "10.0.0.10"', 'dhcp_range_end = "10.0.0.250"',
        'dhcp_lease = "15m"', "",
        "[ap]", 'ssid = "CivicMesh-Test"', "channel = 6", "",
        "[channels]", 'names = ["#civicmesh-test"]', "",
        "[web]", "port = 8080", "",
        "[logging]", f'log_dir = "{log_dir}"', 'log_level = "WARNING"', "",
    ])


class _Server:
    def __init__(self, cfg, db_cfg, tmpdir):
        log = logging.getLogger("test_web_stats_cache")
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


class StatsCacheControlTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.mkdtemp(prefix="civicmesh_stats_cache_")
        db_path = os.path.join(cls.tmpdir, "test.db")
        log_dir = os.path.join(cls.tmpdir, "logs")
        os.makedirs(log_dir, exist_ok=True)
        cfg_path = os.path.join(cls.tmpdir, "config.toml")
        with open(cfg_path, "w") as f:
            f.write(_render_config(db_path, log_dir))
        cls.cfg = load_config(cfg_path)
        cls.db_cfg = DBConfig(path=cls.cfg.db_path)
        assert cls.db_cfg.path.startswith(cls.tmpdir), cls.db_cfg.path
        init_db(cls.db_cfg)
        cls.server = _Server(cls.cfg, cls.db_cfg, cls.tmpdir)

    @classmethod
    def tearDownClass(cls):
        cls.server.close()
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def _get(self, path):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.port, timeout=5)
        try:
            conn.request("GET", path)
            resp = conn.getresponse()
            resp.read()
            return resp.status, {k.lower(): v for k, v in resp.getheaders()}
        finally:
            conn.close()

    def test_api_stats_is_no_store(self) -> None:
        status, headers = self._get("/api/stats")
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("cache-control"), "no-store")

    def test_other_json_endpoint_left_uncached(self) -> None:
        # Scoping guard: we deliberately did NOT blanket-apply no-store to
        # every _json endpoint. /api/channels must keep its prior behavior
        # (no Cache-Control header) so this stays a surgical change.
        status, headers = self._get("/api/channels")
        self.assertEqual(status, 200)
        self.assertNotIn("cache-control", headers)


if __name__ == "__main__":
    unittest.main()
