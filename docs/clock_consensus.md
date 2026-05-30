# Wall-clock correction via display offset (CIV-99)

CivicMesh hubs run on Pi Zero 2W with no RTC. Every cold boot starts at
whatever `fake-hwclock` last saved — often days or weeks stale — and at
deployment sites (Toorcamp, disaster scenarios) there's no internet to
NTP against. Uncorrected, message `ts` columns are wrong, cross-hub
correlation breaks, and absolute-time display ("posted at 3:47 PM") is
meaningless.

This document describes the design of the **offset-on-write** correction
mechanism: how it works, why it is shaped the way it is, and the
invariants future changes must preserve. The mechanism is exposed
through:

- `POST /api/clock` — walk-up phones report their wall-clock time.
- `_clock_task` in `mesh_bot` — periodic consensus task that derives
  `clock_state.offset_seconds` from the reports.
- `civicmesh set-clock` — root admin command that promotes the
  corrected display time into the OS clock.

## Why an offset, not the OS clock

We **never set the system clock from bot code**. The corrected time is
stored as `clock_state.offset_seconds` and added to raw `time.time()` at
every timestamped DB write:

```
ts = int(time.time()) + offset_seconds
```

Trade-offs:

- **No privilege escalation required.** No `CAP_SYS_TIME`, no sudoers
  rule, no setuid helper. The bot runs as the unprivileged
  `civicmesh` user. The admin command does set the OS clock, but it
  is invoked SSH-as-root by a human — that's the one explicit
  privilege boundary.
- **Bad consensus is reversible.** Overwrite `offset_seconds` and the
  next read fixes everything. If we set the OS clock and consensus
  was wrong, every timer in the kernel, every cron job, every
  syslog timestamp, every file mtime is wrong with us.
- **Bounded blast radius.** OS logs (journald), cron, file mtimes,
  and any non-CivicMesh process keep raw system time. Only the rows
  CivicMesh writes through its centralized helpers are corrected.
- **Live tracking.** `offset_seconds` can be nudged each tick to
  follow Pi-Zero RC oscillator drift (typically forward, ~8s/day on
  the bench) without touching the OS clock.

The admin command (`civicmesh set-clock`) exists for the case where
the operator does want the OS clock to match wall — e.g., to make
file mtimes useful for forensics, or to give cron jobs sane firing
times. It is an explicit, human-in-the-loop operation, not something
the bot does on its own.

## Why absolute raw-system offset votes

A walk-up phone posts to `/api/clock` with `{client_time: <epoch sec>}`.
The server stores:

```python
clock_offset_vote_sec = client_time - int(time.time())
```

This is an **absolute raw-system offset vote** — the offset value
the client implicitly endorses, against raw `time.time()`, NOT a
residual against the corrected wall (`wall_now`).

Why absolute?

The consensus task medians the votes directly:

```python
candidate_offset = median(votes)
nudge            = candidate_offset - current_offset
```

The alternative — storing residuals against `wall_now()` and computing
`candidate = current_offset + median(residuals)` — would **double-apply
the offset on every tick after the first acceptance**. If the Pi is 10
days stale, phones report deltas of ~+864000. The first tick sets the
offset to 864000. On the next tick the same phones still report the
same delta (their clocks haven't moved, the Pi's raw clock has barely
moved), and the residual formula computes 864000 + 864000 = 1728000.
After a few ticks the offset is in 2030. This is the "timestamp
machine that slowly invents future history" failure mode.

With absolute votes, the second tick's `median = 864000`, so
`nudge = 864000 - 864000 = 0`, and no-op suppression prevents writing
an audit row. Tested by
`tests/test_clock.py::TestNoDoubleApplicationRegression`.

## Eligibility: boot ID + vote epoch + monotonic age

A naive "filter reports newer than the most recent clock_correction"
mixes clock frames and fails: the consensus row's `system_time_after`
is in the CORRECTED frame (`raw + new_offset`), while
`clock_reported_system_ts` is RAW. After the first consensus
acceptance, every future raw-stamped report looks "older than the
latest correction" and consensus silently starves until an admin
command runs.

