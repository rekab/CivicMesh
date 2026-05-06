"""Tests for hub_docs.py.

Direct calls to install_hub_docs / rollback_hub_docs. Two CLI smoke
tests confirm argparse routing through civicmesh.main(). Fixtures are
in-module, no shared conftest.

Manual atomicity smoke check (NOT automated): hold a `cat` open against
a PDF in the active release directory, run install with a different
zip, verify `cat` completes after the symlink swap. Not unit-testable
without forking processes.
"""

from __future__ import annotations

import argparse
import contextlib
import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch


_REPO_ROOT = Path(__file__).resolve().parent.parent
_BUILD_SCRIPT = _REPO_ROOT / "scripts" / "build_hub_docs.py"


def _load_build_module():
    spec = importlib.util.spec_from_file_location(
        "build_hub_docs", _BUILD_SCRIPT
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("build_hub_docs", mod)
    spec.loader.exec_module(mod)
    return mod


_build_mod = _load_build_module()


import hub_docs  # noqa: E402


_PDF_BYTES = b"%PDF-1.4\n%dummy\n%%EOF\n"


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _make_fixture_zip(
    tmp: Path,
    docs: list[tuple[str, str, str, str]],
    *,
    source_label: str = "Test",
    note: str = "Test note",
    built_at: datetime | None = None,
) -> Path:
    """Build a real CIV-91-style zip via build_hub_docs.run().

    docs entries: (filename, category, title, lang).
    """
    src = tmp / "src"
    src.mkdir(parents=True, exist_ok=True)
    lines = [
        f'source_label = "{source_label}"',
        f'note = "{note}"',
        "",
    ]
    for filename, category, title, lang in docs:
        (src / filename).write_bytes(_PDF_BYTES)
        lines.extend([
            "[[doc]]",
            f'category = "{category}"',
            f'title = "{title}"',
            f'file = "{filename}"',
            f'lang = "{lang}"',
            "",
        ])
    (src / "manifest.toml").write_text("\n".join(lines))

    out = tmp / "out"
    out.mkdir(parents=True, exist_ok=True)
    args = _build_mod._make_parser().parse_args([
        "--source", str(src), "--out", str(out),
    ])
    if built_at is None:
        built_at = datetime.now(timezone.utc)
    rc = _build_mod.run(args, built_at=built_at)
    assert rc == 0, "fixture zip build failed"
    zips = list(out.glob("hub-docs-*.zip"))
    assert len(zips) == 1, f"expected 1 zip, got {zips!r}"
    return zips[0]


def _make_bad_zip_with_index(
    path: Path,
    *,
    index: dict,
    pdf_files: dict[str, bytes] | None = None,
    extra_members: dict[str, bytes] | None = None,
) -> None:
    """Hand-craft a zip with a controlled index.json + members."""
    pdf_files = pdf_files or {}
    extra_members = extra_members or {}
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("hub-docs/index.json", json.dumps(index))
        for name, body in pdf_files.items():
            zf.writestr(f"hub-docs/{name}", body)
        for name, body in extra_members.items():
            zf.writestr(name, body)


def _valid_index(release_id: str = "20260401T143200Z", docs: list[dict] | None = None) -> dict:
    if docs is None:
        docs = [{
            "filename": "ok.pdf",
            "title": "OK",
            "lang": "en",
            "last_reviewed": "2025-01-01",
            "size_bytes": len(_PDF_BYTES),
        }]
    return {
        "schema_version": 1,
        "built_at": "2026-04-01T14:32:00Z",
        "source_label": "Test",
        "note": "n",
        "categories": [{"name": "Cat", "docs": docs}],
    }


@contextlib.contextmanager
def _capture():
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        yield out, err


# ---------------------------------------------------------------------------
# Install — happy paths
# ---------------------------------------------------------------------------


class InstallHappyPathTest(unittest.TestCase):

    def test_first_install_previous_none(self) -> None:
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            var = tmp / "var"
            var.mkdir()
            built = datetime(2026, 4, 1, 14, 32, 0, tzinfo=timezone.utc)
            zip_path = _make_fixture_zip(
                tmp, [("a.pdf", "C1", "A", "en")], built_at=built,
            )
            result = hub_docs.install_hub_docs(
                zip_path, var_dir=var, retention=3,
            )
            self.assertEqual(result["release_id"], "20260401T143200Z")
            self.assertIsNone(result["previous"])
            self.assertEqual(result["pruned"], [])
            link = var / "hub-docs"
            self.assertTrue(link.is_symlink())
            self.assertEqual(
                os.readlink(link),
                "hub-docs.releases/20260401T143200Z",
            )
            # §7 layout: index.json + PDFs sit DIRECTLY under
            # <release_id>/, not nested in <release_id>/hub-docs/.
            release = var / "hub-docs.releases" / "20260401T143200Z"
            self.assertTrue((release / "index.json").is_file())
            self.assertTrue((release / "a.pdf").is_file())
            self.assertFalse(
                (release / "hub-docs").exists(),
                "inner hub-docs/ subdir must be lifted away after install",
            )

    def test_second_install_previous_named(self) -> None:
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            var = tmp / "var"
            var.mkdir()
            zip_a = _make_fixture_zip(
                tmp / "a", [("a.pdf", "C", "A", "en")],
                built_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            )
            zip_b = _make_fixture_zip(
                tmp / "b", [("b.pdf", "C", "B", "en")],
                built_at=datetime(2026, 2, 1, tzinfo=timezone.utc),
            )
            hub_docs.install_hub_docs(zip_a, var_dir=var, retention=3)
            result = hub_docs.install_hub_docs(zip_b, var_dir=var, retention=3)
            self.assertEqual(result["release_id"], "20260201T000000Z")
            self.assertEqual(result["previous"], "20260101T000000Z")
            self.assertEqual(result["pruned"], [])
            self.assertTrue((var / "hub-docs.releases" / "20260101T000000Z").is_dir())
            self.assertTrue((var / "hub-docs.releases" / "20260201T000000Z").is_dir())

    def test_retention_pressure_prunes_oldest(self) -> None:
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            var = tmp / "var"
            var.mkdir()
            for i, day in enumerate(["01", "02", "03"], start=1):
                z = _make_fixture_zip(
                    tmp / f"a{i}", [(f"f{i}.pdf", "C", f"T{i}", "en")],
                    built_at=datetime(
                        2026, 1, int(day), tzinfo=timezone.utc
                    ),
                )
                hub_docs.install_hub_docs(z, var_dir=var, retention=3)
            z4 = _make_fixture_zip(
                tmp / "a4", [("f4.pdf", "C", "T4", "en")],
                built_at=datetime(2026, 1, 4, tzinfo=timezone.utc),
            )
            result = hub_docs.install_hub_docs(z4, var_dir=var, retention=3)
            self.assertEqual(result["pruned"], ["20260101T000000Z"])
            self.assertFalse(
                (var / "hub-docs.releases" / "20260101T000000Z").exists()
            )
            for kept in ["20260102T000000Z", "20260103T000000Z", "20260104T000000Z"]:
                self.assertTrue(
                    (var / "hub-docs.releases" / kept).is_dir(),
                    msg=kept,
                )


# ---------------------------------------------------------------------------
# Install — failure modes
# ---------------------------------------------------------------------------


class InstallFailuresTest(unittest.TestCase):

    def test_rejects_non_zip(self) -> None:
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            (tmp / "var").mkdir()
            txt = tmp / "not-a-zip.zip"
            txt.write_text("plain text, not a zip\n")
            with self.assertRaises(hub_docs.HubDocsError) as ctx:
                hub_docs.install_hub_docs(
                    txt, var_dir=tmp / "var", retention=3,
                )
            self.assertEqual(ctx.exception.exit_code, 2)
            # No symlink, no release dirs.
            self.assertFalse((tmp / "var" / "hub-docs").exists())

    def test_validation_failures(self) -> None:
        cases = [
            (
                "schema_version_drift",
                {**_valid_index(), "schema_version": 99},
                {"ok.pdf": _PDF_BYTES},
                3,
                "schema_version",
            ),
            (
                "listed_file_missing",
                _valid_index(),
                {},  # index says ok.pdf but zip has no PDFs
                3,
                "missing from zip",
            ),
            (
                "size_mismatch",
                _valid_index(docs=[{
                    "filename": "ok.pdf", "title": "OK", "lang": "en",
                    "last_reviewed": "2025-01-01", "size_bytes": 999,
                }]),
                {"ok.pdf": _PDF_BYTES},
                3,
                "size mismatch",
            ),
            (
                "bad_magic",
                _valid_index(),
                # Same length as _PDF_BYTES so the size check passes
                # and the magic check fires (the rule we're testing).
                {"ok.pdf": b"X" * len(_PDF_BYTES)},
                3,
                "%PDF-",
            ),
            (
                "orphan_file",
                _valid_index(),
                {"ok.pdf": _PDF_BYTES, "extra.pdf": _PDF_BYTES},
                3,
                "orphan",
            ),
        ]
        for label, index, pdfs, expected_code, msg_fragment in cases:
            with self.subTest(case=label):
                with tempfile.TemporaryDirectory() as t:
                    tmp = Path(t)
                    var = tmp / "var"
                    var.mkdir()
                    zip_path = tmp / "bad.zip"
                    _make_bad_zip_with_index(
                        zip_path, index=index, pdf_files=pdfs,
                    )
                    with self.assertRaises(hub_docs.HubDocsError) as ctx:
                        hub_docs.install_hub_docs(
                            zip_path, var_dir=var, retention=3,
                        )
                    self.assertEqual(ctx.exception.exit_code, expected_code, msg=label)
                    self.assertIn(msg_fragment, str(ctx.exception), msg=label)
                    # No state change.
                    self.assertFalse((var / "hub-docs").exists(), msg=label)
                    self.assertFalse(
                        any((var / "hub-docs.releases").glob("*"))
                        if (var / "hub-docs.releases").exists()
                        else False,
                        msg=label,
                    )

    def test_rejects_release_id_collision(self) -> None:
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            var = tmp / "var"
            var.mkdir()
            built = datetime(2026, 4, 1, 14, 32, 0, tzinfo=timezone.utc)
            zip_path = _make_fixture_zip(
                tmp, [("a.pdf", "C", "A", "en")], built_at=built,
            )
            collision = var / "hub-docs.releases" / "20260401T143200Z"
            collision.mkdir(parents=True)
            (collision / "marker").write_text("pre-existing")
            with self.assertRaises(hub_docs.HubDocsError) as ctx:
                hub_docs.install_hub_docs(
                    zip_path, var_dir=var, retention=3,
                )
            self.assertEqual(ctx.exception.exit_code, 4)
            # Pre-existing dir untouched.
            self.assertEqual(
                (collision / "marker").read_text(), "pre-existing"
            )

    def test_handles_leftover_incoming(self) -> None:
        """Pre-create <id>.incoming/ from a notional aborted prior run.

        Install should clear it and proceed cleanly.
        """
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            var = tmp / "var"
            var.mkdir()
            built = datetime(2026, 4, 1, 14, 32, 0, tzinfo=timezone.utc)
            zip_path = _make_fixture_zip(
                tmp, [("a.pdf", "C", "A", "en")], built_at=built,
            )
            stale = var / "hub-docs.releases" / "20260401T143200Z.incoming"
            stale.mkdir(parents=True)
            (stale / "stale-marker").write_text("from prior aborted run")
            with _capture() as (_, err):
                result = hub_docs.install_hub_docs(
                    zip_path, var_dir=var, retention=3,
                )
            self.assertEqual(result["release_id"], "20260401T143200Z")
            self.assertIn("cleared stale incoming", err.getvalue())
            self.assertFalse(stale.exists())

    def test_rejects_zip_slip(self) -> None:
        """A zip member resolving outside the staging dir is rejected
        before any byte is written.
        """
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            var = tmp / "var"
            var.mkdir()
            zip_path = tmp / "slip.zip"
            _make_bad_zip_with_index(
                zip_path,
                index=_valid_index(),
                pdf_files={"ok.pdf": _PDF_BYTES},
                extra_members={"../../../etc/passwd": b"ROOT::"},
            )
            with self.assertRaises(hub_docs.HubDocsError) as ctx:
                hub_docs.install_hub_docs(
                    zip_path, var_dir=var, retention=3,
                )
            self.assertEqual(ctx.exception.exit_code, 2)
            self.assertIn("unsafe path", str(ctx.exception))
            # No staging dir, no symlink, no release.
            self.assertFalse((var / "hub-docs").exists())
            releases = var / "hub-docs.releases"
            if releases.exists():
                self.assertEqual(list(releases.iterdir()), [])

    def test_stray_incoming_directory_ignored(self) -> None:
        """A stray <id>.incoming/ next to real release dirs is invisible
        to rollback (lex-prev) and pruning (candidates list).
        """
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            var = tmp / "var"
            var.mkdir()
            zip_a = _make_fixture_zip(
                tmp / "a", [("a.pdf", "C", "A", "en")],
                built_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            )
            zip_b = _make_fixture_zip(
                tmp / "b", [("b.pdf", "C", "B", "en")],
                built_at=datetime(2026, 2, 1, tzinfo=timezone.utc),
            )
            hub_docs.install_hub_docs(zip_a, var_dir=var, retention=3)
            hub_docs.install_hub_docs(zip_b, var_dir=var, retention=3)
            # Plant a stray incoming sibling that would lex-greatest if seen.
            stray = var / "hub-docs.releases" / "29991231T235959Z.incoming"
            stray.mkdir()
            (stray / "junk").write_text("ignore me")

            # Rollback should pick A, not the stray.
            result = hub_docs.rollback_hub_docs(var_dir=var, to_id=None)
            self.assertEqual(result["release_id"], "20260101T000000Z")

            # Prune (via a 3rd install with retention=2) should NOT see the stray.
            zip_c = _make_fixture_zip(
                tmp / "c", [("c.pdf", "C", "C", "en")],
                built_at=datetime(2026, 3, 1, tzinfo=timezone.utc),
            )
            hub_docs.install_hub_docs(zip_c, var_dir=var, retention=2)
            # Stray must still exist.
            self.assertTrue(stray.is_dir())


# ---------------------------------------------------------------------------
# Dry-run
# ---------------------------------------------------------------------------


class DryRunTest(unittest.TestCase):

    def test_dry_run_happy(self) -> None:
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            var = tmp / "var"
            var.mkdir()
            built = datetime(2026, 4, 1, 14, 32, 0, tzinfo=timezone.utc)
            zip_path = _make_fixture_zip(
                tmp,
                [
                    ("a.pdf", "C1", "A", "en"),
                    ("b.pdf", "C1", "B", "en"),
                ],
                built_at=built,
            )
            result = hub_docs.install_hub_docs(
                zip_path, var_dir=var, retention=3, dry_run=True,
            )
            self.assertEqual(result["release_id"], "20260401T143200Z")
            self.assertEqual(result["docs"], 2)
            # No symlink, no persisted release dir, no leftover .incoming.
            self.assertFalse((var / "hub-docs").exists())
            releases = var / "hub-docs.releases"
            if releases.exists():
                self.assertEqual(list(releases.iterdir()), [])

    def test_dry_run_validation_failure(self) -> None:
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            var = tmp / "var"
            var.mkdir()
            zip_path = tmp / "bad.zip"
            _make_bad_zip_with_index(
                zip_path,
                index={**_valid_index(), "schema_version": 99},
                pdf_files={"ok.pdf": _PDF_BYTES},
            )
            with self.assertRaises(hub_docs.HubDocsError) as ctx:
                hub_docs.install_hub_docs(
                    zip_path, var_dir=var, retention=3, dry_run=True,
                )
            self.assertEqual(ctx.exception.exit_code, 3)
            self.assertFalse((var / "hub-docs").exists())


# ---------------------------------------------------------------------------
# Rollback
# ---------------------------------------------------------------------------


class RollbackTest(unittest.TestCase):

    def _setup_two_releases(self, tmp: Path) -> Path:
        var = tmp / "var"
        var.mkdir()
        zip_a = _make_fixture_zip(
            tmp / "a", [("a.pdf", "C", "A", "en")],
            built_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        zip_b = _make_fixture_zip(
            tmp / "b", [("b.pdf", "C", "B", "en")],
            built_at=datetime(2026, 2, 1, tzinfo=timezone.utc),
        )
        hub_docs.install_hub_docs(zip_a, var_dir=var, retention=3)
        hub_docs.install_hub_docs(zip_b, var_dir=var, retention=3)
        return var

    def test_no_flag_two_releases(self) -> None:
        with tempfile.TemporaryDirectory() as t:
            var = self._setup_two_releases(Path(t))
            result = hub_docs.rollback_hub_docs(var_dir=var, to_id=None)
            self.assertEqual(result["release_id"], "20260101T000000Z")
            self.assertEqual(result["previous"], "20260201T000000Z")
            self.assertNotIn("noop", result)
            self.assertEqual(
                os.readlink(var / "hub-docs"),
                "hub-docs.releases/20260101T000000Z",
            )

    def test_no_flag_one_release(self) -> None:
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            var = tmp / "var"
            var.mkdir()
            zip_a = _make_fixture_zip(
                tmp, [("a.pdf", "C", "A", "en")],
                built_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            )
            hub_docs.install_hub_docs(zip_a, var_dir=var, retention=3)
            with self.assertRaises(hub_docs.HubDocsError) as ctx:
                hub_docs.rollback_hub_docs(var_dir=var, to_id=None)
            self.assertEqual(ctx.exception.exit_code, 4)
            self.assertIn("only one release", str(ctx.exception))

    def test_to_missing(self) -> None:
        with tempfile.TemporaryDirectory() as t:
            var = self._setup_two_releases(Path(t))
            with self.assertRaises(hub_docs.HubDocsError) as ctx:
                hub_docs.rollback_hub_docs(
                    var_dir=var, to_id="20991231T000000Z",
                )
            self.assertEqual(ctx.exception.exit_code, 4)
            msg = str(ctx.exception)
            self.assertIn("not found", msg)
            self.assertIn("20260101T000000Z", msg)
            self.assertIn("20260201T000000Z", msg)

    def test_to_current_is_noop(self) -> None:
        with tempfile.TemporaryDirectory() as t:
            var = self._setup_two_releases(Path(t))
            link_before = os.readlink(var / "hub-docs")
            result = hub_docs.rollback_hub_docs(
                var_dir=var, to_id="20260201T000000Z",
            )
            self.assertEqual(result["release_id"], "20260201T000000Z")
            self.assertEqual(result["previous"], "20260201T000000Z")
            self.assertTrue(result["noop"])
            self.assertEqual(os.readlink(var / "hub-docs"), link_before)

    def test_to_other(self) -> None:
        with tempfile.TemporaryDirectory() as t:
            var = self._setup_two_releases(Path(t))
            result = hub_docs.rollback_hub_docs(
                var_dir=var, to_id="20260101T000000Z",
            )
            self.assertEqual(result["release_id"], "20260101T000000Z")
            self.assertEqual(result["previous"], "20260201T000000Z")
            self.assertNotIn("noop", result)

    def test_rollback_self_prune_trap(self) -> None:
        """A,B,C,D installed at retention=3; rollback to B; install E at
        retention=3. B (now active) MUST survive the prune.

        Without the active-target-exclusion rule in _prune, the
        lex-newest-3 selection (C,D,E) would prune B.
        """
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            var = tmp / "var"
            var.mkdir()
            ids: list[str] = []
            for i, day in enumerate(["01", "02", "03", "04"], start=1):
                z = _make_fixture_zip(
                    tmp / f"a{i}", [(f"f{i}.pdf", "C", f"T{i}", "en")],
                    built_at=datetime(2026, 1, int(day), tzinfo=timezone.utc),
                )
                r = hub_docs.install_hub_docs(z, var_dir=var, retention=3)
                ids.append(r["release_id"])
            # ids = [A, B, C, D]; A was pruned on the 4th install.
            target = ids[1]   # B
            self.assertTrue(
                (var / "hub-docs.releases" / target).is_dir(),
                f"setup broken: {target} should still exist",
            )
            hub_docs.rollback_hub_docs(var_dir=var, to_id=target)

            zip_e = _make_fixture_zip(
                tmp / "e", [("fE.pdf", "C", "E", "en")],
                built_at=datetime(2026, 1, 5, tzinfo=timezone.utc),
            )
            result = hub_docs.install_hub_docs(zip_e, var_dir=var, retention=3)
            # After install E with retention=3, candidates are
            # {C, D, E} excluding active B. Total dirs = 4 (B,C,D,E),
            # retention=3 means we need to prune one — C, the oldest
            # non-active. B (active) survives.
            self.assertIn("20260103T000000Z", result["pruned"])  # C pruned
            self.assertTrue(
                (var / "hub-docs.releases" / target).is_dir(),
                f"trap: active rolled-back release {target} was pruned",
            )


# ---------------------------------------------------------------------------
# CLI dispatch & refusal
# ---------------------------------------------------------------------------


class CliDispatchSmokeTest(unittest.TestCase):
    """One smoke test per subcommand to confirm argparse routing reaches
    the right _cmd_* dispatcher.
    """

    def test_argparse_smoke_install(self) -> None:
        import civicmesh
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            var = tmp / "var"
            var.mkdir()
            zip_path = _make_fixture_zip(
                tmp, [("a.pdf", "C", "A", "en")],
                built_at=datetime(2026, 4, 1, 14, 32, 0, tzinfo=timezone.utc),
            )
            old_argv = sys.argv
            sys.argv = ["civicmesh", "install-hub-docs", str(zip_path), "--dry-run"]
            try:
                # Patch _hub_docs_var_dir directly: civicmesh.main()
                # reassigns _MODE / _PROJECT_ROOT during dispatch from
                # the binary path, so patching those globals doesn't
                # survive into the dispatcher.
                with patch("civicmesh._hub_docs_var_dir", return_value=var), \
                     _capture() as (out, _):
                    civicmesh.main()
                self.assertIn("dry_run release_id=20260401T143200Z", out.getvalue())
            finally:
                sys.argv = old_argv

    def test_argparse_smoke_rollback(self) -> None:
        import civicmesh
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            var = tmp / "var"
            var.mkdir()
            zip_a = _make_fixture_zip(
                tmp / "a", [("a.pdf", "C", "A", "en")],
                built_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            )
            zip_b = _make_fixture_zip(
                tmp / "b", [("b.pdf", "C", "B", "en")],
                built_at=datetime(2026, 2, 1, tzinfo=timezone.utc),
            )
            hub_docs.install_hub_docs(zip_a, var_dir=var, retention=3)
            hub_docs.install_hub_docs(zip_b, var_dir=var, retention=3)
            old_argv = sys.argv
            sys.argv = ["civicmesh", "rollback-hub-docs"]
            try:
                with patch("civicmesh._hub_docs_var_dir", return_value=var), \
                     _capture() as (out, _):
                    civicmesh.main()
                self.assertIn(
                    "rolled_back release_id=20260101T000000Z",
                    out.getvalue(),
                )
            finally:
                sys.argv = old_argv


class LoadRetentionMatrixTest(unittest.TestCase):

    def _ns(self, **kwargs) -> argparse.Namespace:
        defaults = {"config": None}
        defaults.update(kwargs)
        return argparse.Namespace(**defaults)

    def test_fresh_node_no_config_no_default_returns_3(self) -> None:
        import civicmesh
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            with patch("civicmesh._MODE", "dev"), \
                 patch("civicmesh._PROJECT_ROOT", tmp):
                self.assertEqual(
                    civicmesh._load_retention(self._ns()), 3
                )

    def test_broken_default_errors(self) -> None:
        """No --config, but default config exists and is malformed.

        Must raise HubDocsError(exit 1), not silently fall back.
        """
        import civicmesh
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            (tmp / "config.toml").write_text("this = is = not valid toml\n")
            with patch("civicmesh._MODE", "dev"), \
                 patch("civicmesh._PROJECT_ROOT", tmp):
                with self.assertRaises(hub_docs.HubDocsError) as ctx:
                    civicmesh._load_retention(self._ns())
            self.assertEqual(ctx.exception.exit_code, 1)
            self.assertIn(str(tmp / "config.toml"), str(ctx.exception))

    def test_broken_explicit_errors(self) -> None:
        """--config <bad path> must raise, not fall back to default."""
        import civicmesh
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            bad = tmp / "missing.toml"   # does not exist
            with patch("civicmesh._MODE", "dev"), \
                 patch("civicmesh._PROJECT_ROOT", tmp):
                with self.assertRaises(hub_docs.HubDocsError) as ctx:
                    civicmesh._load_retention(self._ns(config=str(bad)))
            self.assertEqual(ctx.exception.exit_code, 1)
            self.assertIn(str(bad), str(ctx.exception))


class RefusalRoutingTest(unittest.TestCase):
    """The new subcommands route through _check_refusals like every
    other subcommand.

    These tests assert the *routing* — that calling main() with a
    config path inside PROD_TREE while in dev mode triggers the same
    refusal exit 10 path that other subcommands use.
    """

    def _expect_wrong_mode_exit(self, argv: list[str]) -> None:
        import civicmesh
        old = sys.argv
        sys.argv = argv
        try:
            with patch("civicmesh._MODE", "dev"), \
                 _capture() as (_, err):
                with self.assertRaises(SystemExit) as ctx:
                    civicmesh.main()
            self.assertEqual(ctx.exception.code, civicmesh.EXIT_WRONG_MODE)
            self.assertIn("dev binary", err.getvalue())
        finally:
            sys.argv = old

    def test_install_refuses_dev_with_prod_config(self) -> None:
        with tempfile.TemporaryDirectory() as t:
            zip_path = Path(t) / "fake.zip"
            zip_path.write_bytes(b"\x00")
            self._expect_wrong_mode_exit([
                "civicmesh",
                "--config", "/usr/local/civicmesh/etc/config.toml",
                "install-hub-docs", str(zip_path),
            ])

    def test_rollback_refuses_dev_with_prod_config(self) -> None:
        self._expect_wrong_mode_exit([
            "civicmesh",
            "--config", "/usr/local/civicmesh/etc/config.toml",
            "rollback-hub-docs",
        ])


if __name__ == "__main__":
    unittest.main()
