"""
Silent-hang detection and software recovery for the MeshCore radio.

Two independent triggers (liveness ping timeouts, outbox send failures)
feed a single RecoveryController that runs a ladder of reset actions
until the radio is healthy again.  If the ladder fails, the controller
enters NEEDS_HUMAN and retries on exponential backoff.  The process
never exits voluntarily.
"""

from __future__ import annotations

import asyncio
import enum
import functools
import logging
import time
from collections import deque
from dataclasses import dataclass
from typing import TYPE_CHECKING, Awaitable, Callable, Optional

import serial  # pyserial — transitive dep via meshcore

from config import RecoveryConfig
from database import DBConfig, insert_telemetry_event, upsert_status

if TYPE_CHECKING:
    pass  # MeshCore is lazily imported at runtime


# ---------------------------------------------------------------------------
# State enum
# ---------------------------------------------------------------------------

class RecoveryState(str, enum.Enum):
    DISCONNECTED = "disconnected"
    HEALTHY = "healthy"
    RECOVERING = "recovering"
    NEEDS_HUMAN = "needs_human"


# ---------------------------------------------------------------------------
# Rung / RungContext — extensible ladder of reset actions
# ---------------------------------------------------------------------------

@dataclass
class RungContext:
    serial_port: str
    config: RecoveryConfig
    log: logging.Logger


@dataclass(frozen=True)
class Rung:
    name: str
    action: Callable[[RungContext], Awaitable[None]]


# ---------------------------------------------------------------------------
# RTS pulse action
# ---------------------------------------------------------------------------

async def _rts_pulse_action(ctx: RungContext) -> None:
    """Reset the ESP32 via the CP2102 serial bridge's RTS line.

    Runs in an executor because pyserial is synchronous and the 100ms
    sleep would block all other asyncio tasks.
    """
    def _pulse():
        with serial.Serial(ctx.serial_port, 115200, timeout=0) as s:
            s.dtr = False
            s.rts = True
            time.sleep(ctx.config.rts_pulse_width_sec)
            s.rts = False

    await asyncio.get_running_loop().run_in_executor(None, _pulse)


DEFAULT_LADDER: list[Rung] = [
    Rung(name="rts_pulse", action=_rts_pulse_action),
]


# ---------------------------------------------------------------------------
# Helper: connect once (no retry)
# ---------------------------------------------------------------------------

async def _connect_once(serial_port: str, log: logging.Logger):
    """Create a fresh MeshCore serial connection (no setup/subscribe)."""
    from meshcore import MeshCore
    mc = await MeshCore.create_serial(serial_port, 115200, debug=False)
    if mc is None or getattr(mc, "commands", None) is None:
        raise RuntimeError("create_serial returned unusable client (appstart likely failed)")
    return mc


# ---------------------------------------------------------------------------
# Helper: disconnect with force-close fallback
# ---------------------------------------------------------------------------

async def _disconnect_client(client, log: logging.Logger) -> None:
    """Disconnect mesh_client; force-close the transport on timeout."""
    if client is None:
        return
    try:
        await asyncio.wait_for(client.disconnect(), timeout=3.0)
    except (asyncio.TimeoutError, Exception):
        try:
            if hasattr(client, "connection_manager"):
                cm = client.connection_manager
                if hasattr(cm, "connection") and hasattr(cm.connection, "transport"):
                    t = cm.connection.transport
                    if t is not None:
                        t.close()
        except Exception:
            pass
        log.warning("recovery:force_closed_transport")


# ---------------------------------------------------------------------------
# Helper: backoff calculation
# ---------------------------------------------------------------------------

def _backoff(attempt: int, cfg: RecoveryConfig) -> float:
    return min(cfg.backoff_base_sec * (2 ** (attempt - 1)), cfg.backoff_cap_sec)


# ---------------------------------------------------------------------------
# RecoveryController
# ---------------------------------------------------------------------------

