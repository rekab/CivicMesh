"""Tests for apply.validate: pre-flight syntax checks before any /etc/ write."""

import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

from apply import validate
from apply.driver import FileChange, Plan
from config import load_config


_MINIMAL_CONFIG = (
    Path(__file__).resolve().parent / "goldens" / "minimal-config.toml"
)


def _change(abs_path: str, payload: bytes = b"# stub") -> FileChange:
    return FileChange(
        abs_path=Path(abs_path),
        new_bytes=payload,
        old_bytes=None,
        mode=0o644,
    )


def _ok() -> "subprocess.CompletedProcess[bytes]":
    return subprocess.CompletedProcess(args=[], returncode=0, stdout=b"", stderr=b"")


def _fail(stderr: bytes = b"syntax error") -> "subprocess.CompletedProcess[bytes]":
    return subprocess.CompletedProcess(args=[], returncode=1, stdout=b"", stderr=stderr)


class ValidatePlanTest(unittest.TestCase):

    def setUp(self) -> None:
        self.cfg = load_config(str(_MINIMAL_CONFIG))

    def test_passes_when_all_validators_succeed(self) -> None:
        plan = Plan(
            changes=(
                _change("/etc/dnsmasq.d/civicmesh.conf"),
                _change("/etc/nftables.conf"),
            ),
            services=(),
        )
        with patch("apply.validate._iface_exists", return_value=True), \
             patch("apply.validate.subprocess.run", return_value=_ok()) as mock_run:
            errors = validate.validate_plan(plan, self.cfg)
        self.assertEqual(errors, [])
        # Two validators, two subprocess calls.
        self.assertEqual(mock_run.call_count, 2)

    def test_collects_error_when_validator_fails(self) -> None:
        plan = Plan(
            changes=(_change("/etc/nftables.conf"),),
            services=(),
        )
        with patch("apply.validate._iface_exists", return_value=True), \
             patch("apply.validate.subprocess.run",
                   return_value=_fail(b"syntax error")):
            errors = validate.validate_plan(plan, self.cfg)
        self.assertEqual(len(errors), 1)
        self.assertIn("/etc/nftables.conf", errors[0])
        self.assertIn("nft", errors[0])
        self.assertIn("syntax error", errors[0])

    def test_reports_missing_iface(self) -> None:
        plan = Plan(changes=(), services=())
        with patch("apply.validate._iface_exists", return_value=False):
            errors = validate.validate_plan(plan, self.cfg)
        self.assertEqual(len(errors), 1)
        self.assertIn(self.cfg.network.iface, errors[0])
        self.assertIn("/sys/class/net", errors[0])

    def test_reports_validator_binary_missing(self) -> None:
        plan = Plan(
            changes=(_change("/etc/nftables.conf"),),
            services=(),
        )
        with patch("apply.validate._iface_exists", return_value=True), \
             patch("apply.validate.subprocess.run",
                   side_effect=FileNotFoundError("nft")):
            errors = validate.validate_plan(plan, self.cfg)
        self.assertEqual(len(errors), 1)
        self.assertIn("validator missing", errors[0])
        self.assertIn("nft", errors[0])

    def test_skips_paths_without_validators(self) -> None:
        plan = Plan(
            changes=(
                _change("/etc/sysctl.d/90-civicmesh-disable-ipv6.conf"),
                _change("/etc/systemd/system/civicmesh-web.service"),
            ),
            services=(),
        )
        with patch("apply.validate._iface_exists", return_value=True), \
             patch("apply.validate.subprocess.run", return_value=_ok()) as mock_run:
            errors = validate.validate_plan(plan, self.cfg)
        self.assertEqual(errors, [])
        mock_run.assert_not_called()

    def test_dnsmasq_argv_glues_path_onto_conf_file_flag(self) -> None:
        """dnsmasq's `--conf-file=` flag wants the path appended, not separate."""
        plan = Plan(
            changes=(_change("/etc/dnsmasq.d/civicmesh.conf"),),
            services=(),
        )
        with patch("apply.validate._iface_exists", return_value=True), \
             patch("apply.validate.subprocess.run", return_value=_ok()) as mock_run:
            validate.validate_plan(plan, self.cfg)
        argv = mock_run.call_args_list[0][0][0]
        self.assertEqual(argv[0], "dnsmasq")
        self.assertEqual(argv[1], "--test")
        self.assertTrue(
            argv[2].startswith("--conf-file=") and len(argv[2]) > len("--conf-file="),
            f"expected --conf-file=<path>, got {argv[2]!r}",
        )
        self.assertEqual(len(argv), 3, f"expected 3 argv elements, got {argv!r}")


if __name__ == "__main__":
    unittest.main()
