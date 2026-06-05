# Repository guide

This file is the canonical project guide for both human contributors and
AI coding tools. Tools that auto-discover instructions at the repo root
— Claude Code, Codex, others — read this file as `AGENTS.md`. Human
contributors should read it too. The conventions and invariants below
are load-bearing for either audience.

## What this is

CivicMesh is a WiFi walk-up relay for MeshCore mesh channels at Seattle
Emergency Hubs. It runs a captive portal on a Raspberry Pi (dev: Pi 4,
prod: Pi Zero 2W) that lets users read and post to public MeshCore
radio channels during grid-down events. HTTP-only, offline-first, no
internet required.

## Project layout

Runtime modules at the repo root; vanilla-JS SPA in `static/` (no build
step); stdlib-`unittest` tests in `tests/`; design and operations docs
in `docs/`; bench tooling in `diagnostics/` (not installed as part of
the Python package).

## Commands

```bash
# Setup (uv manages venv + deps)
uv sync

# Run (dev)
uv run civicmesh-web --config config.toml     # captive portal
uv run civicmesh-mesh --config config.toml    # mesh radio relay
uv run civicmesh --config config.toml stats   # admin CLI

# Tests
uv run python -m unittest                                    # all tests
uv run python -m unittest tests.test_admin_outbox_list       # single test module
```

## Architecture

**Two-process model** sharing a single SQLite database (WAL mode):

- **web_server.py** — Synchronous HTTP server on port 8080. Serves the
  SPA, handles `/api/*` endpoints, captive-portal probes, session
  cookies, MAC-based abuse prevention, rate limiting. Queues outbound
  messages.
- **mesh_bot.py** — Async process using `meshcore`. Connects to the
  Heltec V3 radio over USB serial. Concurrent async tasks: outbox
  sender (with exponential backoff), retention pruner, heartbeat
  recorder, and clock-consensus task. Startup acquires
  `fcntl.flock(LOCK_EX)` on `/run/lock/civicmesh-mesh.lock` (pre-created
  by `apply` via `/etc/tmpfiles.d/civicmesh.conf` at mode `0664
  root:dialout`) through `process_lock.acquire_mesh_bot_lock()` to
  prevent two `civicmesh-mesh` processes from running against one
  radio (CIV-80; see `docs/invariants.md`).
- **database.py** — Schema and query functions. Tables: `messages`,
  `outbox`, `votes`, `sessions`, `status`, `heard_packets`,
  `clock_state`, `clock_corrections`. The DB is the sole IPC channel
  between the two processes; see `docs/message_lifecycle.md` for the
  outbox state machine and atomicity rules and
  `docs/clock_consensus.md` for the clock model.
- **clock.py** — Wall-clock correction primitives (CIV-99). Pure
  consensus math + `get_boot_id`, `wall_now`, `get_offset`,
  `ensure_linux_platform`. See "Clock model" below.
- **config.py** — Loads and validates `config.toml`.
- **civicmesh.py** — Operator CLI (SSH-only): pin/unpin, stats, outbox
  management, session queries, `set-clock` admin command.

### Clock model (CIV-99)

CivicMesh nodes run without internet, often on a Pi Zero 2W with no
RTC. The OS clock starts stale every cold boot. CivicMesh keeps an
integer `clock_state.offset_seconds` derived from walk-up phone reports
and adds it to raw `time.time()` on every stamped DB write. We never
set the OS clock from bot code; promotion is a separate root-only
`civicmesh set-clock` admin command.

There are **three time sources** in this codebase. Pick the right one:

| Source | Use it for |
|---|---|
| `wall_now(cfg)` (`int(time.time()) + offset`) | Stamping a moment a human or another process will read — message ts, session timestamps, retention cutoffs, response payloads, log audit rows. |
| `time.monotonic()` | Measuring elapsed time — backoff delays, advert cooldowns, rate-limit windows, cache TTLs. Must not be perturbed by admin/NTP wall jumps. |
| Raw `int(time.time())` | Almost nowhere. The one exception is `compute_and_persist_sender_ts`, which matches the firmware's `time(NULL)` stamp for echo correlation. New code should default to one of the two above. |

Wrong choice doesn't usually fail a test — it silently produces wrong
message timestamps, wrong rate-limit windows, or breaks consensus race
protection. The full design is in `docs/clock_consensus.md`.

**Production prereq**: `systemd-timesyncd.service` and `chrony.service`
must be persistently masked (`sudo systemctl mask <unit>`, NOT
`--runtime`). `civicmesh apply` enforces this by default; dev nodes
that intentionally trust NTP can set `[clock] require_timesync_masked
= false`.

