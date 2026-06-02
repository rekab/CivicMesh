"""mesh_bot._evict_one_contact — three-tier LRU eviction.

Pins:
- Tier 3 (firmware-only) → smallest last_advert evicted
- Tier 3 empty, tier 2 (added, unpinned) → smallest last_seen evicted, NULL-first
- All firmware contacts pinned → (None, 'no_candidate'), no remove_contact call
- remove_contact failure → (None, 'remove_failed'), no DB mutation
- Tier-2 eviction flips DB row to 'evicted' so get_contact_by_pubkey_prefix
  ignores it (the DM handler stops accepting their messages)
"""

import asyncio
import logging
import os
import sqlite3
import tempfile
import types
import unittest
from dataclasses import dataclass
from unittest.mock import AsyncMock

import mesh_bot
from database import (
    DBConfig,
    get_contact_by_pubkey,
    get_contact_by_pubkey_prefix,
    init_db,
    mark_contact_added,
    request_contact_add,
)


def _pk(byte_str: str) -> str:
    """Build a 64-hex pubkey from a 1- or 2-char seed for readability."""
    if len(byte_str) == 1:
        return byte_str * 64
    return (byte_str * 32)[:64]


# Three real-shape pubkeys.
PK_REG_A = _pk("a")  # registered in DB
PK_REG_B = _pk("b")
PK_REG_C = _pk("c")
PK_FW_ONLY_1 = _pk("11")  # in firmware only, no DB row
PK_FW_ONLY_2 = _pk("22")
PK_NEW = _pk("ff")  # the incoming registration we're making room for


class _ET:
    OK = "OK"
    ERROR = "ERROR"


@dataclass
class _Event:
    type: str
    payload: object = None


def _ok() -> _Event:
    return _Event(type=_ET.OK)


def _err() -> _Event:
    return _Event(type=_ET.ERROR, payload={})


def _capture_logs() -> tuple[logging.Logger, list[logging.LogRecord]]:
    log = logging.getLogger(f"test_eviction.{os.getpid()}.{id(object())}")
    log.handlers.clear()
    log.setLevel(logging.DEBUG)
    records: list[logging.LogRecord] = []

    class _H(logging.Handler):
        def emit(self, r):
            records.append(r)

    h = _H()
    h.setLevel(logging.DEBUG)
    log.addHandler(h)
    log.propagate = False
    return log, records


def _make_mc(firmware_contacts: dict[str, dict],
             remove_result=None) -> types.SimpleNamespace:
    """Mock meshcore client. firmware_contacts maps pubkey → contact dict
    (must contain last_advert). get_contacts() is a no-op that returns OK;
    mc.contacts is the dict callers inspect."""
    if remove_result is None:
        remove_result = _ok()
    mc = types.SimpleNamespace()
    mc.contacts = dict(firmware_contacts)

    async def _get_contacts():
        return _ok()

    async def _remove_contact(pubkey_bytes: bytes):
        if (
            isinstance(remove_result, _Event)
            and remove_result.type == _ET.OK
        ):
            mc.contacts.pop(pubkey_bytes.hex(), None)
        return remove_result

    mc.commands = types.SimpleNamespace(
        get_contacts=AsyncMock(side_effect=_get_contacts),
        remove_contact=AsyncMock(side_effect=_remove_contact),
    )
    return mc


def _patch_event_type(test_case):
    """Temporarily install our fake EventType into mesh_bot."""
    saved = mesh_bot.EventType
    mesh_bot.EventType = _ET
    test_case.addCleanup(lambda: setattr(mesh_bot, "EventType", saved))


class _DBBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db_cfg = DBConfig(path=self.tmp.name)
        init_db(self.db_cfg)
        _patch_event_type(self)

    def tearDown(self) -> None:
        try:
            os.unlink(self.tmp.name)
        except FileNotFoundError:
            pass

    def _register(self, pubkey: str, *, last_seen=None, pinned=0) -> None:
        request_contact_add(self.db_cfg, pubkey=pubkey, _ts_for_test=1_700_000_000)
        mark_contact_added(self.db_cfg, pubkey=pubkey)
        if last_seen is not None or pinned:
            conn = sqlite3.connect(self.db_cfg.path)
            try:
                conn.execute(
                    "UPDATE contacts SET last_seen=?, pinned=? WHERE pubkey=?",
                    (last_seen, pinned, pubkey),
                )
                conn.commit()
            finally:
                conn.close()


