"""Tests for civicmesh promote (CIV-63).

Pre-flight checks are tested with real-git tmpdir fixtures (no mocking
of git or uv) — promote runs real subprocess and mocking would defeat
the point. The deploy pipeline (sudo, tar, uv sync, systemctl) is NOT
tested here; it requires real prod infrastructure and is verified
manually per the CIV-63 acceptance criteria.

The `make_dev_tree` helper uses `uv lock` against a bare pyproject (no
deps) so each fixture build is sub-second.
"""

import io
import re
import subprocess
import tempfile
import textwrap
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch, MagicMock

import promote


# --------------------------------------------------------------- fixture


def _git(td: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(td), *args],
        check=True, capture_output=True, text=True,
    )


def make_dev_tree(
    *,
    name: str = "civicmesh",
    branch: str = "main",
    clean: bool = True,
    lock_in_sync: bool = True,
    delete_main: bool = False,
) -> Path:
    """Build a real-git tmpdir fixture for promote tests.

    Each knob exercises a single pre-flight check: `name` for #1,
    `delete_main` for #2, `branch` for #3, `clean` for #4,
    `lock_in_sync` for #5. Caller is responsible for cleanup.
    """
    td = Path(tempfile.mkdtemp(prefix="promote-test-"))
    (td / "pyproject.toml").write_text(textwrap.dedent(f"""\
        [project]
        name = "{name}"
        version = "0.1.0"
        requires-python = ">=3.13"
        dependencies = []
    """))
    _git(td, "init", "-q", "-b", "main")
    _git(td, "config", "user.email", "test@example.com")
    _git(td, "config", "user.name", "Test")
    _git(td, "add", "pyproject.toml")
    _git(td, "commit", "-q", "-m", "init")
    if lock_in_sync:
        subprocess.run(["uv", "lock"], cwd=td, check=True, capture_output=True)
        _git(td, "add", "uv.lock")
        _git(td, "commit", "-q", "-m", "add lock")
    if branch != "main":
        _git(td, "checkout", "-q", "-b", branch)
    if delete_main:
        # Branch off main, then delete it. After this, no `main` ref exists.
        _git(td, "checkout", "-q", "-b", "develop")
        _git(td, "branch", "-D", "main")
    if not clean:
        (td / "DIRTY").write_text("uncommitted")
    return td


def _existing_prod_app(td: Path) -> Path:
    """Return a path that exists (passes pre-flight #6)."""
    p = td / "fake-prod-app"
    p.mkdir(exist_ok=True)
    return p


# --------------------------------------------------------------- helpers


def _run_promote_capture(
    src_dir: Path,
    *,
    mode: str = "dev",
    dry_run: bool = True,
    restart: bool = False,
    prod_app: Path | None = None,
    prod_var: Path | None = None,
    input_responses: list[str] | None = None,
):
    """Run promote.run_promote, capture stdout/stderr, return (rc, out, err)."""
    if prod_app is None:
        prod_app = _existing_prod_app(src_dir)
    if prod_var is None:
        prod_var = src_dir / "fake-prod-var"
        prod_var.mkdir(exist_ok=True)

    out_buf = io.StringIO()
    err_buf = io.StringIO()
    if input_responses is not None:
        responses = iter(input_responses)
        input_fn = lambda _prompt: next(responses)
    else:
        input_fn = lambda _prompt: ""  # default: empty -> aborts

    with redirect_stdout(out_buf), redirect_stderr(err_buf):
        rc = promote.run_promote(
            src_dir,
            mode=mode,
            dry_run=dry_run,
            restart=restart,
            prod_app=prod_app,
            prod_var=prod_var,
            input_fn=input_fn,
        )
    return rc, out_buf.getvalue(), err_buf.getvalue()


# ----------------------------------------------------------- pre-flight tests


