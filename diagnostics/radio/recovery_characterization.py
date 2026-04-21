"""Recovery characterization harness for Heltec V3 radio.

Detects radio hangs via liveness pings, applies reset methods in rotation,
and logs everything to JSONL.  Produces a dataset answering "when the radio
hangs, how long does each reset method take to recover it?"

USB-serial access is exclusive -- mesh_bot must be stopped before running.

Usage:
    python -m diagnostics.radio.recovery_characterization \\
        --config config.toml \\
        --mode sanity \\
        --out diagnostics/radio/runs/recovery_$(date +%%Y%%m%%d_%%H%%M%%S).jsonl

    python -m diagnostics.radio.recovery_characterization \\
        --config config.toml \\
        --mode run \\
        --duration-hours 8 \\
        --out diagnostics/radio/runs/recovery_$(date +%%Y%%m%%d_%%H%%M%%S).jsonl
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from config import load_config  # noqa: E402
from meshcore import MeshCore  # noqa: E402

import serial  # noqa: E402  (pyserial, transitive from meshcore)

try:
    import usb.core  # noqa: E402
except ImportError:
    print(
        "FATAL: pyusb is required.  Install with:\n"
        "  pip install pyusb\n"
        "or:\n"
        "  pip install -e '.[diagnostics]'",
        file=sys.stderr,
    )
    sys.exit(1)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_RESET_METHODS = ["rts", "pyusb"]
_RECOVERY_POLL_INTERVAL_S = 0.5
_RECOVERY_TIMEOUT_S = 60.0
_DISCONNECT_TIMEOUT_S = 3.0
_MAX_CONSECUTIVE_LADDER_FAILURES = 30
_USB_VID_CP2102 = 0x10C4


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _emit(fh, kind: str, *, _last_healthy_mono: float | None = None, **fields) -> None:
    """Write one JSONL record.  Every record has 'kind', 'ts', and
    'elapsed_since_last_healthy_sec'."""
    if _last_healthy_mono is not None:
        elapsed: int | None = int(time.monotonic() - _last_healthy_mono)
    else:
        elapsed = None
    record = {
        "kind": kind,
        "ts": int(time.time()),
        "elapsed_since_last_healthy_sec": elapsed,
        **fields,
    }
    fh.write(json.dumps(record, default=str) + "\n")
    fh.flush()


def _snapshot_self_info(mesh_client) -> dict:
    info = getattr(mesh_client, "self_info", {}) or {}
    return dict(info)


def _resolve_sysfs_device_path(serial_port: str) -> str | None:
    """Resolve the sysfs device path for the USB-serial adapter.
    Returns None if resolution fails (dev machine, missing device)."""
    try:
        tty_basename = os.path.basename(serial_port)
        link = f"/sys/class/tty/{tty_basename}/device"
        if os.path.exists(link):
            return os.path.dirname(os.path.realpath(link))
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Preconditions
# ---------------------------------------------------------------------------

def _check_preconditions(serial_port: str) -> None:
    """Fail fast on broken preconditions."""
    try:
        result = subprocess.run(
            ["systemctl", "is-active", "mesh_bot"],
            capture_output=True, text=True, timeout=5,
        )
        if result.stdout.strip() == "active":
            print(
                "FATAL: mesh_bot.service is active.  Stop it first:\n"
                "  sudo systemctl stop mesh_bot",
                file=sys.stderr,
            )
            sys.exit(1)
    except FileNotFoundError:
        pass  # systemctl not available (dev machine)
    except subprocess.TimeoutExpired:
        print("WARNING: systemctl timed out; skipping mesh_bot check",
              file=sys.stderr)

    if not os.path.exists(serial_port):
        print(f"FATAL: serial port {serial_port} does not exist",
              file=sys.stderr)
        sys.exit(1)
    if not os.access(serial_port, os.R_OK | os.W_OK):
        print(f"FATAL: serial port {serial_port} is not readable/writable",
              file=sys.stderr)
        sys.exit(1)

    # Warn (don't fail) if pyusb can't find the CP2102
    dev = usb.core.find(idVendor=_USB_VID_CP2102)
    if dev is None:
        print(
            f"WARNING: pyusb could not find USB device VID={_USB_VID_CP2102:#06x}.  "
            "pyusb resets may fail.",
            file=sys.stderr,
        )


# ---------------------------------------------------------------------------
# Reset methods
# ---------------------------------------------------------------------------

def _reset_rts(port: str) -> None:
    """RTS serial pulse — resets ESP32 via auto-reset transistor."""
    ser = serial.Serial(port, 115200)
    ser.dtr = False
    ser.rts = True
    time.sleep(0.1)
    ser.rts = False
    ser.close()


def _reset_pyusb(vid: int) -> None:
    """USB logical reset — re-enumerates CP2102 via kernel."""
    dev = usb.core.find(idVendor=vid)
    if dev is None:
        raise RuntimeError(f"USB device VID={vid:#06x} not found")
    dev.reset()


# ---------------------------------------------------------------------------
# Diagnostic capture
# ---------------------------------------------------------------------------

async def _capture_silence_investigation(
    fh, serial_port: str, sysfs_path: str | None,
    mesh_client, consecutive_timeouts: int,
    last_healthy_mono: float | None,
) -> None:
    """Capture diagnostic data before applying any reset.
    Each step has its own try/except — one failure doesn't abort the rest."""

    data: dict = {"consecutive_timeouts": consecutive_timeouts}

    # 1. Port existence
    port_exists = os.path.exists(serial_port)
    data["port_exists"] = port_exists

    # 2. Port stat
    if port_exists:
        try:
            st = os.stat(serial_port)
            data["port_stat"] = {
                "mode": oct(st.st_mode),
                "rdev_major": os.major(st.st_rdev),
                "rdev_minor": os.minor(st.st_rdev),
            }
        except Exception as exc:
            data["port_stat"] = f"error: {exc}"

    # 3. Sysfs idVendor / idProduct
    if port_exists and sysfs_path is not None:
        for attr in ("idVendor", "idProduct"):
            path = os.path.join(sysfs_path, attr)
            try:
                with open(path) as f:
                    data[f"sysfs_{attr}"] = f.read().strip()
            except Exception as exc:
                data[f"sysfs_{attr}"] = f"error: {exc}"

    # 4. lsusb
    try:
        result = subprocess.run(
            ["lsusb"], capture_output=True, text=True, timeout=5,
        )
        data["lsusb"] = result.stdout.strip()
    except Exception as exc:
        data["lsusb"] = f"error: {exc}"

    # 5. Kernel log (last 30s)
    try:
        result = subprocess.run(
            ["journalctl", "-k", "--since", "30 seconds ago", "--no-pager"],
            capture_output=True, text=True, timeout=5,
        )
        data["kernel_log"] = result.stdout.strip()
        data["kernel_log_source"] = "journalctl"
    except Exception:
        try:
            result = subprocess.run(
                ["dmesg", "-T"],
                capture_output=True, text=True, timeout=5,
            )
            # Take last 20 lines
            lines = result.stdout.strip().splitlines()
            data["kernel_log"] = "\n".join(lines[-20:])
            data["kernel_log_source"] = "dmesg"
        except Exception as exc:
            data["kernel_log"] = f"error: {exc}"
            data["kernel_log_source"] = "none"

    # 6. Raw serial probe (non-blocking read for 2s)
    if port_exists:
        try:
            ser = serial.Serial(serial_port, 115200, timeout=0)
            raw_data = b""
            probe_deadline = time.monotonic() + 2.0
            while time.monotonic() < probe_deadline:
                chunk = ser.read(1024)
                if chunk:
                    raw_data += chunk
                else:
                    time.sleep(0.1)
            ser.close()
            data["raw_serial_hex"] = raw_data.hex() if raw_data else ""
            data["raw_serial_bytes"] = len(raw_data)
        except Exception as exc:
            data["raw_serial_hex"] = f"error: {exc}"
    else:
        data["raw_serial_hex"] = "skipped: port does not exist"

    # 7. get_channel(0) probe
    if mesh_client is not None:
        try:
            result = await asyncio.wait_for(
                mesh_client.commands.get_channel(0),
                timeout=2.0,
            )
            data["get_channel_0"] = {
                "success": True,
                "type": str(result.type) if hasattr(result, "type") else "ok",
                "payload": result.payload if hasattr(result, "payload") else None,
            }
        except asyncio.TimeoutError:
            data["get_channel_0"] = {"success": False, "error": "timeout"}
        except Exception as exc:
            data["get_channel_0"] = {"success": False, "error": str(exc)}
    else:
        data["get_channel_0"] = "skipped: no mesh_client"

    _emit(fh, "silence_investigation",
          _last_healthy_mono=last_healthy_mono, **data)


