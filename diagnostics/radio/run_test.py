"""Orchestrator for the CivicMesh radio round-trip diagnostics harness.

Usage:
    python diagnostics/radio/run_test.py t0
    python diagnostics/radio/run_test.py t1
    python diagnostics/radio/run_test.py t2
    python diagnostics/radio/run_test.py t3
    python diagnostics/radio/run_test.py all
    python diagnostics/radio/run_test.py t3 --iterations 10

The harness refuses to run if mesh_bot is still holding /dev/ttyUSB0 on
either node. Stop mesh_bot manually (tmux → Ctrl-C) before invoking.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent  # diagnostics/radio
_REPO_ROOT = _HERE.parents[1]             # CivicMesh
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from diagnostics.radio.harness.logger import (  # noqa: E402
    load_nodes_config,
    make_run_dir,
    update_latest_symlink,
    write_summary,
)
from diagnostics.radio.tests import (  # noqa: E402
    t0_passive_listen,
    t1_unidirectional,
    t2_unidirectional_reverse,
    t3_repetition,
)


DEFAULT_NODES_TOML = _HERE / "nodes.toml"
DEFAULT_RUNS_DIR = _HERE / "runs"


TEST_REGISTRY = {
    "t0": t0_passive_listen,
    "t1": t1_unidirectional,
    "t2": t2_unidirectional_reverse,
    "t3": t3_repetition,
}


async def _run_one(test_key: str, nodes_cfg, runs_dir: Path, *, run_dir: Path | None = None, iterations: int | None = None):
    mod = TEST_REGISTRY[test_key]
    kwargs = {}
    if test_key == "t3" and iterations is not None:
        kwargs["iterations"] = iterations
    return await mod.run(nodes_cfg, runs_dir, run_dir=run_dir, **kwargs)


async def _run_all(nodes_cfg, runs_dir: Path, *, iterations: int | None = None):
    parent_dir = make_run_dir(runs_dir, "all")
    results = {}
    for key in ("t0", "t1", "t2", "t3"):
        sub_dir = parent_dir / key
        sub_dir.mkdir(parents=True, exist_ok=True)
        print(f"[run_test] starting {key} → {sub_dir}", flush=True)
        res = await _run_one(key, nodes_cfg, parent_dir, run_dir=sub_dir, iterations=iterations)
        results[key] = res
        print(f"[run_test] {key} verdict: {res['verdict']}", flush=True)
        if key != "t3":
            await asyncio.sleep(10.0)
    _write_all_summary(parent_dir, results)
    update_latest_symlink(runs_dir, parent_dir)
    return {"verdict": "REPORTED", "run_dir": str(parent_dir), "sub_results": results}


def _write_all_summary(parent_dir: Path, results: dict) -> None:
    lines = ["# ALL — aggregate verdict", ""]
    for key, res in results.items():
        lines.append(f"## {key}")
        lines.append("")
        lines.append(f"- verdict: {res.get('verdict')}")
        lines.append(f"- run_dir: {res.get('run_dir')}")
        lines.append("")
    write_summary(parent_dir, "\n".join(lines))


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="CivicMesh radio round-trip diagnostics.")
    p.add_argument("test", choices=["t0", "t1", "t2", "t3", "all"])
    p.add_argument("--nodes-toml", type=Path, default=DEFAULT_NODES_TOML)
    p.add_argument("--runs-dir", type=Path, default=DEFAULT_RUNS_DIR)
    p.add_argument("--iterations", type=int, default=None,
                   help="T3 only: override iterations per direction.")
    return p.parse_args()


def main() -> int:
    args = _parse_args()
    nodes_cfg = load_nodes_config(args.nodes_toml)
    args.runs_dir.mkdir(parents=True, exist_ok=True)

    if args.test == "all":
        result = asyncio.run(_run_all(nodes_cfg, args.runs_dir, iterations=args.iterations))
    else:
        result = asyncio.run(_run_one(args.test, nodes_cfg, args.runs_dir, iterations=args.iterations))

    print(f"[run_test] {args.test} verdict: {result['verdict']}")
    print(f"[run_test] run_dir: {result['run_dir']}")
    # Non-zero exit on preflight failure so CI/automation can detect it.
    if result.get("verdict") == "PREFLIGHT_FAILED":
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
