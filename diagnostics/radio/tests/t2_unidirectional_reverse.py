"""T2 — Unidirectional send: zero2w → pi4. Same mechanism as T1, reversed."""

from __future__ import annotations

from pathlib import Path

from diagnostics.radio.harness.logger import NodesConfig
from diagnostics.radio.tests import t1_unidirectional


TEST_NAME = "t2_unidirectional_reverse"
SENDER = "zero2w"
RECEIVER = "pi4"


async def run(nodes_cfg: NodesConfig, runs_dir: Path, run_dir: Path | None = None) -> dict:
    return await t1_unidirectional.run(
        nodes_cfg,
        runs_dir,
        run_dir=run_dir,
        sender=SENDER,
        receiver=RECEIVER,
        test_name=TEST_NAME,
    )
