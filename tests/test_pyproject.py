"""Guard against the dev/prod import-path asymmetry.

setuptools editable installs treat py-modules as a hard allowlist.
A .py file at the repo root that isn't listed is invisible to
`import` at runtime — even though `uv run` and `python -m unittest`
will happily import it from the source tree, masking the problem
in dev. The systemd unit on prod invokes `.venv/bin/civicmesh-mesh`
directly: no CWD shenanigans, no uv, the editable-install finder
is the only path to imports. This test fails in dev when prod-mode
imports would.
"""

import tomllib
import unittest
from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parent.parent


# Top-level .py files that are intentionally NOT in py-modules
# (e.g. one-off scripts that are never imported). Each entry must
# carry a one-line rationale; if there's no rationale, the file
# belongs in py-modules instead.
_NOT_PACKAGED: dict[str, str] = {
    # "example_one_off.py": "ad-hoc migration script; never imported",
}


class PyModulesAllowlistTest(unittest.TestCase):

    def test_top_level_py_files_are_either_packaged_or_allowlisted(self) -> None:
        with open(_REPO_ROOT / "pyproject.toml", "rb") as f:
            data = tomllib.load(f)
        py_modules = set(data["tool"]["setuptools"]["py-modules"])
        allowlisted_files = set(_NOT_PACKAGED)
        allowlisted_modules = {n.removesuffix(".py") for n in allowlisted_files}

        overlap = py_modules & allowlisted_modules
        self.assertFalse(
            overlap,
            f"contradiction: these module names appear in BOTH pyproject.toml's "
            f"py-modules and tests/test_pyproject.py's _NOT_PACKAGED: "
            f"{sorted(overlap)}. pick one bucket.",
        )

        on_disk_files = {p.name for p in _REPO_ROOT.glob("*.py")}
        on_disk_modules = {n.removesuffix(".py") for n in on_disk_files}

        stale = py_modules - on_disk_modules
        self.assertFalse(
            stale,
            f"pyproject.toml's py-modules lists {sorted(stale)} but no matching "
            f".py files exist at the repo root. remove the stale entries.",
        )

        orphans = on_disk_files - {f"{n}.py" for n in py_modules} - allowlisted_files
        self.assertFalse(
            orphans,
            f"these top-level .py files are not in pyproject.toml's py-modules "
            f"and not in tests/test_pyproject.py's _NOT_PACKAGED allowlist: "
            f"{sorted(orphans)}. either add them to py-modules (most common — "
            f"setuptools won't expose them in editable installs otherwise) or "
            f"allowlist them in _NOT_PACKAGED with a one-line rationale.",
        )


if __name__ == "__main__":
    unittest.main()
