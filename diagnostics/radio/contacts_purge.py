"""CIV-14: empty the MeshCore companion contact list and disable
firmware auto-add.

Why this exists. CivicMesh hubs run as channel relays; mesh_bot.py
never reads or writes the contact list. With firmware default
`manual_add_contacts=false`, every flood advert the Heltec hears gets
auto-added, so the table fills with strangers over time (we observed
the 350-row ERR_CODE_TABLE_FULL cap in production). Once full, no
intentional contact (the operator's phone for CIV-14 DM-status) can
be added.

This tool fixes the immediate symptom by disabling auto-add and
bulk-removing every existing entry. Pair with a startup-side guard
in mesh_bot.py (see follow-up — keeps the contact table from
refilling on its own across reboots).

Order matters. Disable auto-add BEFORE removing — otherwise a fresh
flood advert during the remove loop will keep adding rows. Two
separate settings have to go off:

  1. `set_manual_add_contacts(True)`  — the self_info boolean,
     applied via set_other_params_from_infos (commands/device.py:146).
  2. `set_autoadd_config(0)`          — the separate byte-flag at
     commands/contact.py:180.

Default mode is DRY RUN — read the state, preview the first 20
contacts (sorted by last_advert ASC = stalest first), exit
without mutating anything. Pass `--yes-really` to actually purge.

USB-serial access is exclusive — mesh_bot.service must be stopped
before running.

Usage:
    # dry run: see what we'd do
    uv run python3 diagnostics/radio/contacts_purge.py --config config.toml

    # actually purge
    uv run python3 diagnostics/radio/contacts_purge.py \\
        --config config.toml --yes-really
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
# I/O helpers — eager flush so tee shows progress mid-run
# ---------------------------------------------------------------------------

def _p(*args, **kwargs) -> None:
    kwargs.setdefault("flush", True)
    print(*args, **kwargs)


def _ts() -> str:
    return datetime.now().isoformat(timespec="milliseconds")


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
            _p(
                "FATAL: mesh_bot.service is active. "
                "Stop it manually first (this script will not stop services):\n"
                "  sudo systemctl stop mesh_bot",
            )
            sys.exit(1)
    except FileNotFoundError:
        pass  # systemctl not available (dev machine)
    except subprocess.TimeoutExpired:
        _p("WARNING: systemctl timed out; skipping mesh_bot check")

    if not os.path.exists(serial_port):
        _p(f"FATAL: serial port {serial_port} does not exist")
        sys.exit(1)
    if not os.access(serial_port, os.R_OK | os.W_OK):
        _p(f"FATAL: serial port {serial_port} is not readable/writable")
        sys.exit(1)


# ---------------------------------------------------------------------------
# State read
# ---------------------------------------------------------------------------

async def _read_settings(mc) -> dict:
    """Return current auto-add settings as a dict. self_info is
    re-fetched via send_appstart so the snapshot reflects post-change
    state on verify calls."""
    appstart = await mc.commands.send_appstart()
    self_info = (appstart.payload if appstart is not None else None) or {}
    manual_add = self_info.get("manual_add_contacts")

    autoadd_res = await mc.commands.get_autoadd_config()
    if autoadd_res is None or autoadd_res.type == EventType.ERROR:
        autoadd_flag = None
    else:
        autoadd_payload = autoadd_res.payload or {}
        # AUTOADD_CONFIG payload — flag byte is whatever the reader
        # parsed from PacketType.AUTOADD_CONFIG (reader.py:468-473);
        # we just surface every key the library exposed.
        autoadd_flag = autoadd_payload

    return {
        "manual_add_contacts": manual_add,
        "autoadd_config": autoadd_flag,
    }


# ---------------------------------------------------------------------------
# Mutation
# ---------------------------------------------------------------------------

async def _disable_autoadd(mc) -> tuple[bool, str]:
    """Apply both settings. Returns (ok, reason)."""
    r1 = await mc.commands.set_manual_add_contacts(True)
    if r1 is None or r1.type == EventType.ERROR:
        return (False, f"set_manual_add_contacts returned {r1!r}")
    r2 = await mc.commands.set_autoadd_config(0)
    if r2 is None or r2.type == EventType.ERROR:
        return (False, f"set_autoadd_config returned {r2!r}")
    return (True, "both settings applied OK")


async def _bulk_remove(mc, pacing_s: float) -> dict:
    """Iterate the contacts cache, remove each. Returns counts."""
    # Snapshot keys upfront — mc.contacts mutates as we delete, and
    # iterating a live dict is asking for trouble. Sort by last_advert
    # ASC so the stalest go first (purely cosmetic — affects the
    # progress messages only).
    snapshot = sorted(
        mc.contacts.items(),
        key=lambda kv: (kv[1].get("last_advert") or 0),
    )
    total = len(snapshot)
    ok = 0
    err = 0
    _p(f"[{_ts()}] purge START total={total}")
    for i, (pubkey_hex, contact) in enumerate(snapshot, 1):
        try:
            res = await mc.commands.remove_contact(bytes.fromhex(pubkey_hex))
            if res is not None and res.type == EventType.OK:
                ok += 1
            else:
                err += 1
                _p(f"  [{i}/{total}] remove_contact "
                   f"name={contact.get('adv_name')!r} "
                   f"pk={pubkey_hex[:16]}... returned {res!r}")
        except Exception as e:
            err += 1
            _p(f"  [{i}/{total}] remove_contact "
               f"pk={pubkey_hex[:16]}... raised {e!r}")
        # Progress every 25.
        if i % 25 == 0:
            _p(f"  [{_ts()}] progress {i}/{total}  ok={ok} err={err}")
        # Light pacing so we don't hammer the serial pipe or the
        # firmware's LittleFS lazy-write debounce.
        if pacing_s > 0:
            await asyncio.sleep(pacing_s)
    _p(f"[{_ts()}] purge END ok={ok} err={err} total={total}")
    return {"ok": ok, "err": err, "total": total}


# ---------------------------------------------------------------------------
# Preview
# ---------------------------------------------------------------------------

def _print_contacts_preview(mc, n: int) -> None:
    items = sorted(
        mc.contacts.items(),
        key=lambda kv: (kv[1].get("last_advert") or 0),
    )
    _p(f"Contact count: {len(items)}")
    if not items:
        return
    show = items[:n]
    _p(f"First {len(show)} contacts (sorted by last_advert ASC — stalest first):")
    _p(f"  {'#':<4} {'last_advert':<12} {'adv_name':<22} "
       f"{'type':<6} {'out_path_len':<12} pubkey_prefix")
    for i, (pubkey_hex, c) in enumerate(show, 1):
        la = c.get("last_advert") or 0
        _p(f"  {i:<4} {la:<12} "
           f"{(c.get('adv_name') or '')[:22]:<22} "
           f"{str(c.get('type', '?')):<6} "
           f"{str(c.get('out_path_len', '?')):<12} "
           f"{pubkey_hex[:16]}...")
    if len(items) > n:
        _p(f"  ... and {len(items) - n} more.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

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

    _p(f"[{_ts()}] contacts_purge connecting to {cfg.radio.serial_port}")
    mc = await MeshCore.create_serial(cfg.radio.serial_port, 115200, debug=False)
    if mc is None:
        _p("FATAL: create_serial returned None")
        return 2

    try:
        # Read current state.
        before = await _read_settings(mc)
        _p()
        _p("=" * 72)
        _p("CURRENT STATE")
        _p("=" * 72)
        _p(f"manual_add_contacts : {before['manual_add_contacts']!r}  "
           "(True = auto-add OFF; False = auto-add ON)")
        _p(f"autoadd_config      : {before['autoadd_config']!r}")
        _p()

        # Fetch contacts.
        r = await mc.commands.get_contacts()
        if r is None or r.type == EventType.ERROR:
            _p(f"FATAL: get_contacts returned {r!r}")
            return 3

        _print_contacts_preview(mc, n=20)

        if not args.yes_really:
            _p()
            _p("DRY RUN — no changes made.")
            _p("Re-run with --yes-really to disable auto-add and purge all contacts.")
            return 0

        # --- mutation path ---

        _p()
        _p("=" * 72)
        _p("MUTATING — disabling auto-add first, then bulk-removing")
        _p("=" * 72)

        ok, reason = await _disable_autoadd(mc)
        _p(f"disable auto-add: {reason}")
        if not ok:
            _p("FATAL: settings did not apply; not removing contacts to "
               "avoid an immediate refill from new adverts.")
            return 4

        # Verify both knobs read back as expected. send_appstart returns
        # the new self_info; get_autoadd_config queries firmware state.
        after_settings = await _read_settings(mc)
        _p(f"verify  manual_add_contacts: {after_settings['manual_add_contacts']!r} "
           f"(want True)")
        _p(f"verify  autoadd_config:      {after_settings['autoadd_config']!r} "
           "(want flag byte 0)")
        if after_settings["manual_add_contacts"] is not True:
            _p("FATAL: manual_add_contacts did not flip to True; aborting.")
            return 5
        # autoadd_config has multiple shapes across firmware versions
        # so we accept anything where the byte parses as 0 OR the
        # payload is empty/None. Belt + suspenders.
        ac = after_settings["autoadd_config"]
        ac_ok = (
            ac is None
            or (isinstance(ac, dict) and (
                ac.get("flag") in (0, None)
                or all(v in (0, None, "", False) for v in ac.values())
            ))
        )
        if not ac_ok:
            _p(f"WARNING: autoadd_config readback nonzero: {ac!r}. "
               "Proceeding anyway since manual_add_contacts is True — "
               "the firmware should respect the boolean.")

        # Re-fetch contacts AFTER disabling — closes the tiny window
        # where a new flood advert could have added a row between
        # connect and the disable.
        r = await mc.commands.get_contacts()
        if r is None or r.type == EventType.ERROR:
            _p(f"FATAL: refresh get_contacts returned {r!r}")
            return 6

        _p()
        _p(f"Snapshot for removal: {len(mc.contacts)} contacts")
        result = await _bulk_remove(mc, pacing_s=args.pacing_ms / 1000.0)

        # Final verify.
        r = await mc.commands.get_contacts()
        if r is None or r.type == EventType.ERROR:
            _p(f"WARNING: final get_contacts returned {r!r}; "
               "couldn't verify empty state.")
        else:
            _p()
            _p(f"final contact count: {len(mc.contacts)}")
            if mc.contacts:
                _p("Residual contacts (likely auto-added during the loop "
                   "if any settings didn't flip, OR remove_contact errors):")
                _print_contacts_preview(mc, n=20)

        _p()
        _p("=" * 72)
        _p(f"DONE  ok={result['ok']} err={result['err']} total={result['total']}")
        _p("=" * 72)
        return 0 if result["err"] == 0 else 7
    finally:
        try:
            if hasattr(mc, "disconnect"):
                await mc.disconnect()
        except Exception:
            pass


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Empty the MeshCore companion contact list and "
                    "disable firmware auto-add. Dry-run by default.",
    )
    p.add_argument("--config", required=True, help="Path to config.toml.")
    p.add_argument(
        "--yes-really",
        action="store_true",
        help="Actually mutate state. Without this, the script is a dry run.",
    )
    p.add_argument(
        "--pacing-ms",
        type=float,
        default=50.0,
        help="Sleep between remove_contact calls (default 50ms). Keeps "
             "the serial pipe and LittleFS lazy-write debounce healthy.",
    )
    return p.parse_args()


def main() -> int:
    args = _parse_args()
    return asyncio.run(_async_main(args))


if __name__ == "__main__":
    sys.exit(main())