# ---------------------------------------------------------------------------
# Recovery
# ---------------------------------------------------------------------------

async def _disconnect_client(mesh_client) -> None:
    """Disconnect mesh_client with timeout.  Force-close on hang."""
    if mesh_client is None:
        return
    try:
        await asyncio.wait_for(
            mesh_client.disconnect(), timeout=_DISCONNECT_TIMEOUT_S,
        )
    except (asyncio.TimeoutError, Exception):
        # Force-close the serial transport
        try:
            if hasattr(mesh_client, "connection_manager"):
                cm = mesh_client.connection_manager
                if hasattr(cm, "connection") and hasattr(cm.connection, "transport"):
                    transport = cm.connection.transport
                    if transport is not None:
                        transport.close()
        except Exception:
            pass


async def _wait_for_recovery(
    serial_port: str, command_timeout: float,
    shutdown_event: asyncio.Event,
) -> tuple:
    """Poll until port + connect + get_stats_core all succeed.

    Returns (mesh_client, duration_ms) on success,
            (None, duration_ms) on timeout or shutdown.
    """
    t0 = time.monotonic()
    deadline = t0 + _RECOVERY_TIMEOUT_S

    while time.monotonic() < deadline and not shutdown_event.is_set():
        # Step a: tty must exist
        if not os.path.exists(serial_port):
            await asyncio.sleep(_RECOVERY_POLL_INTERVAL_S)
            continue

        # Step b: MeshCore must connect
        mc = None
        try:
            mc = await asyncio.wait_for(
                MeshCore.create_serial(serial_port, 115200, debug=False),
                timeout=5.0,
            )
            if mc is None:
                await asyncio.sleep(_RECOVERY_POLL_INTERVAL_S)
                continue
        except (asyncio.TimeoutError, Exception):
            await asyncio.sleep(_RECOVERY_POLL_INTERVAL_S)
            continue

        # Step c: get_stats_core must respond
        try:
            await asyncio.wait_for(
                mc.commands.get_stats_core(),
                timeout=command_timeout,
            )
            duration_ms = (time.monotonic() - t0) * 1000.0
            return mc, duration_ms
        except (asyncio.TimeoutError, Exception):
            try:
                await mc.disconnect()
            except Exception:
                pass
            await asyncio.sleep(_RECOVERY_POLL_INTERVAL_S)
            continue

    duration_ms = (time.monotonic() - t0) * 1000.0
    return None, duration_ms


