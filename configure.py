"""Interactive `civicmesh configure` walk.

Prompts only for Tier-1 fields; round-trips Tier-2 from the existing
config (or `config.toml.example` on first run). Atomic write via
tempfile + os.replace, with post-write validation against the tempfile
*before* the replace — so the live config.toml is never overwritten by
a payload that fails load_config.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path
from typing import Any, Callable

import tomli_w

from civicmesh import _default_db_path
from config import (
    _COUNTRY_RE,
    _IFACE_RE,
    _validate_callsign,
    _validate_ssid,
    load_config,
)


_NON_OVERLAPPING_CHANNELS = (1, 6, 11)
_SERIAL_GLOBS = ("*Silicon_Labs*CP2102*", "*USB_to_UART*")


# --------------------------------------------------------------------- baseline


def _load_baseline(config_path: Path) -> dict[str, Any]:
    """Parse existing config.toml as a raw dict, or fall back to the example."""
    if config_path.is_file():
        with config_path.open("rb") as f:
            return tomllib.load(f)
    example = Path(__file__).resolve().parent / "config.toml.example"
    with example.open("rb") as f:
        return tomllib.load(f)


# --------------------------------------------------------------------- detect


def _detect_serial_port() -> list[Path]:
    """Glob /dev/serial/by-id for known USB-UART bridges."""
    by_id = Path("/dev/serial/by-id")
    if not by_id.is_dir():
        return []
    seen: set[Path] = set()
    out: list[Path] = []
    for pattern in _SERIAL_GLOBS:
        for p in sorted(by_id.glob(pattern)):
            if p not in seen:
                seen.add(p)
                out.append(p)
    return out


def _detect_iface() -> list[str]:
    """Parse `ip -j link show` for wlan* names."""
    try:
        result = subprocess.run(
            ["ip", "-j", "link", "show"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []
    if result.returncode != 0:
        return []
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return []
    return [
        entry["ifname"]
        for entry in data
        if isinstance(entry, dict) and isinstance(entry.get("ifname"), str)
        and entry["ifname"].startswith("wlan")
    ]


# --------------------------------------------------------------------- prompts


def _prompt_string(
    label: str,
    default: str | None,
    validator: Callable[[str], str] | None = None,
) -> str:
    suffix = f" [{default}]" if default else ""
    while True:
        raw = input(f"{label}{suffix}: ").strip()
        if not raw and default is not None:
            raw = default
        if not raw:
            print("  value required; please enter something.")
            continue
        if validator is not None:
            try:
                return validator(raw)
            except ValueError as e:
                print(f"  {e}")
                continue
        return raw


def _prompt_bool(label: str, default: bool) -> bool:
    suffix = "[Y/n]" if default else "[y/N]"
    while True:
        raw = input(f"{label} {suffix}: ").strip().lower()
        if not raw:
            return default
        if raw in ("y", "yes"):
            return True
        if raw in ("n", "no"):
            return False
        print("  please answer y or n.")


def _prompt_choice(label: str, options: tuple[int, ...], default: int) -> int:
    opts_str = ", ".join(str(o) for o in options)
    valid_default = default in options
    if not valid_default:
        print(
            f"  current value {default} is not in {{{opts_str}}}; please pick one."
        )
    suffix = f" [{default}]" if valid_default else ""
    while True:
        raw = input(f"{label} ({opts_str}){suffix}: ").strip()
        if not raw:
            if valid_default:
                return default
            print(f"  must be one of {{{opts_str}}}.")
            continue
        try:
            value = int(raw)
        except ValueError:
            print(f"  please enter an integer in {{{opts_str}}}.")
            continue
        if value in options:
            return value
        print(f"  must be one of {{{opts_str}}}.")


def _prompt_channels(current: list[str]) -> list[str]:
    """Add/remove/clear/done sub-loop for channels.names."""
    chans = list(current)
    while True:
        if chans:
            print(f"  current channels: {chans}")
        else:
            print("  current channels: (empty)")
        raw = input("  [a]dd / [r]emove / [c]lear / [d]one: ").strip().lower()
        if raw in ("d", "done", ""):
            if not chans:
                if not _prompt_bool(
                    "  channel list is empty; continue with no channels?", False
                ):
                    continue
            return chans
        if raw in ("a", "add"):
            name = input("    channel name: ").strip()
            if name:
                chans.append(name)
            continue
        if raw in ("r", "remove"):
            name = input("    channel name to remove: ").strip()
            if name in chans:
                chans.remove(name)
            else:
                print("    not in list.")
            continue
        if raw in ("c", "clear"):
            chans = []
            continue
        print("  please choose a, r, c, or d.")


# ----------------------------------------------------------- field validators


def _validate_iface_name(raw: str) -> str:
    if not _IFACE_RE.match(raw):
        raise ValueError(f"iface {raw!r} must match {_IFACE_RE.pattern}")
    return raw


def _validate_country(raw: str) -> str:
    canonical = raw.strip().upper()
    if not _COUNTRY_RE.match(canonical):
        raise ValueError("country_code must be two uppercase letters (ISO 3166-1 alpha-2)")
    return canonical


def _warn_iface_not_present(name: str) -> None:
    if not Path(f"/sys/class/net/{name}").exists():
        print(f"  warning: iface {name!r} is not currently present on this host.")


def _warn_serial_not_present(path: str) -> None:
    if not Path(path).exists():
        print(f"  warning: serial port {path!r} does not currently exist.")


# --------------------------------------------------------------- prompt walk


def _walk_prompts(baseline: dict[str, Any]) -> dict[str, Any]:
    """Run all Tier-1 prompts; return the user's chosen values."""
    node = baseline.get("node", {})
    radio = baseline.get("radio", {})
    ap_section = baseline.get("ap", {})
    network = baseline.get("network", {})
    channels = baseline.get("channels", {})
    debug = baseline.get("debug", {})

    print("Configuring CivicMesh node. Press Enter to accept defaults shown in brackets.\n")

    print("\nnode.site_name — human-readable name shown to walk-up users "
          "on the captive portal welcome page. e.g. 'Fremont Hub'.")
    site_name = _prompt_string(
        "node.site_name",
        str(node.get("site_name") or node.get("name") or "") or None,
    )
    print("\nnode.callsign — short handle broadcast on the LoRa mesh; "
          "other MeshCore users see this in their contact list. Keep "
          "it short (limits.name_max_chars caps it). e.g. 'fremont1'.")
    callsign = _prompt_string(
        "node.callsign",
        str(node.get("callsign") or "") or None,
        _validate_callsign,
    )

    print("\nchannels.names — channels this node joins on the mesh.")
    chan_names = _prompt_channels([str(x) for x in channels.get("names", [])])

    print("\nradio.serial_port — USB path to the Heltec V3 mesh radio. "
          "Auto-detected when a CP2102 / Silicon Labs USB-UART bridge is plugged in.")
    serial_port = _prompt_serial(str(radio.get("serial_port") or "") or None)
    ssid = _prompt_string(
        "ap.ssid",
        str(ap_section.get("ssid") or "CivicMesh-Messages"),
        _validate_ssid,
    )
    channel = _prompt_choice(
        "ap.channel",
        _NON_OVERLAPPING_CHANNELS,
        int(ap_section.get("channel", 6)),
    )
    iface = _prompt_iface(str(network.get("iface") or "wlan0"))
    country = _prompt_string(
        "network.country_code",
        str(network.get("country_code") or "US"),
        _validate_country,
    )
    print("\ndebug.allow_eth0 — DEV ONLY. When true, traffic on eth0 "
          "bypasses MAC validation and auto-creates portal sessions. "
          "Leave false for any deployed node.")
    allow_eth0 = _prompt_bool(
        "Is this a development machine reachable over wired ethernet?",
        bool(debug.get("allow_eth0", False)),
    )

    return {
        "node.site_name": site_name,
        "node.callsign": callsign,
        "channels.names": chan_names,
        "radio.serial_port": serial_port,
        "ap.ssid": ssid,
        "ap.channel": channel,
        "network.iface": iface,
        "network.country_code": country,
        "debug.allow_eth0": allow_eth0,
    }


