"""End-to-end ordering tests for civicmesh._cmd_apply.

The post-write systemctl sequence is the cutover-critical path: an
out-of-order step (e.g. starting hostapd before disabling wpa_supplicant)
reintroduces the headless-WiFi trapdoor. These tests pin the expected
ordering by recording subprocess.run calls and asserting on the argv
sequence.
"""

import argparse
import unittest
from pathlib import Path
from unittest.mock import patch

import civicmesh
from apply.driver import FileChange, Plan


_MINIMAL_CONFIG = (
    Path(__file__).resolve().parent / "apply" / "goldens" / "minimal-config.toml"
)


def _change(abs_path: str) -> FileChange:
    return FileChange(
        abs_path=Path(abs_path),
        new_bytes=b"# stub",
        old_bytes=None,
        mode=0o644,
    )


def _stub_plan() -> Plan:
    """Plan with a civicmesh-*.service change so the app-restart fires."""
    return Plan(
        changes=(
            _change("/etc/hostapd/hostapd.conf"),
            _change("/etc/systemd/system/civicmesh-web.service"),
        ),
        services=(),
    )


def _ns(*, dry_run: bool = False, no_restart: bool = False) -> argparse.Namespace:
    return argparse.Namespace(
        config=str(_MINIMAL_CONFIG),
        dry_run=dry_run,
        no_restart=no_restart,
    )


class ApplyOrderingTest(unittest.TestCase):

    def test_runs_systemctl_in_correct_order(self) -> None:
        with patch("civicmesh._MODE", "prod"), \
             patch("civicmesh.os.geteuid", return_value=0), \
             patch("apply.driver.plan", return_value=_stub_plan()), \
             patch("apply.driver.apply_plan"), \
             patch("apply.validate.validate_plan", return_value=[]), \
             patch("subprocess.run") as mock_run:
            with self.assertRaises(SystemExit) as ctx:
                civicmesh._cmd_apply(_ns())
        self.assertEqual(ctx.exception.code, 0)
        argvs = [c[0][0] for c in mock_run.call_args_list]
        self.assertEqual(argvs, [
            ["systemctl", "daemon-reload"],
            ["systemctl", "unmask", "hostapd.service", "dnsmasq.service"],
            ["systemctl", "enable", "hostapd", "dnsmasq", "nftables",
             "rfkill-unblock-wifi", "systemd-networkd"],
            ["systemctl", "disable", "wpa_supplicant.service"],
            ["systemctl", "enable", "civicmesh-web", "civicmesh-mesh"],
            ["systemctl", "restart", "civicmesh-web", "civicmesh-mesh"],
        ])

    def test_skips_app_restart_when_no_civicmesh_unit_changed(self) -> None:
        """If no civicmesh-*.service file changed, the app restart is
        omitted — but the cutover-staging steps (including the
        unconditional `enable civicmesh-web civicmesh-mesh`) still fire."""
        plan = Plan(
            changes=(_change("/etc/hostapd/hostapd.conf"),),
            services=(),
        )
        with patch("civicmesh._MODE", "prod"), \
             patch("civicmesh.os.geteuid", return_value=0), \
             patch("apply.driver.plan", return_value=plan), \
             patch("apply.driver.apply_plan"), \
             patch("apply.validate.validate_plan", return_value=[]), \
             patch("subprocess.run") as mock_run:
            with self.assertRaises(SystemExit) as ctx:
                civicmesh._cmd_apply(_ns())
        self.assertEqual(ctx.exception.code, 0)
        argvs = [c[0][0] for c in mock_run.call_args_list]
        self.assertEqual(argvs, [
            ["systemctl", "daemon-reload"],
            ["systemctl", "unmask", "hostapd.service", "dnsmasq.service"],
            ["systemctl", "enable", "hostapd", "dnsmasq", "nftables",
             "rfkill-unblock-wifi", "systemd-networkd"],
            ["systemctl", "disable", "wpa_supplicant.service"],
            ["systemctl", "enable", "civicmesh-web", "civicmesh-mesh"],
        ])

    def test_short_circuits_on_validation_failure(self) -> None:
        with patch("civicmesh._MODE", "prod"), \
             patch("civicmesh.os.geteuid", return_value=0), \
             patch("apply.driver.plan", return_value=_stub_plan()), \
             patch("apply.driver.apply_plan") as mock_apply, \
             patch("apply.validate.validate_plan",
                   return_value=["hostapd: syntax error"]), \
             patch("subprocess.run") as mock_run:
            with self.assertRaises(SystemExit) as ctx:
                civicmesh._cmd_apply(_ns())
        self.assertEqual(ctx.exception.code, 6)
        mock_apply.assert_not_called()
        mock_run.assert_not_called()

    def test_no_restart_skips_all_systemctl(self) -> None:
        with patch("civicmesh._MODE", "prod"), \
             patch("civicmesh.os.geteuid", return_value=0), \
             patch("apply.driver.plan", return_value=_stub_plan()), \
             patch("apply.driver.apply_plan") as mock_apply, \
             patch("apply.validate.validate_plan", return_value=[]), \
             patch("subprocess.run") as mock_run:
            with self.assertRaises(SystemExit) as ctx:
                civicmesh._cmd_apply(_ns(no_restart=True))
        self.assertEqual(ctx.exception.code, 0)
        mock_apply.assert_called_once()
        mock_run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
