"""CIV-14: dump firmware version + self_info from the connected Heltec.

Used to settle hypothesis #4 from the research-agent analysis: the
running firmware may not match the GitHub master tree the source-level
trace was derived from, and the gates in the inbound DM decode path
may differ.

Calls send_device_query (frame `\\x16\\x03` per commands/device.py:18-20),
which fires EventType.DEVICE_INFO. Also dumps self_info for completeness
(self_info ships with create_serial via send_appstart).

USB-serial access is exclusive — mesh_bot.service must be stopped.

Usage:
    uv run python3 diagnostics/radio/device_info.py --config config.toml
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from config import load_config  # noqa: E402
from meshcore import EventType, MeshCore  # noqa: E402


def _p(*args, **kwargs) -> None:
    kwargs.setdefault("flush", True)
    print(*args, **kwargs)


def _check_preconditions(serial_port: str) -> None:
    try:
        r = subprocess.run(
            ["systemctl", "is-active", "mesh_bot"],
            capture_output=True, text=True, timeout=5,
        )
        if r.stdout.strip() == "active":
            _p("FATAL: mesh_bot.service is active. Stop it first:\n"
               "  sudo systemctl stop mesh_bot")
            sys.exit(1)
    except FileNotFoundError:
        pass
    except subprocess.TimeoutExpired:
        _p("WARNING: systemctl timed out; skipping mesh_bot check")
    if not os.path.exists(serial_port):
        _p(f"FATAL: serial port {serial_port} does not exist")
        sys.exit(1)


def _bytes_safe(v):
    """JSON-safe rendering: bytes -> hex with length tag."""
    if isinstance(v, (bytes, bytearray)):
        return f"bytes({len(v)}):{v.hex()}"
    return v


def _render(d):
    if isinstance(d, dict):
        return {k: _render(v) for k, v in d.items()}
    if isinstance(d, list):
        return [_render(v) for v in d]
    return _bytes_safe(d)


async def _async_main(args: argparse.Namespace) -> int:
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass

    cfg = load_config(args.config)
    _check_preconditions(cfg.radio.serial_port)

    mc = await MeshCore.create_serial(cfg.radio.serial_port, 115200, debug=False)
    if mc is None:
        _p("FATAL: create_serial returned None")
        return 2

    try:
        _p("=" * 72)
        _p("self_info (from create_serial / send_appstart)")
        _p("=" * 72)
        info = getattr(mc, "self_info", None) or {}
        _p(json.dumps(_render(dict(info)), indent=2, default=str))

        _p()
        _p("=" * 72)
        _p("send_device_query -> DEVICE_INFO")
        _p("=" * 72)
        r = await mc.commands.send_device_query()
        if r is None:
            _p("FATAL: send_device_query returned None")
            return 3
        _p(f"event type: {r.type}")
        payload = r.payload if r.payload is not None else {}
        _p("payload:")
        _p(json.dumps(_render(payload), indent=2, default=str))

        # Pull out the version string under whatever key it landed in
        # — payload shape differs between firmware revisions; surface
        # any field that looks like a version for the operator.
        if isinstance(payload, dict):
            version_keys = [k for k in payload.keys()
                            if "ver" in k.lower() or "build" in k.lower()
                            or "fw" in k.lower() or "model" in k.lower()]
            if version_keys:
                _p()
                _p("Likely version fields:")
                for k in version_keys:
                    _p(f"  {k}: {payload[k]!r}")
                _p()
                _p("Compare against tags at github.com/meshcore-dev/MeshCore")

        return 0
    finally:
        try:
            if hasattr(mc, "disconnect"):
                await mc.disconnect()
        except Exception:
            pass


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Dump firmware version + self_info from the connected radio.",
    )
    p.add_argument("--config", required=True, help="Path to config.toml.")
    return p.parse_args()


def main() -> int:
    args = _parse_args()
    return asyncio.run(_async_main(args))


if __name__ == "__main__":
    sys.exit(main())
