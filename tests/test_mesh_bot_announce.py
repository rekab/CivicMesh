"""CIV-11: throttle-logic unit tests for mesh_bot._announce_identity.

Tests the helper in isolation — no mesh stack, no serial, no asyncio
plumbing beyond `asyncio.run`. The real `meshcore.events.EventType`
enum is not imported (mesh_bot itself imports it lazily); we use a
stand-in object whose `.OK` and `.ERROR` attributes are sentinels that
match what `_announce_identity` compares against (`result.type ==
EventType.ERROR`).
"""
from __future__ import annotations

import asyncio
import logging
import types
import unittest
from dataclasses import dataclass
from unittest.mock import AsyncMock, patch

import mesh_bot


# Sentinel EventType. mesh_bot's helper only inspects EventType.ERROR.
class _ET:
    OK = "OK"
    ERROR = "ERROR"


@dataclass
class _Event:
    type: str
    payload: object = None


def _ok() -> _Event:
    return _Event(type=_ET.OK)


def _err(payload: str = "boom") -> _Event:
    return _Event(type=_ET.ERROR, payload=payload)


def _make_client(set_name_result=None, send_advert_result=None):
    """Mock mesh_client whose .commands.set_name / .send_advert are
    AsyncMocks. Defaults both to OK events."""
    if set_name_result is None:
        set_name_result = _ok()
    if send_advert_result is None:
        send_advert_result = _ok()
    client = types.SimpleNamespace()
    client.commands = types.SimpleNamespace(
        set_name=AsyncMock(return_value=set_name_result),
        send_advert=AsyncMock(return_value=send_advert_result),
    )
    return client


def _make_cfg(callsign: str = "civic1"):
    return types.SimpleNamespace(
        node=types.SimpleNamespace(callsign=callsign),
    )


def _run(coro):
    return asyncio.run(coro)


class AnnounceIdentityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.log = logging.getLogger("test_mesh_bot_announce")

    def test_first_connect_calls_set_name_and_advert(self) -> None:
        client = _make_client()
        cfg = _make_cfg("civic1")
        state: dict = {"last_callsign": None, "last_ts": None}
        with patch("mesh_bot.time.time", return_value=1000.0):
            _run(mesh_bot._announce_identity(client, cfg, self.log, state, _ET))
        client.commands.set_name.assert_called_once_with("civic1")
        client.commands.send_advert.assert_called_once_with(flood=False)
        self.assertEqual(state["last_callsign"], "civic1")
        self.assertEqual(state["last_ts"], 1000.0)

    def test_within_cooldown_skips_advert_keeps_set_name(self) -> None:
        client = _make_client()
        cfg = _make_cfg("civic1")
        state: dict = {"last_callsign": "civic1", "last_ts": 1000.0}
        # 60 seconds later — well inside the 6h cooldown.
        with patch("mesh_bot.time.time", return_value=1060.0):
            _run(mesh_bot._announce_identity(client, cfg, self.log, state, _ET))
        client.commands.set_name.assert_called_once_with("civic1")
        client.commands.send_advert.assert_not_called()
        # State untouched.
        self.assertEqual(state["last_callsign"], "civic1")
        self.assertEqual(state["last_ts"], 1000.0)

    def test_cooldown_elapsed_re_adverts(self) -> None:
        client = _make_client()
        cfg = _make_cfg("civic1")
        state: dict = {"last_callsign": "civic1", "last_ts": 1000.0}
        elapsed = 1000.0 + mesh_bot._ADVERT_COOLDOWN_SEC + 1
        with patch("mesh_bot.time.time", return_value=elapsed):
            _run(mesh_bot._announce_identity(client, cfg, self.log, state, _ET))
        client.commands.set_name.assert_called_once_with("civic1")
        client.commands.send_advert.assert_called_once_with(flood=False)
        self.assertEqual(state["last_callsign"], "civic1")
        self.assertEqual(state["last_ts"], elapsed)

    def test_callsign_change_re_adverts(self) -> None:
        client = _make_client()
        cfg = _make_cfg("fremont1")
        # Recent ts but a different callsign.
        state: dict = {"last_callsign": "civic1", "last_ts": 1000.0}
        with patch("mesh_bot.time.time", return_value=1060.0):
            _run(mesh_bot._announce_identity(client, cfg, self.log, state, _ET))
        client.commands.set_name.assert_called_once_with("fremont1")
        client.commands.send_advert.assert_called_once_with(flood=False)
        self.assertEqual(state["last_callsign"], "fremont1")
        self.assertEqual(state["last_ts"], 1060.0)

    def test_set_name_failure_skips_advert(self) -> None:
        client = _make_client(set_name_result=_err("set_name no"))
        cfg = _make_cfg("civic1")
        state: dict = {"last_callsign": None, "last_ts": None}
        with patch("mesh_bot.time.time", return_value=1000.0):
            _run(mesh_bot._announce_identity(client, cfg, self.log, state, _ET))
        client.commands.set_name.assert_called_once_with("civic1")
        client.commands.send_advert.assert_not_called()
        # State unchanged so the next connect retries the advert.
        self.assertIsNone(state["last_callsign"])
        self.assertIsNone(state["last_ts"])

    def test_advert_failure_does_not_update_state(self) -> None:
        client = _make_client(send_advert_result=_err("advert no"))
        cfg = _make_cfg("civic1")
        state: dict = {"last_callsign": None, "last_ts": None}
        with patch("mesh_bot.time.time", return_value=1000.0):
            _run(mesh_bot._announce_identity(client, cfg, self.log, state, _ET))
        client.commands.set_name.assert_called_once_with("civic1")
        client.commands.send_advert.assert_called_once_with(flood=False)
        # Failure path: state untouched so the next connect retries.
        self.assertIsNone(state["last_callsign"])
        self.assertIsNone(state["last_ts"])


if __name__ == "__main__":
    unittest.main()
