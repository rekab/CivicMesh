"""CIV-14 event tap: pin the redaction invariant.

The catch-all DEBUG event logger in `mesh_bot._setup_mesh_client` must
never log full message content — AGENTS.md "Security log must not
include full message content." The redaction logic lives in the
module-level `_summarize_event_payload` so this test can hold the line
without spinning up the whole mesh client.
"""
from __future__ import annotations

import unittest

import mesh_bot


class SummarizeEventPayloadTest(unittest.TestCase):
    def test_content_fields_redacted_to_length(self):
        """The three content fields the meshcore library uses for
        user-readable text must surface as a `_len` instead of the
        original value. Pins the AGENTS.md content-redaction invariant
        — adding a content-bearing key in the future means adding it
        to `_EVENT_CONTENT_FIELDS`, not to a one-off call site."""
        payload = {
            "text": "this is a private DM that must not hit the log file",
            "content": "another secret",
            "message": b"\x01\x02\x03 binary",
            "sender": "callsign123",
            "snr": 9.5,
        }
        summary = mesh_bot._summarize_event_payload(payload)
        self.assertNotIn("text", summary)
        self.assertNotIn("content", summary)
        self.assertNotIn("message", summary)
        self.assertEqual(summary["text_len"], len(payload["text"]))
        self.assertEqual(summary["content_len"], len(payload["content"]))
        self.assertEqual(summary["message_len"], len(payload["message"]))
        # Non-content scalars pass through unchanged — useful for triage.
        self.assertEqual(summary["sender"], "callsign123")
        self.assertEqual(summary["snr"], 9.5)

    def test_content_field_with_non_str_value_records_none_len(self):
        # Defensive: a future library version might dispatch a content
        # field as a dict or None. We must not leak it; we still
        # surface the key so the operator sees the field existed.
        payload = {"text": None, "content": {"nested": "leaky"}}
        summary = mesh_bot._summarize_event_payload(payload)
        self.assertIsNone(summary["text_len"])
        self.assertIsNone(summary["content_len"])
        self.assertNotIn("content", summary)

    def test_bytes_field_summarised_as_length(self):
        payload = {"sig": b"\x00" * 64, "path": b"\xab\xcd"}
        summary = mesh_bot._summarize_event_payload(payload)
        self.assertEqual(summary["sig"], "bytes(64)")
        self.assertEqual(summary["path"], "bytes(2)")

    def test_nested_value_summarised_as_type_name(self):
        payload = {"contact": {"name": "x"}, "items": [1, 2, 3]}
        summary = mesh_bot._summarize_event_payload(payload)
        self.assertEqual(summary["contact"], "dict")
        self.assertEqual(summary["items"], "list")

    def test_non_dict_payload_passes_through(self):
        # Some library events carry a scalar payload (e.g. an int
        # status code). The caller logs it with %r — we just return
        # it unchanged.
        self.assertEqual(mesh_bot._summarize_event_payload(42), 42)
        self.assertEqual(mesh_bot._summarize_event_payload("ok"), "ok")
        self.assertIsNone(mesh_bot._summarize_event_payload(None))


if __name__ == "__main__":
    unittest.main()
