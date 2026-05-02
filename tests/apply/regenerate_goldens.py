"""Regenerate golden files from tests/apply/goldens/minimal-config.toml.

Run via:

    uv run python -m tests.apply.regenerate_goldens

Use this when you intentionally change a renderer's output and need to
update the goldens. Review the resulting `git diff tests/apply/goldens/`
before committing — that's the diff a future operator will see in
`apply --dry-run` for the equivalent config change.
"""

from __future__ import annotations

from pathlib import Path

from apply import renderers
from config import load_config


_GOLDEN_DIR = Path(__file__).resolve().parent / "goldens"
_MINIMAL_DIR = _GOLDEN_DIR / "minimal"
_MINIMAL_CONFIG = _GOLDEN_DIR / "minimal-config.toml"


# (renderer, golden filename). Matches the layout in the plan.
_TARGETS: list[tuple] = [
    (renderers.render_hostapd_conf, "hostapd.conf"),
    (renderers.render_hostapd_default, "hostapd.default"),
    (renderers.render_dnsmasq_conf, "dnsmasq.conf"),
    (renderers.render_networkd_conf, "20-wlan0-ap.network"),
    (renderers.render_nm_unmanaged_conf, "99-unmanaged-wlan0.conf"),
    (renderers.render_nftables_conf, "nftables.conf"),
    (renderers.render_sysctl_conf, "sysctl.conf"),
    (renderers.render_systemd_unit_web, "civicmesh-web.service"),
    (renderers.render_systemd_unit_mesh, "civicmesh-mesh.service"),
]


def main() -> None:
    cfg = load_config(str(_MINIMAL_CONFIG))
    _MINIMAL_DIR.mkdir(parents=True, exist_ok=True)
    for render_fn, name in _TARGETS:
        path = _MINIMAL_DIR / name
        path.write_bytes(render_fn(cfg))
        print(f"wrote {path}")


if __name__ == "__main__":
    main()