class PreflightTest(unittest.TestCase):

    def test_passes_clean_main_civicmesh(self) -> None:
        src = make_dev_tree()
        try:
            rc, out, err = _run_promote_capture(src, dry_run=True)
            self.assertEqual(rc, 0, msg=err)
            self.assertIn("dev HEAD:", out)
            self.assertIn("would proceed; exiting.", out)
            # Sanity: deploy pipeline shouldn't have written a marker.
            marker = src / "fake-prod-var" / "last-promoted-commit"
            self.assertFalse(marker.exists())
        finally:
            subprocess.run(["rm", "-rf", str(src)], check=True)

    def test_check1_pyproject_name_mismatch(self) -> None:
        src = make_dev_tree(name="not-civicmesh")
        try:
            rc, _out, err = _run_promote_capture(src, dry_run=True)
            self.assertEqual(rc, 1)
            self.assertIn("pre-flight check 1 failed", err)
            self.assertIn("not-civicmesh", err)
        finally:
            subprocess.run(["rm", "-rf", str(src)], check=True)

    def test_check2_main_branch_missing(self) -> None:
        src = make_dev_tree(delete_main=True)
        try:
            rc, _out, err = _run_promote_capture(src, dry_run=True)
            self.assertEqual(rc, 1)
            self.assertIn("pre-flight check 2 failed", err)
        finally:
            subprocess.run(["rm", "-rf", str(src)], check=True)

    def test_check3_head_not_on_main(self) -> None:
        src = make_dev_tree(branch="feature")
        try:
            rc, _out, err = _run_promote_capture(src, dry_run=True)
            self.assertEqual(rc, 1)
            self.assertIn("pre-flight check 3 failed", err)
            self.assertIn("'feature'", err)
        finally:
            subprocess.run(["rm", "-rf", str(src)], check=True)

    def test_check4_dirty_tree(self) -> None:
        src = make_dev_tree(clean=False)
        try:
            rc, _out, err = _run_promote_capture(src, dry_run=True)
            self.assertEqual(rc, 1)
            self.assertIn("pre-flight check 4 failed", err)
        finally:
            subprocess.run(["rm", "-rf", str(src)], check=True)

    def test_check5_stale_lock(self) -> None:
        src = make_dev_tree(lock_in_sync=False)
        try:
            rc, _out, err = _run_promote_capture(src, dry_run=True)
            self.assertEqual(rc, 1)
            self.assertIn("pre-flight check 5 failed", err)
        finally:
            subprocess.run(["rm", "-rf", str(src)], check=True)

    def test_check6_prod_tree_missing(self) -> None:
        src = make_dev_tree()
        try:
            missing = src / "definitely-does-not-exist"
            rc, _out, err = _run_promote_capture(src, dry_run=True, prod_app=missing)
            self.assertEqual(rc, 1)
            self.assertIn("pre-flight check 6 failed", err)
        finally:
            subprocess.run(["rm", "-rf", str(src)], check=True)

    def test_check6_prod_tree_permission_error(self) -> None:
        """Unreadable prod_app -> _PreflightFailure with actionable hint,
        not an unhandled PermissionError stack trace."""
        src = make_dev_tree()
        try:
            prod_app = src / "fake-prod-app"
            prod_app.mkdir()
            with patch(
                "pathlib.Path.is_dir",
                side_effect=PermissionError(13, "Permission denied"),
            ):
                rc, _out, err = _run_promote_capture(
                    src, dry_run=True, prod_app=prod_app,
                )
            self.assertEqual(rc, 1)
            self.assertIn("pre-flight check 6 failed", err)
            self.assertIn(str(prod_app), err)
            self.assertIn("chmod", err.lower())
        finally:
            subprocess.run(["rm", "-rf", str(src)], check=True)


# ----------------------------------------------------------- behavior tests


