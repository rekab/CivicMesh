"""mesh_bot._process_dm — the async body the DM subscriber spawns.

Pins:
- Registered contact → send_msg called with the reply text
- Unregistered pubkey_prefix → no send, drop with WARN
- Rate-limited contact → no send, drop with WARN
- send_msg returns ERROR → no exception, drop with WARN
- send_msg raises → no exception, drop with WARN
- AGENTS.md log invariant: full message text never appears in log records
- touch_contact_last_seen runs on every accepted inbound DM
"""

import asyncio
import logging
import os
import tempfile
import types
import unittest
from dataclasses import dataclass
from unittest.mock import AsyncMock

import mesh_bot
import dm_bot
from database import (
    DBConfig,
    get_contact_by_pubkey,
    init_db,
    mark_contact_added,
    request_contact_add,
)


_PUBKEY = "1294f888153c0440ed3eedf7752415e7fbd012c4234a89c2bfc04bd215105b89"
_PUBKEY_PREFIX = _PUBKEY[:12]


class _ET:
    OK = "OK"
    ERROR = "ERROR"


@dataclass
class _Event:
    type: str
    payload: object = None


def _ok() -> _Event:
    return _Event(type=_ET.OK)


def _err(payload=None) -> _Event:
    return _Event(type=_ET.ERROR, payload=payload or {})


def _make_cfg(site_name="TestNode", dm_per_hour=6, power_stale_after_sec=180):
    return types.SimpleNamespace(
        node=types.SimpleNamespace(site_name=site_name),
        limits=types.SimpleNamespace(dm_responses_per_hour=dm_per_hour),
        # _process_dm reads stale_after_sec to gate the battery line freshness.
        power_monitor=types.SimpleNamespace(stale_after_sec=power_stale_after_sec),
    )


def _make_mesh_client(send_result=None):
    if send_result is None:
        send_result = _ok()
    client = types.SimpleNamespace()
    client.commands = types.SimpleNamespace(
        send_msg=AsyncMock(return_value=send_result),
    )
    return client


class _LogCapture(logging.Handler):
    """Capture every log record emitted during a test for invariant checks."""

    def __init__(self):
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


def _capture_logs() -> tuple[logging.Logger, _LogCapture]:
    log = logging.getLogger(f"test_dm_handler.{os.getpid()}.{id(object())}")
    log.handlers.clear()
    log.setLevel(logging.DEBUG)
    cap = _LogCapture()
    cap.setLevel(logging.DEBUG)
    log.addHandler(cap)
    log.propagate = False
    return log, cap


class _DBBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db_cfg = DBConfig(path=self.tmp.name)
        init_db(self.db_cfg)

    def tearDown(self) -> None:
        try:
            os.unlink(self.tmp.name)
        except FileNotFoundError:
            pass

    def _register(self, pubkey: str) -> None:
        request_contact_add(self.db_cfg, pubkey=pubkey, _ts_for_test=1_700_000_000)
        mark_contact_added(self.db_cfg, pubkey=pubkey)


class RegisteredContactReplyTest(_DBBase):
    def test_help_command_triggers_send_msg(self):
        self._register(_PUBKEY)
        log, cap = _capture_logs()
        client = _make_mesh_client()
        rl = dm_bot.DMRateLimiter(per_hour=6)
        asyncio.run(mesh_bot._process_dm(
            {"pubkey_prefix": _PUBKEY_PREFIX, "text": "help"},
            cfg=_make_cfg(), db_cfg=self.db_cfg, log=log,
            mesh_client=client, dm_rate_limiter=rl, EventType=_ET,
        ))
        # send_msg called exactly once with the full pubkey + help body
        self.assertEqual(client.commands.send_msg.call_count, 1)
        args, _ = client.commands.send_msg.call_args
        self.assertEqual(args[0], {"public_key": _PUBKEY})
        self.assertIn("CivicMesh @ TestNode", args[1])
        self.assertIn("help", args[1])
        # last_seen updated
        row = get_contact_by_pubkey(self.db_cfg, pubkey=_PUBKEY)
        self.assertIsNotNone(row["last_seen"])

    def test_stats_command_includes_quota(self):
        self._register(_PUBKEY)
        log, _ = _capture_logs()
        client = _make_mesh_client()
        rl = dm_bot.DMRateLimiter(per_hour=10)
        asyncio.run(mesh_bot._process_dm(
            {"pubkey_prefix": _PUBKEY_PREFIX, "text": "stats"},
            cfg=_make_cfg(dm_per_hour=10), db_cfg=self.db_cfg, log=log,
            mesh_client=client, dm_rate_limiter=rl, EventType=_ET,
        ))
        reply = client.commands.send_msg.call_args[0][1]
        # 1 token consumed → 9 left of 10
        self.assertIn("9/10", reply)


