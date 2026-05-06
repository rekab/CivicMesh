"""Tests for scripts/build_hub_docs.py.

The script lives under scripts/ rather than as a top-level module,
so import it via importlib from its file path.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
import zipfile
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT_PATH = _REPO_ROOT / "scripts" / "build_hub_docs.py"


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "build_hub_docs", _SCRIPT_PATH
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["build_hub_docs"] = mod
    spec.loader.exec_module(mod)
    return mod


bhd = _load_module()


_PDF_BYTES = b"%PDF-1.4\n%dummy\n%%EOF\n"


def _make_pdf(path: Path, content: bytes = _PDF_BYTES) -> None:
    path.write_bytes(content)


@contextmanager
def _capture():
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        yield out, err


def _run(*argv: str) -> tuple[int, str, str]:
    with _capture() as (out, err):
        try:
            rc = bhd.main(list(argv))
        except SystemExit as e:
            rc = int(e.code) if e.code is not None else 0
    return rc, out.getvalue(), err.getvalue()


def _make_source(tmp: Path, manifest_text: str, pdfs: dict[str, bytes]) -> Path:
    src = tmp / "src"
    src.mkdir()
    (src / "manifest.toml").write_text(manifest_text)
    for name, body in pdfs.items():
        (src / name).write_bytes(body)
    return src


_VALID_MANIFEST = """\
source_label = "Seattle Emergency Hubs"
note = "test note"

[[doc]]
category = "Water"
title = "Disinfection of Drinking Water"
file = "water-disinfection.pdf"
lang = "en"

[[doc]]
category = "Water"
title = "Drinking Water from Your Water Heater"
file = "water-heater.pdf"
lang = "en"
published = "2023-02"

[[doc]]
category = "Sanitation"
title = "Emergency Toilet (ES)"
file = "toilet-es.pdf"
lang = "es"
published = "2025"
"""

_VALID_PDFS = {
    "water-disinfection.pdf": _PDF_BYTES,
    "water-heater.pdf": _PDF_BYTES,
    "toilet-es.pdf": _PDF_BYTES,
}


class HappyPathBuildTest(unittest.TestCase):

    def test_build_produces_valid_zip(self) -> None:
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            src = _make_source(tmp, _VALID_MANIFEST, _VALID_PDFS)
            out = tmp / "out"

            built_at = datetime(2026, 4, 1, 14, 32, 0, tzinfo=timezone.utc)
            args = bhd._make_parser().parse_args([
                "--source", str(src), "--out", str(out),
            ])
            with _capture():
                rc = bhd.run(args, built_at=built_at)
            self.assertEqual(rc, 0)

            release_id = "20260401T143200Z"
            zip_path = out / f"hub-docs-{release_id}.zip"
            self.assertTrue(zip_path.is_file())

            with zipfile.ZipFile(zip_path) as zf:
                names = set(zf.namelist())
                expected = {
                    "hub-docs/index.json",
                    "hub-docs/water-disinfection.pdf",
                    "hub-docs/water-heater.pdf",
                    "hub-docs/toilet-es.pdf",
                }
                self.assertEqual(names, expected)

                # Every PDF starts with %PDF-
                for name in names - {"hub-docs/index.json"}:
                    head = zf.read(name)[:5]
                    self.assertEqual(head, b"%PDF-")

                index = json.loads(zf.read("hub-docs/index.json"))

            self.assertEqual(index["schema_version"], 1)
            self.assertEqual(index["built_at"], "2026-04-01T14:32:00Z")
            self.assertEqual(index["source_label"], "Seattle Emergency Hubs")
            self.assertEqual(index["note"], "test note")

            cats = index["categories"]
            # First-appearance order: Water, then Sanitation
            self.assertEqual([c["name"] for c in cats], ["Water", "Sanitation"])

            water_docs = cats[0]["docs"]
            self.assertEqual(
                [d["filename"] for d in water_docs],
                ["water-disinfection.pdf", "water-heater.pdf"],
            )
            # First doc has no published; second does.
            self.assertNotIn("published", water_docs[0])
            self.assertEqual(water_docs[1]["published"], "2023-02")

            sanitation = cats[1]["docs"][0]
            self.assertEqual(sanitation["filename"], "toilet-es.pdf")
            self.assertEqual(sanitation["lang"], "es")
            self.assertEqual(sanitation["published"], "2025")

            # size_bytes matches actual file size
            for doc in water_docs + cats[1]["docs"]:
                self.assertEqual(doc["size_bytes"], len(_PDF_BYTES))
                # last_reviewed is YYYY-MM-DD shape
                self.assertRegex(doc["last_reviewed"], r"^\d{4}-\d{2}-\d{2}$")

    def test_last_reviewed_reflects_mtime(self) -> None:
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            src = _make_source(tmp, _VALID_MANIFEST, _VALID_PDFS)
            out = tmp / "out"

            # Set a known mtime on one of the PDFs
            target_mtime = datetime(
                2024, 7, 15, 12, 0, 0, tzinfo=timezone.utc
            ).timestamp()
            os.utime(src / "water-heater.pdf", (target_mtime, target_mtime))

            args = bhd._make_parser().parse_args([
                "--source", str(src), "--out", str(out),
            ])
            built_at = datetime(2026, 4, 1, 0, 0, 0, tzinfo=timezone.utc)
            with _capture():
                rc = bhd.run(args, built_at=built_at)
            self.assertEqual(rc, 0)

            with zipfile.ZipFile(
                out / "hub-docs-20260401T000000Z.zip"
            ) as zf:
                index = json.loads(zf.read("hub-docs/index.json"))

            water_heater = next(
                d
                for c in index["categories"]
                for d in c["docs"]
                if d["filename"] == "water-heater.pdf"
            )
            self.assertEqual(water_heater["last_reviewed"], "2024-07-15")


class HappyPathValidateTest(unittest.TestCase):

    def test_validate_exits_zero_no_writes(self) -> None:
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            src = _make_source(tmp, _VALID_MANIFEST, _VALID_PDFS)
            out = tmp / "out"

            rc, stdout, stderr = _run(
                "--source", str(src), "--validate"
            )
            self.assertEqual(rc, 0, msg=stderr)
            self.assertFalse(out.exists())


# Failure-mode test data: (label, manifest_text, pdf_overrides,
# expected_substring_in_stderr).
#
# pdf_overrides == None means use _VALID_PDFS as-is.
_VALID_HEAD = """\
source_label = "Seattle Emergency Hubs"
note = "test note"

