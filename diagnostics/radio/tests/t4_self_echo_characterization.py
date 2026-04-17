"""T4 — Self-echo characterization.

Answers two questions before any heard-count UI/schema work:

  Q1 (path-determining): does the SENDING node hear its own
      transmissions echoed back via local repeaters as
      CHANNEL_MSG_RECV events? If NO, the heard-count feature
      cannot be built on CHANNEL_MSG_RECV from the sender side and
      must use RX_LOG_DATA with manual decryption.

  Q2 (parameter): how long should the application keep an outbound
      message in active echo-matching state before retiring it?
      This is a correctness/dedup-lifetime question, not a UI
      responsiveness question.

Two batches: pi4 sends 5 markers, then zero2w sends 5 markers. Both
nodes listen continuously throughout each batch. Each marker is
unique (per-send uuid) so cross-marker disambiguation is trivial.
Matching uses the strict composite key (text, sender_timestamp,
txt_type) — substring matches are deliberately not used here, since
T4's whole point is to inform a feature where false attribution is
worse than undercount.
"""

from __future__ import annotations

import asyncio
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
    event_type_counts,
    find_self_info_name,
    make_expected_text,
    manifest_for,
    match_self_echo,
    parse_iso_to_epoch,
    percentiles,
)


TEST_NAME = "t4_self_echo_characterization"
DEFAULT_ITERATIONS = 5
SEND_SPACING_S = 5.0
PRE_SEND_DELAY_S = 2.0
POST_BATCH_LISTEN_S = 15.0
SENDER_TS_TOLERANCE_S = 2.0
LIFETIME_FLOOR_S = 15.0
HISTOGRAM_BUCKET_S = 0.5
HISTOGRAM_MAX_S = 20.0


async def run(
    nodes_cfg: NodesConfig,
    runs_dir: Path,
    run_dir: Path | None = None,
    *,
    iterations: int | None = None,
    do_set_channel: bool = True,
) -> dict:
    iterations = iterations if iterations is not None else DEFAULT_ITERATIONS
    if run_dir is None:
        run_dir = make_run_dir(runs_dir, TEST_NAME)

    run_id_base = f"{TEST_NAME}_{uuid.uuid4().hex[:8]}"
    marker_base = f"{nodes_cfg.test.message_prefix} t4 {run_id_base[-8:]}"
    console_log("mac", f"T4 self-echo characterization — run_dir={run_dir.name} iterations={iterations}/direction spacing={SEND_SPACING_S}s")

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
    batch_results: list[dict] = []
    for i, (sender, listener) in enumerate(directions, start=1):
        batch_id = f"{run_id_base}__{sender}_to_{listener}"
        marker_prefix = f"{marker_base} {sender}->{listener}"
        console_log("mac", f"phase: batch {i}/{len(directions)} — {sender} → {listener} ({iterations} sends)")
        batch = await _run_batch(
            nodes_cfg, run_dir, batch_id, marker_prefix, sender, listener,
            iterations=iterations,
            do_set_channel=do_set_channel,
        )
        batch_results.append(batch)
        if i < len(directions):
            console_log("mac", "cooling down 5s between directions")
            await asyncio.sleep(5.0)

    console_log("mac", "phase: analyzing self-echo + computing lifetime histogram")
    summary, q1_verdict = _summarize(nodes_cfg, batch_results, skews, pre_results, iterations)
    write_summary(run_dir, summary)
    write_manifest(run_dir, {
        **manifest_for(TEST_NAME, marker_base, nodes_cfg, run_id_base, sender=None, receiver=None),
        "iterations_per_direction": iterations,
        "send_spacing_s": SEND_SPACING_S,
        "post_batch_listen_s": POST_BATCH_LISTEN_S,
        "skews": skews,
        "preflight": [pr.__dict__ for pr in pre_results],
        "q1_verdict": q1_verdict,
        "batches": [_batch_manifest(b) for b in batch_results],
    })
    update_latest_symlink(runs_dir, run_dir)
    console_log("mac", f"T4 Q1 verdict: {q1_verdict}")
    return {"verdict": q1_verdict, "run_dir": str(run_dir)}


