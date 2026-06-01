"""CIV-14 resolution: verify the offline-queue hypothesis by draining it.

What this proves. MeshCore companion firmware stores incoming DMs in
an in-RAM `offline_queue` (size 16) and only sends 1-byte
MESSAGES_WAITING (0x83) tickles to the host until the host pulls each
message via CMD_SYNC_NEXT_MESSAGE (0x0A). The
RESP_CODE_CONTACT_MSG_RECV (0x07) frame the meshcore_py reader watches
for *never reaches the wire* until the host drains. mesh_bot.py:817
already calls `start_auto_message_fetching()` so production is fine,
but every diagnostic in this directory subscribes to RX_LOG_DATA and
times out without ever asking the firmware to drain — which is why
all our earlier tests showed RX_LOG_DATA with no CONTACT_MSG_RECV
despite successful firmware-side decryption (proven separately via
manual_decrypt.py).

This tool calls `mc.start_auto_message_fetching()` (meshcore.py:466-472),
subscribes to CONTACT_MSG_RECV, and prints everything that arrives.
On first invocation since the firmware last drained, you should see
the backlog of DMs that have been silently piling up in offline_queue
since the contact was added — every "Test" we sent today, modulo the
16-slot FIFO eviction cap.

USB-serial access is exclusive — mesh_bot.service must be stopped.

Usage:
    uv run python3 diagnostics/radio/drain_queue.py \\
        --config config.toml --window 15
"""

from __future__ import annotations

import argparse
import asyncio
import os
import signal
import subprocess
import sys
from datetime import datetime
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


def _ts() -> str:
    return datetime.now().isoformat(timespec="milliseconds")


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


async def _async_main(args: argparse.Namespace) -> int:
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass

    cfg = load_config(args.config)
    _check_preconditions(cfg.radio.serial_port)

    shutdown_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, shutdown_event.set)

    _p(f"[{_ts()}] connecting to {cfg.radio.serial_port}")
    mc = await MeshCore.create_serial(cfg.radio.serial_port, 115200, debug=False)
    if mc is None:
        _p("FATAL: create_serial returned None")
        return 2

    drained: list[dict] = []
    waiting_tickles = 0
    channel_msgs: list[dict] = []

    def _on_contact_msg(event):
        try:
            p = event.payload or {}
            drained.append(dict(p))
            _p(f"[{_ts()}] *** CONTACT_MSG_RECV "
               f"pubkey_prefix={p.get('pubkey_prefix')} "
               f"txt_type={p.get('txt_type')} "
               f"sender_ts={p.get('sender_timestamp')} "
               f"text={p.get('text', '')!r}")
        except Exception as e:
            _p(f"  contact_msg handler error: {e}")

    def _on_chan_msg(event):
        try:
            p = event.payload or {}
            channel_msgs.append(dict(p))
            text = p.get("text") or p.get("content", "")
            _p(f"[{_ts()}] CHANNEL_MSG_RECV "
               f"channel_idx={p.get('channel_idx')} "
               f"sender={p.get('sender', '')!r} "
               f"text={text!r}")
        except Exception as e:
            _p(f"  channel_msg handler error: {e}")

    def _on_msgs_waiting(_event):
        nonlocal waiting_tickles
        waiting_tickles += 1
        _p(f"[{_ts()}] MESSAGES_WAITING tickle "
           f"(firmware says: more pending, total seen={waiting_tickles})")

    subs = [
        mc.subscribe(EventType.CONTACT_MSG_RECV, _on_contact_msg),
        mc.subscribe(EventType.CHANNEL_MSG_RECV, _on_chan_msg),
        mc.subscribe(EventType.MESSAGES_WAITING, _on_msgs_waiting),
    ]
    try:
        # Channel decryption — same as mesh_bot's setup at mesh_bot.py:766
        # so any backlog channel messages have plaintext.
        try:
            mc.set_decrypt_channel_logs(True)
        except Exception:
            pass

        _p(f"[{_ts()}] start_auto_message_fetching() — triggers initial drain")
        await mc.start_auto_message_fetching()
        _p(f"[{_ts()}] drain loop running; waiting {args.window:.0f}s for "
           "queued + fresh messages")
        try:
            await asyncio.wait_for(shutdown_event.wait(), timeout=args.window)
        except asyncio.TimeoutError:
            pass
        _p(f"[{_ts()}] window closed")
    finally:
        for s in subs:
            s.unsubscribe()
        try:
            if hasattr(mc, "disconnect"):
                await mc.disconnect()
        except Exception:
            pass

    _p()
    _p("=" * 72)
    _p("SUMMARY")
    _p("=" * 72)
    _p(f"CONTACT_MSG_RECV drained         : {len(drained)}")
    _p(f"CHANNEL_MSG_RECV during window   : {len(channel_msgs)}")
    _p(f"MESSAGES_WAITING tickles observed: {waiting_tickles}")

    if not drained:
        _p()
        _p("No DMs surfaced. Either:")
        _p("  - offline_queue was already empty (firmware drained on a")
        _p("    prior auto-fetch run, or reboot since the last DM cleared")
        _p("    the RAM queue), or")
        _p("  - your phone is not currently sending DMs to this hub.")
    else:
        _p()
        _p("Distinct DM texts seen:")
        seen: set[str] = set()
        for d in drained:
            t = d.get("text", "")
            if t not in seen:
                seen.add(t)
                _p(f"  - {t!r} (pubkey_prefix={d.get('pubkey_prefix')})")
    return 0


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Drain the MeshCore companion firmware's offline_queue "
                    "by starting auto-message-fetching and capturing every "
                    "CONTACT_MSG_RECV that surfaces (CIV-14 resolution).",
    )
    p.add_argument("--config", required=True, help="Path to config.toml.")
    p.add_argument(
        "--window",
        type=float,
        default=15.0,
        help="Seconds to listen after starting the auto-fetch loop "
             "(default 15). Drain happens within the first second or two "
             "of MESSAGES_WAITING ticking the loop; the rest is for live "
             "DMs you may want to send during the window.",
    )
    return p.parse_args()


def main() -> int:
    args = _parse_args()
    return asyncio.run(_async_main(args))


if __name__ == "__main__":
    sys.exit(main())
