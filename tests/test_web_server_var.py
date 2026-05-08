"""CIV-92 regression tests for /var/ static-serve plumbing.

Covers the discovery endpoint at /var/index.json, slug-whitelist
enforcement, Content-Disposition attachment for PDFs,
directory-listing refusal, and captive-portal redirect for
non-portal Host headers.
"""

import http.client
import http.server
import json
import logging
import os
import pathlib
import shutil
import tempfile
import threading
import unittest

from config import load_config
from database import DBConfig, init_db
from web_server import CivicMeshHandler


_PORTAL_HOST = "10.0.0.1"  # matches network.ip in the inline test config
_PDF_BYTES = b"%PDF-1.4\n%dummy\n%%EOF\n"


_BASE_NETWORK = {
    "ip": _PORTAL_HOST,
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


class _VarServer:
    """In-process server bound to a caller-controlled var_dir."""

    def __init__(self, cfg, db_cfg, var_dir, tmpdir, log):
        static_dir = os.path.join(
            os.path.dirname(os.path.abspath(os.path.dirname(__file__))),
            "static",
        )

        class _S(http.server.ThreadingHTTPServer):
            def __init__(self, *a, **kw):
                super().__init__(*a, **kw)
                self.cfg = cfg
                self.db_cfg = db_cfg
                self.log = log
                self.sec = log
                self.var_dir = pathlib.Path(var_dir)
                self.feedback_path = os.path.join(tmpdir, "feedback.jsonl")

        def handler(*a, **kw):
            return CivicMeshHandler(*a, directory=static_dir, **kw)

        self.srv = _S(("127.0.0.1", 0), handler)
        self.port = self.srv.server_port
        self.thread = threading.Thread(
            target=self.srv.serve_forever, daemon=True,
        )
        self.thread.start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()
        self.thread.join(timeout=2)


class _VarTestBase(unittest.TestCase):
    """Per-test fresh server bound to a fresh var_dir."""

    LOGGER_NAME = "test_web_server_var"

    @classmethod
    def setUpClass(cls):
        cls._cls_tmp = tempfile.mkdtemp(prefix="civicmesh_var_test_cls_")
        db_path = os.path.join(cls._cls_tmp, "test.db")
        sections = {
            "node":     {"site_name": "TestHub", "callsign": "test1"},
            "network":  dict(_BASE_NETWORK),
            "ap":       {"ssid": "CivicMesh-Test", "channel": 6},
            "channels": {"names": ["#civicmesh-test"]},
            "web":      {"port": 8080, "portal_aliases": ["civicmesh.internal"]},
            "logging":  {"log_dir": os.path.join(cls._cls_tmp, "logs"),
                         "log_level": "WARNING"},
        }
        cfg_path = os.path.join(cls._cls_tmp, "config.toml")
        with open(cfg_path, "w") as f:
            f.write(_render_test_config(db_path, sections))
        cls.cfg = load_config(cfg_path)
        cls.db_cfg = DBConfig(path=cls.cfg.db_path)
        # Sandbox assertion: see test_web_messages_api.py for the rationale.
        # This file previously had the prepend-after-sections bug.
        assert cls.db_cfg.path.startswith(cls._cls_tmp), (
            f"test DB escaped sandbox: {cls.db_cfg.path!r} "
            f"(expected to be under {cls._cls_tmp!r})"
        )
        init_db(cls.db_cfg, log=logging.getLogger(cls.LOGGER_NAME))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls._cls_tmp, ignore_errors=True)

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="civicmesh_var_test_")
        self.var_dir = pathlib.Path(self.tmpdir) / "var"
        # var_dir intentionally NOT mkdir'd by default — tests opt in.
        self.log = logging.getLogger(self.LOGGER_NAME)
        # Make sure log records propagate / aren't dropped
        self.log.setLevel(logging.DEBUG)
        if not self.log.handlers:
            self.log.addHandler(logging.NullHandler())
        self.server = _VarServer(
            self.cfg, self.db_cfg, self.var_dir, self.tmpdir, self.log,
        )

    def tearDown(self):
        self.server.close()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _get(self, path, *, host=_PORTAL_HOST, headers=None):
        conn = http.client.HTTPConnection(
            "127.0.0.1", self.server.port, timeout=5,
        )
        try:
            hdrs = {"Host": host}
            if headers:
                hdrs.update(headers)
            conn.request("GET", path, headers=hdrs)
            resp = conn.getresponse()
            # resp.headers is case-insensitive; keep that contract.
            return resp.status, resp.headers, resp.read()
        finally:
            conn.close()

    def _make_library(self, slug, *, index_obj=None, files=None):
        d = self.var_dir / slug
        d.mkdir(parents=True, exist_ok=True)
        if index_obj is None:
            index_obj = {
                "schema_version": 1,
                "title": f"{slug.title()} Library",
                "source_label": "Test",
                "note": "Test note",
                "categories": [],
            }
        (d / "index.json").write_text(
            json.dumps(index_obj), encoding="utf-8",
        )
        for name, body in (files or {}).items():
            (d / name).write_bytes(body)
        return d


