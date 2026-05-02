import tempfile
import unittest
from ipaddress import IPv4Address
from pathlib import Path

from config import load_config


_BASE_NETWORK = {
    "ip": "10.0.0.1",
    "subnet_cidr": "10.0.0.0/24",
    "iface": "wlan0",
    "country_code": "US",
    "dhcp_range_start": "10.0.0.10",
    "dhcp_range_end": "10.0.0.250",
    "dhcp_lease": "15m",
}
_BASE_AP = {"ssid": "CivicMesh-Messages", "channel": 6}


def _render(sections: dict[str, dict[str, object]]) -> str:
    """Render a TOML string from a {section: {key: value}} dict.

    Stays simple: scalars only (str/int/float/bool), no nested tables or
    arrays-of-tables. Strings get quoted; everything else uses repr.
    """
    lines: list[str] = []
    for section, fields in sections.items():
        lines.append(f"[{section}]")
        for k, v in fields.items():
            if isinstance(v, str):
                lines.append(f'{k} = "{v}"')
            elif isinstance(v, bool):
                lines.append(f"{k} = {'true' if v else 'false'}")
            elif isinstance(v, list):
                inner = ", ".join(f'"{x}"' for x in v)
                lines.append(f"{k} = [{inner}]")
            else:
                lines.append(f"{k} = {v}")
        lines.append("")
    return "\n".join(lines)


def _write(tmp: Path, sections: dict[str, dict[str, object]]) -> Path:
    cfg_path = tmp / "config.toml"
    cfg_path.write_text(_render(sections))
    return cfg_path


def _good_sections(tmp: Path) -> dict[str, dict[str, object]]:
    return {
        "node": {"name": "CivicMesh", "location": "Test"},
        "network": dict(_BASE_NETWORK),
        "ap": dict(_BASE_AP),
        "channels": {"names": ["#test"]},
        "web": {"port": 8080, "portal_aliases": ["civicmesh.internal"]},
        "logging": {"log_dir": str(tmp / "logs"), "log_level": "WARNING"},
    }


class ConfigSchemaTest(unittest.TestCase):
    def test_happy_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            (tmp / "logs").mkdir()
            cfg_path = _write(tmp, _good_sections(tmp))
            cfg = load_config(str(cfg_path))
            self.assertEqual(cfg.network.ip, IPv4Address("10.0.0.1"))
            self.assertEqual(cfg.network.iface, "wlan0")
            self.assertEqual(cfg.network.country_code, "US")
            self.assertEqual(cfg.ap.ssid, "CivicMesh-Messages")
            self.assertEqual(cfg.ap.channel, 6)
            self.assertEqual(cfg.web.port, 8080)
            self.assertEqual(cfg.web.portal_aliases, ("civicmesh.internal",))
            self.assertFalse(hasattr(cfg.web, "portal_host"))

    def test_ip_outside_subnet(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            (tmp / "logs").mkdir()
            sections = _good_sections(tmp)
            sections["network"]["ip"] = "10.0.1.5"
            cfg_path = _write(tmp, sections)
            with self.assertRaises(ValueError) as ctx:
                load_config(str(cfg_path))
            msg = str(ctx.exception)
            self.assertIn("10.0.1.5", msg)
            self.assertIn("10.0.0.0/24", msg)

    def test_dhcp_range_backwards(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            (tmp / "logs").mkdir()
            sections = _good_sections(tmp)
            sections["network"]["dhcp_range_start"] = "10.0.0.250"
            sections["network"]["dhcp_range_end"] = "10.0.0.10"
            cfg_path = _write(tmp, sections)
            with self.assertRaises(ValueError) as ctx:
                load_config(str(cfg_path))
            self.assertIn("10.0.0.250", str(ctx.exception))
            self.assertIn("10.0.0.10", str(ctx.exception))

    def test_ip_inside_dhcp_range(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            (tmp / "logs").mkdir()
            sections = _good_sections(tmp)
            sections["network"]["ip"] = "10.0.0.50"
            cfg_path = _write(tmp, sections)
            with self.assertRaises(ValueError) as ctx:
                load_config(str(cfg_path))
            msg = str(ctx.exception)
            self.assertIn("10.0.0.50", msg)
            self.assertIn("10.0.0.10", msg)
            self.assertIn("10.0.0.250", msg)

    def test_old_shape_migration_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            (tmp / "logs").mkdir()
            sections = {
                "node": {"name": "CivicMesh", "location": "Test"},
                "channels": {"names": ["#test"]},
                "web": {"port": 8080, "portal_host": "10.0.0.1"},
                "logging": {"log_dir": str(tmp / "logs")},
            }
            cfg_path = _write(tmp, sections)
            with self.assertRaises(ValueError) as ctx:
                load_config(str(cfg_path))
            msg = str(ctx.exception)
            self.assertIn("web.portal_host", msg)
            self.assertIn("[network]", msg)
            self.assertIn("[ap]", msg)
            self.assertIn("CIV-60", msg)

    def test_channel_warning_allowed(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            (tmp / "logs").mkdir()
            sections = _good_sections(tmp)
            sections["ap"]["channel"] = 4
            cfg_path = _write(tmp, sections)
            with self.assertLogs("config", level="WARNING") as lg:
                cfg = load_config(str(cfg_path))
            self.assertEqual(cfg.ap.channel, 4)
            self.assertTrue(
                any("non-overlapping" in line for line in lg.output),
                msg=f"expected non-overlapping warning, got {lg.output}",
            )

    def test_channel_hard_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            (tmp / "logs").mkdir()
            sections = _good_sections(tmp)
            sections["ap"]["channel"] = 14
            cfg_path = _write(tmp, sections)
            with self.assertRaises(ValueError) as ctx:
                load_config(str(cfg_path))
            self.assertIn("14", str(ctx.exception))
            self.assertIn("1-11", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