**Annual review**: `sanity_ceiling_epoch` (default 2029-01-01) is an
absolute date. After it, all consensus reports get rejected as "too
far in the future." Bump it once a year. The config loader logs a
WARNING when "now + 180 days" crosses the ceiling so operators see
the reminder.

### MeshCore inbound DM drain (companion firmware quirk)

The Heltec radio does NOT push decoded DMs to the host. The firmware
decrypts a `CONTACT_MSG_RECV` frame and enqueues it in an in-RAM
`offline_queue` (size 16), then sends a 1-byte `MESSAGES_WAITING`
(0x83) tickle. The host has to pull each message by issuing
`CMD_SYNC_NEXT_MESSAGE` (0x0A) until the firmware answers
`NO_MORE_MSGS`. `meshcore_py` wraps that drain loop in
`MeshCore.start_auto_message_fetching()` (`meshcore.py:414-458`).

mesh_bot calls it at `mesh_bot.py:817`, so production is fine. The
trap is for new code that observes inbound DMs without this call:

- **Any host-side reader of inbound DMs — mesh_bot, a diagnostic,
  a future async helper — must `await mc.start_auto_message_fetching()`
  after connect.** Subscribing to `EventType.CONTACT_MSG_RECV` alone
  yields silence: the firmware decodes correctly, sends the
  `MESSAGES_WAITING` tickle, and waits. Nothing crosses the wire
  until the host drains. A 30s passive observer will see zero DMs
  and conclude the radio is broken.

`diagnostics/radio/drain_queue.py` is the minimal reference (subscribe
+ start_auto_message_fetching + window). This protocol cost ~6h of
CIV-14 investigation before it was identified; CIV-106 tracks the
hygiene work that landed this section.

### Firmware contact-table eviction (CIV-14)

The Heltec contact table caps at 350 rows. With `manual_add_contacts`
on (set by `diagnostics/radio/contacts_purge.py`), neighbor adverts no
longer auto-fill the table — but walk-up registrations still hit the
ceiling once enough real users have signed up. When `add_contact`
returns `ERR_CODE_TABLE_FULL`, the contact-registration worker calls
`mesh_bot._evict_one_contact` to free one slot, then retries the add
exactly once.

Three-tier eviction policy, strictly ordered:

- **tier 3** — pubkey present in firmware but absent from the DB. Auto-add
  leftovers from before `manual_add_contacts=True` was set, plus
  anything added out-of-band. Disposable; evicted by smallest
  `last_advert`. No DB row to update.
- **tier 2** — `contacts.status='added'` and `pinned=0`. Real registered
  users who haven't been pinned. Evicted by smallest `last_seen`,
  NULL-first (registered but never DMed the node). The DB row flips
  to `status='evicted'` so `get_contact_by_pubkey_prefix` (used by
  the CONTACT_MSG_RECV handler) stops accepting their DMs.
- **tier 1** — `pinned=1`. Never evicted regardless of age. Pinning is
  absolute.

Operator notes:

- **The success path is silent except for one log line.** Grep for
  `contacts:evicted` (INFO, with `tier=3`/`tier=2`) to see which
  contacts the eviction loop bumped. `tier=3` evictions are healthy
  steady-state churn against neighbor leftovers; mostly `tier=2`
  evictions mean real walk-ups are being bumped — consider pinning
  more or investigating registration churn.
- **All-pinned table** logs `contacts:eviction_no_candidate
  pinned_count=N` (WARN) and falls through to the existing
  `status='error_table_full'` path — the walk-up sees the same
  failure they would have seen before this feature. No regression.
- **Re-registration after eviction** is automatic: `request_contact_add`
  is `INSERT OR REPLACE`, so a previously-evicted user who pastes
  their pubkey into the captive portal again gets a fresh `pending`
  row and goes back through the worker.

## Invariants

These constraints must be preserved across all changes:

- UI must load even if the radio is disconnected; cached messages stay
  readable during outages.
- Outbound message state transitions are monotonic (queued ->
  sent/failed). Both the messages and outbox tables must be updated
  atomically in a single transaction — see `docs/message_lifecycle.md`.
- Rate limiting is enforced before enqueue. Input validation rejects
  empty/oversized messages.
- No HTTPS, accounts, or direct internet dependencies in v0.
- Portal hostnames must **not** use `.local` (iOS bypasses unicast DNS
  for `.local`; see `docs/ios-captive-portal-notes.md`).