def _prompt_serial(current: str | None) -> str:
    detected = _detect_serial_port()
    if len(detected) == 1:
        path = str(detected[0])
        print(f"\nradio.serial_port — detected: {path}")
        if _prompt_bool("Use this?", True):
            return path
    elif len(detected) > 1:
        print("\nradio.serial_port — multiple candidates detected:")
        for i, p in enumerate(detected, 1):
            print(f"  {i}. {p}")
        while True:
            raw = input(f"  pick a number, or enter a path manually [{current or ''}]: ").strip()
            if not raw and current:
                _warn_serial_not_present(current)
                return current
            if raw.isdigit() and 1 <= int(raw) <= len(detected):
                return str(detected[int(raw) - 1])
            if raw and not raw.isdigit():
                _warn_serial_not_present(raw)
                return raw
            print("  pick a number from the list or paste a /dev/... path.")
    else:
        print("\nradio.serial_port — no Heltec V3 / CP2102 device detected on /dev/serial/by-id.")
    path = _prompt_string("radio.serial_port", current)
    _warn_serial_not_present(path)
    return path


def _prompt_iface(current: str) -> str:
    detected = _detect_iface()
    if len(detected) == 1:
        name = detected[0]
        print(f"\nnetwork.iface — detected: {name}")
        if _prompt_bool("Use this?", True):
            return name
    elif len(detected) > 1:
        print(f"\nnetwork.iface — multiple wlan interfaces detected: {detected}")
    name = _prompt_string("network.iface", current, _validate_iface_name)
    _warn_iface_not_present(name)
    return name


# ------------------------------------------------------------------- confirm


def _confirm_write(config_path: Path, tier1: dict[str, Any]) -> bool:
    print("\nProposed configuration (Tier 1):")
    for k, v in tier1.items():
        print(f"  {k} = {v!r}")
    print("(Tier 2 sections are unchanged from the existing config / example defaults.)\n")
    return _prompt_bool(f"Write to {config_path}?", False)