"""

_FAILURES = [
    # Rule 5: missing PDF on disk
    (
        "missing_pdf",
        _VALID_HEAD + """
[[doc]]
category = "Water"
title = "Missing"
file = "absent.pdf"
lang = "en"
""",
        {},
        "source file not found",
    ),
    # Rule 5: file exists but no %PDF- magic
    (
        "bad_magic",
        _VALID_HEAD + """
[[doc]]
category = "Water"
title = "Bogus"
file = "bogus.pdf"
lang = "en"
""",
        {"bogus.pdf": b"not-a-pdf"},
        "does not start with",
    ),
    # Rule 3: unknown key in [[doc]]
    (
        "unknown_doc_key",
        _VALID_HEAD + """
[[doc]]
category = "Water"
title = "Has note"
file = "ok.pdf"
lang = "en"
note = "should not be here"
""",
        {"ok.pdf": _PDF_BYTES},
        "unknown key",
    ),
    # Rule 4: duplicate file
    (
        "duplicate_file",
        _VALID_HEAD + """
[[doc]]
category = "Water"
title = "First"
file = "dup.pdf"
lang = "en"

[[doc]]
category = "Water"
title = "Second"
file = "dup.pdf"
lang = "en"
""",
        {"dup.pdf": _PDF_BYTES},
        "duplicate 'file'",
    ),
    # Rule 4b: path-separator (/)
    (
        "path_traversal_slash",
        _VALID_HEAD + """
[[doc]]
category = "Water"
title = "Bad"
file = "../foo.pdf"
lang = "en"
""",
        {},
        "no path separators",
    ),
    # Rule 4b: subdir slash
    (
        "subdir_slash",
        _VALID_HEAD + """
[[doc]]
category = "Water"
title = "Bad"
file = "subdir/foo.pdf"
lang = "en"
""",
        {},
        "no path separators",
    ),
    # Rule 4b: backslash
    (
        "backslash",
        _VALID_HEAD + """
[[doc]]
category = "Water"
title = "Bad"
file = "..\\\\foo.pdf"
lang = "en"
""",
        {},
        "no path separators",
    ),
    # Rule 4b: dotdot token
    (
        "dotdot_token",
        _VALID_HEAD + """
[[doc]]
category = "Water"
title = "Bad"
file = ".."
lang = "en"
""",
        {},
        "relative-path token",
    ),
    # Rule 4b: wrong extension
    (
        "wrong_extension",
        _VALID_HEAD + """
[[doc]]
category = "Water"
title = "Bad"
file = "weird.txt"
lang = "en"
""",
        {"weird.txt": _PDF_BYTES},
        "must end in '.pdf'",
    ),
    # Rule 4b: no extension at all
    (
        "no_extension",
        _VALID_HEAD + """
[[doc]]
category = "Water"
title = "Bad"
file = "no-extension"
lang = "en"
""",
        {"no-extension": _PDF_BYTES},
        "must end in '.pdf'",
    ),
    # Rule 6: lang uppercase
    (
        "lang_uppercase",
        _VALID_HEAD + """
[[doc]]
category = "Water"
title = "Bad"
file = "ok.pdf"
lang = "EN"
""",
        {"ok.pdf": _PDF_BYTES},
        "'lang' must match",
    ),
    # Rule 6: lang too long
    (
        "lang_too_long",
        _VALID_HEAD + """
[[doc]]
category = "Water"
title = "Bad"
file = "ok.pdf"
lang = "english"
""",
        {"ok.pdf": _PDF_BYTES},
        "'lang' must match",
    ),
    # Rule 7: published English month
    (
        "published_month_text",
        _VALID_HEAD + """
