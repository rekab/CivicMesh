"""MeshCore radio relay for CivicMesh.

Async process driving the Heltec V3 radio over USB serial via the
`meshcore` library. Three concurrent asyncio tasks: an outbox sender
(serial-queue, exponential backoff, echo-confirmed retries), a
retention pruner (drops old rows from the messages / heard_packets /
telemetry tables), and a heartbeat recorder that updates the status
table for liveness monitoring.

Reads from and writes to the same SQLite database as `web_server.py`;
there is no other IPC. The radio link is best-effort — outbox sends
are paced, the queue depth is capped, and a sustained hang triggers a
`RecoveryController` reset via RTS pulse on the serial port (see
`recovery.py`). Entry point: `civicmesh-mesh`.
"""

import argparse
import asyncio
import functools
import hashlib
import logging
import time
from collections import deque
from typing import Any, Optional

from config import load_config
from database import (
    DBConfig,
    cleanup_retention_bytes_per_channel,
    get_outbox_message,
    get_outbox_snapshot,
    get_pending_outbox,
    increment_heard,
    init_db,
    increment_outbox_retry,
    insert_heard_packet,
    compute_and_persist_sender_ts,
    cross_boot_storage_hygiene,
    evaluate_and_maybe_apply_consensus,
    get_latest_clock_correction,
    insert_message_wall,
    insert_telemetry_event,
    mark_outbox_failed,
    prune_heard_packets,
    prune_telemetry,
    prune_terminal_outbox,
    reconcile_message_status,
    record_outbox_send,
    upsert_status,
    write_external_step_correction,
)
import telemetry
import clock
from clock import wall_now
from logger import setup_logging
from outbox_echoes import ActiveOutboxIndex
from recovery import RecoveryController, RecoveryState, liveness_task, recovery_task

EventType = None
MeshCore = None

DEFAULT_BAUDRATE = 115200

_log_db = logging.getLogger(__name__)


def _on_executor_done(fut):
    """done-callback for fire-and-forget DB futures."""
    exc = fut.exception()
    if exc is not None:
        _log_db.error("executor:db_error %s", exc, exc_info=exc)


def _executor_db(fn, *args, **kwargs):
    """Fire-and-forget DB call on the default executor. Logs exceptions."""
    loop = asyncio.get_running_loop()
    fut = loop.run_in_executor(None, functools.partial(fn, *args, **kwargs))
    fut.add_done_callback(_on_executor_done)


# Retention cutoffs and any other comparison against wall-corrected ts
# columns go through wall_now (not raw time.time()) so the cutoff is in
# the same reference frame as the stored ts. After CIV-99 the stored ts
# is `int(time.time()) + clock_state.offset_seconds`; a raw cutoff would
# be too aggressive when offset > 0 and not aggressive enough when
# offset < 0.
async def _retention_task(cfg, db_cfg: DBConfig, log):
    while True:
        try:
            channels = list(dict.fromkeys(cfg.channels.names + cfg.local.names))
            for ch in channels:
                deleted = cleanup_retention_bytes_per_channel(
                    db_cfg, channel=ch, max_bytes=cfg.limits.retention_bytes_per_channel, log=log
                )
                if deleted:
                    log.info("retention:channel=%s deleted=%d", ch, deleted)
            # Prune heard_packets older than 8 days (1 week + 1 day slack)
            try:
                prune_heard_packets(db_cfg, cutoff_ts=wall_now(db_cfg) - 8 * 86400, log=log)
            except Exception as e:
                log.error("retention:heard_packets_error %s", e, exc_info=True)
            # Prune terminal outbox rows older than 8 days
            try:
                prune_terminal_outbox(db_cfg, cutoff_ts=wall_now(db_cfg) - 8 * 86400, log=log)
            except Exception as e:
                log.error("retention:outbox_error %s", e, exc_info=True)
            # Prune telemetry: samples after 7 days, events after 30 days
            try:
                prune_telemetry(
                    db_cfg,
                    samples_cutoff_ts=wall_now(db_cfg) - 7 * 86400,
                    events_cutoff_ts=wall_now(db_cfg) - 30 * 86400,
                    log=log,
                )
            except Exception as e:
                log.error("retention:telemetry_error %s", e, exc_info=True)
            # NOTE (CIV-99): no retention on clock_corrections. The
            # column we'd compare against (system_time_after) is in
            # different reference frames for different triggers —
            # raw for 'consensus' and 'external_step', wall-after-jump
            # for 'admin' — so a single cutoff would silently mix
            # frames. Production with masked NTP produces only a
            # handful of rows per day, so unbounded growth is not
            # urgent. If retention becomes necessary, add an explicit
            # wall-comparable column first.
        except Exception as e:
            log.error("retention:error %s", e, exc_info=True)
        await asyncio.sleep(3600)


