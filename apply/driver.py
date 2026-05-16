"""apply driver: plan -> diff -> write.

Two pure-data classes (FileChange, Plan) plus three top-level functions
(plan, print_plan, apply_plan). The renderers are pure; this module
adds I/O (read /etc/, write tempfiles, os.replace).

`etc_root` lets tests target a tempdir instead of `/`. Renderers know
the absolute target (e.g. /etc/hostapd/hostapd.conf); driver maps
that to `etc_root / abs_path.relative_to("/")` for both read and
write.

Orphan-file caveat: when `network.iface` changes (e.g. wlan0 -> wlan1),
the new networkd / NM files appear as FileChanges, but the old
`20-wlan0-ap.network` and `99-unmanaged-wlan0.conf` are left on disk.
Cleanup is out of CIV-62 scope (see docs/civicmesh-tool.md § apply).
"""

from __future__ import annotations

import difflib
import os
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from config import AppConfig

from . import renderers, restart


@dataclass(frozen=True)
class FileChange:
    abs_path: Path        # absolute target, e.g. /etc/hostapd/hostapd.conf
    new_bytes: bytes
    old_bytes: bytes | None
    mode: int


@dataclass(frozen=True)
class Plan:
    changes: tuple[FileChange, ...]   # sorted by abs_path
    services: tuple[str, ...]         # currently unused; reserved for future


# (renderer, path-fn) pairs. path-fn takes the AppConfig because
# networkd / NM filenames embed cfg.network.iface.
_RENDER_TARGETS: list[tuple[Callable[[AppConfig], bytes], Callable[[AppConfig], Path]]] = [
    (renderers.render_hostapd_conf,
     lambda c: Path("/etc/hostapd/hostapd.conf")),
    (renderers.render_hostapd_default,
     lambda c: Path("/etc/default/hostapd")),
    (renderers.render_dnsmasq_conf,
     lambda c: Path("/etc/dnsmasq.d/civicmesh.conf")),
    (renderers.render_networkd_conf,
     lambda c: Path(f"/etc/systemd/network/20-{c.network.iface}-ap.network")),
    (renderers.render_nm_unmanaged_conf,
     lambda c: Path(f"/etc/NetworkManager/conf.d/99-unmanaged-{c.network.iface}.conf")),
    (renderers.render_nftables_conf,
     lambda c: Path("/etc/nftables.conf")),
    (renderers.render_sysctl_conf,
     lambda c: Path("/etc/sysctl.d/90-civicmesh-disable-ipv6.conf")),
    (renderers.render_systemd_unit_web,
     lambda c: Path("/etc/systemd/system/civicmesh-web.service")),
    (renderers.render_systemd_unit_mesh,
     lambda c: Path("/etc/systemd/system/civicmesh-mesh.service")),
]


def _on_disk_path(abs_path: Path, etc_root: Path) -> Path:
    return etc_root / abs_path.relative_to("/")


def plan(cfg: AppConfig, etc_root: Path = Path("/")) -> Plan:
    """Render all targets, byte-compare against on-disk, return drifted set."""
    changes: list[FileChange] = []
    for render_fn, path_fn in _RENDER_TARGETS:
        abs_path = path_fn(cfg)
        on_disk = _on_disk_path(abs_path, etc_root)
        new_bytes = render_fn(cfg)
        if on_disk.is_file():
            old_bytes: bytes | None = on_disk.read_bytes()
        else:
            old_bytes = None
        if new_bytes != old_bytes:
            changes.append(FileChange(
                abs_path=abs_path,
                new_bytes=new_bytes,
                old_bytes=old_bytes,
                mode=renderers.DEFAULT_FILE_MODE,
            ))
    # Sort by abs_path. Note: this does NOT clean up orphans from a
    # previous iface — a wlan0 -> wlan1 change leaves the old wlan0
    # networkd / NM files on disk. See module docstring.
    changes.sort(key=lambda c: c.abs_path)
    return Plan(changes=tuple(changes), services=())


def print_plan(plan: Plan, *, dry_run: bool) -> None:
    """Emit unified diffs for each FileChange, then list services to restart."""
    if not plan.changes:
        print("apply: no changes (config matches /etc/)")
        return

    for change in plan.changes:
        if change.old_bytes is None:
            old_lines: list[str] = []
            fromfile = "/dev/null"
        else:
            old_lines = change.old_bytes.decode("utf-8", errors="replace").splitlines(keepends=True)
            fromfile = str(change.abs_path)
        new_lines = change.new_bytes.decode("utf-8", errors="replace").splitlines(keepends=True)
        diff = difflib.unified_diff(
            old_lines, new_lines,
            fromfile=fromfile,
            tofile=str(change.abs_path),
            lineterm="",
        )
        sys.stdout.writelines(diff)
        sys.stdout.write("\n")

    actions = restart.derive_actions(c.abs_path for c in plan.changes)
    if actions:
        verb = "would restart" if dry_run else "restart"
        print(f"\n{verb}:")
        for argv in actions:
            print(f"  $ {' '.join(argv)}")


def apply_plan(plan: Plan, etc_root: Path = Path("/")) -> None:
    """Write each FileChange atomically (tempfile + chmod + os.replace).

    Fail-fast: any I/O error bubbles up. Already-replaced files stay in
    place; the apply CLI maps an exception here to exit 4 with no
    automatic rollback (see docs/civicmesh-tool.md § apply, exit-code
    table).
    """
    for change in plan.changes:
        target = _on_disk_path(change.abs_path, etc_root)
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            dir=target.parent, prefix=".apply.", suffix=".tmp"
        )
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(change.new_bytes)
                f.flush()
                os.fsync(f.fileno())
            os.chmod(tmp_path, change.mode)
            os.replace(tmp_path, target)
        except BaseException:
            tmp_path.unlink(missing_ok=True)
            raise
