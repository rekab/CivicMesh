"""civicmesh promote: ship dev's main to /usr/local/civicmesh/app/.

Runs entirely via shell-outs (git, tar, sudo, uv, systemctl). Does not
import code from the prod tree — a broken prod tree is repairable by
re-running promote.

Six fail-fast pre-flight checks (no --force):
  1. pyproject [project].name == "civicmesh"
  2. local main branch exists
  3. HEAD is on main
  4. working tree clean
  5. uv.lock fresh (uv lock --check)
  6. prod tree exists at /usr/local/civicmesh/app

Exit codes:
  0   success
  1   pre-flight failed (message names the check)
  2   shell-out failed (message names the step)
  3   user aborted at confirmation prompt
  10  wrong-mode (PROD invocation refused)
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Callable


_DEFAULT_PROD_APP = Path("/usr/local/civicmesh/app")
_DEFAULT_PROD_VAR = Path("/usr/local/civicmesh/var")

# The astral installer puts uv at ~civicmesh/.local/bin/uv. We use the
# absolute path in any `sudo -u civicmesh sh -c '...'` shell-out below
# because sudo -u doesn't load login profiles — PATH won't include
# ~/.local/bin and bare `uv` won't resolve. Mirrors UV_BIN in
# scripts/civicmesh-bootstrap.sh.
_PROD_UV_BIN = "/usr/local/civicmesh/.local/bin/uv"


class _PreflightFailure(Exception):
    """Raised by a pre-flight check; caller prints & exits 1."""


# ---------------------------------------------------------------- pre-flight


def _check_pyproject_civicmesh(src_dir: Path) -> None:
    py = src_dir / "pyproject.toml"
    if not py.is_file():
        raise _PreflightFailure(
            f"pre-flight check 1 failed: no pyproject.toml at {py}; "
            "run promote from a CivicMesh dev tree (--from PATH)"
        )
    from config import _load_toml

    try:
        data = _load_toml(str(py))
    except Exception as e:
        raise _PreflightFailure(
            f"pre-flight check 1 failed: cannot parse {py}: {e}"
        )
    name = data.get("project", {}).get("name")
    if name != "civicmesh":
        raise _PreflightFailure(
            f"pre-flight check 1 failed: pyproject.toml [project].name is "
            f"{name!r}, expected 'civicmesh'; either you're in the wrong tree "
            "or the rename hasn't landed yet"
        )


def _check_main_branch_exists(src_dir: Path) -> None:
    rc = subprocess.run(
        ["git", "-C", str(src_dir), "rev-parse", "--verify", "--quiet",
         "refs/heads/main"],
        capture_output=True,
    ).returncode
    if rc != 0:
        raise _PreflightFailure(
            "pre-flight check 2 failed: local 'main' branch does not exist; "
            "fetch or create it before promoting"
        )


def _check_head_on_main(src_dir: Path) -> None:
    result = subprocess.run(
        ["git", "-C", str(src_dir), "symbolic-ref", "--short", "HEAD"],
        capture_output=True, text=True,
    )
    branch = result.stdout.strip() if result.returncode == 0 else "(detached HEAD)"
    if branch != "main":
        raise _PreflightFailure(
            f"pre-flight check 3 failed: HEAD is on {branch!r}, expected 'main'; "
            "switch with `git checkout main`"
        )


def _check_clean_tree(src_dir: Path) -> None:
    result = subprocess.run(
        ["git", "-C", str(src_dir), "status", "--porcelain"],
        capture_output=True, text=True, check=True,
    )
    if result.stdout.strip():
        raise _PreflightFailure(
            "pre-flight check 4 failed: working tree has uncommitted changes; "
            "commit or stash them before promoting"
        )


def _check_lock_fresh(src_dir: Path) -> None:
    rc = subprocess.run(
        ["uv", "lock", "--check"],
        cwd=str(src_dir), capture_output=True,
    ).returncode
    if rc != 0:
        raise _PreflightFailure(
            "pre-flight check 5 failed: uv.lock is stale; run `uv lock` "
            "and commit before promoting"
        )


def _check_prod_tree_exists(prod_app: Path) -> None:
    try:
        exists = prod_app.is_dir()
    except PermissionError as e:
        raise _PreflightFailure(
            f"pre-flight check 6 failed: cannot stat {prod_app}: {e}; "
            "a parent directory is not traversable by this user. "
            "Try: sudo chmod 755 /usr/local/civicmesh"
        )
    if not exists:
        raise _PreflightFailure(
            f"pre-flight check 6 failed: {prod_app} does not exist; "
            "run bootstrap on this host first"
        )


# ----------------------------------------------------------- git plumbing


def _git_head_sha(src_dir: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(src_dir), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()


def _git_subject(src_dir: Path, sha: str) -> str:
    return subprocess.run(
        ["git", "-C", str(src_dir), "log", "-1", "--format=%s", sha],
        capture_output=True, text=True, check=True,
    ).stdout.strip()


def _read_prod_sha(src_dir: Path, prod_var: Path, prod_app: Path) -> str | None:
    """Try marker file, fall back to .git/HEAD; verify reachability in dev's log.

    Returns None if no usable SHA found, or if the SHA isn't reachable from
    dev's history (e.g. prod was deployed from a now-discarded branch).
    """
    marker = prod_var / "last-promoted-commit"
    sha: str | None = None
    if marker.is_file():
        try:
            sha = marker.read_text().strip() or None
        except OSError:
            sha = None
    if sha is None:
        prod_head = prod_app / ".git" / "HEAD"
        if prod_head.is_file():
            try:
                head = prod_head.read_text().strip()
                if head.startswith("ref: "):
                    ref_path = prod_app / ".git" / head[5:]
                    if ref_path.is_file():
                        sha = ref_path.read_text().strip()
                else:
                    sha = head
            except OSError:
                sha = None
    if not sha:
        return None
    rc = subprocess.run(
        ["git", "-C", str(src_dir), "rev-parse", "--verify", "--quiet",
         f"{sha}^{{commit}}"],
        capture_output=True,
    ).returncode
    return sha if rc == 0 else None


# --------------------------------------------------------------- summary


def _print_summary(src_dir: Path, new_sha: str, prod_sha: str | None) -> None:
    new_subj = _git_subject(src_dir, new_sha)
    print(f"dev HEAD:   {new_sha[:12]} {new_subj}")
    if prod_sha:
        prod_subj = _git_subject(src_dir, prod_sha)
        print(f"prod HEAD:  {prod_sha[:12]} {prod_subj}")
        print()
        diff = subprocess.run(
            ["git", "-C", str(src_dir), "diff", "--stat",
             f"{prod_sha}..main"],
            capture_output=True, text=True,
        )
        sys.stdout.write(diff.stdout)
    else:
        print("prod HEAD:  unknown")
        print()
        print("prod commit unknown — full diff unavailable")


# --------------------------------------------------------------- deploy


def _run_deploy_pipeline(
    src_dir: Path,
    new_sha: str,
    prod_sha: str | None,
    prod_app: Path,
    prod_var: Path,
    *,
    restart: bool,
) -> int:
    # Cache sudo credentials before the pipeline. `sudo tar`'s stdin is
    # the git-archive stream — sudo can't read a password there.
    if subprocess.run(["sudo", "-v"]).returncode != 0:
        print("civicmesh: promote: sudo -v failed", file=sys.stderr)
        return 2

    archive = subprocess.Popen(
        ["git", "-C", str(src_dir), "archive", "main"],
        stdout=subprocess.PIPE,
    )
    extract = subprocess.Popen(
        ["sudo", "tar", "-x", "-C", str(prod_app) + "/"],
        stdin=archive.stdout,
    )
    archive.stdout.close()  # SIGPIPE plumbing
    extract_rc = extract.wait()
    archive_rc = archive.wait()
    if archive_rc != 0:
        print(f"civicmesh: promote: git archive failed (rc={archive_rc})",
              file=sys.stderr)
        return 2
    if extract_rc != 0:
        print(f"civicmesh: promote: tar extract failed (rc={extract_rc})",
              file=sys.stderr)
        return 2

    rc = subprocess.run([
        "sudo", "-u", "civicmesh", "sh", "-c",
        f"cd '{prod_app}' && '{_PROD_UV_BIN}' sync --frozen",
    ]).returncode
    if rc != 0:
        print(f"civicmesh: promote: uv sync --frozen failed (rc={rc})",
              file=sys.stderr)
        return 2

    # Restart is opt-in (--restart). Default: ship code, leave the running
    # services on the old code, let the operator pick the moment to cut
    # over. promote has no way to know if the new code is config-compatible
    # — a schema-breaking change would put the units into a crash loop the
    # moment systemd restarts them. The operator does know.
    if restart:
        result = subprocess.run(
            ["sudo", "systemctl", "restart", "civicmesh-web", "civicmesh-mesh"],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            print(
                "civicmesh: promote: systemctl restart failed: "
                f"{result.stderr.rstrip()}",
                file=sys.stderr,
            )
            if "not found" in result.stderr.lower():
                print(
                    "(if the unit was 'not found' — this box hasn't been fully\n"
                    " set up; run 'sudo civicmesh apply' first to render the "
                    "unit files.)",
                    file=sys.stderr,
                )
            return 2

    # Marker file. Soft-warn on failure: deploy succeeded, only the next
    # promote's diff display is degraded.
    try:
        subprocess.run(
            ["sudo", "-u", "civicmesh", "tee",
             str(prod_var / "last-promoted-commit")],
            input=(new_sha + "\n").encode(),
            capture_output=True, check=True,
        )
    except subprocess.CalledProcessError as e:
        print(
            "civicmesh: promote: warning: failed to write "
            f"{prod_var / 'last-promoted-commit'} "
            f"({e.stderr.decode(errors='replace').rstrip()}); "
            "next promote's diff display will be degraded",
            file=sys.stderr,
        )

    if prod_sha:
        diff_count = subprocess.run(
            ["git", "-C", str(src_dir), "diff", "--name-only",
             f"{prod_sha}..main"],
            capture_output=True, text=True,
        )
        n_files = len([l for l in diff_count.stdout.splitlines() if l.strip()])
        files_line = f"Files changed: {n_files}"
    else:
        files_line = "Files changed: unknown (prod HEAD was unknown)"

    print()
    print(f"Promoted {(prod_sha or 'unknown')[:12]} -> {new_sha[:12]}")
    print(files_line)
    if restart:
        print("Services restarted: civicmesh-web, civicmesh-mesh")
    else:
        print(
            "Services were not restarted (pass --restart to restart\n"
            "automatically). To pick up the new code:\n"
            "  sudo systemctl restart civicmesh-web civicmesh-mesh\n"
            "\n"
            "If this PR changed the config schema, run\n"
            "`sudo -u civicmesh civicmesh configure` BEFORE restarting,\n"
            "otherwise the new code may reject the existing config."
        )
    return 0


# ----------------------------------------------------------------- entry


def run_promote(
    src_dir: Path,
    *,
    mode: str,
    dry_run: bool,
    restart: bool = False,
    prod_app: Path = _DEFAULT_PROD_APP,
    prod_var: Path = _DEFAULT_PROD_VAR,
    input_fn: Callable[[str], str] = input,
) -> int:
    if mode == "prod":
        print(
            "civicmesh: promote must run from a dev checkout; "
            "`cd` to your dev tree and run `uv run civicmesh promote --from .`",
            file=sys.stderr,
        )
        return 10

    try:
        _check_pyproject_civicmesh(src_dir)
        _check_main_branch_exists(src_dir)
        _check_head_on_main(src_dir)
        _check_clean_tree(src_dir)
        _check_lock_fresh(src_dir)
        _check_prod_tree_exists(prod_app)
    except _PreflightFailure as e:
        print(f"civicmesh: promote: {e}", file=sys.stderr)
        return 1

    new_sha = _git_head_sha(src_dir)
    prod_sha = _read_prod_sha(src_dir, prod_var, prod_app)
    _print_summary(src_dir, new_sha, prod_sha)

    if dry_run:
        print()
        print("would proceed; exiting.")
        return 0

    print()
    try:
        resp = input_fn("Proceed? [y/N]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print("\nAborted, no changes made.", file=sys.stderr)
        return 3
    if resp not in ("y", "yes"):
        print("Aborted, no changes made.", file=sys.stderr)
        return 3

    return _run_deploy_pipeline(
        src_dir, new_sha, prod_sha, prod_app, prod_var, restart=restart,
    )