async def _attempt_single_recovery(
    mesh_client, method: str, serial_port: str, usb_vid: int,
    command_timeout: float, fh, shutdown_event: asyncio.Event,
    attempt_number: int, last_healthy_mono: float | None,
) -> tuple:
    """Try one reset method.  Returns (new_mesh_client_or_None, success)."""
    _emit(fh, "recovery_attempt_start",
          method=method, attempt_number=attempt_number,
          _last_healthy_mono=last_healthy_mono)

    # Disconnect
    await _disconnect_client(mesh_client)

    # Apply reset
    try:
        if method == "rts":
            _reset_rts(serial_port)
        elif method == "pyusb":
            _reset_pyusb(usb_vid)
        else:
            raise ValueError(f"unknown method: {method}")
    except Exception as exc:
        _emit(fh, "recovery_attempt_end",
              method=method, attempt_number=attempt_number,
              success=False, duration_ms=0, error=str(exc),
              _last_healthy_mono=last_healthy_mono)
        return None, False

    # Wait for recovery
    new_mc, duration_ms = await _wait_for_recovery(
        serial_port, command_timeout, shutdown_event,
    )

    success = new_mc is not None
    _emit(fh, "recovery_attempt_end",
          method=method, attempt_number=attempt_number,
          success=success, duration_ms=round(duration_ms, 1),
          _last_healthy_mono=last_healthy_mono)

    return new_mc, success