async def _run_batch(nodes_cfg, run_dir, batch_id, marker_prefix, sender, listener, *,
                     iterations, do_set_channel):
    total_send_window_s = PRE_SEND_DELAY_S + iterations * SEND_SPACING_S
    sender_listen = POST_BATCH_LISTEN_S
    listener_listen = total_send_window_s + sender_listen + 5.0
    hard_deadline_sender = total_send_window_s + sender_listen + 60.0
    hard_deadline_listener = listener_listen + 60.0

    sender_params = _params(
        nodes_cfg, sender, batch_id, marker_prefix,
        role="send_repeat",
        listen_after_s=sender_listen,
        pre_send_delay_s=PRE_SEND_DELAY_S,
        iterations=iterations,
        do_set_channel=do_set_channel,
    )
    listener_params = _params(
        nodes_cfg, listener, batch_id, marker_prefix,
        role="listen",
        listen_after_s=listener_listen,
        pre_send_delay_s=0.0,
        do_set_channel=do_set_channel,
    )
    sender_task = run_node(nodes_cfg.node(sender), sender_params, run_dir, hard_deadline_s=hard_deadline_sender)
    listener_task = run_node(nodes_cfg.node(listener), listener_params, run_dir, hard_deadline_s=hard_deadline_listener)
    sender_result, listener_result = await asyncio.gather(sender_task, listener_task)

    return {
        "batch_id": batch_id,
        "sender": sender,
        "listener": listener,
        "marker_prefix": marker_prefix,
        "iterations": iterations,
        "sender_result": sender_result,
        "listener_result": listener_result,
    }


def _params(nodes_cfg, node_name, run_id, marker, *, role, listen_after_s, pre_send_delay_s,
            iterations=1, do_set_channel=True):
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
        "send_spacing_s": SEND_SPACING_S,
        "iterations": iterations,
        "pre_send_delay_s": pre_send_delay_s,
        "stall_threshold_s": 30.0,
        "do_set_channel": do_set_channel,
    }