class TestVarDiscovery(_VarTestBase):
    def test_index_empty_no_var_dir(self):
        # var/ does not exist
        status, headers, body = self._get("/var/index.json")
        self.assertEqual(status, 200)
        self.assertEqual(
            headers.get("Content-Type"), "application/json; charset=utf-8",
        )
        self.assertEqual(json.loads(body), {"libraries": []})

    def test_index_one_library(self):
        self._make_library("hub-docs")
        status, _, body = self._get("/var/index.json")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"libraries": ["hub-docs"]})

    def test_index_two_libraries_lex_sorted(self):
        # Create in non-lex order; expect lex-sorted output.
        self._make_library("radio-docs")
        self._make_library("hub-docs")
        status, _, body = self._get("/var/index.json")
        self.assertEqual(status, 200)
        self.assertEqual(
            json.loads(body), {"libraries": ["hub-docs", "radio-docs"]},
        )

    def test_index_skips_missing_index_json(self):
        # A bare dir without index.json (e.g. hub-docs.releases/) should
        # not appear and should not warn.
        self.var_dir.mkdir()
        (self.var_dir / "hub-docs.releases").mkdir()
        with self.assertNoLogs(self.LOGGER_NAME, level="WARNING"):
            status, _, body = self._get("/var/index.json")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"libraries": []})

    def test_index_skips_unparseable_index_json(self):
        d = self.var_dir / "hub-docs"
        d.mkdir(parents=True)
        (d / "index.json").write_bytes(b"not valid json {{{")
        with self.assertLogs(self.LOGGER_NAME, level="WARNING") as cm:
            status, _, body = self._get("/var/index.json")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"libraries": []})
        self.assertTrue(
            any("var:unparseable_index" in m for m in cm.output),
            f"expected unparseable warning, got {cm.output!r}",
        )

    def test_index_skips_non_object_index_json(self):
        d = self.var_dir / "hub-docs"
        d.mkdir(parents=True)
        (d / "index.json").write_text("[]", encoding="utf-8")
        with self.assertLogs(self.LOGGER_NAME, level="WARNING") as cm:
            status, _, body = self._get("/var/index.json")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"libraries": []})
        self.assertTrue(
            any("var:non_object_index" in m for m in cm.output),
            f"expected non-object warning, got {cm.output!r}",
        )

    def test_var_index_cache_control_no_cache(self):
        status, headers, _ = self._get("/var/index.json")
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Cache-Control"), "no-cache")


