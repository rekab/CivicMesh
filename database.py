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
    timeout_sec: float = 5.0


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
    now_ts: Optional[int] = None,
    log=None,
) -> None:
    now_ts = int(now_ts or time.time())
    conn = _connect(cfg)
    try:
        if log:
            log.debug("status:upsert process=%s connected=%s state=%s ts=%d",
                       process, bool(radio_connected), state, now_ts)
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
    ts: int,
    channel: str,
    sender: str,
    content: str,
    session_id: str,
    fingerprint: Optional[str] = None,
    max_queue_depth: int,
    log=None,
) -> Optional[tuple[int, int]]:
    """Atomically create an outbox row and its linked messages row.

    Uses an explicit transaction so mesh_bot cannot see the outbox row
    before the messages row exists — prevents the race where
    update_message_status matches 0 rows.

    Refuses the insert and returns None if the queued depth is already
    at max_queue_depth (the relay-wide outbox cap, distinct from the
    per-session post quota). The depth check + INSERT run inside a
    single BEGIN IMMEDIATE transaction so two ThreadingHTTPServer
    workers cannot both pass the cap check concurrently."""
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
        conn.execute("ROLLBACK")
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
    retries don't overwrite the timestamp from the first attempt."""
    conn = _connect(cfg)
    try:
        conn.execute(
            "UPDATE outbox SET sender_ts = ? WHERE id = ? AND sender_ts IS NULL",
            (int(sender_ts), int(outbox_id)),
        )
    finally:
        conn.close()


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
    ts: int,
    log=None,
) -> None:
    """Record one observed echo against an outbox row.

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
    now_ts: Optional[int] = None,
    log=None,
) -> None:
    now_ts = int(now_ts or time.time())
    conn = _connect(cfg)
    try:
        if log:
            log.debug("db:create_or_update_session session=%s mac=%s", session_id, mac_address or "")
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


def record_post_for_session(cfg: DBConfig, *, session_id: str, now_ts: Optional[int] = None, log=None) -> None:
    now_ts = int(now_ts or time.time())
    conn = _connect(cfg)
    try:
        if log:
            log.debug("db:record_post_for_session session=%s ts=%d", session_id, now_ts)
        conn.execute(
            """
            UPDATE sessions
            SET last_post_ts=?,
                post_count_hour=COALESCE(post_count_hour,0)+1
            WHERE session_id=?
            """,
            (now_ts, session_id),
        )
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


def update_vote(cfg: DBConfig, *, message_id: int, session_id: str, vote_type: int, ts: int, log=None) -> None:
    """
    vote_type: 1=upvote, -1=downvote, 0=remove
    """
    if vote_type not in (-1, 0, 1):
        raise ValueError("vote_type must be -1, 0, or 1")
    conn = _connect(cfg)
    try:
        if log:
            log.debug("db:update_vote msg=%d session=%s vote=%d", message_id, session_id, vote_type)
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


def insert_heard_packet(cfg: DBConfig, *, ts, payload_type, route_type, path_len,
                        last_path_byte=None, snr=None, rssi=None, log=None):
    conn = _connect(cfg)
    try:
        conn.execute(
            "INSERT INTO heard_packets (ts, payload_type, route_type, path_len, last_path_byte, snr, rssi) "
            "VALUES (?,?,?,?,?,?,?)",
            (ts, payload_type, route_type, path_len, last_path_byte, snr, rssi),
        )
    finally:
        conn.close()


TOUCH_DEBOUNCE_SEC = 30  # skip write if last_seen_ts is already within this window


def touch_session_last_seen(cfg: DBConfig, session_id, ts, log=None):
    conn = _connect(cfg)
    try:
        conn.execute(
            "UPDATE sessions SET last_seen_ts = ? "
            "WHERE session_id = ? AND (last_seen_ts IS NULL OR last_seen_ts < ? - ?)",
            (ts, session_id, ts, TOUCH_DEBOUNCE_SEC),
        )
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

        result = {
            "now_ts": now_ts,
            "wifi_sessions": wifi,
            "messages_seen": seen,
            "messages_sent": sent,
            "messages_failed": failed,
            "direct_repeaters": repeaters,
            "radio_restarts": restarts,
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
    ts: int,
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
) -> None:
    conn = _connect(cfg)
    try:
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
    finally:
        conn.close()


def insert_telemetry_event(
    cfg: DBConfig,
    *,
    ts: int,
    kind: str,
    detail: Optional[dict] = None,
    log=None,
) -> None:
    conn = _connect(cfg)
    try:
        detail_json = json.dumps(detail) if detail is not None else None
        conn.execute(
            "INSERT INTO telemetry_events (ts, kind, detail) VALUES (?,?,?)",
            (ts, kind, detail_json),
        )
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
        "throttle_events_24h": throttle_events_24h,
        "events_24h": {
            "rate_limit": event_counts.get("rate_limit", 0),
            "mac_mismatch": event_counts.get("mac_mismatch", 0),
            "http_errors": http_errors,
        },
    }
