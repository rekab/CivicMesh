"""CIV-14: verify minimum contact fields needed for bidirectional DM.

Question we're answering: is the 32-byte pubkey alone sufficient to
add a MeshCore companion contact that can both SEND DMs (hub -> phone)
and RECEIVE DMs (phone -> hub)? The answer determines whether the
captive-portal contact-add form needs to collect just a pubkey or
also a display name + other fields.

What this script does:

  1. Removes any existing contact at the provided pubkey (clean slate).
  2. Adds the contact with:
       adv_name        = <user-supplied via --name, default empty string>
       type            = 1 (companion)
       flags           = 0  (known-good for RX per contact_flags_probe
                            output in dated logs; CIV-14 history)
       out_path        = "" / out_path_len = -1   (use flood routing)
       last_advert     = 0
       adv_lat/lon     = 0.0
     Reads the stored record back via get_contacts so the operator can
     see what the firmware actually persisted.
  3. Calls `start_auto_message_fetching()` (the offline_queue drain
     protocol — see AGENTS.md "MeshCore inbound DM drain").
  4. TX LEG: sends a probe DM from hub to the contact and waits for the
     firmware ACK using the same expected_ack/suggested_timeout pattern
     as dm_diagnose.py:_check_send_and_ack.
  5. RX LEG: prompts the operator to send a DM from the phone, then
     captures CONTACT_MSG_RECV inside a 30s window.
  6. Prints PASS/FAIL/INCONCLUSIVE per leg + a UX prompt asking the
     operator to record what sender-name the phone displayed.

USB-serial access is exclusive — mesh_bot.service must be stopped.

Usage:
    uv run python3 diagnostics/radio/minimum_contact_probe.py \\
        --config config.toml \\
        --pubkey 1294f888153c0440ed3eedf7752415e7fbd012c4234a89c2bfc04bd215105b89
    # named-baseline comparison run:
    uv run python3 diagnostics/radio/minimum_contact_probe.py \\
        --config config.toml --pubkey <hex> --name Fremonster
    # advert-backfill watch: add with empty name, see if firmware
    # populates adv_name from an incoming advert within N seconds.
    uv run python3 diagnostics/radio/minimum_contact_probe.py \\
        --config config.toml --pubkey <hex> --watch-adverts 180
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
from datetime import datetime
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from config import load_config  # noqa: E402
from meshcore import EventType, MeshCore  # noqa: E402


PASS = "[PASS]"
FAIL = "[FAIL]"
INCONCLUSIVE = "[INCONCLUSIVE]"


def _ts() -> str:
    return datetime.now().isoformat(timespec="milliseconds")


def _p(*args, **kwargs) -> None:
    kwargs.setdefault("flush", True)
    print(*args, **kwargs)


def _section(title: str) -> None:
    _p()
    _p("=" * 72)
    _p(title)
    _p("=" * 72)


def _verdict(label: str, detail: str = "") -> None:
    _p(f"{label}  {detail}" if detail else label)


def _check_preconditions(serial_port: str) -> None:
    try:
        r = subprocess.run(
            ["systemctl", "is-active", "mesh_bot"],
            capture_output=True, text=True, timeout=5,
        )
        if r.stdout.strip() == "active":
            _p("FATAL: mesh_bot.service is active. Stop it first:\n"
               "  sudo systemctl stop mesh_bot", file=sys.stderr)
            sys.exit(1)
    except FileNotFoundError:
        pass
    except subprocess.TimeoutExpired:
        _p("WARNING: systemctl timed out; skipping mesh_bot check",
           file=sys.stderr)
    if not os.path.exists(serial_port):
        _p(f"FATAL: serial port {serial_port} does not exist",
           file=sys.stderr)
        sys.exit(1)


def _find_contact_by_pubkey(mesh_client, pubkey_hex: str) -> dict | None:
    pubkey_hex = pubkey_hex.lower()
    contacts = getattr(mesh_client, "contacts", {}) or {}
    for c in contacts.values():
        if c.get("public_key", "").lower() == pubkey_hex:
            return c
    return None


async def _remove_existing(mesh_client, pubkey_hex: str) -> None:
    await mesh_client.commands.get_contacts()
    existing = _find_contact_by_pubkey(mesh_client, pubkey_hex)
    if existing is None:
        _p(f"[{_ts()}] no existing contact at pubkey={pubkey_hex[:12]}…")
        return
    _p(f"[{_ts()}] removing existing contact: adv_name={existing.get('adv_name')!r} "
       f"flags={existing.get('flags')} out_path_len={existing.get('out_path_len')}")
    result = await mesh_client.commands.remove_contact(pubkey_hex)
    if result is None or result.type == EventType.ERROR:
        _p(f"WARNING: remove_contact returned {result!r}; continuing")


def _build_minimal_contact(pubkey_hex: str, adv_name: str) -> dict:
    # All fields listed because meshcore.commands.contact.update_contact
    # reads each key directly from the dict; omitting any raises KeyError.
    # adv_name is the *only* field that varies by --name; everything else
    # is set to the known-good defaults that contact_flags_probe ran with
    # when it decoded 7 inbound DMs (verify-fix-20260531-194305.lo).
    return {
        "public_key": pubkey_hex.lower(),
        "adv_name": adv_name,
        "type": 1,
        "flags": 0,
        "out_path": "",
        "out_path_len": -1,
        "out_path_hash_mode": 0,
        "last_advert": 0,
        "adv_lat": 0.0,
        "adv_lon": 0.0,
    }


async def _add_minimal(mesh_client, pubkey_hex: str, adv_name: str) -> dict | None:
    contact = _build_minimal_contact(pubkey_hex, adv_name)
    _p()
    _p("Adding contact with:")
    _p(json.dumps(contact, indent=2))
    result = await mesh_client.commands.add_contact(contact)
    if result is None or result.type == EventType.ERROR:
        _p(f"FATAL: add_contact returned {result!r}", file=sys.stderr)
        return None

    await mesh_client.commands.get_contacts()
    stored = _find_contact_by_pubkey(mesh_client, pubkey_hex)
    _p()
    _p("Stored contact record:")
    _p(json.dumps(stored, indent=2, default=str))
    return stored


async def _tx_leg(mesh_client, contact: dict) -> tuple[str, str]:
    _section("TX LEG — send DM from hub to contact, wait for ACK")
    body = f"civicmesh-min-probe TX {int(time.time())}"
    _p(f"sending message    : {body!r}")
    _p(f"to contact         : adv_name={contact.get('adv_name')!r} "
       f"pubkey_prefix={contact.get('public_key', '')[:12]}…")

    rx_packets: list[dict] = []

    def _on_rx_log(event):
        try:
            p = event.payload or {}
            rx_packets.append(p)
            _p(f"  rx_log payload_type={p.get('payload_type')} "
               f"route_type={p.get('route_type')} "
               f"path_len={p.get('path_len')} "
               f"snr={p.get('snr')} rssi={p.get('rssi')}")
        except Exception as e:
            _p(f"  rx_log handler error: {e}", file=sys.stderr)

    rx_sub = mesh_client.subscribe(EventType.RX_LOG_DATA, _on_rx_log)
    try:
        result = await mesh_client.commands.send_msg(contact, body)
        if result is None or result.type == EventType.ERROR:
            payload = getattr(result, "payload", None)
            _p(f"send_msg result    : ERROR payload={payload!r}")
            return FAIL, f"send_msg refused: {payload!r}"

        expected_ack = result.payload["expected_ack"].hex()
        suggested_ms = result.payload.get("suggested_timeout", 4000)
        wait_s = max(15.0, suggested_ms / 1000.0 * 1.2)
        _p(f"expected_ack       : {expected_ack}")
        _p(f"suggested_timeout  : {suggested_ms} ms")
        _p(f"waiting up to      : {wait_s:.1f}s for ACK")

        t0 = time.monotonic()
        ack = await mesh_client.dispatcher.wait_for_event(
            EventType.ACK,
            attribute_filters={"code": expected_ack},
            timeout=wait_s,
        )
        elapsed = time.monotonic() - t0
        _p()
        _p(f"rx_log packets during wait: {len(rx_packets)}")
        if ack is not None:
            return PASS, f"ACK received in {elapsed:.2f}s"
        if rx_packets:
            return INCONCLUSIVE, (
                f"no ACK in {wait_s:.0f}s but {len(rx_packets)} RX packets "
                "captured — phone may have received, see RX leg"
            )
        return FAIL, (
            f"no ACK and no RX activity in {wait_s:.0f}s — "
            "either RF is dead, no path established, or send refused silently"
        )
    finally:
        rx_sub.unsubscribe()


async def _rx_leg(
    mesh_client,
    contact: dict,
    window_s: float,
) -> tuple[str, str]:
    _section("RX LEG — receive DM from phone within capture window")
    pubkey_prefix_hex = contact.get("public_key", "")[:12].lower()
    decoded: list[dict] = []

    def _on_contact_msg(event):
        try:
            p = event.payload or {}
            decoded.append(dict(p))
            _p(f"[{_ts()}] *** CONTACT_MSG_RECV "
               f"pubkey_prefix={p.get('pubkey_prefix')} "
               f"txt_type={p.get('txt_type')} "
               f"sender_ts={p.get('sender_timestamp')} "
               f"text={p.get('text', '')!r}")
        except Exception as e:
            _p(f"  contact_msg handler error: {e}", file=sys.stderr)

    sub = mesh_client.subscribe(EventType.CONTACT_MSG_RECV, _on_contact_msg)
    try:
        _p(f"[{_ts()}] expecting CONTACT_MSG_RECV with "
           f"pubkey_prefix={pubkey_prefix_hex}")
        _p()
        _p("=" * 72)
        _p("ACTION REQUIRED:")
        _p("  - From your phone, send ONE DM to this hub.")
        _p(f"  - Capture window is {window_s:.0f}s after you press Enter.")
        _p("=" * 72)
        input(">>> Press Enter when ready to start capture: ")
        _p(f"[{_ts()}] capture START window={window_s:.0f}s")

        try:
            await asyncio.sleep(window_s)
        except asyncio.CancelledError:
            pass
        _p(f"[{_ts()}] capture END")
    finally:
        sub.unsubscribe()

    matching = [d for d in decoded if (d.get("pubkey_prefix") or "").lower()
                == pubkey_prefix_hex]
    _p()
    _p(f"CONTACT_MSG_RECV total: {len(decoded)}")
    _p(f"  matching this pubkey: {len(matching)}")
    if matching:
        return PASS, f"{len(matching)} DM(s) decoded from this pubkey"
    if decoded:
        return INCONCLUSIVE, (
            f"{len(decoded)} CONTACT_MSG_RECV fired but none matched "
            "this pubkey prefix — your phone may have sent from a "
            "different identity"
        )
    return FAIL, "no CONTACT_MSG_RECV fired in window"


async def _advert_watch_leg(
    mesh_client,
    pubkey_hex: str,
    window_s: float,
) -> tuple[str, str]:
    _section(f"ADVERT-BACKFILL WATCH — does firmware populate adv_name from "
             f"an incoming advert within {window_s:.0f}s?")
    pubkey_lower = pubkey_hex.lower()
    pubkey_prefix_hex = pubkey_lower[:12]

    advert_events: list[dict] = []
    new_contact_events: list[dict] = []
    next_contact_events: list[dict] = []

    def _on_advert(event):
        try:
            p = event.payload or {}
            advert_events.append(dict(p))
            _p(f"[{_ts()}] ADVERTISEMENT pubkey={p.get('public_key', '')[:12]}…")
        except Exception as e:
            _p(f"  advert handler error: {e}", file=sys.stderr)

    def _on_new_contact(event):
        try:
            p = event.payload or {}
            new_contact_events.append(dict(p))
            _p(f"[{_ts()}] *** NEW_CONTACT "
               f"pubkey={p.get('public_key', '')[:12]}… "
               f"adv_name={p.get('adv_name')!r}")
        except Exception as e:
            _p(f"  new_contact handler error: {e}", file=sys.stderr)

    def _on_next_contact(event):
        try:
            p = event.payload or {}
            next_contact_events.append(dict(p))
        except Exception as e:
            _p(f"  next_contact handler error: {e}", file=sys.stderr)

    subs = [
        mesh_client.subscribe(EventType.ADVERTISEMENT, _on_advert),
        mesh_client.subscribe(EventType.NEW_CONTACT, _on_new_contact),
        mesh_client.subscribe(EventType.NEXT_CONTACT, _on_next_contact),
    ]
    try:
        _p()
        _p("=" * 72)
        _p("ACTION:")
        _p("  - Your phone's companion app advertises periodically. You can")
        _p("    also trigger one manually via 'share identity' / 'send")
        _p("    advert' in the MeshCore app.")
        _p(f"  - Watching for {window_s:.0f}s starting now.")
        _p("=" * 72)
        try:
            await asyncio.sleep(window_s)
        except asyncio.CancelledError:
            pass
    finally:
        for s in subs:
            s.unsubscribe()

    matching_adverts = [e for e in advert_events
                        if (e.get("public_key") or "").lower() == pubkey_lower]
    matching_new = [e for e in new_contact_events
                    if (e.get("public_key") or "").lower() == pubkey_lower]

    _p()
    _p(f"ADVERTISEMENT events (all)       : {len(advert_events)}")
    _p(f"  matching this pubkey           : {len(matching_adverts)}")
    _p(f"NEW_CONTACT events               : {len(new_contact_events)}")
    _p(f"  matching this pubkey           : {len(matching_new)}")
    if matching_new:
        names = [e.get("adv_name") for e in matching_new]
        _p(f"  names carried by NEW_CONTACT   : {names!r}")

    # Re-read the contact via get_contacts. This is the load-bearing
    # question: did the firmware UPDATE the stored adv_name?
    await mesh_client.commands.get_contacts()
    stored = _find_contact_by_pubkey(mesh_client, pubkey_hex)
    _p()
    _p("Stored contact AFTER watch window:")
    _p(json.dumps(stored, indent=2, default=str))

    if stored is None:
        return FAIL, "contact disappeared from the table"
    final_name = stored.get("adv_name", "")
    if final_name:
        return PASS, (
            f"adv_name backfilled to {final_name!r} "
            f"(saw {len(matching_adverts)} ADVERTISEMENT + "
            f"{len(matching_new)} NEW_CONTACT for this pubkey)"
        )
    if matching_adverts or matching_new:
        return INCONCLUSIVE, (
            f"events fired but adv_name stayed empty — firmware doesn't "
            "backfill manually-added contacts"
        )
    return INCONCLUSIVE, (
        "no advert from this pubkey arrived in the window — extend "
        "--watch-adverts or trigger an advert from the phone manually"
    )


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

    _p(f"[{_ts()}] minimum_contact_probe — pubkey={args.pubkey[:12]}… "
       f"adv_name={args.name!r}")
    _p(f"[{_ts()}] connecting to {cfg.radio.serial_port}")
    mesh_client = await MeshCore.create_serial(
        cfg.radio.serial_port, 115200, debug=False,
    )
    if mesh_client is None:
        _p("FATAL: create_serial returned None", file=sys.stderr)
        return 2

    try:
        await _remove_existing(mesh_client, args.pubkey)

        stored = await _add_minimal(mesh_client, args.pubkey, args.name)
        if stored is None:
            return 3

        # Drain protocol: see AGENTS.md "MeshCore inbound DM drain".
        # Without this, CONTACT_MSG_RECV never fires (and NEW_CONTACT
        # in the watch-adverts mode arrives via the same reader path,
        # so calling it is harmless either way).
        _p()
        _p(f"[{_ts()}] start_auto_message_fetching() — required for RX")
        await mesh_client.start_auto_message_fetching()

        if args.watch_adverts is not None:
            label, detail = await _advert_watch_leg(
                mesh_client, args.pubkey, args.watch_adverts,
            )
            _verdict(label, detail)
            _section("SUMMARY")
            _p(f"adv_name supplied  : {args.name!r}  (empty = the experiment)")
            _p(f"watch window       : {args.watch_adverts:.0f}s")
            _p(f"advert backfill    : {label}  {detail}")
            return 0 if label == PASS else 1

        tx_label, tx_detail = await _tx_leg(mesh_client, stored)
        _verdict(tx_label, tx_detail)

        if shutdown_event.is_set():
            return 130

        rx_label, rx_detail = await _rx_leg(
            mesh_client, stored, args.window,
        )
        _verdict(rx_label, rx_detail)

        _section("SUMMARY")
        _p(f"adv_name supplied : {args.name!r}")
        _p(f"TX leg            : {tx_label}  {tx_detail}")
        _p(f"RX leg            : {rx_label}  {rx_detail}")
        return 0 if (tx_label == PASS and rx_label == PASS) else 1
    finally:
        try:
            if hasattr(mesh_client, "disconnect"):
                await mesh_client.disconnect()
        except Exception:
            pass


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Probe whether a contact added with bare-minimum "
                    "fields (pubkey only, empty adv_name) supports both "
                    "inbound and outbound DMs (CIV-14).",
    )
    p.add_argument("--config", required=True, help="Path to config.toml.")
    p.add_argument(
        "--pubkey",
        required=True,
        help="Full 64-char hex pubkey of the test contact (32 bytes).",
    )
    p.add_argument(
        "--name",
        default="",
        help="adv_name to add the contact with. Default is empty string "
             "— the experiment. Set to a non-empty value for a baseline "
             "comparison run.",
    )
    p.add_argument(
        "--window",
        type=float,
        default=30.0,
        help="Seconds to listen for CONTACT_MSG_RECV during the RX leg "
             "after operator presses Enter (default 30).",
    )
    p.add_argument(
        "--watch-adverts",
        type=float,
        default=None,
        metavar="SECONDS",
        help="Alternate mode: skip TX/RX legs; add the contact with the "
             "empty adv_name and watch ADVERTISEMENT + NEW_CONTACT events "
             "for SECONDS to see if the firmware backfills adv_name from "
             "a received advert. Use --name '' (the default) for this to "
             "be meaningful.",
    )
    ns = p.parse_args()
    pk = ns.pubkey.strip().lower()
    if len(pk) != 64 or not all(c in "0123456789abcdef" for c in pk):
        p.error(f"--pubkey must be 64 hex chars, got {len(ns.pubkey)}")
    ns.pubkey = pk
    return ns


def main() -> int:
    args = _parse_args()
    return asyncio.run(_async_main(args))


if __name__ == "__main__":
    sys.exit(main())