async def _full_recovery(
    mesh_client, serial_port: str, usb_vid: int,
    command_timeout: float, primary_method: str,
    fh, shutdown_event: asyncio.Event,
    last_healthy_mono: float | None,
) -> tuple:
    """Try primary method, then fallback.

    Returns (new_mesh_client_or_None, success).
    """
    methods = [primary_method] + [
        m for m in _RESET_METHODS if m != primary_method
    ]

    for i, method in enumerate(methods):
        if shutdown_event.is_set():
            return None, False

        new_mc, success = await _attempt_single_recovery(
            mesh_client, method, serial_port, usb_vid,
            command_timeout, fh, shutdown_event, i, last_healthy_mono,
        )

        if success:
            _emit(fh, "recovery_succeeded",
                  method=method, total_attempts=i + 1,
                  _last_healthy_mono=last_healthy_mono)
            return new_mc, True

        # For next attempt, mesh_client is already disconnected
        mesh_client = None

    return None, False


# ---------------------------------------------------------------------------
# Polling loop
# ---------------------------------------------------------------------------

async def _poll_loop(
    mesh_client, serial_port: str, usb_vid: int, sysfs_path: str | None,
    poll_interval: float, command_timeout: float,
    hang_threshold: int, duration_s: float,
    fh, shutdown_event: asyncio.Event,
) -> tuple:
    """Poll get_stats_core, detect hangs, run recovery.

    Returns (mesh_client, reason, stats) where reason is
    "duration" | "shutdown" | "gave_up".
    """
    consecutive_timeouts = 0
    last_healthy_mono: float | None = time.monotonic()
    hang_count = 0
    consecutive_ladder_failures = 0
    total_probes = 0
    total_hangs = 0
    total_recoveries = 0

    deadline = time.monotonic() + duration_s

    while time.monotonic() < deadline and not shutdown_event.is_set():

        # --- Reconnect if we lost the client ---
        if mesh_client is None:
            if os.path.exists(serial_port):
                try:
                    mesh_client = await asyncio.wait_for(
                        MeshCore.create_serial(
                            serial_port, 115200, debug=False),
                        timeout=5.0,
                    )
                except (asyncio.TimeoutError, Exception):
                    mesh_client = None

        # --- Port existence check ---
        if not os.path.exists(serial_port):
            _emit(fh, "port_disappeared_during_polling",
                  serial_port=serial_port,
                  _last_healthy_mono=last_healthy_mono)
            # Skip investigation's raw serial probe (port gone)
            await _capture_silence_investigation(
                fh, serial_port, sysfs_path, mesh_client,
                consecutive_timeouts, last_healthy_mono,
            )
            hang_count += 1
            total_hangs += 1
            # Prefer pyusb for USB-level problems
            primary = "pyusb"
            mesh_client, recovered = await _full_recovery(
                mesh_client, serial_port, usb_vid, command_timeout,
                primary, fh, shutdown_event, last_healthy_mono,
            )
            consecutive_timeouts = 0
            if recovered:
                total_recoveries += 1
                consecutive_ladder_failures = 0
                last_healthy_mono = time.monotonic()
            else:
                consecutive_ladder_failures += 1
                _emit(fh, "full_ladder_failed",
                      consecutive_ladder_failures=consecutive_ladder_failures,
                      _last_healthy_mono=last_healthy_mono)
                if consecutive_ladder_failures >= _MAX_CONSECUTIVE_LADDER_FAILURES:
                    _emit(fh, "giving_up",
                          total_hangs=total_hangs,
                          total_recoveries=total_recoveries,
                          _last_healthy_mono=last_healthy_mono)
                    stats = _make_stats(total_probes, total_hangs,
                                        total_recoveries)
                    return mesh_client, "gave_up", stats
            continue

        # --- Probe ---
        total_probes += 1
        t0 = time.monotonic()

        if mesh_client is None:
            # Can't probe without a client — count as timeout
            consecutive_timeouts += 1
            _emit(fh, "probe_timeout",
                  latency_ms=0, result_type="no_client",
                  consecutive_timeouts=consecutive_timeouts,
                  _last_healthy_mono=last_healthy_mono)
        else:
            try:
                result = await asyncio.wait_for(
                    mesh_client.commands.get_stats_core(),
                    timeout=command_timeout,
                )
                latency_ms = (time.monotonic() - t0) * 1000.0
                payload_keys: list[str] = []
                if hasattr(result, "payload") and isinstance(
                    result.payload, dict
                ):
                    payload_keys = list(result.payload.keys())
                _emit(fh, "probe",
                      latency_ms=round(latency_ms, 2),
                      result_type="ok",
                      payload_keys=payload_keys,
                      _last_healthy_mono=last_healthy_mono)
                consecutive_timeouts = 0
                last_healthy_mono = time.monotonic()

            except asyncio.TimeoutError:
                latency_ms = (time.monotonic() - t0) * 1000.0
                consecutive_timeouts += 1
                _emit(fh, "probe_timeout",
                      latency_ms=round(latency_ms, 2),
                      result_type="timeout",
                      consecutive_timeouts=consecutive_timeouts,
                      _last_healthy_mono=last_healthy_mono)

            except Exception as exc:
                latency_ms = (time.monotonic() - t0) * 1000.0
                _emit(fh, "probe",
                      latency_ms=round(latency_ms, 2),
                      result_type="error",
                      error=f"{type(exc).__name__}: {exc}",
                      _last_healthy_mono=last_healthy_mono)
                # Errors are not hang signals
                consecutive_timeouts = 0

        # --- Hang detection ---
        if consecutive_timeouts >= hang_threshold:
            total_hangs += 1
            _emit(fh, "silence_detected",
                  consecutive_timeouts=consecutive_timeouts,
                  _last_healthy_mono=last_healthy_mono)

            await _capture_silence_investigation(
                fh, serial_port, sysfs_path, mesh_client,
                consecutive_timeouts, last_healthy_mono,
            )

            hang_count += 1
            primary = _RESET_METHODS[(hang_count - 1) % len(_RESET_METHODS)]
            mesh_client, recovered = await _full_recovery(
                mesh_client, serial_port, usb_vid, command_timeout,
                primary, fh, shutdown_event, last_healthy_mono,
            )

            consecutive_timeouts = 0
            if recovered:
                total_recoveries += 1
                consecutive_ladder_failures = 0
                last_healthy_mono = time.monotonic()
            else:
                consecutive_ladder_failures += 1
                _emit(fh, "full_ladder_failed",
                      consecutive_ladder_failures=consecutive_ladder_failures,
                      _last_healthy_mono=last_healthy_mono)
                if consecutive_ladder_failures >= _MAX_CONSECUTIVE_LADDER_FAILURES:
                    _emit(fh, "giving_up",
                          total_hangs=total_hangs,
                          total_recoveries=total_recoveries,
                          _last_healthy_mono=last_healthy_mono)
                    stats = _make_stats(total_probes, total_hangs,
                                        total_recoveries)
                    return mesh_client, "gave_up", stats

            continue  # skip sleep — recovery already consumed time

        # --- Sleep remainder of interval ---
        elapsed = time.monotonic() - t0
        sleep_s = max(0, poll_interval - elapsed)
        if sleep_s > 0:
            try:
                await asyncio.wait_for(
                    shutdown_event.wait(), timeout=sleep_s,
                )
                break  # shutdown requested
            except asyncio.TimeoutError:
                pass  # normal — sleep expired

    reason = "shutdown" if shutdown_event.is_set() else "duration"
    stats = _make_stats(total_probes, total_hangs, total_recoveries)
    return mesh_client, reason, stats