class TestVarStatic(_VarTestBase):
    def test_serves_per_library_index(self):
        self._make_library(
            "hub-docs",
            index_obj={"schema_version": 1, "title": "Hub Reference Library"},
        )
        status, headers, body = self._get("/var/hub-docs/index.json")
        self.assertEqual(status, 200)
        self.assertIn("Last-Modified", headers)
        parsed = json.loads(body)
        self.assertEqual(parsed["title"], "Hub Reference Library")

    def test_per_library_index_revalidates_304(self):
        self._make_library("hub-docs")
        status, headers, _ = self._get("/var/hub-docs/index.json")
        self.assertEqual(status, 200)
        last_mod = headers.get("Last-Modified")
        self.assertIsNotNone(last_mod)
        status2, _, body2 = self._get(
            "/var/hub-docs/index.json",
            headers={"If-Modified-Since": last_mod},
        )
        self.assertEqual(status2, 304)
        self.assertEqual(body2, b"")

    def test_serves_pdf_with_attachment(self):
        self._make_library(
            "hub-docs", files={"foo.pdf": _PDF_BYTES},
        )
        status, headers, body = self._get("/var/hub-docs/foo.pdf")
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Content-Type"), "application/pdf")
        disp = headers.get("Content-Disposition", "")
        self.assertIn("attachment", disp)
        self.assertIn('filename="foo.pdf"', disp)
        self.assertEqual(body, _PDF_BYTES)

    def test_undiscovered_slug_404(self):
        # `sneaky/` exists with a real file but has no index.json, so its
        # slug isn't discovered → all paths under it must 404.
        d = self.var_dir / "sneaky"
        d.mkdir(parents=True)
        (d / "secret.txt").write_text("nope", encoding="utf-8")
        status, _, _ = self._get("/var/sneaky/secret.txt")
        self.assertEqual(status, 404)

    def test_zip_slip_style_path(self):
        # Translate_path normalizes `..` away — the request must not return
        # a file from outside <var_dir>.
        self._make_library("hub-docs")
        status, _, body = self._get("/var/hub-docs/../etc/passwd")
        self.assertNotEqual(status, 200)
        # Defensive: even if status weren't 200, body must not contain
        # /etc/passwd contents.
        self.assertNotIn(b"root:", body)

    def test_normalized_path_works(self):
        self._make_library(
            "hub-docs", files={"foo.pdf": _PDF_BYTES},
        )
        status, _, body = self._get("/var/hub-docs/sub/../foo.pdf")
        self.assertEqual(status, 200)
        self.assertEqual(body, _PDF_BYTES)

    def test_directory_listing_refused(self):
        self._make_library("hub-docs")
        status, _, body = self._get("/var/hub-docs/")
        self.assertEqual(status, 404)
        # Sanity: not the SimpleHTTPRequestHandler HTML index.
        self.assertNotIn(b"Directory listing", body)

    def test_directory_no_trailing_slash_refused(self):
        self._make_library("hub-docs")
        status, _, _ = self._get("/var/hub-docs")
        self.assertEqual(status, 404)


class TestVarCaptiveRedirect(_VarTestBase):
    def test_captive_probe_to_var_index_redirected(self):
        self._make_library("hub-docs")
        status, headers, body = self._get(
            "/var/index.json", host="some.other.host",
        )
        self.assertEqual(status, 302)
        self.assertTrue(
            headers.get("Location", "").endswith("/"),
            f"unexpected Location {headers.get('Location')!r}",
        )
        self.assertEqual(body, b"")

    def test_captive_probe_to_var_resource_redirected(self):
        self._make_library(
            "hub-docs", files={"foo.pdf": _PDF_BYTES},
        )
        status, headers, body = self._get(
            "/var/hub-docs/foo.pdf", host="some.other.host",
        )
        self.assertEqual(status, 302)
        self.assertTrue(
            headers.get("Location", "").endswith("/"),
            f"unexpected Location {headers.get('Location')!r}",
        )
        # Crucially: the PDF body must not leak past the redirect.
        self.assertNotIn(_PDF_BYTES, body)


if __name__ == "__main__":
    unittest.main()