async def _clock_task(cfg, db_cfg: DBConfig, log):
    """Wall-clock consensus task (CIV-99).

    Periodically evaluates client clock reports stored on the sessions
    table and updates clock_state.offset_seconds via the pure math in
    clock.evaluate_consensus. Also detects external clock steps (NTP
    re-enabling, manual `date`, etc.) by watching the wall-monotonic
    signal and rebases offset to 0 when one occurs without an
    attributable admin row.

    Design contract (see docs/clock_consensus.md):

      - This is the SOLE auto-path writer of clock_state.offset_seconds.
        The civicmesh-set-clock admin command writes too, but as a
        separate root process under BEGIN EXCLUSIVE.
      - vote_epoch is bumped only on admin/external_step writes. Consensus
        applies votes, it doesn't invalidate them.
      - first_correction_done is derived from 'consensus' rows in the
        current boot epoch ONLY. 'admin' and 'external_step' rows do NOT
        consume the privilege.
      - nudge == 0 acceptances are suppressed (no audit row, no telemetry).
    """
    boot_id = clock.get_boot_id()
    cross_boot_storage_hygiene(db_cfg, current_boot_id=boot_id, log=log)

    cc = cfg.clock
    consensus_cfg = clock.ConsensusConfig(
        quorum_min_cookies=cc.quorum_min_cookies,
        sanity_floor_epoch=cc.sanity_floor_epoch,
        sanity_ceiling_epoch=cc.sanity_ceiling_epoch,
        max_nudge_sec=cc.max_nudge_sec,
    )

    latest = get_latest_clock_correction(db_cfg, log=log)
    last_seen_id = int(latest["id"]) if latest else 0

    # Wall-monotonic signal baseline. Used to detect external clock
    # steps via |Δsignal| > external_step_threshold_sec.
    wall_at_last_tick = int(time.time())
    mono_at_last_tick = time.monotonic()

    log.info(
        "clock:task_started boot_id=%s last_seen_correction_id=%d",
        boot_id, last_seen_id,
    )

    while True:
        try:
            wall_t = int(time.time())
            mono_t = time.monotonic()
            signal_now = wall_t - mono_t
            signal_last = wall_at_last_tick - mono_at_last_tick

            # DEBUG-only per-tick heartbeat. Shows the external-step
            # detector's input (the wall-monotonic signal and its delta
            # since the last tick) so an operator running with
            # `log_level = "DEBUG"` can see WHY the detector did or
            # didn't fire. Silent at INFO.
            log.debug(
                "clock:tick wall=%d mono=%.1f signal=%.3f signal_last=%.3f "
                "delta=%.3f threshold=%d",
                wall_t, mono_t, signal_now, signal_last,
                abs(signal_now - signal_last),
                cc.external_step_threshold_sec,
            )

            # --- External-step detection ---
            if abs(signal_now - signal_last) > cc.external_step_threshold_sec:
                # Was this step attributable to a fresh admin row? The
                # admin command writes its own clock_corrections row
                # under BEGIN EXCLUSIVE; we just need to refresh the
                # baseline silently in that case (no double audit, no
                # second cleanup).
                latest = get_latest_clock_correction(db_cfg, log=log)
                attributed = (
                    latest is not None
                    and int(latest["id"]) > last_seen_id
                    and latest.get("trigger") == "admin"
                )
                if attributed:
                    last_seen_id = int(latest["id"])
                    log.info(
                        "clock:external_step_attributed_to_admin id=%d", last_seen_id,
                    )
                else:
                    new_id = write_external_step_correction(
                        db_cfg,
                        source_summary_json=clock.summary_to_json({
                            "signal_delta_sec": signal_now - signal_last,
                            "wall_now": wall_t,
                            "wall_at_last_tick": wall_at_last_tick,
                            "mono_now": mono_t,
                            "mono_at_last_tick": mono_at_last_tick,
                        }),
                        applied_boot_id=boot_id,
                        log=log,
                    )
                    last_seen_id = new_id
                wall_at_last_tick = wall_t
                mono_at_last_tick = mono_t
                # Skip consensus this tick; state just changed under us.
                await asyncio.sleep(cc.eval_interval_sec)
                continue

            # --- Consensus ---
            # ATOMIC tick: read offset + vote_epoch + reports, evaluate,
            # and (if accepted) write the correction — all under one
            # BEGIN IMMEDIATE inside the helper. A naive split across
            # separate connections races against the admin command's
            # BEGIN EXCLUSIVE; see the docstring on
            # evaluate_and_maybe_apply_consensus and
            # docs/clock_consensus.md § "Centralized write helpers."
            applied = evaluate_and_maybe_apply_consensus(
                db_cfg,
                boot_id=boot_id,
                max_report_age_sec=cc.max_report_age_sec,
                min_cookie_age_sec=cc.min_cookie_age_sec,
                consensus_cfg=consensus_cfg,
                log=log,
            )
            if applied is not None:
                last_seen_id = applied["id"]
                insert_telemetry_event(
                    db_cfg,
                    kind="clock_consensus_accepted",
                    detail={
                        "offset_before_sec": applied["offset_before_sec"],
                        "offset_after_sec": applied["offset_after_sec"],
                        "nudge_sec": applied["nudge_sec"],
                        "voter_count": applied["voter_count"],
                        "median_offset_vote_sec": applied["median_offset_vote_sec"],
                        "accept_reason": applied["accept_reason"],
                    },
                    log=log,
                )

            wall_at_last_tick = wall_t
            mono_at_last_tick = mono_t
        except Exception as e:
            log.error("clock:task_error %s", e, exc_info=True)
        await asyncio.sleep(cc.eval_interval_sec)


