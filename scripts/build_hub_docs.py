"""Build a hub-docs release zip from a source directory.

Standalone dev-machine script. Reads a manifest.toml + PDFs from
the source dir and produces an atomic release zip whose internal
shape is the contract documented in docs/hub-reference-library.md
sections 3, 4, 5, and 6.

Invocations:

    uv run python scripts/build_hub_docs.py \\
        --source content/hub-docs/ --out out/

    uv run python scripts/build_hub_docs.py \\
        --source content/hub-docs/ --validate
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tomllib
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


_PDF_MAGIC = b"%PDF-"
_LANG_RE = re.compile(r"^[a-z]{2}$")
_PUBLISHED_RES = (
    re.compile(r"^\d{4}$"),
    re.compile(r"^\d{4}-\d{2}$"),
    re.compile(r"^\d{4}-\d{2}-\d{2}$"),
)
_RECOGNIZED_TOP_KEYS = {"title", "source_label", "note", "doc"}
_RECOGNIZED_DOC_REQUIRED = ("category", "title", "file", "lang")
_RECOGNIZED_DOC_OPTIONAL = ("published",)
_RECOGNIZED_DOC_KEYS = (
    set(_RECOGNIZED_DOC_REQUIRED) | set(_RECOGNIZED_DOC_OPTIONAL)
)
_SCHEMA_VERSION = 1


class ManifestError(Exception):
    """Validation error in the manifest. Carries doc-position context."""

    def __init__(
        self,
        message: str,
        *,
        doc_index: int | None = None,
        doc_title: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.doc_index = doc_index
        self.doc_title = doc_title

    def render(self) -> str:
        if self.doc_index is None:
            return self.message
        title = self.doc_title if self.doc_title is not None else "<untitled>"
        return f'{self.message} (doc {self.doc_index} "{title}")'


@dataclass
class DocFacts:
    filename: str
    title: str
    lang: str
    published: str | None
    last_reviewed: str
    size_bytes: int
    category: str
    source_path: Path


def parse_manifest(path: Path) -> dict:
    """Parse and validate the TOML manifest. No filesystem touches."""
    with open(path, "rb") as f:
        data = tomllib.load(f)

    # Rule 1: required top-level keys + types
    for key in ("source_label", "note", "doc"):
        if key not in data:
            raise ManifestError(f"missing required top-level key {key!r}")
    if not isinstance(data["source_label"], str):
        raise ManifestError("top-level 'source_label' must be a string")
    if not isinstance(data["note"], str):
        raise ManifestError("top-level 'note' must be a string")
    if not isinstance(data["doc"], list):
        raise ManifestError("top-level 'doc' must be an array of tables")

    unknown_top = set(data) - _RECOGNIZED_TOP_KEYS
    if unknown_top:
        raise ManifestError(
            f"unknown top-level key(s): {sorted(unknown_top)}"
        )

    if "title" in data:
        title_val = data["title"]
        if not isinstance(title_val, str) or not title_val.strip():
            raise ManifestError(
                "top-level 'title' must be a non-empty string "
                f"(got {title_val!r})"
            )

    seen_files: dict[str, list[int]] = {}
    for i, doc in enumerate(data["doc"], start=1):
        if not isinstance(doc, dict):
            raise ManifestError(
                "[[doc]] entry is not a table",
                doc_index=i,
            )
        title = doc.get("title") if isinstance(doc.get("title"), str) else None

        # Rule 2: required keys + correct type
        for key in _RECOGNIZED_DOC_REQUIRED:
            if key not in doc:
                raise ManifestError(
                    f"missing required key {key!r}",
                    doc_index=i,
                    doc_title=title,
                )
            if not isinstance(doc[key], str):
                raise ManifestError(
                    f"key {key!r} must be a string",
                    doc_index=i,
                    doc_title=title,
                )

        # Rule 2 (optional keys): type check
        if "published" in doc and not isinstance(doc["published"], str):
            raise ManifestError(
                "key 'published' must be a string",
                doc_index=i,
                doc_title=title,
            )

        # Rule 3: unknown keys
        unknown_doc = set(doc) - _RECOGNIZED_DOC_KEYS
        if unknown_doc:
            raise ManifestError(
                f"unknown key(s): {sorted(unknown_doc)}",
                doc_index=i,
                doc_title=title,
            )

        # Rule 4b: filename safety (prose-only constraint from §4
        # schema description for [[doc]].file).
        filename = doc["file"]
        if "/" in filename or "\\" in filename:
            raise ManifestError(
                "'file' must be a bare filename (no path separators); "
                f"got {filename!r}",
                doc_index=i,
                doc_title=title,
            )
        if filename in (".", ".."):
            raise ManifestError(
                f"'file' must not be a relative-path token; got {filename!r}",
                doc_index=i,
                doc_title=title,
            )
        if not filename.endswith(".pdf"):
            raise ManifestError(
                f"'file' must end in '.pdf'; got {filename!r}",
                doc_index=i,
                doc_title=title,
            )

        # Rule 6: lang format
        if not _LANG_RE.match(doc["lang"]):
            raise ManifestError(
                f"'lang' must match ^[a-z]{{2}}$, got {doc['lang']!r}",
                doc_index=i,
                doc_title=title,
            )

        # Rule 7: published format
        if "published" in doc:
            published = doc["published"]
            if not any(p.match(published) for p in _PUBLISHED_RES):
                raise ManifestError(
                    "'published' must be YYYY, YYYY-MM, or YYYY-MM-DD; "
                    f"got {published!r}",
                    doc_index=i,
                    doc_title=title,
                )

        seen_files.setdefault(filename, []).append(i)

    # Rule 4: uniqueness
    dupes = sorted(
        (f, idxs) for f, idxs in seen_files.items() if len(idxs) > 1
    )
    if dupes:
        f, idxs = dupes[0]
        raise ManifestError(
            f"duplicate 'file' value {f!r} appears at doc indexes {idxs}",
        )

    return data


def validate_sources(manifest: dict, source_dir: Path) -> list[DocFacts]:
    """Rule 5: each [[doc]].file exists and starts with %PDF-.

    Also derives size_bytes and last_reviewed from filesystem facts.
    """
    facts: list[DocFacts] = []
    for i, doc in enumerate(manifest["doc"], start=1):
        title = doc["title"]
        filename = doc["file"]
        path = source_dir / filename
        if not path.is_file():
            raise ManifestError(
                f"source file not found: {path}",
                doc_index=i,
                doc_title=title,
            )
        with open(path, "rb") as f:
            head = f.read(len(_PDF_MAGIC))
        if head != _PDF_MAGIC:
            raise ManifestError(
                f"file does not start with {_PDF_MAGIC!r}: {path}",
                doc_index=i,
                doc_title=title,
            )
        st = path.stat()
        last_reviewed = datetime.fromtimestamp(
            st.st_mtime, tz=timezone.utc
        ).strftime("%Y-%m-%d")
        facts.append(DocFacts(
            filename=filename,
            title=title,
            lang=doc["lang"],
            published=doc.get("published"),
            last_reviewed=last_reviewed,
            size_bytes=st.st_size,
            category=doc["category"],
            source_path=path,
        ))
    return facts


def build_index(
    manifest: dict, doc_facts: list[DocFacts], built_at: datetime
) -> dict:
    """Construct the index.json dict per §3 of the design doc."""
    by_category: dict[str, list[DocFacts]] = {}
    for fact in doc_facts:
        by_category.setdefault(fact.category, []).append(fact)

    categories = []
    for category, facts in by_category.items():
        docs = []
        for fact in facts:
            entry: dict = {
                "filename": fact.filename,
                "title": fact.title,
                "lang": fact.lang,
            }
            if fact.published is not None:
                entry["published"] = fact.published
            entry["last_reviewed"] = fact.last_reviewed
            entry["size_bytes"] = fact.size_bytes
            docs.append(entry)
        categories.append({"name": category, "docs": docs})

    out: dict = {
        "schema_version": _SCHEMA_VERSION,
        "built_at": built_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    if "title" in manifest:
        out["title"] = manifest["title"]
    out["source_label"] = manifest["source_label"]
    out["note"] = manifest["note"]
    out["categories"] = categories
    return out


def write_zip(
    out_dir: Path,
    release_id: str,
    index: dict,
    doc_facts: list[DocFacts],
) -> Path:
    """Write the release zip atomically.

    Refuses to overwrite an existing final .zip. A leftover .tmp
    from a prior interrupted build is overwritten silently — the
    .tmp suffix exists precisely to make leftovers obvious to the
    operator.
    """
    final = out_dir / f"hub-docs-{release_id}.zip"
    tmp = out_dir / f"hub-docs-{release_id}.zip.tmp"
    if final.exists():
        raise FileExistsError(
            f"refusing to overwrite existing release artifact: {final}"
        )
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_STORED) as zf:
            zf.writestr(
                "hub-docs/index.json",
                json.dumps(index, indent=2, ensure_ascii=False) + "\n",
            )
            for fact in doc_facts:
                zf.write(fact.source_path, f"hub-docs/{fact.filename}")
        os.replace(tmp, final)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return final


def run(
    args: argparse.Namespace,
    *,
    built_at: datetime | None = None,
) -> int:
    """Execute the validate-or-build flow. Test-friendly entry point."""
    source_dir = Path(args.source)
    manifest_path = source_dir / "manifest.toml"
    if not manifest_path.is_file():
        print(
            f"build_hub_docs: error: manifest not found: {manifest_path}",
            file=sys.stderr,
        )
        return 2

    try:
        manifest = parse_manifest(manifest_path)
        doc_facts = validate_sources(manifest, source_dir)
    except ManifestError as e:
        print(f"build_hub_docs: error: {e.render()}", file=sys.stderr)
        return 1
    except tomllib.TOMLDecodeError as e:
        print(
            f"build_hub_docs: error: invalid TOML in {manifest_path}: {e}",
            file=sys.stderr,
        )
        return 1

    if args.validate:
        return 0

    if built_at is None:
        built_at = datetime.now(timezone.utc)
    release_id = built_at.strftime("%Y%m%dT%H%M%SZ")
    index = build_index(manifest, doc_facts, built_at)
    out_dir = Path(args.out)
    try:
        path = write_zip(out_dir, release_id, index, doc_facts)
    except FileExistsError as e:
        print(f"build_hub_docs: error: {e}", file=sys.stderr)
        return 1
    print(str(path))
    return 0


def _make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="build_hub_docs",
        description=(
            "Build a hub-docs release zip from a manifest + PDFs. "
            "See docs/hub-reference-library.md sections 3-6."
        ),
    )
    parser.add_argument(
        "--source", required=True,
        help="source directory containing manifest.toml and PDFs",
    )
    g = parser.add_mutually_exclusive_group(required=True)
    g.add_argument(
        "--out",
        help="output directory for the release zip (build mode)",
    )
    g.add_argument(
        "--validate", action="store_true",
        help="parse and validate without producing a zip",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _make_parser()
    args = parser.parse_args(argv)
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
