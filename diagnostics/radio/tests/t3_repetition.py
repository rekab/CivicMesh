"""T3 — Repetition: 20 sends pi4→zero2w, then 20 sends zero2w→pi4.

Each direction runs as a single long-lived pair of node-side sessions
(sender in role=send_repeat, receiver in role=listen) so the radios stay
warm and we don't measure reconnect-thrash noise. Every iteration's marker
gets its own uuid suffix so we can match receiver decodes to specific
sends by content."""

from __future__ import annotations

import asyncio
import re
import uuid
from collections import Counter
from datetime import datetime
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
from diagnostics.radio.harness.preflight import check_version_consistency, preflight
from diagnostics.radio.harness.verdict import (
    classify_send,
    event_type_counts,
    find_send_results,
    manifest_for,
    median,
    percentiles,
    recipient_received_marker,
)


TEST_NAME = "t3_repetition"
DEFAULT_ITERATIONS = 20
PRE_SEND_DELAY_S = 2.0
POST_BATCH_LISTEN_S = 10.0


async def run(
    nodes_cfg: NodesConfig,
    runs_dir: Path,
    run_dir: Path | None = None,
    *,
    iterations: int = DEFAULT_ITERATIONS,
    do_set_channel: bool = True,
) -> dict:
    if run_dir is None:
        run_dir = make_run_dir(runs_dir, TEST_NAME)

    run_id_base = f"{TEST_NAME}_{uuid.uuid4().hex[:8]}"
    marker_base = f"{nodes_cfg.test.message_prefix} t3 {run_id_base[-8:]}"
    console_log("mac", f"T3 repetition — run_dir={run_dir.name} iterations={iterations}/direction spacing={nodes_cfg.test.min_send_spacing_s}s")

    console_log("mac", f"phase: preflight on {list(nodes_cfg.nodes)} (parallel)")
    pre_results = await asyncio.gather(
        *[preflight(nc, nodes_cfg.test.channel) for nc in nodes_cfg.nodes.values()]
    )
    version_warn = check_version_consistency(pre_results)
    if version_warn:
        console_log("mac", version_warn)
    if any(not pr.ok for pr in pre_results):
        console_log("mac", f"⚠️ preflight failed — aborting {TEST_NAME}")
        lines = [f"# {TEST_NAME} — PREFLIGHT FAILED", ""]
        for pr in pre_results:
            lines.append(f"- {pr.summary_line()}")
        write_summary(run_dir, "\n".join(lines))
        write_manifest(run_dir, {
            **manifest_for(TEST_NAME, marker_base, nodes_cfg, run_id_base, sender=None, receiver=None),
            "preflight_failed": True,
            "preflight": [pr.__dict__ for pr in pre_results],
        })
        update_latest_symlink(runs_dir, run_dir)
        return {"verdict": "PREFLIGHT_FAILED", "run_dir": str(run_dir)}

    console_log("mac", "phase: clock skew probe (parallel)")
    skews = await asyncio.gather(
        *[probe_clock_skew(nc) for nc in nodes_cfg.nodes.values()]
    )

    directions = [("pi4", "zero2w"), ("zero2w", "pi4")]
    batch_summaries: list[dict] = []

    for i, (sender, receiver) in enumerate(directions, start=1):
        batch_id = f"{run_id_base}__{sender}_to_{receiver}"
        marker = f"{marker_base} {sender}->{receiver}"
        console_log("mac", f"phase: batch {i}/{len(directions)} — {sender} → {receiver} ({iterations} sends)")
        batch = await _run_batch(
            nodes_cfg, run_dir, batch_id, marker, sender, receiver,
            iterations=iterations,
            send_spacing_s=nodes_cfg.test.min_send_spacing_s,
            do_set_channel=do_set_channel,
        )
        console_log("mac", f"batch {sender}→{receiver} verdict: {batch['verdict']}")
        batch_summaries.append(batch)
        if i < len(directions):
            console_log("mac", "cooling down 5s between directions")
            await asyncio.sleep(5.0)

    console_log("mac", "phase: computing aggregate verdict + writing summary.md + manifest.json")
    summary = _summarize(nodes_cfg, batch_summaries, skews, pre_results, iterations)
    write_summary(run_dir, summary)
    write_manifest(run_dir, {
        **manifest_for(TEST_NAME, marker_base, nodes_cfg, run_id_base, sender=None, receiver=None),
        "iterations_per_direction": iterations,
        "send_spacing_s": nodes_cfg.test.min_send_spacing_s,
        "skews": skews,
        "preflight": [pr.__dict__ for pr in pre_results],
        "batches": [_batch_manifest(b) for b in batch_summaries],
    })
    update_latest_symlink(runs_dir, run_dir)
    worst = _worst_verdict([b["verdict"] for b in batch_summaries])
    return {"verdict": worst, "run_dir": str(run_dir)}


