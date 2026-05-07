"""Tests for apply.driver: plan(), apply_plan(), print_plan()."""

import dataclasses
import io
import os
import re
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from apply import driver
from apply.driver import FileChange, Plan
from apply import renderers
from config import load_config


GOLDEN_DIR = Path(__file__).resolve().parent / "goldens" / "minimal"
MINIMAL_CONFIG = Path(__file__).resolve().parent / "goldens" / "minimal-config.toml"


def _populate_etc(etc_root: Path, cfg) -> None:
    """Write every renderer's output into <etc_root>/etc/ at the correct path."""
    for render_fn, path_fn in driver._RENDER_TARGETS:
        abs_path = path_fn(cfg)
        target = etc_root / abs_path.relative_to("/")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(render_fn(cfg))


class PlanTest(unittest.TestCase):

    def setUp(self) -> None:
        self.cfg = load_config(str(MINIMAL_CONFIG))

    def _swap_iface(self, new: str):
        return dataclasses.replace(
            self.cfg,
            network=dataclasses.replace(self.cfg.network, iface=new),
        )

    def _swap_ap_channel(self, new: int):
        return dataclasses.replace(
            self.cfg,
            ap=dataclasses.replace(self.cfg.ap, channel=new),
        )

    def _swap_site_name(self, new: str):
        return dataclasses.replace(
            self.cfg,
            node=dataclasses.replace(self.cfg.node, site_name=new),
        )

    def test_empty_plan_when_etc_matches(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            etc_root = Path(td)
            _populate_etc(etc_root, self.cfg)
            plan = driver.plan(self.cfg, etc_root=etc_root)
            self.assertEqual(plan.changes, ())

    def test_iface_change_invalidates_iface_dependent_files(self) -> None:
        """wlan0 -> wlan1 invalidates the six iface-dependent files.

        Out of scope for CIV-62: orphan cleanup. The old wlan0 networkd
        and NM files stay on disk; the test only verifies the new files
        appear as changes."""
        with tempfile.TemporaryDirectory() as td:
            etc_root = Path(td)
            _populate_etc(etc_root, self.cfg)
            new_cfg = self._swap_iface("wlan1")
            plan = driver.plan(new_cfg, etc_root=etc_root)
            changed = {c.abs_path for c in plan.changes}
            expected = {
                Path("/etc/hostapd/hostapd.conf"),
                Path("/etc/dnsmasq.d/civicmesh.conf"),
                Path("/etc/systemd/network/20-wlan1-ap.network"),
                Path("/etc/NetworkManager/conf.d/99-unmanaged-wlan1.conf"),
                Path("/etc/nftables.conf"),
                Path("/etc/sysctl.d/90-civicmesh-disable-ipv6.conf"),
            }
            self.assertEqual(changed, expected)

    def test_channel_change_invalidates_only_hostapd_conf(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            etc_root = Path(td)
            _populate_etc(etc_root, self.cfg)
            new_cfg = self._swap_ap_channel(11)
            plan = driver.plan(new_cfg, etc_root=etc_root)
            changed = {c.abs_path for c in plan.changes}
            self.assertEqual(changed, {Path("/etc/hostapd/hostapd.conf")})

    def test_site_name_change_invalidates_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            etc_root = Path(td)
            _populate_etc(etc_root, self.cfg)
            new_cfg = self._swap_site_name("DifferentName")
            plan = driver.plan(new_cfg, etc_root=etc_root)
            self.assertEqual(plan.changes, ())


class ApplyPlanTest(unittest.TestCase):

    def test_apply_plan_atomic_writes_files_and_modes(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            etc_root = Path(td)
            plan = Plan(changes=(
                FileChange(
                    abs_path=Path("/etc/hostapd/hostapd.conf"),
                    new_bytes=b"hostapd-content\n",
                    old_bytes=None,
                    mode=0o644,
                ),
                FileChange(
                    abs_path=Path("/etc/sysctl.d/90-civicmesh-disable-ipv6.conf"),
                    new_bytes=b"net.ipv6.conf.test.disable_ipv6 = 1\n",
                    old_bytes=None,
                    mode=0o644,
                ),
            ), services=())
            driver.apply_plan(plan, etc_root=etc_root)
            for change in plan.changes:
                target = etc_root / change.abs_path.relative_to("/")
                self.assertTrue(target.is_file(), f"missing: {target}")
                self.assertEqual(target.read_bytes(), change.new_bytes)
                self.assertEqual(
                    target.stat().st_mode & 0o777,
                    0o644,
                    f"wrong mode for {target}",
                )

    def test_apply_plan_leaves_no_tempfile_on_failure(self) -> None:
        # Force failure by pointing into a non-writable parent we can't create.
        # Using a dangling symlink as the parent exercises the OSError path.
        with tempfile.TemporaryDirectory() as td:
            etc_root = Path(td)
            target_dir = etc_root / "etc/sysctl.d"
            target_dir.mkdir(parents=True)
            # Make the parent dir read-only after a successful first write.
            target = etc_root / "etc/sysctl.d/90-civicmesh-disable-ipv6.conf"
            plan = Plan(changes=(
                FileChange(
                    abs_path=Path("/etc/sysctl.d/90-civicmesh-disable-ipv6.conf"),
                    new_bytes=b"x\n",
                    old_bytes=None,
                    mode=0o644,
                ),
            ), services=())
            os.chmod(target_dir, 0o555)
            try:
                with self.assertRaises(OSError):
                    driver.apply_plan(plan, etc_root=etc_root)
                # Ensure no leftover tempfile in the now-readonly directory.
                # (mkstemp would have failed; the dir should still be clean.)
                self.assertEqual(
                    list(target_dir.glob(".apply.*.tmp")),
                    [],
                )
            finally:
                os.chmod(target_dir, 0o755)


class PrintPlanTest(unittest.TestCase):

    def test_emits_line_oriented_diff(self) -> None:
        """Regression: difflib.unified_diff must receive lists of lines,
        not strings. Passing a string makes it iterate per-character."""
        plan = Plan(changes=(FileChange(
            abs_path=Path("/etc/foo.conf"),
            new_bytes=b"line one\nline TWO\nline three\n",
            old_bytes=b"line one\nline two\nline three\n",
            mode=0o644,
        ),), services=())
        with redirect_stdout(io.StringIO()) as out:
            driver.print_plan(plan, dry_run=True)
        text = out.getvalue()
        # Exactly one '-line two' line and one '+line TWO' line.
        self.assertIn("-line two", text)
        self.assertIn("+line TWO", text)
        # Sanity: no per-character markers.
        per_char = re.findall(r"^[+-]\w$", text, flags=re.MULTILINE)
        self.assertEqual(per_char, [], f"per-character diff detected: {per_char}")

    def test_emits_no_changes_message_when_plan_empty(self) -> None:
        plan = Plan(changes=(), services=())
        with redirect_stdout(io.StringIO()) as out:
            driver.print_plan(plan, dry_run=True)
        self.assertEqual(out.getvalue().strip(), "apply: no changes (config matches /etc/)")


if __name__ == "__main__":
    unittest.main()
