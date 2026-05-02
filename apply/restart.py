"""Service-restart actions, derived from changed paths.

Each entry in SERVICE_ACTIONS is (matcher, argv, sort_key). The matcher
is a function that takes an absolute Path and returns True/False.
derive_actions walks the changed paths once, collects the matching
sort_keys, and returns argv lists in sort_key order, deduped.

Order (per the design doc):
  0  systemctl restart systemd-networkd
  1  systemctl reload NetworkManager
  2  sysctl --system
  3  nft -f /etc/nftables.conf
  4  systemctl restart hostapd
  5  systemctl restart dnsmasq
  6  systemctl daemon-reload
  7  systemctl restart civicmesh-web civicmesh-mesh

Sort keys 6 and 7 fire together when any civicmesh-*.service file
changes (daemon-reload picks up unit edits before the unit restart).

Rationale: re-establish L2/L3 (networkd, NM) first, then sysctl, then
the firewall, then radio AP / DHCP, then app services last so they
come up against a settled stack.
"""

from __future__ import annotations

import fnmatch
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Callable, Iterable


def _matches(pattern: str) -> Callable[[Path], bool]:
    """Path-glob matcher (fnmatch-style)."""
    def matcher(p: Path) -> bool:
        return fnmatch.fnmatch(str(p), pattern)
    return matcher


# (matcher, argv, sort_key)
SERVICE_ACTIONS: list[tuple[Callable[[Path], bool], list[str], int]] = [
    (_matches("/etc/systemd/network/20-*-ap.network"),
     ["systemctl", "restart", "systemd-networkd"], 0),
    (_matches("/etc/NetworkManager/conf.d/99-unmanaged-*.conf"),
     ["systemctl", "reload", "NetworkManager"], 1),
    (_matches("/etc/sysctl.d/90-civicmesh-disable-ipv6.conf"),
     ["sysctl", "--system"], 2),
    (_matches("/etc/nftables.conf"),
     ["nft", "-f", "/etc/nftables.conf"], 3),
    (_matches("/etc/hostapd/hostapd.conf"),
     ["systemctl", "restart", "hostapd"], 4),
    (_matches("/etc/default/hostapd"),
     ["systemctl", "restart", "hostapd"], 4),
    (_matches("/etc/dnsmasq.d/civicmesh.conf"),
     ["systemctl", "restart", "dnsmasq"], 5),
    (_matches("/etc/systemd/system/civicmesh-*.service"),
     ["systemctl", "daemon-reload"], 6),
    (_matches("/etc/systemd/system/civicmesh-*.service"),
     ["systemctl", "restart", "civicmesh-web", "civicmesh-mesh"], 7),
]


def derive_actions(changed_paths: Iterable[Path]) -> list[list[str]]:
    """Return argv lists in sort-key order, deduped by argv."""
    paths = list(changed_paths)
    matched: dict[int, list[str]] = {}
    for matcher, argv, key in SERVICE_ACTIONS:
        if key in matched:
            continue
        if any(matcher(p) for p in paths):
            matched[key] = argv
    return [matched[k] for k in sorted(matched)]


def run_actions(actions: list[list[str]]) -> None:
    """Invoke each action via subprocess.run(check=True). Print each on success."""
    for argv in actions:
        subprocess.run(argv, check=True)
        print(f"restarted: {shlex.join(argv)}")