- DB schema changes must preserve existing data or include migration.
- CPU/RAM usage must remain suitable for Pi Zero 2W.
- Security log must not include full message content.
- Provisioning must run `apt full-upgrade` and reboot before deployment
  (older kernels have a `brcmfmac` P2P crash from nearby iOS devices).

### Clock-correction invariants (CIV-99)

Sharp edges around the clock model — they are easy to miss in review
and silent-breakage when you do:

- **Wall-stamped DB writes never call `time.time()` inline at the
  caller.** They go through the centralized helpers in `database.py`
  (`insert_message_wall`, `queue_outbox_and_message`,
  `record_post_for_session`, `upsert_status`, `insert_telemetry_*`,
  `update_vote`, `touch_session_last_seen`, `increment_heard`,
  `insert_heard_packet`, `create_or_update_session`,
  `record_clock_report`). Each one reads raw `time.time()` AND
  `clock_state.offset_seconds` INSIDE its own `BEGIN IMMEDIATE` so
  the admin command's `BEGIN EXCLUSIVE` can't slip between the two
  and produce a double-corrected stamp. Production callers must NOT
  pass `ts=` or `_ts_for_test=` (the latter is a test-fixture escape
  hatch).
- **`compute_and_persist_sender_ts` is the one exception** — raw
  `int(time.time())`, no `BEGIN IMMEDIATE`. The MeshCore firmware
  stamps outgoing packets with `time(NULL)`, so our stored sender_ts
  has to use the same reference for echo matching to work. Do NOT
  "fix" this into `wall_now` under a transaction; it looks
  inconsistent on purpose. A source-level test in
  `tests/test_clock.py::TestSenderTsRawTimePin` will catch the
  refactor.
- **`evaluate_and_maybe_apply_consensus` is meaningful as one
  transaction.** Its body — read offset + vote_epoch + reports,
  evaluate, write — is held under a single `BEGIN IMMEDIATE`. Do NOT
  split it for readability; the admin command's `BEGIN EXCLUSIVE`
  serializes against this lock, and any read/eval/write split
  reintroduces the race where admin invalidates the snapshot between
  the bot's read and write. There's a threading test guarding this.
- **`vote_epoch` is bumped only on `admin` and `external_step`.**
  Never on consensus acceptance (consensus consumes votes, doesn't
  invalidate them) and never on cross-boot hygiene (boot ID already
  excludes prior-boot rows). The three rules are distinct; don't
  rationalize them into one.
- **`first_correction_done` is derived from `'consensus'` rows
  only**, scoped to the current boot epoch via
  `clock_corrections.applied_boot_id == current Linux boot ID`.
  Mirrors the `sessions.clock_report_boot_id` design used for client
  clock reports. `applied_at_monotonic` is stored too but is used only
  for in-boot age display in `civicmesh stats`, NOT for identity —
  monotonic-only comparison (`applied_at_monotonic <= time.monotonic()`)
  is sufficient but not necessary: a prior-boot row whose
  CLOCK_MONOTONIC happened to be small silently passes it once
  uptime exceeds the stored value. `'admin'` and `'external_step'`
  rows do NOT count toward first_correction_done — after either, the
  next consensus may legitimately need a large jump.
- **`fake-hwclock save` failure in `civicmesh set-clock` does NOT
  roll back the DB.** The system clock is correct after `date -s`;
  rolling back the DB would leave wall_now = jumped_clock + old_offset
  (double-corrected) at runtime. The audit row carries
  `fake_hwclock_save_failed=true`, CRITICAL is logged, exit non-zero.
  The only residual risk is reboot reversion, recoverable by re-running
  the command after fixing fake-hwclock.
- **CivicMesh runs on Linux only.** `/proc/sys/kernel/random/boot_id`
  is the cross-boot identity gate. `clock.ensure_linux_platform()`
  fails loudly at process startup on macOS/BSD/Windows.

## Style and commits

- 4-space indentation, Python 3.13+ syntax. No linter or formatter
  configured; follow existing file style.
- Imperative commit summaries (e.g., "Add outbox retry logging"); PRs
  include a brief summary and manual-test notes. Include screenshots
  for `static/` changes.

## Configuration

Runtime config is in `config.toml`. When editing config values or
defaults in code, add a comment explaining why the change was made.

`config.toml` is per-deployment (gitignored). Be careful copying a
dev config to a prod node — `[clock] require_timesync_masked = false`
is fine on a dev Pi 4 with internet but disables the production NTP
mask gate. `web_server` and `mesh_bot` log CRITICAL at startup if
they detect the opt-out flag while running from `/usr/local/civicmesh/`
to catch this.
