"""Renderer tests: byte-compare each render_* function's output against
its golden file under tests/apply/goldens/minimal/.

If you intentionally change a renderer's output, regenerate the goldens
with `uv run python -m tests.apply.regenerate_goldens` and review the
resulting `git diff tests/apply/goldens/` before committing.
"""

import difflib
import unittest
from pathlib import Path

from apply import renderers
from config import load_config


GOLDEN_DIR = Path(__file__).resolve().parent / "goldens" / "minimal"
MINIMAL_CONFIG = Path(__file__).resolve().parent / "goldens" / "minimal-config.toml"


def _diff_msg(name: str, expected: bytes, actual: bytes) -> str:
    diff = "".join(difflib.unified_diff(
        expected.decode("utf-8", errors="replace").splitlines(keepends=True),
        actual.decode("utf-8", errors="replace").splitlines(keepends=True),
        fromfile=f"goldens/minimal/{name}",
        tofile=f"render_<{name}>(cfg)",
        lineterm="",
    ))
    return f"{name} drifted from golden:\n{diff}"


class RendererGoldenTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.cfg = load_config(str(MINIMAL_CONFIG))

    def _check(self, render_fn, name: str) -> None:
        actual = render_fn(self.cfg)
        golden = (GOLDEN_DIR / name).read_bytes()
        if actual != golden:
            self.fail(_diff_msg(name, golden, actual))

    def test_hostapd_conf(self) -> None:
        self._check(renderers.render_hostapd_conf, "hostapd.conf")

    def test_hostapd_default(self) -> None:
        self._check(renderers.render_hostapd_default, "hostapd.default")

    def test_dnsmasq_conf(self) -> None:
        self._check(renderers.render_dnsmasq_conf, "dnsmasq.conf")

    def test_networkd_conf(self) -> None:
        self._check(renderers.render_networkd_conf, "20-wlan0-ap.network")

    def test_nm_unmanaged_conf(self) -> None:
        self._check(renderers.render_nm_unmanaged_conf, "99-unmanaged-wlan0.conf")

    def test_nftables_conf(self) -> None:
        self._check(renderers.render_nftables_conf, "nftables.conf")

    def test_sysctl_conf(self) -> None:
        self._check(renderers.render_sysctl_conf, "sysctl.conf")

    def test_systemd_unit_web(self) -> None:
        self._check(renderers.render_systemd_unit_web, "civicmesh-web.service")

    def test_systemd_unit_mesh(self) -> None:
        self._check(renderers.render_systemd_unit_mesh, "civicmesh-mesh.service")


class NftablesInvariantTest(unittest.TestCase):
    """Cheap insurance: even if a future renderer edit drops `flush ruleset`,
    this test fires before the kernel quietly merges into existing rules."""

    def test_flush_ruleset_is_first_directive(self) -> None:
        cfg = load_config(str(MINIMAL_CONFIG))
        out = renderers.render_nftables_conf(cfg).decode()
        body = [
            line for line in out.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        self.assertGreater(len(body), 0, "nftables output is empty?")
        self.assertEqual(
            body[0],
            "flush ruleset",
            f"first non-comment directive must be 'flush ruleset', got {body[0]!r}",
        )


if __name__ == "__main__":
    unittest.main()
