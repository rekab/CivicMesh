"""Victron BMV-712 battery monitor sampler (BLE Instant Readout).

Passively listens to the BMV's encrypted advertisements (~1/s, no GATT
connection) and writes periodic samples to the power_samples table. The BMV's
State of Charge is the true battery state, distinct from the Heltec's coarse
radio_samples.battery_mv (a coil voltage).

The PowerSource seam keeps the transport swappable. BLESource (here) is the
only implementation; a future wired VEDirectSource (VE.Direct serial) can
replace it by config alone. read() stays a non-blocking pull of the latest
normalized reading, and normalization to mV/mA/percent lives *in the source*,
so the Reading contract is uniform across transports — BLE hands us
volts/amps/percent (already normalized by victron_ble), while VE.Direct text
would hand us raw mV/mA/0.1% to normalize there.

bleak + victron_ble are imported lazily (the optional `[power]` dependency
extra), so web_server and nodes without a battery monitor never need them.

Two processes, one DB: mesh_bot runs this sampler; web_server only reads. The
in-memory holder below is therefore only an in-process convenience — the
canonical, cross-process battery state is the latest power_samples row.
"""
from __future__ import annotations

import abc
import asyncio
import functools
import time
from dataclasses import dataclass
from typing import Optional

from config import PowerMonitorConfig
from database import DBConfig, insert_power_sample

# Scanner liveness is polled this often; cheap, just reads cached state.
_HEALTH_INTERVAL_SEC = 10
# Cap on the restart backoff after a scanner error.
_BACKOFF_CAP_SEC = 60
# Backoff when the [power] extra isn't installed — a redeploy fixes it, so
# retrying fast is pointless; just keep the failure visible in the log.
_DEPS_MISSING_BACKOFF_SEC = 300


@dataclass
class Reading:
    """A normalized battery reading.

    Any field may be None: the BMV legitimately reports values as unavailable
    in normal operation (e.g. SoC before the shunt syncs after a reset).
    current_ma is signed — negative = discharge.
    """
    soc: Optional[float]          # percent, e.g. 99.7
    voltage_mv: Optional[int]
    current_ma: Optional[int]
    power_w: Optional[float]


class PowerSource(abc.ABC):
    """A swappable battery-telemetry source. Contract:

    read() -> the latest normalized Reading (or None if nothing decoded yet),
    non-blocking. Implementations consume their own transport (BLE adverts,
    serial frames) in the background and cache the latest decode.
    """

    @abc.abstractmethod
    async def start(self) -> None:
        """Begin streaming (BLE: start the passive scanner)."""

    @abc.abstractmethod
    async def stop(self) -> None:
        """Stop streaming and release the transport. Idempotent."""

    @abc.abstractmethod
    def read(self) -> Optional[Reading]:
        """Latest cached normalized reading, or None. Non-blocking."""

    @abc.abstractmethod
    def last_success_monotonic(self) -> Optional[float]:
        """time.monotonic() of the last successfully decoded reading, or None.
        Used to judge staleness and drive the no-data watchdog."""