def _worst_verdict(verdicts):
    priority = [
        "SENDER_SAYS_FAILED_BUT_RECIPIENT_RECEIVED",
        "SENDER_SAYS_SENT_NOT_RECEIVED",
        "INCONCLUSIVE",
        "BOTH_AGREE_FAILED",
        "BOTH_AGREE_SENT",
        "REPORTED",
    ]
    present = set(verdicts)
    for p in priority:
        if p in present:
            return p
    return "UNKNOWN"


async def _run_batch(nodes_cfg, run_dir, batch_id, marker, sender, receiver, *, iterations, send_spacing_s, do_set_channel=True):
    total_send_window_s = PRE_SEND_DELAY_S + iterations * send_spacing_s + 5.0
    sender_listen = POST_BATCH_LISTEN_S
    receiver_listen = total_send_window_s + sender_listen + 5.0
    hard_deadline_sender = total_send_window_s + sender_listen + 60.0
    hard_deadline_receiver = receiver_listen + 60.0

    sender_params = _params(
        nodes_cfg, sender, batch_id, marker,
        role="send_repeat",
        listen_after_s=sender_listen,
        pre_send_delay_s=PRE_SEND_DELAY_S,
        iterations=iterations,
        send_spacing_s=send_spacing_s,
        do_set_channel=do_set_channel,
    )
    receiver_params = _params(
        nodes_cfg, receiver, batch_id, marker,
        role="listen",
        listen_after_s=receiver_listen,
        pre_send_delay_s=0.0,
        do_set_channel=do_set_channel,
    )

    sender_task = run_node(nodes_cfg.node(sender), sender_params, run_dir, hard_deadline_s=hard_deadline_sender)
    receiver_task = run_node(nodes_cfg.node(receiver), receiver_params, run_dir, hard_deadline_s=hard_deadline_receiver)
    sender_result, receiver_result = await asyncio.gather(sender_task, receiver_task)

    # For each SEND_RESULT, find a matching CHANNEL_MSG_RECV on the receiver
    # keyed on the iteration-specific marker ("... i=N u=XXXXXXXX").
    iteration_rows = []
    for send_ev in find_send_results(sender_result.events):
        payload = send_ev.get("payload") or {}
        iter_marker = payload.get("marker")
        recv_match = recipient_received_marker(receiver_result.events, iter_marker) if iter_marker else None
        verdict = classify_send(send_ev, recv_match)
        iteration_rows.append({
            "marker": iter_marker,
            "sender_type": payload.get("type"),
            "sender_err_payload": payload.get("payload"),
            "sender_pre_mono": payload.get("pre_mono_ts"),
            "sender_post_wall": payload.get("post_wall_ts"),
            "recv_wall": recv_match.get("wall_ts") if recv_match else None,
            "verdict": verdict,
        })

    batch_verdict = "BOTH_AGREE_SENT"
    if any(r["verdict"] == "SENDER_SAYS_FAILED_BUT_RECIPIENT_RECEIVED" for r in iteration_rows):
        batch_verdict = "SENDER_SAYS_FAILED_BUT_RECIPIENT_RECEIVED"
    elif any(r["verdict"] == "SENDER_SAYS_SENT_NOT_RECEIVED" for r in iteration_rows):
        batch_verdict = "SENDER_SAYS_SENT_NOT_RECEIVED"
    elif any(r["verdict"] == "INCONCLUSIVE" for r in iteration_rows):
        batch_verdict = "INCONCLUSIVE"
    elif all(r["verdict"] == "BOTH_AGREE_FAILED" for r in iteration_rows) and iteration_rows:
        batch_verdict = "BOTH_AGREE_FAILED"

    return {
        "sender": sender,
        "receiver": receiver,
        "marker": marker,
        "batch_id": batch_id,
        "iterations": iterations,
        "send_spacing_s": send_spacing_s,
        "iteration_rows": iteration_rows,
        "sender_result": sender_result,
        "receiver_result": receiver_result,
        "verdict": batch_verdict,
    }


def _params(nodes_cfg, node_name, run_id, marker, *, role, listen_after_s, pre_send_delay_s,
            iterations=1, send_spacing_s=None, do_set_channel=True):
    nc = nodes_cfg.node(node_name)
    return {
        "node_name": node_name,
        "serial_port": nc.serial_port,
        "baud": nc.baud,
        "channel_name": nodes_cfg.test.channel,
        "config_path": f"{nc.repo_path}/config.toml",
        "run_id": run_id,
        "role": role,
        "listen_after_s": listen_after_s,
        "marker": marker,
        "send_spacing_s": send_spacing_s if send_spacing_s is not None else nodes_cfg.test.min_send_spacing_s,
        "iterations": iterations,
        "pre_send_delay_s": pre_send_delay_s,
        "stall_threshold_s": 30.0,
        "do_set_channel": do_set_channel,
    }


