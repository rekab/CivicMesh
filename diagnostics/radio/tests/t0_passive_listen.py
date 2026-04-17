"""T0 — Passive listen.

Both nodes open serial, subscribe to all events, listen for 30s. Neither
transmits. Reports per-node packet counts so we can see if one node's
radio is RX-impaired before the other tests run."""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

from diagnostics.radio.harness.connection import probe_clock_skew, run_node
from diagnostics.radio.harness.logger import (
    NodesConfig,
    console_log,
    make_run_dir,
    update_latest_symlink,
    write_manifest,
    write_summary,
)
from diagnostics.radio.harness.preflight import preflight
from diagnostics.radio.harness.verdict import (
    collect_rssi,
    event_type_counts,
    manifest_for,
    median,
)


TEST_NAME = "t0_passive_listen"
LISTEN_S = 30.0


async def run(nodes_cfg: NodesConfig, runs_dir: Path, run_dir: Path | None = None,
              *, do_set_channel: bool = True) -> dict:
    if run_dir is None:
        run_dir = make_run_dir(runs_dir, TEST_NAME)

    run_id = f"{TEST_NAME}_{uuid.uuid4().hex[:8]}"
    test_marker = f"{nodes_cfg.test.message_prefix} t0 {run_id[-8:]}"  # not transmitted; for manifest only
    console_log("mac", f"T0 passive listen — run_dir={run_dir.name} listen={LISTEN_S:.0f}s")

    console_log("mac", f"phase: preflight on {list(nodes_cfg.nodes)} (parallel)")
    pre_results = await asyncio.gather(
        *[preflight(nc, nodes_cfg.test.channel) for nc in nodes_cfg.nodes.values()]
    )
    pre_failed = [pr for pr in pre_results if not pr.ok]
    if pre_failed:
        console_log("mac", f"⚠️ preflight failed on {[pr.node_name for pr in pre_failed]} — aborting")
        summary = _preflight_failure_summary(pre_results)
        write_summary(run_dir, summary)
        write_manifest(run_dir, {
            **manifest_for(TEST_NAME, test_marker, nodes_cfg, run_id, sender=None, receiver=None),
            "preflight_failed": True,
            "preflight": [pr.__dict__ for pr in pre_results],
        })
        update_latest_symlink(runs_dir, run_dir)
        return {"verdict": "PREFLIGHT_FAILED", "run_dir": str(run_dir)}

    console_log("mac", "phase: clock skew probe (parallel)")
    skews = await asyncio.gather(
        *[probe_clock_skew(nc) for nc in nodes_cfg.nodes.values()]
    )

    hard_deadline = LISTEN_S + 30.0
    console_log("mac", f"phase: spawning node_side.py on both nodes in role=listen (listen_after_s={LISTEN_S:.0f})")
    tasks = []
    for node_name, nc in nodes_cfg.nodes.items():
        params = _common_params(node_name, nc, nodes_cfg, run_id, role="listen",
                                 listen_after_s=LISTEN_S, do_set_channel=do_set_channel)
        tasks.append(run_node(nc, params, run_dir, hard_deadline_s=hard_deadline))
    node_results = await asyncio.gather(*tasks)
    console_log("mac", "phase: writing summary.md + manifest.json")

    summary = _summarize(nodes_cfg, node_results, skews, pre_results)
    write_summary(run_dir, summary)
    write_manifest(run_dir, {
        **manifest_for(TEST_NAME, test_marker, nodes_cfg, run_id, sender=None, receiver=None),
        "listen_s": LISTEN_S,
        "skews": skews,
        "preflight": [pr.__dict__ for pr in pre_results],
        "node_results": [{
            "node": nr.node_name,
            "exit_code": nr.exit_code,
            "events_path": str(nr.events_path),
            "stderr_path": str(nr.stderr_path),
            "authoritative_path": str(nr.authoritative_path) if nr.authoritative_path else None,
            "authoritative_event_count": nr.authoritative_event_count,
            "streamed_line_count": nr.streamed_line_count,
            "hardkilled": nr.hardkilled,
            "stream_loss": nr.stream_loss,
            "test_complete": nr.test_complete,
        } for nr in node_results],
    })
    update_latest_symlink(runs_dir, run_dir)
    return {"verdict": "REPORTED", "run_dir": str(run_dir)}