# ----------------------------------------------------------- apply + write


def _apply_tier1(baseline: dict[str, Any], tier1: dict[str, Any]) -> None:
    """Mutate baseline so Tier-1 prompted values land in the right sections."""
    baseline.setdefault("node", {})["site_name"] = tier1["node.site_name"]
    baseline["node"]["callsign"] = tier1["node.callsign"]
    baseline["node"].pop("name", None)       # strip deprecated alias
    baseline["node"].pop("location", None)   # strip removed field
    baseline.setdefault("channels", {})["names"] = list(tier1["channels.names"])
    baseline.setdefault("radio", {})["serial_port"] = tier1["radio.serial_port"]
    baseline.setdefault("ap", {})["ssid"] = tier1["ap.ssid"]
    baseline["ap"]["channel"] = tier1["ap.channel"]
    baseline.setdefault("network", {})["iface"] = tier1["network.iface"]
    baseline["network"]["country_code"] = tier1["network.country_code"]
    baseline.setdefault("debug", {})["allow_eth0"] = tier1["debug.allow_eth0"]

    # Bake an absolute db_path. config.py refuses any other shape.
    baseline["db_path"] = str(_default_db_path().resolve())
    # If a stray db_path landed inside a section from the legacy bug
    # (top-level key written after a section header), strip it so the
    # rendered file has exactly one db_path and tomllib doesn't choke
    # on the next read.
    for section in (
        "logging", "node", "network", "ap", "radio",
        "channels", "local", "web", "limits", "debug", "recovery",
    ):
        sect = baseline.get(section)
        if isinstance(sect, dict):
            sect.pop("db_path", None)


class _PostWriteValidationError(Exception):
    """Sentinel: rendered tempfile failed load_config; live file untouched."""


def _write_validated(
    config_path: Path,
    tier1: dict[str, Any],
    baseline: dict[str, Any],
) -> None:
    """Atomic write + tempfile validation, in that order.

    The live config.toml is replaced only after load_config accepts the
    rendered tempfile. On validation failure, the live file is untouched
    and the tempfile is removed. Raises _PostWriteValidationError on
    validation failure, OSError on any I/O problem.
    """
    _apply_tier1(baseline, tier1)
    payload = tomli_w.dumps(baseline)

    config_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = tempfile.NamedTemporaryFile(
        dir=config_path.parent,
        prefix=".config.toml.",
        suffix=".tmp",
        delete=False,
        mode="w",
        encoding="utf-8",
    )
    tmp_path = Path(tmp.name)
    try:
        tmp.write(payload)
        tmp.flush()
        os.fsync(tmp.fileno())
        tmp.close()
        try:
            load_config(str(tmp_path))
        except (ValueError, KeyError) as e:
            raise _PostWriteValidationError(str(e)) from e
        os.replace(tmp_path, config_path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise


# --------------------------------------------------------------- next steps


def _print_next_steps(
    config_path: Path, mode: str, *, explicit_config: bool,
) -> None:
    print(f"\nWrote {config_path}")
    # If the user invoked configure with --config, suggested follow-ups
    # that read the config need the same flag — otherwise apply silently
    # switches to the default path. Bare invocation prints bare commands
    # so the common case stays terse. promote ships code only (via
    # `git archive main`) and never reads the config, so the flag is
    # NOT propagated to the promote line.
    cfg_flag = f" --config={shlex.quote(str(config_path))}" if explicit_config else ""
    if mode == "prod":
        print(f"\nNext: sudo civicmesh{cfg_flag} apply")
    else:
        print("\nNext:")
        print(f"  uv run civicmesh{cfg_flag} apply --dry-run    # preview the system files this config would render")
        print( "  uv run civicmesh promote --from .   # ship code to prod (config is not carried)")
        print( "                                      # if the schema changed, also run")
        print( "                                      # `sudo -u civicmesh civicmesh configure`")
        print( "                                      # on each prod node")


# ------------------------------------------------------------------- entry


def run_configure(
    config_path: Path, mode: str, *, explicit_config: bool = False,
) -> int:
    try:
        try:
            baseline = _load_baseline(config_path)
        except tomllib.TOMLDecodeError as e:
            print(
                f"civicmesh: configure: failed to parse {config_path}: {e}",
                file=sys.stderr,
            )
            return 1
        except OSError as e:
            print(f"civicmesh: configure: {e}", file=sys.stderr)
            return 1
        tier1 = _walk_prompts(baseline)
        if not _confirm_write(config_path, tier1):
            print("Aborted, no changes made.", file=sys.stderr)
            return 3
        try:
            _write_validated(config_path, tier1, baseline)
        except _PostWriteValidationError as e:
            print(f"civicmesh: configure: post-write validation failed: {e}", file=sys.stderr)
            return 2
        except OSError as e:
            print(f"civicmesh: configure: {e}", file=sys.stderr)
            return 1
        _print_next_steps(config_path, mode, explicit_config=explicit_config)
        return 0
    except KeyboardInterrupt:
        print("\nAborted, no changes made.", file=sys.stderr)
        return 3
