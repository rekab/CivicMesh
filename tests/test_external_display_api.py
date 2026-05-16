"""Phase 0 stub tests for /api/external-display/state.

The endpoint is always registered. Behavior is gated by
external_display.enabled in config.toml:
  - disabled (or section absent): 404 with JSON {"error": "not found"}
  - enabled: 200 with the v0 hardcoded payload (api_version=1)

See docs/external-display-api.md for the full contract.
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
from database import DBConfig, init_db
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
    @classmethod
    def setUpClass(cls):
        sections = _base_sections(tempfile.gettempdir())
        sections["external_display"] = {"enabled": True}
        _bring_up(cls, sections, "test_extdisp_on")

    @classmethod
    def tearDownClass(cls):
        _tear_down(cls)

    def test_returns_200_with_valid_v0_payload(self):
        status, headers, body = _get(self.server.port, "/api/external-display/state")
        self.assertEqual(status, 200)
        self.assertIn("application/json", headers.get("Content-Type", ""))
        payload = json.loads(body)

        self.assertEqual(payload["api_version"], 1)

        hub = payload["hub"]
        self.assertIsInstance(hub["site_name"], str)
        self.assertTrue(hub["site_name"])
        self.assertIsInstance(hub["callsign"], str)
        self.assertTrue(hub["callsign"])

        messages = payload["messages"]
        self.assertIsInstance(messages, list)
        self.assertGreater(len(messages), 0)
        for m in messages:
            self.assertIsInstance(m["id"], int)
            self.assertIsInstance(m["channel"], str)
            self.assertIsInstance(m["sender"], str)
            self.assertIsInstance(m["body"], str)


if __name__ == "__main__":
    unittest.main()
