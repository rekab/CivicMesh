"""Install and rollback for hub-docs releases.

See `docs/hub-reference-library.md` §3 (validation contract), §7
(install procedure, CLI conventions, prod-root refusal, rollback,
pruning), §9 (DEV vs PROD path resolution).

Public API:
    install_hub_docs(zip_path, *, var_dir, retention, dry_run=False) -> dict
    rollback_hub_docs(*, var_dir, to_id) -> dict

Both raise HubDocsError on failure. Callers in civicmesh.py format the
error and exit with `e.exit_code`.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path


_SCHEMA_VERSION = 1
_HUB_DOCS_RETENTION_DEFAULT = 3
_RELEASE_ID_RE = re.compile(r"^\d{8}T\d{6}Z$")


def _load_pdf_magic() -> bytes:
    """Reuse CIV-91's `_PDF_MAGIC` constant via importlib.

    Matches the pattern in tests/test_build_hub_docs.py:23-33. Keeps
    scripts/build_hub_docs.py as a standalone script (not a package).
    """
    repo_root = Path(__file__).resolve().parent
    script = repo_root / "scripts" / "build_hub_docs.py"
    spec = importlib.util.spec_from_file_location("_build_hub_docs", script)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("_build_hub_docs", mod)
    spec.loader.exec_module(mod)
    return mod._PDF_MAGIC


_PDF_MAGIC = _load_pdf_magic()


class HubDocsError(Exception):
    """Carries (message, exit_code). Caught by _cmd_* dispatchers."""

    def __init__(self, message: str, exit_code: int) -> None:
        super().__init__(message)
        self.message = message
        self.exit_code = exit_code

    def __str__(self) -> str:
        return self.message


# ---------------------------------------------------------------------------
# Install
# ---------------------------------------------------------------------------


def install_hub_docs(
    zip_path: Path,
    *,
    var_dir: Path,
    retention: int,
    dry_run: bool = False,
) -> dict:
    """Install a hub-docs release zip.

    On success returns:
        {"release_id": str, "previous": str | None, "pruned": list[str]}
    On dry-run returns:
        {"release_id": str, "docs": int}

    Raises HubDocsError on any failure; the caller formats and exits.
    """
    if not zip_path.is_file():
        raise HubDocsError(f"zip not found: {zip_path}", exit_code=1)

    # Open + validate as a zip; peek index.json to derive release_id.
    try:
        zf = zipfile.ZipFile(zip_path)
    except zipfile.BadZipFile as e:
        raise HubDocsError(
            f"not a valid zip: {zip_path} ({e})", exit_code=2
        )

    try:
        index = _peek_index_json(zf)
    finally:
        # We close after extraction below; leave it open for now and
        # close in the finally there. Reopen pattern keeps this
        # function readable.
        zf.close()

    release_id = _release_id_from_built_at(index["built_at"])
    releases_root = var_dir / "hub-docs.releases"
    final_release = releases_root / release_id
    incoming = releases_root / f"{release_id}.incoming"

    # Step 4 collision check: refuse if final release already exists.
    if final_release.exists():
        raise HubDocsError(
            f"release {release_id} already installed at {final_release}",
            exit_code=4,
        )

    # Step 4 leftover-incoming recovery.
    if incoming.exists():
        print(
            f"civicmesh install-hub-docs: cleared stale incoming "
            f"directory at {incoming}",
            file=sys.stderr,
        )
        shutil.rmtree(incoming)

    releases_root.mkdir(parents=True, exist_ok=True)

    # Step 4 + zip-slip: extract members after path-walking each one.
    try:
        _extract_to_incoming(zip_path, incoming)
    except HubDocsError:
        if incoming.exists():
            shutil.rmtree(incoming)
        raise
    except OSError as e:
        if incoming.exists():
            shutil.rmtree(incoming)
        raise HubDocsError(
            f"extraction failed: {e}", exit_code=1
        )

    # Step 5 validation.
    try:
        _validate_extracted(incoming)
    except HubDocsError:
        shutil.rmtree(incoming)
        raise

    if dry_run:
        doc_count = sum(
            len(c["docs"]) for c in index["categories"]
        )
        shutil.rmtree(incoming)
        return {"release_id": release_id, "docs": doc_count}

    # The zip uses a `hub-docs/` prefix for ergonomic extraction; the
    # on-disk release directory (per docs/hub-reference-library.md §7
    # "INSTALL PROCESS") holds `index.json` and the PDFs directly.
    # Bridge the two before promotion.
    try:
        _lift_inner_hub_docs(incoming)
    except HubDocsError:
        shutil.rmtree(incoming)
        raise

    # Step 7's symlink read must happen before the swap: that's the
    # release we're rolling away from, and it's the rollback safety
    # net. Pruning protects it alongside the new release so a
    # rollback-then-install doesn't delete the rolled-back-to release.
    previous = _read_current_release_id(var_dir)

    # Step 6: promote .incoming/ to <release_id>/ — atomic POSIX rename.
    os.replace(incoming, final_release)

    # Step 7: swap symlink atomically.
    _atomic_symlink_swap(var_dir, release_id)

    # Step 8: prune. Protect both the new release and the prior
    # active target (the rollback safety net).
    protect = {release_id}
    if previous is not None:
        protect.add(previous)
    pruned = _prune(releases_root, retention, protect=protect)

    return {"release_id": release_id, "previous": previous, "pruned": pruned}


def _peek_index_json(zf: zipfile.ZipFile) -> dict:
    try:
        info = zf.getinfo("hub-docs/index.json")
    except KeyError:
        raise HubDocsError(
            "zip is missing hub-docs/index.json", exit_code=2
        )
    try:
        with zf.open(info) as f:
            data = json.loads(f.read().decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise HubDocsError(
            f"hub-docs/index.json is not valid JSON: {e}", exit_code=2
        )
    if not isinstance(data, dict) or "built_at" not in data:
        raise HubDocsError(
            "hub-docs/index.json missing required 'built_at' field",
            exit_code=2,
        )
    return data


def _release_id_from_built_at(built_at: str) -> str:
    """Derive YYYYMMDDTHHMMSSZ release_id from ISO-8601 built_at."""
    # built_at format from CIV-91: "2026-04-01T14:32:00Z".
    m = re.match(
        r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})Z$", built_at
    )
    if not m:
        raise HubDocsError(
            f"index.json built_at not parseable: {built_at!r}",
            exit_code=2,
        )
    return f"{m[1]}{m[2]}{m[3]}T{m[4]}{m[5]}{m[6]}Z"


def _extract_to_incoming(zip_path: Path, incoming: Path) -> None:
    """Extract zip into `incoming`, with zip-slip protection.

    Walks every member name and rejects anything that resolves
    outside the staging directory before any byte is written.
    """
    incoming.mkdir(parents=True)
    incoming_resolved = incoming.resolve()
    with zipfile.ZipFile(zip_path) as zf:
        for member in zf.namelist():
            target = (incoming / member).resolve()
            try:
                target.relative_to(incoming_resolved)
            except ValueError:
                raise HubDocsError(
                    f"unsafe path in zip: {member!r}", exit_code=2
                )
        zf.extractall(incoming)


def _lift_inner_hub_docs(incoming: Path) -> None:
    """Move `incoming/hub-docs/*` up to `incoming/` and remove the inner dir.

    The zip's `hub-docs/` prefix is namespacing for clean extraction;
    docs/hub-reference-library.md §7 "INSTALL PROCESS" specifies a
    release directory with `index.json` directly under `<release_id>/`.
    The web server's slug-discovery walk then sees
    `<var>/hub-docs/index.json` (via the symlink) and surfaces the
    library at `/var/hub-docs/`.
    """
    inner = incoming / "hub-docs"
    if not inner.is_dir():
        return
    for child in inner.iterdir():
        target = incoming / child.name
        if target.exists():
            raise HubDocsError(
                f"layout collision: {child.name!r} already exists at "
                f"the top of incoming directory",
                exit_code=3,
            )
        shutil.move(str(child), str(target))
    inner.rmdir()


def _validate_extracted(incoming: Path) -> None:
    """Apply docs/hub-reference-library.md §3 "THE CONTRACT" install-time validation rules 1-5 to the extracted dir."""
    index_path = incoming / "hub-docs" / "index.json"
    if not index_path.is_file():
        raise HubDocsError(
            "extracted directory has no hub-docs/index.json",
            exit_code=3,
        )
    with open(index_path, "rb") as f:
        try:
            index = json.loads(f.read().decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            raise HubDocsError(
                f"index.json is not valid JSON: {e}", exit_code=3
            )

    # Rule 1: schema_version recognized.
    if index.get("schema_version") != _SCHEMA_VERSION:
        raise HubDocsError(
            f"unsupported schema_version "
            f"{index.get('schema_version')!r} "
            f"(this civicmesh expects {_SCHEMA_VERSION})",
            exit_code=3,
        )

    if "categories" not in index or not isinstance(
        index["categories"], list
    ):
        raise HubDocsError(
            "index.json missing 'categories' array", exit_code=3
        )

    declared: dict[str, int] = {}   # filename -> declared size_bytes
    for category in index["categories"]:
        for doc in category.get("docs", []):
            filename = doc.get("filename")
            size = doc.get("size_bytes")
            if not isinstance(filename, str) or not isinstance(size, int):
                raise HubDocsError(
                    f"index.json doc entry missing filename or "
                    f"size_bytes: {doc!r}",
                    exit_code=3,
                )
            declared[filename] = size

    hub_docs_dir = incoming / "hub-docs"
    present = {
        p.name
        for p in hub_docs_dir.iterdir()
        if p.is_file() and p.name != "index.json"
    }

    # Rule 2: every declared filename exists.
    missing = set(declared) - present
    if missing:
        raise HubDocsError(
            f"index lists files missing from zip: {sorted(missing)}",
            exit_code=3,
        )

    # Rule 5: no orphans (files in hub-docs/ not declared in index).
    orphans = present - set(declared)
    if orphans:
        raise HubDocsError(
            f"orphan files in hub-docs/ not listed in index: "
            f"{sorted(orphans)}",
            exit_code=3,
        )

    # Rules 3 & 4: per-file size + magic bytes.
    for filename, declared_size in declared.items():
        path = hub_docs_dir / filename
        actual_size = path.stat().st_size
        if actual_size != declared_size:
            raise HubDocsError(
                f"size mismatch for {filename}: "
                f"declared {declared_size}, actual {actual_size}",
                exit_code=3,
            )
        with open(path, "rb") as f:
            head = f.read(len(_PDF_MAGIC))
        if head != _PDF_MAGIC:
            raise HubDocsError(
                f"file does not start with %PDF- magic bytes: "
                f"{filename}",
                exit_code=3,
            )


def _read_current_release_id(var_dir: Path) -> str | None:
    link = var_dir / "hub-docs"
    if not link.is_symlink():
        return None
    target = os.readlink(link)
    return Path(target).name


def _atomic_symlink_swap(var_dir: Path, release_id: str) -> None:
    """Atomically point <var_dir>/hub-docs at hub-docs.releases/<release_id>.

    Uses a relative target (so the symlink survives moving var_dir
    around). Defends against a stale `hub-docs.new` from a killed
    prior swap by unlinking unconditionally before symlinking.
    """
    new_link = var_dir / "hub-docs.new"
    final_link = var_dir / "hub-docs"
    target = Path("hub-docs.releases") / release_id
    if new_link.is_symlink() or new_link.exists():
        new_link.unlink()
    new_link.symlink_to(target)
    os.replace(new_link, final_link)


def _list_release_ids(releases_root: Path) -> list[str]:
    """Return release_ids directly under releases_root.

    Filters by the `^\\d{8}T\\d{6}Z$` format so stray
    `<release_id>.incoming/` siblings (or any other unexpected dir)
    are invisible to rollback / pruning.
    """
    if not releases_root.is_dir():
        return []
    return [
        p.name
        for p in releases_root.iterdir()
        if p.is_dir() and _RELEASE_ID_RE.match(p.name)
    ]


def _prune(
    releases_root: Path, retention: int, *, protect: set[str]
) -> list[str]:
    """Keep the lex-greatest N release_ids, never deleting protected ones.

    The active symlink target — both the just-installed release AND
    the prior active target — is excluded unconditionally. Without
    protecting the prior active, a rollback-then-install sequence
    would prune the rolled-back-to release the next time install
    ran (the "rollback-self-prune trap").

    If `len(protect) > retention`, no pruning happens — protect is a
    hard floor, retention a soft target.
    """
    ids = sorted(_list_release_ids(releases_root))
    protected_present = protect & set(ids)
    candidates = sorted(set(ids) - protected_present)
    pruned: list[str] = []
    while candidates and (
        len(candidates) + len(protected_present) > retention
    ):
        oldest = candidates.pop(0)
        shutil.rmtree(releases_root / oldest)
        pruned.append(oldest)
    return pruned


# ---------------------------------------------------------------------------
# Rollback
# ---------------------------------------------------------------------------


def rollback_hub_docs(*, var_dir: Path, to_id: str | None) -> dict:
    """Roll the hub-docs symlink back to a different release.

    With `to_id=None`, picks the lex-greatest release that is not the
    current target. With `to_id` set, validates and uses that id.

    Returns:
        success: {"release_id": str, "previous": str}
        no-op:   {"release_id": str, "previous": str, "noop": True}

    Raises HubDocsError when no rollback is possible.
    """
    releases_root = var_dir / "hub-docs.releases"
    current = _read_current_release_id(var_dir)
    if current is None:
        raise HubDocsError(
            "no hub-docs installed (no symlink at "
            f"{var_dir / 'hub-docs'})",
            exit_code=4,
        )

    available = sorted(_list_release_ids(releases_root))

    if to_id is None:
        candidates = [i for i in available if i != current]
        if not candidates:
            raise HubDocsError(
                f"only one release present ({current}); nothing to "
                "roll back to",
                exit_code=4,
            )
        target = candidates[-1]   # lex-greatest non-current
    else:
        if to_id not in available:
            raise HubDocsError(
                f"release {to_id!r} not found; available: "
                f"{available}",
                exit_code=4,
            )
        target = to_id

    if target == current:
        # --to <current>: documented no-op, exit 0.
        return {
            "release_id": current,
            "previous": current,
            "noop": True,
        }

    _atomic_symlink_swap(var_dir, target)
    return {"release_id": target, "previous": current}