Instead, eligibility is gated by **three pure-equality / monotonic
checks**, none of which mix frames:

### 1. Linux boot ID

`/proc/sys/kernel/random/boot_id` is a UUID generated at OS boot and
stable for the lifetime of the boot. Stored on each report row in
`sessions.clock_report_boot_id`. Eligibility requires equality with
`clock.get_boot_id()` cached at task startup. A prior-boot report has
a different UUID and is excluded.

The earlier alternative — comparing `clock_report_mono` against the
process's current `time.monotonic()` — was broken: a prior-boot
report captured at monotonic=5s passes any "> current_monotonic"
test taken at monotonic=10s after a reboot, because CLOCK_MONOTONIC
resets at OS reboot but starts from 0. Boot-ID equality has no such
gap.

### 2. Vote epoch

`clock_state.vote_epoch` is a monotonically-increasing integer
generation counter. It is bumped **inside the same transaction** that
NULLs the per-session vote columns on:

- `civicmesh set-clock` (BEGIN EXCLUSIVE).
- External clock step detected by the consensus task (BEGIN IMMEDIATE).

Eligibility requires `clock_vote_epoch = current_vote_epoch`.

`vote_epoch` is **NOT** bumped on:

- Consensus acceptances. Consensus applies votes; it doesn't invalidate
  them. Bumping would erase the population the next tick would have
  seen.
- The cross-boot storage-hygiene NULL sweep at task startup. Boot-ID
  equality already gates cross-boot reports; no within-boot generation
  change is needed.

### 3. Monotonic age

Reports must satisfy:

```
clock_report_mono >= time.monotonic() - max_report_age_sec
```

Using `time.monotonic()` (not wall) means a within-tick frame change
(admin command, external step) cannot retroactively flip a report's
eligibility. Cross-boot cases are handled by the boot-ID gate above.

### 4. Cookie age (wall)

Cookie age is checked against `wall_now() - created_ts`, where
`created_ts` is wall-stamped at session creation. Both sides are in
the corrected frame; a within-boot offset change shifts both by the
same delta and doesn't move the comparison. `created_ts` predating an
admin/external step by minutes still represents a session minutes
old.

## Consensus math

```python
deduped         = dedupe_by_mac(reports)              # MAC dedupe
if len({r.session_id for r in deduped}) < quorum_min_cookies:
    return None
candidate_offset = median(deduped.votes)              # absolute offset
nudge            = candidate_offset - current_offset
if wall_at(candidate_offset) outside [sanity_floor, sanity_ceiling]:
    return None
if not first_correction_done:
    accept iff candidate_offset >= current_offset      # forward-only, no cap
else:
    accept iff abs(nudge) <= max_nudge_sec             # bidirectional cap
```

**MAC dedupe.** Reports sharing a non-null MAC collapse to a single
vote (most recent wins). A phone clearing cookies still counts once
per device. Cookies without ARP-resolvable MACs count independently.

**Quorum.** `quorum_min_cookies` distinct effective voters required
(default 3). Lower would let a single bad phone steer; higher would
delay convergence at small sites.

**Sanity bound.** `[sanity_floor_epoch, sanity_ceiling_epoch]`, both
**absolute** epochs. **NOT raw-relative** — the very scenario this
feature exists for is a stale raw clock, and a ceiling computed as
`raw_now + 5y` would reject the correct phone report when the Pi
boots in 2016. Defaults: floor = 2024-01-01 UTC, ceiling = 2029-01-01
UTC. Bump as the deployment ages.

**Forward-only on first correction.** Before any consensus acceptance
this boot, the first accepted correction must move corrected wall
forward (`candidate_offset >= current_offset`). This preserves
ordering of any pre-correction rows (the few telemetry / heartbeat
rows stamped before consensus converges). No magnitude cap on this
correction — the whole point is to absorb a 10-day-stale boot in one
jump.

**Bidirectional nudge after.** After a consensus row exists this
boot, `abs(nudge) <= max_nudge_sec` (default 120s). Sign-agnostic so
Pi-Zero RC drift (typically forward) can be corrected. Ordering of
post-correction rows is preserved provided `max_nudge_sec` is small
relative to the gap between consecutive posts, which it is at
human-paced traffic.

