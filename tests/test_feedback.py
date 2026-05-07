import io
import json
import logging
import os
import tempfile
import time
import unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch

from web_server import _recent_feedback_bytes, _FEEDBACK_WINDOW_S, _FEEDBACK_BUDGET_BYTES


def _make_entry(text="hello", ts=None, **extra):
    """Build a single feedback JSONL line."""
    if ts is None:
        ts = datetime.now(timezone.utc).isoformat()
    entry = {"ts": ts, "ip": "10.0.0.1", "location": "test", "text": text}
    entry.update(extra)
    return json.dumps(entry, ensure_ascii=False) + "\n"


class TestRecentFeedbackBytes(unittest.TestCase):
    """Unit tests for the _recent_feedback_bytes helper."""

    def setUp(self):
        self.log = logging.getLogger("test_feedback")

    def test_missing_file_returns_zero(self):
        result = _recent_feedback_bytes("/nonexistent/feedback.jsonl", self.log)
        self.assertEqual(result, 0)

    def test_counts_recent_entries(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "feedback.jsonl")
            now = datetime.now(timezone.utc)
            line1 = _make_entry("first", ts=now.isoformat())
            line2 = _make_entry("second", ts=now.isoformat())
            with open(path, "w") as f:
                f.write(line1)
                f.write(line2)
            result = _recent_feedback_bytes(path, self.log)
            expected = len(line1.encode("utf-8")) + len(line2.encode("utf-8"))
            self.assertEqual(result, expected)

    def test_old_entries_excluded(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "feedback.jsonl")
            now = datetime.now(timezone.utc)
            old_ts = (now - timedelta(hours=13)).isoformat()
            recent_ts = now.isoformat()
            old_line = _make_entry("old", ts=old_ts)
            recent_line = _make_entry("recent", ts=recent_ts)
            with open(path, "w") as f:
                f.write(old_line)
                f.write(recent_line)
            result = _recent_feedback_bytes(path, self.log)
            self.assertEqual(result, len(recent_line.encode("utf-8")))

    def test_unparseable_lines_logged_not_counted(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "feedback.jsonl")
            now = datetime.now(timezone.utc)
            good_line = _make_entry("good", ts=now.isoformat())
            with open(path, "w") as f:
                f.write("not json at all\n")
                f.write('{"ts": "bad-date", "text": "x"}\n')
                f.write(good_line)
            with self.assertLogs("test_feedback", level="WARNING") as cm:
                result = _recent_feedback_bytes(path, self.log)
            # Only the valid recent line should be counted
            self.assertEqual(result, len(good_line.encode("utf-8")))
            # Two unparseable lines should produce two warnings
            self.assertEqual(len(cm.output), 2)


