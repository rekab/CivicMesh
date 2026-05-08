import logging
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
    # db_path emitted as a top-level key BEFORE any section header. config.py
    # rejects missing/relative db_path; tmp is always absolute (mkdtemp).
    body = f'db_path = "{tmp / "test.db"}"\n\n' + _render(sections)
    cfg_path.write_text(body)
    return cfg_path


def _good_sections(tmp: Path) -> dict[str, dict[str, object]]:
    return {
        "node": {"site_name": "CivicMesh", "callsign": "civic1"},
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


class NodeIdentityTest(unittest.TestCase):
    """CIV-11: site_name + callsign + legacy name alias + location rejection."""

    def _load_with_node(self, tmp: Path, node: dict[str, object]):
        sections = _good_sections(tmp)
        sections["node"] = node
        cfg_path = _write(tmp, sections)
        return load_config(str(cfg_path))

    def test_site_name_and_callsign_load_clean(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            (tmp / "logs").mkdir()
            cfg = self._load_with_node(
                tmp, {"site_name": "Fremont Hub", "callsign": "fremont1"},
            )
            self.assertEqual(cfg.node.site_name, "Fremont Hub")
            self.assertEqual(cfg.node.callsign, "fremont1")

    def test_legacy_name_alone_loads_as_site_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            (tmp / "logs").mkdir()
            sections = _good_sections(tmp)
            sections["node"] = {"name": "Old", "callsign": "civic1"}
            cfg_path = _write(tmp, sections)
            with self.assertLogs("config", level="WARNING") as lg:
                cfg = load_config(str(cfg_path))
            self.assertEqual(cfg.node.site_name, "Old")
            self.assertEqual(cfg.node.callsign, "civic1")
            self.assertTrue(
                any("node.name is deprecated" in line for line in lg.output),
                msg=f"expected deprecation WARN, got {lg.output}",
            )

    def test_name_matching_site_name_silent(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            (tmp / "logs").mkdir()
            sections = _good_sections(tmp)
            sections["node"] = {
                "name": "Same",
                "site_name": "Same",
                "callsign": "civic1",
            }
            cfg_path = _write(tmp, sections)
            # assertNoLogs is 3.10+; we use assertLogs with a sentinel record
            # to confirm no WARNING fires from the config logger.
            sentinel = logging.getLogger("config")
            with self.assertLogs("config", level="WARNING") as lg:
                cfg = load_config(str(cfg_path))
                # Force at least one record so assertLogs doesn't raise.
                sentinel.warning("__sentinel__")
            self.assertEqual(cfg.node.site_name, "Same")
            self.assertFalse(
                any("node.name is deprecated" in line for line in lg.output),
                msg=f"unexpected deprecation WARN, got {lg.output}",
            )

    def test_name_conflicting_site_name_errors(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            (tmp / "logs").mkdir()
            with self.assertRaises(ValueError) as ctx:
                self._load_with_node(tmp, {
                    "name": "A",
                    "site_name": "B",
                    "callsign": "civic1",
                })
            msg = str(ctx.exception)
            self.assertIn("node.name", msg)
            self.assertIn("node.site_name", msg)

    def test_location_field_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            (tmp / "logs").mkdir()
            with self.assertRaises(ValueError) as ctx:
                self._load_with_node(tmp, {
                    "site_name": "Hub",
                    "callsign": "civic1",
                    "location": "Fremont",
                })
            msg = str(ctx.exception)
            self.assertIn("node.location", msg)
            self.assertIn("site_name", msg)
            self.assertIn("callsign", msg)

    def test_callsign_too_long(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            (tmp / "logs").mkdir()
            with self.assertRaises(ValueError) as ctx:
                self._load_with_node(tmp, {
                    "site_name": "Hub",
                    "callsign": "abcdefghij",  # 10 chars
                })
            self.assertIn("callsign", str(ctx.exception))

    def test_callsign_invalid_char(self) -> None:
        bad = ["fre<mont", "fre>mont", "fre@mont", "fre:mont", "fre mont", "fre#mont"]
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            (tmp / "logs").mkdir()
            for cs in bad:
                with self.subTest(callsign=cs):
                    with self.assertRaises(ValueError) as ctx:
                        self._load_with_node(tmp, {
                            "site_name": "Hub",
                            "callsign": cs,
                        })
                    self.assertIn("callsign", str(ctx.exception))

    def test_callsign_empty_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            (tmp / "logs").mkdir()
            with self.assertRaises(ValueError) as ctx:
                self._load_with_node(tmp, {
                    "site_name": "Hub",
                    "callsign": "",
                })
            self.assertIn("callsign", str(ctx.exception))

    def test_legacy_node_without_callsign_flagged_at_load(self) -> None:
        """A pre-CIV-11 [node] (name + location, no callsign) loaded directly
        via load_config raises a schema-changed ValueError that names the
        missing field and points at civicmesh configure. The asymmetric
        migration (name -> site_name aliases, but location -> callsign does
        not) means absence of callsign must surface loudly rather than
        silently default to "". Partner to
        test_legacy_node_shape_migrates_through_walk in test_configure.py."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            (tmp / "logs").mkdir()
            sections = _good_sections(tmp)
            sections["node"] = {"name": "Old Hub"}  # no callsign
            cfg_path = _write(tmp, sections)
            with self.assertRaises(ValueError) as ctx:
                load_config(str(cfg_path))
            msg = str(ctx.exception)
            self.assertIn("callsign", msg)
            self.assertIn("civicmesh configure", msg)
            self.assertIn("CIV-11", msg)
            self.assertIn("schema has changed", msg)

    def test_callsign_lowercased_on_load(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            (tmp / "logs").mkdir()
            cfg = self._load_with_node(tmp, {
                "site_name": "Hub",
                "callsign": "Fremont1",
            })
            self.assertEqual(cfg.node.callsign, "fremont1")


if __name__ == "__main__":
    unittest.main()