async def _heartbeat_task(cfg, db_cfg: DBConfig, log, controller: RecoveryController):
    while True:
        try:
            state = controller.get_state()
            radio_connected = (state == RecoveryState.HEALTHY)
            upsert_status(
                db_cfg, process="mesh_bot",
                radio_connected=radio_connected,
                state=state.value, log=log,
            )
        except Exception as e:
            log.error("heartbeat:upsert_failed err=%s", e, exc_info=True)
        await asyncio.sleep(10)


class _GlobalEgressBucket:
    """Sliding-hour token bucket capping relay-wide mesh egress.

    Single-actor: only `_outbox_task` consults it. The asyncio
    single-threaded event loop removes any need for locking.

    `try_consume` is atomic check + record: a successful claim adds an
    entry to the deque before any send is attempted. The bucket therefore
    counts attempts, not successes — a flaky radio that throws ERROR on
    every send still spends budget, because bytes hit the air regardless
    of higher-level result.

    In-memory only. On process restart the deque is empty; the outbox
    queue-depth cap (`limits.outbox_max_depth`) bounds what pre-restart
    backlog can build, so cold-start granting full budget is not an
    amplification surface.

    Time is `time.monotonic()` from the caller, not wall clock — a
    single NTP step backward must not retroactively grant tokens.
    """

    def __init__(self, capacity_per_hour: int):
        self._capacity = capacity_per_hour
        self._sends: deque[float] = deque()

    def try_consume(self, now: float) -> tuple[bool, float]:
        cutoff = now - 3600
        while self._sends and self._sends[0] < cutoff:
            self._sends.popleft()
        if len(self._sends) < self._capacity:
            self._sends.append(now)
            return True, 0.0
        return False, 3600 - (now - self._sends[0])