def _common_params(node_name, nc, nodes_cfg, run_id, *, role, listen_after_s, do_set_channel=True, **extra):
    params = {
        "node_name": node_name,
        "serial_port": nc.serial_port,
        "baud": nc.baud,
        "channel_name": nodes_cfg.test.channel,
        "config_path": f"{nc.repo_path}/config.toml",
        "run_id": run_id,
        "role": role,
        "listen_after_s": listen_after_s,
        "marker": "",
        "send_spacing_s": nodes_cfg.test.min_send_spacing_s,
        "iterations": 1,
        "pre_send_delay_s": 0.0,
        "stall_threshold_s": 30.0,
        "do_set_channel": do_set_channel,
    }
    params.update(extra)
    return params


def _preflight_failure_summary(pre_results) -> str:
    lines = ["# T0 Passive Listen — PREFLIGHT FAILED", ""]
    for pr in pre_results:
        lines.append(f"- {pr.summary_line()}")
    lines.append("")
    lines.append("Resolve preflight failures and re-run.")
    return "\n".join(lines)


def _summarize(nodes_cfg, node_results, skews, pre_results) -> str:
    lines = [f"# T0 Passive Listen — {LISTEN_S:.0f}s", ""]
    counts_by_node = {}
    for nr in node_results:
        counts = event_type_counts(nr.events)
        counts_by_node[nr.node_name] = counts
        rssis = collect_rssi(nr.events)
        lines.append(f"## {nr.node_name}")
        lines.append("")
        lines.append(f"- exit_code: {nr.exit_code}  | hardkilled: {nr.hardkilled}  | test_complete: {nr.test_complete}  | stream_loss: {nr.stream_loss}")
        lines.append(f"- streamed_lines: {nr.streamed_line_count}  | authoritative_lines: {nr.authoritative_event_count}")
        lines.append(f"- total events: {sum(counts.values())}")
        if counts:
            lines.append("- counts by event_type:")
            for et, c in sorted(counts.items(), key=lambda kv: -kv[1]):
                lines.append(f"    - {et}: {c}")
        if rssis:
            lines.append(f"- rssi: min={min(rssis):.1f} median={median(rssis):.1f} max={max(rssis):.1f} (n={len(rssis)})")
        else:
            lines.append("- rssi: no events carried an `rssi` field")
        lines.append("")

    # Asymmetry check (CHANNEL_MSG_RECV + any RX-flavored types)
    def _rx_total(counts):
        return sum(c for et, c in counts.items() if et.startswith("RX") or et == "CHANNEL_MSG_RECV" or et == "ADVERTISEMENT")

    rx_totals = {n: _rx_total(c) for n, c in counts_by_node.items()}
    lines.append("## Asymmetry check")
    lines.append("")
    lines.append(f"- RX-event totals per node: {rx_totals}")
    if len(rx_totals) >= 2:
        vals = list(rx_totals.values())
        if max(vals) >= 10 * max(min(vals), 1):
            lines.append("- ⚠️ ASYMMETRIC: one node heard ≥10× fewer RX events than the other.")
        else:
            lines.append("- symmetric within 10×.")
    lines.append("")

    lines.append("## Clock skew (informational)")
    lines.append("")
    for s in skews:
        if s.get("ok"):
            lines.append(f"- {s['node']}: rtt={s['rtt_ms']:.1f}ms estimated_skew={s['estimated_skew_ms']:+.1f}ms")
        else:
            lines.append(f"- {s.get('node')}: probe failed: {s.get('err')}")
    lines.append("")

    lines.append("## Preflight notes")
    lines.append("")
    for pr in pre_results:
        lines.append(f"- {pr.summary_line()}")
    return "\n".join(lines)
