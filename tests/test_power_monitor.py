"""BLESource MAC matching: the callback's foreign-address pre-filter.

The [power] extra (victron-ble/bleak) isn't installed in CI, so these tests
inject a fake victron_ble.scanner.Scanner base class, let BLESource.start build
its _BMVScanner subclass against it, then drive the real callback directly.

Pins:
- An advert from the configured MAC decodes and updates the latest reading.
- An advert from any other address returns early (no decode, no log) — this is
  what stops the AdvertisementKeyMissingError traceback spam from a co-located
  SmartSolar MPPT.
- A colon-less configured MAC still matches bleak's colon-separated address (the
  silent-dark bug): normalize_mac canonicalizes both sides to lower-colon.
"""

import argparse
import asyncio
import contextlib
import io
import logging
import sys
import tempfile
import types
import unittest
from pathlib import Path

import civicmesh
import power_monitor
from power_monitor import Reading
from tests.test_config import _good_sections, _write


class _FakeParsed:
    def get_soc(self):
        return 99.7

    def get_voltage(self):
        return 13.2

    def get_current(self):
        return -2.1


class _FakeDevice:
    def parse(self, raw_data):
        return _FakeParsed()


class _FakeScanner:
    """Stand-in for victron_ble.scanner.Scanner. get_device would raise
    AdvertisementKeyMissingError for an unknown address in the real lib; here it
    just returns a parsed stub, so a foreign advert reaching get_device would
    wrongly count as a success — the pre-filter must prevent that."""

    def __init__(self, device_keys=None):
        self.device_keys = device_keys or {}

    async def start(self):
        pass

    async def stop(self):
        pass

    def get_device(self, ble_device, raw_data):
        return _FakeDevice()


class _Dev:
    def __init__(self, address):
        self.address = address


def _silent_log():
    log = logging.getLogger("test_power_monitor")
    log.addHandler(logging.NullHandler())
    log.propagate = False
    return log


class CallbackAddressFilterTest(unittest.TestCase):
    def setUp(self) -> None:
        self._saved = {
            k: sys.modules.get(k) for k in ("victron_ble", "victron_ble.scanner")
        }
        mod = types.ModuleType("victron_ble")
        scanner_mod = types.ModuleType("victron_ble.scanner")
        scanner_mod.Scanner = _FakeScanner
        mod.scanner = scanner_mod
        sys.modules["victron_ble"] = mod
        sys.modules["victron_ble.scanner"] = scanner_mod

    def tearDown(self) -> None:
        for k, v in self._saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v

    def _started_source(self, mac: str) -> power_monitor.BLESource:
        src = power_monitor.BLESource(mac, "0" * 32, _silent_log())
        asyncio.run(src.start())
        return src

    def test_matching_address_decodes(self) -> None:
        src = self._started_source("D6:8E:54:50:53:E2")
        src._scanner.callback(_Dev("D6:8E:54:50:53:E2"), b"raw", None)
        self.assertIsNotNone(src._latest)
        self.assertEqual(src._latest.soc, 99.7)
        self.assertIsNotNone(src.last_success_monotonic())

    def test_foreign_address_ignored(self) -> None:
        # A co-located MPPT advertising on the same manufacturer ID.
        src = self._started_source("d6:8e:54:50:53:e2")
        src._scanner.callback(_Dev("AA:BB:CC:DD:EE:FF"), b"raw", None)
        self.assertIsNone(src._latest)
        self.assertIsNone(src.last_success_monotonic())

    def test_colonless_config_matches_bleak_colon_address(self) -> None:
        # The silent-dark bug: operator pasted colon-less, bleak reports colons.
        src = self._started_source("d68e545053e2")
        src._scanner.callback(_Dev("D6:8E:54:50:53:E2"), b"raw", None)
        self.assertIsNotNone(src._latest)


class PowerTestCommandTest(unittest.TestCase):
    """`civicmesh power-test` wiring: config resolution, exit codes, output.

    power_monitor.probe (the BLE part) is monkeypatched so no hardware/BLE is
    needed; the test pins that the handler reads [power_monitor], maps a decode
    to exit 0 with an OK line, no-data to exit 1, and an empty mac/key to exit 2.
    """

    def _config(self, tmp: Path, power: dict) -> Path:
        (tmp / "logs").mkdir(exist_ok=True)
        sections = _good_sections(tmp)
        sections["power_monitor"] = power
        return _write(tmp, sections)

    def _run(self, cfg_path: Path) -> tuple[int, str]:
        args = argparse.Namespace(config=str(cfg_path), duration=1.0)
        out = io.StringIO()
        with self.assertRaises(SystemExit) as ctx, contextlib.redirect_stdout(out):
            civicmesh._cmd_power_test(args)
        code = ctx.exception.code
        return (code if code is not None else 0), out.getvalue()

    def _patch_probe(self, result):
        async def fake_probe(cfg_pm, log, **kwargs):
            return result

        self._saved_probe = power_monitor.probe
        power_monitor.probe = fake_probe
        self.addCleanup(setattr, power_monitor, "probe", self._saved_probe)

    _GOOD_PM = {
        "mac": "d6:8e:54:50:53:e2",
        "encryption_key": "0123456789abcdef0123456789abcdef",
    }

    def test_decode_prints_reading_exit_0(self) -> None:
        self._patch_probe((True, Reading(soc=99.7, voltage_mv=13200, current_ma=-2100, power_w=-27.7)))
        with tempfile.TemporaryDirectory() as d:
            code, out = self._run(self._config(Path(d), dict(self._GOOD_PM)))
        self.assertEqual(code, 0)
        self.assertIn("SoC=99.7%", out)
        self.assertIn("W=-27.7", out)

    def test_no_data_exit_1(self) -> None:
        self._patch_probe((False, None))
        with tempfile.TemporaryDirectory() as d:
            code, _ = self._run(self._config(Path(d), dict(self._GOOD_PM)))
        self.assertEqual(code, 1)

    def test_empty_mac_exit_2(self) -> None:
        # No probe patch needed: the empty-credential guard fires first.
        with tempfile.TemporaryDirectory() as d:
            code, _ = self._run(self._config(Path(d), {"encryption_key": "ab" * 16}))
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