async def _outbox_task(
    cfg,
    db_cfg: DBConfig,
    log,
    controller: RecoveryController,
    channel_name_to_idx: dict[str, int],
    active_outbox: ActiveOutboxIndex,
    self_name_provider,
    egress_bucket: _GlobalEgressBucket,
):
    # Backoff levels map to a delay sequence [0, 2, 5, max_delay_sec].
    # The last entry is configurable so operators can cap the max pacing.
    # We advance one level after each send attempt to avoid draining large
    # backlogs too quickly on the mesh.
    max_delay_sec = cfg.limits.outbox_max_delay_sec
    delays = [0, 2, 5, max_delay_sec]
    idle_reset_sec = cfg.limits.outbox_idle_reset_sec
    last_send_time: Optional[float] = None
    backoff_level = 0
    # Retry cap prevents infinite resend loops when the radio acks are missing.
    max_retries = getattr(cfg.limits, "outbox_max_retries", 3)
    consecutive_send_failures = 0
    # Egress bucket pause-state tracking. Telemetry fires once on entry and
    # once on exit, never on the per-30s recheck — sampling at the recheck
    # cadence would add no signal but would write ~120 rows/hour during a
    # sustained pause. The mid-pause log line below is also throttled.
    in_paused_state = False
    pause_started_mono = 0.0
    last_pause_log_ts = 0.0

    while True:
        try:
            if controller.outbox_should_pause():
                await asyncio.sleep(1)
                continue
            mc = controller.get_client()
            if mc is None:
                await asyncio.sleep(1)
                continue

            # CIV-99: monotonic for elapsed-time math. An admin command or
            # external NTP step that jumps the wall clock must not retro-
            # actively shrink the idle window or extend the backoff delay.
            now = time.monotonic()
            # If we've been idle long enough, reset the backoff so the next
            # message can go out immediately.
            if last_send_time is not None and now - last_send_time > idle_reset_sec:
                backoff_level = 0

            pending = await asyncio.to_thread(get_pending_outbox, db_cfg, limit=1, log=log)
            # Nothing pending: avoid hot loop but don't alter backoff state.
            if not pending:
                await asyncio.sleep(1)
                continue

            delay = delays[backoff_level]
            # Enforce the current backoff delay since the last send attempt.
            if last_send_time is not None and now - last_send_time < delay:
                await asyncio.sleep(delay - (now - last_send_time))
                continue

            item = pending[0]
            channel = item["channel"]
            sender = item["sender"]
            content = item["content"]
            # Global hourly egress cap. Consume on attempt, not success — bytes
            # hit the air regardless of higher-level result. Placed OUTSIDE
            # the inner try so a pause-and-continue does not advance the
            # backoff state machine via the inner finally.
            now_mono = time.monotonic()
            allowed, wait_sec = egress_bucket.try_consume(now_mono)
            if not allowed:
                if not in_paused_state:
                    queue_depth, queue_oldest_age_s = await asyncio.to_thread(
                        get_outbox_snapshot, db_cfg,
                        now_ts=wall_now(db_cfg), log=log,
                    )
                    await asyncio.to_thread(
                        insert_telemetry_event, db_cfg,
                        kind="egress_bucket_throttled",
                        detail={
                            "wait_sec": round(wait_sec, 1),
                            "queue_depth": queue_depth,
                            "queue_oldest_age_s": queue_oldest_age_s,
                        },
                        log=log,
                    )
                    log.info(
                        "outbox:global_cap_pause wait_sec=%.1f queue_depth=%d oldest_age_s=%s",
                        wait_sec, queue_depth, queue_oldest_age_s,
                    )
                    in_paused_state = True
                    pause_started_mono = now_mono
                    last_pause_log_ts = now_mono
                elif now_mono - last_pause_log_ts > 30:
                    log.info("outbox:global_cap_still_paused wait_sec=%.1f", wait_sec)
                    last_pause_log_ts = now_mono
                await asyncio.sleep(min(wait_sec, 30))
                continue
            elif in_paused_state:
                paused_sec = now_mono - pause_started_mono
                await asyncio.to_thread(
                    insert_telemetry_event, db_cfg,
                    kind="egress_bucket_resumed",
                    detail={"paused_sec": round(paused_sec, 1)},
                    log=log,
                )
                log.info("outbox:global_cap_resumed paused_sec=%.1f", paused_sec)
                in_paused_state = False
            try:
                outbound = f"<{sender}> {content}"
                log.debug("outbox:send id=%s channel=%s len=%d", item["id"], channel, len(content))
                channel_idx = channel_name_to_idx.get(channel)

                if channel_idx is None:
                    # Permanent failure: channel not in config. Will never succeed.
                    log.error("outbox:unknown_channel id=%s channel=%s", item["id"], channel)
                    await asyncio.to_thread(mark_outbox_failed, db_cfg, outbox_ids=[int(item["id"])], log=log)
                    consecutive_send_failures = 0
                else:
                    # Check for echoes before retrying — an echo that
                    # arrived between attempts proves the prior send worked.
                    if item["retry_count"] > 0:
                        row = await asyncio.to_thread(get_outbox_message, db_cfg, outbox_id=int(item["id"]), log=log)
                        if row and row["heard_count"] > 0:
                            await asyncio.to_thread(
                                record_outbox_send,
                                db_cfg, outbox_id=int(item["id"]),
                                sender_ts=row.get("sender_ts") or int(time.time()),
                                log=log,
                            )
                            log.info(
                                "outbox:sent_via_echo id=%s heard_count=%d "
                                "min_path_len=%s best_snr=%s",
                                item["id"], row["heard_count"],
                                row.get("min_path_len"), row.get("best_snr"),
                            )
                            consecutive_send_failures = 0
                            continue

                    # Capture and persist the sender_ts the firmware
                    # will stamp on the outgoing packet. Uses RAW
                    # time.time() (not wall_now) to match what the
                    # firmware's own time(NULL) read produces — see the
                    # docstring on compute_and_persist_sender_ts in
                    # database.py. ±1s tolerance in active_outbox absorbs
                    # second-boundary drift between this read and the
                    # firmware's.
                    sender_ts = await asyncio.to_thread(
                        compute_and_persist_sender_ts,
                        db_cfg, outbox_id=int(item["id"]), log=log,
                    )
                    # Register for echo matching BEFORE the send so
                    # echoes arriving during a no_event_received timeout
                    # are caught. expected_text is the full on-the-wire
                    # form: firmware prepends "Name: " during transmission.
                    my_name = self_name_provider() or ""
                    if my_name:
                        active_outbox.add(
                            outbox_id=int(item["id"]),
                            channel=channel,
                            expected_text=f"{my_name}: {outbound}",
                            sender_ts=sender_ts,
                        )

                    result = await mc.commands.send_chan_msg(channel_idx, outbound)
                    if result.type == EventType.ERROR:
                        payload = result.payload
                        is_no_event = (
                            isinstance(payload, dict)
                            and payload.get("reason") == "no_event_received"
                        )

                        if is_no_event:
                            echo_wait = cfg.limits.outbox_echo_wait_sec
                            log.info(
                                "outbox:no_event_received id=%s waiting_for_echo_sec=%d",
                                item["id"], echo_wait,
                            )
                            await asyncio.sleep(echo_wait)

                            # Re-read outbox row to check if echoes arrived during wait
                            row = await asyncio.to_thread(get_outbox_message, db_cfg, outbox_id=int(item["id"]), log=log)
                            if row and row["heard_count"] > 0:
                                # Echo confirmed — treat as successful send
                                await asyncio.to_thread(
                                    record_outbox_send,
                                    db_cfg, outbox_id=int(item["id"]),
                                    sender_ts=sender_ts, log=log,
                                )
                                log.info(
                                    "outbox:sent_via_echo id=%s heard_count=%d "
                                    "first_heard_ts=%s last_heard_ts=%s "
                                    "min_path_len=%s best_snr=%s",
                                    item["id"], row["heard_count"],
                                    row.get("first_heard_ts"), row.get("last_heard_ts"),
                                    row.get("min_path_len"), row.get("best_snr"),
                                )
                                consecutive_send_failures = 0
                                continue  # finally still advances backoff — intentional

                            # No echo — fall through to normal retry
                            log.info(
                                "outbox:echo_not_heard id=%s proceeding_with_retry",
                                item["id"],
                            )

                        # Normal error path (non-no_event, or no_event without echo)
                        new_count = await asyncio.to_thread(increment_outbox_retry, db_cfg, outbox_id=int(item["id"]), log=log)
                        log.error(
                            "outbox:send_failed id=%s channel=%s attempt=%d/%d err=%s",
                            item["id"], channel, new_count, max_retries, result.payload,
                        )
                        if new_count >= max_retries:
                            await asyncio.to_thread(mark_outbox_failed, db_cfg, outbox_ids=[int(item["id"])], log=log)
                            log.warning(
                                "outbox:giving_up id=%s channel=%s after %d attempts",
                                item["id"], channel, new_count,
                            )
                        consecutive_send_failures += 1
                        if consecutive_send_failures >= cfg.recovery.outbox_consecutive_threshold:
                            controller.request_recovery(
                                source="outbox",
                                reason=f"{consecutive_send_failures} consecutive send failures",
                            )
                            consecutive_send_failures = 0
                    else:
                        await asyncio.to_thread(
                            record_outbox_send,
                            db_cfg,
                            outbox_id=int(item["id"]),
                            sender_ts=sender_ts,
                            log=log,
                        )
                        consecutive_send_failures = 0
            except Exception as e:
                log.error("outbox:send_exception id=%s err=%s", item["id"], e, exc_info=True)
                new_count = await asyncio.to_thread(increment_outbox_retry, db_cfg, outbox_id=int(item["id"]), log=log)
                if new_count >= max_retries:
                    await asyncio.to_thread(mark_outbox_failed, db_cfg, outbox_ids=[int(item["id"])], log=log)
                consecutive_send_failures += 1
                if consecutive_send_failures >= cfg.recovery.outbox_consecutive_threshold:
                    controller.request_recovery(
                        source="outbox",
                        reason=f"{consecutive_send_failures} consecutive send failures",
                    )
                    consecutive_send_failures = 0
            finally:
                # Advance backoff after each attempt to avoid flooding on large backlogs.
                # CIV-99: monotonic — must not be affected by wall-clock jumps.
                last_send_time = time.monotonic()
                backoff_level = min(backoff_level + 1, 3)
        except Exception as e:
            log.error("outbox:error %s", e, exc_info=True)


