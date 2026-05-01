import json
import re
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from config import load_config

import configure


# ---------------------------------------------------------------- helpers

def _good_config_text(
    *,
    log_dir: Path,
    posts_per_hour: int = 10,
    portal_aliases: tuple[str, ...] = ("civicmesh.internal",),
    channel: int = 6,
    dhcp_range_start: str = "10.0.0.10",
    sentinel_name: str = "CivicMesh",
) -> str:
    aliases_repr = ", ".join(f'"{a}"' for a in portal_aliases)
    return f"""\
[node]
name = "{sentinel_name}"
location = "Test"

[network]
ip = "10.0.0.1"
subnet_cidr = "10.0.0.0/24"
iface = "wlan0"
country_code = "US"
dhcp_range_start = "{dhcp_range_start}"
dhcp_range_end = "10.0.0.250"
dhcp_lease = "15m"

[ap]
ssid = "CivicMesh-Messages"
channel = {channel}

[radio]
serial_port = "/dev/null"
freq_mhz = 910.525
bw_khz = 62.5
sf = 7
cr = 5

[channels]
names = ["#test"]

[web]
port = 8080
portal_aliases = [{aliases_repr}]

[limits]
posts_per_hour = {posts_per_hour}

[logging]
log_dir = "{log_dir}"
log_level = "WARNING"
"""


# ----------------------------------------- input sequences for full walks

# Walk through every Tier-1 prompt accepting baseline defaults, with
# _detect_serial_port and _detect_iface mocked to []. Final "y" confirms
# the write.
_ENTER_THROUGH_WALK = [
    "",   # node.name (default accepted)
    "",   # node.location
    "d",  # channels sub-loop: done immediately
    "",   # radio.serial_port (no detection -> just prompt)
    "",   # ap.ssid
    "",   # ap.channel
    "",   # network.iface (no detection -> just prompt)
    "",   # network.country_code
    "",   # debug.allow_eth0 (default False -> Enter accepts)
    "y",  # confirm write
]


# ----------------------------------------- round-trip / preservation tests

