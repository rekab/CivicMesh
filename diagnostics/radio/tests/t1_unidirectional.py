"""T1 — Unidirectional send: pi4 → zero2w.

Both nodes open serial and subscribe to all events. After a sync delay
(so the receiver is definitely subscribed before the sender transmits),
the sender calls send_chan_msg once. Both keep listening for 10s after
the sender returns. Verdict cross-checks sender's reported result
against recipient's CHANNEL_MSG_RECV stream."""

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
    classify_send,
    event_type_counts,
    find_send_results,
    manifest_for,
    recipient_received_marker,
)


TEST_NAME = "t1_unidirectional"
SENDER = "pi4"
RECEIVER = "zero2w"
PRE_SEND_DELAY_S = 2.0
POST_SEND_LISTEN_S = 10.0


async def run(
    nodes_cfg: NodesConfig,
    runs_dir: Path,
    run_dir: Path | None = None,
    *,
    sender: str = SENDER,
    receiver: str = RECEIVER,
    test_name: str = TEST_NAME,
    do_set_channel: bool = True,
) -> dict:
    if run_dir is None:
        run_dir = make_run_dir(runs_dir, f"{test_name}_{sender}_to_{receiver}")

    run_id = f"{test_name}_{uuid.uuid4().hex[:8]}"
    marker = f"{nodes_cfg.test.message_prefix} {test_name.split('_')[0]} {run_id[-8:]} s={sender} r={receiver}"
    if len(marker) > 140:
        marker = marker[:140]

    console_log("mac", f"{test_name} — run_dir={run_dir.name} sender={sender} receiver={receiver}")
    console_log("mac", f"marker=\"{marker}\"")
    console_log("mac", f"phase: preflight on [{sender}, {receiver}] (parallel)")
    pre_results = await asyncio.gather(
        preflight(nodes_cfg.node(sender), nodes_cfg.test.channel),
        preflight(nodes_cfg.node(receiver), nodes_cfg.test.channel),
    )
    if any(not pr.ok for pr in pre_results):
        console_log("mac", f"⚠️ preflight failed — aborting {test_name}")
        return _write_preflight_failure(test_name, run_dir, runs_dir, pre_results, nodes_cfg, marker, run_id, sender, receiver)

    console_log("mac", "phase: clock skew probe (parallel)")
    skews = await asyncio.gather(
        probe_clock_skew(nodes_cfg.node(sender)),
        probe_clock_skew(nodes_cfg.node(receiver)),
    )

    sender_listen = POST_SEND_LISTEN_S
    receiver_listen = PRE_SEND_DELAY_S + sender_listen + 5.0
    hard_deadline_sender = PRE_SEND_DELAY_S + sender_listen + 30.0
    hard_deadline_receiver = receiver_listen + 30.0

    sender_params = _params(
        nodes_cfg, sender, run_id, marker,
        role="send_once",
        listen_after_s=sender_listen,
        pre_send_delay_s=PRE_SEND_DELAY_S,
        do_set_channel=do_set_channel,
    )
    receiver_params = _params(
        nodes_cfg, receiver, run_id, marker,
        role="listen",
        listen_after_s=receiver_listen,
        pre_send_delay_s=0.0,
        do_set_channel=do_set_channel,
    )

    console_log(
        "mac",
        f"phase: spawning sender (role=send_once) and receiver (role=listen). "
        f"sender pre_send_delay={PRE_SEND_DELAY_S}s, listens {sender_listen}s after send; "
        f"receiver listens {receiver_listen}s total",
    )
    sender_task = run_node(nodes_cfg.node(sender), sender_params, run_dir, hard_deadline_s=hard_deadline_sender)
    receiver_task = run_node(nodes_cfg.node(receiver), receiver_params, run_dir, hard_deadline_s=hard_deadline_receiver)
    sender_result, receiver_result = await asyncio.gather(sender_task, receiver_task)

    console_log("mac", "phase: computing verdict and writing summary.md + manifest.json")
    summary, verdict = _summarize(
        test_name, sender, receiver, marker, sender_result, receiver_result, skews, pre_results
    )
    console_log("mac", f"{test_name} verdict: {verdict}")
    write_summary(run_dir, summary)
    write_manifest(run_dir, {
        **manifest_for(test_name, marker, nodes_cfg, run_id, sender=sender, receiver=receiver),
        "verdict": verdict,
        "pre_send_delay_s": PRE_SEND_DELAY_S,
        "sender_listen_s": sender_listen,
        "receiver_listen_s": receiver_listen,
        "skews": skews,
        "preflight": [pr.__dict__ for pr in pre_results],
        "sender_result": _node_result_dict(sender_result),
        "receiver_result": _node_result_dict(receiver_result),
    })
    update_latest_symlink(runs_dir, run_dir)
    return {"verdict": verdict, "run_dir": str(run_dir)}


def _params(nodes_cfg, node_name, run_id, marker, *, role, listen_after_s, pre_send_delay_s,
            do_set_channel=True):
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
        "send_spacing_s": nodes_cfg.test.min_send_spacing_s,
        "iterations": 1,
        "pre_send_delay_s": pre_send_delay_s,
        "stall_threshold_s": 30.0,
        "do_set_channel": do_set_channel,
    }