# An advert costs ~0.5s of LoRa airtime on the PNW config (SF7,
# BW 62.5 kHz, CR=5). The shared mesh budget across the contiguous
# flood domain is ~1-2 kbps. Without throttling, a flapping
# reconnect cycle would push adverts as a measurable percentage of
# total mesh airtime. set_name is firmware-side cheap and stays
# unthrottled; send_advert is gated.
_ADVERT_COOLDOWN_SEC = 6 * 3600


async def _announce_identity(mesh_client, cfg, log, advert_state, EventType):
    """Set the firmware name on every connect; advertise only when
    the cooldown has elapsed, the callsign changed, or this is the
    first connect of the process. advert_state is a mutable dict
    shared across reconnects: {"last_callsign", "last_ts"}.
    """
    callsign = cfg.node.callsign
    try:
        result = await mesh_client.commands.set_name(callsign)
        if result.type == EventType.ERROR:
            log.warning(
                "mesh:set_name_failed callsign=%s err=%s",
                callsign, result.payload,
            )
            return
        log.info("mesh:set_name_ok callsign=%s", callsign)
        # Refresh self_info so _self_name (used by the heard-count
        # echo-match handler) sees the new firmware name. Without this,
        # self_info["name"] keeps the create_serial-time value
        # (typically the pubkey hex prefix on a fresh device) and the
        # echo-match expected_text won't match incoming echoes. Same
        # refresh pattern as the post-set_radio block above.
        await mesh_client.commands.send_appstart()
    except Exception as e:
        log.warning("mesh:set_name_exception err=%s", e, exc_info=True)
        return

    # CIV-99: monotonic — advert cooldown is elapsed time; an admin or
    # NTP step must not reset or extend it. advert_state's "last_ts" is
    # now monotonic across reconnects (it's an in-process dict, so the
    # frame change at OS reboot — which clears in-process state anyway —
    # is moot).
    now = time.monotonic()
    last_callsign = advert_state.get("last_callsign")
    last_ts = advert_state.get("last_ts")
    if last_callsign is None:
        reason = "first_connect"
    elif last_callsign != callsign:
        reason = "callsign_changed"
    elif last_ts is None or (now - last_ts) >= _ADVERT_COOLDOWN_SEC:
        reason = "cooldown_elapsed"
    else:
        log.info(
            "mesh:advert_skipped callsign=%s age_sec=%.0f cooldown_sec=%d",
            callsign, now - last_ts, _ADVERT_COOLDOWN_SEC,
        )
        return

    try:
        # flood=False is explicit so a future maintainer (or upstream
        # default change) doesn't silently break the airtime/throttle
        # cost model the comment above this helper depends on.
        advert = await mesh_client.commands.send_advert(flood=False)
        if advert.type == EventType.ERROR:
            # Don't update advert_state — let the next reconnect retry.
            log.warning(
                "mesh:send_advert_failed reason=%s err=%s",
                reason, advert.payload,
            )
            return
        log.info("mesh:advert_sent callsign=%s reason=%s", callsign, reason)
        advert_state["last_callsign"] = callsign
        advert_state["last_ts"] = now
    except Exception as e:
        log.warning("mesh:send_advert_exception err=%s", e, exc_info=True)