class BehaviorTest(unittest.TestCase):

    def test_dry_run_skips_deploy_pipeline(self) -> None:
        src = make_dev_tree()
        try:
            with patch("promote._run_deploy_pipeline") as mock_deploy:
                rc, out, _err = _run_promote_capture(src, dry_run=True)
            self.assertEqual(rc, 0)
            self.assertIn("would proceed; exiting.", out)
            mock_deploy.assert_not_called()
        finally:
            subprocess.run(["rm", "-rf", str(src)], check=True)

    def test_promote_refused_in_prod_mode(self) -> None:
        src = make_dev_tree()
        try:
            rc, _out, err = _run_promote_capture(src, mode="prod", dry_run=True)
            self.assertEqual(rc, 10)
            self.assertIn("promote must run from a dev checkout", err)
            self.assertIn("`uv run civicmesh promote --from .`", err)
        finally:
            subprocess.run(["rm", "-rf", str(src)], check=True)

    def test_user_aborts_at_confirmation(self) -> None:
        src = make_dev_tree()
        try:
            with patch("promote._run_deploy_pipeline") as mock_deploy:
                rc, _out, err = _run_promote_capture(
                    src, dry_run=False, input_responses=["n"],
                )
            self.assertEqual(rc, 3)
            self.assertIn("Aborted, no changes made.", err)
            mock_deploy.assert_not_called()
        finally:
            subprocess.run(["rm", "-rf", str(src)], check=True)

    def test_uv_sync_uses_prod_absolute_path(self) -> None:
        """The `uv sync --frozen` shell-out must reference uv via its
        absolute prod-tree path. `sudo -u civicmesh sh -c` runs without
        a login profile, so PATH doesn't include ~civicmesh/.local/bin
        and bare `uv` won't resolve (the regression this guards against)."""
        src = make_dev_tree()
        try:
            prod_app = _existing_prod_app(src)
            prod_var = src / "fake-prod-var"
            prod_var.mkdir(exist_ok=True)
            ok = subprocess.CompletedProcess([], 0, b"", b"")
            popen_inst = MagicMock()
            popen_inst.wait.return_value = 0
            popen_inst.stdout = MagicMock()
            with patch("promote.subprocess.run", return_value=ok) as mock_run, \
                 patch("promote.subprocess.Popen", return_value=popen_inst):
                rc = promote._run_deploy_pipeline(
                    src, "abc123", None, prod_app, prod_var, restart=True,
                )
            self.assertEqual(rc, 0)
            # Flatten every argv passed to subprocess.run into one
            # searchable string. _PROD_UV_BIN must appear; bare `uv sync`
            # must not (regression guard).
            joined = " | ".join(
                arg for call in mock_run.call_args_list
                for arg in call.args[0]
            )
            self.assertIn(promote._PROD_UV_BIN, joined)
            self.assertNotRegex(
                joined, r"(?<![/\w])uv sync",
                "found bare `uv sync` — fix regressed",
            )
        finally:
            subprocess.run(["rm", "-rf", str(src)], check=True)

    def test_restart_flag_runs_systemctl_and_reports(self) -> None:
        """With restart=True, the deploy pipeline issues
        `systemctl restart civicmesh-web civicmesh-mesh` and prints the
        legacy "Services restarted: ..." line."""
        src = make_dev_tree()
        try:
            prod_app = _existing_prod_app(src)
            prod_var = src / "fake-prod-var"
            prod_var.mkdir(exist_ok=True)
            ok = subprocess.CompletedProcess([], 0, b"", b"")
            popen_inst = MagicMock()
            popen_inst.wait.return_value = 0
            popen_inst.stdout = MagicMock()
            out_buf = io.StringIO()
            with patch("promote.subprocess.run", return_value=ok) as mock_run, \
                 patch("promote.subprocess.Popen", return_value=popen_inst), \
                 redirect_stdout(out_buf):
                rc = promote._run_deploy_pipeline(
                    src, "abc123", None, prod_app, prod_var, restart=True,
                )
            self.assertEqual(rc, 0)
            argvs = [c.args[0] for c in mock_run.call_args_list]
            self.assertIn(
                ["sudo", "systemctl", "restart",
                 "civicmesh-web", "civicmesh-mesh"],
                argvs,
            )
            self.assertIn(
                "Services restarted: civicmesh-web, civicmesh-mesh",
                out_buf.getvalue(),
            )
        finally:
            subprocess.run(["rm", "-rf", str(src)], check=True)

    def test_default_does_not_restart_and_prints_instructions(self) -> None:
        """Default invocation (restart=False) ships code without
        restarting services, and stdout names the literal systemctl
        command the operator should run when ready."""
        src = make_dev_tree()
        try:
            prod_app = _existing_prod_app(src)
            prod_var = src / "fake-prod-var"
            prod_var.mkdir(exist_ok=True)
            ok = subprocess.CompletedProcess([], 0, b"", b"")
            popen_inst = MagicMock()
            popen_inst.wait.return_value = 0
            popen_inst.stdout = MagicMock()
            out_buf = io.StringIO()
            with patch("promote.subprocess.run", return_value=ok) as mock_run, \
                 patch("promote.subprocess.Popen", return_value=popen_inst), \
                 redirect_stdout(out_buf):
                rc = promote._run_deploy_pipeline(
                    src, "abc123", None, prod_app, prod_var, restart=False,
                )
            self.assertEqual(rc, 0)
            argvs = [c.args[0] for c in mock_run.call_args_list]
            self.assertNotIn(
                ["sudo", "systemctl", "restart",
                 "civicmesh-web", "civicmesh-mesh"],
                argvs,
            )
            stdout = out_buf.getvalue()
            self.assertNotIn("Services restarted:", stdout)
            self.assertIn(
                "sudo systemctl restart civicmesh-web civicmesh-mesh",
                stdout,
            )
            self.assertIn("--restart", stdout)
            self.assertIn("civicmesh configure", stdout)
        finally:
            subprocess.run(["rm", "-rf", str(src)], check=True)


if __name__ == "__main__":
    unittest.main()