class Tier3OnlyTest(_DBBase):
    def test_evicts_firmware_only_contact_with_smallest_last_advert(self):
        # Two firmware-only contacts; PK_FW_ONLY_2 is staler.
        mc = _make_mc({
            PK_FW_ONLY_1: {"last_advert": 5000},
            PK_FW_ONLY_2: {"last_advert": 1000},
        })
        log, recs = _capture_logs()

        evicted, tier = asyncio.run(
            mesh_bot._evict_one_contact(mc, self.db_cfg, log)
        )

        self.assertEqual(evicted, PK_FW_ONLY_2)
        self.assertEqual(tier, "tier3")
        # remove_contact called once with the staler firmware contact.
        self.assertEqual(mc.commands.remove_contact.call_count, 1)
        called_with = mc.commands.remove_contact.call_args[0][0]
        self.assertEqual(called_with.hex(), PK_FW_ONLY_2)
        # No DB log entry for tier-3 (nothing to mark).
        self.assertFalse(any(
            "contacts:evicted_db" in r.getMessage() for r in recs
        ))

    def test_tier3_preferred_even_when_tier2_exists(self):
        # A tier-3 contact (no DB row) AND a tier-2 contact (added, unpinned).
        # Tier 3 must win regardless of tier-2 last_seen.
        self._register(PK_REG_A, last_seen=1)  # very stale tier 2
        mc = _make_mc({
            PK_REG_A: {"last_advert": 9999},
            PK_FW_ONLY_1: {"last_advert": 50},
        })
        log, _ = _capture_logs()

        evicted, tier = asyncio.run(
            mesh_bot._evict_one_contact(mc, self.db_cfg, log)
        )

        self.assertEqual(evicted, PK_FW_ONLY_1)
        self.assertEqual(tier, "tier3")
        # PK_REG_A row was not touched.
        row = get_contact_by_pubkey(self.db_cfg, pubkey=PK_REG_A)
        self.assertEqual(row["status"], "added")


class Tier2LRUTest(_DBBase):
    def test_smallest_last_seen_evicted(self):
        self._register(PK_REG_A, last_seen=2000)
        self._register(PK_REG_B, last_seen=1000)  # stalest
        self._register(PK_REG_C, last_seen=3000)
        mc = _make_mc({
            PK_REG_A: {"last_advert": 0},
            PK_REG_B: {"last_advert": 0},
            PK_REG_C: {"last_advert": 0},
        })
        log, _ = _capture_logs()

        evicted, tier = asyncio.run(
            mesh_bot._evict_one_contact(mc, self.db_cfg, log)
        )

        self.assertEqual(evicted, PK_REG_B)
        self.assertEqual(tier, "tier2")
        # PK_REG_B flipped to 'evicted' in DB.
        row = get_contact_by_pubkey(self.db_cfg, pubkey=PK_REG_B)
        self.assertEqual(row["status"], "evicted")
        self.assertIn("LRU eviction at", row["error_detail"])

    def test_null_last_seen_evicted_first(self):
        # PK_REG_A has last_seen=5000 (recent); PK_REG_B has NULL (never
        # DMed us). NULL-first policy says PK_REG_B goes.
        self._register(PK_REG_A, last_seen=5000)
        self._register(PK_REG_B, last_seen=None)
        mc = _make_mc({
            PK_REG_A: {"last_advert": 0},
            PK_REG_B: {"last_advert": 0},
        })
        log, _ = _capture_logs()

        evicted, tier = asyncio.run(
            mesh_bot._evict_one_contact(mc, self.db_cfg, log)
        )

        self.assertEqual(evicted, PK_REG_B)
        self.assertEqual(tier, "tier2")