class UnregisteredDroppedTest(_DBBase):
    def test_unregistered_pubkey_drops_without_send(self):
        # NO _register call — contact doesn't exist in DB
        log, cap = _capture_logs()
        client = _make_mesh_client()
        rl = dm_bot.DMRateLimiter(per_hour=6)
        asyncio.run(mesh_bot._process_dm(
            {"pubkey_prefix": _PUBKEY_PREFIX, "text": "help"},
            cfg=_make_cfg(), db_cfg=self.db_cfg, log=log,
            mesh_client=client, dm_rate_limiter=rl, EventType=_ET,
        ))
        self.assertEqual(client.commands.send_msg.call_count, 0)
        warnings = [r for r in cap.records if r.levelno == logging.WARNING]
        self.assertTrue(
            any("drop_unregistered" in r.getMessage() for r in warnings),
            [r.getMessage() for r in warnings],
        )

    def test_pending_contact_treated_as_unregistered(self):
        # Row exists but status='pending' — worker hasn't pushed it yet
        request_contact_add(self.db_cfg, pubkey=_PUBKEY, _ts_for_test=1_700_000_000)
        log, _ = _capture_logs()
        client = _make_mesh_client()
        rl = dm_bot.DMRateLimiter(per_hour=6)
        asyncio.run(mesh_bot._process_dm(
            {"pubkey_prefix": _PUBKEY_PREFIX, "text": "help"},
            cfg=_make_cfg(), db_cfg=self.db_cfg, log=log,
            mesh_client=client, dm_rate_limiter=rl, EventType=_ET,
        ))
        self.assertEqual(client.commands.send_msg.call_count, 0)


class RateLimitedDroppedTest(_DBBase):
    def test_drops_when_over_quota(self):
        self._register(_PUBKEY)
        log, cap = _capture_logs()
        client = _make_mesh_client()
        rl = dm_bot.DMRateLimiter(per_hour=2)
        # First two should send; third drops
        for _ in range(3):
            asyncio.run(mesh_bot._process_dm(
                {"pubkey_prefix": _PUBKEY_PREFIX, "text": "help"},
                cfg=_make_cfg(), db_cfg=self.db_cfg, log=log,
                mesh_client=client, dm_rate_limiter=rl, EventType=_ET,
            ))
        self.assertEqual(client.commands.send_msg.call_count, 2)
        self.assertTrue(
            any("rate_limited" in r.getMessage()
                for r in cap.records if r.levelno == logging.WARNING)
        )


class SendFailureTest(_DBBase):
    def test_send_msg_error_logged_no_raise(self):
        self._register(_PUBKEY)
        log, cap = _capture_logs()
        client = _make_mesh_client(send_result=_err({"code_string": "ERR_X"}))
        rl = dm_bot.DMRateLimiter(per_hour=6)
        asyncio.run(mesh_bot._process_dm(
            {"pubkey_prefix": _PUBKEY_PREFIX, "text": "help"},
            cfg=_make_cfg(), db_cfg=self.db_cfg, log=log,
            mesh_client=client, dm_rate_limiter=rl, EventType=_ET,
        ))
        self.assertEqual(client.commands.send_msg.call_count, 1)
        self.assertTrue(
            any("send_failed" in r.getMessage()
                for r in cap.records if r.levelno == logging.WARNING)
        )

    def test_send_msg_exception_logged_no_raise(self):
        self._register(_PUBKEY)
        log, cap = _capture_logs()
        client = _make_mesh_client()
        client.commands.send_msg = AsyncMock(side_effect=RuntimeError("radio gone"))
        rl = dm_bot.DMRateLimiter(per_hour=6)
        asyncio.run(mesh_bot._process_dm(
            {"pubkey_prefix": _PUBKEY_PREFIX, "text": "help"},
            cfg=_make_cfg(), db_cfg=self.db_cfg, log=log,
            mesh_client=client, dm_rate_limiter=rl, EventType=_ET,
        ))
        self.assertTrue(
            any("send_exception" in r.getMessage()
                for r in cap.records if r.levelno == logging.WARNING)
        )


class LogContentInvariantTest(_DBBase):
    """AGENTS.md: 'Security log must not include full message content.'

    Sender's body must never appear verbatim in any log record. Token
    name (first word) is allowed; the full text is not.
    """

    def test_full_message_body_never_logged(self):
        self._register(_PUBKEY)
        log, cap = _capture_logs()
        client = _make_mesh_client()
        rl = dm_bot.DMRateLimiter(per_hour=6)
        secret_body = "stats my-very-secret-token-12345"
        asyncio.run(mesh_bot._process_dm(
            {"pubkey_prefix": _PUBKEY_PREFIX, "text": secret_body},
            cfg=_make_cfg(), db_cfg=self.db_cfg, log=log,
            mesh_client=client, dm_rate_limiter=rl, EventType=_ET,
        ))
        for r in cap.records:
            msg = r.getMessage()
            self.assertNotIn(
                "my-very-secret-token-12345", msg,
                f"log record leaked DM body: {msg!r}",
            )


if __name__ == "__main__":
    unittest.main()
