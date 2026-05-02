"""Tests for civicmesh._find_dev_project_root after the CIV-63 refactor.

The function now takes an optional `start: Path` parameter and returns
None on miss (was: raised RuntimeError, no parameter).
"""

import tempfile
import unittest
from pathlib import Path

from civicmesh import _find_dev_project_root


class FindDevProjectRootTest(unittest.TestCase):

    def test_finds_civicmesh_pyproject_when_walking_up(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "pyproject.toml").write_text(
                '[project]\nname = "civicmesh"\nversion = "0.1.0"\n'
            )
            deep = root / "a" / "b" / "c"
            deep.mkdir(parents=True)
            self.assertEqual(
                _find_dev_project_root(deep),
                root.resolve(),
            )

    def test_returns_none_when_no_civicmesh_pyproject_above(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "pyproject.toml").write_text(
                '[project]\nname = "something-else"\n'
            )
            self.assertIsNone(_find_dev_project_root(root))

    def test_keeps_walking_past_unrelated_pyproject(self) -> None:
        """If a sibling repo's pyproject.toml is closer than civicmesh's,
        the function should keep walking and find civicmesh up the tree."""
        with tempfile.TemporaryDirectory() as td:
            outer = Path(td)
            (outer / "pyproject.toml").write_text(
                '[project]\nname = "civicmesh"\n'
            )
            inner = outer / "vendored-thing"
            inner.mkdir()
            (inner / "pyproject.toml").write_text(
                '[project]\nname = "vendored-thing"\n'
            )
            deeper = inner / "src"
            deeper.mkdir()
            self.assertEqual(_find_dev_project_root(deeper), outer.resolve())


if __name__ == "__main__":
    unittest.main()
