"""CIV-14: standalone diagnostic for "I sent a DM and never got an ACK."

Runs three checks against a specific MeshCore contact and prints
PASS / FAIL / INCONCLUSIVE per check on stdout. Diagnostic detail
(captured packets, payloads, raw timeouts) is printed above each
verdict.

  1. Pubkey check        — does the stored contact pubkey match what
                           the radio sees on the air right now?
  2. Send + ACK          — does send_msg get an ACK back?
                           Records every RX_LOG_DATA seen while
                           waiting, so a missing ACK can be told
                           apart from a missing RX.
  3. Path discovery      — does send_path_discovery produce a
                           PATH_RESPONSE or PATH_UPDATE?

USB-serial access is exclusive — mesh_bot.service must be stopped
before running.

Usage:
    uv run python3 diagnostics/radio/dm_diagnose.py \\
        --config config.toml \\
        CONTACT

CONTACT is matched against the contact list case-insensitively
first as `adv_name`, then as a `public_key` prefix (mirrors
meshcore_py's `get_contact_by_name` / `get_contact_by_key_prefix`
at meshcore/meshcore.py:372-409).
"""

from __future__ import annotations

import argparse
import asyncio
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
from meshcore import EventType, MeshCore  # noqa: E402


# ---------------------------------------------------------------------------
# Verdict glyphs
# ---------------------------------------------------------------------------

PASS = "[PASS]"
FAIL = "[FAIL]"
INCONCLUSIVE = "[INCONCLUSIVE]"


def _section(title: str) -> None:
    print()
    print("=" * 72)
    print(title)
    print("=" * 72)


def _verdict(label: str, detail: str = "") -> None:
    if detail:
        print(f"{label}  {detail}")
    else:
        print(label)


# ---------------------------------------------------------------------------
# Precondition checks (mirrors diagnostics/radio/t9_liveness_latency.py)
# ---------------------------------------------------------------------------

def _check_preconditions(serial_port: str) -> None:
    # mesh_bot.service holds the serial port exclusively. Fail loud
    # rather than producing a confusing "appstart timed out" further down.
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
# Contact resolution
# ---------------------------------------------------------------------------

def _resolve_contact(mesh_client, ident: str) -> dict | None:
    """Match `ident` against the contact cache by name first, then by
    pubkey prefix. Matches meshcore_py's case-insensitivity."""
    by_name = mesh_client.get_contact_by_name(ident)
    if by_name is not None:
        return by_name
    return mesh_client.get_contact_by_key_prefix(ident)


# ---------------------------------------------------------------------------
# Check 1 — pubkey
# ---------------------------------------------------------------------------

async def _check_pubkey(mesh_client, contact: dict, window_s: float) -> None:
    _section("CHECK 1 — Pubkey: stored vs advertised")

    stored_pubkey = (contact.get("public_key") or "").lower()
    stored_name = contact.get("adv_name") or "<no adv_name>"
    print(f"contact name      : {stored_name}")
    print(f"stored public_key : {stored_pubkey}")
    print(f"listening for ADVERTISEMENT events for {window_s:.0f}s ...")

    # ADVERTISEMENT payload is {public_key: "<64 hex>"} per
    # meshcore/reader.py:497-501. Adverts are sparse — our own bot
    # cools down at 6h between sends, and other nodes vary.
    captured: list[str] = []

    def _on_advert(event):
        try:
            payload = event.payload or {}
            pk = (payload.get("public_key") or "").lower()
            if pk:
                captured.append(pk)
                marker = "  *MATCH*" if pk == stored_pubkey else ""
                print(f"  advert public_key={pk}{marker}")
        except Exception as e:
            print(f"  advert handler error: {e}", file=sys.stderr)

    sub = mesh_client.subscribe(EventType.ADVERTISEMENT, _on_advert)
    try:
        await asyncio.sleep(window_s)
    finally:
        sub.unsubscribe()

    n = len(captured)
    matched = [pk for pk in captured if pk == stored_pubkey]
    print()
    print(f"adverts captured  : {n}")
    print(f"matching contact  : {len(matched)}")

    if matched:
        _verdict(PASS, "advert pubkey matches stored pubkey")
    elif n == 0:
        _verdict(
            INCONCLUSIVE,
            f"no ADVERTISEMENT events captured in {window_s:.0f}s window — "
            "can't say whether the contact's pubkey is stable",
        )
    else:
        _verdict(
            FAIL,
            f"{n} adverts captured but none matched stored pubkey "
            "(contact may have rotated, or contact wasn't among the "
            "advertising nodes in this window)",
        )


# ---------------------------------------------------------------------------
# Check 2 — send + ACK
# ---------------------------------------------------------------------------

