import re
import subprocess
import tempfile
import unittest
from pathlib import Path


class CivicmeshSmokeTest(unittest.TestCase):
    def test_stats_runs_via_uv(self) -> None:
        repo_root = Path(__file__).resolve().parent.parent
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            example = (repo_root / "config.toml.example").read_text()
            db_path = tmp / "civic_mesh.db"
            log_dir = tmp / "logs"
            log_dir.mkdir()

            # config.toml.example carries top-level db_path and a [logging]
            # log_dir; rewrite both in place to point at our tmpdir. (Prior
            # to the db_path-required change, this prepended db_path; that
            # would now produce a duplicate top-level key and tomllib would
            # raise.)
            edited = re.sub(
                r'^log_dir\s*=.*$',
                f'log_dir = "{log_dir}"',
                example,
                count=1,
                flags=re.MULTILINE,
            )
            edited = re.sub(
                r'^db_path\s*=.*$',
                f'db_path = "{db_path}"',
                edited,
                count=1,
                flags=re.MULTILINE,
            )

            cfg_path = tmp / "config.toml"
            cfg_path.write_text(edited)

            result = subprocess.run(
                ["uv", "run", "civicmesh", "--config", str(cfg_path), "stats"],
                capture_output=True,
                text=True,
                cwd=repo_root,
                timeout=60,
            )
            self.assertEqual(result.returncode, 0, msg=result.stderr)
            self.assertRegex(
                result.stdout,
                r"messages=\d+ sessions=\d+ outbox_pending=\d+ votes=\d+",
            )


if __name__ == "__main__":
    unittest.main()
