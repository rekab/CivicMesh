# Radio hang detection and recovery

Implementation reference for the silent-hang detection and software
recovery system in `recovery.py` (CIV-41).  For hardware background
— which chips reset under which conditions, what the recovery ladder
can and cannot reach — see `docs/heltec-recovery.md`.

## How it works

Two independent triggers detect radio problems:

1. **Liveness pings** — a background task polls `get_stats_core` every
   30 seconds.  Three consecutive timeouts (≈90 seconds of silence)
   trigger recovery.
2. **Outbox send failures** — the outbox task tracks consecutive
   `send_chan_msg` errors.  Three consecutive failures trigger recovery.

Both triggers call `controller.request_recovery()`, which is
idempotent — multiple triggers coalesce into a single recovery cycle.

The **RecoveryController** owns the `mesh_client` reference.  All tasks
access the radio through `controller.get_client()` rather than holding
their own reference, so the controller can swap in a new client after
recovery without restarting the process.

## State machine

```
DISCONNECTED ──(initial connect)──► HEALTHY
                                       │
                        (trigger fires) │
                                       ▼
                                   RECOVERING ──(rung succeeds)──► HEALTHY
                                       │
                          (all rungs fail)
                                       ▼
                                  NEEDS_HUMAN ──(backoff, retry)──► RECOVERING
                                       │
                     (health check finds radio alive)
                                       ▼
                                    HEALTHY
```

**DISCONNECTED** — only at startup, before the first successful
connection.

**HEALTHY** — radio is responding.  Outbox task sends normally,
heartbeat reports `radio_connected=True`.

**RECOVERING** — ladder is running.  Outbox and liveness tasks pause
(no point sending to or probing a radio being reset).

**NEEDS_HUMAN** — all rungs failed or the flapping cap was exceeded.
The controller sleeps with exponential backoff (60 s → 2 min → 4 min →
… → 1 hour cap), then re-enters the ladder.  The process never exits.

## Recovery ladder

The ladder is a list of `Rung` objects.  Each rung has a name and an
async action.  The `recovery_task` iterates the rungs in order; the
first one to succeed (disconnect → reset → reconnect → verify) wins.

Current ladder (single rung):

| Rung | Action | What it resets |
|------|--------|----------------|
| `rts_pulse` | Pulse RTS on the serial port (100 ms) | ESP32 only; SX1262 may recover via `radio_init()` |

The ladder is a parameter to `recovery_task` (`ladder=DEFAULT_LADDER`),
so additional rungs can be appended without restructuring.  Candidates
for future rungs: GPIO EN toggle (CIV-30), VEXT power cycle (CIV-44),
pyusb logical reset, systemd watchdog notify.

### Recovery procedure per rung

1. Disconnect the current client (3 s timeout, force-close on hang).
2. Run the rung's reset action (RTS pulse runs in a thread executor
   since pyserial is synchronous).
3. Sleep `post_rts_settle_sec` (default 5 s) for the ESP32 to boot.
4. Reconnect: `MeshCore.create_serial` + full radio setup (params,
   channels, subscriptions).
5. Verify: one `get_stats_core` call with `verify_timeout_sec` timeout.
6. On success → `mark_healthy()`.  On failure → try next rung.

### Pre-recovery health check

When re-entering the ladder after a NEEDS_HUMAN backoff (attempt > 1),
the controller first probes the existing client with `get_stats_core`.
If the radio responds OK, recovery is skipped entirely — avoids
gratuitous resets when the radio has settled on its own during the
backoff period.  This check does NOT run on attempt 1 (fresh evidence
of failure).

### Flapping protection

A `deque` of successful-recovery timestamps tracks how often recovery
fires.  If more than `flapping_max_recoveries` (default 6) succeed
within `flapping_window_sec` (default 3600 s / 1 hour), the controller
enters NEEDS_HUMAN instead of HEALTHY — the radio technically works but
something is causing it to fail repeatedly.  Exponential backoff gives
it time to stabilize.

## Configuration

