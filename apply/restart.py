"""App-tier service restart, derived from changed paths.

Each entry in SERVICE_ACTIONS is (matcher, argv, sort_key). The matcher
is a function that takes an absolute Path and returns True/False.
derive_actions walks the changed paths once, collects the matching
sort_keys, and returns argv lists in sort_key order, deduped.

Scope: only the **app-tier** services (`civicmesh-web`, `civicmesh-mesh`).
The system-stack services (`hostapd`, `dnsmasq`, `nftables`,
`systemd-networkd`, `NetworkManager`, `sysctl`) are deliberately not
restarted in-place by apply — they are staged for next boot via
`systemctl enable` and the operator-issued reboot is the cutover. See
`civicmesh.py:_cmd_apply` for the staging order, and
`docs/civicmesh-tool.md` for the rationale (avoiding the headless-WiFi
trapdoor: an in-place hostapd restart while the operator is on SSH over
wlan0 would drop the session).
"""

from __future__ import annotations

import fnmatch
import shlex
import subprocess
from pathlib import Path
from typing import Callable, Iterable


def _matches(pattern: str) -> Callable[[Path], bool]:
    """Path-glob matcher (fnmatch-style)."""
    def matcher(p: Path) -> bool:
        return fnmatch.fnmatch(str(p), pattern)
    return matcher


# (matcher, argv, sort_key)
SERVICE_ACTIONS: list[tuple[Callable[[Path], bool], list[str], int]] = [
    (_matches("/etc/systemd/system/civicmesh-*.service"),
     ["systemctl", "restart", "civicmesh-web", "civicmesh-mesh"], 0),
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
