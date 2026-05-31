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
import os
import tempfile
import types
import unittest
from dataclasses import dataclass
from unittest.mock import AsyncMock, patch

import mesh_bot
from database import DBConfig, get_node_identity, init_db


# A 64-hex-char pubkey + 8-char on-air name modelled on real
# diagnostics/radio/runs/*.jsonl self_info samples.
_TEST_PUBKEY = "6aa0bd72e35732e05483ec9c3f57c27e54587152cdd287dff520c0bfc46d8531"
_TEST_NAME = "6AA0BD72"


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


def _make_client(set_name_result=None, send_advert_result=None,
                 self_info=None):
    """Mock mesh_client whose .commands.set_name / .send_advert /
    .send_appstart are AsyncMocks. Defaults all to OK events. The
    `self_info` dict is what `_announce_identity` reads to publish
    node_identity — default has a realistic pubkey+name."""
    if set_name_result is None:
        set_name_result = _ok()
    if send_advert_result is None:
        send_advert_result = _ok()
    if self_info is None:
        self_info = {"public_key": _TEST_PUBKEY, "name": _TEST_NAME}
    client = types.SimpleNamespace()
    client.commands = types.SimpleNamespace(
        set_name=AsyncMock(return_value=set_name_result),
        send_advert=AsyncMock(return_value=send_advert_result),
        send_appstart=AsyncMock(return_value=_ok()),
    )
    client.self_info = self_info
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
        # Real tmpfile DB — _announce_identity calls upsert_node_identity
        # on the success path. Mocking the DB layer would mask a regression
        # in the persistence helper itself.
        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._tmp.close()
        self.db_cfg = DBConfig(path=self._tmp.name)
        init_db(self.db_cfg)

    def tearDown(self) -> None:
        try:
            os.unlink(self._tmp.name)
        except FileNotFoundError:
            pass

    def test_first_connect_calls_set_name_and_advert(self) -> None:
        client = _make_client()
        cfg = _make_cfg("civic1")
        state: dict = {"last_callsign": None, "last_ts": None}
        with patch("mesh_bot.time.monotonic", return_value=1000.0):
            _run(mesh_bot._announce_identity(client, cfg, self.db_cfg, self.log, state, _ET))
        client.commands.set_name.assert_called_once_with("civic1")
        # send_appstart must run after a successful set_name to refresh
        # self_info["name"] for the heard-count echo-match handler.
        client.commands.send_appstart.assert_called_once_with()
        client.commands.send_advert.assert_called_once_with(flood=False)
        self.assertEqual(state["last_callsign"], "civic1")
        self.assertEqual(state["last_ts"], 1000.0)

    def test_within_cooldown_skips_advert_keeps_set_name(self) -> None:
        client = _make_client()
        cfg = _make_cfg("civic1")
        state: dict = {"last_callsign": "civic1", "last_ts": 1000.0}
        # 60 seconds later — well inside the 6h cooldown.
        with patch("mesh_bot.time.monotonic", return_value=1060.0):
            _run(mesh_bot._announce_identity(client, cfg, self.db_cfg, self.log, state, _ET))
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
        with patch("mesh_bot.time.monotonic", return_value=elapsed):
            _run(mesh_bot._announce_identity(client, cfg, self.db_cfg, self.log, state, _ET))
        client.commands.set_name.assert_called_once_with("civic1")
        client.commands.send_advert.assert_called_once_with(flood=False)
        self.assertEqual(state["last_callsign"], "civic1")
        self.assertEqual(state["last_ts"], elapsed)

    def test_callsign_change_re_adverts(self) -> None:
        client = _make_client()
        cfg = _make_cfg("fremont1")
        # Recent ts but a different callsign.
        state: dict = {"last_callsign": "civic1", "last_ts": 1000.0}
        with patch("mesh_bot.time.monotonic", return_value=1060.0):
            _run(mesh_bot._announce_identity(client, cfg, self.db_cfg, self.log, state, _ET))
        client.commands.set_name.assert_called_once_with("fremont1")
        client.commands.send_advert.assert_called_once_with(flood=False)
        self.assertEqual(state["last_callsign"], "fremont1")
        self.assertEqual(state["last_ts"], 1060.0)

    def test_set_name_failure_skips_advert(self) -> None:
        client = _make_client(set_name_result=_err("set_name no"))
        cfg = _make_cfg("civic1")
        state: dict = {"last_callsign": None, "last_ts": None}
        with patch("mesh_bot.time.monotonic", return_value=1000.0):
            _run(mesh_bot._announce_identity(client, cfg, self.db_cfg, self.log, state, _ET))
        client.commands.set_name.assert_called_once_with("civic1")
        # set_name failure: don't refresh self_info (firmware name
        # unchanged), don't advert.
        client.commands.send_appstart.assert_not_called()
        client.commands.send_advert.assert_not_called()
        # State unchanged so the next connect retries the advert.
        self.assertIsNone(state["last_callsign"])
        self.assertIsNone(state["last_ts"])
        # Identity must not be persisted when set_name fails — self_info
        # may still hold the create_serial-time defaults at that point.
        self.assertIsNone(get_node_identity(self.db_cfg))

    def test_advert_failure_does_not_update_state(self) -> None:
        client = _make_client(send_advert_result=_err("advert no"))
        cfg = _make_cfg("civic1")
        state: dict = {"last_callsign": None, "last_ts": None}
        with patch("mesh_bot.time.monotonic", return_value=1000.0):
            _run(mesh_bot._announce_identity(client, cfg, self.db_cfg, self.log, state, _ET))
        client.commands.set_name.assert_called_once_with("civic1")
        client.commands.send_advert.assert_called_once_with(flood=False)
        # Failure path: state untouched so the next connect retries.
        self.assertIsNone(state["last_callsign"])
        self.assertIsNone(state["last_ts"])