**`first_correction_done` is derived from `'consensus'` rows only.**
`'admin'` and `'external_step'` rows do NOT consume the first-correction
privilege. After either, we have just lost confidence in the offset,
and the next consensus may legitimately need a large jump. Scoped to
the current boot epoch via
`applied_at_monotonic <= time.monotonic()` (CLOCK_MONOTONIC resets at
OS boot but survives process restart).

**No-op suppression.** A consensus tick with `nudge == 0` writes
neither an audit row nor a telemetry event. A steady phone population
produces zero nudges most ticks; flooding `clock_corrections` would
drown the state-change events that matter.

## External-step detection

The consensus task watches the **wall-monotonic signal**:

```
signal = time.time() - time.monotonic()
```

This value is constant within a boot if no one touches the system
clock. A jump of more than `external_step_threshold_sec` (default 30s)
between ticks means something else stepped the wall clock — NTP came
back online, an operator ran `date`, a package upgrade re-enabled
`systemd-timesyncd`.

When detected, the task queries `clock_corrections` for any row
written since the last seen id:

- If a new `'admin'` row exists, it accounts for the step. Silently
  refresh the baseline. (The admin command writes its own audit row.)
- Otherwise, append a `'external_step'` row, set `offset_seconds=0`,
  bump `vote_epoch`, NULL all per-session vote columns. Skip
  consensus this tick.

`last_seen_clock_correction_id` is initialized at task startup from
`MAX(id) FROM clock_corrections`. **Essential**: without this, an
old `'admin'` row from a prior boot would always look "new" on
restart and silently suppress an actual external step happening
concurrently.

## NTP coexistence

`civicmesh apply` refuses to proceed unless `systemd-timesyncd` (and
`chrony`, if installed) is **masked** — not merely disabled. This is
the structural defense: the deployment invariant is "no other
process touches the system clock."

`disabled` is NOT sufficient. A merely-disabled unit can still be
started by a manual `systemctl start <unit>`, by another unit's
`Requires=` dependency, or by `systemd --user` automation that
operators sometimes layer in. `masked` redirects the unit file to
`/dev/null`, which causes any start attempt to fail outright.
Unit-not-installed is also acceptable (no file ⇒ no risk).

`civicmesh apply` accepts only `masked` (and `masked-runtime`), and
treats `enabled`, `disabled`, `static`, `alias`, `indirect`,
`generated`, `linked`, and `enabled-runtime` as failures — they all
allow the unit to be started by something. The exit message points
at `systemctl mask <unit>` for remediation.

The runtime external-step detector above is the safety net for when
the structural defense fails (someone re-enables timesyncd manually,
a package upgrade re-enables it, a phone tether briefly provides
internet). In that case the offset is rebased to 0 with a full audit
trail, and consensus restarts cleanly from the new system-clock
baseline.

## `civicmesh set-clock` (admin promotion)

Run as root over SSH. Refuses unless `geteuid() == 0`. No sudoers
rule, no setuid helper.

Sequence, all under one `BEGIN EXCLUSIVE`:

1. Read `clock_state.offset_seconds`. Compute
   `target = int(time.time()) + offset`.
2. `date -s @<target>`. Epoch form sidesteps the
   timedatectl/NTP-active refusal. **If this fails: ROLLBACK,
   exit non-zero, system clock and DB unchanged.**
3. `fake-hwclock save`. Captures stderr if it fails.
4. `UPDATE clock_state SET offset_seconds = 0`.
5. `UPDATE clock_state SET vote_epoch = vote_epoch + 1`.
6. NULL `sessions.clock_offset_vote_sec`, `clock_reported_system_ts`,
   `clock_report_mono`, `clock_report_boot_id`, `clock_vote_epoch`.
7. Append `clock_corrections` row, `trigger='admin'`, with
   `source_summary.fake_hwclock_save_failed=<bool>` and the captured
   stderr.
8. COMMIT.