class RecoveryController:
    """Single owner of the mesh_client reference.

    All other tasks access the client through get_client().  The
    recovery_task is the only writer of state transitions.
    """

    def __init__(
        self,
        cfg: RecoveryConfig,
        db_cfg: DBConfig,
        serial_port: str,
        log: logging.Logger,
    ) -> None:
        self._cfg = cfg
        self._db_cfg = db_cfg
        self._serial_port = serial_port
        self._log = log

        self._client = None
        self._state = RecoveryState.DISCONNECTED
        self._request_event = asyncio.Event()
        self._flapping_deque: deque[float] = deque()

    # --- Read surface ---

    def get_client(self):
        return self._client

    def get_state(self) -> RecoveryState:
        return self._state

    def outbox_should_pause(self) -> bool:
        return self._state in (RecoveryState.RECOVERING, RecoveryState.NEEDS_HUMAN)

    def is_healthy(self) -> bool:
        return self._state == RecoveryState.HEALTHY

    # --- Write surface (trigger sites) ---

    def request_recovery(self, *, source: str, reason: str) -> None:
        """Idempotent.  Sets internal event; recovery_task picks it up."""
        if not self._request_event.is_set():
            self._log.warning(
                "recovery:requested source=%s reason=%s", source, reason,
            )
            self._emit_telemetry("recovery_requested", {
                "source": source, "reason": reason,
            })
        self._request_event.set()

    # --- Write surface (initial connect + recovery_task) ---

    def set_client(self, c) -> None:
        """Store a new MeshCore reference.  No state change."""
        self._client = c

    def mark_healthy(self) -> None:
        """Transition to HEALTHY; write status table + telemetry."""
        old = self._state
        self._state = RecoveryState.HEALTHY
        self._emit_telemetry("recovery_state_change", {
            "from": old.value, "to": RecoveryState.HEALTHY.value,
        })
        self._write_status(radio_connected=True)

    # --- Internal helpers ---

    def _transition(self, new_state: RecoveryState, *, radio_connected: bool) -> None:
        old = self._state
        if old == new_state:
            return
        self._state = new_state
        self._emit_telemetry("recovery_state_change", {
            "from": old.value, "to": new_state.value,
        })
        self._write_status(radio_connected=radio_connected)

    def _write_status(self, *, radio_connected: bool) -> None:
        try:
            upsert_status(
                self._db_cfg,
                process="mesh_bot",
                radio_connected=radio_connected,
                state=self._state.value,
                log=self._log,
            )
        except Exception as e:
            self._log.error("recovery:status_write_failed err=%s", e, exc_info=True)

    def _emit_telemetry(self, kind: str, detail: dict) -> None:
        try:
            loop = asyncio.get_running_loop()
            loop.run_in_executor(
                None,
                functools.partial(
                    insert_telemetry_event,
                    self._db_cfg,
                    ts=int(time.time()),
                    kind=kind,
                    detail=detail,
                    log=self._log,
                ),
            )
        except RuntimeError:
            # No running loop (e.g. during shutdown).
            pass


# ---------------------------------------------------------------------------
# liveness_task
# ---------------------------------------------------------------------------

async def liveness_task(
    controller: RecoveryController,
    log: logging.Logger,
) -> None:
    """Poll get_stats_core to detect radio silence.

    On liveness_consecutive_threshold consecutive timeouts, fires
    controller.request_recovery and resets the counter.
    """
    cfg = controller._cfg
    consecutive_misses = 0

    while True:
        await asyncio.sleep(cfg.liveness_interval_sec)

        if controller.outbox_should_pause():
            continue

        mc = controller.get_client()
        if mc is None:
            continue

        try:
            from meshcore import EventType
            result = await asyncio.wait_for(
                mc.commands.get_stats_core(),
                timeout=cfg.liveness_timeout_sec,
            )
            if result.type == EventType.ERROR:
                raise RuntimeError(f"get_stats_core returned ERROR: {result.payload}")
            consecutive_misses = 0
        except Exception:
            consecutive_misses += 1
            log.warning("liveness:miss consecutive=%d", consecutive_misses)
            if consecutive_misses >= cfg.liveness_consecutive_threshold:
                controller.request_recovery(
                    source="liveness",
                    reason=f"{consecutive_misses} consecutive liveness timeouts",
                )
                consecutive_misses = 0


# ---------------------------------------------------------------------------
# recovery_task
# ---------------------------------------------------------------------------