async def _setup_mesh_client(
    mesh_client,
    cfg,
    db_cfg: DBConfig,
    log,
    active_outbox: ActiveOutboxIndex,
    known_channel_names: set[str],
    EventType,
    advert_state: dict,
):
    """Configure a connected MeshCore client: verify/set radio params,
    set up channels, enable auto-fetch and decryption, subscribe handlers.

    Does NOT include MeshCore.create_serial — the caller owns connection.
    Raises on any setup failure.
    """
    # Verify radio params from self_info (populated by
    # send_appstart inside create_serial).  If they already
    # match cfg, skip set_radio entirely — on some firmware
    # (e.g. v1.11.0) set_radio breaks the session.
    radio_info = getattr(mesh_client, "self_info", {}) or {}
    expected_radio = {
        "radio_freq": cfg.radio.freq_mhz,
        "radio_bw": cfg.radio.bw_khz,
        "radio_sf": cfg.radio.sf,
        "radio_cr": cfg.radio.cr,
    }

    def _radio_matches(info, expected):
        for key, want in expected.items():
            got = info.get(key)
            if got is None or float(got) != float(want):
                return False
        return True

    if _radio_matches(radio_info, expected_radio):
        log.info(
            "mesh:radio_params_ok freq=%s bw=%s sf=%s cr=%s",
            radio_info.get("radio_freq"),
            radio_info.get("radio_bw"),
            radio_info.get("radio_sf"),
            radio_info.get("radio_cr"),
        )
    else:
        log.info(
            "mesh:radio_mismatch, attempting set_radio "
            "freq=%s bw=%s sf=%s cr=%s",
            cfg.radio.freq_mhz, cfg.radio.bw_khz,
            cfg.radio.sf, cfg.radio.cr,
        )
        await mesh_client.commands.set_radio(
            cfg.radio.freq_mhz,
            cfg.radio.bw_khz,
            cfg.radio.sf,
            cfg.radio.cr,
        )
        # Re-read self_info — the library return value is not
        # authoritative; self_info after appstart is.
        await mesh_client.commands.send_appstart()
        radio_info = getattr(mesh_client, "self_info", {}) or {}
        if _radio_matches(radio_info, expected_radio):
            log.info(
                "mesh:radio_reconfigured freq=%s bw=%s sf=%s cr=%s",
                radio_info.get("radio_freq"),
                radio_info.get("radio_bw"),
                radio_info.get("radio_sf"),
                radio_info.get("radio_cr"),
            )
        else:
            for key, want in expected_radio.items():
                got = radio_info.get(key)
                if got is None or float(got) != float(want):
                    log.error(
                        "mesh:radio_verify_failed %s expected=%s got=%s",
                        key, want, got,
                    )
            raise RuntimeError(
                "radio params do not match config after set_radio"
            )
    channel_info = await mesh_client.commands.get_channel(0)
    log.info("mesh:get_channel idx=0 event=%s payload=%s", channel_info.type, channel_info.payload)
    stats_core = await mesh_client.commands.get_stats_core()
    log.info("mesh:get_stats_core event=%s payload=%s", stats_core.type, stats_core.payload)

    channels_ok = 0
    for idx, name in enumerate(cfg.channels.names):
        secret_hex = hashlib.sha256(name.encode()).hexdigest()[:32]
        secret_bytes = bytes.fromhex(secret_hex)
        result = await mesh_client.commands.set_channel(idx, name, secret_bytes)
        if result.type == EventType.ERROR:
            log.error("mesh:set_channel_failed channel=%s err=%s", name, result.payload)
        else:
            log.info("mesh:channel_set idx=%d name=%s", idx, name)
            channels_ok += 1

    if channels_ok == 0:
        raise RuntimeError(
            f"radio setup failed: 0/{len(cfg.channels.names)} channels configured"
        )

    await mesh_client.start_auto_message_fetching()
    log.info("mesh:auto_fetch_started")
    upsert_status(db_cfg, process="mesh_bot", radio_connected=True, log=log)

    # Enable channel-message decryption in meshcore_py's
    # parser. Required for RX_LOG_DATA events to carry
    # decrypted `message` / `sender_timestamp` / `chan_name`
    # — the inputs the heard-count handler needs. Verified
    # in diagnostics/radio/FINDINGS.md.
    try:
        mesh_client.set_decrypt_channel_logs(True)
        log.info("mesh:decrypt_channel_logs_enabled")
    except Exception as e:
        log.error("mesh:decrypt_channel_logs_failed err=%s", e, exc_info=True)

    await _announce_identity(mesh_client, cfg, log, advert_state, EventType)

    # RX_LOG_DATA handler — increments heard_count on the
    # outbox row whose echo we just observed. Filter on
    # `message != None && chan_name in our channels` per
    # FINDINGS: chan_hash is 1 byte and collisions happen,
    # but the library leaves message=None when the HMAC
    # validation fails, so message != None is the
    # authoritative "this is a real channel message we can
    # see plaintext for" signal.
    def _on_rx_log_data(event):
        try:
            p = event.payload
            msg = p.get("message")
            if msg is None:
                return
            chan_name = p.get("chan_name")
            if chan_name not in known_channel_names:
                return
            sender_ts = p.get("sender_timestamp")
            if not isinstance(sender_ts, int):
                return
            outbox_id = active_outbox.match(
                channel=chan_name,
                message_text=msg,
                sender_ts=sender_ts,
            )
            if outbox_id is None:
                return  # not one of our active sends
            _executor_db(
                increment_heard,
                db_cfg,
                outbox_id=outbox_id,
                path_len=p.get("path_len"),
                snr=p.get("snr"),
                log=log,
            )
            log.debug(
                "outbox:heard outbox_id=%d path_len=%s snr=%s",
                outbox_id, p.get("path_len"), p.get("snr"),
            )
        except Exception as e:
            log.error("mesh:rx_log_data_error %s", e, exc_info=True)

    mesh_client.subscribe(EventType.RX_LOG_DATA, _on_rx_log_data)
    log.info("mesh:rx_log_data_subscribed")

    # Stats: record every heard packet for the /api/stats endpoint.
    # Separate handler from echo-matching — fires on ALL packets
    # (adverts, acks, routing, undecryptable), not just decoded
    # channel messages. DB write offloaded to executor to avoid
    # blocking the event loop during SD-card GC stalls.
    def _on_rx_log_stats(event):
        try:
            p = event.payload
            payload_type = p.get("payload_type")
            route_type = p.get("route_type")
            path_len = p.get("path_len")
            if payload_type is None or route_type is None or path_len is None:
                log.debug("stats:skip_malformed_packet")
                return
            path_hex = p.get("path") or ""
            last_path_byte = None
            if path_len >= 1 and len(path_hex) >= 2:
                last_path_byte = int(path_hex[-2:], 16)
            snr = p.get("snr")
            rssi = p.get("rssi")
            _executor_db(
                insert_heard_packet,
                db_cfg,
                payload_type=payload_type, route_type=route_type,
                path_len=path_len, last_path_byte=last_path_byte,
                snr=snr, rssi=rssi, log=log,
            )
        except Exception as e:
            log.error("stats:rx_log_error %s", e, exc_info=True)

    mesh_client.subscribe(EventType.RX_LOG_DATA, _on_rx_log_stats)
    log.info("stats:rx_log_subscribed")

    # Handlers
    def _on_channel_message(event):
        try:
            msg = event.payload
            channel_idx = msg.get("channel_idx")
            if isinstance(channel_idx, int) and 0 <= channel_idx < len(cfg.channels.names):
                channel = cfg.channels.names[channel_idx]
            else:
                channel = f"#channel-{channel_idx}"
            sender = msg.get("sender", "") or msg.get("pubkey_prefix", "")
            content = msg.get("text") or msg.get("content", "")
            if not sender and isinstance(content, str) and ": " in content:
                # Fallback for payloads that embed "Name: message" without sender metadata.
                name, rest = content.split(": ", 1)
                if name:
                    sender = name
                    content = rest
            log.debug("mesh:rx channel=%s sender=%s len=%d", channel, sender, len(content))
            _executor_db(insert_message_wall, db_cfg, channel=channel, sender=sender, content=content, source="mesh", log=log)
        except Exception as e:
            log.error("mesh:rx_error %s", e, exc_info=True)

    mesh_client.subscribe(EventType.CHANNEL_MSG_RECV, _on_channel_message)

    log.info(
        "mesh:setup_complete channels_ok=%d/%d",
        channels_ok,
        len(cfg.channels.names),
    )