[[doc]]
category = "Water"
title = "Bad"
file = "ok.pdf"
lang = "en"
published = "Feb 2023"
""",
        {"ok.pdf": _PDF_BYTES},
        "'published' must be",
    ),
    # Rule 7: published 2-digit year
    (
        "published_short_year",
        _VALID_HEAD + """
[[doc]]
category = "Water"
title = "Bad"
file = "ok.pdf"
lang = "en"
published = "23-02"
""",
        {"ok.pdf": _PDF_BYTES},
        "'published' must be",
    ),
    # Rule 7: published slash separator
    (
        "published_slashes",
        _VALID_HEAD + """
[[doc]]
category = "Water"
title = "Bad"
file = "ok.pdf"
lang = "en"
published = "2023/02"
""",
        {"ok.pdf": _PDF_BYTES},
        "'published' must be",
    ),
    # Rule 2: missing required key
    (
        "missing_required_key",
        _VALID_HEAD + """
[[doc]]
category = "Water"
file = "ok.pdf"
lang = "en"
""",
        {"ok.pdf": _PDF_BYTES},
        "missing required key 'title'",
    ),
    # Rule 2: optional key wrong type (int instead of string)
    (
        "published_wrong_type",
        _VALID_HEAD + """
[[doc]]
category = "Water"
title = "Bad"
file = "ok.pdf"
lang = "en"
published = 2023
""",
        {"ok.pdf": _PDF_BYTES},
        "'published' must be a string",
    ),
    # Rule 1: missing required top-level key
    (
        "missing_top_level",
        """source_label = "Seattle"

[[doc]]
category = "Water"
title = "OK"
file = "ok.pdf"
lang = "en"
""",
        {"ok.pdf": _PDF_BYTES},
        "missing required top-level key 'note'",
    ),
    # Malformed TOML
    (
        "malformed_toml",
        "this is not = valid = toml = at all\n[[doc\n",
        {"ok.pdf": _PDF_BYTES},
        "invalid TOML",
    ),
]


class ValidationFailuresTest(unittest.TestCase):

    def test_failures_in_validate_mode(self) -> None:
        for label, manifest, pdf_overrides, expected in _FAILURES:
            with self.subTest(case=label):
                with tempfile.TemporaryDirectory() as t:
                    tmp = Path(t)
                    pdfs = (
                        dict(_VALID_PDFS) if pdf_overrides is None
                        else pdf_overrides
                    )
                    src = _make_source(tmp, manifest, pdfs)
                    rc, _, stderr = _run(
                        "--source", str(src), "--validate"
                    )
                    self.assertNotEqual(rc, 0, msg=f"{label}: {stderr!r}")
                    self.assertIn(expected, stderr, msg=f"{label}")

    def test_failures_in_build_mode(self) -> None:
        for label, manifest, pdf_overrides, expected in _FAILURES:
            with self.subTest(case=label):
                with tempfile.TemporaryDirectory() as t:
                    tmp = Path(t)
                    pdfs = (
                        dict(_VALID_PDFS) if pdf_overrides is None
                        else pdf_overrides
                    )
                    src = _make_source(tmp, manifest, pdfs)
                    out = tmp / "out"
                    rc, _, stderr = _run(
                        "--source", str(src), "--out", str(out),
                    )
                    self.assertNotEqual(rc, 0, msg=f"{label}: {stderr!r}")
                    self.assertIn(expected, stderr, msg=f"{label}")
                    # No zip should have been produced.
                    if out.exists():
                        self.assertEqual(list(out.glob("*.zip")), [])


class AtomicWriteTest(unittest.TestCase):

    def test_refuse_to_overwrite_existing_zip(self) -> None:
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            src = _make_source(tmp, _VALID_MANIFEST, _VALID_PDFS)
            out = tmp / "out"
            out.mkdir()

            built_at = datetime(2026, 4, 1, 14, 32, 0, tzinfo=timezone.utc)
            existing = out / "hub-docs-20260401T143200Z.zip"
            existing.write_bytes(b"PRE-EXISTING")

            args = bhd._make_parser().parse_args([
                "--source", str(src), "--out", str(out),
            ])
            with _capture() as (_, err):
                rc = bhd.run(args, built_at=built_at)
            self.assertNotEqual(rc, 0)
            self.assertIn("refusing to overwrite", err.getvalue())
            self.assertEqual(existing.read_bytes(), b"PRE-EXISTING")

    def test_leftover_tmp_does_not_block_build(self) -> None:
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            src = _make_source(tmp, _VALID_MANIFEST, _VALID_PDFS)
            out = tmp / "out"
            out.mkdir()

            built_at = datetime(2026, 4, 1, 14, 32, 0, tzinfo=timezone.utc)
            leftover = out / "hub-docs-20260401T143200Z.zip.tmp"
            leftover.write_bytes(b"LEFTOVER")

            args = bhd._make_parser().parse_args([
                "--source", str(src), "--out", str(out),
            ])
            with _capture():
                rc = bhd.run(args, built_at=built_at)
            self.assertEqual(rc, 0)

            final = out / "hub-docs-20260401T143200Z.zip"
            self.assertTrue(final.is_file())
            self.assertFalse(leftover.exists())


if __name__ == "__main__":
    unittest.main()
