"""`civicmesh contact list/pin/unpin/remove` admin CLI.

Covers the DB helpers (resolve, list, set_pinned, delete) and the
two confirm-then-act handlers (_handle_contact_remove). The argparse
glue is exercised end-to-end by invoking _cmd_* with a built
Namespace.
"""

import argparse
import io
import logging
import os
import sqlite3
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

import civicmesh
from database import (
    ContactLookupError,
    DBConfig,
    delete_contact,
    get_contact_by_pubkey,
    init_db,
    list_contacts,
    mark_contact_added,
    request_contact_add,
    resolve_contact_pubkey,
    set_contact_pinned,
)


_PK_A = "a" * 64
_PK_B = "b" * 64
# Two pubkeys sharing the same 12-char prefix (12 c's) but differing
# at char 13; used to test ambiguous-prefix rejection.
_PK_C = "c" * 12 + "1" + "0" * 51
_PK_C_ALT = "c" * 12 + "2" + "0" * 51


def _capture_stdout(fn, *a, **kw) -> tuple[object, str]:
    buf = io.StringIO()
    with redirect_stdout(buf):
        result = fn(*a, **kw)
    return result, buf.getvalue()


class _DBTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.cfg = DBConfig(path=self.tmp.name)
        init_db(self.cfg)

    def tearDown(self) -> None:
        try:
            os.unlink(self.tmp.name)
        except FileNotFoundError:
            pass

    def _register(self, pubkey: str) -> None:
        request_contact_add(self.cfg, pubkey=pubkey, _ts_for_test=1_700_000_000)
        mark_contact_added(self.cfg, pubkey=pubkey)


# ---------------------------------------------------------------------------
# resolve_contact_pubkey
# ---------------------------------------------------------------------------


class ResolveContactPubkeyTest(_DBTestBase):
    def test_full_pubkey_returns_match(self) -> None:
        self._register(_PK_A)
        out = resolve_contact_pubkey(self.cfg, query=_PK_A)
        self.assertEqual(out, _PK_A)

    def test_full_pubkey_uppercase_normalized(self) -> None:
        self._register(_PK_A)
        out = resolve_contact_pubkey(self.cfg, query=_PK_A.upper())
        self.assertEqual(out, _PK_A)

    def test_full_pubkey_no_match_raises(self) -> None:
        self._register(_PK_A)
        with self.assertRaises(ContactLookupError):
            resolve_contact_pubkey(self.cfg, query=_PK_B)

    def test_prefix_unique_match(self) -> None:
        self._register(_PK_A)
        self._register(_PK_B)
        out = resolve_contact_pubkey(self.cfg, query="aaaaaaaaaaaa")  # 12 'a'
        self.assertEqual(out, _PK_A)

    def test_prefix_no_match_raises(self) -> None:
        self._register(_PK_A)
        with self.assertRaises(ContactLookupError) as cm:
            resolve_contact_pubkey(self.cfg, query="ffffffffffff")
        self.assertIn("no contact matches", str(cm.exception))

    def test_prefix_ambiguous_raises(self) -> None:
        # Two contacts share the prefix 'cccccc'.
        self._register(_PK_C)
        self._register(_PK_C_ALT)
        with self.assertRaises(ContactLookupError) as cm:
            resolve_contact_pubkey(self.cfg, query="cccccccccccc")
        self.assertIn("multiple", str(cm.exception))

    def test_prefix_too_short_rejected(self) -> None:
        self._register(_PK_A)
        with self.assertRaises(ContactLookupError) as cm:
            resolve_contact_pubkey(self.cfg, query="aaaa")  # 4 chars
        self.assertIn("12-63 char prefix", str(cm.exception))

    def test_non_hex_input_rejected(self) -> None:
        self._register(_PK_A)
        with self.assertRaises(ContactLookupError) as cm:
            resolve_contact_pubkey(self.cfg, query="not-hex-input")
        self.assertIn("not hex", str(cm.exception))

    def test_empty_input_rejected(self) -> None:
        with self.assertRaises(ContactLookupError):
            resolve_contact_pubkey(self.cfg, query="")

    def test_resolves_evicted_contact(self) -> None:
        # Operators must be able to clean up evicted rows too.
        self._register(_PK_A)
        conn = sqlite3.connect(self.cfg.path)
        try:
            conn.execute(
                "UPDATE contacts SET status='evicted' WHERE pubkey=?", (_PK_A,),
            )
            conn.commit()
        finally:
            conn.close()
        out = resolve_contact_pubkey(self.cfg, query=_PK_A)
        self.assertEqual(out, _PK_A)


