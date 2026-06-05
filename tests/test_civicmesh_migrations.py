"""Tests for one-shot config migrations in civicmesh.py.

These helpers run inside `civicmesh apply` against the on-disk prod
config.toml. They must be idempotent — subsequent applies are no-ops.
"""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import civicmesh


class MigrateLogsCiv104Test(unittest.TestCase):
    def _run_against(self, config_text: str) -> tuple[str, list[str]]:
        """Write config_text to a tmp path, run the migration, return
        (final_text, captured_stdout_lines)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg_path = Path(tmpdir) / "config.toml"
            cfg_path.write_text(config_text)
            with mock.patch(
                "civicmesh.Path", side_effect=lambda p: Path(p) if p != "/usr/local/civicmesh/etc/config.toml" else cfg_path
            ):
                printed: list[str] = []
                with mock.patch("builtins.print", side_effect=lambda *a, **k: printed.append(" ".join(str(x) for x in a))):
                    civicmesh._migrate_logs_civ_104()
            return cfg_path.read_text(), printed

    def test_legacy_value_rewritten(self) -> None:
        legacy = '[logging]\nlog_dir = "logs"\nlog_level = "INFO"\n'
        final, printed = self._run_against(legacy)
        self.assertIn('log_dir = "var/logs"', final)
        self.assertNotIn('log_dir = "logs"', final)
        self.assertTrue(any("CIV-104" in line for line in printed))
        self.assertTrue(any("orphaned" in line for line in printed))

    def test_idempotent_on_second_run(self) -> None:
        # Already-migrated config: second run is a no-op.
        already = '[logging]\nlog_dir = "var/logs"\nlog_level = "INFO"\n'
        final, printed = self._run_against(already)
        self.assertEqual(final, already)
        self.assertEqual(printed, [])

    def test_other_log_dir_values_not_touched(self) -> None:
        # Operator may have set an absolute or custom path. Only the exact
        # legacy literal "logs" gets rewritten; anything else is left alone.
        custom = '[logging]\nlog_dir = "/srv/civicmesh/logs"\nlog_level = "INFO"\n'
        final, printed = self._run_against(custom)
        self.assertEqual(final, custom)
        self.assertEqual(printed, [])

    def test_missing_config_file_is_noop(self) -> None:
        # First apply on a fresh node may run before /usr/local/civicmesh/etc/
        # has a config.toml (configure hasn't been run). Migration must
        # silently no-op rather than crash.
        with tempfile.TemporaryDirectory() as tmpdir:
            missing = Path(tmpdir) / "does-not-exist.toml"
            with mock.patch(
                "civicmesh.Path", side_effect=lambda p: missing if p == "/usr/local/civicmesh/etc/config.toml" else Path(p)
            ):
                printed: list[str] = []
                with mock.patch("builtins.print", side_effect=lambda *a, **k: printed.append(" ".join(str(x) for x in a))):
                    civicmesh._migrate_logs_civ_104()
            self.assertFalse(missing.exists())
            self.assertEqual(printed, [])


if __name__ == "__main__":
    unittest.main()
