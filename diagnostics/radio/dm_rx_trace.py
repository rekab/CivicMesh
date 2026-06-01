"""CIV-14: passive RX trace for "phone DM never arrives" investigations.

Symptom this script localises: a phone sends a DM to this Pi node;
the packet shows up in RX_LOG_DATA (payload_type=2, TXT_MSG) but the
phone reports "failed" because no ACK comes back. Three stages can
fail independently:

  1. RX                — radio antenna -> firmware RX log.
  2. DECODE            — firmware decrypts + verifies the DM and
                          surfaces it as CONTACT_MSG_RECV.
  3. ACK transmission  — firmware emits an ACK packet on the air.

This tool subscribes to every event needed to localise where the
inbound DM dies. It is a pure observer — it does NOT send anything;
the operator generates inbound DMs by hand from the phone.

Per-DM verdict is two-state — `RX_ONLY` (heard but never decoded —
decode/MAC drop) or `RX+DECODED` (firmware surfaced the DM via
CONTACT_MSG_RECV, which is the observable success boundary from the
companion side). Reason: per `meshcore/packets.py:106-122` the ACK
push-notification (PacketType.ACK = 0x82) fires when the firmware
tells the companion "I just processed an ACK packet that arrived from
the air" — INBOUND ACK, i.e. a reply to one of our outbound sends.
The firmware's own OUTBOUND ACK (its automatic reply to a
successfully-decoded inbound DM) is silent at the companion API, so
we can't observe it from this script. We still print any inbound ACK
events we see in the trace and surface them in the per-DM line for
visibility, but they don't elevate the verdict.

USB-serial access is exclusive — mesh_bot.service must be stopped
before running.

Usage:
    uv run python3 diagnostics/radio/dm_rx_trace.py \\
        --config config.toml \\
        --window 120

Then DM the bot from the phone several times during the window.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from config import load_config  # noqa: E402
from meshcore import EventType, MeshCore  # noqa: E402


# ---------------------------------------------------------------------------
# Lookup tables (mirror docs/meshcore-protocol-reference.md §4)
# ---------------------------------------------------------------------------

# Payload types — protocol-reference.md:189-204. The library exposes
# payload_typename on RX_LOG_DATA already; this fallback table keeps
# the diagnostic readable if a future firmware version returns "UNK".
_PAYLOAD_TYPE_NAMES = {
    0x00: "REQ",
    0x01: "RESPONSE",
    0x02: "TXT_MSG",
    0x03: "ACK",
    0x04: "ADVERT",
    0x05: "GRP_TXT",
    0x06: "GRP_DATA",
    0x07: "ANON_REQ",
    0x08: "PATH",
    0x09: "TRACE",
    0x0A: "MULTIPART",
    0x0B: "CONTROL",
    0x0F: "RAW_CUSTOM",
}

# Route types — protocol-reference.md:180-185.
_ROUTE_TYPE_NAMES = {
    0x00: "TRANSPORT_FLOOD",
    0x01: "FLOOD",
    0x02: "DIRECT",
    0x03: "TRANSPORT_DIRECT",
}

# TXT_MSG payload_type value — used for inbound-DM correlation.
_PAYLOAD_TYPE_TXT_MSG = 0x02


# ---------------------------------------------------------------------------
# Precondition checks (mirror diagnostics/radio/t9_liveness_latency.py)
# ---------------------------------------------------------------------------

def _check_preconditions(serial_port: str) -> None:
    try:
        result = subprocess.run(
            ["systemctl", "is-active", "mesh_bot"],
            capture_output=True, text=True, timeout=5,
        )
        if result.stdout.strip() == "active":
            print(
                "FATAL: mesh_bot.service is active. Stop it first:\n"
                "  sudo systemctl stop mesh_bot",
                file=sys.stderr,
            )
            sys.exit(1)
    except FileNotFoundError:
        pass  # systemctl not available (dev machine)
    except subprocess.TimeoutExpired:
        print("WARNING: systemctl timed out; skipping mesh_bot check", file=sys.stderr)

    if not os.path.exists(serial_port):
        print(f"FATAL: serial port {serial_port} does not exist", file=sys.stderr)
        sys.exit(1)
    if not os.access(serial_port, os.R_OK | os.W_OK):
        print(f"FATAL: serial port {serial_port} is not readable/writable", file=sys.stderr)
        sys.exit(1)


# ---------------------------------------------------------------------------
# Tracker — one entry per inbound TXT_MSG observed via RX_LOG_DATA
# ---------------------------------------------------------------------------

class _DmTracker:
    """A single inbound TXT_MSG candidate and what followed it.

    Correlation is heuristic — RX_LOG_DATA and CONTACT_MSG_RECV don't
    share an obvious join key, so we use sequence + a time window:

      - When a CONTACT_MSG_RECV arrives, attach it to the most recent
        TXT_MSG RX tracker that doesn't already have a decode, IF that
        RX is within `correlation_window` seconds. This drives the
        verdict (RX_ONLY vs RX+DECODED).
      - When an ACK arrives, attach it to the most recent decoded
        tracker within the same window for informational display.
        ACK events fire for INBOUND ACKs only (see module docstring),
        so this is opportunistic visibility, not a verdict signal.
    """

    __slots__ = ("rx_ts", "rx_payload", "decoded_ts", "decoded_payload",
                 "ack_ts", "ack_payload")

    def __init__(self, rx_ts: float, rx_payload: dict):
        self.rx_ts = rx_ts
        self.rx_payload = rx_payload
        self.decoded_ts: float | None = None
        self.decoded_payload: dict | None = None
        self.ack_ts: float | None = None
        self.ack_payload: dict | None = None

    def verdict(self) -> str:
        if self.decoded_payload is None:
            return "RX_ONLY"
        return "RX+DECODED"


# ---------------------------------------------------------------------------
# Capture
# ---------------------------------------------------------------------------

async def _passive_capture(
    mesh_client,
    window_s: float,
    correlation_window_s: float,
    shutdown_event: asyncio.Event,
) -> dict:
    """Subscribe to every event we need, sleep for `window_s`, print
    each event live, return a summary dict for the end-of-run report.
    """
    trackers: list[_DmTracker] = []
    counts = {
        "rx_log_data_total": 0,
        "rx_log_data_text_msg": 0,
        "contact_msg_recv": 0,
        "ack": 0,
        "channel_msg_recv": 0,
    }
    t0_mono = time.monotonic()

    def _ts() -> str:
        # Wall-clock ISO timestamp for human readability. monotonic
        # elapsed lives in the end-of-run summary.
        return datetime.now().isoformat(timespec="milliseconds")

    def _typename(payload: dict) -> str:
        # Prefer the library's payload_typename when present; fall
        # back to our table for forward compatibility if the firmware
        # ships a new payload type.
        name = payload.get("payload_typename")
        if name and name != "UNK":
            return name
        pt = payload.get("payload_type")
        return _PAYLOAD_TYPE_NAMES.get(pt, f"UNK(0x{pt:02x})") if pt is not None else "?"

    def _route_name(payload: dict) -> str:
        name = payload.get("route_typename")
        if name and name != "UNK":
            return name
        rt = payload.get("route_type")
        return _ROUTE_TYPE_NAMES.get(rt, f"UNK(0x{rt:02x})") if rt is not None else "?"

    def _on_rx_log_data(event):
        try:
            counts["rx_log_data_total"] += 1
            now = time.monotonic()
            payload = event.payload or {}
            payload_type = payload.get("payload_type")
            raw = payload.get("pkt_payload")
            raw_hex = raw.hex() if isinstance(raw, (bytes, bytearray)) else ""

            print(
                f"[{_ts()}] RX_LOG_DATA "
                f"payload_type=0x{(payload_type or 0):02x} ({_typename(payload)}) "
                f"route_type=0x{(payload.get('route_type') or 0):02x} ({_route_name(payload)}) "
                f"path_len={payload.get('path_len')} "
                f"snr={payload.get('snr')} "
                f"rssi={payload.get('rssi')} "
                f"raw_len={len(raw_hex) // 2 if raw_hex else 0} "
                f"raw={raw_hex}"
            )

            if payload_type == _PAYLOAD_TYPE_TXT_MSG:
                counts["rx_log_data_text_msg"] += 1
                trackers.append(_DmTracker(rx_ts=now, rx_payload=payload))
        except Exception as e:
            print(f"  rx_log_data handler error: {e}", file=sys.stderr)

    def _on_contact_msg_recv(event):
        try:
            counts["contact_msg_recv"] += 1
            now = time.monotonic()
            payload = event.payload or {}
            text = payload.get("text", "")
            print(
                f"[{_ts()}] CONTACT_MSG_RECV "
                f"pubkey_prefix={payload.get('pubkey_prefix')} "
                f"txt_type={payload.get('txt_type')} "
                f"sender_ts={payload.get('sender_timestamp')} "
                f"path_len={payload.get('path_len')} "
                f"text_len={len(text)} "
                f"text={text!r}"
            )

            # Correlate: attach to most recent TXT_MSG RX without a
            # decode, within the time window. Walk back so the most
            # recent unmatched tracker wins.
            for t in reversed(trackers):
                if t.decoded_payload is None and (now - t.rx_ts) <= correlation_window_s:
                    t.decoded_ts = now
                    t.decoded_payload = payload
                    break
        except Exception as e:
            print(f"  contact_msg_recv handler error: {e}", file=sys.stderr)

    def _on_ack(event):
        try:
            counts["ack"] += 1
            now = time.monotonic()
            payload = event.payload or {}
            code = payload.get("code", "")
            print(f"[{_ts()}] ACK code={code}")

            # Same heuristic: attach to the most recent decoded
            # tracker without an ack, within the window. Documented
            # caveat: this rarely fires for outbound ACK transmission,
            # so this column typically stays empty even on success.
            for t in reversed(trackers):
                if (t.decoded_payload is not None
                        and t.ack_payload is None
                        and (now - t.decoded_ts) <= correlation_window_s):
                    t.ack_ts = now
                    t.ack_payload = payload
                    break
        except Exception as e:
            print(f"  ack handler error: {e}", file=sys.stderr)

    def _on_channel_msg(event):
        try:
            counts["channel_msg_recv"] += 1
            payload = event.payload or {}
            text = payload.get("text") or payload.get("content", "")
            print(
                f"[{_ts()}] CHANNEL_MSG_RECV "
                f"channel_idx={payload.get('channel_idx')} "
                f"sender={payload.get('sender', '')!r} "
                f"text_len={len(text)} "
                f"text={text!r}"
            )
        except Exception as e:
            print(f"  channel_msg_recv handler error: {e}", file=sys.stderr)

    subscriptions = [
        mesh_client.subscribe(EventType.RX_LOG_DATA, _on_rx_log_data),
        mesh_client.subscribe(EventType.CONTACT_MSG_RECV, _on_contact_msg_recv),
        mesh_client.subscribe(EventType.ACK, _on_ack),
        mesh_client.subscribe(EventType.CHANNEL_MSG_RECV, _on_channel_msg),
    ]

    print(f"[{_ts()}] capture START window={window_s:.0f}s "
          f"correlation_window={correlation_window_s:.1f}s")
    print(f"[{_ts()}] Send DMs from the phone now. Ctrl-C to stop early.")
    print()

    try:
        try:
            await asyncio.wait_for(shutdown_event.wait(), timeout=window_s)
        except asyncio.TimeoutError:
            pass  # normal — window elapsed
    finally:
        for sub in subscriptions:
            sub.unsubscribe()

    elapsed = time.monotonic() - t0_mono
    print()
    print(f"[{_ts()}] capture END elapsed={elapsed:.1f}s")
    return {"trackers": trackers, "counts": counts, "elapsed_s": elapsed}


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

def _print_summary(result: dict, correlation_window_s: float) -> None:
    trackers: list[_DmTracker] = result["trackers"]
    counts = result["counts"]

    print()
    print("=" * 72)
    print("PER-DM VERDICTS  (one line per inbound TXT_MSG RX_LOG_DATA)")
    print("=" * 72)
    print(f"correlation window: {correlation_window_s:.1f}s "
          "(RX -> CONTACT_MSG_RECV)")
    print("verdict is two-state: RX_ONLY | RX+DECODED. ACK column is "
          "informational — see module docstring.")
    print()

    if not trackers:
        print("  (no TXT_MSG RX events observed)")
    else:
        for i, t in enumerate(trackers, 1):
            verdict = t.verdict()
            rx_pt = t.rx_payload.get("payload_type")
            rx_rt = t.rx_payload.get("route_type")
            snr = t.rx_payload.get("snr")
            rssi = t.rx_payload.get("rssi")
            decoded_pubkey = (
                t.decoded_payload.get("pubkey_prefix")
                if t.decoded_payload else "—"
            )
            decoded_text_len = (
                len(t.decoded_payload.get("text", ""))
                if t.decoded_payload else 0
            )
            ack_code = t.ack_payload.get("code") if t.ack_payload else "—"
            print(
                f"  #{i:<3} {verdict:<22} "
                f"rx(pt=0x{(rx_pt or 0):02x} rt=0x{(rx_rt or 0):02x} "
                f"snr={snr} rssi={rssi})  "
                f"decoded(pubkey={decoded_pubkey} text_len={decoded_text_len})  "
                f"ack(code={ack_code})"
            )

    print()
    print("=" * 72)
    print("TOTALS")
    print("=" * 72)
    decoded = sum(1 for t in trackers if t.decoded_payload is not None)
    acked = sum(1 for t in trackers if t.ack_payload is not None)
    print(f"  total TXT_MSG RX               : {counts['rx_log_data_text_msg']}")
    print(f"  reached CONTACT_MSG_RECV       : {decoded}")
    print(f"  inbound ACK in correlation win : {acked}  "
          "(informational; not our outbound ACK)")
    print()
    print(f"  RX_LOG_DATA (all types)        : {counts['rx_log_data_total']}")
    print(f"  CONTACT_MSG_RECV (total)       : {counts['contact_msg_recv']}")
    print(f"  ACK events (total, inbound)    : {counts['ack']}")
    print(f"  CHANNEL_MSG_RECV (health)      : {counts['channel_msg_recv']}")

    # If there are orphan decodes / acks not matched to any tracker,
    # surface them so the heuristic's edges are visible to the operator.
    orphan_decoded = counts["contact_msg_recv"] - decoded
    if orphan_decoded > 0:
        print()
        print(f"  NOTE: {orphan_decoded} CONTACT_MSG_RECV event(s) did not "
              f"correlate to any TXT_MSG RX_LOG_DATA within "
              f"{correlation_window_s:.1f}s. The RX may have arrived before "
              "the capture window, or the correlation window may be too "
              "tight — try --correlation-window 6.0.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def _async_main(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    _check_preconditions(cfg.radio.serial_port)

    shutdown_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, shutdown_event.set)

    print(f"Connecting to {cfg.radio.serial_port} ...", file=sys.stderr)
    mesh_client = await MeshCore.create_serial(
        cfg.radio.serial_port, 115200, debug=False,
    )
    if mesh_client is None:
        print("FATAL: create_serial returned None", file=sys.stderr)
        return 2
    print("Connected.", file=sys.stderr)

    # Enable channel decryption so CHANNEL_MSG_RECV plaintext is
    # populated — mirrors mesh_bot's setup at mesh_bot.py:763-769.
    # We only set this for nicer CHANNEL_MSG_RECV output; nothing
    # else depends on it.
    try:
        mesh_client.set_decrypt_channel_logs(True)
    except Exception as e:
        print(f"  set_decrypt_channel_logs failed: {e}", file=sys.stderr)

    # CRITICAL: companion firmware queues decoded DMs in an in-RAM
    # offline_queue and only sends 1-byte MESSAGES_WAITING tickles
    # until the host calls CMD_SYNC_NEXT_MESSAGE. Without this
    # CONTACT_MSG_RECV NEVER fires here — RX_LOG_DATA alone makes
    # this script look like decode is failing. mesh_bot.py:817 has
    # the same call.
    try:
        await mesh_client.start_auto_message_fetching()
    except Exception as e:
        print(f"  start_auto_message_fetching failed: {e}", file=sys.stderr)

    try:
        result = await _passive_capture(
            mesh_client,
            window_s=args.window,
            correlation_window_s=args.correlation_window,
            shutdown_event=shutdown_event,
        )
        _print_summary(result, args.correlation_window)
        return 0
    finally:
        try:
            if hasattr(mesh_client, "disconnect"):
                await mesh_client.disconnect()
        except Exception:
            pass


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Passive RX trace — localise where inbound DMs die "
                    "between radio RX and decoded message.",
    )
    p.add_argument("--config", required=True, help="Path to config.toml.")
    p.add_argument(
        "--window",
        type=float,
        default=120.0,
        help="Capture duration in seconds (default 120).",
    )
    p.add_argument(
        "--correlation-window",
        type=float,
        default=3.0,
        help="Seconds within which a CONTACT_MSG_RECV is attributed to "
             "a preceding TXT_MSG RX_LOG_DATA (and same for ACK -> decode). "
             "Default 3.0.",
    )
    return p.parse_args()


def main() -> int:
    args = _parse_args()
    return asyncio.run(_async_main(args))


if __name__ == "__main__":
    sys.exit(main())