class BLESource(PowerSource):
    """Passive Victron Instant Readout source via victron_ble + bleak.

    Owns a background BleakScanner (through victron_ble's Scanner). Each
    decodable advertisement updates the cached latest Reading and the
    last-success timestamp. Scanning is *not* a GATT connection, so this
    honors the "no persistent connection / minimize Pi-Zero-2W WiFi<->BT
    radio contention" constraint.
    """

    def __init__(self, mac: str, encryption_key: str, log):
        self._mac = mac
        self._key = encryption_key
        self._log = log
        self._scanner = None
        self._latest: Optional[Reading] = None
        self._last_success_monotonic: Optional[float] = None

    async def start(self) -> None:
        # Lazy import of the [power] extra. ImportError propagates to the
        # supervisor, which logs it and backs off.
        from victron_ble.scanner import Scanner

        source = self  # captured by the inner subclass' callback

        class _BMVScanner(Scanner):
            # Verified against victron-ble==0.9.3: override
            #   callback(self, ble_device, raw_data, advertisement)
            # get_device() does key lookup + detect_device_type + parser
            # caching; device.parse(raw_data) returns BatteryMonitorData.
            #
            # If a future victron-ble changes this signature, the override
            # silently stops firing and the source goes dark with no error —
            # the worst failure mode here. Re-verify on every version bump
            # (see the pinned `[power]` extra in pyproject.toml). Fallback if
            # the Scanner API drifts: drive a plain bleak.BleakScanner with a
            # detection_callback and use victron_ble.devices.detect_device_type
            # + parser(key).parse(raw) directly — the README-documented
            # low-level path. Keep any such change inside this class.
            def callback(self, ble_device, raw_data, advertisement):
                try:
                    device = self.get_device(ble_device, raw_data)
                    parsed = device.parse(raw_data)
                    source._on_parsed(parsed)
                except Exception:
                    # bleak swallows callback exceptions silently, so an
                    # unguarded error here is indistinguishable from "no
                    # signal". Log it so a real decode failure is visible.
                    source._log.exception("power:callback_failed")

        self._scanner = _BMVScanner(device_keys={self._mac: self._key})
        await self._scanner.start()

    async def stop(self) -> None:
        scanner = self._scanner
        self._scanner = None
        if scanner is not None:
            try:
                await scanner.stop()
            except Exception:
                self._log.debug("power:scanner_stop_failed", exc_info=True)

    def _on_parsed(self, parsed) -> None:
        # Null-safe normalization → uniform Reading. Each field is built
        # independently and NULLed when missing; an advert that decodes at all
        # counts as a success (freshness tracks "did we hear a decodable
        # advert", not "is SoC present").
        soc = parsed.get_soc()           # percent (e.g. 99.7) or None
        volts = parsed.get_voltage()     # volts or None
        amps = parsed.get_current()      # amps, signed, or None
        voltage_mv = round(volts * 1000) if volts is not None else None
        current_ma = round(amps * 1000) if amps is not None else None
        # No get_power() on BatteryMonitorData — compute, but only when both
        # operands are present.
        power_w = round(volts * amps, 1) if (volts is not None and amps is not None) else None
        self._latest = Reading(
            soc=soc, voltage_mv=voltage_mv, current_ma=current_ma, power_w=power_w
        )
        self._last_success_monotonic = time.monotonic()

    def read(self) -> Optional[Reading]:
        return self._latest

    def last_success_monotonic(self) -> Optional[float]:
        return self._last_success_monotonic


def make_source(cfg_pm: PowerMonitorConfig, log) -> PowerSource:
    """Pick a PowerSource by transport. Only 'ble' is implemented."""
    transport = (cfg_pm.transport or "ble").lower()
    if transport == "ble":
        return BLESource(cfg_pm.mac, cfg_pm.encryption_key, log)
    if transport == "vedirect":
        raise NotImplementedError(
            "power_monitor: transport='vedirect' (wired VE.Direct serial) is the "
            "reserved future seam and is not implemented yet. Use transport='ble'."
        )
    raise ValueError(
        f"power_monitor: unknown transport {cfg_pm.transport!r} (expected 'ble' or 'vedirect')"
    )


# In-process snapshot of the most recently *persisted* reading and the wall ts
# it was stored at. Lives only in the mesh_bot process that runs the sampler;
# cross-process consumers (web_server) read the power_samples table instead.
_latest_holder: dict = {"reading": None, "last_success_wall": None}


def get_latest() -> dict:
    """A copy of the in-process latest-persisted snapshot. For on-demand reads
    within mesh_bot; not a cross-process source of truth."""
    return dict(_latest_holder)