def _batch_manifest(batch):
    return {
        "sender": batch["sender"],
        "receiver": batch["receiver"],
        "marker": batch["marker"],
        "batch_id": batch["batch_id"],
        "iterations": batch["iterations"],
        "send_spacing_s": batch["send_spacing_s"],
        "verdict": batch["verdict"],
        "iteration_rows": batch["iteration_rows"],
        "sender_result": _nr_dict(batch["sender_result"]),
        "receiver_result": _nr_dict(batch["receiver_result"]),
    }


def _nr_dict(nr):
    return {
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
    }


_WALL_RE = re.compile(r"^(.+?)Z$")


def _parse_wall(ts):
    if not isinstance(ts, str):
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        return None


def _summarize(nodes_cfg, batches, skews, pre_results, iterations):
    lines = [f"# T3 Repetition — {iterations} sends per direction", ""]

    total_verdicts: Counter = Counter()
    for b in batches:
        for r in b["iteration_rows"]:
            total_verdicts[r["verdict"]] += 1
    lines.append("## Aggregate verdict counts (both directions combined)")
    lines.append("")
    for v, c in total_verdicts.most_common():
        note = ""
        if v == "SENDER_SAYS_FAILED_BUT_RECIPIENT_RECEIVED":
            note = " ← THE BUG"
        lines.append(f"- {v}: {c}{note}")
    lines.append("")

    for b in batches:
        rows = b["iteration_rows"]
        ok_count = sum(1 for r in rows if r["verdict"] == "BOTH_AGREE_SENT")
        false_err = sum(1 for r in rows if r["verdict"] == "SENDER_SAYS_FAILED_BUT_RECIPIENT_RECEIVED")
        honest_fail = sum(1 for r in rows if r["verdict"] == "BOTH_AGREE_FAILED")
        lost = sum(1 for r in rows if r["verdict"] == "SENDER_SAYS_SENT_NOT_RECEIVED")
        incon = sum(1 for r in rows if r["verdict"] == "INCONCLUSIVE")

        lines.append(f"## {b['sender']} → {b['receiver']}  (batch verdict: {b['verdict']})")
        lines.append("")
        lines.append(f"- iterations: {b['iterations']} (spacing {b['send_spacing_s']}s)")
        lines.append(f"- BOTH_AGREE_SENT: {ok_count}/{len(rows)}")
        lines.append(f"- BOTH_AGREE_FAILED: {honest_fail}")
        lines.append(f"- SENDER_SAYS_SENT_NOT_RECEIVED: {lost}")
        lines.append(f"- SENDER_SAYS_FAILED_BUT_RECIPIENT_RECEIVED: {false_err}  ← bug-of-interest count")
        lines.append(f"- INCONCLUSIVE: {incon}")

        # Latency distribution (wall-clock, subject to NTP skew)
        latencies = []
        for r in rows:
            s = _parse_wall(r["sender_post_wall"])
            rv = _parse_wall(r["recv_wall"])
            if s and rv:
                latencies.append((rv - s).total_seconds())
        if latencies:
            p = percentiles(latencies, [50, 95, 100])
            lines.append(
                f"- latency (wall-clock): p50={p[50]*1000:.0f}ms p95={p[95]*1000:.0f}ms max={p[100]*1000:.0f}ms (n={len(latencies)})"
            )
        else:
            lines.append("- latency: no wall-clock pairs available")

        # Clustering: were failures back-to-back?
        failure_indices = [i for i, r in enumerate(rows) if r["verdict"] != "BOTH_AGREE_SENT"]
        if failure_indices:
            clusters = _cluster_consecutive(failure_indices)
            biggest = max((len(c) for c in clusters), default=0)
            lines.append(f"- failure index clusters: {clusters} (largest consecutive run: {biggest})")
        else:
            lines.append("- failure index clusters: none (all iterations were BOTH_AGREE_SENT)")

        # Full listing of the bug-of-interest cases
        false_err_rows = [r for r in rows if r["verdict"] == "SENDER_SAYS_FAILED_BUT_RECIPIENT_RECEIVED"]
        if false_err_rows:
            lines.append("")
            lines.append("### SENDER_SAYS_FAILED_BUT_RECIPIENT_RECEIVED — full listing")
            lines.append("")
            for r in false_err_rows:
                lines.append(
                    f"- marker=`{r['marker']}` sender_err_payload=`{r['sender_err_payload']}` "
                    f"sender_post_wall=`{r['sender_post_wall']}` recv_wall=`{r['recv_wall']}`"
                )
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


def _cluster_consecutive(indices):
    if not indices:
        return []
    clusters = [[indices[0]]]
    for idx in indices[1:]:
        if idx == clusters[-1][-1] + 1:
            clusters[-1].append(idx)
        else:
            clusters.append([idx])
    return clusters
