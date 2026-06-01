"""CIV-14 endgame: bypass the firmware and decrypt inbound DMs by hand.

This is the definitive test of whether the X25519 / AES / HMAC math is
self-consistent. If we can manually decrypt a packet the firmware
silently dropped, the firmware has a bug (or a gate we haven't found
in source). If we CAN'T decrypt it either, the keys we think we have
don't match the keys actually in use.

What it does:

  1. Exports the Pi's private key from the firmware
     (mc.commands.export_private_key, frame \\x17 — device.py:206-208).
  2. Derives the Pi's X25519 pubkey from each plausible interpretation
     of the export blob, compares to mc.self_info.public_key. Picks
     the interpretation that round-trips.
  3. Reads Fremonster's stored pubkey from mc.contacts.
  4. Computes the X25519 ECDH shared secret in Python.
  5. Either decrypts a hex packet from --cipher-hex, OR listens live
     for inbound TXT_MSG from Fremonster and decrypts each one as it
     arrives.

Crypto primitives all verified against RFC 7748 test vectors before
shipping this tool.

USB-serial access is exclusive — mesh_bot.service must be stopped.

Usage:
    # Decrypt a packet you already captured (paste the raw= hex from a
    # prior dm_rx_trace log — full 20+ bytes including dst/src/MAC):
    uv run python3 diagnostics/radio/manual_decrypt.py \\
        --config config.toml \\
        --cipher-hex 6a12dd8c96f26a5fbb657263429443e44f923541

    # Listen live and decrypt each inbound TXT_MSG from Fremonster:
    uv run python3 diagnostics/radio/manual_decrypt.py \\
        --config config.toml --capture --window 60
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from config import load_config  # noqa: E402
from meshcore import EventType, MeshCore  # noqa: E402

# pycryptodome — verified curve25519 + AES-128-ECB available.
from Crypto.Cipher import AES  # noqa: E402
from Crypto.PublicKey import ECC  # noqa: E402


FREMONSTER_PUBKEY_HEX = "1294f888153c0440ed3eedf7752415e7fbd012c4234a89c2bfc04bd215105b89"

PASS = "[PASS]"
FAIL = "[FAIL]"
INCONCLUSIVE = "[INCONCLUSIVE]"


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


# ---------------------------------------------------------------------------
# Crypto primitives — verified against RFC 7748 §6.1 test vectors before
# shipping. If any of these breaks, the manual decrypt is meaningless,
# so we self-test at startup too.
# ---------------------------------------------------------------------------

def x25519_pub_from_priv(priv_32: bytes) -> bytes:
    """Derive X25519 pubkey bytes (little-endian) from a 32-byte seed."""
    k = ECC.construct(curve="curve25519", seed=priv_32)
    return k.pointQ.x.to_bytes(32, "little")


def x25519_ecdh(priv_32: bytes, peer_pub_32: bytes) -> bytes:
    """Compute X25519 ECDH shared secret (32 bytes little-endian)."""
    priv = ECC.construct(curve="curve25519", seed=priv_32)
    pub = ECC.construct(
        curve="curve25519",
        point_x=int.from_bytes(peer_pub_32, "little"),
    )
    shared_point = priv.d * pub.pointQ
    return shared_point.x.to_bytes(32, "little")


# Curve25519 field modulus.
_P25519 = 2**255 - 19


def ed25519_pub_to_x25519_pub(ed_pub_32: bytes) -> bytes:
    """Convert an Ed25519 pubkey (Edwards-form compressed Y, 32 bytes
    little-endian with sign bit in top bit) to the corresponding
    X25519 pubkey (Montgomery U, 32 bytes little-endian). MeshCore's
    advertised/stored public keys are Ed25519; the firmware converts
    them to X25519 on demand inside getSharedSecret. We have to do
    the same on the manual-decrypt side.

    Formula: u = (1 + y) / (1 - y) mod p, with the Ed25519 sign bit
    masked out of y. (RFC 7748 §4.1; documented in `crypto_sign_ed25519`
    libsodium derivation.)
    """
    y_bytes = bytearray(ed_pub_32)
    y_bytes[31] &= 0x7f
    y = int.from_bytes(y_bytes, "little")
    u = ((1 + y) * pow(1 - y, _P25519 - 2, _P25519)) % _P25519
    return u.to_bytes(32, "little")


def aes128_ecb_decrypt(key16: bytes, ciphertext: bytes) -> bytes:
    """AES-128-ECB decrypt. Caller must pad ciphertext to a multiple of 16."""
    cipher = AES.new(key16, AES.MODE_ECB)
    return cipher.decrypt(ciphertext)


def hmac_sha256_short(key32: bytes, data: bytes) -> bytes:
    """HMAC-SHA-256 truncated to 2 bytes (the MeshCore MAC convention)."""
    return hmac.new(key32, data, hashlib.sha256).digest()[:2]


def _self_test_crypto() -> None:
    """Sanity-check pycryptodome's curve25519 against RFC 7748 §6.1
    before we trust it on the real packets."""
    a_priv = bytes.fromhex(
        "77076d0a7318a57d3c16c17251b26645df4c2f87ebc0992ab177fba51db92c2a"
    )
    b_pub = bytes.fromhex(
        "de9edb7d7b7dc1b4d35b61c2ece435373f8343c85b78674dadfc7e146f882b4f"
    )
    expected_shared = bytes.fromhex(
        "4a5d9d5ba4ce2de1728e3bf480350f25e07e21c947d19e3376f09b3c1e161742"
    )
    got = x25519_ecdh(a_priv, b_pub)
    if got != expected_shared:
        raise SystemExit(
            f"FATAL: X25519 ECDH self-test failed.\n"
            f"  expected: {expected_shared.hex()}\n"
            f"  got:      {got.hex()}"
        )


# ---------------------------------------------------------------------------
# Private-key blob interpretation
# ---------------------------------------------------------------------------

def _find_x25519_priv(blob: bytes, pi_pub_ed25519: bytes) -> tuple[bytes | None, str]:
    """Identify which slice of the export blob is the X25519 private,
    by comparing the derived X25519 pubkey against the Edwards->Montgomery
    conversion of self_info.public_key (which is Ed25519 form).

    Returns (priv32, interpretation_name) or (None, reason).
    """
    expected_x25519_pub = ed25519_pub_to_x25519_pub(pi_pub_ed25519)
    _p(f"  self_info.public_key (Ed25519)         : {pi_pub_ed25519.hex()}")
    _p(f"  -> X25519 pub (Edwards->Montgomery)    : {expected_x25519_pub.hex()}")
    _p()

    candidates: list[tuple[str, bytes]] = []
    if len(blob) == 64:
        candidates.append(("first 32 bytes of 64-byte blob (raw X25519 priv)", blob[:32]))
        candidates.append(("last 32 bytes of 64-byte blob",  blob[32:]))
        # Ed25519 priv->X25519 priv via SHA-512+clamping (RFC 8032 §5.1.5).
        # Tried as a fallback if blob[:32] turns out NOT to be raw X25519.
        h = hashlib.sha512(blob[:32]).digest()
        clamped = bytearray(h[:32])
        clamped[0] &= 248
        clamped[31] &= 127
        clamped[31] |= 64
        candidates.append((
            "SHA-512(blob[:32])[:32] with Ed25519 clamping",
            bytes(clamped),
        ))
    elif len(blob) == 32:
        candidates.append(("full 32-byte blob", blob))
    else:
        return (None, f"unexpected blob length {len(blob)}")

    for name, priv in candidates:
        try:
            derived_pub = x25519_pub_from_priv(priv)
        except Exception as e:
            _p(f"  candidate {name!r} derivation raised: {e}")
            continue
        match = derived_pub == expected_x25519_pub
        _p(f"  candidate: {name}")
        _p(f"    derived X25519 pub: {derived_pub.hex()}")
        _p(f"    match expected X25519 pub: {match}")
        if match:
            return (priv, name)
    return (None, "no candidate derived to expected X25519 pubkey")


# ---------------------------------------------------------------------------
# Packet decode
# ---------------------------------------------------------------------------

def _decrypt_packet(raw: bytes, shared_secret: bytes) -> dict:
    """Parse a DM-family envelope, verify MAC, decrypt.

    Envelope per docs/meshcore-protocol-reference.md §5.3:
      byte 0  dst_hash
      byte 1  src_hash
      bytes 2-3  MAC (2-byte truncated HMAC-SHA-256 over ciphertext)
      bytes 4+   AES-128-ECB ciphertext (multiple of 16 bytes)
    """
    if len(raw) < 4:
        return {"ok": False, "reason": f"raw too short ({len(raw)} bytes)"}
    dst_hash = raw[0]
    src_hash = raw[1]
    mac_in_packet = raw[2:4]
    ciphertext = raw[4:]

    if len(ciphertext) % 16 != 0:
        return {
            "ok": False,
            "reason": (
                f"ciphertext length {len(ciphertext)} not a multiple of 16 — "
                "not AES-ECB or packet truncated"
            ),
            "dst_hash": dst_hash,
            "src_hash": src_hash,
            "mac_in_packet": mac_in_packet.hex(),
        }

    mac_computed = hmac_sha256_short(shared_secret, ciphertext)
    mac_match = mac_computed == mac_in_packet

    plaintext = aes128_ecb_decrypt(shared_secret[:16], ciphertext)
    # TXT_MSG plaintext shape (protocol-reference.md:344-362):
    #   timestamp(4 LE) | txt_type+attempt(1 packed) | message(UTF-8)
    timestamp = int.from_bytes(plaintext[:4], "little") if len(plaintext) >= 4 else None
    if len(plaintext) >= 5:
        b = plaintext[4]
        txt_type = b >> 2
        attempt = b & 0x03
    else:
        txt_type = attempt = None
    message_bytes = plaintext[5:] if len(plaintext) > 5 else b""
    # Trim trailing nulls (AES-ECB padding artifact).
    message_bytes = message_bytes.rstrip(b"\x00")
    try:
        message_text = message_bytes.decode("utf-8")
        utf8_ok = True
    except UnicodeDecodeError:
        message_text = None
        utf8_ok = False

    return {
        "ok": True,
        "dst_hash": dst_hash,
        "src_hash": src_hash,
        "mac_in_packet": mac_in_packet.hex(),
        "mac_computed": mac_computed.hex(),
        "mac_match": mac_match,
        "ciphertext_hex": ciphertext.hex(),
        "plaintext_hex": plaintext.hex(),
        "timestamp_raw": timestamp,
        "timestamp_iso": (
            datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat()
            if timestamp and 1_700_000_000 < timestamp < 2_000_000_000
            else None
        ),
        "txt_type": txt_type,
        "attempt": attempt,
        "message_bytes_len": len(message_bytes),
        "message_text": message_text,
        "utf8_ok": utf8_ok,
    }


def _print_decrypt_result(r: dict) -> None:
    if not r["ok"]:
        _p(f"  decrypt FAILED: {r['reason']}")
        if "dst_hash" in r:
            _p(f"  dst_hash=0x{r['dst_hash']:02x} src_hash=0x{r['src_hash']:02x}")
        return
    _p(f"  dst_hash       = 0x{r['dst_hash']:02x}")
    _p(f"  src_hash       = 0x{r['src_hash']:02x}")
    _p(f"  MAC in packet  = {r['mac_in_packet']}")
    _p(f"  MAC computed   = {r['mac_computed']}")
    _p(f"  MAC match      = {r['mac_match']}")
    _p(f"  plaintext hex  = {r['plaintext_hex']}")
    _p(f"  timestamp      = {r['timestamp_raw']!r} "
       f"({r['timestamp_iso'] or 'out of plausible range'})")
    _p(f"  txt_type       = {r['txt_type']!r} (0=plain, 1=cli, 2=signed)")
    _p(f"  attempt        = {r['attempt']!r}")
    _p(f"  message_len    = {r['message_bytes_len']} bytes")
    if r["utf8_ok"]:
        _p(f"  message_text   = {r['message_text']!r}")
    else:
        _p(f"  message_text   = (not valid UTF-8)")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def _async_main(args: argparse.Namespace) -> int:
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass

    _p(f"[{_ts()}] manual_decrypt starting")
    _self_test_crypto()
    _p("[crypto self-test] RFC 7748 §6.1 ECDH vector — PASS")
    _p()

    cfg = load_config(args.config)
    _check_preconditions(cfg.radio.serial_port)

    shutdown_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, shutdown_event.set)

    mc = await MeshCore.create_serial(cfg.radio.serial_port, 115200, debug=False)
    if mc is None:
        _p("FATAL: create_serial returned None")
        return 2

    try:
        # Step 1 — self_info pubkey
        info = getattr(mc, "self_info", None) or {}
        pi_pub_hex = (info.get("public_key") if isinstance(info, dict) else None) or ""
        pi_pub_hex = pi_pub_hex.lower()
        if not pi_pub_hex or len(pi_pub_hex) != 64:
            _p(f"FATAL: self_info.public_key looks wrong: {pi_pub_hex!r}")
            return 3
        pi_pub_32 = bytes.fromhex(pi_pub_hex)
        _p(f"Pi self_info.public_key : {pi_pub_hex}")

        # Step 2 — export private key
        _p("Exporting private key (frame \\x17) ...")
        r = await mc.commands.export_private_key()
        if r is None:
            _p("FATAL: export_private_key returned None")
            return 4
        if r.type == EventType.DISABLED:
            _p("FATAL: export_private_key DISABLED by firmware. Enable via "
               "MeshCore web config (config.meshcore.dev) -> 'Export "
               "Private Key' toggle, or accept that this path is blocked.")
            return 5
        if r.type == EventType.ERROR:
            _p(f"FATAL: export_private_key returned ERROR {r.payload!r}")
            return 6
        blob = (r.payload or {}).get("private_key")
        if not isinstance(blob, (bytes, bytearray)):
            _p(f"FATAL: PRIVATE_KEY payload missing 'private_key' bytes: "
               f"{r.payload!r}")
            return 7
        blob = bytes(blob)
        _p(f"Exported blob ({len(blob)} bytes): {blob.hex()}")

        # Step 3 — find which slice / derivation is the X25519 private
        _p()
        _p("=" * 72)
        _p("Identifying X25519 private from export blob")
        _p("=" * 72)
        pi_x25519_priv, interp = _find_x25519_priv(blob, pi_pub_32)
        if pi_x25519_priv is None:
            _p()
            _p(f"{FAIL}  no interpretation of the export blob derives "
               f"to self_info.public_key.")
            _p(f"  Reason: {interp}")
            _p("This means the identity-pubkey shown in self_info is NOT "
               "the X25519 pubkey used for ECDH. The firmware must apply "
               "some derivation we don't know. Without the X25519 pubkey "
               "Fremonster has stored for us, we can't compute a shared "
               "secret. STOP.")
            return 8
        _p()
        _p(f"{PASS}  identified X25519 private via: {interp}")
        _p(f"  X25519 private (32B): {pi_x25519_priv.hex()}")

        # Step 4 — Fremonster pubkey
        await mc.commands.get_contacts()
        fremonster = None
        for pk, c in mc.contacts.items():
            if pk.lower() == FREMONSTER_PUBKEY_HEX.lower():
                fremonster = c
                break
        if fremonster is None:
            _p()
            _p(f"FATAL: Fremonster contact not present. Add it first via "
               "contact_add_minimal_test.py or contact_flags_probe.py.")
            return 9
        fremonster_pub_hex = fremonster["public_key"].lower()
        fremonster_pub_ed25519 = bytes.fromhex(fremonster_pub_hex)
        # Same Edwards->Montgomery conversion the firmware does inside
        # getSharedSecret. Without this, our ECDH would use a point on
        # the wrong curve form and the shared secret would not match.
        fremonster_pub_x25519 = ed25519_pub_to_x25519_pub(fremonster_pub_ed25519)
        _p()
        _p(f"Fremonster Ed25519 pub : {fremonster_pub_hex}")
        _p(f"-> X25519 pub          : {fremonster_pub_x25519.hex()}")

        # Step 5 — ECDH
        shared = x25519_ecdh(pi_x25519_priv, fremonster_pub_x25519)
        _p()
        _p("=" * 72)
        _p("Computed X25519 shared secret")
        _p("=" * 72)
        _p(f"  shared (32B)         : {shared.hex()}")
        _p(f"  AES key (first 16B)  : {shared[:16].hex()}")
        _p(f"  HMAC key (full 32B)  : {shared.hex()}")

        # Step 6 — decrypt
        _p()
        _p("=" * 72)
        if args.cipher_hex:
            _p("Decrypting --cipher-hex packet")
            _p("=" * 72)
            try:
                raw = bytes.fromhex(args.cipher_hex)
            except ValueError as e:
                _p(f"FATAL: --cipher-hex not valid hex: {e}")
                return 10
            _p(f"raw ({len(raw)} bytes): {raw.hex()}")
            result = _decrypt_packet(raw, shared)
            _print_decrypt_result(result)
            verdict = _verdict_for(result)
            _p()
            _p(verdict)
            return 0

        # --capture mode
        _p(f"Listening for inbound TXT_MSG from Fremonster for {args.window:.0f}s")
        _p("=" * 72)
        captured: list[dict] = []
        seen_ciphertexts: set[str] = set()  # dedupe repeats

        def _on_rx(event):
            try:
                p = event.payload or {}
                pt = p.get("payload_type")
                if pt != 0x02:  # TXT_MSG only
                    return
                raw_bytes = p.get("pkt_payload")
                if not isinstance(raw_bytes, (bytes, bytearray)):
                    return
                raw_bytes = bytes(raw_bytes)
                if len(raw_bytes) < 4:
                    return
                if raw_bytes[1] != 0x12:  # src_hash != Fremonster
                    return
                # Dedupe — flooded copies of the same message
                key = raw_bytes.hex()
                if key in seen_ciphertexts:
                    _p(f"[{_ts()}] (dup of earlier packet — skipped)")
                    return
                seen_ciphertexts.add(key)
                _p(f"[{_ts()}] inbound TXT_MSG raw_len={len(raw_bytes)} "
                   f"raw={raw_bytes.hex()}")
                result = _decrypt_packet(raw_bytes, shared)
                _print_decrypt_result(result)
                captured.append(result)
                _p()
            except Exception as e:
                _p(f"  capture handler error: {e}")

        sub = mc.subscribe(EventType.RX_LOG_DATA, _on_rx)
        try:
            _p(f"[{_ts()}] capture START")
            _p()
            _p("ACTION REQUIRED:")
            _p("  - From the phone, send DMs to this hub from Fremonster.")
            _p("  - Each TXT_MSG will be decrypted live as it arrives.")
            _p()
            try:
                await asyncio.wait_for(shutdown_event.wait(), timeout=args.window)
            except asyncio.TimeoutError:
                pass
            _p(f"[{_ts()}] capture END")
        finally:
            sub.unsubscribe()

        _p()
        _p("=" * 72)
        _p("SUMMARY")
        _p("=" * 72)
        _p(f"distinct TXT_MSG packets decrypted: {len(captured)}")
        n_mac_ok = sum(1 for r in captured if r.get("mac_match"))
        n_utf8_ok = sum(1 for r in captured if r.get("utf8_ok"))
        _p(f"  with MAC match                  : {n_mac_ok}")
        _p(f"  with valid UTF-8 plaintext      : {n_utf8_ok}")

        if not captured:
            _p()
            _p(f"{INCONCLUSIVE}  no inbound TXT_MSG from Fremonster in window. "
               "Phone didn't send or radio didn't hear.")
        elif n_mac_ok == len(captured) and n_utf8_ok == len(captured):
            _p()
            _p(f"{PASS}  ALL packets decrypted with valid MAC and UTF-8 plaintext. "
               "The X25519/AES/HMAC math IS consistent — firmware has a bug "
               "or undocumented gate preventing CONTACT_MSG_RECV surfacing.")
        elif n_mac_ok == 0:
            _p()
            _p(f"{FAIL}  zero MAC matches. The shared secret we computed is "
               "WRONG. Either Pi's X25519 private != identity private "
               "(unlikely — we verified the derivation), or Fremonster's "
               "stored pubkey isn't what they're actually signing with.")
        else:
            _p()
            _p(f"{INCONCLUSIVE}  mixed results: {n_mac_ok}/{len(captured)} MAC ok, "
               f"{n_utf8_ok}/{len(captured)} UTF-8 ok. Inspect the per-packet "
               "output above.")
        return 0
    finally:
        try:
            if hasattr(mc, "disconnect"):
                await mc.disconnect()
        except Exception:
            pass


def _verdict_for(result: dict) -> str:
    if not result.get("ok"):
        return f"{FAIL}  decrypt failed: {result.get('reason')}"
    if result.get("mac_match") and result.get("utf8_ok"):
        return (f"{PASS}  MAC matched AND plaintext is valid UTF-8: "
                f"{result['message_text']!r}. Firmware has a bug or "
                "undocumented gate.")
    if result.get("mac_match"):
        return (f"{INCONCLUSIVE}  MAC matched but plaintext isn't valid UTF-8 — "
                "shared secret is right but plaintext shape may differ from "
                "the documented TXT_MSG layout.")
    return (f"{FAIL}  MAC mismatch — the shared secret we computed is WRONG. "
            "Either we picked the wrong X25519 private, or Fremonster's "
            "stored pubkey isn't what they're signing DMs with.")


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Manually decrypt an inbound MeshCore DM in Python — "
                    "bypasses the firmware decode pipeline.",
    )
    p.add_argument("--config", required=True, help="Path to config.toml.")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument(
        "--cipher-hex",
        help="Hex string of the captured raw packet (dst_hash + src_hash + "
             "MAC + ciphertext). Use a value from an earlier dm_rx_trace log.",
    )
    g.add_argument(
        "--capture",
        action="store_true",
        help="Listen live for inbound TXT_MSG from Fremonster and decrypt each.",
    )
    p.add_argument(
        "--window",
        type=float,
        default=60.0,
        help="Capture window seconds (with --capture; default 60).",
    )
    return p.parse_args()


def main() -> int:
    args = _parse_args()
    return asyncio.run(_async_main(args))


if __name__ == "__main__":
    sys.exit(main())
