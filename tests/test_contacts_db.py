"""DB helpers for the CIV-14 contact-add flow.

Covers schema shape, request_contact_add idempotency, worker status
transitions, and the wall-clock stamping invariant.
"""

import os
import sqlite3
import tempfile
import unittest

from database import (
    DBConfig,
    get_contact_by_pubkey,
    get_pending_contacts,
    init_db,
    mark_contact_added,
    mark_contact_error,
    request_contact_add,
)


_PK_A = "a" * 64
_PK_B = "b" * 64


class _DBTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.cfg = DBConfig(path=self.tmp.name)
        init_db(self.cfg)

    def tearDown(self) -> None:
        os.unlink(self.tmp.name)


class SchemaTest(_DBTestBase):
    def test_contacts_table_has_expected_columns(self) -> None:
        conn = sqlite3.connect(self.cfg.path)
        try:
            cols = [r[1] for r in conn.execute("PRAGMA table_info(contacts)").fetchall()]
            self.assertEqual(
                cols,
                ["pubkey", "status", "error_detail", "pinned", "created_at", "last_seen"],
            )
            idx = [
                r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type='index' AND tbl_name='contacts'"
                ).fetchall()
            ]
            self.assertIn("idx_contacts_pending", idx)
        finally:
            conn.close()


class RequestContactAddTest(_DBTestBase):
    def test_first_request_creates_pending_row(self) -> None:
        status = request_contact_add(self.cfg, pubkey=_PK_A, _ts_for_test=1_700_000_000)
        self.assertEqual(status, "pending")
        row = get_contact_by_pubkey(self.cfg, pubkey=_PK_A)
        self.assertEqual(row["status"], "pending")
        self.assertEqual(row["error_detail"], None)
        self.assertEqual(row["pinned"], 0)
        self.assertEqual(row["created_at"], 1_700_000_000)
        self.assertIsNone(row["last_seen"])

    def test_lowercases_pubkey(self) -> None:
        request_contact_add(self.cfg, pubkey=_PK_A.upper(), _ts_for_test=1_700_000_000)
        row = get_contact_by_pubkey(self.cfg, pubkey=_PK_A)
        self.assertEqual(row["pubkey"], _PK_A)

    def test_short_circuits_on_already_added(self) -> None:
        request_contact_add(self.cfg, pubkey=_PK_A, _ts_for_test=1_700_000_000)
        mark_contact_added(self.cfg, pubkey=_PK_A)
        # Second request should NOT churn the row; status stays 'added'.
        status = request_contact_add(self.cfg, pubkey=_PK_A, _ts_for_test=1_700_000_999)
        self.assertEqual(status, "added")
        row = get_contact_by_pubkey(self.cfg, pubkey=_PK_A)
        self.assertEqual(row["status"], "added")
        # created_at stays at the original add time — short-circuit didn't rewrite.
        self.assertEqual(row["created_at"], 1_700_000_000)

    def test_resets_error_row_to_pending(self) -> None:
        request_contact_add(self.cfg, pubkey=_PK_A, _ts_for_test=1_700_000_000)
        mark_contact_error(
            self.cfg, pubkey=_PK_A,
            status="error_table_full", detail="ERR_CODE_TABLE_FULL",
        )
        # Re-request: status flips back to pending, error_detail cleared.
        status = request_contact_add(self.cfg, pubkey=_PK_A, _ts_for_test=1_700_000_500)
        self.assertEqual(status, "pending")
        row = get_contact_by_pubkey(self.cfg, pubkey=_PK_A)
        self.assertEqual(row["status"], "pending")
        self.assertIsNone(row["error_detail"])


class WorkerTransitionsTest(_DBTestBase):
    def test_mark_added_clears_error_detail(self) -> None:
        request_contact_add(self.cfg, pubkey=_PK_A, _ts_for_test=1_700_000_000)
        mark_contact_error(
            self.cfg, pubkey=_PK_A,
            status="error_other", detail="some leftover detail",
        )
        mark_contact_added(self.cfg, pubkey=_PK_A)
        row = get_contact_by_pubkey(self.cfg, pubkey=_PK_A)
        self.assertEqual(row["status"], "added")
        self.assertIsNone(row["error_detail"])

    def test_mark_error_rejects_unknown_status(self) -> None:
        request_contact_add(self.cfg, pubkey=_PK_A, _ts_for_test=1_700_000_000)
        with self.assertRaises(ValueError):
            mark_contact_error(self.cfg, pubkey=_PK_A, status="error_typo")

    def test_get_pending_returns_only_pending_oldest_first(self) -> None:
        request_contact_add(self.cfg, pubkey=_PK_A, _ts_for_test=1_700_000_000)
        request_contact_add(self.cfg, pubkey=_PK_B, _ts_for_test=1_700_000_100)
        mark_contact_added(self.cfg, pubkey=_PK_A)
        pending = get_pending_contacts(self.cfg, limit=10)
        self.assertEqual([r["pubkey"] for r in pending], [_PK_B])

    def test_get_pending_respects_limit(self) -> None:
        for i, pk in enumerate(["c", "d", "e"]):
            request_contact_add(
                self.cfg, pubkey=pk * 64,
                _ts_for_test=1_700_000_000 + i,
            )
        pending = get_pending_contacts(self.cfg, limit=2)
        self.assertEqual(len(pending), 2)
        self.assertEqual(pending[0]["pubkey"], "c" * 64)
        self.assertEqual(pending[1]["pubkey"], "d" * 64)


class MinimumContactDictPinTest(unittest.TestCase):
    """Pin the field shape the mesh_bot worker passes to firmware
    add_contact. This shape was empirically verified by
    diagnostics/radio/minimum_contact_probe.py against the Fremonster
    phone (both TX and RX legs PASSed). If a future change drops any
    field or alters a type, the meshcore_py update_contact will raise
    KeyError or the firmware will reject the call — neither failure
    mode surfaces well at runtime in a relay context."""

    def test_minimum_contact_shape(self) -> None:
        from mesh_bot import _build_minimum_contact_dict
        d = _build_minimum_contact_dict(_PK_A.upper())
        self.assertEqual(d["public_key"], _PK_A)
        self.assertEqual(d["adv_name"], "")
        self.assertEqual(d["type"], 1)
        self.assertEqual(d["flags"], 0)
        self.assertEqual(d["out_path"], "")
        self.assertEqual(d["out_path_len"], -1)
        self.assertEqual(d["out_path_hash_mode"], 0)
        self.assertEqual(d["last_advert"], 0)
        self.assertEqual(d["adv_lat"], 0.0)
        self.assertEqual(d["adv_lon"], 0.0)
        # The library's update_contact reads every key directly; missing
        # any one raises KeyError. Pin the exact field set.
        self.assertEqual(
            set(d.keys()),
            {
                "public_key", "adv_name", "type", "flags",
                "out_path", "out_path_len", "out_path_hash_mode",
                "last_advert", "adv_lat", "adv_lon",
            },
        )


if __name__ == "__main__":
    unittest.main()