def _make_stats(total_probes: int, total_hangs: int,
                total_recoveries: int) -> dict:
    return {
        "total_probes": total_probes,
        "total_hangs": total_hangs,
        "total_recoveries": total_recoveries,
    }


# ---------------------------------------------------------------------------
# Sanity mode
# ---------------------------------------------------------------------------

async def _sanity_mode(
    mesh_client, serial_port: str, usb_vid: int, sysfs_path: str | None,
    poll_interval: float, command_timeout: float,
    fh, shutdown_event: asyncio.Event,
) -> tuple:
    """~5 minute sanity check: poll, test each reset, poll again.

    Returns (mesh_client, exit_code).
    """
    _emit(fh, "mode", mode="sanity", _last_healthy_mono=None)
    print("Sanity mode: polling 2 minutes ...", file=sys.stderr)

    # Phase 1: 2 minutes of normal polling (no recovery — just confirm
    # the radio is healthy).  Use a high hang_threshold so we never enter
    # recovery in this phase.
    mesh_client, reason, _ = await _poll_loop(
        mesh_client, serial_port, usb_vid, sysfs_path,
        poll_interval, command_timeout,
        hang_threshold=9999, duration_s=120,
        fh=fh, shutdown_event=shutdown_event,
    )
    if shutdown_event.is_set():
        return mesh_client, 0

    # Phase 2: test each reset method on a healthy radio
    last_healthy_mono = time.monotonic()
    for method in _RESET_METHODS:
        if shutdown_event.is_set():
            return mesh_client, 0

        print(f"Sanity test: {method} reset ...", file=sys.stderr)
        _emit(fh, "sanity_reset_test",
              method=method, phase="start",
              _last_healthy_mono=last_healthy_mono)

        await _disconnect_client(mesh_client)
        mesh_client = None

        # Apply reset
        try:
            if method == "rts":
                _reset_rts(serial_port)
            elif method == "pyusb":
                _reset_pyusb(usb_vid)
        except Exception as exc:
            _emit(fh, "sanity_reset_test",
                  method=method, phase="failed",
                  error=str(exc),
                  _last_healthy_mono=last_healthy_mono)
            print(f"FAIL: {method} reset raised: {exc}", file=sys.stderr)
            return mesh_client, 1

        # Wait for recovery
        new_mc, duration_ms = await _wait_for_recovery(
            serial_port, command_timeout, shutdown_event,
        )

        if new_mc is not None:
            _emit(fh, "sanity_reset_test",
                  method=method, phase="succeeded",
                  duration_ms=round(duration_ms, 1),
                  _last_healthy_mono=last_healthy_mono)
            mesh_client = new_mc
            last_healthy_mono = time.monotonic()
            print(
                f"  {method}: recovered in {duration_ms:.0f} ms",
                file=sys.stderr,
            )
        else:
            _emit(fh, "sanity_reset_test",
                  method=method, phase="failed",
                  duration_ms=round(duration_ms, 1),
                  _last_healthy_mono=last_healthy_mono)
            print(
                f"FAIL: {method} reset did not recover within "
                f"{_RECOVERY_TIMEOUT_S:.0f}s",
                file=sys.stderr,
            )
            return mesh_client, 1

    if shutdown_event.is_set():
        return mesh_client, 0

    # Phase 3: 1 minute of normal polling
    print("Sanity mode: polling 1 minute post-reset ...", file=sys.stderr)
    mesh_client, reason, _ = await _poll_loop(
        mesh_client, serial_port, usb_vid, sysfs_path,
        poll_interval, command_timeout,
        hang_threshold=9999, duration_s=60,
        fh=fh, shutdown_event=shutdown_event,
    )

    print("Sanity mode complete.", file=sys.stderr)
    return mesh_client, 0