def _node_result_dict(nr):
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


def _write_preflight_failure(test_name, run_dir, runs_dir, pre_results, nodes_cfg, marker, run_id, sender, receiver) -> dict:
    lines = [f"# {test_name} — PREFLIGHT FAILED", ""]
    for pr in pre_results:
        lines.append(f"- {pr.summary_line()}")
    write_summary(run_dir, "\n".join(lines))
    write_manifest(run_dir, {
        **manifest_for(test_name, marker, nodes_cfg, run_id, sender=sender, receiver=receiver),
        "preflight_failed": True,
        "preflight": [pr.__dict__ for pr in pre_results],
    })
    update_latest_symlink(runs_dir, run_dir)
    return {"verdict": "PREFLIGHT_FAILED", "run_dir": str(run_dir)}


def _summarize(test_name, sender, receiver, marker, sender_result, receiver_result, skews, pre_results):
    send_results = find_send_results(sender_result.events)
    primary_send = send_results[-1] if send_results else None
    recv_match = recipient_received_marker(receiver_result.events, marker)
    verdict = classify_send(primary_send, recv_match)

    sender_payload = primary_send.get("payload") if primary_send else None
    sender_type = sender_payload.get("type") if isinstance(sender_payload, dict) else None
    sender_err_payload = sender_payload.get("payload") if isinstance(sender_payload, dict) else None

    sender_counts = event_type_counts(sender_result.events)
    receiver_counts = event_type_counts(receiver_result.events)

    latency_s = None
    if primary_send and recv_match:
        send_post_mono = (sender_payload or {}).get("post_mono_ts") if isinstance(sender_payload, dict) else None
        recv_mono = recv_match.get("mono_ts")
        if isinstance(send_post_mono, (int, float)) and isinstance(recv_mono, (int, float)):
            # NOTE: mono clocks are PER-NODE, so this delta is not meaningful
            # cross-machine. Reported as wall-clock delta below for sanity;
            # mono delta is omitted unless same-host (which it never is here).
            pass
        try:
            from datetime import datetime
            send_wall = datetime.fromisoformat((sender_payload or {}).get("post_wall_ts", "").replace("Z", "+00:00"))
            recv_wall = datetime.fromisoformat(recv_match.get("wall_ts", "").replace("Z", "+00:00"))
            latency_s = (recv_wall - send_wall).total_seconds()
        except Exception:
            latency_s = None

    lines = []
    lines.append(f"# {test_name} — {sender} → {receiver}")
    lines.append("")
    lines.append(f"**VERDICT: {verdict}**")
    lines.append("")
    lines.append(f"- marker: `{marker}`")
    lines.append(f"- sender's send_chan_msg returned: type=`{sender_type}` payload=`{sender_err_payload}`")
    lines.append(f"- recipient saw CHANNEL_MSG_RECV matching marker: {recv_match is not None}")
    if latency_s is not None:
        lines.append(f"- wall-clock latency (send → recv): {latency_s*1000:.0f}ms (subject to NTP skew)")
    if primary_send is None:
        lines.append("- ⚠️ no SEND_RESULT event from sender (check sender stderr for create_serial errors)")

    # Did sender hear its own transmission as RX_LOG_DATA / RX_*?
    sender_self_rx = sum(c for et, c in sender_counts.items() if et.startswith("RX") or et == "CHANNEL_MSG_RECV")
    lines.append(f"- sender self-RX events (any RX_*/CHANNEL_MSG_RECV): {sender_self_rx}")

    # Receiver raw RX even if no decode
    receiver_raw_rx = sum(c for et, c in receiver_counts.items() if et.startswith("RX"))
    lines.append(f"- receiver raw RX_* events (any): {receiver_raw_rx} — distinguishes RF from decode failure")
    lines.append("")

    lines.append("## Verdict legend")
    lines.append("")
    lines.append("- BOTH_AGREE_SENT — sender said OK, recipient received. Healthy round trip.")
    lines.append("- BOTH_AGREE_FAILED — sender said ERROR, recipient got nothing. Honest failure.")
    lines.append("- SENDER_SAYS_FAILED_BUT_RECIPIENT_RECEIVED — ⚠️ the bug we're hunting.")
    lines.append("- SENDER_SAYS_SENT_NOT_RECEIVED — sent cleanly but lost on air.")
    lines.append("- INCONCLUSIVE — missing data; check raw events.jsonl.")
    lines.append("")

    for nr, label, counts in (
        (sender_result, "sender", sender_counts),
        (receiver_result, "receiver", receiver_counts),
    ):
        lines.append(f"## {label} ({nr.node_name})")
        lines.append("")
        lines.append(f"- exit_code: {nr.exit_code} | hardkilled: {nr.hardkilled} | test_complete: {nr.test_complete} | stream_loss: {nr.stream_loss}")
        lines.append(f"- streamed_lines: {nr.streamed_line_count}  | authoritative_lines: {nr.authoritative_event_count}")
        lines.append(f"- total events: {sum(counts.values())}")
        if counts:
            lines.append("- counts by event_type:")
            for et, c in sorted(counts.items(), key=lambda kv: -kv[1]):
                lines.append(f"    - {et}: {c}")
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

    return "\n".join(lines), verdict