All fields are optional.  Defaults apply if the `[recovery]` section
is absent from `config.toml`.

```toml
[recovery]
liveness_interval_sec = 30.0    # seconds between liveness pings
liveness_timeout_sec = 5.0      # timeout per ping
liveness_consecutive_threshold = 3  # pings before triggering recovery
outbox_consecutive_threshold = 3    # send failures before triggering
verify_timeout_sec = 5.0        # timeout for post-reset verification
post_rts_settle_sec = 5.0       # wait after RTS pulse before reconnecting
rts_pulse_width_sec = 0.1       # how long to hold RTS high
flapping_window_sec = 3600      # window for flapping detection
flapping_max_recoveries = 6     # max recoveries in window before NEEDS_HUMAN
backoff_base_sec = 60.0         # initial backoff for NEEDS_HUMAN
backoff_cap_sec = 3600.0        # maximum backoff (1 hour)
```

## Observability

### Status table

The `status` table has a `state` column (`TEXT`, nullable) that the
heartbeat task updates every 10 seconds:

| state | radio_connected | meaning |
|-------|----------------|---------|
| `disconnected` | false | startup, not yet connected |
| `healthy` | true | radio responding normally |
| `recovering` | false | reset ladder in progress |
| `needs_human` | false | all resets failed, backing off |

The web server reads this via `get_status()`.  CIV-42 will surface
the state on the captive portal.

### Telemetry events

All events are written to `telemetry_events` via executor (non-blocking).

| kind | detail fields | when |
|------|---------------|------|
| `recovery_requested` | `source`, `reason` | trigger fires |
| `recovery_state_change` | `from`, `to` | any state transition |
| `recovery_rung_attempted` | `rung`, `attempt` | before each rung |
| `recovery_rung_failed` | `rung`, `attempt`, `stage`, `err` | rung step fails |
| `recovery_succeeded` | `rung`, `attempt` | radio recovered |
| `recovery_skipped_healthy` | `attempt` | health check found radio alive |
| `recovery_needs_human` | `attempt`, `backoff_sec` | entering backoff |
| `recovery_flapping_cap_exceeded` | `window_sec`, `count` | too many recoveries |

### Log messages

Key log lines to grep for:

- `recovery:requested` — trigger fired (WARNING)
- `recovery:succeeded` / `recovery:skipped_healthy` — back to healthy (INFO)
- `recovery:needs_human` — all rungs failed (WARNING)
- `recovery:rung_action_failed` — individual rung failure (ERROR)
- `liveness:miss` — single liveness timeout (WARNING)

## Architecture

```
mesh_bot.py
  main_async()
    ├── _connect_loop()          initial connect + setup
    │     └── _setup_mesh_client()   radio params, channels, handlers
    │
    └── asyncio.gather()
          ├── _outbox_task()       reads controller.get_client()
          ├── _retention_task()    unchanged
          ├── _heartbeat_task()    reads controller.get_state()
          ├── telemetry_loop()     unchanged
          ├── liveness_task()      probes radio, fires request_recovery
          └── recovery_task()      runs ladder, swaps client

recovery.py
  RecoveryController     owns mesh_client, state, request_event
  liveness_task()        standalone async function
  recovery_task()        standalone async function
  DEFAULT_LADDER         [Rung("rts_pulse", _rts_pulse_action)]
```

The controller is the single owner of `mesh_client`.  This fixes the
prior bug where `_outbox_task` captured `mesh_client` as a parameter
and would not see a new client after reconnection.

## Files

| File | Role |
|------|------|
| `recovery.py` | Controller, state machine, ladder, liveness/recovery tasks |
| `mesh_bot.py` | Wiring: creates controller, passes to all tasks |
| `config.py` | `RecoveryConfig` dataclass, `[recovery]` TOML section |
| `database.py` | `status.state` column, updated `upsert_status` |
| `tests/test_recovery.py` | 24 unit tests |
| `docs/heltec-recovery.md` | Hardware reference (which resets affect which chips) |