# ---------------------------------------------------------------------------
# list_contacts / set_contact_pinned / delete_contact
# ---------------------------------------------------------------------------


class ListContactsTest(_DBTestBase):
    def test_returns_all_when_unfiltered(self) -> None:
        self._register(_PK_A)
        request_contact_add(self.cfg, pubkey=_PK_B, _ts_for_test=1_700_000_500)
        rows = list_contacts(self.cfg)
        self.assertEqual(len(rows), 2)

    def test_ordered_by_created_at_desc(self) -> None:
        request_contact_add(self.cfg, pubkey=_PK_A, _ts_for_test=1_700_000_000)
        request_contact_add(self.cfg, pubkey=_PK_B, _ts_for_test=1_700_000_500)
        rows = list_contacts(self.cfg)
        self.assertEqual(rows[0]["pubkey"], _PK_B)  # newer first
        self.assertEqual(rows[1]["pubkey"], _PK_A)

    def test_status_filter(self) -> None:
        self._register(_PK_A)
        request_contact_add(self.cfg, pubkey=_PK_B, _ts_for_test=1_700_000_500)
        rows = list_contacts(self.cfg, status="added")
        self.assertEqual([r["pubkey"] for r in rows], [_PK_A])

    def test_pinned_filter(self) -> None:
        self._register(_PK_A)
        self._register(_PK_B)
        set_contact_pinned(self.cfg, pubkey=_PK_A, pinned=True)
        rows = list_contacts(self.cfg, pinned=True)
        self.assertEqual([r["pubkey"] for r in rows], [_PK_A])
        rows = list_contacts(self.cfg, pinned=False)
        self.assertEqual([r["pubkey"] for r in rows], [_PK_B])

    def test_combined_status_and_pinned(self) -> None:
        self._register(_PK_A)
        self._register(_PK_B)
        set_contact_pinned(self.cfg, pubkey=_PK_A, pinned=True)
        # PK_B added but unpinned should drop out of (added, pinned=True)
        rows = list_contacts(self.cfg, status="added", pinned=True)
        self.assertEqual([r["pubkey"] for r in rows], [_PK_A])


class SetContactPinnedTest(_DBTestBase):
    def test_pin_then_unpin_round_trip(self) -> None:
        self._register(_PK_A)
        self.assertEqual(set_contact_pinned(self.cfg, pubkey=_PK_A, pinned=True), 1)
        row = get_contact_by_pubkey(self.cfg, pubkey=_PK_A)
        self.assertEqual(row["pinned"], 1)
        self.assertEqual(set_contact_pinned(self.cfg, pubkey=_PK_A, pinned=False), 1)
        row = get_contact_by_pubkey(self.cfg, pubkey=_PK_A)
        self.assertEqual(row["pinned"], 0)

    def test_missing_pubkey_returns_zero_rowcount(self) -> None:
        self.assertEqual(
            set_contact_pinned(self.cfg, pubkey=_PK_A, pinned=True), 0,
        )


class DeleteContactTest(_DBTestBase):
    def test_delete_removes_row(self) -> None:
        self._register(_PK_A)
        self.assertEqual(delete_contact(self.cfg, pubkey=_PK_A), 1)
        self.assertIsNone(get_contact_by_pubkey(self.cfg, pubkey=_PK_A))

    def test_delete_missing_pubkey_returns_zero(self) -> None:
        self.assertEqual(delete_contact(self.cfg, pubkey=_PK_A), 0)


# ---------------------------------------------------------------------------
# CLI handler integration: _cmd_contact_pin / unpin / list / remove
# ---------------------------------------------------------------------------


def _ns(**fields) -> argparse.Namespace:
    """Build an argparse Namespace with `cmd='contact'` and config path
    pre-filled. Tests pass --config explicitly because _load_runtime
    requires it.
    """
    return argparse.Namespace(cmd="contact", **fields)