async def _check_send_and_ack(mesh_client, contact: dict) -> None:
    _section("CHECK 2 — Send DM and wait for ACK")

    body = f"civicmesh-dmdiag {int(time.time())}"
    print(f"sending message    : {body!r}")
    print(f"to contact         : {contact.get('adv_name')!r} "
          f"({contact.get('public_key', '')[:16]}...)")

    # Subscribe to RX_LOG_DATA so we can tell "ACK didn't come back"
    # apart from "radio heard nothing at all" — the two failure
    # modes have very different next steps.
    rx_packets: list[dict] = []

    def _on_rx_log(event):
        try:
            payload = event.payload or {}
            rx_packets.append(payload)
            print(
                f"  rx_log payload_type={payload.get('payload_type')} "
                f"route_type={payload.get('route_type')} "
                f"path_len={payload.get('path_len')} "
                f"snr={payload.get('snr')} "
                f"rssi={payload.get('rssi')}"
            )
        except Exception as e:
            print(f"  rx_log handler error: {e}", file=sys.stderr)

    rx_sub = mesh_client.subscribe(EventType.RX_LOG_DATA, _on_rx_log)
    try:
        result = await mesh_client.commands.send_msg(contact, body)
        if result is None or result.type == EventType.ERROR:
            payload = getattr(result, "payload", None)
            print(f"send_msg result    : ERROR payload={payload!r}")
            _verdict(FAIL, "send_msg returned ERROR — radio refused the send")
            return

        # expected_ack / suggested_timeout shape per
        # meshcore/commands/messaging.py:150-151.
        expected_ack = result.payload["expected_ack"].hex()
        suggested_ms = result.payload.get("suggested_timeout", 4000)
        # send_msg_with_retry uses suggested_ms / 1000 * 1.2. Mirror
        # that, then floor at 15s per the diagnostic spec.
        wait_s = max(15.0, suggested_ms / 1000.0 * 1.2)

        print(f"expected_ack       : {expected_ack}")
        print(f"suggested_timeout  : {suggested_ms} ms")
        print(f"waiting up to      : {wait_s:.1f}s for ACK code={expected_ack}")
        print()

        t0 = time.monotonic()
        ack = await mesh_client.dispatcher.wait_for_event(
            EventType.ACK,
            attribute_filters={"code": expected_ack},
            timeout=wait_s,
        )
        elapsed = time.monotonic() - t0

        print()
        print(f"rx_log packets seen during wait : {len(rx_packets)}")
        if ack is not None:
            print(f"ACK received after {elapsed:.2f}s")
            _verdict(PASS, f"ACK received in {elapsed:.2f}s")
        elif rx_packets:
            _verdict(
                INCONCLUSIVE,
                f"no ACK in {wait_s:.0f}s but {len(rx_packets)} RX packets "
                "captured — radio is RXing, recipient may have received "
                "but not ACKed",
            )
        else:
            _verdict(
                FAIL,
                f"no ACK and no RX activity in {wait_s:.0f}s — "
                "either RF is dead or no addressed packet reached us",
            )
    finally:
        rx_sub.unsubscribe()


# ---------------------------------------------------------------------------
# Check 3 — path discovery
# ---------------------------------------------------------------------------