If `fake-hwclock save` failed: log CRITICAL, exit non-zero. **DB is
still committed.** Rolling back would leave the system clock jumped
but offset unchanged, so `wall_now = jumped_clock + old_offset` —
double-corrected, every live message and log line wrong, until reboot
or manual intervention. Committing keeps runtime correct. The
remaining risk is "correction may be lost on next reboot" — bounded,
observable in the audit row's flag, and recoverable by re-running the
command after fixing fake-hwclock (permissions / disk / package).

## Centralized timestamped-write helpers (invariant)

**Every DB write that stamps a wall-clock ts reads raw `time.time()`
AND `clock_state.offset_seconds` INSIDE its own write transaction
(`BEGIN IMMEDIATE`).** Production callers may not pass a precomputed
`ts` to these helpers; their signatures don't accept one.

The helpers are:

- `insert_message_wall` (production replacement for `insert_message`)
- `queue_outbox_and_message`
- `record_post_for_session`
- `create_or_update_session`
- `upsert_status`
- `insert_telemetry_event`
- `insert_telemetry_sample`
- `increment_heard`
- `insert_heard_packet`
- `touch_session_last_seen`
- `update_vote`
- `evaluate_and_maybe_apply_consensus` *(the consensus-tick helper —
  folds read, evaluate, and write into one BEGIN IMMEDIATE so the
  admin command's BEGIN EXCLUSIVE can't invalidate the snapshot
  between read and write)*
- `compute_and_persist_sender_ts`  *(deliberate exception — see below)*

Why this invariant matters: the admin command sets the system clock
and resets `offset_seconds` to 0 under `BEGIN EXCLUSIVE`. If an
insert reads the offset OUTSIDE its own transaction (e.g., via
`wall_now()` at the call site) and then writes the row inside a
different transaction, the admin command can interleave between the
two — the insert would stamp `new_system_clock + old_offset` =
double-corrected. With the offset read inside the same BEGIN
IMMEDIATE that issues the INSERT, the admin's BEGIN EXCLUSIVE
serializes via SQLite's `busy_timeout` (10s); the insert sees either
the old frame fully or the new frame fully, never a mix.

For test fixtures and migrations that need explicit timestamps, the
helpers expose an `_ts_for_test=` keyword. Production code MUST NOT
pass it; the verification test
`tests/test_clock.py::TestCentralizationInvariant` enforces this by
scanning production source files.

### sender_ts is the exception

`compute_and_persist_sender_ts` intentionally **deviates from both
halves** of the centralized-wall-writer discipline: it uses **raw
`time.time()`** (not `wall_now`) AND it does NOT wrap the UPDATE in
`BEGIN IMMEDIATE`. Reasons:

- **Raw, not wall**: The MeshCore firmware stamps each outgoing
  packet with `time(NULL)` — the unmodified Pi system clock — so for
  our stored `sender_ts` to match the firmware's stamp (which the
  echo carries back via `RX_LOG_DATA.payload.sender_timestamp`), we
  must use the same reference. Echo-match tolerance (±1s in
  `outbox_echoes.py`) absorbs the small gap between our read and the
  firmware's.

- **No BEGIN IMMEDIATE**: The value being stored is raw time, not
  `raw + offset`. There is no offset read to pair with the time
  read, so there is no actor-vs-actor race with the admin command's
  `BEGIN EXCLUSIVE` to defend against. The only failure mode an
  admin step can introduce is a missed echo match for a packet
  already in flight when `date -s` lands — at worst one retransmit,
  no data-integrity issue.

`sender_ts` is never used for human display, row ordering, or
retention cutoffs — only as an echo-match key — so the deviation
does not pollute the wall-corrected `ts` columns. **Do not "fix"
this into `wall_now` under `BEGIN IMMEDIATE`** — the same warning
appears at the function's docstring in `database.py`.

## Elapsed-time discipline

Elapsed-time math uses `time.monotonic()`, not raw or wall time. A
wall-clock jump (admin command, NTP step) must not retroactively
shrink or extend any window. Sites converted in CIV-99:

- `mesh_bot` outbox backoff / idle reset.
- `mesh_bot` advert cooldown.
- `logger._RateLimitedSecurityLogger` window.
- `_stats_cache` TTL in web_server.

`time.monotonic()` is consistent across processes within a boot on
Linux (CLOCK_MONOTONIC), so cross-process monotonic state is
meaningful as long as it resides in memory or in tables that get
invalidated at OS reboot.

## Invariants future code must preserve

1. **No precomputed `ts` from production callers.** All wall-stamped
   DB writes go through the centralized helpers above, which read
   raw time and offset inside their own write transaction. Adding a
   new write site? Read both inside the helper. Reusing an existing
   helper? Don't add a `ts=` kwarg to its signature; if a test needs
   explicit ts, use the `_ts_for_test=` escape hatch.

2. **`vote_epoch` is bumped only on admin and external_step.** Never
   on consensus. Never on cross-boot hygiene. (The first invariant
   protects correctness of accepted consensus; the second avoids
   redundant work that boot-ID already handles.)

3. **Boot identity is `/proc/sys/kernel/random/boot_id`.** Do NOT
   replace this with monotonic comparisons. See the "Eligibility"
   section for why monotonic alone is unreliable.

4. **`first_correction_done` is derived from `'consensus'` rows
   only**, scoped to the current boot epoch via
   `applied_at_monotonic`. Admin and external_step rows must NOT
   count toward it.

5. **No-op consensus ticks (`nudge == 0`) write nothing.** No audit
   row, no telemetry event. The state-change discipline is what keeps
   `clock_corrections` operationally useful.

6. **`fake-hwclock save` failure does NOT roll back the DB.** The
   live system is correct; reboot recovery is the only thing at
   risk, and the audit row's `fake_hwclock_save_failed=true` flag is
   how the operator notices.

7. **`PRAGMA busy_timeout=10000` on every connection.** `_connect`
   sets it; the admin command's `BEGIN EXCLUSIVE` relies on it to
   wait for concurrent inserts instead of failing fast.

8. **Absolute sanity bounds, not raw-relative.** The whole feature
   targets stale raw clocks. A relative ceiling reintroduces the
   exact failure mode the feature exists to fix.

9. **`civicmesh apply` requires `systemd-timesyncd` masked.** The
   external-step detector will rebase if NTP runs anyway, but
   keeping the unit masked is the structural defense.

## Telemetry & operator triage

Two telemetry events fire on state changes (never on no-op ticks):

- `clock_consensus_accepted` — fields include `offset_before_sec`,
  `offset_after_sec`, `nudge_sec`, `voter_count`,
  `median_offset_vote_sec`, `accept_reason`.

State changes are also recorded in `clock_corrections` with
`source_summary` JSON carrying the full voter set:

```json
{
  "cookies": ["session-X", "session-Y", "session-Z"],
  "macs": ["aa:bb:cc:dd:ee:01", "aa:bb:cc:dd:ee:02"],
  "votes": [864000, 864001, 864002],
  "candidate_offset_sec": 864001,
  "nudge_sec": 864001
}
```

For SSH-triage convenience, scalar `voter_count` and
`median_offset_vote_sec` are columns alongside the JSON — no need to
shell into `jq` to answer "who voted last Tuesday."

Admin and external_step rows carry trigger-specific flags in
`source_summary`:

- admin: `fake_hwclock_save_failed`, `fake_hwclock_stderr`,
  `target_epoch`, `original_system_time`, `offset_before_sec`.
- external_step: `signal_delta_sec`, `wall_now`, `wall_at_last_tick`,
  `mono_now`, `mono_at_last_tick`.

## References

- Schema: `database.py` (`SCHEMA_SQL`, plus `clock_state`,
  `clock_corrections`, and sessions migrations in `init_db`).
- Pure consensus math: `clock.py` (`evaluate_consensus`).
- Writers: `database.py` clock-correction helpers section.
- Periodic task: `mesh_bot._clock_task`.
- Admin command: `civicmesh._cmd_set_clock`.
- Tests: `tests/test_clock.py`.