class CLIHandlersTest(_DBTestBase):
    """Drive the handlers end-to-end via _cmd_contact_*.

    Patches _load_runtime to skip the real config + log setup — those
    paths are tested elsewhere and reconstructing a fully-valid
    config.toml for every CLI test would add ~30 mostly-irrelevant
    fields. The handler's mid-section (resolve → mutate → print) is
    what we actually want to exercise here.
    """

    def setUp(self) -> None:
        super().setUp()
        cfg = types.SimpleNamespace()  # _load_runtime returns (cfg, log, db_cfg)
        log = logging.getLogger(f"test_contact_cli.{id(self)}")
        log.addHandler(logging.NullHandler())
        log.propagate = False
        self._runtime_patch = patch.object(
            civicmesh, "_load_runtime",
            return_value=(cfg, log, self.cfg),
        )
        self._runtime_patch.start()
        # init_db is a no-op here (we already initialized in _DBTestBase),
        # but the handlers call it; let it run normally against our DB.

    def tearDown(self) -> None:
        self._runtime_patch.stop()
        super().tearDown()

    def _ns(self, **kw) -> argparse.Namespace:
        return _ns(config=None, **kw)

    def test_list_empty_prints_no_contacts(self) -> None:
        ns = self._ns(contact_cmd="list", status=None,
                      pinned=False, unpinned=False)
        _, out = _capture_stdout(civicmesh._cmd_contact_list, ns)
        self.assertIn("no contacts", out)

    def test_list_prints_header_and_rows(self) -> None:
        self._register(_PK_A)
        ns = self._ns(contact_cmd="list", status=None,
                      pinned=False, unpinned=False)
        _, out = _capture_stdout(civicmesh._cmd_contact_list, ns)
        self.assertIn("PUBKEY", out)
        self.assertIn("STATUS", out)
        self.assertIn(_PK_A[:12], out)
        self.assertIn("added", out)

    def test_pin_then_unpin(self) -> None:
        self._register(_PK_A)
        ns_pin = self._ns(contact_cmd="pin", pubkey=_PK_A)
        _, out = _capture_stdout(civicmesh._cmd_contact_pin, ns_pin)
        self.assertIn("pinned=1", out)
        self.assertEqual(
            get_contact_by_pubkey(self.cfg, pubkey=_PK_A)["pinned"], 1,
        )
        ns_unpin = self._ns(contact_cmd="unpin", pubkey=_PK_A)
        _, out = _capture_stdout(civicmesh._cmd_contact_unpin, ns_unpin)
        self.assertIn("unpinned=1", out)
        self.assertEqual(
            get_contact_by_pubkey(self.cfg, pubkey=_PK_A)["pinned"], 0,
        )

    def test_pin_prefix_lookup(self) -> None:
        self._register(_PK_A)
        ns = self._ns(contact_cmd="pin", pubkey="aaaaaaaaaaaa")
        _, out = _capture_stdout(civicmesh._cmd_contact_pin, ns)
        self.assertIn("pinned=1", out)

    def test_pin_unknown_pubkey_exits_with_message(self) -> None:
        ns = self._ns(contact_cmd="pin", pubkey=_PK_A)
        # Capture both stdout and stderr; assert SystemExit.
        stderr_buf = io.StringIO()
        old_stderr = sys.stderr
        sys.stderr = stderr_buf
        try:
            with self.assertRaises(SystemExit):
                civicmesh._cmd_contact_pin(ns)
        finally:
            sys.stderr = old_stderr
        self.assertIn("no contact", stderr_buf.getvalue())

    def test_remove_skip_confirmation_deletes(self) -> None:
        self._register(_PK_A)
        ns = self._ns(contact_cmd="remove", pubkey=_PK_A,
                      skip_confirmation=True)
        _, out = _capture_stdout(civicmesh._cmd_contact_remove, ns)
        self.assertIn("removed=1", out)
        self.assertIsNone(get_contact_by_pubkey(self.cfg, pubkey=_PK_A))

    def test_remove_prompts_when_not_skipping(self) -> None:
        self._register(_PK_A)
        # Drive the prompt via the handler directly so we can inject input_fn.
        _, out = _capture_stdout(
            civicmesh._handle_contact_remove, self.cfg,
            pubkey=_PK_A, skip_confirmation=False, input_fn=lambda _: "y",
        )
        self.assertIn("removed=1", out)

    def test_remove_declined_keeps_row(self) -> None:
        self._register(_PK_A)
        _, out = _capture_stdout(
            civicmesh._handle_contact_remove, self.cfg,
            pubkey=_PK_A, skip_confirmation=False, input_fn=lambda _: "n",
        )
        self.assertIn("removed=0", out)
        self.assertIsNotNone(get_contact_by_pubkey(self.cfg, pubkey=_PK_A))

    def test_remove_missing_pubkey_returns_zero(self) -> None:
        # Resolve fails before remove, so this is the same path as
        # test_pin_unknown_pubkey_exits_with_message.
        ns = self._ns(contact_cmd="remove", pubkey=_PK_A,
                      skip_confirmation=True)
        stderr_buf = io.StringIO()
        old_stderr = sys.stderr
        sys.stderr = stderr_buf
        try:
            with self.assertRaises(SystemExit):
                civicmesh._cmd_contact_remove(ns)
        finally:
            sys.stderr = old_stderr
        self.assertIn("no contact", stderr_buf.getvalue())


if __name__ == "__main__":
    unittest.main()