class AnnounceIdentityPersistTest(unittest.TestCase):
    def setUp(self) -> None:
        self.log = logging.getLogger("test_mesh_bot_announce_persist")
        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._tmp.close()
        self.db_cfg = DBConfig(path=self._tmp.name)
        init_db(self.db_cfg)

    def tearDown(self) -> None:
        try:
            os.unlink(self._tmp.name)
        except FileNotFoundError:
            pass

    def test_first_connect_persists_identity(self) -> None:
        client = _make_client()
        cfg = _make_cfg("civic1")
        state: dict = {"last_callsign": None, "last_ts": None}
        with patch("mesh_bot.time.monotonic", return_value=1000.0):
            _run(mesh_bot._announce_identity(client, cfg, self.db_cfg, self.log, state, _ET))
        row = get_node_identity(self.db_cfg)
        self.assertIsNotNone(row)
        self.assertEqual(row["public_key"], _TEST_PUBKEY)
        self.assertEqual(row["name"], _TEST_NAME)

    def test_public_key_change_logs_warning(self) -> None:
        client_a = _make_client(self_info={"public_key": _TEST_PUBKEY, "name": _TEST_NAME})
        cfg = _make_cfg("civic1")
        state: dict = {"last_callsign": None, "last_ts": None}
        with patch("mesh_bot.time.monotonic", return_value=1000.0):
            _run(mesh_bot._announce_identity(client_a, cfg, self.db_cfg, self.log, state, _ET))

        # Reflash with a different pubkey + name.
        new_pubkey = "ff" * 32
        new_name = "FFFFFFFF"
        client_b = _make_client(self_info={"public_key": new_pubkey, "name": new_name})
        with self.assertLogs("test_mesh_bot_announce_persist", level="WARNING") as cm:
            with patch("mesh_bot.time.monotonic", return_value=2000.0):
                _run(mesh_bot._announce_identity(client_b, cfg, self.db_cfg, self.log, state, _ET))
        joined = "\n".join(cm.output)
        self.assertIn("identity:public_key_changed", joined)
        self.assertIn(_TEST_PUBKEY[:16], joined)
        self.assertIn(new_pubkey[:16], joined)

        row = get_node_identity(self.db_cfg)
        self.assertEqual(row["public_key"], new_pubkey)
        self.assertEqual(row["name"], new_name)

    def test_missing_self_info_logs_warning_and_skips_persist(self) -> None:
        client = _make_client(self_info={})  # post-appstart self_info empty
        cfg = _make_cfg("civic1")
        state: dict = {"last_callsign": None, "last_ts": None}
        with self.assertLogs("test_mesh_bot_announce_persist", level="WARNING") as cm:
            with patch("mesh_bot.time.monotonic", return_value=1000.0):
                _run(mesh_bot._announce_identity(client, cfg, self.db_cfg, self.log, state, _ET))
        self.assertTrue(any("identity:missing_self_info" in line for line in cm.output))
        self.assertIsNone(get_node_identity(self.db_cfg))
        # The advert path still runs even without identity persistence.
        client.commands.send_advert.assert_called_once_with(flood=False)

    def test_upsert_failure_is_swallowed(self) -> None:
        # If the DB write blows up (locked, disk full, etc), the connect
        # path must not raise — operationally critical: identity is a
        # nice-to-have, the radio relay is not.
        client = _make_client()
        cfg = _make_cfg("civic1")
        state: dict = {"last_callsign": None, "last_ts": None}
        with patch(
            "mesh_bot.upsert_node_identity",
            side_effect=RuntimeError("simulated db failure"),
        ):
            with self.assertLogs("test_mesh_bot_announce_persist", level="WARNING") as cm:
                with patch("mesh_bot.time.monotonic", return_value=1000.0):
                    _run(mesh_bot._announce_identity(client, cfg, self.db_cfg, self.log, state, _ET))
        self.assertTrue(any("identity:upsert_failed" in line for line in cm.output))
        client.commands.send_advert.assert_called_once_with(flood=False)


if __name__ == "__main__":
    unittest.main()