class ConfigureRoundTripTest(unittest.TestCase):

    def test_tier2_values_preserved_through_walk(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            (tmp / "logs").mkdir()
            cfg_path = tmp / "config.toml"
            cfg_path.write_text(_good_config_text(
                log_dir=tmp / "logs",
                posts_per_hour=99,
                portal_aliases=("civicmesh.internal",),
            ))
            with patch("configure._detect_serial_port", return_value=[]), \
                 patch("configure._detect_iface", return_value=[]), \
                 patch("builtins.input", side_effect=_ENTER_THROUGH_WALK):
                rc = configure.run_configure(cfg_path, "dev")
            self.assertEqual(rc, 0)

            cfg = load_config(str(cfg_path))
            self.assertEqual(cfg.limits.posts_per_hour, 99)
            self.assertEqual(cfg.web.portal_aliases, ("civicmesh.internal",))
            # Tier 1 round-tripped too (defaults accepted).
            self.assertEqual(cfg.node.name, "CivicMesh")
            self.assertEqual(cfg.ap.channel, 6)

    def test_live_file_preserved_on_validation_failure(self) -> None:
        """Bad Tier-2 (dhcp_range_start outside subnet) survives Tier-1 walk;
        post-write validation fails; live file is untouched, no .tmp left."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            (tmp / "logs").mkdir()
            cfg_path = tmp / "config.toml"
            original_text = _good_config_text(
                log_dir=tmp / "logs",
                # dhcp_range_start outside the 10.0.0.0/24 subnet:
                dhcp_range_start="192.168.0.10",
                sentinel_name="ORIGINAL",
            )
            cfg_path.write_text(original_text)
            with patch("configure._detect_serial_port", return_value=[]), \
                 patch("configure._detect_iface", return_value=[]), \
                 patch("builtins.input", side_effect=_ENTER_THROUGH_WALK):
                rc = configure.run_configure(cfg_path, "dev")
            self.assertEqual(rc, 2)
            # Live file unchanged.
            self.assertEqual(cfg_path.read_text(), original_text)
            # No tempfile left behind.
            stragglers = list(tmp.glob(".config.toml.*.tmp"))
            self.assertEqual(stragglers, [], msg=f"unexpected: {stragglers}")


# --------------------------------------- helper / sub-loop unit tests

class ConfigurePromptTest(unittest.TestCase):

    def test_prompt_serial_one_detected_accept(self) -> None:
        with patch("configure._detect_serial_port",
                   return_value=[Path("/dev/serial/by-id/usb-Silicon_Labs_X")]), \
             patch("builtins.input", side_effect=[""]):
            # Single Enter accepts the "Use this? [Y/n]" default.
            result = configure._prompt_serial(None)
        self.assertEqual(result, "/dev/serial/by-id/usb-Silicon_Labs_X")

    def test_prompt_serial_zero_detected(self) -> None:
        with patch("configure._detect_serial_port", return_value=[]), \
             patch("builtins.input", side_effect=["/dev/ttyUSB0"]):
            result = configure._prompt_serial(None)
        self.assertEqual(result, "/dev/ttyUSB0")

    def test_prompt_serial_multiple_detected_pick_number(self) -> None:
        with patch("configure._detect_serial_port", return_value=[
            Path("/dev/serial/by-id/A"),
            Path("/dev/serial/by-id/B"),
        ]), patch("builtins.input", side_effect=["2"]):
            result = configure._prompt_serial(None)
        self.assertEqual(result, "/dev/serial/by-id/B")

    def test_prompt_iface_one_detected_accept(self) -> None:
        with patch("configure._detect_iface", return_value=["wlan0"]), \
             patch("builtins.input", side_effect=[""]):
            result = configure._prompt_iface("wlan9")
        self.assertEqual(result, "wlan0")

    def test_prompt_iface_zero_detected_falls_back_to_prompt(self) -> None:
        with patch("configure._detect_iface", return_value=[]), \
             patch("builtins.input", side_effect=[""]):
            result = configure._prompt_iface("wlan0")
        self.assertEqual(result, "wlan0")

    def test_prompt_iface_multiple_detected(self) -> None:
        with patch("configure._detect_iface", return_value=["wlan0", "wlan1"]), \
             patch("builtins.input", side_effect=["wlan1"]):
            result = configure._prompt_iface("wlan0")
        self.assertEqual(result, "wlan1")

    def test_channel_sub_loop_add_remove(self) -> None:
        seq = [
            "a", "#foo",
            "a", "#bar",
            "r", "#foo",
            "d",
        ]
        with patch("builtins.input", side_effect=seq):
            result = configure._prompt_channels([])
        self.assertEqual(result, ["#bar"])

    def test_channel_sub_loop_clear(self) -> None:
        # After clearing, [d]one triggers the "empty list, continue?" guard;
        # answer "y" to accept the empty list.
        with patch("builtins.input", side_effect=["c", "d", "y"]):
            result = configure._prompt_channels(["#existing"])
        self.assertEqual(result, [])


# ------------------------------------- subprocess tests for the CLI

class ConfigCliTest(unittest.TestCase):
    """End-to-end subprocess tests for `civicmesh config show / validate` and refusal."""

    def setUp(self) -> None:
        self.repo_root = Path(__file__).resolve().parent.parent

    def _run(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["uv", "run", "civicmesh", *args],
            capture_output=True, text=True, cwd=self.repo_root, timeout=60,
        )

    def test_config_show_format_json_round_trips(self) -> None:
        result = self._run("--config", "config.toml.example", "config", "show", "--format", "json")
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        parsed = json.loads(result.stdout)
        self.assertEqual(parsed["network"]["ip"], "10.0.0.1")
        self.assertEqual(parsed["ap"]["channel"], 6)
        self.assertIn("logging", parsed)

    def test_config_validate_passes_on_example(self) -> None:
        result = self._run("--config", "config.toml.example", "config", "validate")
        self.assertEqual(result.returncode, 0, msg=result.stderr)

    def test_config_validate_rejects_channel_5(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            (tmp / "logs").mkdir()
            bad = tmp / "config.toml"
            bad.write_text(_good_config_text(log_dir=tmp / "logs", channel=5))
            result = self._run("--config", str(bad), "config", "validate")
            self.assertEqual(result.returncode, 1, msg=result.stdout + result.stderr)
            self.assertRegex(result.stderr, r"ap\.channel 5: must be 1, 6, or 11")

    def test_configure_refusal_in_dev_mode(self) -> None:
        result = self._run(
            "--config", "/usr/local/civicmesh/etc/config.toml", "configure",
        )
        self.assertEqual(result.returncode, 10, msg=result.stdout + result.stderr)
        self.assertIn("dev binary", result.stderr)


if __name__ == "__main__":
    unittest.main()