async def recovery_task(
    controller: RecoveryController,
    setup_fn: Callable,
    log: logging.Logger,
    ladder: list[Rung] = DEFAULT_LADDER,
) -> None:
    """Main recovery loop.  Waits for request_recovery signals, then
    runs the ladder of reset actions until the radio is healthy or all
    rungs fail (enters NEEDS_HUMAN with exponential backoff).
    """
    cfg = controller._cfg
    ctx = RungContext(
        serial_port=controller._serial_port,
        config=cfg,
        log=log,
    )

    while True:
        await controller._request_event.wait()
        controller._request_event.clear()

        attempt = 1
        while True:  # ladder retry loop
            # Pre-recovery health check: if we're re-entering the ladder
            # after a backoff (attempt > 1) and a client exists, probe it
            # before resetting.  The radio may have recovered on its own
            # (e.g. after a flapping-cap backoff period).
            if attempt > 1 and controller.get_client() is not None:
                try:
                    from meshcore import EventType as _ET
                    result = await asyncio.wait_for(
                        controller.get_client().commands.get_stats_core(),
                        timeout=cfg.verify_timeout_sec,
                    )
                    if result.type != _ET.ERROR:
                        controller.mark_healthy()
                        controller._emit_telemetry(
                            "recovery_skipped_healthy", {"attempt": attempt},
                        )
                        log.info(
                            "recovery:skipped_healthy attempt=%d", attempt,
                        )
                        break  # exit ladder retry loop
                except Exception as e:
                    log.debug("recovery:health_probe_failed attempt=%d err=%s", attempt, e)

            controller._transition(
                RecoveryState.RECOVERING, radio_connected=False,
            )

            recovered = False
            for rung in ladder:
                controller._emit_telemetry("recovery_rung_attempted", {
                    "rung": rung.name, "attempt": attempt,
                })

                # 1. Disconnect current client
                old = controller.get_client()
                if old is not None:
                    await _disconnect_client(old, log)
                    controller._client = None

                # 2. Run the reset action
                try:
                    await rung.action(ctx)
                except Exception as e:
                    log.error(
                        "recovery:rung_action_failed rung=%s err=%s",
                        rung.name, e, exc_info=True,
                    )
                    controller._emit_telemetry("recovery_rung_failed", {
                        "rung": rung.name, "attempt": attempt,
                        "stage": "action", "err": repr(e),
                    })
                    continue

                # 3. Settle
                await asyncio.sleep(cfg.post_rts_settle_sec)

                # 4. Reconnect + verify
                # setup_fn runs _setup_mesh_client which calls
                # get_stats_core internally — if that succeeds, the
                # radio is verified.  A separate verify step after
                # setup would race with the auto-fetch event pump
                # (which consumes the response), causing spurious
                # TimeoutErrors.
                try:
                    new_client = await asyncio.wait_for(
                        _connect_once(controller._serial_port, log),
                        timeout=cfg.verify_timeout_sec,
                    )
                    await setup_fn(new_client)
                except Exception as e:
                    log.error(
                        "recovery:reconnect_failed rung=%s err=%s",
                        rung.name, e, exc_info=True,
                    )
                    controller._emit_telemetry("recovery_rung_failed", {
                        "rung": rung.name, "attempt": attempt,
                        "stage": "reconnect", "err": repr(e),
                    })
                    try:
                        await new_client.disconnect()
                    except Exception as e:
                        log.debug("recovery:disconnect_cleanup_failed rung=%s err=%s", rung.name, e)
                    continue

                # 5. Success
                controller.set_client(new_client)

                # Check flapping cap
                now = time.monotonic()
                controller._flapping_deque.append(now)
                while (controller._flapping_deque
                       and controller._flapping_deque[0]
                       < now - cfg.flapping_window_sec):
                    controller._flapping_deque.popleft()

                if len(controller._flapping_deque) > cfg.flapping_max_recoveries:
                    log.warning(
                        "recovery:flapping_cap_exceeded count=%d window=%ds",
                        len(controller._flapping_deque),
                        cfg.flapping_window_sec,
                    )
                    controller._emit_telemetry("recovery_flapping_cap_exceeded", {
                        "window_sec": cfg.flapping_window_sec,
                        "count": len(controller._flapping_deque),
                    })
                    controller._transition(
                        RecoveryState.NEEDS_HUMAN, radio_connected=False,
                    )
                    controller._emit_telemetry("recovery_needs_human", {
                        "attempt": attempt,
                        "backoff_sec": _backoff(attempt, cfg),
                    })
                    await asyncio.sleep(_backoff(attempt, cfg))
                    attempt += 1
                    break  # exit rung loop, continue ladder retry loop

                controller.mark_healthy()
                controller._emit_telemetry("recovery_succeeded", {
                    "rung": rung.name, "attempt": attempt,
                })
                log.info(
                    "recovery:succeeded rung=%s attempt=%d",
                    rung.name, attempt,
                )
                recovered = True
                break  # exit rung loop

            if recovered:
                break  # exit ladder retry loop

            # All rungs failed this attempt
            controller._transition(
                RecoveryState.NEEDS_HUMAN, radio_connected=False,
            )
            backoff_sec = _backoff(attempt, cfg)
            controller._emit_telemetry("recovery_needs_human", {
                "attempt": attempt, "backoff_sec": backoff_sec,
            })
            log.warning(
                "recovery:needs_human attempt=%d backoff=%.0fs",
                attempt, backoff_sec,
            )
            await asyncio.sleep(backoff_sec)
            attempt += 1