async def main_async(config_path: str, *, meshcore_debug: bool = False):
    cfg = load_config(config_path)
    log, sec = setup_logging("mesh_bot", cfg.logging)
    log.info("Civic Mesh mesh_bot starting")

    # CIV-99: loud CRITICAL if running from /usr/local/civicmesh/ with
    # `[clock] require_timesync_masked = false` — the dev opt-out
    # shouldn't reach a production disaster node.
    if not cfg.clock.require_timesync_masked:
        clock.warn_if_prod_opt_out_of_timesync_mask(log, caller_file=__file__)

    db_cfg = DBConfig(path=cfg.db_path)
    init_db(db_cfg, log=log)
    reconcile_message_status(db_cfg, log=log)

    global EventType
    global MeshCore

    # Lazy import so web_server can run without meshcore installed.
    try:
        from meshcore import EventType, MeshCore  # type: ignore
    except Exception:
        EventType = None  # type: ignore
        MeshCore = None  # type: ignore

    channel_name_to_idx = {name: idx for idx, name in enumerate(cfg.channels.names)}
    active_outbox = ActiveOutboxIndex()
    egress_bucket = _GlobalEgressBucket(cfg.limits.global_egress_per_hour)
    known_channel_names = set(cfg.channels.names)

    controller = RecoveryController(cfg.recovery, db_cfg, cfg.radio.serial_port, log)

    # Shared across reconnects within this process. On service
    # restart this is fresh, so the next connect adverts once
    # (first_connect). See _announce_identity.
    advert_state: dict[str, Any] = {"last_callsign": None, "last_ts": None}

    async def _connect_loop():
        backoff = 1
        while True:
            try:
                if MeshCore is None or EventType is None:
                    raise RuntimeError("meshcore not available")

                mesh_client = await MeshCore.create_serial(
                    cfg.radio.serial_port,
                    DEFAULT_BAUDRATE,
                    debug=meshcore_debug,
                )
                if mesh_client is None or getattr(mesh_client, "commands", None) is None:
                    raise RuntimeError("create_serial returned unusable client (appstart likely failed)")
                log.info("mesh:connected port=%s baudrate=%d", cfg.radio.serial_port, DEFAULT_BAUDRATE)

                await _setup_mesh_client(
                    mesh_client, cfg, db_cfg, log,
                    active_outbox, known_channel_names, EventType,
                    advert_state,
                )

                controller.set_client(mesh_client)
                controller.mark_healthy()
                backoff = 1
                return
            except Exception as e:
                try:
                    upsert_status(db_cfg, process="mesh_bot", radio_connected=False, log=log)
                except Exception as se:
                    log.error("heartbeat:down_failed err=%s", se, exc_info=True)
                log.error("mesh:connect_failed err=%s backoff=%ds", e, backoff, exc_info=True)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    await _connect_loop()

    async def _setup_fn(mc):
        await _setup_mesh_client(
            mc, cfg, db_cfg, log,
            active_outbox, known_channel_names, EventType,
            advert_state,
        )

    def _self_name():
        client = controller.get_client()
        info = getattr(client, "self_info", None) or {}
        return info.get("name") if isinstance(info, dict) else None

    await asyncio.gather(
        _outbox_task(
            cfg, db_cfg, log, controller, channel_name_to_idx,
            active_outbox, _self_name, egress_bucket,
        ),
        _retention_task(cfg, db_cfg, log),
        _heartbeat_task(cfg, db_cfg, log, controller),
        _clock_task(cfg, db_cfg, log),
        telemetry.telemetry_loop(db_cfg, log),
        liveness_task(controller, log),
        recovery_task(controller, _setup_fn, log),
    )


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--meshcore-debug", action="store_true", help="Enable meshcore library debug logging")
    args = ap.parse_args()
    asyncio.run(main_async(args.config, meshcore_debug=args.meshcore_debug))


def main() -> None:
    """
    Console-script entrypoint (sync wrapper).
    """
    # CIV-99: hard-fail on non-Linux before anything else (clock-consensus
    # depends on /proc/sys/kernel/random/boot_id; _clock_task would
    # explode at startup otherwise).
    clock.ensure_linux_platform()

    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--meshcore-debug", action="store_true", help="Enable meshcore library debug logging")
    args = ap.parse_args()
    asyncio.run(main_async(args.config, meshcore_debug=args.meshcore_debug))