async def _scan_supervisor(source: PowerSource, cfg_pm: PowerMonitorConfig, log) -> None:
    """Own the scanner lifecycle: (re)start it, watch for silent death, and
    emit the no-data watchdog. Restarts on error/stall with capped backoff.
    Never raises out (except on cancellation)."""
    # Grace before warning that the scanner decoded nothing at all (wrong
    # key/MAC/adapter all look like silence). Restart the scanner if it goes
    # quiet for a longer window (covers a silent BlueZ/D-Bus death).
    no_data_grace = max(2 * cfg_pm.sample_interval_sec, 30)
    stall_restart = max(2 * cfg_pm.stale_after_sec, 120)
    backoff = 1
    while True:
        deps_missing = False
        try:
            await source.start()
            log.info("power:scanner_started mac=%s interval=%ds", cfg_pm.mac, cfg_pm.sample_interval_sec)
            backoff = 1
            started = time.monotonic()
            warned_no_data = False
            while True:
                await asyncio.sleep(_HEALTH_INTERVAL_SEC)
                last = source.last_success_monotonic()
                now = time.monotonic()
                if last is None:
                    if not warned_no_data and (now - started) > no_data_grace:
                        log.warning(
                            "power:no_adverts scanner up %ds but decoded nothing — "
                            "check mac/key/adapter (rfkill, bluetooth.service)",
                            round(now - started),
                        )
                        warned_no_data = True
                elif (now - last) > stall_restart:
                    log.warning(
                        "power:scanner_stalled no advert for %ds, restarting scanner",
                        round(now - last),
                    )
                    break  # fall through to stop + restart
        except ImportError:
            deps_missing = True
            log.error(
                "power:deps_missing the [power] extra (victron-ble) is not installed; "
                "backing off %ds", _DEPS_MISSING_BACKOFF_SEC,
            )
        except Exception:
            log.warning("power:scanner_error restarting", exc_info=True)
        await source.stop()
        await asyncio.sleep(_DEPS_MISSING_BACKOFF_SEC if deps_missing else backoff)
        if not deps_missing:
            backoff = min(backoff * 2, _BACKOFF_CAP_SEC)


async def _sampler(
    source: PowerSource, cfg_pm: PowerMonitorConfig, db_cfg: DBConfig, log
) -> None:
    """Every sample_interval_sec, persist the latest reading if it's fresh.

    Skip-insert-when-stale: if the stream has dropped, log a gap and skip the
    write, so the latest power_samples.ts always marks the last *good* sample
    and staleness is computable downstream as (now - ts)."""
    from clock import wall_now
    while True:
        await asyncio.sleep(cfg_pm.sample_interval_sec)
        reading = source.read()
        last = source.last_success_monotonic()
        fresh = (
            reading is not None
            and last is not None
            and (time.monotonic() - last) <= cfg_pm.stale_after_sec
        )
        if not fresh:
            log.info("power:gap no fresh reading (stale or absent), skipping sample")
            continue
        try:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(
                None,
                functools.partial(
                    insert_power_sample,
                    db_cfg,
                    soc=reading.soc,
                    voltage_mv=reading.voltage_mv,
                    current_ma=reading.current_ma,
                    power_w=reading.power_w,
                    log=log,
                ),
            )
            _latest_holder["reading"] = reading
            _latest_holder["last_success_wall"] = wall_now(db_cfg)
        except Exception:
            log.exception("power:insert_failed")


async def power_monitor_loop(cfg_pm: PowerMonitorConfig, db_cfg: DBConfig, log) -> None:
    """Long-running task (mesh_bot gather): sample the BMV into power_samples.

    Runs the scanner supervisor and the periodic sampler concurrently. Wrapped
    so a failure never escapes and kills the sibling gather tasks. Callers gate
    on cfg_pm.enabled; the guard here is belt-and-suspenders.
    """
    if not cfg_pm.enabled:
        return
    source = None
    try:
        source = make_source(cfg_pm, log)
        await asyncio.gather(
            _scan_supervisor(source, cfg_pm, log),
            _sampler(source, cfg_pm, db_cfg, log),
        )
    except Exception:
        # CancelledError is BaseException (3.8+), so it is NOT caught here and
        # propagates for clean shutdown; the finally still stops the scanner.
        log.exception("power:loop_crashed")
    finally:
        if source is not None:
            await source.stop()
