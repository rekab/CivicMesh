"""Schema and query functions for CivicMesh's SQLite database.

The database is the sole IPC channel between `web_server.py` and
`mesh_bot.py`. Tables: messages, outbox, votes, sessions, status,
heard_packets, telemetry_events, contacts. WAL mode with
synchronous=NORMAL and foreign keys enabled.

Intentionally a flat collection of query functions — no ORM, no
schema introspection at runtime. The outbox state machine and the
atomicity rules around `queue_outbox_and_message` are documented in
`docs/message_lifecycle.md`.
"""

import functools
import json
import logging
import os
import sqlite3
import time
from dataclasses import dataclass
from typing import Any, Optional

from logger import _sanitize_for_log


@dataclass(frozen=True)
class DBConfig:
    path: str = "civic_mesh.db"
    # 10s gives admin command's BEGIN EXCLUSIVE room to wait on a
    # concurrent insert's BEGIN IMMEDIATE without failing fast. The Python
    # sqlite3 `timeout=` kwarg sets sqlite3_busy_timeout under the hood;
    # _connect ALSO issues `PRAGMA busy_timeout=10000` explicitly for
    # belt-and-suspenders (the connect-time setting can be silently
    # overridden by future code, the PRAGMA is harder to lose).
    timeout_sec: float = 10.0


SCHEMA_SQL = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY,
    ts INTEGER NOT NULL,
    channel TEXT NOT NULL,
    sender TEXT NOT NULL,
    content TEXT NOT NULL,
    source TEXT NOT NULL,        -- "mesh" | "wifi"
    session_id TEXT,
    fingerprint TEXT,
    upvotes INTEGER DEFAULT 0,
    downvotes INTEGER DEFAULT 0,
    pinned INTEGER DEFAULT 0,
    pin_order INTEGER,
    outbox_id INTEGER,
    status TEXT              -- "queued" | "sent" | "failed" for wifi; NULL for mesh/local/legacy
);

CREATE TABLE IF NOT EXISTS outbox (
    id INTEGER PRIMARY KEY,
    ts INTEGER NOT NULL,
    channel TEXT NOT NULL,
    sender TEXT NOT NULL,
    content TEXT NOT NULL,
    session_id TEXT NOT NULL,
    fingerprint TEXT,
    sent INTEGER DEFAULT 0,
    retry_count INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'queued',
    -- Heard-count tracking (see docs/heard_count_design.md):
    heard_count INTEGER NOT NULL DEFAULT 0,
    min_path_len INTEGER,
    first_heard_ts INTEGER,
    last_heard_ts INTEGER,
    best_snr REAL,
    sender_ts INTEGER
);

CREATE TABLE IF NOT EXISTS votes (
    message_id INTEGER NOT NULL,
    session_id TEXT NOT NULL,
    vote_type INTEGER NOT NULL,  -- 1=upvote, -1=downvote
    ts INTEGER NOT NULL,
    PRIMARY KEY (message_id, session_id)
);

CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    name TEXT,
    location TEXT,
    mac_address TEXT,
    fingerprint TEXT,
    created_ts INTEGER,
    last_post_ts INTEGER,
    last_seen_ts INTEGER,
    post_count_hour INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS status (
    process TEXT PRIMARY KEY,
    last_seen_ts INTEGER NOT NULL,
    radio_connected INTEGER NOT NULL DEFAULT 0,
    state TEXT
);

-- The radio's own MeshCore identity (public_key + on-air name) republished
-- by mesh_bot on every connect. Single-row table by construction: the CHECK
-- pins id=1 so the upsert always targets the same row and an identity
-- change overwrites the previous values atomically. mesh_bot logs WARN on
-- a public_key delta — see docs/radio-debugging/failure-modes.md for why
-- the radio's pubkey can change unannounced (reflash, factory reset).
-- Consumed by /api/identity for the QR onboarding card in the stats sheet.
CREATE TABLE IF NOT EXISTS node_identity (
    id INTEGER PRIMARY KEY CHECK(id = 1),
    public_key TEXT NOT NULL,
    name TEXT NOT NULL,
    updated_ts INTEGER NOT NULL
);

-- Walk-up users who have registered with this node via the captive-portal
-- contact-add form (CIV-14). pubkey is the 64-char lowercase-hex MeshCore
-- identity key. status is the registration state machine:
--   'pending'          — queued; mesh_bot's contact-registration worker
--                        has not yet pushed it to the firmware contact
--                        table.
--   'added'            — firmware add_contact returned OK; the node can
--                        now both receive DMs from this pubkey and route
--                        replies back to it.
--   'evicted'          — firmware table was full when a newer pubkey
--                        registered and this row was bumped to make room
--                        (mesh_bot._evict_one_contact, three-tier LRU
--                        policy). Re-registration via the captive portal
--                        flips it back to 'pending' (request_contact_add
--                        does INSERT OR REPLACE).
--   'error_table_full' — firmware refused with ERR_CODE_TABLE_FULL AND
--                        the eviction helper found no candidate to bump
--                        (every slot pinned, or eviction itself failed).
--   'error_other'      — any other firmware ERROR; payload in error_detail.
-- pinned (0/1) reserves a contact against LRU eviction. Pinned rows are
-- tier 1: never evicted regardless of last_seen. created_at is wall_now
-- at first registration; last_seen is wall_now of the most recent
-- inbound DM (populated by the CONTACT_MSG_RECV handler). adv_name is
-- NOT stored locally — mesh_bot reads it from the firmware contact cache
-- via get_contacts() when admin UIs need it. The firmware backfills
-- adv_name from received ADVERTISEMENT packets automatically (verified
-- empirically; see diagnostics/radio/minimum_contact_probe.py
-- --watch-adverts).
CREATE TABLE IF NOT EXISTS contacts (
    pubkey TEXT PRIMARY KEY,
    status TEXT NOT NULL DEFAULT 'pending',
    error_detail TEXT,
    pinned INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL,
    last_seen INTEGER
);
CREATE INDEX IF NOT EXISTS idx_contacts_pending ON contacts(status);

-- Clock-correction state. clock_state is a KV singleton: 'offset_seconds'
-- is the integer added to raw time.time() to get corrected wall time, and
-- 'vote_epoch' is a monotonically-increasing generation counter bumped on
-- admin and external-step events to invalidate stale per-session clock
-- reports without scanning timestamps across reference frames. See
-- docs/clock_consensus.md.
CREATE TABLE IF NOT EXISTS clock_state (
    key TEXT PRIMARY KEY,
    value TEXT
);

-- Audit log: consensus acceptance + admin command + external-step
-- detection. system_time_before/after meaning is per-trigger:
--
--   'consensus'     : both = int(time.time()) at event time. Consensus
--                     never touches the OS clock, only clock_state.offset,
--                     so before == after.
--   'admin'         : system_time_before = original time.time() pre-jump;
--                     system_time_after  = `date -s` target value (the
--                     new time.time() after the jump). The change here
--                     is the operator's clock promotion.
--   'external_step' : both = int(time.time()) when the step was detected.
--                     We don't know what wall was before the external
--                     step happened (NTP, manual `date`, etc.) — we
--                     only know what it is now.
--
-- applied_boot_id is the Linux boot ID at insert time. Boot scoping
-- (first_correction_done check, stats prior-boot detection) compares
-- equality against the current boot ID. applied_at_monotonic alone
-- cannot answer "is this row from this boot?" — a prior-boot row with
-- small monotonic value silently passes "monotonic <= now" tests once
-- the current process has been up long enough. Mirrors the sessions
-- table's boot-id-for-identity / monotonic-for-age split.
--
-- voter_count and median_offset_vote_sec are NULL for non-consensus
-- triggers. source_summary holds JSON: voter cookies/MACs, individual
-- offset votes, and trigger-specific flags like fake_hwclock_save_failed.
CREATE TABLE IF NOT EXISTS clock_corrections (
    id INTEGER PRIMARY KEY,
    applied_at_monotonic    REAL,
    applied_boot_id         TEXT,
    system_time_before      INTEGER,
    system_time_after       INTEGER,
    offset_before_sec       INTEGER,
    offset_after_sec        INTEGER,
    trigger                 TEXT,
    voter_count             INTEGER,
    median_offset_vote_sec  INTEGER,
    source_summary          TEXT
);
CREATE INDEX IF NOT EXISTS idx_clock_corrections_id ON clock_corrections(id);

CREATE INDEX IF NOT EXISTS idx_messages_channel_ts ON messages(channel, ts DESC);
CREATE INDEX IF NOT EXISTS idx_messages_pinned ON messages(pinned, pin_order);
CREATE INDEX IF NOT EXISTS idx_outbox_pending ON outbox(sent, ts);
CREATE INDEX IF NOT EXISTS idx_votes_message ON votes(message_id);
CREATE INDEX IF NOT EXISTS idx_sessions_mac ON sessions(mac_address);

CREATE TABLE IF NOT EXISTS heard_packets (
    ts INTEGER NOT NULL,
    payload_type INTEGER NOT NULL,
    route_type INTEGER NOT NULL,
    path_len INTEGER NOT NULL,
    last_path_byte INTEGER,
    snr REAL,
    rssi INTEGER
);
CREATE INDEX IF NOT EXISTS idx_heard_packets_ts ON heard_packets(ts);

CREATE TABLE IF NOT EXISTS telemetry_samples (
    ts INTEGER PRIMARY KEY,
    uptime_s INTEGER,
    load_1m REAL,
    cpu_temp_c REAL,
    mem_available_kb INTEGER,
    mem_total_kb INTEGER,
    disk_free_kb INTEGER,
    disk_total_kb INTEGER,
    net_rx_bytes INTEGER,
    net_tx_bytes INTEGER,
    outbox_depth INTEGER,
    outbox_oldest_age_s INTEGER,
    throttled_bitmask INTEGER
);