class PinnedNeverEvictedTest(_DBBase):
    def test_all_pinned_returns_no_candidate(self):
        self._register(PK_REG_A, last_seen=1, pinned=1)
        self._register(PK_REG_B, last_seen=2, pinned=1)
        mc = _make_mc({
            PK_REG_A: {"last_advert": 0},
            PK_REG_B: {"last_advert": 0},
        })
        log, recs = _capture_logs()

        evicted, reason = asyncio.run(
            mesh_bot._evict_one_contact(mc, self.db_cfg, log)
        )

        self.assertIsNone(evicted)
        self.assertEqual(reason, "no_candidate")
        # remove_contact must not have been called.
        self.assertEqual(mc.commands.remove_contact.call_count, 0)
        # Pinned rows unchanged.
        for pk in (PK_REG_A, PK_REG_B):
            row = get_contact_by_pubkey(self.db_cfg, pubkey=pk)
            self.assertEqual(row["status"], "added")
            self.assertEqual(row["pinned"], 1)
        # WARN logged with pinned count for operator visibility.
        msgs = [r.getMessage() for r in recs if r.levelno == logging.WARNING]
        self.assertTrue(
            any("eviction_no_candidate" in m and "pinned_count=2" in m
                for m in msgs),
            msgs,
        )

    def test_pinned_never_picked_even_with_oldest_last_seen(self):
        # PK_REG_A is pinned with last_seen=1 (oldest possible).
        # PK_REG_B is unpinned with last_seen=5000 (recent).
        # Unpinned must win despite the time delta — pinning is absolute.
        self._register(PK_REG_A, last_seen=1, pinned=1)
        self._register(PK_REG_B, last_seen=5000, pinned=0)
        mc = _make_mc({
            PK_REG_A: {"last_advert": 0},
            PK_REG_B: {"last_advert": 0},
        })
        log, _ = _capture_logs()

        evicted, tier = asyncio.run(
            mesh_bot._evict_one_contact(mc, self.db_cfg, log)
        )

        self.assertEqual(evicted, PK_REG_B)
        self.assertEqual(tier, "tier2")
        # Pinned row stays added.
        row = get_contact_by_pubkey(self.db_cfg, pubkey=PK_REG_A)
        self.assertEqual(row["status"], "added")


class RemoveFailureTest(_DBBase):
    def test_remove_contact_error_does_not_mark_evicted(self):
        self._register(PK_REG_A, last_seen=1)
        mc = _make_mc(
            {PK_REG_A: {"last_advert": 0}},
            remove_result=_err(),
        )
        log, recs = _capture_logs()

        evicted, reason = asyncio.run(
            mesh_bot._evict_one_contact(mc, self.db_cfg, log)
        )

        self.assertIsNone(evicted)
        self.assertEqual(reason, "remove_failed")
        # DB row unchanged — eviction is atomic from the caller's view.
        row = get_contact_by_pubkey(self.db_cfg, pubkey=PK_REG_A)
        self.assertEqual(row["status"], "added")
        # WARN logged.
        self.assertTrue(any(
            "eviction_remove_failed" in r.getMessage()
            for r in recs if r.levelno == logging.WARNING
        ))


class GetByPrefixExcludesEvictedTest(_DBBase):
    """An evicted row must stop matching the DM handler's prefix lookup,
    or we'd keep replying to a contact whose firmware slot is gone."""

    def test_prefix_lookup_returns_none_for_evicted(self):
        self._register(PK_REG_A, last_seen=1)
        mc = _make_mc({PK_REG_A: {"last_advert": 0}})
        log, _ = _capture_logs()

        asyncio.run(mesh_bot._evict_one_contact(mc, self.db_cfg, log))

        # Sanity: row is in 'evicted'.
        row = get_contact_by_pubkey(self.db_cfg, pubkey=PK_REG_A)
        self.assertEqual(row["status"], "evicted")
        # Prefix lookup (what CONTACT_MSG_RECV uses) returns None for an
        # evicted contact.
        hit = get_contact_by_pubkey_prefix(
            self.db_cfg, pubkey_prefix=PK_REG_A[:12],
        )
        self.assertIsNone(hit)


class FirmwareEmptyTest(_DBBase):
    def test_returns_firmware_empty_when_mc_contacts_empty(self):
        # Firmware reports zero contacts but the worker says full —
        # we refuse to evict (could be a readback glitch).
        mc = _make_mc({})
        log, recs = _capture_logs()

        evicted, reason = asyncio.run(
            mesh_bot._evict_one_contact(mc, self.db_cfg, log)
        )

        self.assertIsNone(evicted)
        self.assertEqual(reason, "firmware_empty")
        self.assertEqual(mc.commands.remove_contact.call_count, 0)


if __name__ == "__main__":
    unittest.main()