class TestFeedbackPostHandler(unittest.TestCase):
    """Handler-level tests for POST /feedback using io.BytesIO."""

    def _make_handler(self, body_str, feedback_path, db_path=":memory:"):
        """Construct a CivicMeshHandler-like POST /feedback without a live socket."""
        from web_server import CivicMeshHandler
        encoded = body_str.encode("utf-8")

        # Mock server
        server = MagicMock()
        server.cfg.node.site_name = "TestNode"
        server.db_cfg.path = db_path
        server.feedback_path = feedback_path
        server.log = logging.getLogger("test_feedback_handler")
        server.sec = None

        # Build a minimal HTTP request
        request_line = f"POST /feedback HTTP/1.1\r\n"
        headers = (
            f"Content-Type: application/x-www-form-urlencoded\r\n"
            f"Content-Length: {len(encoded)}\r\n"
            f"\r\n"
        )
        rfile = io.BytesIO((request_line + headers).encode("utf-8") + encoded)
        # Skip past the request line — BaseHTTPRequestHandler.parse_request reads it
        rfile.readline()

        wfile = io.BytesIO()

        # Construct handler without triggering __init__'s handle_one_request
        handler = CivicMeshHandler.__new__(CivicMeshHandler)
        handler.server = server
        handler.client_address = ("10.0.0.42", 12345)
        handler.rfile = rfile
        handler.wfile = wfile
        handler.requestline = "POST /feedback HTTP/1.1"
        handler.command = "POST"
        handler.path = "/feedback"
        handler.request_version = "HTTP/1.1"
        handler.headers = {}
        handler.close_connection = True

        # Parse headers from our rfile
        import http.client
        handler.headers = http.client.parse_headers(rfile)

        return handler, wfile

    @patch("web_server._feedback_ctx", return_value={"uptime_s": 100})
    def test_successful_submit(self, _mock_ctx):
        with tempfile.TemporaryDirectory() as tmpdir:
            fb_path = os.path.join(tmpdir, "feedback.jsonl")
            handler, wfile = self._make_handler("text=hello+world", fb_path)
            handler.do_POST()
            wfile.seek(0)
            response = wfile.read().decode("utf-8")
            self.assertIn("303", response)
            self.assertIn("/feedback/thanks", response)
            # Verify file was written
            with open(fb_path) as f:
                line = f.readline()
            entry = json.loads(line)
            self.assertEqual(entry["text"], "hello world")
            self.assertEqual(entry["ip"], "10.0.0.42")
            # CIV-11: feedback entry's "location" field is stamped with
            # cfg.node.site_name (the old node.location was removed).
            self.assertEqual(entry["location"], "TestNode")
            self.assertIn("ts", entry)
            # Verify ts is parseable by fromisoformat (the circuit breaker requirement)
            datetime.fromisoformat(entry["ts"])

    @patch("web_server._feedback_ctx", return_value={})
    def test_empty_text_returns_400(self, _mock_ctx):
        with tempfile.TemporaryDirectory() as tmpdir:
            fb_path = os.path.join(tmpdir, "feedback.jsonl")
            handler, wfile = self._make_handler("text=", fb_path)
            handler.do_POST()
            wfile.seek(0)
            response = wfile.read().decode("utf-8")
            self.assertIn("400", response)
            self.assertFalse(os.path.exists(fb_path))

    @patch("web_server._feedback_ctx", return_value={})
    def test_missing_text_returns_400(self, _mock_ctx):
        with tempfile.TemporaryDirectory() as tmpdir:
            fb_path = os.path.join(tmpdir, "feedback.jsonl")
            handler, wfile = self._make_handler("", fb_path)
            handler.do_POST()
            wfile.seek(0)
            response = wfile.read().decode("utf-8")
            self.assertIn("400", response)

    @patch("web_server._feedback_ctx", return_value={})
    def test_oversized_text_returns_413(self, _mock_ctx):
        with tempfile.TemporaryDirectory() as tmpdir:
            fb_path = os.path.join(tmpdir, "feedback.jsonl")
            big_text = "x" * 2100
            handler, wfile = self._make_handler(f"text={big_text}", fb_path)
            handler.do_POST()
            wfile.seek(0)
            response = wfile.read().decode("utf-8")
            self.assertIn("413", response)
            self.assertFalse(os.path.exists(fb_path))

    @patch("web_server._feedback_ctx", return_value={})
    def test_circuit_breaker_returns_503(self, _mock_ctx):
        with tempfile.TemporaryDirectory() as tmpdir:
            fb_path = os.path.join(tmpdir, "feedback.jsonl")
            # Seed with >1MB of recent entries
            now = datetime.now(timezone.utc).isoformat()
            big_entry = json.dumps({"ts": now, "text": "x" * 500}) + "\n"
            with open(fb_path, "w") as f:
                # Each line is ~530 bytes; need ~2000 lines to exceed 1MB
                for _ in range(2100):
                    f.write(big_entry)
            handler, wfile = self._make_handler("text=hello", fb_path)
            handler.do_POST()
            wfile.seek(0)
            response = wfile.read().decode("utf-8")
            self.assertIn("503", response)


if __name__ == "__main__":
    unittest.main()