CREATE TABLE IF NOT EXISTS telemetry_events (
    id INTEGER PRIMARY KEY,
    ts INTEGER NOT NULL,
    kind TEXT NOT NULL,
    detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_telemetry_events_ts ON telemetry_events(ts);
CREATE INDEX IF NOT EXISTS idx_telemetry_events_kind_ts ON telemetry_events(kind, ts);

-- Companion-radio stats (distinct from telemetry_samples, which is the Pi's
-- own health). Sampled from get_stats_core()/get_stats_radio() over serial.
CREATE TABLE IF NOT EXISTS radio_samples (
    ts INTEGER PRIMARY KEY,
    battery_mv INTEGER,
    radio_uptime_s INTEGER,
    err_bitmask INTEGER,
    tx_queue_len INTEGER,
    noise_floor INTEGER,
    last_rssi INTEGER,
    last_snr REAL,
    tx_air_secs INTEGER,
    rx_air_secs INTEGER
);

-- Battery state from the Victron BMV-712 (BLE Instant Readout). Distinct from
-- radio_samples.battery_mv, which is the Heltec's coarse coil voltage; this is
-- true pack state of charge / voltage / signed current sampled by power_monitor.
-- soc is percent (e.g. 99.7); any field may be NULL when the BMV reports it
-- unavailable (e.g. SoC before the shunt syncs).
CREATE TABLE IF NOT EXISTS power_samples (
    ts INTEGER PRIMARY KEY,
    soc REAL,
    voltage_mv INTEGER,
    current_ma INTEGER,
    power_w REAL
);

"""


_lock_retry_log = logging.getLogger("civicmesh.db")


def _retry_on_locked(attempts=3, base_delay=0.05):
    """Retry on sqlite3.OperationalError('database is locked').

    Sequential backoff: 50ms / 150ms / 450ms.  Applied only to
    data-critical writes where a lock failure means data loss.
    """
    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            for i in range(attempts):
                try:
                    return fn(*args, **kwargs)
                except sqlite3.OperationalError as e:
                    if "database is locked" not in str(e) or i == attempts - 1:
                        raise
                    _lock_retry_log.warning(
                        "db:retry_on_locked fn=%s attempt=%d", fn.__name__, i + 1,
                    )
                    time.sleep(base_delay * (3 ** i))
            raise AssertionError("unreachable")
        return wrapper
    return decorator


def _connect(cfg: DBConfig) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(cfg.path) or ".", exist_ok=True)
    conn = sqlite3.connect(cfg.path, timeout=cfg.timeout_sec, isolation_level=None)  # autocommit
    conn.row_factory = sqlite3.Row
    # Belt-and-suspenders explicit busy_timeout. sqlite3.connect(timeout=)
    # already installs sqlite3_busy_timeout, but the explicit PRAGMA is
    # more visible to readers and harder to silently override. Without
    # busy_timeout, the admin command's BEGIN EXCLUSIVE would fail fast
    # against any concurrent BEGIN IMMEDIATE from web_server / mesh_bot
    # instead of waiting. See docs/clock_consensus.md § concurrency.
    conn.execute(f"PRAGMA busy_timeout={int(cfg.timeout_sec * 1000)}")
    return conn


def init_db(cfg: DBConfig, log=None) -> None:
    conn = _connect(cfg)
    try:
        if log:
            log.debug("db:init schema path=%s", _sanitize_for_log(cfg.path))
        conn.executescript(SCHEMA_SQL)
        # Lightweight migration for older DBs.
        sessions_cols = [r["name"] for r in conn.execute("PRAGMA table_info(sessions)").fetchall()]
        if "fingerprint" not in sessions_cols:
            if log:
                log.info("db:migrate add sessions.fingerprint")
            conn.execute("ALTER TABLE sessions ADD COLUMN fingerprint TEXT")
        messages_cols = [r["name"] for r in conn.execute("PRAGMA table_info(messages)").fetchall()]
        if "session_id" not in messages_cols:
            if log:
                log.info("db:migrate add messages.session_id")
            conn.execute("ALTER TABLE messages ADD COLUMN session_id TEXT")
        if "fingerprint" not in messages_cols:
            if log:
                log.info("db:migrate add messages.fingerprint")
            conn.execute("ALTER TABLE messages ADD COLUMN fingerprint TEXT")
        if "outbox_id" not in messages_cols:
            if log:
                log.info("db:migrate add messages.outbox_id")
            conn.execute("ALTER TABLE messages ADD COLUMN outbox_id INTEGER")
        if "status" not in messages_cols:
            if log:
                log.info("db:migrate add messages.status")
            conn.execute("ALTER TABLE messages ADD COLUMN status TEXT")
        outbox_cols = [r["name"] for r in conn.execute("PRAGMA table_info(outbox)").fetchall()]
        if "fingerprint" not in outbox_cols:
            if log:
                log.info("db:migrate add outbox.fingerprint")
            conn.execute("ALTER TABLE outbox ADD COLUMN fingerprint TEXT")
        if "retry_count" not in outbox_cols:
            if log:
                log.info("db:migrate add outbox.retry_count")
            conn.execute("ALTER TABLE outbox ADD COLUMN retry_count INTEGER NOT NULL DEFAULT 0")
            conn.execute("UPDATE outbox SET retry_count=0 WHERE retry_count IS NULL")
        if "status" not in outbox_cols:
            if log:
                log.info("db:migrate add outbox.status")
            conn.execute("ALTER TABLE outbox ADD COLUMN status TEXT NOT NULL DEFAULT 'queued'")
        # Heard-count columns (echo tracking from RX_LOG_DATA).
        # Schema rationale and matching design are in
        # docs/heard_count_design.md and diagnostics/radio/FINDINGS.md.
        if "heard_count" not in outbox_cols:
            if log:
                log.info("db:migrate add outbox.heard_count")
            conn.execute("ALTER TABLE outbox ADD COLUMN heard_count INTEGER NOT NULL DEFAULT 0")
        if "min_path_len" not in outbox_cols:
            if log:
                log.info("db:migrate add outbox.min_path_len")
            conn.execute("ALTER TABLE outbox ADD COLUMN min_path_len INTEGER")
        if "first_heard_ts" not in outbox_cols:
            if log:
                log.info("db:migrate add outbox.first_heard_ts")
            conn.execute("ALTER TABLE outbox ADD COLUMN first_heard_ts INTEGER")
        if "last_heard_ts" not in outbox_cols:
            if log:
                log.info("db:migrate add outbox.last_heard_ts")
            conn.execute("ALTER TABLE outbox ADD COLUMN last_heard_ts INTEGER")
        if "best_snr" not in outbox_cols:
            if log:
                log.info("db:migrate add outbox.best_snr")
            conn.execute("ALTER TABLE outbox ADD COLUMN best_snr REAL")
        if "sender_ts" not in outbox_cols:
            if log:
                log.info("db:migrate add outbox.sender_ts")
            conn.execute("ALTER TABLE outbox ADD COLUMN sender_ts INTEGER")
        # Align status with existing sent flags or clean up bad values.
        conn.execute(
            """
            UPDATE outbox
            SET status = CASE
                WHEN sent=1 THEN 'sent'
                WHEN sent=0 THEN 'queued'
                ELSE 'queued'
            END
            WHERE status IS NULL OR status NOT IN ('queued', 'sent', 'failed')
            """
        )
        # Legacy migration: reconcile old outbox rows that were sent before
        # the status column existed. Only matches messages WITHOUT a status
        # (pre-Strategy-A rows inserted by mesh_bot on success). Messages
        # with status='queued' are Strategy A rows — do NOT treat as sent.
        conn.execute(
            """
            UPDATE outbox
            SET status='sent', sent=1
            WHERE status='queued'
              AND EXISTS (
                SELECT 1
                FROM messages m
                WHERE m.ts = outbox.ts
                  AND m.channel = outbox.channel
                  AND m.sender = outbox.sender
                  AND m.content = outbox.content
                  AND m.source = 'wifi'
                  AND m.status IS NULL
              )
            """
        )
        outbox_cols = [r["name"] for r in conn.execute("PRAGMA table_info(outbox)").fetchall()]
        if "status" in outbox_cols:
            conn.execute("CREATE INDEX IF NOT EXISTS idx_outbox_status ON outbox(status, ts)")

        # -- heard_packets table (stats endpoint) --
        conn.execute("""CREATE TABLE IF NOT EXISTS heard_packets (
            ts INTEGER NOT NULL,
            payload_type INTEGER NOT NULL,
            route_type INTEGER NOT NULL,
            path_len INTEGER NOT NULL,
            last_path_byte INTEGER,
            snr REAL,
            rssi INTEGER
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_heard_packets_ts ON heard_packets(ts)")

        # -- telemetry tables --
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS telemetry_samples (
                ts INTEGER PRIMARY KEY,
                uptime_s INTEGER,
                load_1m REAL,
                cpu_temp_c REAL,
                mem_available_kb INTEGER,
                mem_total_kb INTEGER,
                disk_free_kb INTEGER,
                disk_total_kb INTEGER,
                net_rx_bytes INTEGER,
                net_tx_bytes INTEGER,
                outbox_depth INTEGER,
                outbox_oldest_age_s INTEGER,
                throttled_bitmask INTEGER
            );
            CREATE TABLE IF NOT EXISTS telemetry_events (
                id INTEGER PRIMARY KEY,
                ts INTEGER NOT NULL,
                kind TEXT NOT NULL,
                detail TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_telemetry_events_ts ON telemetry_events(ts);
            CREATE INDEX IF NOT EXISTS idx_telemetry_events_kind_ts ON telemetry_events(kind, ts);
            CREATE TABLE IF NOT EXISTS radio_samples (
                ts INTEGER PRIMARY KEY,
                battery_mv INTEGER,
                radio_uptime_s INTEGER,
                err_bitmask INTEGER,
                tx_queue_len INTEGER,
                noise_floor INTEGER,
                last_rssi INTEGER,
                last_snr REAL,
                tx_air_secs INTEGER,
                rx_air_secs INTEGER
            );
        """)

        # -- sessions.last_seen_ts --
        sessions_cols = {r[1] for r in conn.execute("PRAGMA table_info(sessions)").fetchall()}
        if "last_seen_ts" not in sessions_cols:
            if log:
                log.info("db:migrate add sessions.last_seen_ts")
            conn.execute("ALTER TABLE sessions ADD COLUMN last_seen_ts INTEGER")

        # -- status.state (recovery state) --
        status_cols = [r["name"] for r in conn.execute("PRAGMA table_info(status)").fetchall()]
        if "state" not in status_cols:
            if log:
                log.info("db:migrate add status.state")
            conn.execute("ALTER TABLE status ADD COLUMN state TEXT")

        # -- clock-correction migrations (CIV-99) --
        # sessions gets four columns to hold per-client wall-clock reports.
        # clock_offset_vote_sec is the ABSOLUTE raw-system offset endorsed
        # by the client: client_epoch - int(time.time()), NOT a residual
        # against corrected wall time. The consensus task medians votes
        # directly (candidate = median(votes)); double-applying the offset
        # tick-over-tick is the bug this naming exists to prevent.
        # clock_report_boot_id holds /proc/sys/kernel/random/boot_id at the
        # moment of capture and gates eligibility across OS reboots; the
        # within-boot generation counter clock_vote_epoch handles admin
        # and external-step invalidations.
        sessions_cols2 = {r[1] for r in conn.execute("PRAGMA table_info(sessions)").fetchall()}
        if "clock_offset_vote_sec" not in sessions_cols2:
            if log:
                log.info("db:migrate add sessions.clock_offset_vote_sec")
            conn.execute("ALTER TABLE sessions ADD COLUMN clock_offset_vote_sec INTEGER")
        if "clock_reported_system_ts" not in sessions_cols2:
            if log:
                log.info("db:migrate add sessions.clock_reported_system_ts")
            conn.execute("ALTER TABLE sessions ADD COLUMN clock_reported_system_ts INTEGER")
        if "clock_report_mono" not in sessions_cols2:
            if log:
                log.info("db:migrate add sessions.clock_report_mono")
            conn.execute("ALTER TABLE sessions ADD COLUMN clock_report_mono REAL")
        if "clock_report_boot_id" not in sessions_cols2:
            if log:
                log.info("db:migrate add sessions.clock_report_boot_id")
            conn.execute("ALTER TABLE sessions ADD COLUMN clock_report_boot_id TEXT")
        if "clock_vote_epoch" not in sessions_cols2:
            if log:
                log.info("db:migrate add sessions.clock_vote_epoch")
            conn.execute("ALTER TABLE sessions ADD COLUMN clock_vote_epoch INTEGER")

        # CIV-99 follow-up: applied_boot_id on clock_corrections.
        # Without this, "is this row from this boot?" devolves into a
        # monotonic comparison, which only proves "could be this boot,"
        # not "is this boot" — see the schema comment.
        corrections_cols = {
            r[1]
            for r in conn.execute("PRAGMA table_info(clock_corrections)").fetchall()
        }
        if "applied_boot_id" not in corrections_cols:
            if log:
                log.info("db:migrate add clock_corrections.applied_boot_id")
            conn.execute(
                "ALTER TABLE clock_corrections ADD COLUMN applied_boot_id TEXT"
            )

        # Seed clock_state singletons. INSERT OR IGNORE so a re-run never
        # overwrites a live offset or vote_epoch.
        conn.execute(
            "INSERT OR IGNORE INTO clock_state (key, value) VALUES ('offset_seconds', '0')"
        )
        conn.execute(
            "INSERT OR IGNORE INTO clock_state (key, value) VALUES ('vote_epoch', '0')"
        )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Centralized wall-clock write helpers (CIV-99)
#
# INVARIANT for stamped DB writes: read raw `time.time()` AND
# `clock_state.offset_seconds` INSIDE the same write transaction (BEGIN
# IMMEDIATE) that issues the INSERT/UPDATE. The admin command
# (`civicmesh-set-clock`) uses BEGIN EXCLUSIVE to atomically step the
# system clock and reset offset to 0; the BEGIN IMMEDIATE here serializes
# against it via busy_timeout, so any given write lands fully in the old
# reference frame (old time.time(), old offset) or fully in the new (new
# time.time(), offset=0) — never a mix. This is the actor-vs-actor race
# the centralization defends. Production callers MUST go through these
# `*_wall` helpers; passing a pre-computed `ts` would reintroduce the
# race. The non-`_wall` siblings (insert_message, queue_outbox_and_message,
# upsert_status, etc.) remain for test fixtures and migration tooling that
# need explicit timestamps.
#
# See docs/clock_consensus.md § "Centralized write helpers" and the
# verification test in tests/test_clock.py.
# ---------------------------------------------------------------------------


def _read_offset_for_wall_write(conn) -> int:
    """Read clock_state.offset_seconds for in-txn stamping.

    Caller MUST already hold the writer lock (BEGIN IMMEDIATE) so the
    offset value is consistent with the raw time.time() reads that pair
    with it under this transaction. Returns 0 if the row is missing
    (fresh DB pre-init) or unparseable, which is the safe default —
    `wall = raw + 0` keeps writes consistent with no-correction state.
    """
    row = conn.execute(
        "SELECT value FROM clock_state WHERE key='offset_seconds'"
    ).fetchone()
    if row is None:
        return 0
    try:
        return int(row["value"])
    except (TypeError, ValueError):
        return 0


def _wall_ts_in_txn(conn) -> int:
    """Inside an open write transaction, return `int(time.time()) + offset`.

    Reads raw time and offset under the caller's BEGIN IMMEDIATE so an
    admin BEGIN EXCLUSIVE can't slip between the two reads.
    """
    offset = _read_offset_for_wall_write(conn)
    return int(time.time()) + offset


@_retry_on_locked()
def insert_message_wall(
    cfg: DBConfig,
    *,
    channel: str,
    sender: str,
    content: str,
    source: str,
    session_id: Optional[str] = None,
    fingerprint: Optional[str] = None,
    outbox_id: Optional[int] = None,
    status: Optional[str] = None,
    log=None,
) -> int:
    """Insert a row into messages, stamping ts inside a BEGIN IMMEDIATE.

    Production callers in web_server (local posts) and mesh_bot (mesh rx)
    use this. The signature deliberately does NOT accept `ts` — see the
    section header above.
    """
    conn = _connect(cfg)
    try:
        if log:
            log.debug(
                "db:insert_message_wall channel=%s sender=%s source=%s len=%d",
                channel, sender, source, len(content),
            )
        conn.execute("BEGIN IMMEDIATE")
        try:
            ts = _wall_ts_in_txn(conn)
            cur = conn.execute(
                "INSERT INTO messages (ts, channel, sender, content, source, "
                "session_id, fingerprint, outbox_id, status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (ts, channel, sender, content, source, session_id,
                 fingerprint, outbox_id, status),
            )
            mid = int(cur.lastrowid)
            conn.execute("COMMIT")
            return mid
        except:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.OperationalError:
                pass
            raise
    finally:
        conn.close()


@_retry_on_locked()
def insert_message(
    cfg: DBConfig,
    *,
    ts: int,
    channel: str,
    sender: str,
    content: str,
    source: str,
    session_id: Optional[str] = None,
    fingerprint: Optional[str] = None,
    outbox_id: Optional[int] = None,
    status: Optional[str] = None,
    log=None,
) -> int:
    """Lower-level message INSERT accepting an explicit `ts`.

    Used by test fixtures and migration tooling. Production callers
    MUST use insert_message_wall instead (see the section header on
    centralized wall-clock write helpers above).
    """
    conn = _connect(cfg)
    try:
        if log:
            log.debug("db:insert_message channel=%s sender=%s source=%s len=%d", channel, sender, source, len(content))
        cur = conn.execute(
            "INSERT INTO messages (ts, channel, sender, content, source, session_id, fingerprint, outbox_id, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (ts, channel, sender, content, source, session_id, fingerprint, outbox_id, status),
        )
        return int(cur.lastrowid)
    finally:
        conn.close()


def reconcile_message_status(cfg: DBConfig, log=None) -> int:
    """Fix messages whose status is out of sync with their outbox row.

    This handles the case where mesh_bot is killed after outbox status
    changes but before the messages row is updated, or any other
    inconsistency between the two tables."""
    conn = _connect(cfg)
    try:
        conn.execute("BEGIN")
        cur1 = conn.execute(
            "UPDATE messages SET status='sent' WHERE outbox_id IN "
            "(SELECT id FROM outbox WHERE status='sent') AND status='queued'"
        )
        cur2 = conn.execute(
            "UPDATE messages SET status='failed' WHERE outbox_id IN "
            "(SELECT id FROM outbox WHERE status='failed') AND status='queued'"
        )
        conn.execute("COMMIT")
        total = (cur1.rowcount or 0) + (cur2.rowcount or 0)
        if total and log:
            log.info("db:reconcile_message_status fixed=%d", total)
        return total
    except:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def update_message_status(cfg: DBConfig, *, outbox_id: int, status: str, log=None) -> None:
    """Update the status of a message row linked to an outbox send."""
    conn = _connect(cfg)
    try:
        if log:
            log.debug("db:update_message_status outbox_id=%d status=%s", outbox_id, status)
        conn.execute(
            "UPDATE messages SET status = ? WHERE outbox_id = ?",
            (status, int(outbox_id)),
        )
    finally:
        conn.close()


def insert_session(
    cfg: DBConfig,
    *,
    session_id: str,
    name: Optional[str],
    location: Optional[str],
    mac_address: Optional[str],
    fingerprint: Optional[str],
    created_ts: Optional[int],
    last_post_ts: Optional[int],
    post_count_hour: int = 0,
    log=None,
) -> None:
    conn = _connect(cfg)
    try:
        if log:
            log.debug("db:insert_session session_id=%s", session_id)
        conn.execute(
            "INSERT INTO sessions (session_id, name, location, mac_address, fingerprint, created_ts, last_post_ts, post_count_hour) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (session_id, name, location, mac_address, fingerprint, created_ts, last_post_ts, post_count_hour),
        )
    finally:
        conn.close()


def get_recent_messages_filtered(
    cfg: DBConfig,
    *,
    channel: Optional[str],
    source: Optional[str],
    session_id: Optional[str] = None,
    limit: int = 20,
    log=None,
) -> list[dict[str, Any]]:
    conn = _connect(cfg)
    try:
        if log:
            log.debug(
                "db:get_recent_messages_filtered channel=%s source=%s session_id=%s limit=%d",
                channel,
                source,
                session_id,
                limit,
            )

        conditions = []
        params: list[Any] = []
        if channel:
            conditions.append("messages.channel=?")
            params.append(channel)
        if source:
            conditions.append("messages.source=?")
            params.append(source)
        if session_id:
            conditions.append("messages.session_id=?")
            params.append(session_id)

        where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
        sql = (
            "SELECT messages.*, outbox.retry_count"
            " FROM messages"
            " LEFT JOIN outbox ON messages.outbox_id = outbox.id"
            f"{where} "
            "ORDER BY messages.ts DESC "
            "LIMIT ?"
        )
        params.append(limit)
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_recent_sessions(cfg: DBConfig, *, limit: int = 20, log=None) -> list[dict[str, Any]]:
    conn = _connect(cfg)
    try:
        if log:
            log.debug("db:get_recent_sessions limit=%d", limit)
        rows = conn.execute(
            "SELECT * FROM sessions "
            "ORDER BY (last_post_ts IS NULL) ASC, last_post_ts DESC, created_ts DESC "
            "LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_session_by_id(cfg: DBConfig, *, session_id: str, log=None) -> Optional[dict[str, Any]]:
    conn = _connect(cfg)
    try:
        if log:
            log.debug("db:get_session_by_id session_id=%s", session_id)
        row = conn.execute(
            "SELECT * FROM sessions WHERE session_id=?",
            (session_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_messages(
    cfg: DBConfig,
    *,
    channel: str,
    viewer_session_id: Optional[str],
    limit: int = 50,
    offset: int = 0,
    include_pinned: bool = True,
    log=None,
) -> list[dict[str, Any]]:
    """Return messages for a channel, with `is_own` projected against the
    viewer's session id. `session_id` and `fingerprint` are intentionally
    omitted from the projection — the row's session_id IS the poster's
    cookie value, and exposing it would let any portal viewer hijack any
    other walk-up's session. Pass `viewer_session_id=None` for an
    anonymous viewer; every row will come back with `is_own = 0`.
    """
    conn = _connect(cfg)
    try:
        if log:
            log.debug("db:get_messages channel=%s limit=%d offset=%d", channel, limit, offset)

        # Hand-rolled column list (not messages.*) so a future column
        # added to the messages table does not auto-flow into HTTP
        # responses. New columns must be opted in here explicitly.
        cols = (
            "messages.id, messages.ts, messages.channel, messages.sender,"
            " messages.content, messages.source,"
            " messages.upvotes, messages.downvotes,"
            " messages.pinned, messages.pin_order,"
            " messages.outbox_id, messages.status,"
            " (messages.session_id IS NOT NULL AND messages.session_id = ?) AS is_own,"
            " outbox.heard_count, outbox.retry_count"
        )

        rows: list[sqlite3.Row] = []
        if include_pinned:
            rows.extend(
                conn.execute(
                    f"SELECT {cols}"
                    " FROM messages"
                    " LEFT JOIN outbox ON messages.outbox_id = outbox.id"
                    " WHERE messages.channel=? AND messages.pinned=1"
                    " ORDER BY messages.pin_order ASC NULLS LAST, messages.ts DESC",
                    (viewer_session_id, channel),
                ).fetchall()
            )
        rows.extend(
            conn.execute(
                f"SELECT {cols}"
                " FROM messages"
                " LEFT JOIN outbox ON messages.outbox_id = outbox.id"
                " WHERE messages.channel=? AND messages.pinned=0"
                " ORDER BY messages.ts DESC LIMIT ? OFFSET ?",
                (viewer_session_id, channel, limit, offset),
            ).fetchall()
        )
        return [dict(r) for r in rows]
    finally:
        conn.close()


def upsert_status(
    cfg: DBConfig,
    *,
    process: str,
    radio_connected: bool,
    state: Optional[str] = None,
    log=None,
    _ts_for_test: Optional[int] = None,
) -> None:
    """Upsert the process-liveness row in `status`.

    CIV-99: `last_seen_ts` is stamped as `wall_now()` (raw + offset) read
    INSIDE the BEGIN IMMEDIATE that holds the write lock — so an admin
    `civicmesh-set-clock` command's BEGIN EXCLUSIVE serializes correctly
    and the heartbeat row lands fully in either the pre- or post-jump
    frame, never a mix. Production callers MUST NOT pass `_ts_for_test`.
    """
    conn = _connect(cfg)
    try:
        if log:
            log.debug("status:upsert process=%s connected=%s state=%s",
                       process, bool(radio_connected), state)
        conn.execute("BEGIN IMMEDIATE")
        try:
            now_ts = int(_ts_for_test) if _ts_for_test is not None else _wall_ts_in_txn(conn)
            conn.execute(
                """
                INSERT INTO status(process, last_seen_ts, radio_connected, state)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(process) DO UPDATE SET
                    last_seen_ts=excluded.last_seen_ts,
                    radio_connected=excluded.radio_connected,
                    state=COALESCE(excluded.state, status.state)
                """,
                (process, now_ts, 1 if radio_connected else 0, state),
            )
            conn.execute("COMMIT")
        except:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.OperationalError:
                pass
            raise
    finally:
        conn.close()


def get_status(cfg: DBConfig, *, process: str, log=None) -> Optional[dict[str, Any]]:
    conn = _connect(cfg)
    try:
        if log:
            log.debug("status:get process=%s", process)
        row = conn.execute("SELECT * FROM status WHERE process=?", (process,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def upsert_node_identity(
    cfg: DBConfig,
    *,
    public_key: str,
    name: str,
    log=None,
    _ts_for_test: Optional[int] = None,
) -> Optional[str]:
    """Upsert the node_identity singleton row. Returns the prior public_key
    if one was persisted and differed (so the caller can WARN), else None.

    `updated_ts` is wall_now() read inside the BEGIN IMMEDIATE — same
    invariant as upsert_status (CIV-99). Production callers MUST NOT
    pass `_ts_for_test`.
    """
    conn = _connect(cfg)
    try:
        if log:
            log.debug("identity:upsert name=%s pubkey_prefix=%s",
                      name, (public_key or "")[:8])
        conn.execute("BEGIN IMMEDIATE")
        try:
            prior = conn.execute(
                "SELECT public_key FROM node_identity WHERE id=1"
            ).fetchone()
            now_ts = int(_ts_for_test) if _ts_for_test is not None else _wall_ts_in_txn(conn)
            conn.execute(
                """
                INSERT INTO node_identity(id, public_key, name, updated_ts)
                VALUES (1, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    public_key=excluded.public_key,
                    name=excluded.name,
                    updated_ts=excluded.updated_ts
                """,
                (public_key, name, now_ts),
            )
            conn.execute("COMMIT")
        except:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.OperationalError:
                pass
            raise
    finally:
        conn.close()
    if prior is not None and prior["public_key"] != public_key:
        return prior["public_key"]
    return None


def get_node_identity(cfg: DBConfig, *, log=None) -> Optional[dict[str, Any]]:
    conn = _connect(cfg)
    try:
        if log:
            log.debug("identity:get")
        row = conn.execute("SELECT * FROM node_identity WHERE id=1").fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# contacts table — captive-portal contact-add flow (CIV-14)
# ---------------------------------------------------------------------------

def request_contact_add(
    cfg: DBConfig,
    *,
    pubkey: str,
    log=None,
    _ts_for_test: Optional[int] = None,
) -> str:
    """Queue a contact-registration request.

    Idempotent: if status='added', leaves the row untouched and returns
    'added' so callers can short-circuit. Any other state (pending or
    error_*) resets to 'pending' so the mesh_bot worker picks it up
    again on its next poll. Returns the resulting status.

    CIV-99: `created_at` is stamped via wall_now read inside the
    BEGIN IMMEDIATE, same invariant as upsert_status. Production
    callers MUST NOT pass `_ts_for_test`.
    """
    pubkey = pubkey.lower()
    conn = _connect(cfg)
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            existing = conn.execute(
                "SELECT status FROM contacts WHERE pubkey=?", (pubkey,),
            ).fetchone()
            if existing is not None and existing["status"] == "added":
                conn.execute("COMMIT")
                if log:
                    log.info(
                        "contacts:short_circuit pubkey=%s already=added",
                        pubkey[:12],
                    )
                return "added"
            now_ts = (
                int(_ts_for_test)
                if _ts_for_test is not None
                else _wall_ts_in_txn(conn)
            )
            conn.execute(
                """
                INSERT INTO contacts(pubkey, status, error_detail, created_at)
                VALUES (?, 'pending', NULL, ?)
                ON CONFLICT(pubkey) DO UPDATE SET
                    status='pending',
                    error_detail=NULL
                """,
                (pubkey, now_ts),
            )
            conn.execute("COMMIT")
            if log:
                log.info("contacts:queued pubkey=%s", pubkey[:12])
            return "pending"
        except:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.OperationalError:
                pass
            raise
    finally:
        conn.close()


def get_contact_by_pubkey(
    cfg: DBConfig,
    *,
    pubkey: str,
    log=None,
) -> Optional[dict[str, Any]]:
    """Read a single contact row by exact pubkey match (case-insensitive)."""
    pubkey = pubkey.lower()
    conn = _connect(cfg)
    try:
        row = conn.execute(
            "SELECT pubkey, status, error_detail, pinned, created_at, last_seen "
            "FROM contacts WHERE pubkey=?",
            (pubkey,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_pending_contacts(
    cfg: DBConfig,
    *,
    limit: int = 16,
    log=None,
) -> list[dict[str, Any]]:
    """Return pending contact-add requests, oldest first. Used by the
    mesh_bot contact-registration worker."""
    conn = _connect(cfg)
    try:
        rows = conn.execute(
            "SELECT pubkey, created_at FROM contacts "
            "WHERE status='pending' ORDER BY created_at ASC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def mark_contact_added(
    cfg: DBConfig,
    *,
    pubkey: str,
    log=None,
) -> None:
    """Worker writes this after firmware add_contact returned OK."""
    pubkey = pubkey.lower()
    conn = _connect(cfg)
    try:
        conn.execute(
            "UPDATE contacts SET status='added', error_detail=NULL "
            "WHERE pubkey=?",
            (pubkey,),
        )
        if log:
            log.info("contacts:added pubkey=%s", pubkey[:12])
    finally:
        conn.close()


def mark_contact_error(
    cfg: DBConfig,
    *,
    pubkey: str,
    status: str,
    detail: Optional[str] = None,
    log=None,
) -> None:
    """Worker writes this after firmware add_contact returned ERROR.
    `status` must be 'error_table_full' or 'error_other'."""
    pubkey = pubkey.lower()
    if status not in ("error_table_full", "error_other"):
        raise ValueError(f"invalid contact error status: {status!r}")
    conn = _connect(cfg)
    try:
        conn.execute(
            "UPDATE contacts SET status=?, error_detail=? WHERE pubkey=?",
            (status, detail, pubkey),
        )
        if log:
            log.info(
                "contacts:error pubkey=%s status=%s detail=%s",
                pubkey[:12], status, _sanitize_for_log(detail or ""),
            )
    finally:
        conn.close()


def get_contacts_for_eviction(
    cfg: DBConfig,
    *,
    log=None,
) -> list[dict[str, Any]]:
    """Snapshot of every contact row, for mesh_bot._evict_one_contact.

    Returns every row regardless of status. The eviction helper needs
    all statuses for the firmware-vs-DB set diff: a 'pending' or
    'error_table_full' row excludes its pubkey from tier 3 even though
    we wouldn't pick it for tier 2 (only status='added' AND pinned=0
    is tier 2 territory). Filtering to 'added' here would leak races
    into tier 3 — see _evict_one_contact for the signpost comment.
    """
    conn = _connect(cfg)
    try:
        rows = conn.execute(
            "SELECT pubkey, status, pinned, last_seen FROM contacts"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def mark_contact_evicted(
    cfg: DBConfig,
    *,
    pubkey: str,
    log=None,
    _ts_for_test: Optional[int] = None,
) -> None:
    """Worker writes this after firmware remove_contact returned OK for
    a tier-2 LRU eviction. Status flips to 'evicted'; error_detail
    records the wall-clock moment so an operator can correlate with
    incident timelines.

    CIV-99: ts via wall_now read inside BEGIN IMMEDIATE."""
    pubkey = pubkey.lower()
    conn = _connect(cfg)
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            now_ts = (
                int(_ts_for_test)
                if _ts_for_test is not None
                else _wall_ts_in_txn(conn)
            )
            conn.execute(
                "UPDATE contacts SET status='evicted', error_detail=? "
                "WHERE pubkey=?",
                (f"LRU eviction at {now_ts}", pubkey),
            )
            conn.execute("COMMIT")
            if log:
                log.info(
                    "contacts:evicted_db pubkey=%s ts=%d",
                    pubkey[:12], now_ts,
                )
        except:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.OperationalError:
                pass
            raise
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Admin contact CLI helpers (`civicmesh contact list/pin/unpin/remove`)
# ---------------------------------------------------------------------------

class ContactLookupError(ValueError):
    """Raised by resolve_contact_pubkey when the query is malformed,
    matches no row, or matches multiple rows. Carries an `exit_code`
    so the CLI dispatcher can map to a stable exit value."""

    def __init__(self, msg: str, *, exit_code: int = 1):
        super().__init__(msg)
        self.exit_code = exit_code


def resolve_contact_pubkey(
    cfg: DBConfig,
    *,
    query: str,
    log=None,
) -> str:
    """Resolve a CLI-supplied pubkey or prefix to a full 64-char pubkey.

    Accepts the full 64-hex key or a prefix of 12..63 hex chars (12 is
    the smallest accepted because that's what CONTACT_MSG_RECV
    delivers — operators reading mesh_bot logs will type that length).
    Shorter prefixes are refused to keep accidental ambiguous matches
    out of admin commands.

    Raises ContactLookupError on malformed input, no match, or
    multiple matches. Searches across ALL statuses — operators should
    be able to pin/unpin/remove an 'evicted' or 'error_*' row, not
    just 'added'.
    """
    if not query:
        raise ContactLookupError("contact: pubkey is required")
    q = query.strip().lower()
    if not all(c in "0123456789abcdef" for c in q):
        raise ContactLookupError(
            f"contact: {query!r} is not hex; expected 12-64 hex chars",
        )
    if len(q) < 12 or len(q) > 64:
        raise ContactLookupError(
            f"contact: {query!r} is {len(q)} chars; need a 64-char pubkey "
            "or a 12-63 char prefix",
        )
    conn = _connect(cfg)
    try:
        if len(q) == 64:
            row = conn.execute(
                "SELECT pubkey FROM contacts WHERE pubkey=?", (q,),
            ).fetchone()
            if row is None:
                raise ContactLookupError(
                    f"contact: no contact with pubkey {q[:12]}...",
                )
            return row["pubkey"]
        rows = conn.execute(
            "SELECT pubkey FROM contacts WHERE pubkey LIKE ? LIMIT 2",
            (q + "%",),
        ).fetchall()
        if not rows:
            raise ContactLookupError(
                f"contact: no contact matches prefix {q!r}",
            )
        if len(rows) > 1:
            raise ContactLookupError(
                f"contact: prefix {q!r} matches multiple contacts; "
                "use more hex chars or the full pubkey",
            )
        return rows[0]["pubkey"]
    finally:
        conn.close()


def list_contacts(
    cfg: DBConfig,
    *,
    status: Optional[str] = None,
    pinned: Optional[bool] = None,
    log=None,
) -> list[dict[str, Any]]:
    """Return contact rows for the admin CLI's `contact list`.

    Filters are optional and combine with AND. Ordered by created_at
    DESC so the most-recently-registered show first — matches what an
    operator triaging a fresh walk-up wants to see at the top.
    """
    clauses = []
    params: list[Any] = []
    if status is not None:
        clauses.append("status=?")
        params.append(status)
    if pinned is not None:
        clauses.append("pinned=?")
        params.append(1 if pinned else 0)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    conn = _connect(cfg)
    try:
        rows = conn.execute(
            "SELECT pubkey, status, error_detail, pinned, created_at, last_seen "
            f"FROM contacts {where} ORDER BY created_at DESC",
            params,
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def set_contact_pinned(
    cfg: DBConfig,
    *,
    pubkey: str,
    pinned: bool,
    log=None,
) -> int:
    """Flip the pinned flag on a contact. Returns the number of rows
    touched (1 on success, 0 if the pubkey was deleted between the
    CLI's resolve step and this write — rare, but handled cleanly)."""
    pubkey = pubkey.lower()
    conn = _connect(cfg)
    try:
        cur = conn.execute(
            "UPDATE contacts SET pinned=? WHERE pubkey=?",
            (1 if pinned else 0, pubkey),
        )
        if log:
            log.info(
                "contacts:set_pinned pubkey=%s pinned=%s rows=%d",
                pubkey[:12], pinned, cur.rowcount,
            )
        return cur.rowcount
    finally:
        conn.close()


def delete_contact(
    cfg: DBConfig,
    *,
    pubkey: str,
    log=None,
) -> int:
    """Delete a contact row outright. The firmware contact (if still
    present) is NOT touched — once the row is gone the pubkey will be
    treated as tier 3 disposable by mesh_bot._evict_one_contact and
    reclaimed on the next ERR_CODE_TABLE_FULL. Returns the number of
    rows deleted (1 on success, 0 on miss)."""
    pubkey = pubkey.lower()
    conn = _connect(cfg)
    try:
        cur = conn.execute(
            "DELETE FROM contacts WHERE pubkey=?",
            (pubkey,),
        )
        if log:
            log.info(
                "contacts:deleted pubkey=%s rows=%d",
                pubkey[:12], cur.rowcount,
            )
        return cur.rowcount
    finally:
        conn.close()


def get_contact_by_pubkey_prefix(
    cfg: DBConfig,
    *,
    pubkey_prefix: str,
    log=None,
) -> Optional[dict[str, Any]]:
    """Resolve a contact by the 12-hex-char (6-byte) prefix that
    CONTACT_MSG_RECV carries — the firmware sends only the prefix in DM
    events, not the full 64-char pubkey. Returns the first matching row
    with status='added' (i.e., the user has completed the registration
    flow and has not been evicted) or None if no match. Rows in
    'pending', 'evicted', or any 'error_*' state are intentionally
    excluded: pending and error rows aren't in the firmware contact
    table so the DM never arrived; evicted rows lost their slot and
    must re-register before they're heard again. Prefix collisions are
    theoretically possible but astronomically unlikely at 6 bytes for a
    single-node contact table sized in the hundreds."""
    pubkey_prefix = pubkey_prefix.lower()
    conn = _connect(cfg)
    try:
        row = conn.execute(
            "SELECT pubkey, status, error_detail, pinned, created_at, last_seen "
            "FROM contacts WHERE pubkey LIKE ? AND status='added' "
            "ORDER BY created_at ASC LIMIT 1",
            (pubkey_prefix + "%",),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def touch_contact_last_seen(
    cfg: DBConfig,
    *,
    pubkey: str,
    log=None,
    _ts_for_test: Optional[int] = None,
) -> None:
    """Update last_seen for a contact on inbound DM. pubkey is the full
    64-char key, not a prefix — caller (the DM handler) resolves the
    prefix to a full pubkey first via get_contact_by_pubkey_prefix.

    CIV-99: ts via wall_now read inside BEGIN IMMEDIATE."""
    pubkey = pubkey.lower()
    conn = _connect(cfg)
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            now_ts = (
                int(_ts_for_test)
                if _ts_for_test is not None
                else _wall_ts_in_txn(conn)
            )
            conn.execute(
                "UPDATE contacts SET last_seen=? WHERE pubkey=?",
                (now_ts, pubkey),
            )
            conn.execute("COMMIT")
        except:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.OperationalError:
                pass
            raise
    finally:
        conn.close()


def compute_dm_stats(cfg: DBConfig, now_ts: int, log=None) -> dict[str, Any]:
    """Compact stat snapshot for the DM `stats` reply.

    Reads the latest telemetry_samples row for system metrics (uptime,
    CPU temp, 1-min load, disk free/total) and the latest radio_samples row
    for companion-radio metrics (RSSI, SNR, TX queue, noise floor, radio
    uptime); counts messages, distinct sessions, and RTS resets over
    1h/24h/7d windows. Returns a flat dict so the reply formatter can
    iterate it without surprises.

    Cheaper than compute_stats() (the /api/stats source): three windowed
    SELECTs per window + two LIMIT 1 reads for the latest samples, versus
    the nested system telemetry build plus its full series queries.
    """
    conn = _connect(cfg)
    try:
        latest = conn.execute(
            "SELECT uptime_s, cpu_temp_c, load_1m, disk_free_kb, disk_total_kb "
            "FROM telemetry_samples ORDER BY ts DESC LIMIT 1"
        ).fetchone()
        latest_row = dict(latest) if latest else {}

        radio = conn.execute(
            "SELECT last_rssi, last_snr, tx_queue_len, noise_floor, radio_uptime_s "
            "FROM radio_samples ORDER BY ts DESC LIMIT 1"
        ).fetchone()
        radio_row = dict(radio) if radio else {}

        power = conn.execute(
            "SELECT soc, voltage_mv, current_ma, power_w "
            "FROM power_samples ORDER BY ts DESC LIMIT 1"
        ).fetchone()
        power_row = dict(power) if power else {}

        windows = [("1h", 3600), ("24h", 86400), ("7d", 604800)]
        msgs_sent: dict[str, int] = {}
        wifi_sessions: dict[str, int] = {}
        rts_resets: dict[str, int] = {}
        for label, window in windows:
            cutoff = now_ts - window
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM messages WHERE ts >= ?",
                (cutoff,),
            ).fetchone()
            msgs_sent[label] = row["n"] if row else 0
            row = conn.execute(
                "SELECT COUNT(DISTINCT session_id) AS n FROM sessions "
                "WHERE last_seen_ts >= ?",
                (cutoff,),
            ).fetchone()
            wifi_sessions[label] = row["n"] if row else 0
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM telemetry_events "
                "WHERE kind = 'rts_pulse' AND ts >= ?",
                (cutoff,),
            ).fetchone()
            rts_resets[label] = row["n"] if row else 0

        return {
            "uptime_s": latest_row.get("uptime_s"),
            "cpu_temp_c": latest_row.get("cpu_temp_c"),
            "load_1m": latest_row.get("load_1m"),
            "disk_free_kb": latest_row.get("disk_free_kb"),
            "disk_total_kb": latest_row.get("disk_total_kb"),
            "radio": {
                "last_rssi": radio_row.get("last_rssi"),
                "last_snr": radio_row.get("last_snr"),
                "tx_queue_len": radio_row.get("tx_queue_len"),
                "noise_floor": radio_row.get("noise_floor"),
                "uptime_s": radio_row.get("radio_uptime_s"),
            },
            "power": {
                "soc": power_row.get("soc"),
                "voltage_mv": power_row.get("voltage_mv"),
                "current_ma": power_row.get("current_ma"),
                "power_w": power_row.get("power_w"),
            },
            "rts_resets": rts_resets,
            "msgs_sent": msgs_sent,
            "wifi_sessions": wifi_sessions,
        }
    finally:
        conn.close()


def queue_outbox(
    cfg: DBConfig,
    *,
    ts: int,
    channel: str,
    sender: str,
    content: str,
    session_id: str,
    fingerprint: Optional[str] = None,
    log=None,
) -> int:
    conn = _connect(cfg)
    try:
        if log:
            log.debug("db:queue_outbox channel=%s sender=%s session=%s len=%d", channel, sender, session_id, len(content))
        cur = conn.execute(
            "INSERT INTO outbox (ts, channel, sender, content, session_id, fingerprint, sent) VALUES (?, ?, ?, ?, ?, ?, 0)",
            (ts, channel, sender, content, session_id, fingerprint),
        )
        return int(cur.lastrowid)
    finally:
        conn.close()


@_retry_on_locked()
def queue_outbox_and_message(
    cfg: DBConfig,
    *,
    channel: str,
    sender: str,
    content: str,
    session_id: str,
    fingerprint: Optional[str] = None,
    max_queue_depth: int,
    log=None,
    _ts_for_test: Optional[int] = None,
) -> Optional[tuple[int, int]]:
    """Atomically create an outbox row and its linked messages row.

    Uses an explicit BEGIN IMMEDIATE so (a) mesh_bot cannot see the
    outbox row before the messages row exists — prevents the race
    where update_message_status matches 0 rows — and (b) ts is read
    from raw `time.time()` and `clock_state.offset_seconds` UNDER the
    same write lock the INSERTs hold, so an admin-command BEGIN
    EXCLUSIVE serializes correctly (see the centralized-wall-writer
    section header above and docs/clock_consensus.md).

    Refuses the insert and returns None if the queued depth is already
    at max_queue_depth (the relay-wide outbox cap, distinct from the
    per-session post quota). The depth check + INSERT run inside a
    single BEGIN IMMEDIATE transaction so two ThreadingHTTPServer
    workers cannot both pass the cap check concurrently.

    `_ts_for_test` is a fixture-only escape hatch for tests that need
    deterministic timestamps; production callers must never pass it.
    """
    conn = _connect(cfg)
    try:
        if log:
            log.debug("db:queue_outbox_and_message channel=%s sender=%s session=%s len=%d", channel, sender, session_id, len(content))
        # BEGIN IMMEDIATE acquires the writer lock at BEGIN time so the SELECT
        # below sees the post-commit state of any peer transaction. With
        # plain BEGIN (deferred), two /api/post threads can both take read
        # snapshots at depth=N-1, both pass the cap check, both INSERT —
        # racing the cap by N. _retry_on_locked covers SQLITE_BUSY contention
        # but is not load-bearing for cap correctness here.
        conn.execute("BEGIN IMMEDIATE")
        depth = conn.execute(
            "SELECT COUNT(*) FROM outbox WHERE status='queued'"
        ).fetchone()[0]
        if depth >= max_queue_depth:
            conn.execute("ROLLBACK")
            if log:
                log.warning(
                    "db:outbox_full depth=%d cap=%d dropping channel=%s session=%s",
                    depth, max_queue_depth, channel, session_id,
                )
            return None
        ts = _ts_for_test if _ts_for_test is not None else _wall_ts_in_txn(conn)
        cur = conn.execute(
            "INSERT INTO outbox (ts, channel, sender, content, session_id, fingerprint, sent) VALUES (?,?,?,?,?,?,0)",
            (ts, channel, sender, content, session_id, fingerprint),
        )
        oid = int(cur.lastrowid)
        cur2 = conn.execute(
            "INSERT INTO messages (ts, channel, sender, content, source, session_id, fingerprint, outbox_id, status) VALUES (?,?,?,?,?,?,?,?,?)",
            (ts, channel, sender, content, "wifi", session_id, fingerprint, oid, "queued"),
        )
        mid = int(cur2.lastrowid)
        conn.execute("COMMIT")
        return oid, mid
    except:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.OperationalError:
            pass
        raise
    finally:
        conn.close()


def get_pending_outbox(cfg: DBConfig, *, limit: int, log=None) -> list[dict[str, Any]]:
    conn = _connect(cfg)
    try:
        if log:
            log.debug("db:get_pending_outbox limit=%d", limit)
        rows = conn.execute(
            "SELECT * FROM outbox WHERE status='queued' ORDER BY ts ASC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_outbox_message(cfg: DBConfig, *, outbox_id: int, log=None) -> Optional[dict[str, Any]]:
    conn = _connect(cfg)
    try:
        if log:
            log.debug("db:get_outbox_message id=%d", outbox_id)
        row = conn.execute(
            "SELECT * FROM outbox WHERE id=?",
            (outbox_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_pending_outbox_filtered(
    cfg: DBConfig,
    *,
    channel: Optional[str],
    limit: int = 20,
    log=None,
) -> list[dict[str, Any]]:
    conn = _connect(cfg)
    try:
        if log:
            log.debug("db:get_pending_outbox_filtered channel=%s limit=%d", channel, limit)
        if channel:
            rows = conn.execute(
                "SELECT * FROM outbox WHERE status='queued' AND channel=? ORDER BY ts ASC LIMIT ?",
                (channel, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM outbox WHERE status='queued' ORDER BY ts ASC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def cancel_outbox_message(cfg: DBConfig, *, outbox_id: int, log=None) -> bool:
    conn = _connect(cfg)
    try:
        if log:
            log.debug("db:cancel_outbox_message id=%d", outbox_id)
        cur = conn.execute(
            "DELETE FROM outbox WHERE id=? AND status='queued'",
            (outbox_id,),
        )
        return cur.rowcount > 0
    finally:
        conn.close()


def clear_pending_outbox(cfg: DBConfig, *, log=None) -> int:
    conn = _connect(cfg)
    try:
        if log:
            log.debug("db:clear_pending_outbox")
        cur = conn.execute("DELETE FROM outbox WHERE status='queued'")
        return cur.rowcount
    finally:
        conn.close()


def get_pending_outbox_for_session(
    cfg: DBConfig,
    *,
    session_id: str,
    channel: str,
    limit: int = 20,
    log=None,
) -> list[dict[str, Any]]:
    conn = _connect(cfg)
    try:
        if log:
            log.debug("db:get_pending_outbox_for_session session=%s channel=%s limit=%d", session_id, channel, limit)
        rows = conn.execute(
            "SELECT * FROM outbox WHERE status='queued' AND session_id=? AND channel=? ORDER BY ts DESC LIMIT ?",
            (session_id, channel, limit),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def mark_outbox_sent(cfg: DBConfig, *, outbox_ids: list[int], log=None) -> None:
    if not outbox_ids:
        return
    conn = _connect(cfg)
    try:
        if log:
            log.warning("db:mark_outbox_sent UNEXPECTED count=%d ids=%s", len(outbox_ids), outbox_ids)
        q = "UPDATE outbox SET sent=1, status='sent' WHERE id IN (%s)" % ",".join("?" for _ in outbox_ids)
        conn.execute(q, outbox_ids)
    finally:
        conn.close()


def update_outbox_sender_ts(cfg: DBConfig, *, outbox_id: int, sender_ts: int, log=None) -> None:
    """Persist sender_ts on first send attempt so echo-confirmed retries
    can reference the original timestamp. The IS NULL guard ensures
    retries don't overwrite the timestamp from the first attempt.

    Lower-level than compute_and_persist_sender_ts; production callers
    should prefer the latter (see CIV-99 docstring there)."""
    conn = _connect(cfg)
    try:
        conn.execute(
            "UPDATE outbox SET sender_ts = ? WHERE id = ? AND sender_ts IS NULL",
            (int(sender_ts), int(outbox_id)),
        )
    finally:
        conn.close()


@_retry_on_locked()
def compute_and_persist_sender_ts(cfg: DBConfig, *, outbox_id: int, log=None) -> int:
    """Capture int(time.time()) as the outgoing packet's sender_ts and persist.

    CIV-99: this is the ONE timestamp helper that intentionally
    deviates from the centralized-wall-writer discipline. It uses
    RAW system time and does NOT wrap the UPDATE in BEGIN IMMEDIATE.
    Reasons:

      1. The MeshCore firmware stamps each outgoing packet with
         time(NULL) — the unmodified Pi system clock value — so for
         our stored sender_ts to match the firmware's stamp (which
         the echo carries back via
         RX_LOG_DATA.payload.sender_timestamp), we MUST also use raw
         time.time(), not the corrected wall_now. Echo-match
         tolerance (±1s, see outbox_echoes.py) absorbs the small
         gap between this read and the firmware's own read.

      2. sender_ts is NEVER used for human display, row ordering, or
         retention-cutoff comparisons. It is purely an echo-match
         key. So the "raw, not wall" choice does not pollute the
         wall-corrected `ts` columns we use everywhere else.

      3. No BEGIN IMMEDIATE because (a) the value being stored is
         raw time, not derived from offset_seconds, so there is no
         actor-vs-actor race with the admin command's BEGIN
         EXCLUSIVE to defend against, and (b) the only failure mode
         an admin step can introduce is a missed echo match for a
         packet already in flight — at worst one retransmit, no
         data integrity issue.

    DO NOT "fix" this into wall_now under BEGIN IMMEDIATE — the
    docstring above, docs/clock_consensus.md § "sender_ts is the
    exception," and the unit-test note in tests/test_clock.py
    all reflect the deliberate choice.

    Returns the captured sender_ts so the caller can hand it to the
    firmware send call. IS NULL guard preserves the first-send value
    across retries.
    """
    sender_ts = int(time.time())
    conn = _connect(cfg)
    try:
        if log:
            log.debug("db:compute_and_persist_sender_ts id=%d sender_ts=%d", outbox_id, sender_ts)
        conn.execute(
            "UPDATE outbox SET sender_ts = ? WHERE id = ? AND sender_ts IS NULL",
            (sender_ts, int(outbox_id)),
        )
    finally:
        conn.close()
    return sender_ts


@_retry_on_locked()
def record_outbox_send(cfg: DBConfig, *, outbox_id: int, sender_ts: int, log=None) -> None:
    """Mark a single outbox row as sent and update the linked messages row
    atomically.  Uses an explicit transaction so a process kill between
    the two UPDATEs can't leave them out of sync.

    `sender_ts` is captured by the caller as `int(time.time())` around
    the moment of the `send_chan_msg` call. It's used as the matching
    key tie-breaker when echoes come back via RX_LOG_DATA — see
    docs/heard_count_design.md.
    """
    conn = _connect(cfg)
    try:
        if log:
            log.info("db:record_outbox_send id=%d sender_ts=%d", outbox_id, sender_ts)
        conn.execute("BEGIN")
        conn.execute(
            "UPDATE outbox SET sent=1, status='sent', sender_ts=? WHERE id=?",
            (int(sender_ts), int(outbox_id)),
        )
        cur = conn.execute(
            "UPDATE messages SET status='sent' WHERE outbox_id=?",
            (int(outbox_id),),
        )
        if log:
            log.info("db:record_outbox_send id=%d messages_updated=%d", outbox_id, cur.rowcount)
        conn.execute("COMMIT")
    except:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


@_retry_on_locked()
def increment_heard(
    cfg: DBConfig,
    *,
    outbox_id: int,
    path_len: Optional[int],
    snr: Optional[float],
    log=None,
    _ts_for_test: Optional[int] = None,
) -> None:
    """Record one observed echo against an outbox row.

    CIV-99: first_heard_ts / last_heard_ts stamped from wall_now read
    inside this BEGIN IMMEDIATE. Production callers MUST NOT pass
    `_ts_for_test`.

    Increments heard_count by 1; updates first_heard_ts (if NULL),
    last_heard_ts, min_path_len (if smaller or NULL), best_snr (if
    larger or NULL). Idempotent in the SQL sense: if outbox_id no
    longer exists, the UPDATE silently affects 0 rows.
    """
    conn = _connect(cfg)
    try:
        if log:
            log.debug(
                "db:increment_heard outbox_id=%d path_len=%s snr=%s",
                outbox_id, path_len, snr,
            )
        conn.execute("BEGIN IMMEDIATE")
        try:
            ts = int(_ts_for_test) if _ts_for_test is not None else _wall_ts_in_txn(conn)
            conn.execute(
                """
                UPDATE outbox
                SET
                  heard_count    = heard_count + 1,
                  first_heard_ts = COALESCE(first_heard_ts, ?),
                  last_heard_ts  = ?,
                  min_path_len   = CASE
                      WHEN ? IS NULL THEN min_path_len
                      WHEN min_path_len IS NULL OR ? < min_path_len THEN ?
                      ELSE min_path_len
                  END,
                  best_snr       = CASE
                      WHEN ? IS NULL THEN best_snr
                      WHEN best_snr IS NULL OR ? > best_snr THEN ?
                      ELSE best_snr
                  END
                WHERE id = ?
                """,
                (
                    int(ts), int(ts),
                    path_len, path_len, path_len,
                    snr, snr, snr,
                    int(outbox_id),
                ),
            )
            conn.execute("COMMIT")
        except:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.OperationalError:
                pass
            raise
    finally:
        conn.close()


def mark_outbox_failed(cfg: DBConfig, *, outbox_ids: list[int], log=None) -> None:
    """Mark outbox rows as permanently failed and update linked messages
    atomically."""
    if not outbox_ids:
        return
    conn = _connect(cfg)
    try:
        if log:
            log.debug("db:mark_outbox_failed count=%d", len(outbox_ids))
        conn.execute("BEGIN")
        placeholders = ",".join("?" for _ in outbox_ids)
        conn.execute(
            "UPDATE outbox SET status='failed' WHERE id IN (%s)" % placeholders,
            outbox_ids,
        )
        conn.execute(
            "UPDATE messages SET status='failed' WHERE outbox_id IN (%s)" % placeholders,
            outbox_ids,
        )
        conn.execute("COMMIT")
        for oid in outbox_ids:
            if log:
                log.warning("outbox:marked_failed id=%d", oid)
    except:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def increment_outbox_retry(cfg: DBConfig, *, outbox_id: int, log=None) -> int:
    """Increment retry_count for an outbox row. Returns new count."""
    conn = _connect(cfg)
    try:
        if log:
            log.debug("db:increment_outbox_retry id=%d", outbox_id)
        conn.execute(
            "UPDATE outbox SET retry_count = retry_count + 1 WHERE id = ?",
            (outbox_id,),
        )
        row = conn.execute(
            "SELECT retry_count FROM outbox WHERE id = ?",
            (outbox_id,),
        ).fetchone()
        count = row["retry_count"] if row else 0
        return count
    finally:
        conn.close()


def create_or_update_session(
    cfg: DBConfig,
    *,
    session_id: str,
    name: str,
    location: str,
    mac_address: Optional[str],
    fingerprint: Optional[str] = None,
    log=None,
    _ts_for_test: Optional[int] = None,
) -> None:
    """Upsert a row in `sessions`.

    CIV-99: `created_ts` is stamped as wall_now read inside the BEGIN
    IMMEDIATE write txn. See the centralized-wall-writer section header
    above. Production callers MUST NOT pass `_ts_for_test`.
    """
    conn = _connect(cfg)
    try:
        if log:
            log.debug("db:create_or_update_session session=%s mac=%s", session_id, mac_address or "")
        conn.execute("BEGIN IMMEDIATE")
        try:
            now_ts = int(_ts_for_test) if _ts_for_test is not None else _wall_ts_in_txn(conn)
            conn.execute(
                """
                INSERT INTO sessions(session_id, name, location, mac_address, fingerprint, created_ts, last_post_ts, post_count_hour)
                VALUES (?, ?, ?, ?, ?, ?, NULL, 0)
                ON CONFLICT(session_id) DO UPDATE SET
                  name=excluded.name,
                  location=excluded.location,
                  mac_address=excluded.mac_address,
                  fingerprint=excluded.fingerprint
                """,
                (session_id, name, location, mac_address, fingerprint, now_ts),
            )
            conn.execute("COMMIT")
        except:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.OperationalError:
                pass
            raise
    finally:
        conn.close()


def update_session_fingerprint(cfg: DBConfig, *, session_id: str, fingerprint: str, log=None) -> None:
    conn = _connect(cfg)
    try:
        if log:
            log.debug("db:update_session_fingerprint session=%s", session_id)
        conn.execute("UPDATE sessions SET fingerprint=? WHERE session_id=?", (fingerprint, session_id))
    finally:
        conn.close()


def get_session(cfg: DBConfig, *, session_id: str, log=None) -> Optional[dict[str, Any]]:
    conn = _connect(cfg)
    try:
        if log:
            log.debug("db:get_session session=%s", session_id)
        row = conn.execute("SELECT * FROM sessions WHERE session_id=?", (session_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def record_post_for_session(cfg: DBConfig, *, session_id: str, log=None, _ts_for_test: Optional[int] = None) -> None:
    """Increment a session's hourly post counter and stamp last_post_ts.

    CIV-99: `last_post_ts` is wall_now read inside the BEGIN IMMEDIATE
    write txn. Production callers MUST NOT pass `_ts_for_test`.
    """
    conn = _connect(cfg)
    try:
        if log:
            log.debug("db:record_post_for_session session=%s", session_id)
        conn.execute("BEGIN IMMEDIATE")
        try:
            now_ts = int(_ts_for_test) if _ts_for_test is not None else _wall_ts_in_txn(conn)
            conn.execute(
                """
                UPDATE sessions
                SET last_post_ts=?,
                    post_count_hour=COALESCE(post_count_hour,0)+1
                WHERE session_id=?
                """,
                (now_ts, session_id),
            )
            conn.execute("COMMIT")
        except:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.OperationalError:
                pass
            raise
    finally:
        conn.close()


def posts_in_last_window(cfg: DBConfig, *, session_id: str, window_sec: int = 3600, now_ts: Optional[int] = None, log=None) -> int:
    """
    Rolling window approximation: uses last_post_ts + counter for the window.
    For a precise rolling window, we would store per-post timestamps; this keeps schema minimal.
    """
    now_ts = int(now_ts or time.time())
    sess = get_session(cfg, session_id=session_id, log=log)
    if not sess:
        return 0
    last_ts = sess.get("last_post_ts")
    count = int(sess.get("post_count_hour") or 0)
    if not last_ts:
        return 0
    if now_ts - int(last_ts) > window_sec:
        # window expired; reset in DB
        conn = _connect(cfg)
        try:
            if log:
                log.debug("db:rate_limit_reset session=%s", session_id)
            conn.execute("UPDATE sessions SET post_count_hour=0 WHERE session_id=?", (session_id,))
        finally:
            conn.close()
        return 0
    return count


def update_vote(cfg: DBConfig, *, message_id: int, session_id: str, vote_type: int, log=None, _ts_for_test: Optional[int] = None) -> None:
    """
    vote_type: 1=upvote, -1=downvote, 0=remove

    CIV-99: votes.ts stamped from wall_now inside this BEGIN IMMEDIATE.
    Production callers MUST NOT pass `_ts_for_test`.
    """
    if vote_type not in (-1, 0, 1):
        raise ValueError("vote_type must be -1, 0, or 1")
    conn = _connect(cfg)
    try:
        if log:
            log.debug("db:update_vote msg=%d session=%s vote=%d", message_id, session_id, vote_type)
        conn.execute("BEGIN IMMEDIATE")
        try:
            ts = int(_ts_for_test) if _ts_for_test is not None else _wall_ts_in_txn(conn)
            if vote_type == 0:
                conn.execute("DELETE FROM votes WHERE message_id=? AND session_id=?", (message_id, session_id))
            else:
                conn.execute(
                    """
                    INSERT INTO votes(message_id, session_id, vote_type, ts)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(message_id, session_id) DO UPDATE SET vote_type=excluded.vote_type, ts=excluded.ts
                    """,
                    (message_id, session_id, vote_type, ts),
                )
            _recount_votes(conn, message_id)
            conn.execute("COMMIT")
        except:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.OperationalError:
                pass
            raise
    finally:
        conn.close()


def _recount_votes(conn: sqlite3.Connection, message_id: int) -> None:
    up = conn.execute("SELECT COUNT(*) AS c FROM votes WHERE message_id=? AND vote_type=1", (message_id,)).fetchone()["c"]
    down = conn.execute(
        "SELECT COUNT(*) AS c FROM votes WHERE message_id=? AND vote_type=-1", (message_id,)
    ).fetchone()["c"]
    conn.execute("UPDATE messages SET upvotes=?, downvotes=? WHERE id=?", (int(up), int(down), message_id))


def get_vote_counts(cfg: DBConfig, *, message_id: int, log=None) -> tuple[int, int]:
    conn = _connect(cfg)
    try:
        if log:
            log.debug("db:get_vote_counts msg=%d", message_id)
        row = conn.execute("SELECT upvotes, downvotes FROM messages WHERE id=?", (message_id,)).fetchone()
        if not row:
            return 0, 0
        return int(row["upvotes"] or 0), int(row["downvotes"] or 0)
    finally:
        conn.close()


def get_message(cfg: DBConfig, *, message_id: int, log=None) -> Optional[dict[str, Any]]:
    conn = _connect(cfg)
    try:
        if log:
            log.debug("db:get_message id=%d", message_id)
        row = conn.execute("SELECT * FROM messages WHERE id=?", (message_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_user_vote(cfg: DBConfig, *, message_id: int, session_id: str, log=None) -> int:
    conn = _connect(cfg)
    try:
        if log:
            log.debug("db:get_user_vote msg=%d session=%s", message_id, session_id)
        row = conn.execute(
            "SELECT vote_type FROM votes WHERE message_id=? AND session_id=?",
            (message_id, session_id),
        ).fetchone()
        return int(row["vote_type"]) if row else 0
    finally:
        conn.close()


def pin_message(cfg: DBConfig, *, message_id: int, pin_order: Optional[int] = None, log=None) -> None:
    conn = _connect(cfg)
    try:
        if pin_order is None:
            row = conn.execute("SELECT COALESCE(MAX(pin_order),0) AS m FROM messages WHERE pinned=1").fetchone()
            pin_order = int(row["m"] or 0) + 1
        if log:
            log.debug("db:pin_message msg=%d order=%d", message_id, pin_order)
        conn.execute("UPDATE messages SET pinned=1, pin_order=? WHERE id=?", (pin_order, message_id))
    finally:
        conn.close()


def unpin_message(cfg: DBConfig, *, message_id: int, log=None) -> None:
    conn = _connect(cfg)
    try:
        if log:
            log.debug("db:unpin_message msg=%d", message_id)
        conn.execute("UPDATE messages SET pinned=0, pin_order=NULL WHERE id=?", (message_id,))
    finally:
        conn.close()


def cleanup_retention_bytes_per_channel(cfg: DBConfig, *, channel: str, max_bytes: int, log=None) -> int:
    """
    Deletes oldest unpinned messages until approximate total content size is under max_bytes.
    Returns number of deleted rows.
    """
    conn = _connect(cfg)
    try:
        # Approximate bytes as UTF-8 length of content plus small overhead
        row = conn.execute("SELECT COALESCE(SUM(LENGTH(content)),0) AS b FROM messages WHERE channel=?", (channel,)).fetchone()
        total = int(row["b"] or 0)
        if total <= max_bytes:
            return 0
        deleted = 0
        if log:
            log.info("retention:channel=%s total_bytes=%d max_bytes=%d", channel, total, max_bytes)
        while total > max_bytes:
            r = conn.execute(
                """
                SELECT id, LENGTH(content) AS b
                FROM messages
                WHERE channel=? AND pinned=0 AND (status IS NULL OR status != 'queued')
                ORDER BY ts ASC
                LIMIT 1
                """,
                (channel,),
            ).fetchone()
            if not r:
                break
            mid = int(r["id"])
            b = int(r["b"] or 0)
            conn.execute("DELETE FROM messages WHERE id=?", (mid,))
            # votes are not FK-enforced; clean them too
            conn.execute("DELETE FROM votes WHERE message_id=?", (mid,))
            total -= b
            deleted += 1
        if log and deleted:
            log.info("retention:deleted channel=%s count=%d", channel, deleted)
        return deleted
    finally:
        conn.close()


def search_messages(
    cfg: DBConfig,
    *,
    query: str,
    channel: Optional[str] = None,
    sender: Optional[str] = None,
    limit: int = 5,
    log=None,
) -> list[dict[str, Any]]:
    q = query.strip()
    if not q:
        return []
    conn = _connect(cfg)
    try:
        if log:
            log.debug("db:search_messages q=%s channel=%s sender=%s limit=%d", q, channel or "", sender or "", limit)
        where = ["content LIKE ?"]
        params: list[Any] = [f"%{q}%"]
        if channel:
            where.append("channel=?")
            params.append(channel)
        if sender:
            where.append("sender LIKE ?")
            params.append(f"%{sender}%")
        sql = "SELECT id, ts, channel, sender, content FROM messages WHERE " + " AND ".join(where) + " ORDER BY ts DESC LIMIT ?"
        params.append(limit)
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def insert_heard_packet(cfg: DBConfig, *, payload_type, route_type, path_len,
                        last_path_byte=None, snr=None, rssi=None, log=None,
                        _ts_for_test: Optional[int] = None):
    """Insert one row into heard_packets. CIV-99: ts inside the txn."""
    conn = _connect(cfg)
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            ts = int(_ts_for_test) if _ts_for_test is not None else _wall_ts_in_txn(conn)
            conn.execute(
                "INSERT INTO heard_packets (ts, payload_type, route_type, path_len, last_path_byte, snr, rssi) "
                "VALUES (?,?,?,?,?,?,?)",
                (ts, payload_type, route_type, path_len, last_path_byte, snr, rssi),
            )
            conn.execute("COMMIT")
        except:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.OperationalError:
                pass
            raise
    finally:
        conn.close()


TOUCH_DEBOUNCE_SEC = 30  # skip write if last_seen_ts is already within this window


def touch_session_last_seen(cfg: DBConfig, session_id, log=None, _ts_for_test: Optional[int] = None):
    """Debounced update of sessions.last_seen_ts. CIV-99: ts inside the txn."""
    conn = _connect(cfg)
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            ts = int(_ts_for_test) if _ts_for_test is not None else _wall_ts_in_txn(conn)
            conn.execute(
                "UPDATE sessions SET last_seen_ts = ? "
                "WHERE session_id = ? AND (last_seen_ts IS NULL OR last_seen_ts < ? - ?)",
                (ts, session_id, ts, TOUCH_DEBOUNCE_SEC),
            )
            conn.execute("COMMIT")
        except:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.OperationalError:
                pass
            raise
    finally:
        conn.close()


def prune_heard_packets(cfg: DBConfig, *, cutoff_ts, log=None):
    conn = _connect(cfg)
    try:
        cur = conn.execute("DELETE FROM heard_packets WHERE ts < ?", (cutoff_ts,))
        if cur.rowcount and log:
            log.info("retention:heard_packets deleted=%d", cur.rowcount)
    finally:
        conn.close()


def prune_terminal_outbox(cfg: DBConfig, *, cutoff_ts, log=None):
    """Delete sent/failed outbox rows older than cutoff. The corresponding
    messages rows retain their status independently."""
    conn = _connect(cfg)
    try:
        cur = conn.execute(
            "DELETE FROM outbox WHERE status IN ('sent', 'failed') AND ts < ?",
            (cutoff_ts,),
        )
        if cur.rowcount and log:
            log.info("retention:outbox deleted=%d", cur.rowcount)
    finally:
        conn.close()


def compute_stats(cfg: DBConfig, now_ts, log=None):
    conn = _connect(cfg)
    try:
        # -- wifi_sessions --
        wifi = {}
        for label, window in [("now", 300), ("day", 86400), ("week", 604800)]:
            row = conn.execute(
                "SELECT COUNT(DISTINCT session_id) AS n FROM sessions WHERE last_seen_ts >= ?",
                (now_ts - window,),
            ).fetchone()
            wifi[label] = row["n"] if row else 0

        # -- messages_seen histograms --
        seen = {}
        windows = [
            ("5min", 30, 10),     # 30s buckets, 10 bars
            ("hour", 300, 12),    # 5min buckets, 12 bars
            ("day", 3600, 24),    # 1hr buckets, 24 bars
            ("week", 21600, 28),  # 6hr buckets, 28 bars
        ]
        for label, bucket_s, n_bars in windows:
            window_s = bucket_s * n_bars
            cutoff = now_ts - window_s
            rows = conn.execute(
                "SELECT (ts / ?) * ? AS bucket, COUNT(*) AS n "
                "FROM heard_packets WHERE ts >= ? GROUP BY bucket ORDER BY bucket",
                (bucket_s, bucket_s, cutoff),
            ).fetchall()
            counts = {r["bucket"]: r["n"] for r in rows}
            # Align to present: most recent bar covers the current time slice
            end_bucket = (now_ts // bucket_s) * bucket_s
            start_bucket = end_bucket - (n_bars - 1) * bucket_s
            bars = [counts.get(start_bucket + i * bucket_s, 0) for i in range(n_bars)]
            seen[label] = {"bucket_s": bucket_s, "bars": bars}

        # -- messages_sent --
        sent = {}
        for label, window in [("hour", 3600), ("day", 86400), ("week", 604800)]:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM messages WHERE source = 'wifi' AND (status IS NULL OR status = 'sent') AND ts >= ?",
                (now_ts - window,),
            ).fetchone()
            sent[label] = row["n"] if row else 0

        # -- messages_failed --
        failed = {}
        for label, window in [("hour", 3600), ("day", 86400), ("week", 604800)]:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM messages "
                "WHERE source = 'wifi' AND status = 'failed' AND ts >= ?",
                (now_ts - window,),
            ).fetchone()
            failed[label] = row["n"] if row else 0

        # -- direct_repeaters --
        repeaters = {}
        for label, window in [("hour", 3600), ("day", 86400), ("week", 604800)]:
            row = conn.execute(
                "SELECT COUNT(DISTINCT last_path_byte) AS n FROM heard_packets "
                "WHERE path_len >= 1 AND ts >= ?",
                (now_ts - window,),
            ).fetchone()
            repeaters[label] = row["n"] if row else 0

        # -- radio_restarts (recovery_succeeded events) --
        restarts = {}
        for label, window in [("hour", 3600), ("day", 86400), ("week", 604800)]:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM telemetry_events "
                "WHERE kind = 'recovery_succeeded' AND ts >= ?",
                (now_ts - window,),
            ).fetchone()
            restarts[label] = row["n"] if row else 0

        # -- rts_resets (rts_pulse events: a failed health-check that hard-reset
        #    the radio via the serial RTS line). Distinct from radio_restarts,
        #    which counts only the pulses that went on to recover successfully.
        rts_resets = {}
        for label, window in [("hour", 3600), ("day", 86400), ("week", 604800)]:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM telemetry_events "
                "WHERE kind = 'rts_pulse' AND ts >= ?",
                (now_ts - window,),
            ).fetchone()
            rts_resets[label] = row["n"] if row else 0

        result = {
            "now_ts": now_ts,
            "wifi_sessions": wifi,
            "messages_seen": seen,
            "messages_sent": sent,
            "messages_failed": failed,
            "direct_repeaters": repeaters,
            "radio_restarts": restarts,
            "rts_resets": rts_resets,
        }
        result["system"] = _build_system_telemetry(cfg, now_ts, log=log)
        return result
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Telemetry helpers
# ---------------------------------------------------------------------------

def insert_telemetry_sample(
    cfg: DBConfig,
    *,
    uptime_s: Optional[int],
    load_1m: Optional[float],
    cpu_temp_c: Optional[float],
    mem_available_kb: Optional[int],
    mem_total_kb: Optional[int],
    disk_free_kb: Optional[int],
    disk_total_kb: Optional[int],
    net_rx_bytes: Optional[int],
    net_tx_bytes: Optional[int],
    outbox_depth: Optional[int],
    outbox_oldest_age_s: Optional[int],
    throttled_bitmask: Optional[int],
    log=None,
    _ts_for_test: Optional[int] = None,
) -> None:
    """Insert a telemetry sample row. CIV-99: ts stamped inside the txn.

    Production callers MUST NOT pass `_ts_for_test`.
    """
    conn = _connect(cfg)
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            ts = int(_ts_for_test) if _ts_for_test is not None else _wall_ts_in_txn(conn)
            conn.execute(
                "INSERT INTO telemetry_samples "
                "(ts, uptime_s, load_1m, cpu_temp_c, mem_available_kb, mem_total_kb, "
                "disk_free_kb, disk_total_kb, net_rx_bytes, net_tx_bytes, "
                "outbox_depth, outbox_oldest_age_s, throttled_bitmask) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (ts, uptime_s, load_1m, cpu_temp_c, mem_available_kb, mem_total_kb,
                 disk_free_kb, disk_total_kb, net_rx_bytes, net_tx_bytes,
                 outbox_depth, outbox_oldest_age_s, throttled_bitmask),
            )
            conn.execute("COMMIT")
        except:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.OperationalError:
                pass
            raise
    finally:
        conn.close()


def insert_radio_sample(
    cfg: DBConfig,
    *,
    battery_mv: Optional[int],
    radio_uptime_s: Optional[int],
    err_bitmask: Optional[int],
    tx_queue_len: Optional[int],
    noise_floor: Optional[int],
    last_rssi: Optional[int],
    last_snr: Optional[float],
    tx_air_secs: Optional[int],
    rx_air_secs: Optional[int],
    log=None,
    _ts_for_test: Optional[int] = None,
) -> None:
    """Insert a companion-radio stats sample. CIV-99: ts stamped inside the txn.

    Production callers MUST NOT pass `_ts_for_test`.
    """
    conn = _connect(cfg)
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            ts = int(_ts_for_test) if _ts_for_test is not None else _wall_ts_in_txn(conn)
            conn.execute(
                "INSERT INTO radio_samples "
                "(ts, battery_mv, radio_uptime_s, err_bitmask, tx_queue_len, "
                "noise_floor, last_rssi, last_snr, tx_air_secs, rx_air_secs) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (ts, battery_mv, radio_uptime_s, err_bitmask, tx_queue_len,
                 noise_floor, last_rssi, last_snr, tx_air_secs, rx_air_secs),
            )
            conn.execute("COMMIT")
        except:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.OperationalError:
                pass
            raise
    finally:
        conn.close()


def insert_power_sample(
    cfg: DBConfig,
    *,
    soc: Optional[float],
    voltage_mv: Optional[int],
    current_ma: Optional[int],
    power_w: Optional[float],
    log=None,
    _ts_for_test: Optional[int] = None,
) -> None:
    """Insert a Victron BMV-712 battery sample. CIV-99: ts stamped inside the txn.

    Any field may be None — the BMV reports SoC (and others) as unavailable in
    normal operation (e.g. before the shunt syncs). Production callers MUST NOT
    pass `_ts_for_test`.
    """
    conn = _connect(cfg)
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            ts = int(_ts_for_test) if _ts_for_test is not None else _wall_ts_in_txn(conn)
            conn.execute(
                "INSERT INTO power_samples (ts, soc, voltage_mv, current_ma, power_w) "
                "VALUES (?,?,?,?,?)",
                (ts, soc, voltage_mv, current_ma, power_w),
            )
            conn.execute("COMMIT")
        except:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.OperationalError:
                pass
            raise
    finally:
        conn.close()


def insert_telemetry_event(
    cfg: DBConfig,
    *,
    kind: str,
    detail: Optional[dict] = None,
    log=None,
    _ts_for_test: Optional[int] = None,
) -> None:
    """Insert a telemetry event row. CIV-99: ts stamped inside the txn.

    Production callers MUST NOT pass `_ts_for_test`.
    """
    conn = _connect(cfg)
    try:
        detail_json = json.dumps(detail) if detail is not None else None
        conn.execute("BEGIN IMMEDIATE")
        try:
            ts = int(_ts_for_test) if _ts_for_test is not None else _wall_ts_in_txn(conn)
            conn.execute(
                "INSERT INTO telemetry_events (ts, kind, detail) VALUES (?,?,?)",
                (ts, kind, detail_json),
            )
            conn.execute("COMMIT")
        except:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.OperationalError:
                pass
            raise
    finally:
        conn.close()


def get_telemetry_samples_since(
    cfg: DBConfig,
    *,
    since_ts: int,
    log=None,
) -> list[dict]:
    conn = _connect(cfg)
    try:
        rows = conn.execute(
            "SELECT * FROM telemetry_samples WHERE ts > ? ORDER BY ts",
            (since_ts,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_telemetry_events_since(
    cfg: DBConfig,
    *,
    since_ts: int,
    kinds: Optional[list[str]] = None,
    log=None,
) -> list[dict]:
    conn = _connect(cfg)
    try:
        if kinds:
            placeholders = ",".join("?" for _ in kinds)
            rows = conn.execute(
                f"SELECT ts, kind, detail FROM telemetry_events "
                f"WHERE ts > ? AND kind IN ({placeholders}) ORDER BY ts",
                (since_ts, *kinds),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT ts, kind, detail FROM telemetry_events WHERE ts > ? ORDER BY ts",
                (since_ts,),
            ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            if d.get("detail"):
                try:
                    d["detail"] = json.loads(d["detail"])
                except (json.JSONDecodeError, TypeError):
                    pass
            result.append(d)
        return result
    finally:
        conn.close()


def get_outbox_snapshot(
    cfg: DBConfig,
    *,
    now_ts: int,
    log=None,
) -> tuple[int, Optional[int]]:
    conn = _connect(cfg)
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS depth, MIN(ts) AS oldest_ts FROM outbox WHERE status = 'queued'"
        ).fetchone()
        depth = row["depth"] if row else 0
        oldest_ts = row["oldest_ts"] if row else None
        oldest_age_s = (now_ts - oldest_ts) if oldest_ts is not None else None
        return depth, oldest_age_s
    finally:
        conn.close()


def prune_telemetry(
    cfg: DBConfig,
    *,
    samples_cutoff_ts: int,
    events_cutoff_ts: int,
    log=None,
) -> None:
    conn = _connect(cfg)
    try:
        conn.execute("DELETE FROM telemetry_samples WHERE ts < ?", (samples_cutoff_ts,))
        conn.execute("DELETE FROM radio_samples WHERE ts < ?", (samples_cutoff_ts,))
        conn.execute("DELETE FROM power_samples WHERE ts < ?", (samples_cutoff_ts,))
        conn.execute("DELETE FROM telemetry_events WHERE ts < ?", (events_cutoff_ts,))
    finally:
        conn.close()


def _build_system_telemetry(cfg: DBConfig, now_ts: int, log=None) -> dict:
    """Build the system telemetry dict for compute_stats(). Single connection."""
    conn = _connect(cfg)
    try:
        # 1. 1h samples
        rows_1h = conn.execute(
            "SELECT * FROM telemetry_samples WHERE ts > ? ORDER BY ts",
            (now_ts - 3600,),
        ).fetchall()
        samples_1h = [dict(r) for r in rows_1h]

        # 2. 24h samples (subset of columns for hourly aggregation)
        rows_24h = conn.execute(
            "SELECT ts, disk_free_kb, disk_total_kb, net_rx_bytes, net_tx_bytes "
            "FROM telemetry_samples WHERE ts > ? ORDER BY ts",
            (now_ts - 86400,),
        ).fetchall()
        samples_24h = [dict(r) for r in rows_24h]

        # 3. Event counts by kind
        event_count_rows = conn.execute(
            "SELECT kind, COUNT(*) AS n FROM telemetry_events "
            "WHERE ts > ? AND kind IN ('rate_limit','mac_mismatch','http_error','throttle_change') "
            "GROUP BY kind",
            (now_ts - 86400,),
        ).fetchall()
        event_counts = {r["kind"]: r["n"] for r in event_count_rows}

        # 4. Throttle change events
        throttle_rows = conn.execute(
            "SELECT ts, detail FROM telemetry_events "
            "WHERE ts > ? AND kind = 'throttle_change' ORDER BY ts",
            (now_ts - 86400,),
        ).fetchall()

        # 5. HTTP error breakdown by status code
        http_err_rows = conn.execute(
            "SELECT json_extract(detail, '$.status') AS s, COUNT(*) AS n "
            "FROM telemetry_events WHERE ts > ? AND kind = 'http_error' GROUP BY s",
            (now_ts - 86400,),
        ).fetchall()

        # 6. Outbox snapshot
        outbox_row = conn.execute(
            "SELECT COUNT(*) AS depth, MIN(ts) AS oldest_ts FROM outbox WHERE status = 'queued'"
        ).fetchone()

        # 7. Companion-radio 1h samples
        radio_rows_1h = conn.execute(
            "SELECT * FROM radio_samples WHERE ts > ? ORDER BY ts",
            (now_ts - 3600,),
        ).fetchall()
        radio_1h = [dict(r) for r in radio_rows_1h]

        # 8. Battery (Victron BMV) 1h samples for sparklines, plus a dedicated
        # latest-row read. We deliberately do NOT derive the latest scalars from
        # power_1h[-1] (the way radio does): the BMV can go stale > 1h, and a
        # stale-but-real last value is more useful here than a blank tile.
        power_rows_1h = conn.execute(
            "SELECT ts, soc, voltage_mv FROM power_samples WHERE ts > ? ORDER BY ts",
            (now_ts - 3600,),
        ).fetchall()
        power_1h = [dict(r) for r in power_rows_1h]
        power_latest_row = conn.execute(
            "SELECT ts, soc, voltage_mv, current_ma, power_w "
            "FROM power_samples ORDER BY ts DESC LIMIT 1"
        ).fetchone()
        power_latest = dict(power_latest_row) if power_latest_row else {}
    finally:
        conn.close()

    # -- Build the result dict --
    latest = samples_1h[-1] if samples_1h else {}

    # CPU
    load_1h_values = [s["load_1m"] for s in samples_1h if s.get("load_1m") is not None]
    temp_1h_values = [s["cpu_temp_c"] for s in samples_1h if s.get("cpu_temp_c") is not None]
    throttled_bitmask = latest.get("throttled_bitmask")
    throttled_now = None
    if throttled_bitmask is not None:
        throttled_now = bool(throttled_bitmask & 0xF)

    # Memory
    mem_1h_values = [
        round(s["mem_available_kb"] / 1024)
        for s in samples_1h if s.get("mem_available_kb") is not None
    ]

    # Network rate from last two 1h samples
    rx_Bps, tx_Bps = 0, 0
    if len(samples_1h) >= 2:
        s_prev, s_last = samples_1h[-2], samples_1h[-1]
        dt = s_last["ts"] - s_prev["ts"]
        if dt > 0 and s_last.get("net_rx_bytes") is not None and s_prev.get("net_rx_bytes") is not None:
            drx = s_last["net_rx_bytes"] - s_prev["net_rx_bytes"]
            dtx = s_last["net_tx_bytes"] - s_prev["net_tx_bytes"]
            rx_Bps = max(0, drx) // dt
            tx_Bps = max(0, dtx) // dt

    # 24h hourly series: disk and net
    hourly_disk = {}  # bucket -> [free_kb values]
    # Store first and last cumulative net bytes per hourly bucket so we can
    # compute the delta *within* each bucket (not just between buckets).
    # This produces a chart point as soon as 2 samples exist in one hour.
    hourly_net = {}   # bucket -> {"first_rx", "first_tx", "last_rx", "last_tx", "dt"}
    for s in samples_24h:
        bucket = (s["ts"] // 3600) * 3600
        if s.get("disk_free_kb") is not None:
            hourly_disk.setdefault(bucket, []).append(s["disk_free_kb"])
        if s.get("net_rx_bytes") is not None:
            if bucket not in hourly_net:
                hourly_net[bucket] = {
                    "first_rx": s["net_rx_bytes"], "first_tx": s["net_tx_bytes"],
                    "first_ts": s["ts"],
                    "last_rx": s["net_rx_bytes"], "last_tx": s["net_tx_bytes"],
                    "last_ts": s["ts"],
                }
            else:
                hourly_net[bucket]["last_rx"] = s["net_rx_bytes"]
                hourly_net[bucket]["last_tx"] = s["net_tx_bytes"]
                hourly_net[bucket]["last_ts"] = s["ts"]

    # Disk: average free per hour bucket
    end_bucket = (now_ts // 3600) * 3600
    disk_24h_values = []
    for i in range(24):
        b = end_bucket - (23 - i) * 3600
        vals = hourly_disk.get(b)
        if vals:
            disk_24h_values.append(round(sum(vals) / len(vals) / 1024))
        else:
            disk_24h_values.append(None)

    # Net: intra-bucket byte deltas, normalized to bytes/sec.
    # For the current hour bucket, if only 1 sample exists (not enough for
    # an intra-bucket delta), fall back to the instantaneous rate computed
    # from the last two 1h samples above. This avoids a blank chart for the
    # first hour after boot.
    rx_24h_values = []
    tx_24h_values = []
    for i in range(24):
        b = end_bucket - (23 - i) * 3600
        hn = hourly_net.get(b)
        if hn and hn["last_ts"] > hn["first_ts"]:
            dt = hn["last_ts"] - hn["first_ts"]
            drx = max(0, hn["last_rx"] - hn["first_rx"])
            dtx = max(0, hn["last_tx"] - hn["first_tx"])
            rx_24h_values.append(drx // dt)
            tx_24h_values.append(dtx // dt)
        elif b == end_bucket and (rx_Bps or tx_Bps):
            # Current hour, single sample — use instantaneous rate
            rx_24h_values.append(rx_Bps)
            tx_24h_values.append(tx_Bps)
        else:
            rx_24h_values.append(None)
            tx_24h_values.append(None)

    # Outbox
    outbox_depth = outbox_row["depth"] if outbox_row else 0
    outbox_oldest_ts = outbox_row["oldest_ts"] if outbox_row else None
    outbox_oldest_age_s = (now_ts - outbox_oldest_ts) if outbox_oldest_ts is not None else None

    # Throttle events
    throttle_events_24h = []
    for r in throttle_rows:
        detail = r["detail"]
        if detail:
            try:
                d = json.loads(detail)
            except (json.JSONDecodeError, TypeError):
                d = {}
        else:
            d = {}
        throttle_events_24h.append({
            "ts": r["ts"],
            "old": d.get("old"),
            "new": d.get("new"),
            "changed_bits": d.get("changed_bits", []),
            "active_now": d.get("active_now", []),
        })

    # HTTP error breakdown
    http_errors = {}
    for r in http_err_rows:
        status_str = str(r["s"]) if r["s"] is not None else "unknown"
        http_errors[status_str] = r["n"]

    # Companion radio: latest scalars + 1h series for the chartable fields.
    radio_latest = radio_1h[-1] if radio_1h else {}
    rssi_1h_values = [s["last_rssi"] for s in radio_1h if s.get("last_rssi") is not None]
    snr_1h_values = [s["last_snr"] for s in radio_1h if s.get("last_snr") is not None]
    noise_1h_values = [s["noise_floor"] for s in radio_1h if s.get("noise_floor") is not None]
    queue_1h_values = [s["tx_queue_len"] for s in radio_1h if s.get("tx_queue_len") is not None]

    # Battery (Victron BMV): latest scalars from the dedicated LIMIT 1 read (so a
    # stale-but-real value survives), 1h series for the chartable fields. Each
    # field is independent — soc can be NULL while voltage is valid.
    power_soc_1h_values = [s["soc"] for s in power_1h if s.get("soc") is not None]
    power_voltage_1h_values = [s["voltage_mv"] for s in power_1h if s.get("voltage_mv") is not None]
    power_latest_ts = power_latest.get("ts")
    power_last_sample_age_s = (now_ts - power_latest_ts) if power_latest_ts is not None else None

    return {
        "now_ts": now_ts,
        "uptime_s": latest.get("uptime_s"),
        "cpu": {
            "load_1m": latest.get("load_1m"),
            "temp_c": latest.get("cpu_temp_c"),
            "throttled_now": throttled_now,
            "load_1h_series": {"sample_sec": 60, "values": load_1h_values},
            "temp_1h_series": {"sample_sec": 60, "values": temp_1h_values},
        },
        "mem": {
            "available_mb": round(latest["mem_available_kb"] / 1024) if latest.get("mem_available_kb") is not None else None,
            "total_mb": round(latest["mem_total_kb"] / 1024) if latest.get("mem_total_kb") is not None else None,
            "available_1h_series": {"sample_sec": 60, "values": mem_1h_values},
        },
        "disk": {
            "free_mb": round(latest["disk_free_kb"] / 1024) if latest.get("disk_free_kb") is not None else None,
            "total_mb": round(latest["disk_total_kb"] / 1024) if latest.get("disk_total_kb") is not None else None,
            "series_24h": {"sample_sec": 3600, "values": disk_24h_values},
        },
        "net": {
            "rx_now_Bps": rx_Bps,
            "tx_now_Bps": tx_Bps,
            "rx_24h": {"sample_sec": 3600, "values": rx_24h_values},
            "tx_24h": {"sample_sec": 3600, "values": tx_24h_values},
        },
        "outbox": {
            "depth_now": outbox_depth,
            "oldest_age_s": outbox_oldest_age_s,
        },
        "radio": {
            "battery_mv": radio_latest.get("battery_mv"),
            "uptime_s": radio_latest.get("radio_uptime_s"),
            "tx_queue_len": radio_latest.get("tx_queue_len"),
            "noise_floor": radio_latest.get("noise_floor"),
            "last_rssi": radio_latest.get("last_rssi"),
            "last_snr": radio_latest.get("last_snr"),
            "tx_air_secs": radio_latest.get("tx_air_secs"),
            "rx_air_secs": radio_latest.get("rx_air_secs"),
            "rssi_1h_series": {"sample_sec": 60, "values": rssi_1h_values},
            "snr_1h_series": {"sample_sec": 60, "values": snr_1h_values},
            "noise_1h_series": {"sample_sec": 60, "values": noise_1h_values},
            "queue_1h_series": {"sample_sec": 60, "values": queue_1h_values},
        },
        "power": {
            "soc": power_latest.get("soc"),
            "voltage_mv": power_latest.get("voltage_mv"),
            "current_ma": power_latest.get("current_ma"),
            "power_w": power_latest.get("power_w"),
            "last_sample_age_s": power_last_sample_age_s,
            "soc_1h_series": {"sample_sec": 60, "values": power_soc_1h_values},
            "voltage_1h_series": {"sample_sec": 60, "values": power_voltage_1h_values},
        },
        "throttle_events_24h": throttle_events_24h,
        "events_24h": {
            "rate_limit": event_counts.get("rate_limit", 0),
            "mac_mismatch": event_counts.get("mac_mismatch", 0),
            "http_errors": http_errors,
        },
    }


# ---------------------------------------------------------------------------
# Clock-correction helpers (CIV-99)
#
# /api/clock writes go through record_clock_report. The mesh_bot consensus
# task reads via get_eligible_clock_reports + read_clock_state_for_task,
# and writes via write_consensus_correction / write_external_step_correction.
# The civicmesh-set-clock admin command uses write_admin_clock_correction
# (under BEGIN EXCLUSIVE). All three writer functions bump vote_epoch for
# admin/external_step (within-boot invalidation); consensus does NOT bump
# vote_epoch because it doesn't invalidate reports — it applies them.
#
# See clock.py for the boot ID + offset + epoch read helpers used by callers
# OUTSIDE a write transaction, and docs/clock_consensus.md for the design.
# ---------------------------------------------------------------------------


@_retry_on_locked()
def record_clock_report(
    cfg: DBConfig,
    *,
    session_id: str,
    client_time: int,
    boot_id: str,
    log=None,
    _ts_for_test: Optional[int] = None,
    _mono_for_test: Optional[float] = None,
) -> int:
    """Persist one client clock report onto the session row.

    Stores:
      clock_offset_vote_sec    = client_time - int(time.time())    # raw delta
      clock_reported_system_ts = int(time.time())                  # for audit
      clock_report_mono        = time.monotonic()                  # for aging
      clock_report_boot_id     = boot_id                           # cross-boot gate
      clock_vote_epoch         = current clock_state.vote_epoch    # within-boot gate

    The raw time read happens INSIDE the BEGIN IMMEDIATE so an admin
    command's BEGIN EXCLUSIVE can't slip between the read and the UPDATE.
    Returns the computed offset vote (for logging / response).
    """
    conn = _connect(cfg)
    try:
        if log:
            log.debug("db:record_clock_report session=%s client_time=%d", session_id, client_time)
        conn.execute("BEGIN IMMEDIATE")
        try:
            raw_now = int(_ts_for_test) if _ts_for_test is not None else int(time.time())
            mono = float(_mono_for_test) if _mono_for_test is not None else time.monotonic()
            vote = int(client_time) - raw_now
            # vote_epoch read inside the txn so an admin/external_step
            # bump-and-NULL doesn't land between our read of the epoch
            # and our UPDATE (which would write the old epoch onto a
            # row the admin just cleared, defeating the gate).
            row = conn.execute(
                "SELECT value FROM clock_state WHERE key='vote_epoch'"
            ).fetchone()
            vote_epoch = int(row["value"]) if row else 0
            conn.execute(
                """
                UPDATE sessions
                SET clock_offset_vote_sec=?,
                    clock_reported_system_ts=?,
                    clock_report_mono=?,
                    clock_report_boot_id=?,
                    clock_vote_epoch=?
                WHERE session_id=?
                """,
                (vote, raw_now, mono, boot_id, vote_epoch, session_id),
            )
            conn.execute("COMMIT")
            return vote
        except:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.OperationalError:
                pass
            raise
    finally:
        conn.close()


def get_latest_clock_correction(cfg: DBConfig, *, log=None) -> Optional[dict[str, Any]]:
    """Return the most recent clock_corrections row, or None.

    Used by the consensus task at startup to seed last_seen_id and at each
    tick to detect newer rows since the last observation.
    """
    conn = _connect(cfg)
    try:
        row = conn.execute(
            "SELECT * FROM clock_corrections ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def has_consensus_correction_in_boot(cfg: DBConfig, *, current_boot_id: str, log=None) -> bool:
    """True iff a 'consensus' clock_corrections row exists this boot epoch.

    Per the design (docs/clock_consensus.md), `first_correction_done` is
    driven by 'consensus' rows only. 'admin' and 'external_step' rows do
    NOT consume the first-correction privilege — after either, the next
    consensus may legitimately need a large jump.

    Boot-epoch scope is by `applied_boot_id == current_boot_id`. Earlier
    revisions used `applied_at_monotonic <= time.monotonic()` for this,
    which is only sufficient — a prior-boot row with a small monotonic
    value silently passes once the current process has been up long
    enough. Identity comparison against the Linux boot ID has no such
    gap, mirroring the sessions.clock_report_boot_id design.

    NULL applied_boot_id (rows predating the column) does not match any
    current boot id, so they don't consume the privilege either —
    defensive and conservative.
    """
    conn = _connect(cfg)
    try:
        row = conn.execute(
            "SELECT 1 FROM clock_corrections "
            "WHERE trigger='consensus' AND applied_boot_id = ? LIMIT 1",
            (current_boot_id,),
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def get_eligible_clock_reports(
    cfg: DBConfig,
    *,
    boot_id: str,
    vote_epoch: int,
    mono_now: float,
    max_report_age_sec: int,
    wall_now_ts: int,
    min_cookie_age_sec: int,
    log=None,
) -> list[dict[str, Any]]:
    """Return per-session clock reports eligible for consensus this tick.

    Eligibility (all four must hold — see docs/clock_consensus.md):

      1. clock_offset_vote_sec IS NOT NULL.
      2. clock_report_boot_id = current boot id (cross-boot identity gate
         — pure equality on /proc/sys/kernel/random/boot_id captured at
         report time).
      3. clock_vote_epoch = current vote_epoch (within-boot generation
         gate — admin/external_step bump invalidates).
      4. clock_report_mono >= mono_now - max_report_age_sec (aging —
         compared against monotonic, not wall, so within-tick frame
         changes don't matter).
      5. wall_now_ts - created_ts >= min_cookie_age_sec (cookie age, wall
         frame — created_ts is wall-stamped).

    Result rows are ordered by clock_report_mono ASC so the caller's MAC
    dedupe (most-recent-wins) picks the freshest report per device.
    """
    conn = _connect(cfg)
    try:
        rows = conn.execute(
            """
            SELECT session_id, mac_address, clock_offset_vote_sec,
                   clock_report_mono, clock_reported_system_ts, created_ts
            FROM sessions
            WHERE clock_offset_vote_sec IS NOT NULL
              AND clock_report_boot_id = ?
              AND clock_vote_epoch = ?
              AND clock_report_mono >= ?
              AND (? - COALESCE(created_ts, 0)) >= ?
            ORDER BY clock_report_mono ASC
            """,
            (
                boot_id, int(vote_epoch),
                float(mono_now) - int(max_report_age_sec),
                int(wall_now_ts), int(min_cookie_age_sec),
            ),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@_retry_on_locked()
def evaluate_and_maybe_apply_consensus(
    cfg: DBConfig,
    *,
    boot_id: str,
    max_report_age_sec: int,
    min_cookie_age_sec: int,
    consensus_cfg,  # clock.ConsensusConfig — lazy-imported to avoid circular dep
    log=None,
) -> Optional[dict]:
    """Read state, evaluate consensus, and write the correction — all
    under ONE BEGIN IMMEDIATE. Returns a dict describing the accepted
    decision (or None for no-op / no-eligible / quorum fail / etc).

    THE ATOMICITY IS LOAD-BEARING. A naive split — read offset and
    vote_epoch and reports through separate connections, then call a
    separate writer — races against `civicmesh-set-clock`'s BEGIN
    EXCLUSIVE: admin can commit between the bot's reads and write,
    invalidating the votes and bumping vote_epoch, but the bot's write
    proceeds anyway with the stale snapshot and silently undoes the
    admin correction. The only safe serialization is holding the
    writer lock continuously from the offset read through the audit-
    row INSERT, so that the admin's BEGIN EXCLUSIVE either runs fully
    before us (we then see post-admin state — NULL reports, new epoch,
    no decision) or fully after us (we commit a correction the admin
    immediately overwrites — but at least we committed against a
    consistent snapshot).

    raw_now and mono_now are read INSIDE the transaction so they pair
    with the offset read under the same lock. Eligibility evaluates
    the boot ID equality, vote_epoch equality, monotonic age, and
    cookie age (in the corrected wall frame) consistently.

    No-op suppression is enforced here: nudge==0 commits the txn
    (releasing locks) and writes no audit row. See
    docs/clock_consensus.md § "Consensus math" and § "Centralized
    write helpers."
    """
    import clock as _clock  # lazy: clock imports DBConfig/_connect from us

    conn = _connect(cfg)
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            mono_now = time.monotonic()
            raw_now = int(time.time())

            offset_row = conn.execute(
                "SELECT value FROM clock_state WHERE key='offset_seconds'"
            ).fetchone()
            try:
                current_offset = int(offset_row["value"]) if offset_row else 0
            except (TypeError, ValueError):
                current_offset = 0

            ve_row = conn.execute(
                "SELECT value FROM clock_state WHERE key='vote_epoch'"
            ).fetchone()
            try:
                current_vote_epoch = int(ve_row["value"]) if ve_row else 0
            except (TypeError, ValueError):
                current_vote_epoch = 0

            # Boot scoping via applied_boot_id, NOT applied_at_monotonic.
            # A prior-boot consensus row whose monotonic happened to be
            # small (e.g. 60s into that boot) would silently pass a
            # `monotonic <= mono_now` check once the current process has
            # been up for >60s. Identity comparison against the current
            # boot ID has no such gap.
            fcd_row = conn.execute(
                "SELECT 1 FROM clock_corrections "
                "WHERE trigger='consensus' AND applied_boot_id = ? LIMIT 1",
                (boot_id,),
            ).fetchone()
            first_correction_done = fcd_row is not None

            corrected_wall = raw_now + current_offset
            report_rows = conn.execute(
                """
                SELECT session_id, mac_address, clock_offset_vote_sec
                FROM sessions
                WHERE clock_offset_vote_sec IS NOT NULL
                  AND clock_report_boot_id = ?
                  AND clock_vote_epoch = ?
                  AND clock_report_mono >= ?
                  AND (? - COALESCE(created_ts, 0)) >= ?
                ORDER BY clock_report_mono ASC
                """,
                (
                    boot_id, current_vote_epoch,
                    float(mono_now) - int(max_report_age_sec),
                    corrected_wall, int(min_cookie_age_sec),
                ),
            ).fetchall()

            if not report_rows:
                # DEBUG-only: tick fired but no eligible reports. Common
                # while waiting for quorum or for cookie age to mature;
                # silent at default INFO so it doesn't clutter prod logs.
                # See docs/clock_consensus.md § "Tracing consensus".
                if log:
                    log.debug(
                        "clock:tick_no_eligible_reports boot_id=%s vote_epoch=%d "
                        "current_offset=%d first_correction_done=%s",
                        boot_id, current_vote_epoch, current_offset,
                        first_correction_done,
                    )
                conn.execute("COMMIT")
                return None

            reports = [
                _clock.Report(
                    session_id=r["session_id"],
                    mac_address=r["mac_address"],
                    offset_vote_sec=int(r["clock_offset_vote_sec"]),
                )
                for r in report_rows
            ]
            decision = _clock.evaluate_consensus(
                reports,
                current_offset=current_offset,
                raw_now=raw_now,
                first_correction_done=first_correction_done,
                cfg=consensus_cfg,
            )

            if decision is None:
                # DEBUG-only: reports were eligible but evaluate_consensus
                # rejected. From the caller side we can't distinguish
                # quorum / sanity / acceptance-rule failure without
                # changing the pure function's return shape; the count +
                # context is usually enough for dev triage.
                if log:
                    log.debug(
                        "clock:tick_no_decision eligible=%d quorum_min=%d "
                        "current_offset=%d first_correction_done=%s "
                        "(quorum / sanity / acceptance rule rejected — "
                        "increase eligible reports, check sanity bounds, "
                        "or check whether a forward-only or max_nudge_sec "
                        "constraint applies)",
                        len(reports), consensus_cfg.quorum_min_cookies,
                        current_offset, first_correction_done,
                    )
                conn.execute("COMMIT")
                return None

            if decision.nudge == 0:
                # DEBUG-only: consensus produced the same offset that's
                # already stored — common in steady state. No audit row,
                # no telemetry, per the no-op suppression policy.
                if log:
                    log.debug(
                        "clock:tick_noop eligible=%d candidate_offset=%d "
                        "current_offset=%d (nudge=0)",
                        len(reports), int(decision.new_offset), current_offset,
                    )
                conn.execute("COMMIT")
                return None

            # Apply: bump offset, append audit row. Both system_time_*
            # columns get raw_now — consensus does not touch the OS clock,
            # so time.time() reads the same before and after. See the
            # clock_corrections schema comment for per-trigger meanings.
            conn.execute(
                "UPDATE clock_state SET value=? WHERE key='offset_seconds'",
                (str(int(decision.new_offset)),),
            )
            cur = conn.execute(
                """
                INSERT INTO clock_corrections
                  (applied_at_monotonic, applied_boot_id,
                   system_time_before, system_time_after,
                   offset_before_sec, offset_after_sec,
                   trigger, voter_count, median_offset_vote_sec, source_summary)
                VALUES (?, ?, ?, ?, ?, ?, 'consensus', ?, ?, ?)
                """,
                (mono_now, boot_id, raw_now, raw_now,
                 current_offset, int(decision.new_offset),
                 int(decision.voter_count),
                 int(decision.median_offset_vote_sec),
                 _clock.summary_to_json(decision.source_summary)),
            )
            new_id = int(cur.lastrowid)
            conn.execute("COMMIT")
            if log:
                log.info(
                    "clock:consensus_accepted id=%d offset_before=%d offset_after=%d "
                    "voter_count=%d nudge=%d reason=%s",
                    new_id, current_offset, int(decision.new_offset),
                    int(decision.voter_count), int(decision.nudge),
                    decision.accept_reason,
                )
            return {
                "id": new_id,
                "offset_before_sec": current_offset,
                "offset_after_sec": int(decision.new_offset),
                "nudge_sec": int(decision.nudge),
                "voter_count": int(decision.voter_count),
                "median_offset_vote_sec": int(decision.median_offset_vote_sec),
                "accept_reason": decision.accept_reason,
            }
        except:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.OperationalError:
                pass
            raise
    finally:
        conn.close()


@_retry_on_locked()
def write_consensus_correction(
    cfg: DBConfig,
    *,
    new_offset: int,
    voter_count: int,
    median_offset_vote_sec: int,
    source_summary_json: str,
    applied_boot_id: str,
    log=None,
) -> int:
    """Lower-level consensus writer used by tests and the migration path.

    Production callers MUST use `evaluate_and_maybe_apply_consensus`
    instead — it folds the offset/vote_epoch/reports read AND the
    write into a single BEGIN IMMEDIATE so the admin command's
    BEGIN EXCLUSIVE can't invalidate the snapshot between read and
    write. Splitting them (as this function alone does) reintroduces
    that race; this helper survives because the unit tests for the
    schema-level behavior need a direct entry point.

    `applied_boot_id` is required: tests pass an explicit value (often
    `clock.get_boot_id()` or a fabricated string like "test-boot-1"),
    making boot-scoping behavior testable without depending on
    /proc state.

    Runs under BEGIN IMMEDIATE. Caller MUST have already verified
    `nudge != 0` (no-op suppression policy) before calling — this
    function unconditionally writes when invoked. Does NOT bump
    vote_epoch (consensus applies votes, doesn't invalidate them).
    Both system_time_before and system_time_after are stored as
    int(time.time()) — see the clock_corrections schema comment for
    per-trigger meaning. Returns the new clock_corrections.id.
    """
    conn = _connect(cfg)
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT value FROM clock_state WHERE key='offset_seconds'"
            ).fetchone()
            offset_before = int(row["value"]) if row else 0
            raw_now = int(time.time())
            mono = time.monotonic()
            conn.execute(
                "UPDATE clock_state SET value=? WHERE key='offset_seconds'",
                (str(int(new_offset)),),
            )
            cur = conn.execute(
                """
                INSERT INTO clock_corrections
                  (applied_at_monotonic, applied_boot_id,
                   system_time_before, system_time_after,
                   offset_before_sec, offset_after_sec,
                   trigger, voter_count, median_offset_vote_sec, source_summary)
                VALUES (?, ?, ?, ?, ?, ?, 'consensus', ?, ?, ?)
                """,
                (mono, applied_boot_id, raw_now, raw_now,
                 offset_before, int(new_offset),
                 int(voter_count), int(median_offset_vote_sec), source_summary_json),
            )
            new_id = int(cur.lastrowid)
            conn.execute("COMMIT")
            if log:
                log.info(
                    "clock:consensus_accepted id=%d offset_before=%d offset_after=%d voter_count=%d",
                    new_id, offset_before, int(new_offset), int(voter_count),
                )
            return new_id
        except:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.OperationalError:
                pass
            raise
    finally:
        conn.close()


@_retry_on_locked()
def write_external_step_correction(
    cfg: DBConfig,
    *,
    source_summary_json: str,
    applied_boot_id: str,
    log=None,
) -> int:
    """Apply an external-clock-step rebase: offset->0, vote_epoch bump,
    NULL session vote columns, append audit row.

    `applied_boot_id` is required so the audit row can be boot-scoped
    later (mainly for stats display; first_correction_done filters by
    trigger='consensus' only, so external_step rows don't otherwise
    use the column).

    Runs under BEGIN IMMEDIATE on the consensus task's connection.
    Returns the new clock_corrections.id.
    """
    conn = _connect(cfg)
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT value FROM clock_state WHERE key='offset_seconds'"
            ).fetchone()
            offset_before = int(row["value"]) if row else 0
            system_time_before = int(time.time())
            mono = time.monotonic()
            conn.execute(
                "UPDATE clock_state SET value='0' WHERE key='offset_seconds'"
            )
            conn.execute(
                "UPDATE clock_state SET value=CAST(CAST(value AS INTEGER)+1 AS TEXT) "
                "WHERE key='vote_epoch'"
            )
            conn.execute(
                """
                UPDATE sessions SET
                  clock_offset_vote_sec=NULL,
                  clock_reported_system_ts=NULL,
                  clock_report_mono=NULL,
                  clock_report_boot_id=NULL,
                  clock_vote_epoch=NULL
                """
            )
            cur = conn.execute(
                """
                INSERT INTO clock_corrections
                  (applied_at_monotonic, applied_boot_id,
                   system_time_before, system_time_after,
                   offset_before_sec, offset_after_sec,
                   trigger, voter_count, median_offset_vote_sec, source_summary)
                VALUES (?, ?, ?, ?, ?, 0, 'external_step', NULL, NULL, ?)
                """,
                (mono, applied_boot_id,
                 system_time_before, system_time_before,
                 offset_before, source_summary_json),
            )
            new_id = int(cur.lastrowid)
            conn.execute("COMMIT")
            if log:
                log.warning(
                    "clock:external_step id=%d offset_before=%d", new_id, offset_before,
                )
            return new_id
        except:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.OperationalError:
                pass
            raise
    finally:
        conn.close()


def cross_boot_storage_hygiene(
    cfg: DBConfig,
    *,
    current_boot_id: str,
    log=None,
) -> int:
    """NULL session clock-report columns on rows whose boot_id ≠ current.

    Optional storage hygiene; the boot-id eligibility filter
    (get_eligible_clock_reports) already excludes prior-boot rows from
    consensus, so this is purely about keeping the table tidy across
    reboots. Does NOT bump vote_epoch — boot_id mismatch alone gates
    cross-boot reports, no within-boot generation change is needed.

    Returns the number of rows updated.
    """
    conn = _connect(cfg)
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            cur = conn.execute(
                """
                UPDATE sessions SET
                  clock_offset_vote_sec=NULL,
                  clock_reported_system_ts=NULL,
                  clock_report_mono=NULL,
                  clock_report_boot_id=NULL,
                  clock_vote_epoch=NULL
                WHERE clock_report_boot_id IS NOT NULL
                  AND clock_report_boot_id != ?
                """,
                (current_boot_id,),
            )
            n = int(cur.rowcount or 0)
            conn.execute("COMMIT")
            if n and log:
                log.info("clock:cross_boot_hygiene cleared=%d", n)
            return n
        except:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.OperationalError:
                pass
            raise
    finally:
        conn.close()