# ---------------------------------------------------------------------------
# Run mode
# ---------------------------------------------------------------------------

async def _run_mode(
    mesh_client, serial_port: str, usb_vid: int, sysfs_path: str | None,
    poll_interval: float, command_timeout: float,
    hang_threshold: int, duration_hours: float,
    fh, shutdown_event: asyncio.Event,
) -> tuple:
    """Long-running characterization.

    Returns (mesh_client, exit_code).
    """
    duration_s = duration_hours * 3600.0
    _emit(fh, "mode",
          mode="run", duration_hours=duration_hours,
          _last_healthy_mono=None)
    print(
        f"Run mode: {duration_hours}h ({duration_s:.0f}s)",
        file=sys.stderr,
    )

    mesh_client, reason, stats = await _poll_loop(
        mesh_client, serial_port, usb_vid, sysfs_path,
        poll_interval, command_timeout, hang_threshold, duration_s,
        fh=fh, shutdown_event=shutdown_event,
    )

    print(
        f"Run mode ended: {reason}  "
        f"probes={stats['total_probes']}  "
        f"hangs={stats['total_hangs']}  "
        f"recoveries={stats['total_recoveries']}",
        file=sys.stderr,
    )
    return mesh_client, 1 if reason == "gave_up" else 0


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def _async_main(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    serial_port = cfg.radio.serial_port

    _check_preconditions(serial_port)

    # Stash paths at startup — never re-resolve during recovery
    sysfs_path = _resolve_sysfs_device_path(serial_port)
    usb_vid = _USB_VID_CP2102

    shutdown_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, shutdown_event.set)

    fh = open(args.out, "a")
    mesh_client = None

    try:
        print(f"Connecting to {serial_port} ...", file=sys.stderr)
        mesh_client = await MeshCore.create_serial(
            serial_port, 115200, debug=False,
        )
        if mesh_client is None:
            print("FATAL: MeshCore.create_serial returned None — "
                  "is this a serial companion?", file=sys.stderr)
            return 1
        print("Connected.", file=sys.stderr)

        self_info = _snapshot_self_info(mesh_client)
        _emit(fh, "startup",
              self_info=self_info,
              serial_port=serial_port,
              sysfs_path=sysfs_path,
              mode=args.mode,
              _last_healthy_mono=None)

        if args.mode == "sanity":
            mesh_client, exit_code = await _sanity_mode(
                mesh_client, serial_port, usb_vid, sysfs_path,
                args.poll_interval, args.command_timeout,
                fh, shutdown_event,
            )
        else:
            mesh_client, exit_code = await _run_mode(
                mesh_client, serial_port, usb_vid, sysfs_path,
                args.poll_interval, args.command_timeout,
                args.hang_threshold, args.duration_hours,
                fh, shutdown_event,
            )

        return exit_code

    finally:
        reason = "shutdown"
        if shutdown_event.is_set():
            reason = "signal"

        _emit(fh, "shutdown", reason=reason, _last_healthy_mono=None)

        if mesh_client is not None:
            await _disconnect_client(mesh_client)
            print("Disconnected.", file=sys.stderr)

        fh.close()


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Recovery characterization harness for Heltec V3 radio.",
    )
    p.add_argument(
        "--config", type=str,
        default=str(_REPO_ROOT / "config.toml"),
        help="Path to config.toml (default: %(default)s)",
    )
    p.add_argument(
        "--mode", type=str, choices=["sanity", "run"], required=True,
        help="'sanity' (~5 min smoke test) or 'run' (long characterization)",
    )
    p.add_argument(
        "--duration-hours", type=float, default=8.0,
        help="Duration in hours for run mode (default: %(default)s)",
    )
    p.add_argument(
        "--poll-interval", type=float, default=2.0,
        help="Seconds between probes (default: %(default)s)",
    )
    p.add_argument(
        "--command-timeout", type=float, default=3.0,
        help="Timeout per get_stats_core() call in seconds (default: %(default)s)",
    )
    p.add_argument(
        "--hang-threshold", type=int, default=10,
        help="Consecutive timeouts to declare a hang (default: %(default)s)",
    )
    p.add_argument(
        "--out", type=str,
        default=str(_HERE / "runs" / "recovery.jsonl"),
        help="Output JSONL file path (default: %(default)s)",
    )
    return p.parse_args()


def main() -> int:
    args = _parse_args()
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    return asyncio.run(_async_main(args))


if __name__ == "__main__":
    sys.exit(main())
