"""Tests for apply.restart: derive_actions() and run_actions().

apply.restart now only fires the app-tier restart (civicmesh-{web,mesh}).
System-stack services (hostapd, dnsmasq, nftables, networkd,
NetworkManager, sysctl) are no longer restarted in-place — they are
enabled-for-boot from civicmesh.py:_cmd_apply and the operator-issued
reboot is the cutover. See the module docstring for the rationale.
"""

import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch, call

from apply import restart


class DeriveActionsTest(unittest.TestCase):

    def test_civicmesh_unit_change_yields_app_restart(self) -> None:
        """A change to either civicmesh-*.service unit triggers the app restart."""
        for path in (
            "/etc/systemd/system/civicmesh-web.service",
            "/etc/systemd/system/civicmesh-mesh.service",
        ):
            with self.subTest(path=path):
                self.assertEqual(
                    restart.derive_actions([Path(path)]),
                    [["systemctl", "restart", "civicmesh-web", "civicmesh-mesh"]],
                )

    def test_both_civicmesh_unit_changes_dedupe_to_one_restart(self) -> None:
        paths = [
            Path("/etc/systemd/system/civicmesh-web.service"),
            Path("/etc/systemd/system/civicmesh-mesh.service"),
        ]
        self.assertEqual(
            restart.derive_actions(paths),
            [["systemctl", "restart", "civicmesh-web", "civicmesh-mesh"]],
        )

    def test_system_stack_paths_yield_no_actions(self) -> None:
        """hostapd / dnsmasq / nftables / networkd / NM / sysctl paths no
        longer trigger any in-place restart — they are staged for boot."""
        paths = [
            Path("/etc/hostapd/hostapd.conf"),
            Path("/etc/default/hostapd"),
            Path("/etc/dnsmasq.d/civicmesh.conf"),
            Path("/etc/systemd/network/20-wlan0-ap.network"),
            Path("/etc/NetworkManager/conf.d/99-unmanaged-wlan0.conf"),
            Path("/etc/nftables.conf"),
            Path("/etc/sysctl.d/90-civicmesh-disable-ipv6.conf"),
        ]
        self.assertEqual(restart.derive_actions(paths), [])

    def test_mixed_input_returns_only_app_restart(self) -> None:
        paths = [
            Path("/etc/nftables.conf"),
            Path("/etc/hostapd/hostapd.conf"),
            Path("/etc/systemd/system/civicmesh-web.service"),
        ]
        self.assertEqual(
            restart.derive_actions(paths),
            [["systemctl", "restart", "civicmesh-web", "civicmesh-mesh"]],
        )

    def test_empty_input_yields_no_actions(self) -> None:
        self.assertEqual(restart.derive_actions([]), [])


class RunActionsTest(unittest.TestCase):

    def test_invokes_subprocess_in_order(self) -> None:
        actions = [
            ["systemctl", "restart", "civicmesh-web", "civicmesh-mesh"],
        ]
        with patch("apply.restart.subprocess.run") as mock_run:
            restart.run_actions(actions)
        self.assertEqual(mock_run.call_args_list, [
            call(actions[0], check=True),
        ])

    def test_raises_on_failure(self) -> None:
        actions = [
            ["systemctl", "restart", "civicmesh-web", "civicmesh-mesh"],
        ]
        with patch("apply.restart.subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.CalledProcessError(1, actions[0])
            with self.assertRaises(subprocess.CalledProcessError):
                restart.run_actions(actions)


if __name__ == "__main__":
    unittest.main()