def _batch_manifest(b):
    return {
        "batch_id": b["batch_id"],
        "sender": b["sender"],
        "listener": b["listener"],
        "marker_prefix": b["marker_prefix"],
        "iterations": b["iterations"],
        "sender_result": _nr_dict(b["sender_result"]),
        "listener_result": _nr_dict(b["listener_result"]),
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


def _find_send_pres(events):
    return [ev for ev in events if isinstance(ev, dict) and ev.get("event_type") == "SEND_PRE"]


def _find_send_results(events):
    return [ev for ev in events if isinstance(ev, dict) and ev.get("event_type") == "SEND_RESULT"]


def _find_rx_log_data(events):
    return [ev for ev in events if isinstance(ev, dict) and ev.get("event_type") == "RX_LOG_DATA"]


def _summarize(nodes_cfg, batches, skews, pre_results, iterations):
    """Build summary.md text, return (text, q1_verdict_str)."""
    lines: list[str] = []
    lines.append(f"# T4 Self-Echo Characterization — {iterations} sends per direction")
    lines.append("")

    # Per-batch matching pass; collect echoes for both Q1 (sender vantage)
    # and Q2 (lifetime histogram across all vantages).
    per_batch_analysis = []
    sender_echo_total = 0   # CHANNEL_MSG_RECV matches on sender, all batches
    sender_send_total = 0
    sender_rx_log_total = 0
    all_offsets: list[float] = []   # any-vantage CHANNEL_MSG_RECV offsets (s)
    sender_offsets: list[float] = []
    listener_offsets: list[float] = []
    all_path_lens: list[int] = []
    per_marker_counts: list[int] = []  # echoes per marker (any vantage)

    for b in batches:
        sender = b["sender"]
        listener = b["listener"]
        sender_events = b["sender_result"].events or []
        listener_events = b["listener_result"].events or []

        sender_name = find_self_info_name(sender_events) or sender

        send_pres = _find_send_pres(sender_events)
        send_results = _find_send_results(sender_events)
        # Build a {marker -> SEND_PRE event} map
        send_pres_by_marker = {}
        for sp in send_pres:
            m = (sp.get("payload") or {}).get("marker")
            if m:
                send_pres_by_marker[m] = sp

        # Per-marker analysis
        marker_rows = []
        for sp in send_pres:
            payload = sp.get("payload") or {}
            marker = payload.get("marker")
            if not marker:
                continue
            send_wall_iso = payload.get("pre_wall_ts") or sp.get("wall_ts")
            send_epoch = parse_iso_to_epoch(send_wall_iso)
            if send_epoch is None:
                marker_rows.append({
                    "marker": marker,
                    "send_wall": send_wall_iso,
                    "skipped": "could not parse send wall_ts",
                })
                continue

            expected_text = make_expected_text(sender_name, marker)

            sender_matches = match_self_echo(
                sender_events, expected_text, send_epoch,
                txt_type=0, ts_tolerance_s=SENDER_TS_TOLERANCE_S,
            )
            listener_matches = match_self_echo(
                listener_events, expected_text, send_epoch,
                txt_type=0, ts_tolerance_s=SENDER_TS_TOLERANCE_S,
            )

            sender_offset_list = []
            for ev in sender_matches:
                ep = parse_iso_to_epoch(ev.get("wall_ts"))
                if ep is not None:
                    off = ep - send_epoch
                    sender_offset_list.append(off)
                    all_offsets.append(off)
                    sender_offsets.append(off)
                    pl = (ev.get("payload") or {}).get("path_len")
                    if isinstance(pl, int):
                        all_path_lens.append(pl)

            listener_offset_list = []
            for ev in listener_matches:
                ep = parse_iso_to_epoch(ev.get("wall_ts"))
                if ep is not None:
                    off = ep - send_epoch
                    listener_offset_list.append(off)
                    all_offsets.append(off)
                    listener_offsets.append(off)
                    pl = (ev.get("payload") or {}).get("path_len")
                    if isinstance(pl, int):
                        all_path_lens.append(pl)

            per_marker_counts.append(len(sender_matches) + len(listener_matches))

            marker_rows.append({
                "marker": marker,
                "send_wall": send_wall_iso,
                "send_epoch": send_epoch,
                "expected_text": expected_text,
                "sender_match_count": len(sender_matches),
                "listener_match_count": len(listener_matches),
                "sender_offsets_s": sender_offset_list,
                "listener_offsets_s": listener_offset_list,
                "sender_path_lens": [
                    (ev.get("payload") or {}).get("path_len") for ev in sender_matches
                ],
                "listener_path_lens": [
                    (ev.get("payload") or {}).get("path_len") for ev in listener_matches
                ],
            })

        # Sender-vantage Q1 totals
        sender_with_echo = sum(1 for r in marker_rows if r.get("sender_match_count", 0) > 0)
        sender_send_count = len(send_pres)
        sender_rx_log_count = len(_find_rx_log_data(sender_events))
        sender_echo_total += sum(r.get("sender_match_count", 0) for r in marker_rows)
        sender_send_total += sender_send_count
        sender_rx_log_total += sender_rx_log_count

        per_batch_analysis.append({
            "sender": sender,
            "listener": listener,
            "sender_name": sender_name,
            "sender_send_count": sender_send_count,
            "sender_with_echo": sender_with_echo,
            "sender_rx_log_count": sender_rx_log_count,
            "marker_rows": marker_rows,
            "sender_event_counts": event_type_counts(sender_events),
            "listener_event_counts": event_type_counts(listener_events),
        })

    # Q1 verdict — careful about distinguishing NO from UNCLEAR.
    #
    # Naive logic ("sender saw any RX_LOG_DATA → must have heard own packet
    # back → therefore firmware suppresses CHANNEL_MSG_RECV") is wrong:
    # RX_LOG_DATA on the sender could be ambient traffic from other
    # operators on the channel, not a repeater rebroadcasting OUR message.
    #
    # Definitive evidence that a repeater is active in this test:
    # at least one CHANNEL_MSG_RECV (any vantage, any marker) with
    # path_len > 0. Without that, this test cannot rule out "no repeater
    # rebroadcast our packet" and Q1 must remain UNCLEAR.
    any_repeater_seen = False
    for b in per_batch_analysis:
        for r in b["marker_rows"]:
            for pl in (r.get("sender_path_lens") or []) + (r.get("listener_path_lens") or []):
                if isinstance(pl, int) and pl > 0:
                    any_repeater_seen = True
                    break
            if any_repeater_seen:
                break
        if any_repeater_seen:
            break

    if sender_echo_total > 0:
        q1_verdict = "YES"
        q1_line = (
            f"**Q1 — Self-echo via CHANNEL_MSG_RECV: YES** "
            f"({sender_echo_total} matched echoes across {sender_send_total} sender-side sends)"
        )
    elif not any_repeater_seen:
        # No echoes at all (sender or listener) carried path_len > 0, so
        # we never observed a repeater participating in this run. Cannot
        # distinguish firmware suppression from "no rebroadcast happened
        # at all."
        q1_verdict = "UNCLEAR_NO_REPEATER"
        q1_line = (
            "**Q1 — Self-echo via CHANNEL_MSG_RECV: UNCLEAR (no repeater observed)** "
            "(0 sender-side echoes; no path_len > 0 events anywhere in this run, so "
            "no repeater is rebroadcasting on this channel near these nodes — "
            "we cannot distinguish firmware suppression from 'nothing to echo'. "
            "Re-run from a location with confirmed repeater activity, or check the "
            "Android client at this location to see if it observes any path_len > 0 "
            "messages on this channel.)"
        )
    else:
        # Repeater activity WAS observed (path_len > 0 elsewhere) but
        # zero of those rebroadcasts decoded as CHANNEL_MSG_RECV on the
        # sender. That's evidence the firmware suppresses self-echoes.
        q1_verdict = "NO"
        q1_line = (
            f"**Q1 — Self-echo via CHANNEL_MSG_RECV: NO** "
            f"(0 sender-side echoes across {sender_send_total} sends, despite "
            f"path_len > 0 events being observed elsewhere in this run — "
            f"firmware appears to suppress self-echo decoding)"
        )

    lines.append(q1_line)
    lines.append("")
    lines.append(
        f"_Sender RX_LOG_DATA events (any source — own echo, ambient, or other): "
        f"{sender_rx_log_total} across batches. Treat as suggestive only; without "
        f"per-event decryption we can't say which were self-echoes._"
    )
    lines.append("")

    if q1_verdict == "NO":
        lines.append(
            "**Implementation path: heard-count must be built from "
            "RX_LOG_DATA with manual decryption; CHANNEL_MSG_RECV cannot be "
            "used on the sender side.** The Q2 lifetime histogram below is "
            "still useful (it characterizes echoes seen on the OTHER node) "
            "but the production code path will need to subscribe to "
            "RX_LOG_DATA on the sender, decrypt the payload using the "
            "channel secret, and match against locally-originated sends."
        )
        lines.append("")
    elif q1_verdict == "UNCLEAR_NO_REPEATER":
        lines.append(
            "**Implementation path is undecided.** No repeater activity was "
            "observed during this run, so Q1 cannot be answered. Until Q1 "
            "is answered, the heard-count feature design has two possible "
            "implementation paths and we don't know which is correct."
        )
        lines.append("")

    # Q2 — lifetime histogram + recommendation
    lines.append(f"## Q2 — Echo arrival distribution (any vantage)")
    lines.append("")
    if not all_offsets:
        lines.append("_No matching CHANNEL_MSG_RECV echoes found on any vantage. Cannot compute lifetime recommendation._")
        lines.append("")
        lines.append(f"**Recommended echo-matching lifetime: {LIFETIME_FLOOR_S:.0f}s** "
                     f"(falling back to floor; no observed data to inform a tighter value)")
    else:
        observed_max = max(all_offsets)
        p_stats = percentiles(all_offsets, [50, 95, 99])
        recommended = max(observed_max + 5.0, LIFETIME_FLOOR_S)
        lines.append(
            f"- total matched echoes: {len(all_offsets)} "
            f"(sender-vantage: {len(sender_offsets)}, listener-vantage: {len(listener_offsets)})"
        )
        lines.append(
            f"- offset stats (s): p50={p_stats[50]:.2f}  p95={p_stats[95]:.2f}  "
            f"p99={p_stats[99]:.2f}  max={observed_max:.2f}"
        )
        if per_marker_counts:
            pmc_stats = percentiles([float(c) for c in per_marker_counts], [50, 95])
            lines.append(
                f"- echoes per marker: min={min(per_marker_counts)}  median={pmc_stats[50]:.1f}  "
                f"p95={pmc_stats[95]:.1f}  max={max(per_marker_counts)}"
            )
        if all_path_lens:
            pl_counter = Counter(all_path_lens)
            pl_summary = " ".join(
                f"len={k}:{v}" for k, v in sorted(pl_counter.items())
            )
            lines.append(f"- path_len distribution: {pl_summary}")
        lines.append("")
        lines.append(
            f"**Recommended echo-matching lifetime: {recommended:.0f}s** "
            f"(observed_max={observed_max:.2f}s + 5s safety; floor={LIFETIME_FLOOR_S:.0f}s)"
        )
        lines.append("")
        lines.append("### Histogram (offset bucket → count, both vantages)")
        lines.append("")
        lines.extend(_render_histogram(all_offsets, HISTOGRAM_BUCKET_S, HISTOGRAM_MAX_S))
        lines.append("")

    # Per-batch detail
    for b in per_batch_analysis:
        lines.append(f"## Batch: {b['sender']} → {b['listener']}  (sender_name={b['sender_name']})")
        lines.append("")
        lines.append(
            f"- sender sends: {b['sender_send_count']}  "
            f"sender CHANNEL_MSG_RECV self-matches: {sum(r.get('sender_match_count', 0) for r in b['marker_rows'])}  "
            f"sender RX_LOG_DATA events: {b['sender_rx_log_count']}"
        )
        lines.append(
            f"- markers with ≥1 sender-side echo: "
            f"{b['sender_with_echo']}/{b['sender_send_count']}"
        )
        lines.append("")
        lines.append("| marker | send (UTC) | sender echoes | listener echoes | sender path_lens | listener path_lens |")
        lines.append("|---|---|---|---|---|---|")
        for r in b["marker_rows"]:
            if r.get("skipped"):
                lines.append(f"| `{r['marker']}` | `{r['send_wall']}` | (skipped: {r['skipped']}) | | | |")
                continue
            sender_offsets_str = (
                ", ".join(f"{o:.2f}s" for o in r["sender_offsets_s"]) or "—"
            )
            listener_offsets_str = (
                ", ".join(f"{o:.2f}s" for o in r["listener_offsets_s"]) or "—"
            )
            sender_pls = (
                ", ".join(str(x) for x in r["sender_path_lens"] if x is not None) or "—"
            )
            listener_pls = (
                ", ".join(str(x) for x in r["listener_path_lens"] if x is not None) or "—"
            )
            lines.append(
                f"| `{_truncate(r['marker'], 40)}` | `{r['send_wall']}` | "
                f"{r['sender_match_count']} ({sender_offsets_str}) | "
                f"{r['listener_match_count']} ({listener_offsets_str}) | "
                f"{sender_pls} | {listener_pls} |"
            )
        lines.append("")

    # Clock skew + preflight footer
    lines.append("## Clock skew (informational — affects sender_timestamp tolerance)")
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

    return "\n".join(lines), q1_verdict


def _render_histogram(offsets_s: list[float], bucket_s: float, max_s: float) -> list[str]:
    """Plain-text histogram of offsets, fixed-width bars."""
    if not offsets_s:
        return ["_no data_"]
    n_buckets = int(max_s / bucket_s) + 1
    counts = [0] * n_buckets
    overflow = 0
    for off in offsets_s:
        if off < 0:
            continue
        idx = int(off / bucket_s)
        if idx >= n_buckets:
            overflow += 1
        else:
            counts[idx] += 1
    max_count = max(counts) if counts else 0
    if max_count == 0 and overflow == 0:
        return ["_no data in range_"]
    bar_width = 40
    out: list[str] = []
    for i, c in enumerate(counts):
        if c == 0:
            continue
        lo = i * bucket_s
        hi = (i + 1) * bucket_s
        bar = "#" * max(1, int(c * bar_width / max(max_count, 1)))
        out.append(f"`{lo:5.1f}–{hi:5.1f}s` {bar} {c}")
    if overflow:
        out.append(f"`>{max_s:.1f}s        ` {overflow} (overflow)")
    return out


def _truncate(s: str, n: int) -> str:
    if len(s) <= n:
        return s
    return s[: n - 1] + "…"
