"""Pre-flight validation for civicmesh apply.

Runs each rendered config through its native syntax checker before any
writes hit /etc/. Failures short-circuit apply; no filesystem or systemd
state is touched on validator failure.

Validators run against tempfile copies of the rendered bytes — never the
target paths in /etc/ — so a failed validation can't leave a half-written
config behind.
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

from config import AppConfig

from .driver import Plan


_VALIDATORS: dict[str, list[str]] = {
    "/etc/hostapd/hostapd.conf": ["hostapd", "-t"],
    "/etc/dnsmasq.d/civicmesh.conf": ["dnsmasq", "--test", "--conf-file="],
    "/etc/nftables.conf": ["nft", "-c", "-f"],
}


def _iface_exists(iface: str) -> bool:
    return (Path("/sys/class/net") / iface).is_dir()


def validate_plan(plan: Plan, cfg: AppConfig) -> list[str]:
    """Return a list of validation error strings; empty list means OK to apply."""
    errors: list[str] = []

    if not _iface_exists(cfg.network.iface):
        errors.append(
            f"network.iface {cfg.network.iface!r} not present in "
            "/sys/class/net (radio/USB issue or wrong iface name in config)"
        )

    with tempfile.TemporaryDirectory(prefix="civicmesh-validate-") as tmp:
        tmpdir = Path(tmp)
        for change in plan.changes:
            argv_template = _VALIDATORS.get(str(change.abs_path))
            if argv_template is None:
                continue
            tmpfile = tmpdir / change.abs_path.name
            tmpfile.write_bytes(change.new_bytes)
            argv = _build_argv(argv_template, tmpfile)
            errors.extend(_run_one(argv, str(change.abs_path)))

    return errors


def _build_argv(template: list[str], tmpfile: Path) -> list[str]:
    # `dnsmasq --test --conf-file=` needs the path glued onto the trailing
    # `=`; the others append the path as a separate argv element.
    if template[-1].endswith("="):
        return [*template[:-1], template[-1] + str(tmpfile)]
    return [*template, str(tmpfile)]


def _run_one(argv: list[str], target: str) -> list[str]:
    try:
        rc = subprocess.run(argv, capture_output=True, check=False)
    except FileNotFoundError:
        return [f"{target}: validator missing: {argv[0]!r}"]
    if rc.returncode == 0:
        return []
    msg = (rc.stderr or rc.stdout or b"").decode("utf-8", errors="replace").strip()
    return [f"{target}: {argv[0]} failed: {msg}"]
