"""Tests for apply.restart: derive_actions() and run_actions().

Manual integration check for atomicity (not automated; documented for
operators):

    # On a real Pi with nftables loaded:
    nft list ruleset > /tmp/before
    sudo civicmesh apply
    nft list ruleset > /tmp/after
    diff /tmp/before /tmp/after

The `flush ruleset` directive in /etc/nftables.conf makes `nft -f` a
ruleset swap rather than a merge. The before/after diff confirms the
swap landed atomically — no in-between state where partial rules are
loaded.
"""

import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch, call

from apply import restart


class DeriveActionsTest(unittest.TestCase):

    def test_dedupes_and_orders_full_set(self) -> None:
        """Every category fires exactly once, in spec order."""
        paths = [
            Path("/etc/hostapd/hostapd.conf"),
            Path("/etc/default/hostapd"),       # same action as hostapd.conf
            Path("/etc/dnsmasq.d/civicmesh.conf"),
            Path("/etc/systemd/network/20-wlan0-ap.network"),
            Path("/etc/NetworkManager/conf.d/99-unmanaged-wlan0.conf"),
            Path("/etc/nftables.conf"),
            Path("/etc/sysctl.d/90-civicmesh-disable-ipv6.conf"),
            Path("/etc/systemd/system/civicmesh-web.service"),
            Path("/etc/systemd/system/civicmesh-mesh.service"),
        ]
        actions = restart.derive_actions(paths)
        self.assertEqual(actions, [
            ["systemctl", "restart", "systemd-networkd"],
            ["systemctl", "reload", "NetworkManager"],
            ["sysctl", "--system"],
            ["nft", "-f", "/etc/nftables.conf"],
            ["systemctl", "restart", "hostapd"],
            ["systemctl", "restart", "dnsmasq"],
            ["systemctl", "daemon-reload"],
            ["systemctl", "restart", "civicmesh-web", "civicmesh-mesh"],
        ])

    def test_subset_returns_only_matching_actions(self) -> None:
        """Only nftables and hostapd changed -> only those two restart actions."""
        paths = [
            Path("/etc/nftables.conf"),
            Path("/etc/hostapd/hostapd.conf"),
        ]
        self.assertEqual(restart.derive_actions(paths), [
            ["nft", "-f", "/etc/nftables.conf"],
            ["systemctl", "restart", "hostapd"],
        ])

    def test_systemd_unit_change_emits_one_daemon_reload_and_one_restart(self) -> None:
        """Both civicmesh-*.service files map to the same daemon-reload and
        the same restart command — the dedup must collapse them."""
        paths = [
            Path("/etc/systemd/system/civicmesh-web.service"),
            Path("/etc/systemd/system/civicmesh-mesh.service"),
        ]
        self.assertEqual(restart.derive_actions(paths), [
            ["systemctl", "daemon-reload"],
            ["systemctl", "restart", "civicmesh-web", "civicmesh-mesh"],
        ])

    def test_empty_input_yields_no_actions(self) -> None:
        self.assertEqual(restart.derive_actions([]), [])


class RunActionsTest(unittest.TestCase):

    def test_invokes_subprocess_in_order(self) -> None:
        actions = [
            ["systemctl", "restart", "systemd-networkd"],
            ["nft", "-f", "/etc/nftables.conf"],
        ]
        with patch("apply.restart.subprocess.run") as mock_run:
            restart.run_actions(actions)
        self.assertEqual(mock_run.call_args_list, [
            call(actions[0], check=True),
            call(actions[1], check=True),
        ])

    def test_raises_on_first_failure_and_skips_remainder(self) -> None:
        actions = [
            ["systemctl", "restart", "systemd-networkd"],
            ["nft", "-f", "/etc/nftables.conf"],
            ["systemctl", "restart", "civicmesh-web", "civicmesh-mesh"],
        ]
        with patch("apply.restart.subprocess.run") as mock_run:
            mock_run.side_effect = [
                None,  # first ok
                subprocess.CalledProcessError(1, actions[1]),
                None,  # would be third — must not be reached
            ]
            with self.assertRaises(subprocess.CalledProcessError):
                restart.run_actions(actions)
        # Third call must not have happened.
        self.assertEqual(mock_run.call_count, 2)


if __name__ == "__main__":
    unittest.main()
