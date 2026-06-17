#!/usr/bin/env python3
# ble_smoke.py — bench test the Victron BMV-712 BLE link BEFORE wiring it into
# CivicMesh. Exercises the real BLESource code path (same callback the sampler
# uses), so it also catches a victron-ble version whose Scanner.callback
# signature no longer matches our override — the silent "no data" failure mode.
#
# Needs the optional [power] extra (bleak + victron_ble): `uv pip install '.[power]'`.
# Run from the repo root:
#
#   uv run python scripts/ble_smoke.py --mac AA:BB:CC:DD:EE:FF --key 0123...ef
#   uv run python scripts/ble_smoke.py --config config.toml      # read mac/key from [power_monitor]
#
# Cross-check against the app: the printed SoC / voltage / current should match
# VictronConnect live. See docs/victron-ble-setup.md for setup + troubleshooting.

import argparse
import asyncio
import logging
import sys
import time

# Import from the repo root (this file lives in scripts/).
sys.path.insert(0, ".")
import power_monitor  # noqa: E402


def _parse_args():
    ap = argparse.ArgumentParser(description="Victron BMV BLE smoke test (BLESource).")
    ap.add_argument("--mac", help="BMV BLE MAC, e.g. AA:BB:CC:DD:EE:FF")
    ap.add_argument("--key", help="advertisement encryption key (hex) from VictronConnect")
    ap.add_argument("--config", help="read mac/key from this config.toml [power_monitor] instead")
    ap.add_argument("--duration", type=float, default=30.0, help="seconds to listen (default 30)")
    ap.add_argument("--interval", type=float, default=2.0, help="seconds between prints (default 2)")
    return ap.parse_args()


def _resolve_mac_key(args):
    if args.mac and args.key:
        return args.mac, args.key
    if args.config:
        import config
        pm = config.load_config(args.config).power_monitor
        if not pm.mac or not pm.encryption_key:
            sys.exit(f"--config {args.config}: [power_monitor] mac/encryption_key are empty")
        return pm.mac, pm.encryption_key
    sys.exit("provide --mac AND --key, or --config <path> to read them from [power_monitor]")


async def _run(mac, key, duration, interval, log):
    source = power_monitor.BLESource(mac, key, log)
    try:
        await source.start()
    except ImportError:
        sys.exit(
            "victron_ble / bleak not installed. Install the extra:\n"
            "  uv pip install '.[power]'"
        )
    print(f"scanning for {mac} for {duration:.0f}s "
          f"(scanning is passive — no pairing/connection)…\n")
    start = time.monotonic()
    decoded_any = False
    last_seen = None
    try:
        while time.monotonic() - start < duration:
            await asyncio.sleep(interval)
            r = source.read()
            last_ok = source.last_success_monotonic()
            if last_ok is not None:
                decoded_any = True
                last_seen = r
            age = "never" if last_ok is None else f"{time.monotonic() - last_ok:.1f}s ago"
            print(f"  read()={_fmt(r)}  last_decode={age}")
    finally:
        await source.stop()
    return decoded_any, last_seen


def _fmt(r):
    if r is None:
        return "None (nothing decoded yet)"
    return (f"soc={r.soc}% v={_mv(r.voltage_mv)} "
            f"i={_mv(r.current_ma)}A p={r.power_w}W")


def _mv(x):
    return "—" if x is None else f"{x / 1000:.2f}"


def main():
    args = _parse_args()
    mac, key = _resolve_mac_key(args)
    # Route logs to stdout so a callback decode/parse error (which bleak would
    # otherwise swallow) is visible during the bench test.
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    log = logging.getLogger("ble_smoke")

    decoded_any, last = asyncio.run(_run(mac, key, args.duration, args.interval, log))

    print()
    if decoded_any:
        print("RESULT: OK — decoded live data from the BMV.")
        print(f"        latest: {_fmt(last)}")
        print("        Cross-check these against VictronConnect, then set "
              "enabled=true in [power_monitor].")
    else:
        print("RESULT: NO DATA decoded. Likely causes, in order:")
        print("  1. Wrong MAC, BT blocked, or adapter down — confirm the BMV "
              "appears in `bluetoothctl scan on`, and `rfkill unblock bluetooth`.")
        print("  2. Wrong/rotated encryption key — re-copy it from VictronConnect "
              "(it is per-device; changes if the BMV was reset/swapped).")
        print("  3. victron-ble version drift — confirm Scanner.callback signature "
              "(see docs/victron-ble-setup.md § Troubleshooting).")
        print("  Cross-check with the library CLI: "
              f'victron-ble read "{mac}@{key}"')
        sys.exit(1)


if __name__ == "__main__":
    main()