async def _check_path_discovery(mesh_client, contact: dict) -> None:
    _section("CHECK 3 — Path discovery")

    pubkey = contact.get("public_key", "").lower()
    # PATH_RESPONSE attributes carry the first 12 hex chars of the
    # pubkey under the key `pubkey_pre` per meshcore/reader.py:841.
    pubkey_pre = pubkey[:12]

    print(f"calling send_path_discovery on contact {pubkey[:16]}...")

    # Pre-subscribe BEFORE sending so a fast PATH_RESPONSE / PATH_UPDATE
    # can't slip past us between the request returning and us starting
    # to listen. asyncio.create_task plus the dispatcher's future
    # pattern gives both subscribes the same wait window.
    path_resp_task = asyncio.create_task(
        mesh_client.dispatcher.wait_for_event(
            EventType.PATH_RESPONSE,
            attribute_filters={"pubkey_pre": pubkey_pre},
            timeout=None,
        )
    )
    path_upd_task = asyncio.create_task(
        mesh_client.dispatcher.wait_for_event(
            EventType.PATH_UPDATE,
            attribute_filters=None,
            timeout=None,
        )
    )

    try:
        # Spec called for the raw `send_path_discovery` (the library
        # warns to prefer `send_path_discovery_sync`) so we can observe
        # BOTH PATH_RESPONSE and PATH_UPDATE — the sync wrapper only
        # waits for PATH_RESPONSE.
        result = await mesh_client.commands.send_path_discovery(contact)
        if result is None or result.type == EventType.ERROR:
            payload = getattr(result, "payload", None)
            print(f"send_path_discovery result : ERROR payload={payload!r}")
            _verdict(FAIL, "send_path_discovery returned ERROR")
            return

        suggested_ms = result.payload.get("suggested_timeout", 4000)
        # send_path_discovery_sync uses suggested_ms / 800, floored at 15s
        # to give path setup time on a multi-hop mesh.
        wait_s = max(15.0, suggested_ms / 800.0)
        print(f"suggested_timeout  : {suggested_ms} ms")
        print(f"waiting up to      : {wait_s:.1f}s for PATH_RESPONSE or PATH_UPDATE")
        print()

        done, pending = await asyncio.wait(
            {path_resp_task, path_upd_task},
            timeout=wait_s,
            return_when=asyncio.FIRST_COMPLETED,
        )

        if not done:
            _verdict(
                INCONCLUSIVE,
                f"no PATH_RESPONSE or PATH_UPDATE in {wait_s:.0f}s — "
                "router along the path may be down, or contact is asleep",
            )
            return

        # Whichever fired first — print both branches.
        for task in done:
            event = task.result()
            if event is None:
                continue
            if event.type == EventType.PATH_RESPONSE:
                p = event.payload or {}
                print("PATH_RESPONSE captured")
                print(f"  out_path_len  : {p.get('out_path_len')}")
                print(f"  out_path      : {p.get('out_path')}")
                print(f"  in_path_len   : {p.get('in_path_len')}")
                print(f"  in_path       : {p.get('in_path')}")
                print(f"  pubkey_pre    : {p.get('pubkey_pre')}")
                _verdict(PASS, "PATH_RESPONSE received")
            elif event.type == EventType.PATH_UPDATE:
                print("PATH_UPDATE captured (no path payload — re-reading contact)")
                # Path landed in the contact cache via the library's
                # internal handler at meshcore/meshcore.py:310-311.
                refreshed = _resolve_contact(
                    mesh_client, contact.get("public_key", "")
                ) or contact
                print(f"  refreshed contact out_path_len : "
                      f"{refreshed.get('out_path_len')}")
                print(f"  refreshed contact out_path     : "
                      f"{refreshed.get('out_path')}")
                _verdict(PASS, "PATH_UPDATE received")

    finally:
        for task in (path_resp_task, path_upd_task):
            if not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def _async_main(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    _check_preconditions(cfg.radio.serial_port)

    # Honour Ctrl-C cleanly so a hung wait doesn't leave the radio
    # in a weird state.
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

    try:
        # Populate the contacts cache. The send_msg / send_path_discovery
        # paths both want a contact dict, and contact resolution by
        # name/prefix walks the cache.
        print("Fetching contacts ...", file=sys.stderr)
        contacts_result = await mesh_client.commands.get_contacts()
        if contacts_result is None or contacts_result.type == EventType.ERROR:
            print(
                f"FATAL: get_contacts failed: {contacts_result!r}",
                file=sys.stderr,
            )
            return 3

        contact = _resolve_contact(mesh_client, args.contact)
        if contact is None:
            print(
                f"FATAL: no contact matched {args.contact!r} "
                f"(tried name, then pubkey prefix). "
                f"{len(mesh_client.contacts)} contacts in cache.",
                file=sys.stderr,
            )
            return 4

        await _check_pubkey(mesh_client, contact, args.advert_window)
        if shutdown_event.is_set():
            return 130

        await _check_send_and_ack(mesh_client, contact)
        if shutdown_event.is_set():
            return 130

        await _check_path_discovery(mesh_client, contact)
        return 0
    finally:
        # Best-effort cleanup. MeshCore.create_serial doesn't expose a
        # clean shutdown; the connection_manager owns the asyncio task.
        try:
            if hasattr(mesh_client, "disconnect"):
                await mesh_client.disconnect()
        except Exception:
            pass


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Diagnose DM-no-ACK issues against a specific MeshCore contact.",
    )
    p.add_argument("--config", required=True, help="Path to config.toml.")
    p.add_argument(
        "contact",
        help="Contact identifier — adv_name or pubkey prefix "
             "(case-insensitive).",
    )
    p.add_argument(
        "--advert-window",
        type=float,
        default=30.0,
        help="Seconds to listen for ADVERTISEMENT events in check 1 "
             "(default 30).",
    )
    return p.parse_args()


def main() -> int:
    args = _parse_args()
    return asyncio.run(_async_main(args))


if __name__ == "__main__":
    sys.exit(main())
